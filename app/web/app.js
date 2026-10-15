"use strict";
/* SignalShift dashboard: polls the read APIs once, then stays live over Server-Sent Events. */

const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const num = new Intl.NumberFormat("en-IN");
const $ = (s) => document.querySelector(s);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const STATUS_LABEL = { out: "Out", low: "Low", short: "Before restock", ok: "OK" };
const SEV_LABEL = { critical: "Critical", warning: "Warning", opportunity: "Opportunity" };
const SOURCE_LABEL = { gemini: "Gemini", gemini_fallback_model: "Gemini (fallback model)", template: "Template" };

const state = { invGroup: "hero", inboxStatus: "open", inventory: [], timers: {} };

function debounce(key, ms, fn) {
  clearTimeout(state.timers[key]);
  state.timers[key] = setTimeout(fn, ms);
}
const getJSON = (url) => fetch(url).then((r) => { if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); });

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = "toast"), 3200);
}
function setTile(id, tileId, text) {
  const el = $(id);
  if (el.textContent === text) return;
  const first = el.textContent === "–";  // no flash on the initial load, only on live changes
  el.textContent = text;
  if (tileId && !first) { const t = $(tileId); t.classList.remove("flash"); void t.offsetWidth; t.classList.add("flash"); }
}
function cover(h) {
  if (h === null || h === undefined) return "no demand";
  if (h < 1) return `${Math.max(1, Math.floor(h * 60))} min`;
  if (h < 10) return `${(Math.floor(h * 10) / 10).toFixed(1)} h`;
  if (h < 48) return `${Math.floor(h)} h`;
  return `${Math.floor(h / 24)} d`;
}
function ago(ts) {
  const d = new Date(ts.replace(" ", "T"));
  return d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* ---------------- sales ---------------- */
async function loadSales() {
  const d = await getJSON("/api/dashboard/sales");
  setTile("#k-orders", "#t-orders", num.format(d.today.orders));
  $("#k-units").textContent = `${num.format(d.today.units)} units`;
  setTile("#k-revenue", "#t-revenue", inr.format(d.today.revenue));
  $("#asof").textContent = "as of " + ago(d.as_of);
  drawHourly(d);
  $("#top-products").innerHTML = d.top_products.length
    ? d.top_products.map((p) => `<tr><td>${esc(p.name)}</td><td class="r">${num.format(p.units)}</td><td class="r">${inr.format(p.revenue)}</td></tr>`).join("")
    : `<tr><td colspan="3" class="faint">No orders yet today</td></tr>`;
}

function drawHourly(d) {
  if (!window.Plotly) { $("#chart-hourly").innerHTML = '<p class="faint">Chart library failed to load.</p>'; return; }
  const hours = d.by_hour_today.map((p) => p.hour);
  const nowHour = new Date(d.as_of).getHours();
  const today = d.by_hour_today.map((p) => (p.hour <= nowHour ? p.orders : null));
  const ink = css("--text"), muted = css("--faint"), grid = css("--grid"), blue = css("--series-1");
  const traces = [
    { x: hours, y: today, type: "bar", name: "Today", marker: { color: blue },
      hovertemplate: "%{x}:00 · %{y} orders<extra>Today</extra>" },
    { x: hours, y: d.by_hour_yesterday.map((p) => p.orders), type: "scatter", mode: "lines", name: "Yesterday",
      line: { color: muted, width: 2, dash: "dot" }, hovertemplate: "%{x}:00 · %{y} orders<extra>Yesterday</extra>" },
  ];
  const layout = {
    margin: { l: 36, r: 8, t: 8, b: 30 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "system-ui, -apple-system, Segoe UI, sans-serif", color: ink, size: 12 },
    xaxis: { tickmode: "array", tickvals: [0, 3, 6, 9, 12, 15, 18, 21], ticktext: ["0", "3", "6", "9", "12", "15", "18", "21"],
             gridcolor: "rgba(0,0,0,0)", linecolor: grid, tickfont: { color: muted }, fixedrange: true },
    yaxis: { gridcolor: grid, zeroline: false, tickfont: { color: muted }, fixedrange: true, rangemode: "tozero" },
    legend: { orientation: "h", x: 0, y: 1.12, font: { color: muted } }, bargap: 0.25, hovermode: "x",
  };
  Plotly.react("chart-hourly", traces, layout, { displayModeBar: false, responsive: true });
}

/* ---------------- inventory ---------------- */
async function loadInventory() {
  state.inventory = await getJSON("/api/dashboard/inventory");
  renderHeatmap();
  renderRiskTable();
}

function renderHeatmap() {
  const rows = state.inventory.filter((p) => p.sku_group === state.invGroup);
  const sizes = [];
  rows.forEach((p) => p.sizes.forEach((c) => { if (!sizes.includes(c.size)) sizes.push(c.size); }));
  const heat = $("#heatmap");
  heat.style.gridTemplateColumns = `minmax(250px, 1.8fr) repeat(${sizes.length}, minmax(64px, 1fr))`;
  let html = `<div class="colh"></div>` + sizes.map((s) => `<div class="colh">${esc(s)}</div>`).join("");
  for (const p of rows) {
    const cv = p.cover_days === null ? "no demand" : `${p.cover_days} d total cover`;
    const colour = p.name.includes(" — ") ? p.name.split(" — ").slice(1).join(" — ") : "";
    html += `<div class="pname" title="${esc(p.name)}"><b>${esc(p.model)}${p.campaign_live ? '<span class="mark-live">ads live</span>' : ""}</b>
      <span class="faint">${colour ? esc(colour) + " · " : ""}${num.format(p.total_available)} ${esc(p.unit_label)}s · ${cv}</span></div>`;
    for (const s of sizes) {
      const c = p.sizes.find((x) => x.size === s);
      if (!c) { html += `<div class="cell na"></div>`; continue; }
      const tip = `${p.name} ${c.size}\n${STATUS_LABEL[c.status]} · ${c.available} available, ${c.in_transit} in transit\n` +
        `Demand ${c.demand_per_day}/day · cover ${cover(c.hours_of_cover)}` + (c.click_share !== null ? `\n${Math.round(c.click_share * 100)}% of ad clicks` : "");
      html += `<div class="cell ${c.status}" data-vid="${c.variant_id}" title="${esc(tip)}" aria-label="${esc(tip)}">
        <span class="a">${num.format(c.available)}</span><span class="c">${cover(c.hours_of_cover)}</span></div>`;
    }
  }
  heat.innerHTML = rows.length ? html : '<p class="faint">No products in this group.</p>';
}

function renderRiskTable() {
  const order = { out: 0, low: 1, short: 2, ok: 3 };
  const cells = [];
  state.inventory.forEach((p) => p.sizes.forEach((c) => { if (c.status !== "ok") cells.push({ p, c }); }));
  cells.sort((a, b) => order[a.c.status] - order[b.c.status] || (a.c.hours_of_cover ?? 1e9) - (b.c.hours_of_cover ?? 1e9));
  $("#risk-table").innerHTML = cells.length ? cells.slice(0, 12).map(({ p, c }) => `<tr>
      <td>${esc(p.name)}${p.campaign_live ? '<span class="mark-live">ads live</span>' : ""}</td><td>${esc(c.size)}</td>
      <td class="r">${num.format(c.on_hand)}</td><td class="r">${num.format(c.in_transit)}</td>
      <td class="r">${c.demand_per_day}</td><td class="r">${cover(c.hours_of_cover)}</td>
      <td><span class="st st-${c.status}">${STATUS_LABEL[c.status]}</span></td></tr>`).join("")
    : `<tr><td colspan="7" class="faint">No sizes at risk.</td></tr>`;
}

function patchInventory(d) {
  for (const p of state.inventory) {
    const c = p.sizes.find((x) => x.variant_id === d.variant_id);
    if (!c) continue;
    p.total_available += d.available - c.available;
    c.available = d.available;
    c.on_hand = d.on_hand;
    const el = document.querySelector(`.cell[data-vid="${d.variant_id}"] .a`);
    if (el) { el.textContent = num.format(d.available); el.parentElement.classList.remove("flash"); void el.offsetWidth; el.parentElement.classList.add("flash"); }
  }
}

/* ---------------- campaigns ---------------- */
async function loadCampaigns() {
  const d = await getJSON("/api/dashboard/campaigns");
  setTile("#k-spend", "#t-spend", inr.format(d.spend_today));
  $("#k-clicks").textContent = `${num.format(d.clicks_today)} clicks`;
  setTile("#k-wasted", "#t-wasted", inr.format(d.wasted_spend_today));
  $("#k-wasted30").textContent = `${inr.format(d.wasted_spend_30d)} in the last 30 days`;
  $("#campaign-rows").innerHTML = d.campaigns.map((c) => {
    const mix = c.sizes.map((s) => {
      const pct = Math.round(s.click_share * 100);
      const color = { out: "var(--critical)", low: "var(--serious)", short: "var(--warning)", ok: "var(--good)" }[s.status];
      // label only segments wide enough to fit "UK 9 35%"; the tooltip always carries the full value
      const label = s.click_share >= 0.2 ? `${esc(s.size)} ${pct}%` : s.click_share >= 0.12 ? `${pct}%` : "";
      return `<span style="flex:${Math.max(s.click_share, 0.02)};--c:${color}" title="${esc(s.size)}: ${pct}% of clicks · ${STATUS_LABEL[s.status]}">${label}</span>`;
    }).join("");
    const risk = c.at_risk_click_share > 0 ? `<div class="faint">${Math.round(c.at_risk_click_share * 100)}% of clicks on low/out sizes</div>` : "";
    return `<tr><td><b>${esc(c.name)}</b><div class="faint">${c.platform === "google_ads" ? "Google Ads" : "Meta Ads"} · ${esc(c.product_name)}</div></td>
      <td><span class="chip ${c.status === "active" ? "active" : ""}">${esc(c.status)}</span></td>
      <td class="r">${inr.format(c.daily_budget)}</td><td class="r">${inr.format(c.spend_today)}</td>
      <td class="r">${num.format(c.clicks_today)}</td><td class="r">${inr.format(c.wasted_spend_today)}</td>
      <td><div class="mix">${mix}</div>${risk}</td></tr>`;
  }).join("");
}

/* ---------------- savings ---------------- */
async function loadSavings() {
  const d = await getJSON("/api/dashboard/savings");
  setTile("#k-saved", "#t-saved", inr.format(d.ad_spend_avoided));
  $("#k-protected").textContent = `${inr.format(d.sales_protected)} sales protected`;
  $("#s-avoided").textContent = inr.format(d.ad_spend_avoided);
  $("#s-protected").textContent = inr.format(d.sales_protected);
  $("#s-prevented").textContent = num.format(d.stockouts_prevented);
  $("#s-actions").textContent = num.format(d.actions_executed);
  $("#ledger").innerHTML = d.entries.length
    ? d.entries.map((e) => `<li>${ago(e.ts)} · alert #${e.alert_id}: ${inr.format(e.ad_spend_avoided)} avoided, ${inr.format(e.sales_protected)} protected${e.explanation ? " — " + esc(e.explanation) : ""}</li>`).join("")
    : "<li>No approved actions yet. Approve an alert in the inbox to start the ledger.</li>";
}

/* ---------------- inbox ---------------- */
async function loadInbox() {
  const q = state.inboxStatus ? `?status=${state.inboxStatus}&limit=40` : "?status=&limit=40";
  const [alerts, open] = await Promise.all([getJSON("/alerts" + q), state.inboxStatus === "open" ? null : getJSON("/alerts?status=open&limit=200")]);
  const openList = open || alerts;
  const crit = openList.filter((a) => a.severity === "critical").length;
  setTile("#k-alerts", "#t-alerts", num.format(openList.length));
  $("#k-alerts-sub").textContent = `${crit} critical · ${openList.filter((a) => a.severity === "warning").length} warning`;
  $("#t-alerts").classList.toggle("alarm", crit > 0);
  $("#inbox").innerHTML = alerts.length ? alerts.map(alertCard).join("") : '<p class="faint">Nothing needs your attention right now.</p>';
}

function alertCard(a) {
  const f = a.facts, r = a.report;
  const band = f.hours_of_cover !== null && f.severity !== "opportunity"
    ? `<div class="band">Cover ${cover(f.hours_of_cover)} · likely range ${cover(f.hours_of_cover_low)}–${cover(f.hours_of_cover_high)}${f.anomalies.length ? " · " + f.anomalies.map((x) => x.replace(/_/g, " ")).join(", ") : ""}</div>` : "";
  const body = r ? `<p>${esc(r.summary)}</p>
      <details class="more" ${a.severity === "critical" && a.status === "open" ? "open" : ""}><summary>Why and what to do</summary>
        <ul>${r.why.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
        <ol>${r.recommendations.map((x) => `<li>${esc(x.text)}</li>`).join("")}</ol>
        <p class="impact">${esc(r.estimated_impact)}</p></details>`
    : `<p class="faint">Report is being written…</p>`;
  const actions = a.status === "open"
    ? `<div class="act"><button class="btn primary" data-act="approve" data-id="${a.id}">Approve</button><button class="btn" data-act="dismiss" data-id="${a.id}">Dismiss</button></div>`
    : `<div class="meta"><span class="chip">${esc(a.status)}${a.decided_by ? " via " + esc(a.decided_by) : ""}</span>${a.decided_at ? `<span>${ago(a.decided_at)}</span>` : ""}</div>`;
  return `<article class="alert sev-${a.severity} ${a.status !== "open" ? "closed" : ""}" data-id="${a.id}">
    <div class="meta"><span class="sev">${SEV_LABEL[a.severity]}</span><span>${ago(a.ts)}</span>
      <span class="chip" title="Who wrote this text (numbers always come from the engine)">${SOURCE_LABEL[a.report_source] || "…"}</span></div>
    <h3>${esc(r ? r.headline : a.headline)}</h3>${band}${body}${actions}</article>`;
}

async function decide(id, act, btn) {
  btn.disabled = true;
  try {
    const res = await fetch(`/alerts/${id}/${act}`, { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { toast(body.detail || body.message || `Failed (${res.status})`, true); return; }
    if (act === "approve") {
      const n = body.actions.length, saved = body.ledger ? body.ledger.ad_spend_avoided : 0;
      toast(`Approved: ${n} action${n === 1 ? "" : "s"} executed via mock APIs · ${inr.format(saved)} ad spend avoided`);
    } else toast("Dismissed: I'll stay quiet on this for a while (cooldown)");
    refreshAll();
  } finally { btn.disabled = false; }
}

/* ---------------- ask ---------------- */
const SRC = { gemini: "Gemini", gemini_fallback_model: "Gemini (fallback)", template: "Template", keywords: "keyword router" };

function evidenceHtml(ev) {
  if (ev.type === "sql_template") {
    const rows = ev.rows || [];
    const cols = rows.length ? Object.keys(rows[0]) : [];
    const table = rows.length ? `<div class="scroll-x"><table><thead><tr>${cols.map((c) => `<th>${esc(c.replace(/_/g, " "))}</th>`).join("")}</tr></thead>
      <tbody>${rows.slice(0, 8).map((r) => `<tr>${cols.map((c) => `<td>${esc(typeof r[c] === "number" ? num.format(r[c]) : r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : "";
    return `<div>Template <code>${esc(ev.template)}</code> · params <code>${esc(JSON.stringify(ev.params))}</code></div>${table}`;
  }
  if (ev.type === "alert_retrieval") {
    return `<div>Retrieved alerts (full-text search${ev.product ? " · product " + esc(ev.product) : ""}):</div><ul>` +
      ev.alerts.map((a) => `<li>#${a.alert_id} · ${esc(a.severity)} · ${esc(a.status)} — ${esc(a.headline)}</li>`).join("") + "</ul>";
  }
  return "<div>No data needed.</div>";
}

async function ask(question) {
  const box = $("#ask-answers");
  const card = document.createElement("div");
  card.className = "answer";
  card.innerHTML = `<div class="q">${esc(question)}</div><div class="a faint">Thinking…</div>`;
  box.prepend(card);
  try {
    const res = await fetch("/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) });
    const r = await res.json();
    if (!res.ok) throw new Error(r.detail?.[0]?.msg || r.detail || res.status);
    card.innerHTML = `<div class="q">${esc(question)}</div><div class="a">${esc(r.answer)}</div>
      <details class="more"><summary>Evidence · ${esc(r.route)} · routed by ${SRC[r.plan_source] || esc(r.plan_source)} · worded by ${SRC[r.answer_source] || esc(r.answer_source)}</summary>${evidenceHtml(r.evidence)}</details>`;
  } catch (e) {
    card.querySelector(".a").textContent = "Sorry, that failed: " + e.message;
  }
  while (box.children.length > 6) box.lastElementChild.remove();
}

/* ---------------- demo controls + replay ---------------- */
async function simulateRush() {
  const res = await fetch("/demo/simulate-rush", { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) return toast(body.detail || "Rush failed", true);
  toast(`Rush started: ${body.planned} orders (${Object.entries(body.sizes).map(([k, v]) => `${v}× ${k}`).join(", ")})`);
}

async function resetDemo() {
  if (!confirm("Reset the demo data? Orders, alerts and approvals from this session are discarded.")) return;
  const b = $("#btn-reset");
  b.disabled = true; b.textContent = "Resetting…";
  try {
    const res = await fetch("/demo/reset", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return toast(body.detail || "Reset failed", true);
    toast(`Demo reset in ${body.seconds}s: data up to ${ago(body.anchor)}. Alerts will reappear in a few seconds.`);
    $("#ask-answers").innerHTML = ""; $("#replay").innerHTML = "";
    refreshAll();
  } finally { b.disabled = false; b.textContent = "↺ Reset demo"; }
}

async function runReplay() {
  const b = $("#btn-replay");
  b.disabled = true; b.textContent = "Replaying…";
  try {
    const r = await getJSON("/savings/replay");
    const wo = r.without_signalshift, wi = r.with_signalshift, d = r.detection;
    $("#replay").innerHTML = `<div class="scroll-x"><table>
      <thead><tr><th></th><th class="r">Without</th><th class="r">With SignalShift</th></tr></thead><tbody>
      <tr><td>Wasted on out-of-stock clicks</td><td class="r">${inr.format(wo.wasted_on_out_of_stock_clicks)}</td><td class="r">${inr.format(wi.wasted_on_out_of_stock_clicks)}</td></tr>
      <tr><td>Stockouts</td><td class="r">${wo.stockouts}</td><td class="r">${wo.stockouts - wi.stockouts_prevented}</td></tr>
      <tr><td>Lost sales from stockouts</td><td class="r">${inr.format(wo.lost_sales)}</td><td class="r">${inr.format(wo.lost_sales - wi.sales_protected)}</td></tr>
      </tbody></table></div>
      <p class="faint" style="margin:8px 0 0">Detection vs hidden ground truth: recall ${d.recall} (${d.detected}/${d.ground_truth_stockouts}),
      precision ${d.precision} (${d.true_positive_episodes}/${d.critical_alert_episodes} critical alerts). Early warning for
      ${d.warned_in_advance}/${d.ground_truth_stockouts} stockouts, median ${d.median_warning_lead_days} days ahead. ${esc(d.note)}</p>
      <details class="more"><summary>Assumptions</summary><ul>${r.assumptions.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>`;
  } catch (e) { toast("Replay failed: " + e.message, true); }
  finally { b.disabled = false; b.textContent = "Run replay"; }
}

/* ---------------- live ---------------- */
function refreshAll() {
  return Promise.allSettled([loadSales(), loadInventory(), loadCampaigns(), loadSavings(), loadInbox()]);
}

function connect() {
  const es = new EventSource("/stream");
  es.onopen = () => { $("#live-dot").className = "dot live"; $("#live-label").textContent = "live"; };
  es.onerror = () => { $("#live-dot").className = "dot"; $("#live-label").textContent = "reconnecting…"; };
  es.addEventListener("sale", (e) => {
    const d = JSON.parse(e.data).data;
    setTile("#k-orders", "#t-orders", num.format(d.today.orders));
    setTile("#k-revenue", "#t-revenue", inr.format(d.today.revenue));
    $("#k-units").textContent = `${num.format(d.today.units)} units`;
    debounce("sales", 800, loadSales);
    debounce("inv", 1500, loadInventory);
    debounce("camp", 3000, loadCampaigns);
  });
  es.addEventListener("inventory", (e) => patchInventory(JSON.parse(e.data).data));
  ["alert", "report", "action"].forEach((t) => es.addEventListener(t, () => debounce("inbox", 400, () => { loadInbox(); loadSavings(); })));
  es.addEventListener("ads", () => debounce("camp", 4000, loadCampaigns));
  es.addEventListener("reset", () => debounce("reset", 300, refreshAll));
  es.addEventListener("rush", (e) => {
    const d = JSON.parse(e.data).data;
    if (!d.running) toast(`Rush done: ${d.placed} orders placed, ${d.sold_out} hit a sold-out size`);
  });
  es.addEventListener("campaign", () => debounce("camp", 300, loadCampaigns));
}

function init() {
  document.querySelectorAll("#inv-tabs .tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll("#inv-tabs .tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active"); state.invGroup = t.dataset.g; renderHeatmap();
  }));
  document.querySelectorAll("#inbox-tabs .tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll("#inbox-tabs .tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active"); state.inboxStatus = t.dataset.s; loadInbox();
  }));
  $("#inbox").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (b) decide(b.dataset.id, b.dataset.act, b);
  });
  $("#ask-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#ask-input").value.trim();
    if (q.length >= 2) { ask(q); $("#ask-input").value = ""; }
  });
  document.querySelectorAll("#ask-suggest .tab").forEach((t) => t.addEventListener("click", () => ask(t.textContent)));
  $("#btn-rush").addEventListener("click", simulateRush);
  $("#btn-reset").addEventListener("click", resetDemo);
  $("#btn-replay").addEventListener("click", runReplay);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => loadSales());
  refreshAll();
  connect();
  setInterval(() => { loadCampaigns(); loadSales(); }, 60000);  // safety net if an event is missed
}

init();
