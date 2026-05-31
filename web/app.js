"use strict";
const grid = document.getElementById('grid');
const empty = document.getElementById('empty');
const sel = new Set();    // composite keys: `${dir}\n${name}`

let curDir = '';
let dstDir = 'selected';     // last chosen move destination
let hiddenSet = new Set();   // dirs shown only in the "hidden" dropdown
let files = [];              // current folder items: [{name,dir,w,h,mtime,size}]
let searchResults = null;    // cross-folder search hits, or null when not searching
let crossSearch = false;     // true while showing cross-folder results
let view = [];               // filtered + sorted subset currently shown
let lbIndex = -1;            // index into `view`
let searchTerm = '';
let metaSearch = false;      // also scan embedded metadata when searching
let sortKey = 'mtime';       // mtime | name | size
let sortDir = -1;            // 1 = ascending, -1 = descending
let metaOpen = false;
let searchTimer = null;

const BATCH = 120;
let rendered = 0;
const promptCache = new Map();   // key -> {pos, neg} (lazy)

const TRASH = '_trash';
const WF_KEY = 'explore_gallery_pending_workflow';

const $ = id => document.getElementById(id);
const enc = encodeURIComponent;
function esc(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
const keyOf = f => f.dir + '\n' + f.name;
function parseKey(k) { const i = k.indexOf('\n'); return { dir: k.slice(0, i), name: k.slice(i + 1) }; }
const isHiddenDir = dir => dir.startsWith('_') || dir.startsWith('.') || hiddenSet.has(dir);
const isTrash = () => curDir === TRASH;

const EXPAND_SVG =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"'
  + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
  + '<path d="M8 3H3v5M16 3h5v5M21 16v5h-5M3 16v5h5"/></svg>';

// ---- url helpers (per-file dir, so cross-folder results work) ----
const thumbUrl = f => '/explore_gallery/thumb?dir=' + enc(f.dir) + '&file=' + enc(f.name);
const fullUrl = f => '/view?filename=' + enc(f.name) + '&subfolder=' + enc(f.dir) + '&type=output';
const dlUrl = f => '/explore_gallery/download?dir=' + enc(f.dir) + '&file=' + enc(f.name);
const metaUrl = f => '/explore_gallery/meta?dir=' + enc(f.dir) + '&file=' + enc(f.name);
const wfUrl = f => '/explore_gallery/workflow?dir=' + enc(f.dir) + '&file=' + enc(f.name);

function fmtDate(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
       + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function fmtDateShort(ts) {   // MM/DD HH:MM (no year), for grid cards
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return p(d.getMonth() + 1) + '/' + p(d.getDate())
       + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function gcd(a, b) { while (b) { [a, b] = [b, a % b]; } return a || 1; }
function fmtAspect(w, h) {
  if (!w || !h) return '—';
  const g = gcd(w, h);
  return (w / g) + ':' + (h / g);
}
const dirLabel = dir => dir || '(ルート)';

// ---- toast notifications ----
const toasts = $('toasts');
function toast(text, type = '', life = 2600) {
  const el = document.createElement('div');
  el.className = 'toast' + (type ? ' ' + type : '');
  el.textContent = text;
  toasts.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 220); }, life);
}

// ---- selection bar ----
function refreshButtons() {
  $('count').textContent = sel.size + ' / ' + view.length;
  const has = sel.size > 0;
  $('selbar').hidden = !has;
  $('selinfo').textContent = sel.size + ' 枚選択中';
  const del = $('del');
  if (isTrash()) {
    del.textContent = '❌ 完全削除';
    del.title = '元に戻せません';
    $('move').textContent = '↩ 復元';
    $('dstwrap').firstChild.textContent = '復元先';
  } else {
    del.textContent = '🗑 ゴミ箱へ';
    del.title = '_trash へ移動（復元可能）';
    $('move').textContent = '→ 移動';
    $('dstwrap').firstChild.textContent = '移動先';
  }
  $('dlfolder').disabled = files.length === 0 || crossSearch;
}

// ---- tabs + hidden dropdown ----
function renderTabs(dirs, hidden) {
  const t = $('tabs');
  t.innerHTML = '<h1>📂 Explore Gallery</h1>';
  for (const d of dirs) {
    const b = document.createElement('button');
    b.className = 'tab' + (d.dir === curDir ? ' active' : '');
    b.innerHTML = esc(d.label) + '<span class="cnt">' + d.count + '</span>';
    b.dataset.dir = d.dir;
    b.onclick = () => openDir(d.dir);
    t.appendChild(b);
  }
  if (hidden.length) {
    const s = document.createElement('select');
    s.id = 'hiddensel'; s.className = 'hiddensel';
    s.title = '非表示フォルダ（空フォルダ・_trash など）';
    let opts = '<option value="">非表示フォルダ ▾</option>';
    for (const d of hidden)
      opts += '<option value="' + esc(d.dir) + '">' + esc(d.label)
            + '（' + d.count + '）</option>';
    s.innerHTML = opts;
    s.value = isHiddenDir(curDir) ? curDir : '';
    s.onchange = () => { if (s.value || isHiddenDir(curDir)) openDir(s.value); };
    t.appendChild(s);
  }
}
function populateDst(dirs, hidden) {
  const s = $('dstsel');
  const prev = s.value;
  const all = dirs.concat(hidden);
  let html = '';
  for (const d of all) {
    if (d.dir === curDir) continue;       // can't move into the current folder
    html += '<option value="' + esc(d.dir) + '">' + esc(d.label) + '</option>';
  }
  html += '<option value="__new__">➕ 新規フォルダ…</option>';
  s.innerHTML = html;
  const want = (prev && prev !== '__new__') ? prev : dstDir;
  const has = v => [...s.options].some(o => o.value === v);
  if (has(want)) s.value = want;
  else if (has('selected')) s.value = 'selected';
}
function updateActive() {
  for (const b of document.querySelectorAll('#tabs .tab'))
    b.classList.toggle('active', b.dataset.dir === curDir);
  const hs = $('hiddensel');
  if (hs) hs.value = isHiddenDir(curDir) ? curDir : '';
}
function clearSearchInput() {
  searchTerm = ''; searchResults = null; crossSearch = false;
  if ($('search')) $('search').value = '';
}
function openDir(dir) {
  curDir = dir; sel.clear(); clearSearchInput();
  lastStat = null;
  loadList(); updateActive();
}

// ---- incremental grid render ----
const sentinel = document.createElement('div');
sentinel.id = 'sentinel';
const io = new IntersectionObserver((entries) => {
  if (entries.some(e => e.isIntersecting)) renderMore();
}, { root: null, rootMargin: '800px' });

// lazily fetch positive/negative prompts only for visible cards
const pio = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (e.isIntersecting) { pio.unobserve(e.target); fillPrompts(e.target); }
  }
}, { root: null, rootMargin: '300px' });

function toggleSel(f, fig) {
  const k = keyOf(f);
  if (sel.has(k)) sel.delete(k); else sel.add(k);
  fig.classList.toggle('sel', sel.has(k));
  refreshButtons();
}

function makeFigure(f, i) {
  const fig = document.createElement('figure');
  const k = keyOf(f);
  if (sel.has(k)) fig.classList.add('sel');
  fig.dataset.key = k; fig.dataset.name = f.name; fig.dataset.dir = f.dir;

  const thumb = document.createElement('div');
  thumb.className = 'thumb';
  const img = document.createElement('img');
  img.loading = 'lazy'; img.decoding = 'async';
  img.src = thumbUrl(f);

  // resolution + aspect badge (top-left)
  const badge = document.createElement('div');
  badge.className = 'tbadge';
  const dim = (f.w && f.h) ? (f.w + '×' + f.h) : '—';
  badge.textContent = dim + ' · ' + fmtAspect(f.w, f.h);
  // copy-workflow (bottom-left), expand (bottom-right)
  const cwf = document.createElement('button');
  cwf.className = 'copywf'; cwf.title = 'ワークフローをコピー'; cwf.textContent = '📋';
  cwf.onclick = (e) => { e.stopPropagation(); copyWorkflow(f, cwf); };
  const expand = document.createElement('button');
  expand.className = 'expand'; expand.title = '拡大表示'; expand.innerHTML = EXPAND_SVG;
  expand.onclick = (e) => { e.stopPropagation(); openLightbox(i); };
  thumb.append(img, badge, cwf, expand);

  const cap = document.createElement('figcaption');
  let nameHtml = esc(f.name);
  if (crossSearch)
    nameHtml = '<span class="fbadge">' + esc(dirLabel(f.dir)) + '</span>' + nameHtml;
  cap.innerHTML = '<span class="nm">' + nameHtml + '</span>'
    + '<span class="date">' + fmtDateShort(f.mtime) + '</span>';

  const prompts = document.createElement('div');
  prompts.className = 'prompts';
  prompts.onclick = (e) => e.stopPropagation();
  if (crossSearch && f.match) {        // show where a metadata search hit
    const hit = document.createElement('div');
    hit.className = 'hit';
    hit.innerHTML = '<span class="hitkey">🔎 ' + esc(f.match.key) + '</span>'
      + '<span class="hittext">' + esc(f.match.snippet) + '</span>';
    hit.title = f.match.snippet;
    prompts.appendChild(hit);
  }
  prompts.append(makeProw('pos', 'P'), makeProw('neg', 'N'));

  fig.append(thumb, cap, prompts);
  fig.onclick = () => toggleSel(f, fig);   // single click selects
  pio.observe(fig);
  return fig;
}

function renderMore() {
  if (rendered >= view.length) { io.unobserve(sentinel); return; }
  const frag = document.createDocumentFragment();
  const end = Math.min(rendered + BATCH, view.length);
  for (let i = rendered; i < end; i++) frag.appendChild(makeFigure(view[i], i));
  grid.insertBefore(frag, sentinel);
  rendered = end;
  if (rendered >= view.length) io.unobserve(sentinel);
}

function renderReset() {
  pio.disconnect();
  grid.innerHTML = '';
  rendered = 0;
  grid.appendChild(sentinel);
  empty.hidden = view.length > 0;
  io.observe(sentinel);
  renderMore();
  refreshButtons();
}

function applyView() {
  const q = searchTerm.trim().toLowerCase();
  let base;
  if (q && searchResults) base = searchResults;                          // cross-folder
  else if (q) base = files.filter(f => f.name.toLowerCase().includes(q)); // local
  else base = files;
  const v = base.slice();
  v.sort((a, b) => {
    let r;
    if (sortKey === 'name') r = a.name.localeCompare(b.name);
    else r = (a[sortKey] || 0) - (b[sortKey] || 0);
    return r * sortDir;
  });
  view = v;
  renderReset();
}

// ---- prompt rows (lazy) ----
function extractPN(fields) {
  let pos = '', neg = '';
  for (const [k, v] of (fields || [])) {
    if (k === 'Negative') neg = v;
    else if (!pos && /^Prompt/.test(k)) pos = v;
  }
  return { pos: String(pos || ''), neg: String(neg || '') };
}
function setProw(fig, cls, text) {
  const row = fig.querySelector('.prow.' + cls);
  if (!row) return;
  const tx = row.querySelector('.ptext');
  const cp = row.querySelector('.copy');
  if (text) {
    tx.dataset.full = text;
    tx.textContent = text.replace(/\s+/g, ' ').trim();
    tx.title = text; tx.classList.remove('empty'); cp.disabled = false;
  } else {
    tx.dataset.full = ''; tx.textContent = '—'; tx.title = '';
    tx.classList.add('empty'); cp.disabled = true;
  }
}
async function fillPrompts(fig) {
  const k = fig.dataset.key;
  let pn = promptCache.get(k);
  if (!pn) {
    try {
      const r = await fetch(metaUrl({ dir: fig.dataset.dir, name: fig.dataset.name }));
      const d = await r.json();
      pn = extractPN(d.fields);
    } catch (e) { pn = { pos: '', neg: '' }; }
    promptCache.set(k, pn);
  }
  setProw(fig, 'pos', pn.pos);
  setProw(fig, 'neg', pn.neg);
}
async function copyText(btn, text) {
  if (!text) return;
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
  }
  if (ok) {
    const old = btn.textContent;
    btn.textContent = '✓'; btn.classList.add('done');
    setTimeout(() => { btn.textContent = old; btn.classList.remove('done'); }, 900);
  } else { toast('コピーできませんでした', 'err'); }
}
function makeProw(cls, label) {
  const row = document.createElement('div');
  row.className = 'prow ' + cls;
  const lb = document.createElement('span'); lb.className = 'plabel'; lb.textContent = label;
  const tx = document.createElement('span'); tx.className = 'ptext'; tx.textContent = '…';
  const cp = document.createElement('button');
  cp.className = 'copy'; cp.textContent = '⧉'; cp.title = 'コピー'; cp.disabled = true;
  cp.onclick = (e) => { e.stopPropagation(); copyText(cp, tx.dataset.full || ''); };
  row.append(lb, tx, cp);
  return row;
}

// ---- copy workflow → ComfyUI paste ----
async function copyWorkflow(f, btn) {
  try {
    const d = await (await fetch(wfUrl(f))).json();
    const wf = d.workflow || d.prompt;
    if (!wf) { toast('この画像にワークフロー情報がありません', 'err'); return; }
    localStorage.setItem(WF_KEY, JSON.stringify({
      t: Date.now(), name: f.name,
      kind: d.workflow ? 'workflow' : 'prompt', data: wf,
    }));
    if (btn) {
      const o = btn.textContent;
      btn.textContent = '✓'; btn.classList.add('done');
      setTimeout(() => { btn.textContent = o; btn.classList.remove('done'); }, 1000);
    }
    toast('ワークフローをコピー → ComfyUI画面で Ctrl+V（貼り付け）', 'ok', 4500);
  } catch (e) { toast('ワークフローの取得に失敗しました', 'err'); }
}

// ---- data loading ----
async function loadDirs() {
  try {
    const d = await (await fetch('/explore_gallery/dirs')).json();
    if (dstDir === 'selected') dstDir = d.dst || 'selected';
    const dirs = d.dirs || [];
    const hidden = d.hidden || [];
    hiddenSet = new Set(hidden.map(x => x.dir));
    const all = dirs.concat(hidden);
    if (!all.some(x => x.dir === curDir)) curDir = dirs.length ? dirs[0].dir : '';
    renderTabs(dirs, hidden);
    populateDst(dirs, hidden);
  } catch (e) {
    toast('フォルダ一覧の取得に失敗しました', 'err');
  }
}

async function loadList() {
  promptCache.clear();
  searchResults = null; crossSearch = false;
  try {
    const d = await (await fetch('/explore_gallery/list?dir=' + enc(curDir))).json();
    files = (d.files || []).map(f => (f.dir = curDir, f));
    pruneSel();
    applyView();
  } catch (e) {
    files = []; applyView();
    toast('一覧の取得に失敗しました', 'err');
  }
}

async function doSearch() {
  const q = searchTerm.trim();
  if (!q) { searchResults = null; crossSearch = false; applyView(); return; }
  if (isHiddenDir(curDir)) {   // search within the open hidden folder only
    searchResults = null; crossSearch = false; applyView(); return;
  }
  promptCache.clear();
  try {
    const url = '/explore_gallery/search?q=' + enc(q) + (metaSearch ? '&meta=1' : '');
    const d = await (await fetch(url)).json();
    searchResults = d.files || []; crossSearch = true;
    pruneSel();
    applyView();
    toast(searchResults.length + ' 件' + (metaSearch ? '（メタ込み）' : ''), '', 1800);
  } catch (e) {
    searchResults = []; crossSearch = true; applyView();
    toast('検索に失敗しました', 'err');
  }
}

function pruneSel() {
  const valid = new Set((searchResults || files).map(keyOf));
  for (const k of [...sel]) if (!valid.has(k)) sel.delete(k);
}

async function reloadAll() { await loadDirs(); await loadList(); }
async function refreshCurrent() {
  await loadDirs(); updateActive();
  if (searchTerm.trim() && crossSearch) await doSearch();
  else await loadList();
}

// ---- new-arrival auto refresh ----
let autoTimer = null;
let lastStat = null;
function setAuto(on) {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  localStorage.setItem('eg_auto', on ? '1' : '0');
  if (on) { lastStat = null; autoTimer = setInterval(pollStat, 4000); }
}
async function pollStat() {
  if (document.hidden || crossSearch) return;
  try {
    const d = await (await fetch('/explore_gallery/stat?dir=' + enc(curDir))).json();
    const sig = d.count + '|' + d.latest;
    if (lastStat === null) { lastStat = sig; return; }
    if (sig === lastStat) return;
    lastStat = sig;
    if (!$('lb').classList.contains('open') && window.scrollY < 300) {
      await refreshCurrent();
      toast('新着を取り込みました', 'ok');
    } else {
      toast('新着があります（🔄 で更新）');
    }
  } catch (e) { /* ignore transient poll errors */ }
}

// ---- lightbox ----
function openLightbox(i) {
  lbIndex = i;
  $('lb').classList.add('open');
  buildStrip();
  showLightbox();
}
function buildStrip() {
  const s = $('lbstrip');
  s.innerHTML = '';
  view.forEach((f, i) => {
    const t = document.createElement('img');
    t.loading = 'lazy';
    t.src = thumbUrl(f);
    t.dataset.i = i;
    if (i === lbIndex) t.classList.add('cur');
    t.onclick = () => { lbIndex = i; showLightbox(); };
    s.appendChild(t);
  });
}
function highlightStrip() {
  const s = $('lbstrip');
  for (const im of s.querySelectorAll('img'))
    im.classList.toggle('cur', Number(im.dataset.i) === lbIndex);
  const cur = s.querySelector('img.cur');
  if (cur) cur.scrollIntoView({ inline: 'center', block: 'nearest' });
}
function showLightbox() {
  const f = view[lbIndex];
  if (!f) return;
  resetZoom();
  $('lbimg').src = fullUrl(f);
  const dim = (f.w && f.h) ? (f.w + '×' + f.h) : '—';
  const where = crossSearch ? ('<span class="meta">📂 ' + esc(dirLabel(f.dir)) + '</span>') : '';
  $('lbcap').innerHTML = '<b>' + esc(f.name) + '</b>' + where + '<span class="meta">'
    + dim + ' · ' + fmtAspect(f.w, f.h) + ' · ' + fmtDate(f.mtime) + '</span>';
  $('lbpos').textContent = (lbIndex + 1) + ' / ' + view.length;
  $('lbsel').classList.toggle('on', sel.has(keyOf(f)));
  highlightStrip();
  if (metaOpen) loadMeta(f);
}
function closeLightbox() { $('lb').classList.remove('open'); lbIndex = -1; }
function step(n) {
  if (lbIndex < 0 || !view.length) return;
  lbIndex = (lbIndex + n + view.length) % view.length;
  showLightbox();
}
function toggleCurrentSel() {
  const f = view[lbIndex];
  if (!f) return;
  const k = keyOf(f);
  if (sel.has(k)) sel.delete(k); else sel.add(k);
  const fig = grid.querySelector('figure[data-key="' + CSS.escape(k) + '"]');
  if (fig) fig.classList.toggle('sel', sel.has(k));
  showLightbox();
  refreshButtons();
}

// ---- lightbox zoom / pan ----
let zScale = 1, zTX = 0, zTY = 0, panning = false, panSX = 0, panSY = 0, moved = false;
function applyZoom() {
  const im = $('lbimg');
  im.style.transform = 'translate(' + zTX + 'px,' + zTY + 'px) scale(' + zScale + ')';
  im.classList.toggle('zoomed', zScale > 1);
  im.classList.toggle('panning', panning);
}
function resetZoom() { zScale = 1; zTX = 0; zTY = 0; panning = false; applyZoom(); }
function zoomAt(clientX, clientY, ns) {
  const im = $('lbimg');
  ns = Math.min(8, Math.max(1, ns));
  const r = im.getBoundingClientRect();
  const d = clientX - (r.left + r.width / 2);
  const dy = clientY - (r.top + r.height / 2);
  if (ns === 1) { zTX = 0; zTY = 0; }
  else { zTX += d * (1 - ns / zScale); zTY += dy * (1 - ns / zScale); }
  zScale = ns;
  applyZoom();
}
function initZoomHandlers() {
  const im = $('lbimg');
  im.addEventListener('wheel', (e) => {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, zScale * (e.deltaY < 0 ? 1.15 : 1 / 1.15));
  }, { passive: false });
  im.addEventListener('pointerdown', (e) => {
    if (zScale <= 1) return;
    panning = true; moved = false; panSX = e.clientX; panSY = e.clientY;
    try { im.setPointerCapture(e.pointerId); } catch (_) {}
    applyZoom();
  });
  im.addEventListener('pointermove', (e) => {
    if (!panning) return;
    const dx = e.clientX - panSX, dy = e.clientY - panSY;
    if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
    zTX += dx; zTY += dy; panSX = e.clientX; panSY = e.clientY;
    applyZoom();
  });
  const endPan = (e) => {
    if (!panning) return;
    panning = false; applyZoom();
    try { im.releasePointerCapture(e.pointerId); } catch (_) {}
  };
  im.addEventListener('pointerup', endPan);
  im.addEventListener('pointercancel', endPan);
  im.addEventListener('click', (e) => {
    if (moved) { moved = false; return; }       // drag, not a click
    if (zScale > 1) zoomAt(e.clientX, e.clientY, 1);
    else zoomAt(e.clientX, e.clientY, 2);
  });
}

// ---- metadata panel ----
function toggleMeta() {
  metaOpen = !metaOpen;
  $('lbinfo').classList.toggle('on', metaOpen);
  $('lbmeta').hidden = !metaOpen;
  if (metaOpen) { const f = view[lbIndex]; if (f) loadMeta(f); }
}
function renderMeta(d) {
  const panel = $('lbmeta');
  const fields = d.fields || [];
  const raw = d.raw || {};
  const hasRaw = raw && Object.keys(raw).length > 0;
  if (!fields.length && !hasRaw) {
    panel.innerHTML = '<div class="metahint">埋め込みメタデータはありません。</div>';
    return;
  }
  let h = '';
  if (fields.length) {
    h += '<table class="metatbl">';
    for (const [k, v] of fields)
      h += '<tr><th>' + esc(k) + '</th><td>' + esc(v) + '</td></tr>';
    h += '</table>';
  }
  if (hasRaw)
    h += '<details><summary>RAW</summary><pre>'
      + esc(JSON.stringify(raw, null, 2)) + '</pre></details>';
  panel.innerHTML = h;
}
async function loadMeta(f) {
  const panel = $('lbmeta');
  const forKey = keyOf(f);
  panel.innerHTML = '<div class="metahint">読み込み中…</div>';
  try {
    const r = await fetch(metaUrl(f));
    const d = await r.json();
    if (view[lbIndex] && keyOf(view[lbIndex]) === forKey) renderMeta(d);
  } catch (e) {
    panel.innerHTML = '<div class="metahint">メタデータを取得できませんでした。</div>';
  }
}

function removeFile(f) {
  files = files.filter(x => x !== f);
  if (searchResults) searchResults = searchResults.filter(x => x !== f);
}
async function trashCurrent() {
  const f = view[lbIndex];
  if (!f) return;
  const inTrash = isTrash();
  const url = inTrash ? '/explore_gallery/delete' : '/explore_gallery/trash';
  if (inTrash && !confirm('この画像を完全に削除します。元に戻せません。')) return;
  let ok = false;
  try {
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir: f.dir, files: [f.name] }),
    });
    const d = await r.json();
    ok = r.ok && ((d.trashed || d.deleted || []).length > 0);
  } catch (e) { ok = false; }
  if (!ok) { toast(inTrash ? '削除に失敗しました' : 'ゴミ箱への移動に失敗しました', 'err'); return; }

  sel.delete(keyOf(f));
  removeFile(f);
  view.splice(lbIndex, 1);
  toast(inTrash ? '1 枚を完全削除' : '1 枚をゴミ箱へ', 'ok');
  if (view.length === 0) { closeLightbox(); renderReset(); loadDirs(); return; }
  if (lbIndex >= view.length) lbIndex = view.length - 1;
  renderReset(); buildStrip(); showLightbox(); loadDirs();
}

const stop = (fn) => (e) => { e.stopPropagation(); fn(e); };
$('lbclose').onclick = stop(closeLightbox);
$('lbprev').onclick = stop(() => step(-1));
$('lbnext').onclick = stop(() => step(1));
$('lbdl').onclick = stop(() => { const f = view[lbIndex]; if (f) downloadOne(f); });
$('lbsel').onclick = stop(toggleCurrentSel);
$('lbinfo').onclick = stop(toggleMeta);
$('lbcopy').onclick = stop(() => { const f = view[lbIndex]; if (f) copyWorkflow(f, $('lbcopy')); });
$('lb').onclick = (e) => {
  if (e.target === $('lb') || e.target.classList.contains('stage')) closeLightbox();
};
initZoomHandlers();
document.addEventListener('keydown', (e) => {
  if (!$('lb').classList.contains('open')) return;
  if (e.key === 'Escape') closeLightbox();
  else if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); toggleCurrentSel(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); trashCurrent(); }
  else if (e.key === 'i' || e.key === 'I') { e.preventDefault(); toggleMeta(); }
  else if (e.key === 'c' || e.key === 'C') {
    e.preventDefault(); const f = view[lbIndex]; if (f) copyWorkflow(f, $('lbcopy'));
  }
});

// ---- downloads ----
function downloadOne(f) {
  const a = document.createElement('a');
  a.href = dlUrl(f); a.download = f.name;
  document.body.appendChild(a); a.click(); a.remove();
}
async function downloadZipDir(dir, names) {
  const r = await fetch('/explore_gallery/zip', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dir, files: names }),
  });
  if (!r.ok) throw new Error('zip failed');
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = (dir || 'output') + '.zip';
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

// group selected composite keys by their folder
function selByDir() {
  const byDir = new Map();
  for (const k of sel) {
    const { dir, name } = parseKey(k);
    if (!byDir.has(dir)) byDir.set(dir, []);
    byDir.get(dir).push(name);
  }
  return byDir;
}
// run a POST bulk op per source folder, summing the result arrays
async function bulkOp(url, extra) {
  let done = 0, skip = 0;
  for (const [dir, names] of selByDir()) {
    try {
      const r = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({ dir, files: names }, extra || {})),
      });
      const d = await r.json();
      done += (d.moved || d.trashed || d.deleted || []).length;
      skip += (d.skipped || []).length;
    } catch (e) { /* count as skip via difference */ }
  }
  return { done, skip };
}

// ---- toolbar ----
$('reload').onclick = () => { lastStat = null; refreshCurrent(); };
$('all').onclick = () => {
  view.forEach(f => sel.add(keyOf(f)));
  for (const fig of grid.querySelectorAll('figure')) fig.classList.add('sel');
  refreshButtons();
};
$('none').onclick = () => {
  sel.clear();
  for (const fig of grid.querySelectorAll('figure')) fig.classList.remove('sel');
  refreshButtons();
};
function getMoveDst() {
  let v = $('dstsel').value;
  if (v === '__new__') {
    const name = (window.prompt('新しいフォルダ名を入力') || '').trim().replace(/[\\/]+/g, '');
    if (!name) return null;
    v = name;
  }
  return v;
}
$('move').onclick = async () => {
  if (!sel.size) return;
  const dst = getMoveDst();
  if (dst === null) return;
  $('move').disabled = true;
  const { done, skip } = await bulkOp('/explore_gallery/move', { dst });
  dstDir = dst;
  toast(done + ' 枚を ' + dirLabel(dst) + ' へ' + (isTrash() ? '復元' : '移動')
    + (skip ? '（' + skip + ' 枚スキップ）' : ''), done ? 'ok' : 'err');
  sel.clear(); await refreshCurrent();
};
$('del').onclick = async () => {
  if (!sel.size) return;
  if (isTrash()) {
    if (!confirm(sel.size + ' 枚を完全に削除します。元に戻せません。')) return;
    $('del').disabled = true;
    const { done, skip } = await bulkOp('/explore_gallery/delete', {});
    toast(done + ' 枚を完全削除' + (skip ? '（' + skip + ' 枚スキップ）' : ''), done ? 'ok' : 'err');
  } else {
    if (!confirm(sel.size + ' 枚を _trash へ移動します。（後で復元できます）')) return;
    $('del').disabled = true;
    const { done, skip } = await bulkOp('/explore_gallery/trash', {});
    toast(done + ' 枚をゴミ箱へ' + (skip ? '（' + skip + ' 枚スキップ）' : ''), done ? 'ok' : 'err');
  }
  sel.clear(); await refreshCurrent();
};
$('dl').onclick = async () => {
  if (!sel.size) return;
  const byDir = selByDir();
  try {
    if (sel.size === 1) {
      const [dir, names] = [...byDir][0];
      downloadOne({ dir, name: names[0] });
    } else {
      toast('ZIP生成中…');
      for (const [dir, names] of byDir) await downloadZipDir(dir, names);
    }
  } catch (e) { toast('ダウンロードに失敗しました', 'err'); }
};
$('dlfolder').onclick = async () => {
  if (!files.length || crossSearch) return;
  toast('ZIP生成中…');
  try { await downloadZipDir(curDir, []); }
  catch (e) { toast('ZIPの生成に失敗しました', 'err'); }
};

// ---- filters ----
$('search').oninput = () => {
  searchTerm = $('search').value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(doSearch, 250);
};
$('metasearch').onchange = () => {
  metaSearch = $('metasearch').checked;
  if (searchTerm.trim()) doSearch();
};
$('sortkey').onchange = () => { sortKey = $('sortkey').value; applyView(); };
$('sortdir').onclick = () => {
  sortDir = -sortDir;
  $('sortdir').textContent = sortDir < 0 ? '▼' : '▲';
  applyView();
};
function applyThumbSize(px) {
  document.documentElement.style.setProperty('--thumb', px + 'px');
  localStorage.setItem('eg_thumb', px);
}
$('thumbsize').onchange = () => applyThumbSize($('thumbsize').value);
$('autorefresh').onchange = () => setAuto($('autorefresh').checked);

// ---- rubber-band (drag) selection ----
function syncSelClasses() {
  for (const fig of grid.querySelectorAll('figure'))
    fig.classList.toggle('sel', sel.has(fig.dataset.key));
}
let marqStart = null, marqBox = null, marqBase = null, marqing = false, justDragged = false;

grid.addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  // don't start on interactive bits (buttons, prompt rows, links, the scrollbar)
  if (e.target.closest('button, select, input, a, .prompts')) return;
  marqStart = { x: e.clientX, y: e.clientY };
  // additive when a modifier is held; otherwise a fresh marquee replaces selection
  marqBase = (e.shiftKey || e.ctrlKey || e.metaKey) ? new Set(sel) : new Set();
  marqing = false;
});
window.addEventListener('mousemove', (e) => {
  if (!marqStart) return;
  const dx = e.clientX - marqStart.x, dy = e.clientY - marqStart.y;
  if (!marqing) {
    if (Math.abs(dx) + Math.abs(dy) < 6) return;   // movement threshold
    marqing = true;
    document.body.classList.add('marqing');
    marqBox = document.createElement('div');
    marqBox.id = 'marquee';
    document.body.appendChild(marqBox);
  }
  const x1 = Math.min(e.clientX, marqStart.x), y1 = Math.min(e.clientY, marqStart.y);
  const x2 = Math.max(e.clientX, marqStart.x), y2 = Math.max(e.clientY, marqStart.y);
  marqBox.style.left = x1 + 'px'; marqBox.style.top = y1 + 'px';
  marqBox.style.width = (x2 - x1) + 'px'; marqBox.style.height = (y2 - y1) + 'px';

  const next = new Set(marqBase);
  for (const fig of grid.querySelectorAll('figure')) {
    const r = fig.getBoundingClientRect();
    if (!(r.right < x1 || r.left > x2 || r.bottom < y1 || r.top > y2))
      next.add(fig.dataset.key);
  }
  sel.clear(); for (const k of next) sel.add(k);
  syncSelClasses(); refreshButtons();
  e.preventDefault();
});
window.addEventListener('mouseup', () => {
  if (!marqStart) return;
  const wasDragging = marqing;
  marqStart = null; marqing = false;
  if (marqBox) { marqBox.remove(); marqBox = null; }
  document.body.classList.remove('marqing');
  if (wasDragging) { justDragged = true; setTimeout(() => { justDragged = false; }, 0); }
});
// swallow the click that fires right after a drag so it doesn't toggle a card
grid.addEventListener('click', (e) => {
  if (justDragged) { e.stopPropagation(); e.preventDefault(); }
}, true);

// ---- drag & drop upload from the OS (mainly for RunPod) ----
const hasFiles = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files');
let dragDepth = 0;
function showDrop() {
  $('dropzone').querySelector('.dz-folder').textContent = dirLabel(curDir);
  $('dropzone').hidden = false;
}
function hideDrop() { $('dropzone').hidden = true; }
window.addEventListener('dragenter', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth++; showDrop();
});
window.addEventListener('dragover', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); e.dataTransfer.dropEffect = 'copy';
});
window.addEventListener('dragleave', (e) => {
  if (!hasFiles(e)) return;
  dragDepth--; if (dragDepth <= 0) { dragDepth = 0; hideDrop(); }
});
window.addEventListener('drop', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; hideDrop();
  if (e.dataTransfer.files && e.dataTransfer.files.length)
    uploadFiles(e.dataTransfer.files);
});
async function uploadFiles(fileList) {
  const imgs = [...fileList].filter(f => /\.(png|jpe?g|webp|bmp|gif)$/i.test(f.name));
  if (!imgs.length) { toast('画像ファイルが見つかりません', 'err'); return; }
  const fd = new FormData();
  for (const f of imgs) fd.append('files', f, f.name);
  toast('アップロード中… (' + imgs.length + ' 枚)');
  try {
    const r = await fetch('/explore_gallery/upload?dir=' + enc(curDir), { method: 'POST', body: fd });
    const d = await r.json();
    const n = (d.saved || []).length, s = (d.skipped || []).length;
    toast(n + ' 枚を「' + dirLabel(curDir) + '」へアップロード'
      + (s ? '（' + s + ' 枚スキップ）' : ''), n ? 'ok' : 'err', 3500);
    if (n) { lastStat = null; await refreshCurrent(); }
  } catch (e) { toast('アップロードに失敗しました', 'err'); }
}

// ---- init persisted prefs ----
(function initPrefs() {
  const t = localStorage.getItem('eg_thumb') || '190';
  $('thumbsize').value = ['140', '190', '260'].includes(t) ? t : '190';
  applyThumbSize($('thumbsize').value);
  const auto = localStorage.getItem('eg_auto') === '1';
  $('autorefresh').checked = auto;
  if (auto) setAuto(true);
})();

reloadAll();
