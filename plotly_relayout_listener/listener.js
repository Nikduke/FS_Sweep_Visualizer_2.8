// Minimal Streamlit component (no build tooling), using Streamlit's postMessage protocol:
// - Binds to Plotly charts rendered by Streamlit (`stPlotlyChart` blocks)
// - Captures Plotly `plotly_relayout` events
// - Sends ranges to Python via `streamlit:setComponentValue`

function sendToStreamlit(type, payload) {
  try {
    window.parent.postMessage(
      {
        isStreamlitMessage: true,
        type,
        ...(payload || {}),
      },
      "*",
    );
  } catch (e) {}
}

let latestArgs = null;
let debounceMs = 120;
let pending = null;
let lastSentAt = 0;
let lastDataId = null;
let lastSigByPlot = new Map();

function nowMs() {
  return Date.now ? Date.now() : new Date().getTime();
}

function scheduleSend(value) {
  pending = value;
  const t = nowMs();
  const dueIn = Math.max(0, debounceMs - (t - lastSentAt));
  setTimeout(() => {
    if (!pending) return;
    lastSentAt = nowMs();
    sendToStreamlit("streamlit:setComponentValue", { value: pending });
    pending = null;
  }, dueIn);
}

function getStreamlitPlotDivsFromDoc(doc) {
  const out = [];
  try {
    const blocks = doc?.querySelectorAll?.('div[data-testid="stPlotlyChart"], div.stPlotlyChart') || [];
    for (const block of blocks) {
      // Prefer Plotly inside chart iframe if present.
      const fr = block.querySelector?.("iframe");
      if (fr && fr.contentWindow && fr.contentWindow.document) {
        const gd = fr.contentWindow.document.querySelector?.("div.js-plotly-plot");
        if (gd) {
          out.push(gd);
          continue;
        }
      }
      // Fallback: Plotly directly in the block.
      const gd2 = block.querySelector?.("div.js-plotly-plot");
      if (gd2) out.push(gd2);
    }
  } catch (e) {}
  return out;
}

function getPlotDivs() {
  // Only bind to Streamlit plotly charts. Do NOT bind to other Plotly instances
  // (e.g., offscreen exporter plots created in component iframes).
  const parentDoc = (window.parent || window).document;
  return getStreamlitPlotDivsFromDoc(parentDoc);
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
      const payload = { data_id: String(dataId), plot_index: idx };

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

      // Ignore events that don't carry axis info.
      if (
        payload.x0 === undefined &&
        payload.xautorange !== true &&
        payload.y0 === undefined &&
        payload.yautorange !== true
      ) {
        return;
      }

      // Avoid sending duplicate payloads for the same plot index. Streamlit remounts Plotly
      // charts on reruns (e.g. case toggles), and Plotly may emit an initial relayout event
      // with the current axis ranges. If we resend the same ranges, it causes an extra rerun
      // and visible flicker.
      const roundNum = (v) => {
        const n = Number(v);
        if (!Number.isFinite(n)) return v;
        return Math.round(n * 1e6) / 1e6;
      };
      const sigObj = {
        plot_index: idx,
        xautorange: payload.xautorange === true,
        yautorange: payload.yautorange === true,
        x0: payload.x0 !== undefined ? roundNum(payload.x0) : undefined,
        x1: payload.x1 !== undefined ? roundNum(payload.x1) : undefined,
        y0: payload.y0 !== undefined ? roundNum(payload.y0) : undefined,
        y1: payload.y1 !== undefined ? roundNum(payload.y1) : undefined,
      };
      const sig = JSON.stringify(sigObj);
      if (lastSigByPlot.get(idx) === sig) return;
      lastSigByPlot.set(idx, sig);

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
  const plotCount = Number(latestArgs.plot_count || 3);
  const dataId = String(latestArgs.data_id || "");
  const plots = getPlotDivs();
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
  // Incoming Streamlit messages may or may not include an `isStreamlitMessage` flag.
  // Match by `type` to avoid dropping the render message (which would prevent any binding).
  const data = event && event.data ? event.data : null;
  if (!data || data.type !== "streamlit:render") return;
  latestArgs = data.args || {};
  debounceMs = Number(latestArgs.debounce_ms || 120);
  try {
    const newDataId = String(latestArgs.data_id || "");
    if (lastDataId !== newDataId) {
      lastDataId = newDataId;
      lastSigByPlot = new Map();
    }
  } catch (e) {}
  try {
    sendToStreamlit("streamlit:setFrameHeight", { height: 0 });
  } catch (e) {}
  kickRebindLoop();
});

sendToStreamlit("streamlit:componentReady", { apiVersion: 1 });
