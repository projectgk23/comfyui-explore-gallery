# -*- coding: utf-8 -*-
"""Explore Gallery — a small ComfyUI extension to browse the output folders in
the browser, enlarge images, move picks into output/selected, delete, and
download (single file or whole-folder zip).

Why: when ComfyUI runs on a remote box (RunPod), there is no OS file explorer
to triage generated images. This piggy-backs on ComfyUI's own aiohttp server
(port 8188), so no extra port / Jupyter / proxy sub-path is needed.
Open  https://[pod]-8188.proxy.runpod.net/explore_gallery .

Paths are resolved via folder_paths.get_output_directory(), so the same file
works locally and on RunPod (/workspace/runpod-slim/ComfyUI/output).
"""
import io
import os
import shutil
import zipfile

from aiohttp import web

import folder_paths
from server import PromptServer

DST_SUB = "selected"   # move target
TRASH_SUB = "_trash"   # recoverable bin (hidden from tabs: leading "_")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")

# cache: (path, mtime, size) -> (w, h)
_dim_cache = {}


def _output_root():
    # resolved lazily so a runtime change to the output dir is honored
    return os.path.realpath(folder_paths.get_output_directory())


def _resolve_dir(sub):
    """Resolve a subfolder under output root, guarding against traversal.

    Returns an absolute path, or None if it escapes the output root.
    sub == "" means the output root itself.
    """
    root = _output_root()
    target = os.path.realpath(os.path.join(root, sub or ""))
    try:
        if os.path.commonpath([target, root]) != root:
            return None
    except ValueError:  # different drive on Windows, etc.
        return None
    return target


def _safe_name(name):
    """Reduce any client-supplied name to a bare image filename."""
    base = os.path.basename(str(name))
    if base in ("", ".", ".."):
        return None
    if not base.lower().endswith(IMG_EXT):
        return None
    return base


def _dimensions(path, st):
    key = (path, st.st_mtime, st.st_size)
    if key in _dim_cache:
        return _dim_cache[key]
    wh = (None, None)
    try:
        from PIL import Image
        with Image.open(path) as im:
            wh = (im.width, im.height)
    except Exception:  # noqa: BLE001 — missing PIL / unreadable file
        pass
    _dim_cache[key] = wh
    return wh


def _list_dirs():
    """Top-level subfolders under output (hidden/_-prefixed excluded) plus the
    root entry, each with an image count."""
    root = _output_root()
    entries = []

    def count_images(d):
        try:
            return sum(1 for f in os.listdir(d)
                       if f.lower().endswith(IMG_EXT)
                       and os.path.isfile(os.path.join(d, f)))
        except OSError:
            return 0

    root_count = count_images(root)
    if root_count:
        entries.append({"dir": "", "label": "(ルート)", "count": root_count})

    try:
        names = sorted(os.listdir(root), key=str.lower)
    except OSError:
        names = []
    for name in names:
        if name.startswith((".", "_")):
            continue
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        entries.append({"dir": name, "label": name, "count": count_images(p)})
    return entries


def _list_images(sub):
    d = _resolve_dir(sub)
    if d is None or not os.path.isdir(d):
        return []
    items = []
    for fn in os.listdir(d):
        if not fn.lower().endswith(IMG_EXT):
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        w, h = _dimensions(p, st)
        items.append({
            "name": fn,
            "mtime": st.st_mtime,
            "size": st.st_size,
            "w": w,
            "h": h,
        })
    items.sort(key=lambda it: it["mtime"], reverse=True)  # newest first
    return items


def _unique_dest(dst_dir, name):
    stem, ext = os.path.splitext(name)
    candidate = os.path.join(dst_dir, name)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dst_dir, f"{stem}_{i}{ext}")
        i += 1
    return candidate


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #
@PromptServer.instance.routes.get("/explore_gallery")
async def explore_gallery_page(request):
    return web.Response(text=_PAGE, content_type="text/html")


@PromptServer.instance.routes.get("/explore_gallery/dirs")
async def explore_gallery_dirs(request):
    return web.json_response({"dirs": _list_dirs(), "dst": DST_SUB})


@PromptServer.instance.routes.get("/explore_gallery/list")
async def explore_gallery_list(request):
    sub = request.query.get("dir", "")
    return web.json_response({"dir": sub, "dst": DST_SUB, "files": _list_images(sub)})


@PromptServer.instance.routes.post("/explore_gallery/move")
async def explore_gallery_move(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    src_dir = _resolve_dir(data.get("dir", ""))
    if src_dir is None:
        return web.json_response({"error": "bad dir"}, status=400)
    dst_dir = _resolve_dir(DST_SUB)
    os.makedirs(dst_dir, exist_ok=True)

    moved, skipped = [], []
    for raw in (data.get("files") or []):
        name = _safe_name(raw)
        if name is None:
            skipped.append({"file": raw, "reason": "invalid name"})
            continue
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            skipped.append({"file": raw, "reason": "not found"})
            continue
        if os.path.realpath(src_dir) == os.path.realpath(dst_dir):
            skipped.append({"file": raw, "reason": "already in selected"})
            continue
        try:
            shutil.move(src, _unique_dest(dst_dir, name))
            moved.append(name)
        except Exception as e:  # noqa: BLE001
            skipped.append({"file": raw, "reason": str(e)})
    return web.json_response({"moved": moved, "skipped": skipped})


@PromptServer.instance.routes.post("/explore_gallery/trash")
async def explore_gallery_trash(request):
    """Move files into output/_trash (recoverable) rather than deleting."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    src_dir = _resolve_dir(data.get("dir", ""))
    if src_dir is None:
        return web.json_response({"error": "bad dir"}, status=400)
    trash_dir = _resolve_dir(TRASH_SUB)
    os.makedirs(trash_dir, exist_ok=True)

    trashed, skipped = [], []
    for raw in (data.get("files") or []):
        name = _safe_name(raw)
        if name is None:
            skipped.append({"file": raw, "reason": "invalid name"})
            continue
        if os.path.realpath(src_dir) == os.path.realpath(trash_dir):
            skipped.append({"file": raw, "reason": "already in trash"})
            continue
        p = os.path.join(src_dir, name)
        if not os.path.isfile(p):
            skipped.append({"file": raw, "reason": "not found"})
            continue
        try:
            shutil.move(p, _unique_dest(trash_dir, name))
            trashed.append(name)
        except Exception as e:  # noqa: BLE001
            skipped.append({"file": raw, "reason": str(e)})
    return web.json_response({"trashed": trashed, "skipped": skipped})


@PromptServer.instance.routes.get("/explore_gallery/download")
async def explore_gallery_download(request):
    d = _resolve_dir(request.query.get("dir", ""))
    name = _safe_name(request.query.get("file", ""))
    if d is None or name is None:
        return web.Response(status=400, text="bad request")
    p = os.path.join(d, name)
    if not os.path.isfile(p):
        return web.Response(status=404, text="not found")
    return web.FileResponse(p, headers={
        "Content-Disposition": f'attachment; filename="{name}"',
    })


@PromptServer.instance.routes.post("/explore_gallery/zip")
async def explore_gallery_zip(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    d = _resolve_dir(data.get("dir", ""))
    if d is None:
        return web.json_response({"error": "bad dir"}, status=400)

    names = data.get("files")
    if not names:  # whole folder
        names = [f for f in os.listdir(d)
                 if f.lower().endswith(IMG_EXT)
                 and os.path.isfile(os.path.join(d, f))]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # PNG already compressed
        for raw in names:
            name = _safe_name(raw)
            if name is None:
                continue
            p = os.path.join(d, name)
            if os.path.isfile(p):
                z.write(p, name)
    buf.seek(0)

    label = os.path.basename(d.rstrip("/\\")) or "output"
    return web.Response(body=buf.read(), headers={
        "Content-Type": "application/zip",
        "Content-Disposition": f'attachment; filename="{label}.zip"',
    })


# --------------------------------------------------------------------------- #
#  Inline page (no separate web assets needed)
# --------------------------------------------------------------------------- #
_PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Explore Gallery</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#1b1b1f; color:#eee;
         font-family: system-ui, "Segoe UI", sans-serif; }
  header { position: sticky; top:0; z-index:10;
           padding:10px 14px; background:#26262c; border-bottom:1px solid #3a3a42; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .row + .row { margin-top:8px; }
  h1 { font-size:15px; margin:0 8px 0 0; font-weight:600; }
  button { background:#3a3a44; color:#eee; border:1px solid #50505c;
           border-radius:6px; padding:7px 12px; font-size:13px; cursor:pointer; }
  button:hover:not(:disabled) { background:#46465c; }
  button.primary { background:#3b6; border-color:#4c7; color:#04200f; font-weight:600; }
  button.danger  { background:#a33; border-color:#c55; color:#fee; }
  button:disabled { opacity:.4; cursor:default; }
  .tab { background:#2f2f37; }
  .tab.active { background:#4a6cff; border-color:#6a8bff; color:#fff; font-weight:600; }
  .tab .cnt { opacity:.7; font-size:11px; margin-left:4px; }
  #count { margin-left:auto; font-size:13px; opacity:.85; white-space:nowrap; }
  #msg { font-size:12px; opacity:.85; min-width:80px; }
  #grid { display:grid; gap:10px; padding:12px;
          grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); }
  figure { margin:0; position:relative; border:3px solid transparent;
           border-radius:8px; overflow:hidden; background:#111; cursor:pointer; }
  .thumb { position:relative; aspect-ratio:1/1; background:#000; }
  .thumb img { width:100%; height:100%; object-fit:contain; display:block; }
  figure.sel { border-color:#3b6; }
  figure.sel .thumb::after { content:"✓"; position:absolute; top:5px; right:6px;
           background:#3b6; color:#04200f; font-weight:700;
           width:22px; height:22px; border-radius:50%; display:flex;
           align-items:center; justify-content:center; font-size:14px; }
  .zoom { position:absolute; top:5px; left:6px; width:24px; height:24px;
          border-radius:6px; border:none; background:#000a; color:#fff;
          font-size:13px; padding:0; display:none; align-items:center; justify-content:center; }
  .thumb:hover .zoom { display:flex; }
  figcaption { font-size:11px; padding:4px 6px; line-height:1.4;
           background:#222; border-top:1px solid #333; }
  figcaption .nm { display:block; white-space:nowrap; overflow:hidden;
           text-overflow:ellipsis; }
  figcaption .meta { opacity:.7; font-size:10px; }
  #empty { padding:40px; text-align:center; opacity:.6; }

  /* lightbox */
  #lb { position:fixed; inset:0; background:#000d; z-index:100; display:none; }
  #lb.open { display:flex; flex-direction:column; }
  #lb .stage { flex:1; min-height:0; width:100%; display:flex;
        align-items:center; justify-content:center; position:relative; }
  .imgwrap { position:relative; display:inline-block; line-height:0; }
  #lbimg { max-width:92vw; max-height:calc(100vh - 140px); display:block; cursor:zoom-out; }
  /* filmstrip */
  .film { flex:0 0 auto; height:118px; background:#0009; border-top:1px solid #333;
        display:flex; flex-direction:column; }
  .filminfo { display:flex; gap:16px; align-items:center; padding:5px 12px; font-size:12px; }
  .filminfo #lbpos { font-weight:600; color:#9ab4ff; }
  .filminfo .legend { opacity:.5; }
  .filminfo .legend kbd { background:#3338; border:1px solid #5556; border-radius:4px;
        padding:0 5px; font-family:inherit; font-size:11px; }
  .filmstrip { flex:1; display:flex; gap:6px; overflow-x:auto; overflow-y:hidden;
        padding:0 12px 8px; align-items:center; }
  .filmstrip img { height:100%; aspect-ratio:1/1; object-fit:cover; flex:0 0 auto;
        border:2px solid #0000; border-radius:4px; cursor:pointer; opacity:.55; }
  .filmstrip img:hover { opacity:.85; }
  .filmstrip img.cur { opacity:1; border-color:#4a6cff; box-shadow:0 0 0 2px #4a6cff; }
  .lbtools { position:absolute; top:6px; right:6px; display:flex; gap:6px; z-index:2; }
  .lbtools button { width:34px; height:34px; padding:0; border-radius:8px;
        border:1px solid #0006; background:#000a; color:#fff; font-size:16px;
        display:flex; align-items:center; justify-content:center; cursor:pointer; }
  .lbtools button:hover { background:#000d; }
  .lbtools .selbtn { color:#fff8; }
  .lbtools .selbtn.on { background:#3b6; border-color:#4c7; color:#04200f; }
  #lbcap { position:absolute; left:0; right:0; bottom:0; pointer-events:none;
        font-size:12px; line-height:1.4; padding:6px 10px;
        background:linear-gradient(transparent, #000c); color:#fff; }
  #lbcap .meta { opacity:.75; margin-left:8px; }
  #lb .nav { position:absolute; top:0; bottom:0; width:14%; border:none;
        background:transparent; color:#fff8; font-size:48px; cursor:pointer; z-index:1; }
  #lb .nav:hover { color:#fff; background:#ffffff14; }
  #lb .prev { left:0; } #lb .next { right:0; }
</style>
</head>
<body>
<header>
  <div class="row" id="tabs"></div>
  <div class="row">
    <button id="reload">🔄 再読み込み</button>
    <button id="all">全選択</button>
    <button id="none">全解除</button>
    <button id="move" class="primary" disabled>→ selectedへ移動</button>
    <button id="dl" disabled>⬇ 選択をDL</button>
    <button id="dlfolder">⬇ フォルダZIP</button>
    <button id="del" class="danger" disabled>🗑 ゴミ箱へ</button>
    <span id="msg"></span>
    <span id="count">0 / 0</span>
  </div>
</header>
<div id="grid"></div>
<div id="empty" hidden>このフォルダに画像がありません。</div>

<div id="lb">
  <div class="stage">
    <button class="nav prev" id="lbprev">‹</button>
    <div class="imgwrap">
      <img id="lbimg" alt="">
      <div class="lbtools">
        <button id="lbsel" class="selbtn" title="選択トグル">✓</button>
        <button id="lbdl" title="ダウンロード">⬇</button>
        <button id="lbclose" title="閉じる">✕</button>
      </div>
      <div id="lbcap"></div>
    </div>
    <button class="nav next" id="lbnext">›</button>
  </div>
  <div class="film">
    <div class="filminfo">
      <span id="lbpos">0 / 0</span>
      <span class="legend"><kbd>←</kbd><kbd>→</kbd> 送り　<kbd>↑</kbd> 選択　<kbd>↓</kbd> ゴミ箱　<kbd>Esc</kbd> 閉じる</span>
    </div>
    <div class="filmstrip" id="lbstrip"></div>
  </div>
</div>

<script>
const grid = document.getElementById('grid');
const empty = document.getElementById('empty');
const msg = document.getElementById('msg');
const sel = new Set();
let curDir = '';
let dstDir = 'selected';
let files = [];          // [{name,w,h,mtime,size}]
let lbIndex = -1;

const $ = id => document.getElementById(id);
const enc = encodeURIComponent;

function viewUrl(name, preview) {
  let u = '/view?filename=' + enc(name) + '&subfolder=' + enc(curDir)
        + '&type=output';
  if (preview) u += '&preview=webp';
  return u;
}
function fmtDate(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate())
       + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function fmtSize(b) {
  if (b >= 1048576) return (b/1048576).toFixed(1) + 'MB';
  if (b >= 1024) return (b/1024).toFixed(0) + 'KB';
  return b + 'B';
}

function refreshButtons() {
  $('count').textContent = sel.size + ' / ' + files.length;
  const has = sel.size > 0;
  $('move').disabled = !has || curDir === dstDir;
  $('dl').disabled = !has;
  $('del').disabled = !has;
  $('dlfolder').disabled = files.length === 0;
}

function renderTabs(dirs) {
  const t = $('tabs');
  t.innerHTML = '<h1>📂 Explore Gallery</h1>';
  for (const d of dirs) {
    const b = document.createElement('button');
    b.className = 'tab' + (d.dir === curDir ? ' active' : '');
    b.innerHTML = d.label + '<span class="cnt">' + d.count + '</span>';
    b.onclick = () => { if (d.dir !== curDir) { curDir = d.dir; sel.clear(); loadList(); markActiveTab(); } };
    b.dataset.dir = d.dir;
    t.appendChild(b);
  }
}
function markActiveTab() {
  for (const b of document.querySelectorAll('#tabs .tab'))
    b.classList.toggle('active', b.dataset.dir === curDir);
}

function render() {
  grid.innerHTML = '';
  empty.hidden = files.length > 0;
  files.forEach((f, i) => {
    const fig = document.createElement('figure');
    if (sel.has(f.name)) fig.classList.add('sel');
    fig.dataset.name = f.name;

    const thumb = document.createElement('div');
    thumb.className = 'thumb';
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.src = viewUrl(f.name, true);
    const zoom = document.createElement('button');
    zoom.className = 'zoom'; zoom.textContent = '🔍'; zoom.title = '拡大';
    zoom.onclick = (e) => { e.stopPropagation(); openLightbox(i); };
    thumb.append(img, zoom);

    const cap = document.createElement('figcaption');
    const dim = (f.w && f.h) ? (f.w + '×' + f.h) : '—';
    cap.innerHTML = '<span class="nm">' + f.name + '</span>'
      + '<span class="meta">' + dim + ' · ' + fmtSize(f.size)
      + ' · ' + fmtDate(f.mtime) + '</span>';

    fig.append(thumb, cap);
    fig.onclick = () => {
      if (sel.has(f.name)) { sel.delete(f.name); fig.classList.remove('sel'); }
      else { sel.add(f.name); fig.classList.add('sel'); }
      refreshButtons();
    };
    fig.ondblclick = (e) => { e.preventDefault(); openLightbox(i); };
    grid.appendChild(fig);
  });
  refreshButtons();
}

async function loadDirs() {
  const d = await (await fetch('/explore_gallery/dirs')).json();
  dstDir = d.dst || 'selected';
  const dirs = d.dirs || [];
  if (!dirs.some(x => x.dir === curDir)) curDir = dirs.length ? dirs[0].dir : '';
  renderTabs(dirs);
}

async function loadList() {
  msg.textContent = '読み込み中…';
  const d = await (await fetch('/explore_gallery/list?dir=' + enc(curDir))).json();
  files = d.files || [];
  dstDir = d.dst || dstDir;
  for (const s of [...sel]) if (!files.some(f => f.name === s)) sel.delete(s);
  render();
  msg.textContent = '';
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
  files.forEach((f, i) => {
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
  const f = files[lbIndex];
  if (!f) return;
  $('lbimg').src = viewUrl(f.name, false);
  const dim = (f.w && f.h) ? (f.w + '×' + f.h) : '—';
  $('lbcap').innerHTML = '<b>' + f.name + '</b><span class="meta">'
    + dim + ' · ' + fmtSize(f.size) + ' · ' + fmtDate(f.mtime) + '</span>';
  $('lbpos').textContent = (lbIndex + 1) + ' / ' + files.length;
  $('lbsel').classList.toggle('on', sel.has(f.name));
  highlightStrip();
}
function closeLightbox() { $('lb').classList.remove('open'); lbIndex = -1; }
function step(n) {
  if (lbIndex < 0) return;
  lbIndex = (lbIndex + n + files.length) % files.length;
  showLightbox();
}
function toggleCurrentSel() {
  const f = files[lbIndex];
  if (!f) return;
  if (sel.has(f.name)) sel.delete(f.name); else sel.add(f.name);
  const fig = grid.querySelector('figure[data-name="' + CSS.escape(f.name) + '"]');
  if (fig) fig.classList.toggle('sel', sel.has(f.name));
  showLightbox();
  refreshButtons();
}
async function trashCurrent() {
  const f = files[lbIndex];
  if (!f) return;
  await fetch('/explore_gallery/trash', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dir: curDir, files: [f.name] }),
  });
  sel.delete(f.name);
  files.splice(lbIndex, 1);
  msg.textContent = '1 枚をゴミ箱へ';
  if (files.length === 0) { closeLightbox(); render(); loadDirs(); return; }
  if (lbIndex >= files.length) lbIndex = files.length - 1;
  render();        // grid
  buildStrip();    // strip (indices shifted)
  showLightbox();  // new current image
  loadDirs();      // tab counts
}
const stop = (fn) => (e) => { e.stopPropagation(); fn(e); };
$('lbclose').onclick = stop(closeLightbox);
$('lbprev').onclick = stop(() => step(-1));
$('lbnext').onclick = stop(() => step(1));
$('lbdl').onclick = stop(() => { const f = files[lbIndex]; if (f) downloadOne(f.name); });
$('lbsel').onclick = stop(toggleCurrentSel);
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
  const r = await fetch('/explore_gallery/zip', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dir: curDir, files: names }),
  });
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = (curDir || 'output') + '.zip';
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  msg.textContent = '';
}

// ---- toolbar ----
$('reload').onclick = reloadAll;
$('all').onclick = () => { files.forEach(f => sel.add(f.name)); render(); };
$('none').onclick = () => { sel.clear(); render(); };
$('move').onclick = async () => {
  const picked = [...sel]; if (!picked.length) return;
  $('move').disabled = true; msg.textContent = '移動中…';
  const r = await fetch('/explore_gallery/move', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dir: curDir, files: picked }),
  });
  const d = await r.json();
  const sk = (d.skipped || []).length;
  msg.textContent = (d.moved || []).length + ' 枚を selected へ移動'
    + (sk ? ('（' + sk + ' 枚スキップ）') : '');
  sel.clear(); await reloadAll();
};
$('del').onclick = async () => {
  const picked = [...sel]; if (!picked.length) return;
  if (!confirm(picked.length + ' 枚を _trash へ移動します。（後で復元できます）')) return;
  $('del').disabled = true; msg.textContent = 'ゴミ箱へ移動中…';
  const r = await fetch('/explore_gallery/trash', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dir: curDir, files: picked }),
  });
  const d = await r.json();
  const sk = (d.skipped || []).length;
  msg.textContent = (d.trashed || []).length + ' 枚をゴミ箱へ'
    + (sk ? ('（' + sk + ' 枚スキップ）') : '');
  sel.clear(); await reloadAll();
};
$('dl').onclick = () => {
  const picked = [...sel]; if (!picked.length) return;
  if (picked.length === 1) downloadOne(picked[0]);
  else downloadZip(picked);
};
$('dlfolder').onclick = () => { if (files.length) downloadZip([]); };

reloadAll();
</script>
</body>
</html>
"""

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = None

print("[explore-gallery] route ready: GET /explore_gallery")
