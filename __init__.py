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
import hashlib
import io
import json
import os
import shutil
import zipfile
from urllib.parse import quote

from aiohttp import web

import folder_paths
from server import PromptServer

DST_SUB = "selected"        # move target
TRASH_SUB = "_trash"        # recoverable bin (hidden from tabs: leading "_")
CACHE_SUB = ".gallery_cache"  # thumbnail cache (hidden from tabs: leading ".")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
THUMB_MAX = 384             # longest edge of generated thumbnails (px)

WEB_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "web")

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
#  Thumbnails (persistent disk cache)
# --------------------------------------------------------------------------- #
def _cache_dir():
    d = os.path.join(_output_root(), CACHE_SUB)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _thumb_key(path, st):
    raw = f"{os.path.realpath(path)}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _make_thumb(src, dst):
    """Render a small WEBP thumbnail of `src` to `dst`. Returns True on success."""
    tmp = dst + ".tmp"
    try:
        from PIL import Image
        with Image.open(src) as im:
            try:
                im.draft("RGB", (THUMB_MAX, THUMB_MAX))  # speeds up JPEG decode
            except Exception:  # noqa: BLE001
                pass
            im = im.convert("RGB")
            im.thumbnail((THUMB_MAX, THUMB_MAX))
            im.save(tmp, "WEBP", quality=80, method=4)
        os.replace(tmp, dst)
        return True
    except Exception:  # noqa: BLE001 — missing PIL / unreadable file
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


# --------------------------------------------------------------------------- #
#  Embedded metadata (ComfyUI writes prompt/workflow into PNG text chunks)
# --------------------------------------------------------------------------- #
_TEXT_KEYS = ("string", "prompt", "populated_text", "wildcard_text",
              "value", "t5xxl", "clip_l", "clip_g")


def _resolve_text(graph, ref, depth=4):
    """Follow a graph link [node_id, slot] to the nearest node holding a literal
    prompt string. Handles CLIPTextEncode (`text`) as well as upstream string
    nodes that feed it via keys like `text`, `text_0`, `string`, etc."""
    while depth > 0 and isinstance(ref, list) and ref:
        node = graph.get(str(ref[0]))
        if not isinstance(node, dict):
            return None
        ins = node.get("inputs")
        if not isinstance(ins, dict):
            return None
        parts = []
        for k, v in ins.items():
            if isinstance(v, str) and (k == "text" or k.startswith("text")
                                       or k in _TEXT_KEYS):
                s = v.strip()
                if s:
                    parts.append(s)
        if parts:
            return "\n".join(parts)
        nxt = ins.get("text")
        ref = nxt if isinstance(nxt, list) else ins.get("conditioning")
        depth -= 1
    return None


def _summarize_prompt(prompt_str):
    """Best-effort extraction of common generation fields from a ComfyUI
    API-prompt graph. Works across model types by scanning input keys."""
    fields = []
    if not prompt_str:
        return fields
    try:
        graph = json.loads(prompt_str)
    except Exception:  # noqa: BLE001
        return fields
    if not isinstance(graph, dict):
        return fields

    scalars = [
        ("ckpt_name", "Model"), ("unet_name", "Model"), ("model_name", "Model"),
        ("steps", "Steps"), ("cfg", "CFG"),
        ("sampler_name", "Sampler"), ("scheduler", "Scheduler"),
        ("seed", "Seed"), ("noise_seed", "Seed"),
        ("width", "Width"), ("height", "Height"),
        ("length", "Length"), ("frame_rate", "FPS"), ("fps", "FPS"),
        ("vae_name", "VAE"),
    ]
    found = {}
    texts = []
    positive = negative = None
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        ins = node.get("inputs")
        if not isinstance(ins, dict):
            continue
        # sampler-style nodes link positive/negative conditioning -> resolve text
        if positive is None and isinstance(ins.get("positive"), list):
            positive = _resolve_text(graph, ins.get("positive"))
        if negative is None and isinstance(ins.get("negative"), list):
            negative = _resolve_text(graph, ins.get("negative"))
        for k, v in ins.items():
            if k == "text" and isinstance(v, str):
                s = v.strip()
                if s and s not in texts:
                    texts.append(s)
            for key, label in scalars:
                if k == key and not isinstance(v, (list, dict)) and label not in found:
                    found[label] = v

    if positive:
        fields.append(["Prompt", positive])
    if negative:
        fields.append(["Negative", negative])
    if not positive and not negative:  # fallback: raw text nodes, unlabeled
        for i, t in enumerate(texts[:2]):
            fields.append(["Prompt" if i == 0 else f"Prompt {i + 1}", t])
    for label in ("Model", "Steps", "CFG", "Sampler", "Scheduler",
                  "Seed", "Width", "Height", "Length", "FPS", "VAE"):
        if label in found:
            fields.append([label, found[label]])
    return fields


def _read_meta(path):
    info = {}
    try:
        from PIL import Image
        with Image.open(path) as im:
            text = getattr(im, "text", None)
            if text:
                info = dict(text)
            else:
                info = {k: v for k, v in (im.info or {}).items()
                        if isinstance(v, str)}
    except Exception:  # noqa: BLE001
        return {"fields": [], "raw": {}}

    fields = _summarize_prompt(info.get("prompt"))
    raw = {}
    for k, v in info.items():
        if not isinstance(v, str) or len(v) > 200000:
            continue
        try:  # parse JSON values so the client can pretty-print them
            raw[k] = json.loads(v)
        except Exception:  # noqa: BLE001
            raw[k] = v
    return {"fields": fields, "raw": raw}


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #
@PromptServer.instance.routes.get("/explore_gallery")
async def explore_gallery_page(request):
    index = os.path.join(WEB_DIR, "index.html")
    if os.path.isfile(index):
        return web.FileResponse(index)
    return web.Response(status=500, text="explore-gallery: web/index.html missing")


# static assets (style.css / app.js)
PromptServer.instance.routes.static("/explore_gallery/web", WEB_DIR)


@PromptServer.instance.routes.get("/explore_gallery/thumb")
async def explore_gallery_thumb(request):
    d = _resolve_dir(request.query.get("dir", ""))
    name = _safe_name(request.query.get("file", ""))
    if d is None or name is None:
        return web.Response(status=400, text="bad request")
    p = os.path.join(d, name)
    if not os.path.isfile(p):
        return web.Response(status=404, text="not found")
    try:
        st = os.stat(p)
    except OSError:
        return web.Response(status=404, text="not found")

    cache = os.path.join(_cache_dir(), _thumb_key(p, st) + ".webp")
    if not os.path.isfile(cache) and not _make_thumb(p, cache):
        # PIL unavailable / unreadable: fall back to ComfyUI's on-the-fly preview
        raise web.HTTPFound(
            "/view?filename=%s&subfolder=%s&type=output&preview=webp"
            % (quote(name), quote(request.query.get("dir", "")))
        )
    return web.FileResponse(cache, headers={"Cache-Control": "max-age=86400"})


@PromptServer.instance.routes.get("/explore_gallery/meta")
async def explore_gallery_meta(request):
    d = _resolve_dir(request.query.get("dir", ""))
    name = _safe_name(request.query.get("file", ""))
    if d is None or name is None:
        return web.json_response({"fields": [], "raw": {}}, status=400)
    p = os.path.join(d, name)
    if not os.path.isfile(p):
        return web.json_response({"fields": [], "raw": {}}, status=404)
    return web.json_response(_read_meta(p))


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

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = None

print("[explore-gallery] route ready: GET /explore_gallery")
