/* Minimal Streamlit component (no build tooling) that captures Plotly `plotly_relayout`
 * events from the parent document and sends ranges to Python via `streamlit:setComponentValue`.
 */

function isStreamlitMessage(eventData) {
  return Boolean(eventData && eventData.isStreamlitMessage);
}

function sendToStreamlit(type, payload) {
  try {
    window.parent.postMessage(
      {
        isStreamlitMessage: true,
        type,
        ...payload,
      },
      "*",
    );
  } catch (e) {}
}

let latestArgs = null;
let debounceMs = 120;
let pending = null;
let lastSentAt = 0;

function nowMs() {
  return Date.now ? Date.now() : new Date().getTime();
}

function scheduleSend(value) {
  pending = value;
  const t = nowMs();
  const dueIn = Math.max(0, debounceMs - (t - lastSentAt));
  if (dueIn === 0) {
    lastSentAt = t;
    sendToStreamlit("streamlit:setComponentValue", { value: pending });
    pending = null;
    return;
  }
  setTimeout(() => {
    if (!pending) return;
    lastSentAt = nowMs();
    sendToStreamlit("streamlit:setComponentValue", { value: pending });
    pending = null;
  }, dueIn);
}

function getPlotsFromDoc(doc) {
  const out = [];
  try {
    // Prefer Streamlit plot containers for stable ordering.
    const blocks =
      doc?.querySelectorAll?.('div[data-testid="stPlotlyChart"], div.stPlotlyChart') || [];
    for (const b of blocks) {
      const el = b.querySelector?.("div.js-plotly-plot");
      if (el) out.push(el);
    }
  } catch (e) {}

  if (out.length) return out;

  // Do NOT fall back to "all plotly divs". Streamlit apps often embed other Plotly
  // instances (e.g. offscreen export plots) inside component iframes; binding to those
  // breaks the plot index mapping and makes zoom appear to "not save".
  return [];
}

function getAllPlots(parentWin) {
  const out = [];
  try {
    for (const el of getPlotsFromDoc(parentWin.document)) out.push(el);
  } catch (e) {}
  try {
    const iframes = parentWin.document?.querySelectorAll?.("iframe") || [];
    for (const fr of iframes) {
      try {
        const doc = fr.contentWindow?.document;
        for (const el of getPlotsFromDoc(doc)) out.push(el);
      } catch (e) {}
    }
  } catch (e) {}
  return out;
}

function bindOne(gd, idx, dataId) {
  if (!gd || !gd.on) return;
  try {
    if (gd.__fsRelayoutHandler && gd.removeListener) {
      gd.removeListener("plotly_relayout", gd.__fsRelayoutHandler);
    }
  } catch (e) {}

  const handler = (evt) => {
    try {
      if (!evt || typeof evt !== "object") return;
      const payload = {
        data_id: String(dataId),
        plot_index: idx,
      };

      if (evt["xaxis.autorange"] === true) payload.xautorange = true;
      if (evt["yaxis.autorange"] === true) payload.yautorange = true;

      if (evt["xaxis.range[0]"] != null && evt["xaxis.range[1]"] != null) {
        payload.x0 = evt["xaxis.range[0]"];
        payload.x1 = evt["xaxis.range[1]"];
      }
      if (evt["yaxis.range[0]"] != null && evt["yaxis.range[1]"] != null) {
        payload.y0 = evt["yaxis.range[0]"];
        payload.y1 = evt["yaxis.range[1]"];
      }

      // Ignore events that don't carry any axis info.
      if (
        payload.x0 === undefined &&
        payload.xautorange !== true &&
        payload.y0 === undefined &&
        payload.yautorange !== true
      ) {
        return;
      }

      scheduleSend(payload);
    } catch (e) {}
  };

  try {
    gd.__fsRelayoutHandler = handler;
  } catch (e) {}
  gd.on("plotly_relayout", handler);
}

function syncBindings() {
  if (!latestArgs) return;
  const parentWin = window.parent || window;
  const plotCount = Number(latestArgs.plot_count || 3);
  const dataId = String(latestArgs.data_id || "");

  const plots = getAllPlots(parentWin);
  const n = Math.min(plotCount, plots.length);
  for (let i = 0; i < n; i++) bindOne(plots[i], i, dataId);
}

function kickRebindLoop() {
  let tries = 0;
  (function tick() {
    syncBindings();
    tries += 1;
    if (tries < 30) setTimeout(tick, 100);
  })();
}

window.addEventListener("message", (event) => {
  if (!isStreamlitMessage(event.data)) return;
  if (event.data.type === "streamlit:render") {
    latestArgs = event.data.args || {};
    debounceMs = Number(latestArgs.debounce_ms || 120);
    try {
      sendToStreamlit("streamlit:setFrameHeight", { height: 0 });
    } catch (e) {}
    kickRebindLoop();
  }
});

sendToStreamlit("streamlit:componentReady", { apiVersion: 1 });
