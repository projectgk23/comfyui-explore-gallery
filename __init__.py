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
import asyncio
import hashlib
import io
import json
import os
import shutil
import threading
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
CACHE_MAX_BYTES = 500 * 1024 * 1024   # サムネキャッシュ総量の上限 (約500MB)
CACHE_SWEEP_EVERY = 50      # この回数だけ新規サムネを作るごとに上限チェック

# Aesthetic scorer (anime-tuned). Weights auto-download on first use (~107MB) and
# are cached in models/. Scores (0..1, higher = nicer) are cached per-image.
SCORE_MODEL_NAME = "anime-aesthetic.onnx"
SCORE_MODEL_REPO = "skytnt/anime-aesthetic"
SCORE_MODEL_FILE = "model.onnx"
SCORE_MODEL_URL = ("https://huggingface.co/%s/resolve/main/%s"
                   % (SCORE_MODEL_REPO, SCORE_MODEL_FILE))
SCORE_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "models")
SCORE_SIZE = 768            # model input is [1,3,768,768]
SCORES_FILE = "scores.json"  # under .gallery_cache : {thumb_key: score}

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


def _count_images(d):
    try:
        return sum(1 for f in os.listdir(d)
                   if f.lower().endswith(IMG_EXT)
                   and os.path.isfile(os.path.join(d, f)))
    except OSError:
        return 0


def _list_dirs():
    """Return (visible, hidden) folder lists, each entry with an image count.

    visible : the output root (if non-empty) and non-empty top-level folders.
    hidden  : empty folders and "_"-prefixed bins (e.g. _trash) — accessible but
              kept out of the main tab bar. "."-prefixed folders (e.g. the
              .gallery_cache thumbnail cache) are never exposed.
    """
    root = _output_root()
    visible, hidden = [], []

    # ルート(output)は枚数0でも常にタブの最左に表示する
    visible.append({"dir": "", "label": "(ルート)", "count": _count_images(root)})

    try:
        names = sorted(os.listdir(root), key=str.lower)
    except OSError:
        names = []
    for name in names:
        if name.startswith("."):          # internal (.gallery_cache, dotfolders)
            continue
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        cnt = _count_images(p)
        entry = {"dir": name, "label": name, "count": cnt}
        if name.startswith("_") or cnt < 1:
            hidden.append(entry)          # _trash, or empty folders
        else:
            visible.append(entry)
    return visible, hidden


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


def _drop_thumb(path, st):
    """指定ファイルに対応するキャッシュ済みサムネ(WEBP)を削除する。
    移動/ゴミ箱/完全削除の直前に呼ぶことで孤児サムネの発生を抑える。
    `st` は移動前に取得した os.stat 結果（mtime/size を含む）を渡す。"""
    try:
        cache = os.path.join(_cache_dir(), _thumb_key(path, st) + ".webp")
        if os.path.isfile(cache):
            os.remove(cache)
    except OSError:
        pass


def _valid_thumb_keys():
    """output 配下の全画像から、現在有効なサムネキャッシュキーの集合を作る。
    キャッシュフォルダ自体は走査対象から除外する。"""
    keys = set()
    root = _output_root()
    cache_real = os.path.realpath(_cache_dir())
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.realpath(dirpath) == cache_real:
            dirnames[:] = []          # キャッシュ配下には降りない
            continue
        for fn in filenames:
            if not fn.lower().endswith(IMG_EXT):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not os.path.isfile(p):
                continue
            keys.add(_thumb_key(p, st))
    return keys


def _enforce_cache_limit(max_bytes):
    """キャッシュ総量が上限を超えていたら、古い順(mtime)に削除して収める。
    削除した枚数を返す。"""
    d = _cache_dir()
    try:
        entries, total = [], 0
        for fn in os.listdir(d):
            if not fn.endswith(".webp"):
                continue
            p = os.path.join(d, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
            total += st.st_size
    except OSError:
        return 0
    if total <= max_bytes:
        return 0
    entries.sort()                     # 古いもの(mtime小)から
    removed = 0
    for _, size, p in entries:
        if total <= max_bytes:
            break
        try:
            os.remove(p)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


_thumb_writes_since_sweep = 0


def _maybe_enforce_cache_limit():
    """新規サムネ生成のたびに呼ぶ軽量フック。毎回全走査すると重いので、
    CACHE_SWEEP_EVERY 回に一度だけ上限チェックを実行する。"""
    global _thumb_writes_since_sweep
    _thumb_writes_since_sweep += 1
    if _thumb_writes_since_sweep < CACHE_SWEEP_EVERY:
        return
    _thumb_writes_since_sweep = 0
    _enforce_cache_limit(CACHE_MAX_BYTES)


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


def _png_text(path):
    """Return the embedded text-chunk dict of an image (cheap for PNG: the
    chunks are read at open() without decoding pixels)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            text = getattr(im, "text", None)
            if text:
                return dict(text)
            return {k: v for k, v in (im.info or {}).items()
                    if isinstance(v, str)}
    except Exception:  # noqa: BLE001
        return {}


def _meta_find(path, q):
    """Return {"key", "snippet"} for the first embedded text chunk containing the
    lowercase query (covers prompt text, model names, seeds, etc.), else None.
    The snippet is a short window of context around the match."""
    for k, v in _png_text(path).items():
        if not isinstance(v, str):
            continue
        idx = v.lower().find(q)
        if idx == -1:
            continue
        start = max(0, idx - 30)
        end = min(len(v), idx + len(q) + 30)
        snip = " ".join(v[start:end].split())
        if start > 0:
            snip = "…" + snip
        if end < len(v):
            snip = snip + "…"
        return {"key": k, "snippet": snip}
    return None


def _read_workflow(path):
    """Return the embedded ComfyUI graph: the UI `workflow` (loadable via
    app.loadGraphData) and the API `prompt`, each parsed to an object or None."""
    text = _png_text(path)
    out = {"workflow": None, "prompt": None}
    for key in ("workflow", "prompt"):
        v = text.get(key)
        if isinstance(v, str):
            try:
                out[key] = json.loads(v)
            except Exception:  # noqa: BLE001
                out[key] = None
    return out


# --------------------------------------------------------------------------- #
#  Aesthetic scoring (anime-tuned ONNX; lazy-loaded, results cached on disk)
# --------------------------------------------------------------------------- #
_score_session = None
_score_lock = threading.Lock()     # guards model load + scores.json read/write
_score_cache = None                # {thumb_key: float}


def _scores_path():
    return os.path.join(_cache_dir(), SCORES_FILE)


def _load_scores():
    global _score_cache
    if _score_cache is None:
        try:
            with open(_scores_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            _score_cache = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 — missing / corrupt cache file
            _score_cache = {}
    return _score_cache


def _save_scores():
    if _score_cache is None:
        return
    try:
        tmp = _scores_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_score_cache, f)
        os.replace(tmp, _scores_path())
    except OSError:
        pass


def _download_model(dst):
    """Fetch the scorer weights. Tries huggingface_hub, then requests, then
    urllib (some Windows Python builds lack OpenSSL applink for urllib, so the
    earlier methods are preferred)."""
    os.makedirs(SCORE_DIR, exist_ok=True)
    errors = []

    def via_hfhub():
        from huggingface_hub import hf_hub_download
        p = hf_hub_download(SCORE_MODEL_REPO, SCORE_MODEL_FILE)
        shutil.copyfile(p, dst)

    def via_requests():
        import requests
        with requests.get(SCORE_MODEL_URL, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dst, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)

    def via_urllib():
        import urllib.request
        urllib.request.urlretrieve(SCORE_MODEL_URL, dst)

    for fn in (via_hfhub, via_requests, via_urllib):
        try:
            fn()
            if os.path.isfile(dst) and os.path.getsize(dst) > 1_000_000:
                return
        except Exception as e:  # noqa: BLE001 — try the next method
            errors.append("%s: %s" % (fn.__name__, e))
            try:
                if os.path.exists(dst):
                    os.remove(dst)
            except OSError:
                pass
    raise RuntimeError("could not download aesthetic model (" + "; ".join(errors) + ")")


def _get_session():
    global _score_session
    if _score_session is None:
        with _score_lock:
            if _score_session is None:
                import onnxruntime as ort
                path = os.path.join(SCORE_DIR, SCORE_MODEL_NAME)
                if not os.path.isfile(path):
                    _download_model(path)
                avail = ort.get_available_providers()
                prefer = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
                          if p in avail] or ["CPUExecutionProvider"]
                _score_session = ort.InferenceSession(path, providers=prefer)
    return _score_session


def _preprocess(path):
    """Resize keeping aspect into a centered 768x768 pad, normalize to [0,1],
    return an NCHW float32 batch (matches skytnt/anime-aesthetic)."""
    import numpy as np
    from PIL import Image
    s = SCORE_SIZE
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        if h > w:
            nh, nw = s, max(1, round(s * w / h))
        else:
            nh, nw = max(1, round(s * h / w)), s
        im = im.resize((nw, nh), Image.BILINEAR)
        arr = np.zeros((s, s, 3), dtype=np.float32)
        ph, pw = (s - nh) // 2, (s - nw) // 2
        arr[ph:ph + nh, pw:pw + nw] = np.asarray(im, dtype=np.float32) / 255.0
    return np.transpose(arr, (2, 0, 1))[None]


def _compute_batch(d, names):
    """Score the listed files in folder `d`, using/filling the disk cache.
    Returns (scores_by_name, computed_count, error_or_None). Runs in a worker
    thread so the ComfyUI server stays responsive."""
    cache = _load_scores()
    out, computed, err = {}, 0, None
    sess = None
    for raw in names:
        name = _safe_name(raw)
        if name is None:
            continue
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        key = _thumb_key(p, st)
        if key in cache:
            out[name] = cache[key]
            continue
        try:
            if sess is None:
                sess = _get_session()
            val = float(sess.run(None, {"img": _preprocess(p)})[0].reshape(-1)[0])
        except Exception as e:  # noqa: BLE001 — surface to the client, stop early
            err = str(e)
            break
        with _score_lock:
            cache[key] = val
        out[name] = val
        computed += 1
    if computed:
        with _score_lock:
            _save_scores()
    return out, computed, err


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
    if not os.path.isfile(cache):
        if _make_thumb(p, cache):
            _maybe_enforce_cache_limit()   # 新規生成時のみ上限チェック
        else:
            # PIL unavailable / unreadable: fall back to ComfyUI's on-the-fly
            # preview. Forward the client's cache-buster (?v=) so the fallback
            # can't serve a stale preview of a since-replaced same-named file.
            raise web.HTTPFound(
                "/view?filename=%s&subfolder=%s&type=output&preview=webp&v=%s"
                % (quote(name), quote(request.query.get("dir", "")),
                   quote(request.query.get("v", "")))
            )
    # The URL is content-addressed (?v=<mtime>-<size>), so the bytes for a given
    # URL never change -> cache aggressively and immutably.
    return web.FileResponse(
        cache, headers={"Cache-Control": "public, max-age=31536000, immutable"})


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
    visible, hidden = _list_dirs()
    return web.json_response({"dirs": visible, "hidden": hidden, "dst": DST_SUB})


@PromptServer.instance.routes.get("/explore_gallery/list")
async def explore_gallery_list(request):
    sub = request.query.get("dir", "")
    return web.json_response({"dir": sub, "dst": DST_SUB, "files": _list_images(sub)})


@PromptServer.instance.routes.get("/explore_gallery/search")
async def explore_gallery_search(request):
    """Filename substring search across visible top-level folders (root +
    non-prefixed). _trash, "_"-prefixed bins and "."-folders are excluded;
    to search those, open the folder and filter within it client-side."""
    q = (request.query.get("q") or "").strip().lower()
    if not q:
        return web.json_response({"q": "", "files": []})
    do_meta = request.query.get("meta") in ("1", "true", "yes", "on")

    root = _output_root()
    subdirs = [""]  # root
    try:
        for name in sorted(os.listdir(root), key=str.lower):
            if name.startswith((".", "_")):
                continue
            if os.path.isdir(os.path.join(root, name)):
                subdirs.append(name)
    except OSError:
        pass

    out = []
    for sub in subdirs:
        d = _resolve_dir(sub)
        if d is None or not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for fn in entries:
            low = fn.lower()
            if not low.endswith(IMG_EXT):
                continue
            p = os.path.join(d, fn)
            if not os.path.isfile(p):
                continue
            match = None
            hit = q in low
            if not hit and do_meta and low.endswith(".png"):
                match = _meta_find(p, q)
                hit = match is not None
            if not hit:
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            w, h = _dimensions(p, st)
            entry = {"name": fn, "dir": sub, "mtime": st.st_mtime,
                     "size": st.st_size, "w": w, "h": h}
            if match:
                entry["match"] = match
            out.append(entry)
    out.sort(key=lambda it: it["mtime"], reverse=True)
    return web.json_response({"q": q, "meta": do_meta, "files": out})


@PromptServer.instance.routes.post("/explore_gallery/move")
async def explore_gallery_move(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    src_dir = _resolve_dir(data.get("dir", ""))
    if src_dir is None:
        return web.json_response({"error": "bad dir"}, status=400)
    dst_sub = data.get("dst")
    if dst_sub is None:
        dst_sub = DST_SUB
    dst_dir = _resolve_dir(dst_sub)
    if dst_dir is None:
        return web.json_response({"error": "bad dst"}, status=400)
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
            skipped.append({"file": raw, "reason": "already there"})
            continue
        try:
            st = os.stat(src)
            shutil.move(src, _unique_dest(dst_dir, name))
            _drop_thumb(src, st)        # 移動元のサムネキャッシュを破棄
            moved.append(name)
        except Exception as e:  # noqa: BLE001
            skipped.append({"file": raw, "reason": str(e)})
    return web.json_response({"moved": moved, "skipped": skipped, "dst": dst_sub})


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
            st = os.stat(p)
            shutil.move(p, _unique_dest(trash_dir, name))
            _drop_thumb(p, st)          # 移動元のサムネキャッシュを破棄
            trashed.append(name)
        except Exception as e:  # noqa: BLE001
            skipped.append({"file": raw, "reason": str(e)})
    return web.json_response({"trashed": trashed, "skipped": skipped})


@PromptServer.instance.routes.post("/explore_gallery/delete")
async def explore_gallery_delete(request):
    """Permanently delete files (intended for emptying _trash). Irreversible."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    src_dir = _resolve_dir(data.get("dir", ""))
    if src_dir is None:
        return web.json_response({"error": "bad dir"}, status=400)

    deleted, skipped = [], []
    for raw in (data.get("files") or []):
        name = _safe_name(raw)
        if name is None:
            skipped.append({"file": raw, "reason": "invalid name"})
            continue
        p = os.path.join(src_dir, name)
        if not os.path.isfile(p):
            skipped.append({"file": raw, "reason": "not found"})
            continue
        try:
            st = os.stat(p)
            os.remove(p)
            _drop_thumb(p, st)          # 完全削除時はサムネキャッシュも破棄
            deleted.append(name)
        except Exception as e:  # noqa: BLE001
            skipped.append({"file": raw, "reason": str(e)})
    return web.json_response({"deleted": deleted, "skipped": skipped})


@PromptServer.instance.routes.get("/explore_gallery/stat")
async def explore_gallery_stat(request):
    """Lightweight folder fingerprint (image count + newest mtime) so the
    client can poll cheaply and refresh only when something changed."""
    d = _resolve_dir(request.query.get("dir", ""))
    if d is None or not os.path.isdir(d):
        return web.json_response({"count": 0, "latest": 0})
    count, latest = 0, 0.0
    try:
        for fn in os.listdir(d):
            if not fn.lower().endswith(IMG_EXT):
                continue
            p = os.path.join(d, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not os.path.isfile(p):
                continue
            count += 1
            if st.st_mtime > latest:
                latest = st.st_mtime
    except OSError:
        pass
    return web.json_response({"count": count, "latest": latest})


@PromptServer.instance.routes.get("/explore_gallery/workflow")
async def explore_gallery_workflow(request):
    d = _resolve_dir(request.query.get("dir", ""))
    name = _safe_name(request.query.get("file", ""))
    if d is None or name is None:
        return web.json_response({"workflow": None, "prompt": None}, status=400)
    p = os.path.join(d, name)
    if not os.path.isfile(p):
        return web.json_response({"workflow": None, "prompt": None}, status=404)
    return web.json_response(_read_workflow(p))


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

@PromptServer.instance.routes.post("/explore_gallery/upload")
async def explore_gallery_upload(request):
    """Receive image files (multipart) dragged from the OS and store them into
    the open folder. Intended for RunPod, where there is no desktop explorer."""
    sub = request.query.get("dir", "")
    d = _resolve_dir(sub)
    if d is None:
        return web.json_response({"error": "bad dir"}, status=400)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)

    try:
        reader = await request.multipart()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "expected multipart form"}, status=400)

    saved, skipped = [], []
    while True:
        part = await reader.next()
        if part is None:
            break
        if not part.filename:          # non-file field
            await part.release()
            continue
        name = _safe_name(part.filename)
        if name is None:
            skipped.append({"file": part.filename, "reason": "unsupported type"})
            await part.release()
            continue
        dest = _unique_dest(d, name)
        try:
            with open(dest, "wb") as fh:
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    fh.write(chunk)
            saved.append(os.path.basename(dest))
        except Exception as e:  # noqa: BLE001
            skipped.append({"file": part.filename, "reason": str(e)})
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
    return web.json_response({"saved": saved, "skipped": skipped, "dir": sub})


@PromptServer.instance.routes.post("/explore_gallery/cache/prune")
async def explore_gallery_cache_prune(request):
    """サムネキャッシュを整理する:
      1) output 配下のどの画像にも対応しない孤児 WEBP を削除
      2) 中断で残った *.tmp を削除
      3) その上でなお上限(CACHE_MAX_BYTES)を超えていれば古い順に削除
    削除枚数と解放バイト数を返す。"""
    valid = _valid_thumb_keys()
    d = _cache_dir()
    removed, freed = 0, 0
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for fn in names:
        p = os.path.join(d, fn)
        is_orphan_webp = fn.endswith(".webp") and fn[:-5] not in valid
        is_tmp = fn.endswith(".tmp")
        if not (is_orphan_webp or is_tmp):
            continue
        try:
            sz = os.path.getsize(p)
            os.remove(p)
            removed += 1
            freed += sz
        except OSError:
            pass
    capped = _enforce_cache_limit(CACHE_MAX_BYTES)
    return web.json_response({"removed": removed, "freed": freed, "capped": capped})


@PromptServer.instance.routes.get("/explore_gallery/scores")
async def explore_gallery_scores_get(request):
    """Return already-cached aesthetic scores for a folder (no compute), so the
    client can show badges / sort instantly on load."""
    d = _resolve_dir(request.query.get("dir", ""))
    if d is None or not os.path.isdir(d):
        return web.json_response({"scores": {}})
    cache = _load_scores()
    out = {}
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for fn in names:
        if not fn.lower().endswith(IMG_EXT):
            continue
        p = os.path.join(d, fn)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        key = _thumb_key(p, st)
        if key in cache:
            out[fn] = cache[key]
    return web.json_response({"scores": out})


@PromptServer.instance.routes.post("/explore_gallery/scores")
async def explore_gallery_scores_post(request):
    """Compute (and cache) aesthetic scores for the listed files. Heavy ONNX
    work is offloaded to a thread so the server event loop is not blocked."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    d = _resolve_dir(data.get("dir", ""))
    if d is None or not os.path.isdir(d):
        return web.json_response({"error": "bad dir"}, status=400)
    names = data.get("files") or []
    loop = asyncio.get_event_loop()
    out, computed, err = await loop.run_in_executor(None, _compute_batch, d, names)
    if err:
        return web.json_response({"error": err, "scores": out}, status=500)
    return web.json_response({"scores": out, "computed": computed})


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
# Loaded into ComfyUI's own page: enables "copy workflow in gallery → paste in
# ComfyUI" by reading a handoff key from the (same-origin) localStorage.
WEB_DIRECTORY = "./js"

print("[explore-gallery] route ready: GET /explore_gallery")
