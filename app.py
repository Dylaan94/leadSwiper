#!/usr/bin/env python3
"""Lead Swiper - load a CSV of websites, review them one by one, keep or remove.

Run:  python3 app.py      then open http://localhost:8765
See:  python3 app.py --help   for tuning options
"""
import argparse
import csv
import errno
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_VERSION = 5  # bump together with the matching constant in static/app.js whenever the API changes
BASE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(BASE, "shots")
SESSIONS = os.path.join(BASE, "sessions")
OUTPUT = os.path.join(BASE, "output")
STATE_FILE = os.path.join(BASE, "state.json")
parser = argparse.ArgumentParser(description="Lead Swiper - review a CSV of websites one by one.")
parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")), help="port to serve on (default 8765)")
parser.add_argument("--workers", type=int, default=3, help="parallel browser tabs taking screenshots (default 3)")
parser.add_argument("--wait", type=float, default=None, help="seconds to wait after a page loads before screenshotting, for loading screens and animations (default 1.5, remembered between runs)")
parser.add_argument("--timeout", type=float, default=12, help="seconds to give a page to load before screenshotting whatever rendered (default 12)")
parser.add_argument("--prefetch", type=int, default=8, help="how many sites ahead of you to screenshot in the background (default 8)")
parser.add_argument("--full", action="store_true", default=None, help="capture the whole homepage instead of just the first screen (slower, bigger images)")
parser.add_argument("--width", type=int, default=1280, help="browser viewport width (default 1280)")
parser.add_argument("--height", type=int, default=800, help="browser viewport height (default 800)")
args = parser.parse_args()

PORT = args.port
PREFETCH_AHEAD = args.prefetch
WORKERS = max(1, args.workers)
NAV_TIMEOUT_MS = int(args.timeout * 1000)
SHOT_TIMEOUT_MS = 8000
FULL_SHOT_TIMEOUT_MS = 20000
MAX_FULL_HEIGHT = 8000      # px; stops infinite-scroll pages producing enormous images
VIEWPORT = {"width": args.width, "height": args.height}

os.makedirs(SHOTS, exist_ok=True)
os.makedirs(SESSIONS, exist_ok=True)
os.makedirs(OUTPUT, exist_ok=True)

# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
lock = threading.Lock()
wake = threading.Event()
SESSION_FIELDS = ("id", "filename", "header", "rows", "url_col", "decisions", "cursor", "created", "updated", "out_base")

state = {
    "id": None,          # current sheet id (file in sessions/), None when nothing is open
    "filename": None,
    "header": [],
    "rows": [],          # list of lists
    "url_col": None,     # index into header
    "decisions": {},     # str(index) -> "keep" | "remove"
    "cursor": 0,
    "created": None,
    "updated": None,
    "out_base": None,    # output file prefix, unique across sheets
    "settings": {"wait": 1.5 if args.wait is None else args.wait, "full": bool(args.full)},
}


def session_path(sid):
    return os.path.join(SESSIONS, sid + ".json")


def session_summary(d):
    dec = d.get("decisions", {})
    return {
        "id": d.get("id"), "filename": d.get("filename"), "total": len(d.get("rows", [])),
        "kept": sum(1 for v in dec.values() if v == "keep"),
        "removed": sum(1 for v in dec.values() if v == "remove"),
        "created": d.get("created"), "updated": d.get("updated"),
    }


def list_sessions():
    out = []
    for name in os.listdir(SESSIONS):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS, name)) as f:
                d = json.load(f)
            d.setdefault("id", name[:-5])
            out.append(session_summary(d))
        except Exception as e:  # noqa
            print("Skipping unreadable session", name, e)
    out.sort(key=lambda x: x.get("updated") or "", reverse=True)
    return out


def new_session_id(filename):
    base = re.sub(r"[^a-z0-9]+", "-", os.path.splitext(os.path.basename(filename or "leads"))[0].lower()).strip("-") or "leads"
    sid = f"{base}-{time.strftime('%Y%m%d-%H%M%S')}"
    n, candidate = 1, sid
    while os.path.exists(session_path(candidate)):
        n += 1
        candidate = f"{sid}-{n}"
    return candidate


def unique_out_base(filename):
    base = os.path.splitext(os.path.basename(filename or "leads.csv"))[0]
    taken = set()
    for name in os.listdir(SESSIONS):
        if name.endswith(".json"):
            try:
                with open(os.path.join(SESSIONS, name)) as f:
                    taken.add(json.load(f).get("out_base"))
            except Exception:
                pass
    if base not in taken:
        return base
    return base + "_" + time.strftime("%Y-%m-%d_%H%M")


def sheet_urls(d):
    col = d.get("url_col")
    if col is None:
        return set()
    return {normalize_url(r[col]) for r in d.get("rows", []) if col < len(r) and normalize_url(r[col])}


def delete_sheet_shots(sid):
    """Remove cached screenshots for a sheet's sites, unless another sheet still uses them. Returns count removed."""
    with open(session_path(sid)) as f:
        mine = sheet_urls(json.load(f))
    still_used = set()
    for name in os.listdir(SESSIONS):
        if name.endswith(".json") and name[:-5] != sid:
            try:
                with open(os.path.join(SESSIONS, name)) as f:
                    still_used |= sheet_urls(json.load(f))
            except Exception:
                pass
    n = 0
    for url in mine - still_used:
        for full in (False, True):
            for path in shot_paths(url, full):
                if os.path.exists(path):
                    os.remove(path)
                    n += 1
    return n


def clear_current():
    state.update({"id": None, "filename": None, "header": [], "rows": [], "url_col": None,
                  "decisions": {}, "cursor": 0, "created": None, "updated": None, "out_base": None})


def open_session(sid):
    with open(session_path(sid)) as f:
        d = json.load(f)
    clear_current()
    for k in SESSION_FIELDS:
        if k in d:
            state[k] = d[k]
    state["id"] = sid
    if not state.get("out_base"):
        state["out_base"] = os.path.splitext(os.path.basename(state["filename"] or "leads.csv"))[0]
wanted = []  # (url, wait, full) explicitly requested by the UI; served before prefetch
in_progress = set()  # (url, full) pairs a worker is currently capturing


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def save_state():
    """state.json holds settings + which sheet is open; each sheet lives in sessions/<id>.json."""
    if state["id"]:
        state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _write_json(session_path(state["id"]), {k: state[k] for k in SESSION_FIELDS})
    _write_json(STATE_FILE, {"current": state["id"], "settings": state["settings"]})


def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
    except Exception as e:  # noqa
        print("Could not load state.json:", e)
        return
    settings = saved.get("settings") or {}
    settings.setdefault("wait", 1.5)
    settings.setdefault("full", False)
    if args.wait is not None:  # an explicit flag beats the saved setting
        settings["wait"] = args.wait
    if args.full:
        settings["full"] = True
    state["settings"] = settings
    if saved.get("rows"):
        # old single-sheet format: move it into sessions/ so it shows up in the sheet list
        sid = new_session_id(saved.get("filename"))
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        sheet = {k: saved.get(k) for k in SESSION_FIELDS if k in saved}
        sheet.update({"id": sid, "created": now, "updated": now,
                      "out_base": os.path.splitext(os.path.basename(saved.get("filename") or "leads.csv"))[0]})
        sheet.setdefault("decisions", {}); sheet.setdefault("cursor", 0)
        _write_json(session_path(sid), sheet)
        saved["current"] = sid
        print(f"Migrated existing sheet to sessions/{sid}.json")
    cur = saved.get("current")
    if cur and os.path.exists(session_path(cur)):
        try:
            open_session(cur)
        except Exception as e:  # noqa
            print("Could not open sheet", cur, e)
            clear_current()
    save_state()


URL_HEADER_EXACT = ("website", "web site", "url", "domain", "homepage", "home page", "site", "web")
URL_HEADER_RE = re.compile(r"website|url|domain|homepage|site|web", re.I)
SOCIAL_RE = re.compile(r"linkedin|facebook|instagram|twitter|tiktok|youtube|pinterest", re.I)
DOMAIN_RE = re.compile(r"^(https?://)?([\w-]+\.)+[a-z]{2,}(/.*)?$", re.I)


def detect_url_column(header, rows):
    names = [(h or "").strip().lower() for h in header]
    for i, h in enumerate(names):            # exact header names first
        if h in URL_HEADER_EXACT:
            return i
    for i, h in enumerate(names):            # then fuzzy, skipping social-profile columns
        if URL_HEADER_RE.search(h) and not SOCIAL_RE.search(h):
            return i
    best, best_score = None, 0
    for i in range(len(header)):
        score = sum(1 for r in rows[:50] if i < len(r) and DOMAIN_RE.match(r[i].strip()))
        if score > best_score:
            best, best_score = i, score
    return best


# Encodings to try, in order, when the CSV isn't UTF-8. The UI language moves its usual
# Excel export encoding to the front: Japanese Excel writes Shift_JIS (cp932), Chinese GBK or Big5.
ENCODING_FALLBACKS = {
    "ja": ["cp932", "euc_jp", "gbk", "big5", "cp1252"],
    "zh-Hant": ["big5", "gbk", "cp932", "cp1252"],
    "zh-Hans": ["gbk", "big5", "cp932", "cp1252"],
}
DEFAULT_FALLBACKS = ["cp1252", "cp932", "gbk", "big5"]


def decode_csv(raw, lang):
    """Returns (text, encoding). UTF-8 (with or without BOM) and UTF-16 are recognised outright;
    otherwise the first legacy encoding that decodes cleanly wins."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in ENCODING_FALLBACKS.get(lang, DEFAULT_FALLBACKS):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8 (with replacement characters)"


def normalize_url(raw):
    u = (raw or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def shot_key(url):
    return hashlib.sha1(url.encode()).hexdigest()[:20]


def shot_paths(url, full=False):
    k = shot_key(url) + ("-full" if full else "")
    return os.path.join(SHOTS, k + ".png"), os.path.join(SHOTS, k + ".fail")


def row_url(i):
    r = state["rows"][i]
    c = state["url_col"]
    return normalize_url(r[c]) if c is not None and c < len(r) else ""


# ----------------------------------------------------------------------------
# Screenshot worker (single thread owns Playwright)
# ----------------------------------------------------------------------------
def _needs_shot(url, full):
    png, fail = shot_paths(url, full)
    return (url, full) not in in_progress and not os.path.exists(png) and not os.path.exists(fail)


def next_job():
    """Returns (url, wait_seconds, full_page) or None."""
    with lock:
        while wanted:
            url, wait, full = wanted.pop(0)
            if _needs_shot(url, full):
                in_progress.add((url, full))
                return url, wait, full
        n = len(state["rows"])
        full = bool(state["settings"].get("full"))
        start = state["cursor"]
        for i in range(start, min(n, start + PREFETCH_AHEAD)):
            if str(i) in state["decisions"]:
                continue
            url = row_url(i)
            if not url:
                continue
            if _needs_shot(url, full):
                in_progress.add((url, full))
                return url, state["settings"]["wait"], full
    return None


def capture(page, url, wait, full=False):
    png, fail = shot_paths(url, full)
    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[len("https://"):])
    last_err = "unknown"
    for cand in candidates:
        try:
            try:
                page.goto(cand, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                page.wait_for_timeout(int(wait * 1000))
            except Exception as e:
                if "timeout" not in str(e).lower():
                    raise
                # page is still loading: freeze it so the screenshot doesn't wait on fonts/resources
                try:
                    page.evaluate("window.stop()")
                except Exception:
                    pass
            if full:
                # scroll through the page first so lazy-loaded images below the fold actually load
                try:
                    page.evaluate("""async (maxH) => {
                        const h = Math.min(document.documentElement.scrollHeight, maxH);
                        for (let y = 0; y < h; y += 600) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 80)); }
                        window.scrollTo(0, 0);
                    }""", MAX_FULL_HEIGHT)
                    page.wait_for_timeout(600)
                except Exception:
                    pass
                try:
                    h = int(page.evaluate("Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)"))
                except Exception:
                    h = VIEWPORT["height"]
                h = max(VIEWPORT["height"], min(h, MAX_FULL_HEIGHT))
                page.screenshot(path=png + ".tmp.png", full_page=True, timeout=FULL_SHOT_TIMEOUT_MS,
                                clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": h})
            else:
                page.screenshot(path=png + ".tmp.png", full_page=False, timeout=SHOT_TIMEOUT_MS)
            os.replace(png + ".tmp.png", png)
            return
        except Exception as e:
            last_err = str(e).splitlines()[0][:300]
            # only fall back to http:// if https never connected at all
            if "net::ERR" not in last_err:
                break
    with open(fail, "w") as f:
        f.write(last_err)


USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def worker(n):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if n == 0:
            print("\n!! playwright not installed. Run:  pip3 install playwright && python3 -m playwright install chromium\n")
        return
    while True:  # if Chromium dies, relaunch rather than silently losing a worker
        try:
            worker_loop(n, sync_playwright)
        except Exception as e:  # noqa
            print(f"  worker {n + 1} browser error ({str(e).splitlines()[0][:120]}); restarting in 3s")
            time.sleep(3)


def worker_loop(n, sync_playwright):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport=VIEWPORT, user_agent=USER_AGENT, ignore_https_errors=True)
        page = ctx.new_page()
        print(f"Screenshot worker {n + 1} ready.")
        while True:
            job = next_job()
            if not job:
                wake.clear()
                wake.wait(timeout=1.0)
                continue
            url, wait, full = job
            t = time.time()
            try:
                capture(page, url, wait, full)
            except Exception as e:
                print(f"  worker {n + 1} error on {url}: {e}")
            finally:
                with lock:
                    in_progress.discard((url, full))
            png, _ = shot_paths(url, full)
            print(f"  {'shot' if os.path.exists(png) else 'FAIL'}{' [full]' if full else ''} {url}  ({time.time() - t:.1f}s)")
            # fresh page each time keeps memory in check and drops any stuck loads
            try:
                page.close()
            except Exception:
                pass
            try:
                page = ctx.new_page()
            except Exception:
                ctx = browser.new_context(viewport=VIEWPORT, user_agent=USER_AGENT, ignore_https_errors=True)
                page = ctx.new_page()


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def public_state():
    n = len(state["rows"])
    kept = sum(1 for v in state["decisions"].values() if v == "keep")
    removed = sum(1 for v in state["decisions"].values() if v == "remove")
    return {
        "loaded": n > 0,
        "id": state["id"],
        "filename": state["filename"],
        "total": n,
        "kept": kept,
        "removed": removed,
        "cursor": state["cursor"],
        "header": state["header"],
        "url_col": state["url_col"],
        "decided": sorted(int(k) for k in state["decisions"]),
        "settings": state["settings"],
        "workers": WORKERS,
        "version": APP_VERSION,
    }


def row_payload(i):
    r = state["rows"][i]
    return {
        "index": i,
        "url": row_url(i),
        "fields": {h: (r[j] if j < len(r) else "") for j, h in enumerate(state["header"])},
        "decision": state["decisions"].get(str(i)),
    }


def write_outputs():
    base = state.get("out_base") or os.path.splitext(os.path.basename(state["filename"] or "leads.csv"))[0]
    kept_path = os.path.join(OUTPUT, base + "_kept.csv")
    removed_path = os.path.join(OUTPUT, base + "_removed.csv")
    for path, want in ((kept_path, "keep"), (removed_path, "remove")):
        # utf-8-sig: the BOM makes Excel on Windows open non-ASCII text correctly
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(state["header"])
            for i, r in enumerate(state["rows"]):
                if state["decisions"].get(str(i)) == want:
                    w.writerow(r)
    return kept_path, removed_path


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter log
        if args and "/shot/" in str(args[0]):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # row numbers are reused across sheets; never cache
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype, download_name=None):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            ascii_name = download_name.encode("ascii", "replace").decode().replace('"', "")
            utf8_name = urllib.parse.quote(download_name)
            self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/":
            return self._file(os.path.join(BASE, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            # only plain files directly inside static/, no traversal
            name = path[len("/static/"):]
            if not re.match(r"^[\w.-]+$", name) or name.startswith("."):
                self.send_error(404)
                return
            f = os.path.join(BASE, "static", name)
            if not os.path.isfile(f):
                self.send_error(404)
                return
            ctype = {"css": "text/css; charset=utf-8", "js": "application/javascript; charset=utf-8",
                     "svg": "image/svg+xml", "png": "image/png"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
            return self._file(f, ctype)
        if path in ("/logo", "/logomark"):
            # serve logo.<ext> / logoCat.<ext> from the project folder if present; the page hides the image otherwise
            stem = "logo" if path == "/logo" else "logoCat"
            for ext, ctype in (("svg", "image/svg+xml"), ("png", "image/png"), ("jpg", "image/jpeg"),
                               ("jpeg", "image/jpeg"), ("webp", "image/webp")):
                f = os.path.join(BASE, stem + "." + ext)
                if os.path.exists(f):
                    return self._file(f, ctype)
            self.send_error(404)
            return
        if path == "/state":
            with lock:
                return self._json(public_state())
        m = re.match(r"^/row/(\d+)$", path)
        if m:
            i = int(m.group(1))
            with lock:
                if i >= len(state["rows"]):
                    return self._json({"error": "out of range"}, 404)
                return self._json(row_payload(i))
        m = re.match(r"^/shot/(\d+)$", path)
        if m:
            i = int(m.group(1))
            with lock:
                if i >= len(state["rows"]):
                    return self._json({"error": "out of range"}, 404)
                url = row_url(i)
            if not url:
                return self._json({"error": "no url"}, 404)
            full = query.get("full", ["0"])[0] == "1"
            png, fail = shot_paths(url, full)
            if os.path.exists(png):
                return self._file(png, "image/png")
            if os.path.exists(fail):
                with open(fail) as f:
                    return self._json({"error": f.read()}, 410)
            with lock:
                if not any(u == url and f == full for u, _, f in wanted):
                    wanted.append((url, state["settings"]["wait"], full))
            wake.set()
            return self._json({"pending": True}, 202)
        if path == "/sessions":
            with lock:
                return self._json({"sessions": list_sessions(), "current": state["id"]})
        if path == "/export":
            with lock:
                if not state["rows"]:
                    return self._json({"error": "nothing loaded"}, 400)
                kept_path, _ = write_outputs()
            return self._file(kept_path, "text/csv", os.path.basename(kept_path))
        self.send_error(404)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(n)

    def do_POST(self):
        # a web page in another tab could fire POSTs at localhost; only accept our own origin
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).hostname not in ("localhost", "127.0.0.1"):
            return self._json({"error": "cross-origin request refused"}, 403)
        path = urllib.parse.urlparse(self.path).path
        if path == "/upload":
            name = urllib.parse.unquote(self.headers.get("X-Filename", "leads.csv")) or "leads.csv"
            lang = self.headers.get("X-Lang", "en")
            text, encoding = decode_csv(self._read_body(), lang)
            print(f"Upload {name!r} decoded as {encoding}")
            reader = list(csv.reader(io.StringIO(text)))
            reader = [r for r in reader if any(c.strip() for c in r)]
            if len(reader) < 2:
                return self._json({"error": "CSV needs a header row and at least one data row"}, 400)
            header, rows = reader[0], reader[1:]
            col = detect_url_column(header, rows)
            if col is None:
                return self._json({"error": "Couldn't find a website/URL column"}, 400)
            with lock:
                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                clear_current()
                state.update({"id": new_session_id(name), "filename": name, "header": header, "rows": rows,
                              "url_col": col, "decisions": {}, "cursor": 0, "created": now, "updated": now,
                              "out_base": unique_out_base(name)})
                save_state()
                payload = public_state()
            wake.set()
            return self._json(payload)
        if path == "/decide":
            body = json.loads(self._read_body() or b"{}")
            i, d = int(body.get("index", -1)), body.get("decision")
            with lock:
                if not (0 <= i < len(state["rows"])) or d not in ("keep", "remove", None):
                    return self._json({"error": "bad request"}, 400)
                if d is None:
                    state["decisions"].pop(str(i), None)
                else:
                    state["decisions"][str(i)] = d
                # cursor = first undecided index
                n = len(state["rows"])
                c = 0
                while c < n and str(c) in state["decisions"]:
                    c += 1
                state["cursor"] = c
                save_state()
                write_outputs()
                payload = public_state()
            wake.set()
            return self._json(payload)
        if path == "/set_url_col":
            body = json.loads(self._read_body() or b"{}")
            with lock:
                state["url_col"] = int(body["col"])
                save_state()
                payload = public_state()
            wake.set()
            return self._json(payload)
        if path == "/settings":
            body = json.loads(self._read_body() or b"{}")
            with lock:
                if "wait" in body:
                    state["settings"]["wait"] = max(0.0, min(30.0, float(body["wait"])))
                if "full" in body:
                    state["settings"]["full"] = bool(body["full"])
                save_state()
                payload = public_state()
            return self._json(payload)
        if path == "/retake":
            body = json.loads(self._read_body() or b"{}")
            i = int(body.get("index", -1))
            with lock:
                if not (0 <= i < len(state["rows"])):
                    return self._json({"error": "bad request"}, 400)
                url = row_url(i)
                full = bool(body.get("full", state["settings"].get("full")))
                if (url, full) in in_progress:
                    return self._json({"busy": True})
                wait = float(body.get("wait", state["settings"]["wait"]))
                png, fail = shot_paths(url, full)
                for f in (png, fail):
                    try:
                        os.remove(f)
                    except FileNotFoundError:
                        pass
                    except OSError as e:  # Windows: file momentarily open for a /shot read
                        return self._json({"error": f"could not clear cached screenshot: {e}"}, 500)
                wanted.insert(0, (url, max(0.0, min(30.0, wait)), full))
            wake.set()
            return self._json({"ok": True})
        if path == "/open":
            body = json.loads(self._read_body() or b"{}")
            sid = str(body.get("id", ""))
            if not re.match(r"^[a-z0-9-]+$", sid) or not os.path.exists(session_path(sid)):
                return self._json({"error": "no such sheet"}, 404)
            with lock:
                save_state()  # flush the sheet we're leaving
                try:
                    open_session(sid)
                except Exception as e:  # noqa
                    clear_current()
                    return self._json({"error": f"could not open sheet: {e}"}, 500)
                save_state()
                payload = public_state()
            wake.set()
            return self._json(payload)
        if path == "/delete_session":
            body = json.loads(self._read_body() or b"{}")
            sid = str(body.get("id", ""))
            if not re.match(r"^[a-z0-9-]+$", sid) or not os.path.exists(session_path(sid)):
                return self._json({"error": "no such sheet"}, 404)
            with lock:
                removed_shots = delete_sheet_shots(sid)
                os.remove(session_path(sid))
                if state["id"] == sid:
                    clear_current()
                save_state()
                print(f"Deleted sheet {sid} and {removed_shots} cached screenshot(s)")
                return self._json({"sessions": list_sessions(), "current": state["id"], "state": public_state()})
        if path == "/reset":
            # close the current sheet; it stays in sessions/ and can be reopened
            with lock:
                save_state()
                clear_current()
                save_state()
            return self._json(public_state())
        self.send_error(404)


def main():
    load_state()
    for n in range(WORKERS):
        threading.Thread(target=worker, args=(n,), daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"Port {PORT} is already in use. Another copy is probably running.\n"
                  f"Close it, or run this one on another port:  python3 app.py --port {PORT + 1}")
            sys.exit(1)
        raise
    print(f"Lead Swiper running at http://localhost:{PORT}   "
          f"(workers={WORKERS}, wait={state['settings']['wait']}s, timeout={args.timeout}s, "
          f"prefetch={PREFETCH_AHEAD}, full_page={'on' if state['settings'].get('full') else 'off'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
