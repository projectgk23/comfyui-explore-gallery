"use strict";
const grid = document.getElementById('grid');
const empty = document.getElementById('empty');
const msg = document.getElementById('msg');
const sel = new Set();

let curDir = '';
let dstDir = 'selected';
let files = [];          // full list from server: [{name,w,h,mtime,size}]
let view = [];           // filtered + sorted subset currently shown
let lbIndex = -1;        // index into `view`
let searchTerm = '';
let sortKey = 'mtime';   // mtime | name | size
let sortDir = -1;        // 1 = ascending, -1 = descending
let metaOpen = false;

const BATCH = 120;       // how many figures to add per incremental render
let rendered = 0;        // how many of `view` are in the DOM
const promptCache = new Map();  // name -> {pos, neg} (lazy, cleared per folder)

const $ = id => document.getElementById(id);
const enc = encodeURIComponent;
function esc(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function viewUrl(name, preview) {
  if (preview)
    return '/explore_gallery/thumb?dir=' + enc(curDir) + '&file=' + enc(name);
  return '/view?filename=' + enc(name) + '&subfolder=' + enc(curDir) + '&type=output';
}
function fmtDate(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
       + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function fmtSize(b) {
  if (b >= 1048576) return (b / 1048576).toFixed(1) + 'MB';
  if (b >= 1024) return (b / 1024).toFixed(0) + 'KB';
  return b + 'B';
}

function refreshButtons() {
  $('count').textContent = sel.size + ' / ' + view.length;
  const has = sel.size > 0;
  $('move').disabled = !has || curDir === dstDir;
  $('dl').disabled = !has;
  $('del').disabled = !has;
  $('dlfolder').disabled = files.length === 0;
}

// ---- tabs ----
function renderTabs(dirs) {
  const t = $('tabs');
  t.innerHTML = '<h1>📂 Explore Gallery</h1>';
  for (const d of dirs) {
    const b = document.createElement('button');
    b.className = 'tab' + (d.dir === curDir ? ' active' : '');
    b.innerHTML = esc(d.label) + '<span class="cnt">' + d.count + '</span>';
    b.onclick = () => { if (d.dir !== curDir) { curDir = d.dir; sel.clear(); loadList(); markActiveTab(); } };
    b.dataset.dir = d.dir;
    t.appendChild(b);
  }
}
function markActiveTab() {
  for (const b of document.querySelectorAll('#tabs .tab'))
    b.classList.toggle('active', b.dataset.dir === curDir);
}

// ---- incremental grid render ----
const sentinel = document.createElement('div');
sentinel.id = 'sentinel';
const io = new IntersectionObserver((entries) => {
  if (entries.some(e => e.isIntersecting)) renderMore();
}, { root: null, rootMargin: '800px' });

// lazily fetch positive/negative prompts only for cards that scroll into view
const pio = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (e.isIntersecting) { pio.unobserve(e.target); fillPrompts(e.target); }
  }
}, { root: null, rootMargin: '300px' });

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
    tx.title = text;
    tx.classList.remove('empty');
    cp.disabled = false;
  } else {
    tx.dataset.full = '';
    tx.textContent = '—';
    tx.title = '';
    tx.classList.add('empty');
    cp.disabled = true;
  }
}
async function fillPrompts(fig) {
  const name = fig.dataset.name;
  let pn = promptCache.get(name);
  if (!pn) {
    try {
      const r = await fetch('/explore_gallery/meta?dir=' + enc(curDir) + '&file=' + enc(name));
      const d = await r.json();
      pn = extractPN(d.fields);
    } catch (e) { pn = { pos: '', neg: '' }; }
    promptCache.set(name, pn);
  }
  setProw(fig, 'pos', pn.pos);
  setProw(fig, 'neg', pn.neg);
}
async function copyText(btn, text) {
  if (!text) return;
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch (e) {  // insecure context / older browser fallback
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
  } else {
    msg.textContent = 'コピーできませんでした';
  }
}
function makeProw(cls, label) {
  const row = document.createElement('div');
  row.className = 'prow ' + cls;
  const lb = document.createElement('span');
  lb.className = 'plabel'; lb.textContent = label;
  const tx = document.createElement('span');
  tx.className = 'ptext'; tx.textContent = '…';
  const cp = document.createElement('button');
  cp.className = 'copy'; cp.textContent = '⧉'; cp.title = 'コピー'; cp.disabled = true;
  cp.onclick = (e) => { e.stopPropagation(); copyText(cp, tx.dataset.full || ''); };
  row.append(lb, tx, cp);
  return row;
}

function makeFigure(f, i) {
  const fig = document.createElement('figure');
  if (sel.has(f.name)) fig.classList.add('sel');
  fig.dataset.name = f.name;

  const thumb = document.createElement('div');
  thumb.className = 'thumb';
  const img = document.createElement('img');
  img.loading = 'lazy';
  img.decoding = 'async';
  img.src = viewUrl(f.name, true);
  const zoom = document.createElement('button');
  zoom.className = 'zoom'; zoom.textContent = '🔍'; zoom.title = '拡大';
  zoom.onclick = (e) => { e.stopPropagation(); openLightbox(i); };
  thumb.append(img, zoom);

  const cap = document.createElement('figcaption');
  const dim = (f.w && f.h) ? (f.w + '×' + f.h) : '—';
  cap.innerHTML = '<span class="nm">' + esc(f.name) + '</span>'
    + '<span class="meta">' + dim + ' · ' + fmtSize(f.size)
    + ' · ' + fmtDate(f.mtime) + '</span>';

  const prompts = document.createElement('div');
  prompts.className = 'prompts';
  prompts.onclick = (e) => e.stopPropagation();  // don't toggle selection here
  prompts.append(makeProw('pos', 'P'), makeProw('neg', 'N'));

  fig.append(thumb, cap, prompts);
  fig.onclick = () => {
    if (sel.has(f.name)) { sel.delete(f.name); fig.classList.remove('sel'); }
    else { sel.add(f.name); fig.classList.add('sel'); }
    refreshButtons();
  };
  fig.ondblclick = (e) => { e.preventDefault(); openLightbox(i); };
  pio.observe(fig);  // lazily load P/N prompts when scrolled into view
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
  pio.disconnect();  // drop observers on figures we're about to discard
  grid.innerHTML = '';
  rendered = 0;
  grid.appendChild(sentinel);
  empty.hidden = view.length > 0;
  io.observe(sentinel);   // re-observing the same node is a no-op if already observed
  renderMore();
  refreshButtons();
}

function applyView() {
  const q = searchTerm.trim().toLowerCase();
  let v = q ? files.filter(f => f.name.toLowerCase().includes(q)) : files.slice();
  v.sort((a, b) => {
    let r;
    if (sortKey === 'name') r = a.name.localeCompare(b.name);
    else r = (a[sortKey] || 0) - (b[sortKey] || 0);
    return r * sortDir;
  });
  view = v;
  renderReset();
}

function syncSelClasses() {
  for (const fig of grid.querySelectorAll('figure'))
    fig.classList.toggle('sel', sel.has(fig.dataset.name));
}

// ---- data loading ----
async function loadDirs() {
  try {
    const d = await (await fetch('/explore_gallery/dirs')).json();
    dstDir = d.dst || 'selected';
    const dirs = d.dirs || [];
    if (!dirs.some(x => x.dir === curDir)) curDir = dirs.length ? dirs[0].dir : '';
    renderTabs(dirs);
  } catch (e) {
    msg.textContent = 'フォルダ一覧の取得に失敗しました';
  }
}

async function loadList() {
  msg.textContent = '読み込み中…';
  promptCache.clear();  // prompts are folder-scoped (keyed by bare filename)
  try {
    const d = await (await fetch('/explore_gallery/list?dir=' + enc(curDir))).json();
    files = d.files || [];
    dstDir = d.dst || dstDir;
    for (const s of [...sel]) if (!files.some(f => f.name === s)) sel.delete(s);
    applyView();
    msg.textContent = '';
  } catch (e) {
    files = []; applyView();
    msg.textContent = '一覧の取得に失敗しました';
  }
}

async function reloadAll() { await loadDirs(); await loadList(); }

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
    t.src = viewUrl(f.name, true);
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
  $('lbimg').src = viewUrl(f.name, false);
  const dim = (f.w && f.h) ? (f.w + '×' + f.h) : '—';
  $('lbcap').innerHTML = '<b>' + esc(f.name) + '</b><span class="meta">'
    + dim + ' · ' + fmtSize(f.size) + ' · ' + fmtDate(f.mtime) + '</span>';
  $('lbpos').textContent = (lbIndex + 1) + ' / ' + view.length;
  $('lbsel').classList.toggle('on', sel.has(f.name));
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
  if (sel.has(f.name)) sel.delete(f.name); else sel.add(f.name);
  const fig = grid.querySelector('figure[data-name="' + CSS.escape(f.name) + '"]');
  if (fig) fig.classList.toggle('sel', sel.has(f.name));
  showLightbox();
  refreshButtons();
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
  const forName = f.name;
  panel.innerHTML = '<div class="metahint">読み込み中…</div>';
  try {
    const r = await fetch('/explore_gallery/meta?dir=' + enc(curDir) + '&file=' + enc(f.name));
    const d = await r.json();
    if (view[lbIndex] && view[lbIndex].name === forName) renderMeta(d);  // ignore stale
  } catch (e) {
    panel.innerHTML = '<div class="metahint">メタデータを取得できませんでした。</div>';
  }
}

async function trashCurrent() {
  const f = view[lbIndex];
  if (!f) return;
  let ok = false;
  try {
    const r = await fetch('/explore_gallery/trash', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir: curDir, files: [f.name] }),
    });
    const d = await r.json();
    ok = r.ok && (d.trashed || []).length > 0;
  } catch (e) { ok = false; }
  if (!ok) { msg.textContent = 'ゴミ箱への移動に失敗しました'; return; }

  sel.delete(f.name);
  files = files.filter(x => x !== f);
  view.splice(lbIndex, 1);
  msg.textContent = '1 枚をゴミ箱へ';
  if (view.length === 0) { closeLightbox(); renderReset(); loadDirs(); return; }
  if (lbIndex >= view.length) lbIndex = view.length - 1;
  renderReset();   // grid
  buildStrip();    // strip (indices shifted)
  showLightbox();  // new current image
  loadDirs();      // tab counts
}

const stop = (fn) => (e) => { e.stopPropagation(); fn(e); };
$('lbclose').onclick = stop(closeLightbox);
$('lbprev').onclick = stop(() => step(-1));
$('lbnext').onclick = stop(() => step(1));
$('lbdl').onclick = stop(() => { const f = view[lbIndex]; if (f) downloadOne(f.name); });
$('lbsel').onclick = stop(toggleCurrentSel);
$('lbinfo').onclick = stop(toggleMeta);
// single-click the enlarged image (or the dark backdrop) returns to the grid
$('lbimg').onclick = closeLightbox;
$('lb').onclick = (e) => {
  if (e.target === $('lb') || e.target.classList.contains('stage')) closeLightbox();
};
document.addEventListener('keydown', (e) => {
  if (!$('lb').classList.contains('open')) return;
  if (e.key === 'Escape') closeLightbox();
  else if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); toggleCurrentSel(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); trashCurrent(); }
  else if (e.key === 'i' || e.key === 'I') { e.preventDefault(); toggleMeta(); }
});

// ---- downloads ----
function downloadOne(name) {
  const a = document.createElement('a');
  a.href = '/explore_gallery/download?dir=' + enc(curDir) + '&file=' + enc(name);
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
}
async function downloadZip(names) {
  msg.textContent = 'ZIP生成中…';
  try {
    const r = await fetch('/explore_gallery/zip', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir: curDir, files: names }),
    });
    if (!r.ok) { msg.textContent = 'ZIPの生成に失敗しました'; return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = (curDir || 'output') + '.zip';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    msg.textContent = '';
  } catch (e) { msg.textContent = 'ZIPの生成に失敗しました'; }
}

// ---- toolbar ----
$('reload').onclick = reloadAll;
$('all').onclick = () => { view.forEach(f => sel.add(f.name)); syncSelClasses(); refreshButtons(); };
$('none').onclick = () => { sel.clear(); syncSelClasses(); refreshButtons(); };
$('move').onclick = async () => {
  const picked = [...sel]; if (!picked.length) return;
  $('move').disabled = true; msg.textContent = '移動中…';
  try {
    const r = await fetch('/explore_gallery/move', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir: curDir, files: picked }),
    });
    const d = await r.json();
    const sk = (d.skipped || []).length;
    msg.textContent = (d.moved || []).length + ' 枚を selected へ移動'
      + (sk ? ('（' + sk + ' 枚スキップ）') : '');
  } catch (e) { msg.textContent = '移動に失敗しました'; }
  sel.clear(); await reloadAll();
};
$('del').onclick = async () => {
  const picked = [...sel]; if (!picked.length) return;
  if (!confirm(picked.length + ' 枚を _trash へ移動します。（後で復元できます）')) return;
  $('del').disabled = true; msg.textContent = 'ゴミ箱へ移動中…';
  try {
    const r = await fetch('/explore_gallery/trash', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir: curDir, files: picked }),
    });
    const d = await r.json();
    const sk = (d.skipped || []).length;
    msg.textContent = (d.trashed || []).length + ' 枚をゴミ箱へ'
      + (sk ? ('（' + sk + ' 枚スキップ）') : '');
  } catch (e) { msg.textContent = 'ゴミ箱への移動に失敗しました'; }
  sel.clear(); await reloadAll();
};
$('dl').onclick = () => {
  const picked = [...sel]; if (!picked.length) return;
  if (picked.length === 1) downloadOne(picked[0]);
  else downloadZip(picked);
};
$('dlfolder').onclick = () => { if (files.length) downloadZip([]); };

// ---- filters ----
$('search').oninput = () => { searchTerm = $('search').value; applyView(); };
$('sortkey').onchange = () => { sortKey = $('sortkey').value; applyView(); };
$('sortdir').onclick = () => {
  sortDir = -sortDir;
  $('sortdir').textContent = sortDir < 0 ? '▼' : '▲';
  applyView();
};

reloadAll();
