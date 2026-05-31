// Explore Gallery — ComfyUI-side helper.
//
// The gallery page (/explore_gallery) and ComfyUI run on the same origin, so
// they share localStorage. When the user presses "ワークフローをコピー" in the
// gallery, the workflow JSON is stashed under EG_KEY. Here, on ComfyUI's page,
// the next Ctrl+V (paste) picks it up and loads it via app.loadGraphData.
import { app } from "../../scripts/app.js";

const EG_KEY = "explore_gallery_pending_workflow";
const MAX_AGE = 10 * 60 * 1000;   // ignore handoffs older than 10 min

function readPending() {
  try {
    const raw = localStorage.getItem(EG_KEY);
    if (!raw) return null;
    const obj = JSON.parse(raw);
    if (!obj || !obj.data || (Date.now() - (obj.t || 0)) > MAX_AGE) {
      localStorage.removeItem(EG_KEY);
      return null;
    }
    return obj;
  } catch (e) {
    localStorage.removeItem(EG_KEY);
    return null;
  }
}

async function loadPending(obj) {
  try {
    if (obj.kind === "workflow") {
      await app.loadGraphData(obj.data);
    } else if (typeof app.loadApiJson === "function") {
      await app.loadApiJson(obj.data, obj.name || "explore-gallery");
    } else {
      await app.loadGraphData(obj.data);   // best effort
    }
    return true;
  } catch (e) {
    console.error("[explore-gallery] workflow load failed", e);
    return false;
  }
}

function notify(ok, name) {
  try {
    app.extensionManager?.toast?.add?.({
      severity: ok ? "success" : "error",
      summary: "Explore Gallery",
      detail: ok
        ? `ワークフローを読み込みました${name ? "（" + name + "）" : ""}`
        : "ワークフローの読み込みに失敗しました",
      life: 4000,
    });
  } catch (e) { /* toast unavailable on older ComfyUI — silent */ }
}

app.registerExtension({
  name: "explore.gallery.paste",
  setup() {
    // capture phase so we run before ComfyUI's own paste handler and can
    // suppress it only when we actually have a pending gallery workflow.
    window.addEventListener("paste", (e) => {
      const obj = readPending();
      if (!obj) return;                  // nothing from the gallery: let ComfyUI handle it
      e.preventDefault();
      e.stopImmediatePropagation();
      localStorage.removeItem(EG_KEY);   // one-shot
      loadPending(obj).then((ok) => notify(ok, obj.name));
    }, true);
    console.log("[explore-gallery] paste-to-load ready");
  },
});
