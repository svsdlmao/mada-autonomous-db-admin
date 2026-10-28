// MADA live dashboard: polls the demo server and renders state, charts and the agent log.
const $ = (id) => document.getElementById(id);
const AGENTS = [["monitor", "Monitor"], ["index-agent", "Index"], ["knob-agent", "Knob"], ["query-agent", "Query"],
  ["diagnosis", "Diagnosis"], ["security-agent", "Security"], ["coordinator", "Coordinator"], ["operator", "Operator"]];
let logSeq = 0, lastState = null, pendingShown = null;

function post(url, body) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
    .then((r) => r.json().then((j) => { if (!r.ok && j.msg) toast(j.msg); return j; }));
}
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 3500); }
const ms = (v) => v == null ? "–" : v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 1 : 2) + " s" : v >= 10 ? Math.round(v) + " ms" : v.toFixed(1) + " ms";
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// ---------- agents strip ----------
$("agents").innerHTML = AGENTS.map(([k, l]) => `<div class="agent" id="ag-${k}"><span class="dot"></span>${l}</div>`).join("");

// ---------- charts ----------
const grid = { color: "#eef0f4" }, tick = { color: "#566074", font: { size: 11 } };
const xAxis = { type: "linear", min: -300, max: 0, grid, ticks: { ...tick, stepSize: 60, callback: (v) => v === 0 ? "now" : `${Math.round(-v / 60)} min ago` } };
const latChart = new Chart($("latChart"), {
  type: "line",
  data: { datasets: [
    { label: "Web pages (OLTP) p95", data: [], borderColor: "#12877a", backgroundColor: "#12877a", borderWidth: 2, pointRadius: 0, tension: .25 },
    { label: "Reports (OLAP) p95", data: [], borderColor: "#d2691e", backgroundColor: "#d2691e", borderWidth: 2, pointRadius: 0, tension: .25, spanGaps: true },
  ] },
  options: { animation: false, maintainAspectRatio: false, parsing: false, plugins: { legend: { labels: { boxWidth: 12, font: { size: 12 } } } },
    scales: { x: xAxis, y: { type: "logarithmic", grid, ticks: { ...tick, callback: (v) => [1, 10, 100, 1000, 10000, 100000].includes(v) ? ms(v) : "" }, title: { display: true, text: "ms (log scale)", color: "#566074" } } } },
});
const tpsChart = new Chart($("tpsChart"), {
  type: "line",
  data: { datasets: [
    { label: "Statements / s", data: [], borderColor: "#2a3045", backgroundColor: "#2a3045", borderWidth: 2, pointRadius: 0, tension: .25, yAxisID: "y" },
    { label: "Connections", data: [], borderColor: "#8a94a6", backgroundColor: "#8a94a6", borderWidth: 1.5, pointRadius: 0, borderDash: [4, 3], yAxisID: "y2" },
    { label: "Waiting on locks", data: [], borderColor: "#b83a3a", backgroundColor: "#b83a3a", borderWidth: 1.5, pointRadius: 0, yAxisID: "y2" },
  ] },
  options: { animation: false, maintainAspectRatio: false, parsing: false, plugins: { legend: { labels: { boxWidth: 12, font: { size: 12 } } } },
    scales: { x: xAxis, y: { beginAtZero: true, grid, ticks: tick }, y2: { beginAtZero: true, position: "right", grid: { display: false }, ticks: tick } } },
});

// ---------- fault buttons ----------
function renderFaultButtons(faults) {
  if ($("faults").childElementCount) return;
  $("faults").innerHTML = Object.entries(faults).map(([k, v]) => `<button data-f="${k}"><b>${k}</b>${esc(v)}</button>`).join("");
  $("faults").querySelectorAll("button").forEach((b) => b.onclick = () => post(`/api/fault/${b.dataset.f}`).then((j) => j.ok && toast(`${b.dataset.f} injected — watch the agents respond`)));
}

// ---------- render ----------
function render(s) {
  lastState = s;
  $("phase").textContent = s.phase; $("phase").className = `pill ph-${s.phase}`;
  $("detail").textContent = s.detail || "";
  $("gatePill").textContent = s.gate ? "Safety gate ON" : "Safety gate OFF"; $("gatePill").className = `pill gate ${s.gate ? "on" : "off"}`;
  $("gateToggle").checked = s.gate; $("autoToggle").checked = s.auto_approve;
  renderFaultButtons(s.faults);

  // KPIs
  const last = s.series.length ? s.series[s.series.length - 1] : null;
  $("kTps").textContent = s.tps_now ? s.tps_now.toFixed(0) : "–";
  $("kTpsSub").textContent = s.before_tps ? `statements / s · was ${s.before_tps.toFixed(0)} before tuning` : "statements / s";
  $("kOltp").textContent = last ? ms(last.oltp) : "–";
  $("kOlap").textContent = last ? ms(last.olap) : "–";
  const applied = new Set(s.decisions.filter((d) => d.status === "applied").map((d) => d.id)).size;
  const rejected = new Set(s.decisions.filter((d) => d.decision === "REJECT").map((d) => d.id)).size;
  $("kApplied").textContent = applied; $("kAppliedSub").textContent = `${rejected} rejected by the gate`;
  const handled = s.incidents.filter((i) => i.detected_s != null).length;
  $("kInc").textContent = handled; $("kIncSub").textContent = s.fault ? `${s.fault.key} active` : (s.incidents.length ? `${s.incidents.length} injected` : "inject a fault to test");

  // agents
  AGENTS.forEach(([k]) => $(`ag-${k}`).classList.toggle("on", !!(s.agents && s.agents[k])));

  // buttons
  $("btnTune").disabled = s.busy || !["ready", "watching"].includes(s.phase);
  $("btnReset").disabled = s.busy || !["ready", "watching", "error"].includes(s.phase);
  $("faults").querySelectorAll("button").forEach((b) => b.disabled = !s.watching || !!s.fault);
  $("btnEndFault").disabled = !s.fault;
  $("btnHarmful").disabled = !s.watching || s.busy || !!s.fault;
  $("btnUndo").disabled = !s.harmful_applied;

  // charts
  const t0 = s.server_time;
  // a 10 s window with no report finishing has no p95: leave a gap rather than drawing a drop to zero
  latChart.data.datasets[0].data = s.series.map((r) => ({ x: r.t - t0, y: r.oltp > 0 ? r.oltp : null }));
  latChart.data.datasets[1].data = s.series.map((r) => ({ x: r.t - t0, y: r.olap > 0 ? r.olap : null }));
  latChart.update();
  tpsChart.data.datasets[0].data = s.series.map((r) => ({ x: r.t - t0, y: r.tps }));
  tpsChart.data.datasets[1].data = s.series.map((r) => ({ x: r.t - t0, y: r.conn }));
  tpsChart.data.datasets[2].data = s.series.map((r) => ({ x: r.t - t0, y: r.locks }));
  tpsChart.update();

  // templates
  $("tplTable").querySelector("tbody").innerHTML = s.templates.map((t) => {
    let ch = "";
    if (t.before && t.now) { const p = 100 * (t.now / t.before - 1); ch = `<span class="${p < -10 ? "good" : p > 10 ? "bad" : ""}">${p > 0 ? "+" : ""}${p.toFixed(0)}%</span>`; }
    return `<tr><td>${esc(t.label)}</td><td class="r">${ms(t.before)}</td><td class="r">${ms(t.now)}</td><td class="r">${ch}</td></tr>`;
  }).join("");

  // decisions
  $("decTable").querySelector("tbody").innerHTML = s.decisions.slice().reverse().map((d) => {
    const cls = d.decision === "PASS" ? (d.status === "rolled back" ? "bad" : "good") : d.decision === "DEFER" ? "defer" : "bad";
    const label = d.decision === "PASS" ? (d.status === "rolled back" ? "ROLLED BACK" : d.status === "failed" ? "FAILED" : "APPLIED") : d.decision;
    return `<tr><td>${d.id}</td><td>${esc(d.title)}<div class="reason">${esc(d.reason)}</div></td><td>${esc(d.agent)}</td><td class="${cls}">${label}</td></tr>`;
  }).join("") || `<tr><td colspan="4" class="reason">No proposals yet — run the tuning cycle.</td></tr>`;

  // incidents
  $("incTable").querySelector("tbody").innerHTML = s.incidents.slice().reverse().map((i) => {
    const fix = i.fix ? `${esc(i.fix)}<div class="reason">${i.decision === "PASS" ? "applied" : esc(i.decision || "")}</div>` : (i.detected_s == null ? '<span class="reason">waiting…</span>' : "");
    return `<tr><td><b>${i.key}</b> ${esc(i.name)}${i.ended ? "" : ' <span class="defer">active</span>'}</td><td class="r">${i.detected_s == null ? "–" : i.detected_s + " s"}</td><td>${esc(i.cause || "–")}</td><td>${fix}</td><td class="r">${i.fixed_s == null ? "–" : i.fixed_s + " s"}</td></tr>`;
  }).join("") || `<tr><td colspan="5" class="reason">No faults injected yet.</td></tr>`;

  // db state
  const db = s.db || {};
  $("dbState").innerHTML = `
    <div><b>MADA indexes:</b> ${(db.indexes || []).map((x) => `<code>${esc(x.replace(/^CREATE INDEX /, ""))}</code>`).join(" ") || "none"}</div>
    <div><b>Query rewrites:</b> ${(db.rewrites || []).map((x) => `<code>${esc(x)}</code>`).join(" ") || "none"} &nbsp; <b>Hints:</b> ${(db.hints || []).map((x) => `<code>${esc(x)}</code>`).join(" ") || "none"}</div>
    <div><b>Settings:</b> ${Object.entries(db.settings || {}).map(([k, v]) => `<code>${k}=${esc(v)}</code>`).join(" ")}</div>`;

  // approval
  if (s.pending && pendingShown !== s.pending.id) {
    pendingShown = s.pending.id;
    $("mTitle").textContent = `#${s.pending.id} ${s.pending.title}`;
    $("mMeta").textContent = `Proposed by the ${s.pending.agent} agent · category ${s.pending.category} · risk ${s.pending.risk} · not reversible`;
    $("mAction").textContent = s.pending.action + "\n\nevidence: " + JSON.stringify(s.pending.evidence, null, 1);
    $("modal").classList.remove("hidden");
  } else if (!s.pending) { $("modal").classList.add("hidden"); pendingShown = null; }
}

function colourLine(t) {
  const l = t.toLowerCase();
  const cls = l.includes("fault injected") ? "l-fault" : l.includes("operator") ? "l-op" : l.includes("security-agent") ? "l-sec" : l.includes("diagnosis") ? "l-diag"
    : l.includes("coordinator") ? "l-coord" : l.includes("index-agent") ? "l-idx" : l.includes("knob-agent") ? "l-knob" : l.includes("query-agent") ? "l-qry" : l.includes("monitor") ? "l-mon" : "";
  return cls ? `<span class="${cls}">${esc(t)}</span>` : esc(t);
}

function pollLog() {
  fetch(`/api/log?since=${logSeq}`).then((r) => r.json()).then((lines) => {
    if (!lines.length) return;
    const el = $("log"), atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.insertAdjacentHTML("beforeend", lines.map((x) => colourLine(x.text)).join("\n") + "\n");
    logSeq = lines[lines.length - 1].seq;
    if (el.childNodes.length > 4000) el.innerHTML = el.innerHTML.split("\n").slice(-1500).join("\n");
    if (atBottom) el.scrollTop = el.scrollHeight;
  }).catch(() => {});
}
function pollState() { fetch("/api/state").then((r) => r.json()).then(render).catch(() => { $("phase").textContent = "offline"; $("detail").textContent = "server not reachable"; }); }

// ---------- controls ----------
$("btnTune").onclick = () => post("/api/tune").then((j) => j.ok && toast("Tuning cycle started"));
$("btnReset").onclick = () => { if (confirm("Drop MADA's indexes and settings and measure the untuned database again?")) post("/api/reset"); };
$("btnEndFault").onclick = () => post("/api/fault/end");
$("gateToggle").onchange = (e) => post("/api/gate", { enabled: e.target.checked });
$("autoToggle").onchange = (e) => post("/api/auto_approve", { enabled: e.target.checked });
$("btnHarmful").onclick = () => post("/api/harmful").then(() => toast(lastState && lastState.gate ? "Sending harmful proposals through the gate…" : "Gate is OFF — harmful proposals will reach production"));
$("btnUndo").onclick = () => post("/api/harmful/undo").then(() => toast("Harmful changes undone"));
$("mApprove").onclick = () => { post("/api/approve", { ok: true }); $("modal").classList.add("hidden"); };
$("mReject").onclick = () => { post("/api/approve", { ok: false }); $("modal").classList.add("hidden"); };

pollState(); pollLog();
setInterval(pollState, 2000);
setInterval(pollLog, 1000);
