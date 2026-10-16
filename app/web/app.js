"use strict";
/* SignalShift.ai owner views: polls the read APIs once, then stays live over Server-Sent Events.
   Started by shell.js after an owner signs in (window.OwnerApp.start). */

const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const num = new Intl.NumberFormat("en-IN");
const $ = (s) => document.querySelector(s);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const STATUS_LABEL = { out: "Out", low: "Low", short: "Before restock", ok: "OK" };
const SEV_LABEL = { critical: "Critical", warning: "Warning", opportunity: "Opportunity" };
const SOURCE_LABEL = { gemini: "Gemini", gemini_fallback_model: "Gemini (fallback model)", template: "Template" };

const state = { invGroup: "hero", invSort: "qty-asc", view: "", savings: null, inboxStatus: "open", inventory: [], timers: {}, seen: new Set(), openList: [], inboxLoaded: false };

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

/* inventory sort: quantity ascending by default, so the scarcest stock is on top */
const SORT_NOTE = { "qty-asc": "lowest quantity first", "qty-desc": "highest quantity first", name: "by name" };
function sortBy(list, qty, name, keepTies = false) {
  const byName = (a, b) => name(a).localeCompare(name(b), undefined, { numeric: true });
  if (state.invSort === "name") return list.sort(byName);
  const dir = state.invSort === "qty-desc" ? -1 : 1;
  return list.sort((a, b) => dir * (qty(a) - qty(b)) || (keepTies ? 0 : byName(a, b)));  // Array#sort is stable
}

function renderHeatmap() {
  const rows = sortBy(state.inventory.filter((p) => p.sku_group === state.invGroup), (p) => p.total_available, (p) => p.name);
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
  sortBy(cells, (x) => x.c.available, (x) => `${x.p.name} ${x.c.size}`, true);  // equal quantities keep the risk order
  $("#risk-sort-note").textContent = SORT_NOTE[state.invSort];
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
  const d = state.savings = await getJSON("/api/dashboard/savings");
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
  renderOwnerStatus(openList);
}

/* ---------------- business status, attention list, notifications ---------------- */
const SEV_ORDER = { critical: 0, warning: 1, opportunity: 2 };
const SEV_COLOR = { critical: "var(--critical)", warning: "var(--warning)", opportunity: "var(--series-1)" };

function renderOwnerStatus(openList) {
  const crit = openList.filter((a) => a.severity === "critical").length;
  const warn = openList.filter((a) => a.severity === "warning").length;
  const chip = $("#biz-status");
  chip.className = "status-chip " + (crit ? "crit" : warn ? "warn" : "ok");
  $("#biz-status-text").textContent = crit ? `Action needed: ${crit} critical alert${crit === 1 ? "" : "s"}`
    : warn ? `Watch: ${warn} warning${warn === 1 ? "" : "s"}` : "All healthy: no urgent risks";

  const navCount = $("#nav-alerts-count");
  navCount.hidden = !openList.length;
  navCount.textContent = openList.length;

  const top = [...openList].sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity] || b.id - a.id).slice(0, 4);
  $("#attention").innerHTML = top.length
    ? `<ul class="att-list">${top.map((a) => `<li><button type="button" data-alert="${a.id}" style="--c:${SEV_COLOR[a.severity]}">
        <span class="bar"></span><span><span class="h">${esc(a.report ? a.report.headline : a.headline)}</span>
        <div class="s">${SEV_LABEL[a.severity]} · ${ago(a.ts)}</div></span><span class="go">Review →</span></button></li>`).join("")}</ul>`
    : `<div class="all-clear"><svg><use href="#i-check"/></svg>Nothing needs your attention right now.</div>`;

  // notifications: anything not yet seen in the bell counts as unread
  const fresh = openList.filter((a) => !state.seen.has(a.id));
  const badge = $("#bell-badge");
  const prevUnread = Number(badge.dataset.n || 0);
  badge.hidden = !fresh.length;
  badge.textContent = fresh.length > 99 ? "99+" : fresh.length;
  badge.dataset.n = fresh.length;
  badge.classList.toggle("crit", fresh.some((a) => a.severity === "critical"));
  if (fresh.length > prevUnread && state.inboxLoaded) { badge.classList.remove("pulse"); void badge.offsetWidth; badge.classList.add("pulse"); }
  state.inboxLoaded = true;
  state.openList = openList;
  const list = [...openList].sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity] || b.id - a.id).slice(0, 8);
  $("#notif-list").innerHTML = list.length
    ? list.map((a) => `<li class="${state.seen.has(a.id) ? "" : "unread"}"><button type="button" data-alert="${a.id}" style="--c:${SEV_COLOR[a.severity]}">
        <span class="sdot"></span><span class="h">${esc(a.report ? a.report.headline : a.headline)}</span>
        <span class="t">${SEV_LABEL[a.severity]} · ${ago(a.ts)}</span></button></li>`).join("")
    : '<li class="faint pad">You are all caught up.</li>';
}

function markNotificationsSeen() {
  (state.openList || []).forEach((a) => state.seen.add(a.id));
  const badge = $("#bell-badge");
  badge.hidden = true; badge.dataset.n = 0;
}

/* jump to one alert card in the Alerts view */
function openAlert(id) {
  window.Shell.navigate("#/owner/alerts");
  requestAnimationFrame(() => {
    const card = document.querySelector(`#inbox .alert[data-id="${id}"]`);
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("focus");
    setTimeout(() => card.classList.remove("focus"), 2000);
  });
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
  window.Shell.navigate("#/owner/ask");
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
  const ok = await window.Shell.confirm("Reset the demo?", "Orders, alerts and approvals from this session are discarded.", "Reset");
  if (!ok) return;
  const b = $("#btn-reset"), label = b.querySelector("span");
  // nothing else should touch the data while it is being rebuilt
  const busy = [b, $("#btn-rush")];
  busy.forEach((x) => { x.disabled = true; });
  label.textContent = "Resetting…";
  try {
    const res = await fetch("/demo/reset", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return toast(body.detail || "Reset failed", true);
    toast(`Demo reset in ${body.seconds}s: data up to ${ago(body.anchor)}. Alerts will reappear in a few seconds.`);
    $("#ask-answers").innerHTML = ""; $("#replay").innerHTML = "";
    state.seen.clear();
    refreshAll();
  } finally { busy.forEach((x) => { x.disabled = false; }); label.textContent = "Reset demo"; }
}

async function runReplay() {
  const b = $("#btn-replay");
  b.disabled = true; b.textContent = "Replaying…";
  try {
    const r = await getJSON("/savings/replay");
    const wo = r.without_signalshift, wi = r.with_signalshift, d = r.detection;
    $("#replay").innerHTML = `<div class="scroll-x"><table>
      <thead><tr><th></th><th class="r">Without</th><th class="r">With SignalShift.ai</th></tr></thead><tbody>
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

/* ---------------- shared chart layout ---------------- */
function chartLayout(extra = {}) {
  const ink = css("--text"), muted = css("--faint"), grid = css("--grid");
  return {
    margin: { l: 44, r: 8, t: 8, b: 34 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "system-ui, -apple-system, Segoe UI, sans-serif", color: ink, size: 12 },
    xaxis: { gridcolor: "rgba(0,0,0,0)", linecolor: grid, tickfont: { color: muted }, fixedrange: true, type: "date", tickformat: "%d %b" },
    yaxis: { gridcolor: grid, zeroline: false, tickfont: { color: muted }, fixedrange: true, rangemode: "tozero" },
    legend: { orientation: "h", x: 0, y: 1.14, font: { color: muted } }, bargap: 0.25, hovermode: "x", ...extra,
  };
}
const PLOT_CFG = { displayModeBar: false, responsive: true };

/* ---------------- sales page: order log ---------------- */
const CHANNEL_LABEL = { web: "Web", app: "App", instagram_shop: "Instagram shop", store_demo: "Demo store", simulator: "Rush simulator" };
const sales = { offset: 0, limit: 50, total: 0, seq: 0, loaded: false, trend: null, trendAt: 0, group: "", sort: "units", sel: null, daily: null };
const fullDate = (ts) => new Date(String(ts).replace(" ", "T")).toLocaleString("en-IN",
  { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });

async function loadOrders() {
  const seq = ++sales.seq;
  const q = $("#o-q").value.trim(), days = $("#o-days").value, channel = $("#o-channel").value;
  const p = new URLSearchParams({ limit: sales.limit, offset: sales.offset });
  if (q) p.set("q", q);
  if (days) p.set("days", days);
  if (channel) p.set("channel", channel);
  $("#orders-panel").classList.add("loading");
  $("#o-range").textContent = "Searching…";
  const d = await getJSON("/api/dashboard/orders?" + p).finally(() => { if (seq === sales.seq) $("#orders-panel").classList.remove("loading"); });
  if (seq !== sales.seq) return;  // a newer filter is already on its way
  sales.loaded = true;
  sales.total = d.total;
  $("#o-count").textContent = num.format(d.total);
  $("#o-units").textContent = num.format(d.units);
  $("#o-revenue").textContent = inr.format(d.revenue);
  $("#o-aov").textContent = d.total ? `avg ${inr.format(d.revenue / d.total)} per order` : "–";
  $("#o-scope").textContent = [$("#o-days").selectedOptions[0].text, channel ? CHANNEL_LABEL[channel] : "all channels",
    q ? `matching “${q}”` : ""].filter(Boolean).join(" · ");
  $("#order-rows").innerHTML = d.rows.length ? d.rows.map((o) => `<tr>
      <td><b>#${o.order_id}</b></td><td class="num">${fullDate(o.ts)}</td><td>${esc(CHANNEL_LABEL[o.channel] || o.channel)}</td><td>${esc(o.city)}</td>
      <td>${o.lines.map((l) => `<span class="item">${esc(l.product_name)} · ${esc(l.size)}${l.qty > 1 ? ` × ${l.qty}` : ""} <span class="sku">${esc(l.sku)}</span></span>`).join("")}</td>
      <td class="r">${num.format(o.units)}</td><td class="r">${inr.format(o.total)}</td></tr>`).join("")
    : `<tr><td colspan="7" class="faint">No orders match these filters.</td></tr>`;
  const from = d.total ? d.offset + 1 : 0, to = d.offset + d.rows.length;
  $("#o-range").textContent = `${num.format(from)}–${num.format(to)} of ${num.format(d.total)} orders`;
  $("#o-prev").disabled = d.offset <= 0;
  $("#o-next").disabled = to >= d.total;
}
const reloadOrders = () => { sales.offset = 0; debounce("orders", 250, loadOrders); };

/* ---------------- sales page: per-SKU trend ---------------- */
function sparkline(vals, starts) {
  const w = 120, h = 28, pad = 3, max = Math.max(1, ...vals);
  const x = (i) => pad + (i * (w - 2 * pad)) / Math.max(1, vals.length - 1);
  const y = (v) => h - pad - (v / max) * (h - 2 * pad);
  const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const tip = vals.map((v, i) => `${new Date(starts[i]).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}: ${v}`).join("\n");
  const last = vals.length - 1;
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Weekly units ${vals.join(", ")}">
    <title>Units per week (week starting)\n${tip}</title>
    <polygon points="${x(0)},${h - pad} ${pts} ${x(last)},${h - pad}" fill="currentColor" opacity=".14"/>
    <polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${x(last)}" cy="${y(vals[last])}" r="3" fill="currentColor" stroke="var(--surface)" stroke-width="1.5"/></svg>`;
}
const delta = (pct) => pct === null ? '<span class="faint">new</span>'
  : `<span class="delta">${pct > 0 ? "▲" : pct < 0 ? "▼" : "■"} ${Math.abs(pct).toFixed(0)}%</span>`;

async function loadTrend(force = false) {
  if (!force && sales.trend && Date.now() - sales.trendAt < 60000) return;
  sales.trend = await getJSON("/api/dashboard/sku-trend");
  sales.trendAt = Date.now();
  const ws = sales.trend.week_starts;
  $("#trend-scope").textContent = `last 13 weeks · ${new Date(ws[0]).toLocaleDateString("en-IN", { day: "numeric", month: "short" })} to today`;
  renderTrend();
}

function renderTrend() {
  if (!sales.trend) return;
  const q = $("#trend-q").value.trim().toLowerCase();
  let rows = sales.trend.rows.filter((r) => (!sales.group || r.sku_group === sales.group) &&
    (!q || r.sku.toLowerCase().includes(q) || r.product_name.toLowerCase().includes(q)));
  const name = (a, b) => a.product_name.localeCompare(b.product_name) || a.sku.localeCompare(b.sku, undefined, { numeric: true });
  const sorters = {
    units: (a, b) => b.units - a.units || name(a, b),
    rising: (a, b) => (b.change_pct ?? -1e9) - (a.change_pct ?? -1e9) || b.units - a.units,
    falling: (a, b) => (a.change_pct ?? 1e9) - (b.change_pct ?? 1e9) || b.units - a.units,
    name,
  };
  rows = rows.sort(sorters[sales.sort]);
  const ws = sales.trend.week_starts;
  $("#trend-rows").innerHTML = rows.length ? rows.map((r) => `<tr data-vid="${r.variant_id}" class="${r.variant_id === sales.sel ? "sel" : ""}" tabindex="0">
      <td class="sku">${esc(r.sku)}</td><td>${esc(r.product_name)}</td><td>${esc(r.size)}</td>
      ${r.units_by_month.map((u) => `<td class="r">${num.format(u)}</td>`).join("")}
      <td>${sparkline(r.weekly_units, ws)}</td><td class="r"><b>${num.format(r.units)}</b></td><td class="r">${inr.format(r.revenue)}</td>
      <td class="r">${delta(r.change_pct)}</td></tr>`).join("")
    : `<tr><td colspan="10" class="faint">No SKUs match.</td></tr>`;
}

async function showSku(vid) {
  sales.sel = vid;
  renderTrend();
  const d = sales.daily = await getJSON(`/api/dashboard/sku-trend/${vid}`);
  $("#trend-detail").hidden = false;
  $("#trend-detail-title").textContent = `${d.product_name} · ${d.size} (${d.sku})`;
  drawSku();
  $("#trend-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function drawSku() {
  const d = sales.daily;
  if (!d || !window.Plotly || $("#trend-detail").hidden) return;
  const x = d.days.map((p) => p.date), units = d.days.map((p) => p.units);
  const avg = units.map((_, i) => (i < 6 ? null : units.slice(i - 6, i + 1).reduce((a, b) => a + b, 0) / 7));
  Plotly.react("chart-sku", [
    { x, y: units, type: "bar", name: "Units per day", marker: { color: css("--series-1") },
      hovertemplate: "%{x|%d %b %Y} · %{y} units<extra></extra>" },
    { x, y: avg, type: "scatter", mode: "lines", name: "7-day average", line: { color: css("--text"), width: 2 },
      hovertemplate: "7-day avg %{y:.1f}<extra></extra>" },
  ], chartLayout(), PLOT_CFG);
}

/* ---------------- live ---------------- */
function refreshAll() {
  return Promise.allSettled([loadSales(), loadInventory(), loadCampaigns(), loadSavings(), loadInbox(),
    sales.loaded ? loadOrders() : null, sales.trend ? loadTrend(true) : null]);
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
  // sales page: new orders show up live on the first page; the SKU trend refreshes next time it is opened
  es.addEventListener("sale", () => {
    sales.trendAt = 0;
    if (state.view === "sales" && sales.offset === 0) debounce("orders", 1200, loadOrders);
  });
  ["alert", "report", "action"].forEach((t) => es.addEventListener(t, () => debounce("inbox", 400, () => { loadInbox(); loadSavings(); })));
  es.addEventListener("ads", () => debounce("camp", 4000, loadCampaigns));
  // a shopper clicked an ad on the customer side: update the wasted-spend tile now, not on the next ad tick
  es.addEventListener("ad_click", (e) => {
    const d = JSON.parse(e.data).data;
    debounce("camp", 300, loadCampaigns);
    if (d.wasted) toast(`Wasted ${inr.format(d.cost)}: a shopper clicked an ad for ${d.product_name} ${d.size}, which is out of stock`, true);
  });
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
  document.querySelectorAll("#ask-suggest .suggest").forEach((t) => t.addEventListener("click", () => ask(t.textContent)));
  // alert shortcuts from the overview list and the notification bell
  ["#attention", "#notif-list"].forEach((sel) => $(sel).addEventListener("click", (e) => {
    const b = e.target.closest("button[data-alert]");
    if (b) { window.Shell.closePopovers(); openAlert(Number(b.dataset.alert)); }
  }));
  $("#btn-bell").addEventListener("click", () => setTimeout(markNotificationsSeen, 1500));
  $("#btn-rush").addEventListener("click", simulateRush);
  $("#btn-reset").addEventListener("click", resetDemo);
  $("#btn-replay").addEventListener("click", runReplay);
  $("#inv-sort").addEventListener("change", (e) => { state.invSort = e.target.value; renderHeatmap(); renderRiskTable(); });
  // sales page
  $("#orders-filter").addEventListener("submit", (e) => { e.preventDefault(); reloadOrders(); });
  $("#o-q").addEventListener("input", () => { sales.offset = 0; debounce("orders", 400, loadOrders); });
  ["#o-days", "#o-channel"].forEach((sel) => $(sel).addEventListener("change", reloadOrders));
  $("#o-prev").addEventListener("click", () => { sales.offset = Math.max(0, sales.offset - sales.limit); loadOrders(); });
  $("#o-next").addEventListener("click", () => { sales.offset += sales.limit; loadOrders(); });
  document.querySelectorAll("#trend-tabs .tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll("#trend-tabs .tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active"); sales.group = t.dataset.g; renderTrend();
  }));
  $("#trend-q").addEventListener("input", () => debounce("trendq", 200, renderTrend));
  $("#trend-sort").addEventListener("change", (e) => { sales.sort = e.target.value; renderTrend(); });
  const pick = (e) => { const tr = e.target.closest("tr[data-vid]"); if (tr) showSku(Number(tr.dataset.vid)); };
  $("#trend-rows").addEventListener("click", pick);
  $("#trend-rows").addEventListener("keydown", (e) => { if (e.key === "Enter") pick(e); });
  $("#trend-detail-close").addEventListener("click", () => { $("#trend-detail").hidden = true; sales.sel = null; renderTrend(); });
  const redraw = () => { loadSales(); drawSku(); };
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
  document.addEventListener("themechange", redraw);
  refreshAll();
  connect();
  setInterval(() => { loadCampaigns(); loadSales(); }, 60000);  // safety net if an event is missed
}

/* called by the shell on every page change. Plotly sizes itself on window resize only, so charts drawn
   while their page was hidden are resized (or drawn) when it becomes visible */
function onShow(view) {
  state.view = view;
  const resize = (id) => { if (window.Plotly && $("#" + id).data) Plotly.Plots.resize(id); };
  if (view === "overview") resize("chart-hourly");
  if (view === "sales") {
    if (!sales.loaded) loadOrders();
    loadTrend();
    requestAnimationFrame(() => resize("chart-sku"));
  }
}

let started = false;
window.OwnerApp = { start() { if (!started) { started = true; init(); } }, onShow };
