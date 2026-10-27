// Lead Swiper front end. Translations live in i18n.js, styles in style.css.
const APP_VERSION = 5;  // must match APP_VERSION in app.py
const $ = s => document.querySelector(s);
const msgBox = () => $('#shotmsg > div');


let lang = 'en';

function t(key, vars = {}) {
  let s = (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
  for (const [k, v] of Object.entries(vars)) s = s.split('{' + k + '}').join(v);
  return s;
}

function detectLang() {
  try { const saved = localStorage.getItem('ls-lang'); if (saved && I18N[saved]) return saved; } catch (e) {}
  const nav = (navigator.language || 'en').toLowerCase();
  if (nav.startsWith('ja')) return 'ja';
  if (nav.startsWith('zh')) return /tw|hk|mo|hant/.test(nav) ? 'zh-Hant' : 'zh-Hans';
  return 'en';
}

function applyLang(l) {
  lang = I18N[l] ? l : 'en';
  try { localStorage.setItem('ls-lang', lang); } catch (e) {}
  document.documentElement.lang = lang;
  $('#lang').value = lang;
  document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
  applyTheme(theme);  // theme button label is translated
  // re-render anything built in JS from the current state
  if (S) {
    if (!$('#img').hidden) applyZoom();
    if (cur !== null && $('#review').classList.contains('on')) loadRow(cur, { keepShot: true });
    if ($('#sheets').style.display !== 'none' || $('#drop').style.display !== 'none') loadSheets();
    $('#workers').textContent = workersLabel();
  }
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
let theme = 'dark';
function applyTheme(th) {
  theme = th === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('ls-theme', theme); } catch (e) {}
  $('#theme-btn').textContent = theme === 'dark' ? '☀ ' + t('themeLight') : '☾ ' + t('themeDark');
}
try { theme = localStorage.getItem('ls-theme') === 'light' ? 'light' : 'dark'; } catch (e) {}
document.documentElement.dataset.theme = theme;

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------
let S = null;          // server state
let cur = null;        // current index being shown
let history = [];      // {index, prev} decided this session, for undo
let deciding = false;  // a decision is in flight; ignore further key presses until the next row is shown
let browse = false;    // true when stepping through rows manually (prev/next/review), including decided ones
let shotTimer = null;
let shotSeq = 0;        // bumps on every loadShot so stale in-flight responses are ignored
let fullOverride = {};  // index -> true when this card was captured full-page as a one-off
let zoom = 'fit';       // 'fit' | 'width' | number (scale, 1 = actual size)

function isFull(i) { return !!(fullOverride[i] || (S && S.settings && S.settings.full)); }
function workersLabel() { return !S || !S.workers ? '' : S.workers === 1 ? t('workersOne') : t('workers', { n: S.workers }); }

function applyZoom() {
  const img = $('#img'), inner = $('#inner'), lvl = $('#z-lvl');
  img.className = typeof zoom === 'string' ? zoom : '';
  if (typeof zoom === 'number') { img.style.width = Math.round(img.naturalWidth * zoom) + 'px'; img.style.height = 'auto'; }
  else { img.style.width = ''; img.style.height = ''; }
  // tall images anchor to the top so you scroll down the page naturally
  const box = $('#scroll');
  const tall = typeof zoom === 'number' ? img.naturalHeight * zoom > box.clientHeight
             : zoom === 'width' || img.naturalHeight / img.naturalWidth > box.clientHeight / box.clientWidth;
  inner.classList.toggle('top', tall);
  lvl.textContent = zoom === 'fit' ? t('fit') : zoom === 'width' ? t('width') : Math.round(zoom * 100) + '%';
}

function currentScale() {
  const img = $('#img'), shot = $('#scroll');
  if (typeof zoom === 'number') return zoom;
  if (zoom === 'width') return shot.clientWidth / img.naturalWidth;
  return Math.min(shot.clientWidth / img.naturalWidth, shot.clientHeight / img.naturalHeight);
}

function zoomBy(f) {
  if ($('#img').hidden) return;
  zoom = Math.max(0.1, Math.min(4, currentScale() * f));
  applyZoom();
}

function setZoom(z) { zoom = z; applyZoom(); }

async function api(path, opts = {}) {
  const r = await fetch(path, { cache: 'no-store', ...opts });
  return r.json();
}

window.addEventListener('unhandledrejection', e => {
  $('#drop h2').textContent = t('wentWrong', { err: (e.reason && e.reason.message) || e.reason });
});

async function refresh() {
  S = await api('/state');
  $('#stale').style.display = (S.version || 0) < APP_VERSION ? 'block' : 'none';
  $('#s-left').textContent = S.total - S.kept - S.removed;
  $('#s-kept').textContent = S.kept;
  $('#s-removed').textContent = S.removed;
  $('#fname').textContent = S.filename || '';
  $('#bar').style.width = S.total ? (100 * (S.kept + S.removed) / S.total) + '%' : '0';
  $('#export').disabled = !S.loaded;
  $('#home-btn').disabled = !S.loaded;
  if (S.id !== refresh.lastId) { fullOverride = {}; refresh.lastId = S.id; }
  if (!S.loaded) { loadSheets(); return show('drop'); }
  const sel = $('#urlcol');
  sel.innerHTML = S.header.map((h, i) => `<option value="${i}" ${i === S.url_col ? 'selected' : ''}>${esc(h || t('col', { n: i + 1 }))}</option>`).join('');
  S.settings = S.settings || { wait: 1.5, full: false };
  if (document.activeElement !== $('#wait')) $('#wait').value = S.settings.wait;
  $('#full').checked = !!S.settings.full;
  $('#workers').textContent = workersLabel();
  const allDone = S.kept + S.removed >= S.total;
  if (cur === null) cur = S.cursor;
  if (!browse) {
    if (cur < S.total && (S.decided || []).includes(cur)) cur = nextUndecided(cur);
    if (cur >= S.total || allDone) return finished();
  }
  if (cur >= S.total) cur = S.total - 1;
  if (cur < 0) cur = 0;
  $('#btn-summary').style.display = allDone ? '' : 'none';
  show('review');
  await loadRow(cur);
}

function show(which) {
  $('#drop').style.display = which === 'drop' ? 'flex' : 'none';
  $('#review').classList.toggle('on', which === 'review');
  $('#done').classList.toggle('on', which === 'done');
}

function finished() {
  $('#d-kept').textContent = S.kept; $('#d-removed').textContent = S.removed;
  show('done');
}

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function loadRow(i, { keepShot = false } = {}) {
  const row = await api('/row/' + i);
  cur = i;
  const pill = $('#pill');
  pill.className = 'pill ' + (row.decision || 'none');
  pill.textContent = row.decision === 'keep' ? t('keptPill') : row.decision === 'remove' ? t('removedPill') : t('undecided');
  $('#pos').textContent = `${i + 1} / ${S.total}`;
  $('#z-mode').textContent = isFull(i) ? t('fullPage') : t('firstScreen');
  $('#url').textContent = row.url || t('noUrl');
  $('#url').href = row.url || '#';
  $('#fields').innerHTML = Object.entries(row.fields)
    .filter(([k, v]) => v && v.trim())
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  if (!keepShot) loadShot(i);
}

function loadShot(i) {
  clearTimeout(shotTimer);
  const img = $('#img'), msg = msgBox();
  const token = ++shotSeq;
  $('#z-mode').textContent = isFull(i) ? t('fullPage') : t('firstScreen');
  img.hidden = true; img.src = '';
  if (!msg.dataset.retaking) msg.innerHTML = '<div class="spin"></div>' + esc(t('taking'));
  delete msg.dataset.retaking;
  const poll = async () => {
    if (cur !== i || token !== shotSeq) return;
    const r = await fetch('/shot/' + i + (isFull(i) ? '?full=1' : ''), { cache: 'no-store' });
    if (cur !== i || token !== shotSeq) return;
    if (r.status === 200) {
      const blob = await r.blob();
      img.onload = () => { zoom = isFull(i) ? 'width' : 'fit'; applyZoom(); $('#scroll').scrollTop = 0; };
      if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);  // free the previous screenshot
      img.src = URL.createObjectURL(blob);
      img.hidden = false; msg.innerHTML = '';
    } else if (r.status === 202) {
      shotTimer = setTimeout(poll, 500);
    } else {
      const j = await r.json().catch(() => ({}));
      msg.innerHTML = `<div>${esc(t('cantLoad'))}</div><div class="err">${esc(j.error || '')}</div>
        <div style="margin-top:12px"><button onclick="window.open($('#url').href)">${esc(t('openInBrowser'))}</button>
        <button onclick="retake()">${esc(t('retryLonger'))}</button></div>`;
    }
  };
  poll();
}

function flash(kind) {
  const f = $('#flash');
  f.className = 'flash ' + kind + ' on';
  requestAnimationFrame(() => requestAnimationFrame(() => f.classList.remove('on')));
}

async function decide(kind) {
  if (cur === null || deciding || !$('#review').classList.contains('on')) return;
  const i = cur;
  deciding = true;
  try {
    flash(kind);
    const pillClass = $('#pill').className;
    history.push({ index: i, prev: pillClass.includes('keep') ? 'keep' : pillClass.includes('remove') ? 'remove' : null });
    S = await api('/decide', { method: 'POST', body: JSON.stringify({ index: i, decision: kind }) });
    if (browse) {
      if (i + 1 < S.total) cur = i + 1;
      else { browse = false; cur = S.cursor; }   // ran off the end: back to normal flow / summary
    } else {
      cur = nextUndecided(i + 1);
    }
    await refresh();
  } finally {
    deciding = false;
  }
}

function nextUndecided(from) {
  // move forward from here, skipping decided rows; wrap to the first undecided overall
  const done = new Set(S.decided || []);
  for (let i = from; i < S.total; i++) if (!done.has(i)) return i;
  return S.cursor;
}

async function undo() {
  const last = history.pop();
  if (!last || deciding) return;
  // restore whatever the row was before (undecided, or an earlier keep/remove you changed)
  await api('/decide', { method: 'POST', body: JSON.stringify({ index: last.index, decision: last.prev }) });
  cur = last.index;
  browse = browse || last.prev !== null;  // a restored decision stays visible rather than being skipped
  await refresh();
}

async function retake(opts = {}) {
  if (cur === null || !$('#review').classList.contains('on')) return;
  const full = opts.full !== undefined ? opts.full : isFull(cur);
  const wait = S.settings.wait;  // exactly what "Wait after load" says
  const r = await api('/retake', { method: 'POST', body: JSON.stringify({ index: cur, wait, full }) });
  // "busy" means a worker already has it in hand; polling will pick up the result either way
  const msg = msgBox();
  msg.innerHTML = '<div class="spin"></div>' + esc(r.busy ? t('retakingBusy') : t(full ? 'retakingFull' : 'retaking', { wait }));
  msg.dataset.retaking = '1';
  $('#img').hidden = true;
  loadShot(cur);
}

async function fullThisOne() {
  if (cur === null || !$('#review').classList.contains('on')) return;
  fullOverride[cur] = true;
  // requesting the full-page shot is enough: the server captures it if it isn't cached yet
  loadShot(cur);
}

async function step(delta) {
  if (!S || !S.loaded) return;
  browse = true;
  cur = Math.max(0, Math.min(S.total - 1, (cur === null ? S.cursor : cur) + delta));
  await refresh();
}

function reviewAll() {
  browse = true; cur = 0; refresh();
}

function toSummary() {
  browse = false; cur = null; refresh();
}

async function skip() {
  if (cur === null) return;
  cur = nextUndecided(cur + 1);
  await refresh();
}

async function upload(file) {
  const bytes = await file.arrayBuffer();  // raw bytes: the server detects the encoding
  const r = await api('/upload', { method: 'POST', headers: { 'X-Filename': encodeURIComponent(file.name), 'X-Lang': lang }, body: bytes });
  if (r.error) return alert(r.error);
  cur = 0; history = []; browse = false; fullOverride = {};
  await refresh();
}

// --- sheets ---
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso.replace(' ', 'T'));
  const loc = I18N[lang]._locale;
  return isNaN(d) ? iso : d.toLocaleDateString(loc, { day: 'numeric', month: 'short', year: 'numeric' }) + ' ' + d.toLocaleTimeString(loc, { hour: '2-digit', minute: '2-digit' });
}

function renderSheets(list, current, el, { allowDelete = true } = {}) {
  if (!list.length) { el.innerHTML = `<div class="empty">${esc(t('noSheets'))}</div>`; return; }
  el.innerHTML = list.map(sh => {
    const done = sh.kept + sh.removed, left = sh.total - done;
    const rest = left ? t('leftRest', { left }) : t('finished');
    return `<div class="sheet ${sh.id === current ? 'current' : ''}" data-id="${esc(sh.id)}">
      <div class="name"><b title="${esc(sh.filename)}">${esc(sh.filename)}</b><span>${sh.id === current ? esc(t('openNow')) + ' · ' : ''}${esc(t('lastWorked', { date: fmtDate(sh.updated) }))}</span></div>
      <div class="prog">${t('progress', { kept: sh.kept, removed: sh.removed, rest, total: sh.total })}</div>
      ${sh.id === current ? '' : `<button data-act="open">${esc(t('open'))}</button>`}
      ${allowDelete ? `<button data-act="delete" class="danger" title="${esc(t('deleteTitle'))}">${esc(t('delete'))}</button>` : ''}
    </div>`;
  }).join('');
}

async function loadSheets() {
  const r = await api('/sessions');
  renderSheets(r.sessions, r.current, $('#sheet-list'));
  $('#drop-sheets').style.display = r.sessions.length ? '' : 'none';
  renderSheets(r.sessions, r.current, $('#drop-sheet-list'));
}

async function goHome() {
  // close the current sheet (it stays in the list) and show the home screen: drop zone + all sheets
  if (S && S.loaded) await api('/reset', { method: 'POST' });
  cur = null; history = []; browse = false; fullOverride = {};
  $('#sheets').style.display = 'none';
  refresh();
}

async function sheetAction(e) {
  const btn = e.target.closest('button[data-act]'); if (!btn) return;
  const id = btn.closest('.sheet').dataset.id;
  if (btn.dataset.act === 'open') {
    const r = await api('/open', { method: 'POST', body: JSON.stringify({ id }) });
    if (r.error) return alert(r.error);
    cur = null; history = []; browse = false; fullOverride = {};
    $('#sheets').style.display = 'none';
    await refresh();
  } else if (btn.dataset.act === 'delete') {
    const sh = btn.closest('.sheet').querySelector('b').textContent;
    if (!confirm(t('deleteConfirm', { name: sh }))) return;
    const r = await api('/delete_session', { method: 'POST', body: JSON.stringify({ id }) });
    if (r.error) return alert(r.error);
    cur = null; history = []; browse = false;
    await refresh();
  }
}

// --- wiring ---
$('#pick').onclick = () => $('#file').click();
$('#file').onchange = e => e.target.files[0] && upload(e.target.files[0]);
const drop = $('#drop');
['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));
$('#btn-keep').onclick = () => decide('keep');
$('#btn-remove').onclick = () => decide('remove');
$('#btn-undo').onclick = undo;
$('#btn-skip').onclick = skip;
$('#btn-retake').onclick = () => retake();
$('#btn-full').onclick = fullThisOne;
$('#full').onchange = async e => {
  S = await api('/settings', { method: 'POST', body: JSON.stringify({ full: e.target.checked }) });
  e.target.blur();
  if (cur !== null) loadShot(cur);
};
$('#z-in').onclick = () => zoomBy(1.25);
$('#z-out').onclick = () => zoomBy(0.8);
$('#z-fit').onclick = () => setZoom('fit');
$('#z-100').onclick = () => setZoom(1);
window.addEventListener('resize', () => { if (!$('#img').hidden) applyZoom(); });
$('#btn-prev').onclick = () => step(-1);
$('#btn-next').onclick = () => step(1);
$('#btn-review').onclick = reviewAll;
$('#btn-summary').onclick = toSummary;
$('#wait').onchange = async e => {
  S = await api('/settings', { method: 'POST', body: JSON.stringify({ wait: +e.target.value }) });
  e.target.value = S.settings.wait; e.target.blur();
};
$('#btn-open').onclick = () => window.open($('#url').href);
$('#export').onclick = $('#export2').onclick = () => { location.href = '/export'; };
$('#sheets-btn').onclick = () => { const p = $('#sheets'); p.style.display = p.style.display === 'none' ? '' : 'none'; if (p.style.display === '') loadSheets(); };
$('#sheets-close').onclick = () => { $('#sheets').style.display = 'none'; };
$('#new-sheet').onclick = goHome;
$('#home-btn').onclick = goHome;
$('#sheet-list').onclick = sheetAction;
$('#drop-sheet-list').onclick = sheetAction;
$('#theme-btn').onclick = () => applyTheme(theme === 'dark' ? 'light' : 'dark');
$('#lang').onchange = e => { applyLang(e.target.value); e.target.blur(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape' && $('#sheets').style.display !== 'none') $('#sheets').style.display = 'none'; });
$('#urlcol').onchange = async e => {
  await api('/set_url_col', { method: 'POST', body: JSON.stringify({ col: +e.target.value }) });
  refresh();
};
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'SELECT' || (e.target.tagName === 'INPUT' && e.target.type !== 'checkbox')) { if (e.key === 'Enter' || e.key === 'Escape') e.target.blur(); return; }
  if (e.target.type === 'checkbox' && e.key === ' ') return;
  if (e.metaKey || e.ctrlKey || e.altKey || e.repeat) return;  // held keys don't auto-swipe through leads
  const k = e.key.toLowerCase();
  if (e.key === 'ArrowRight' || k === 'k') { e.preventDefault(); decide('keep'); }
  else if (e.key === 'ArrowLeft' || k === 'x') { e.preventDefault(); decide('remove'); }
  else if (k === 'z') undo();
  else if (k === 'o') window.open($('#url').href);
  else if (k === 's') skip();
  else if (k === 'r') retake();
  else if (k === 'f') fullThisOne();
  else if (e.key === '=' || e.key === '+') zoomBy(1.25);
  else if (e.key === '-' || e.key === '_') zoomBy(0.8);
  else if (e.key === '0') setZoom('fit');
  else if (e.key === '1') setZoom(1);
  else if (e.key === 'Escape' && S && S.loaded && S.kept + S.removed >= S.total) toSummary();
  else if (e.key === 'Enter' && $('#done').classList.contains('on')) reviewAll();
  else if (k === 'h') goHome();
  else if (e.key === '[' || k === 'p') step(-1);
  else if (e.key === ']' || k === 'n') step(1);
});
applyLang(detectLang());
refresh();
