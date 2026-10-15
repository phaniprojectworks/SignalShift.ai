"use strict";
/* Ad-Assist customer storefront. Started by shell.js after a customer signs in (window.StoreApp.start). */
(() => {

const LOW_STOCK = 5;
const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const num = new Intl.NumberFormat("en-IN");
let products = [];
let group = "hero";
let pendingFocus = null;  // set by an ad click: the product and size to show once the store has rendered
let onAds = false;
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

const $ = (sel) => document.querySelector(sel);

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = "toast"), 2600);
}

function setToday(today) {
  const set = (id, tile, v) => {
    const el = $(id);
    if (el.textContent !== v) { el.textContent = v; $(tile).classList.remove("flash"); void $(tile).offsetWidth; $(tile).classList.add("flash"); }
  };
  set("#v-orders", "#tile-orders", num.format(today.orders));
  set("#v-units", "#tile-units", num.format(today.units));
  set("#v-revenue", "#tile-revenue", inr.format(today.revenue));
}

function sizeButton(p, v) {
  const b = document.createElement("button");
  b.className = "size-btn" + (v.available > 0 && v.available <= LOW_STOCK ? " low" : "");
  b.dataset.sku = v.sku;
  b.disabled = v.available <= 0;
  b.innerHTML = `<span class="sz">${v.size}</span><span class="left">${v.available > 0 ? num.format(v.available) + " left" : "sold out"}</span>`;
  b.title = `Buy 1 × ${p.name} ${v.size}`;
  b.addEventListener("click", () => buy(p, v, b));
  return b;
}

function render() {
  const grid = $("#grid");
  grid.innerHTML = "";
  for (const p of products.filter((x) => group === "all" || x.sku_group === group)) {
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.pid = p.product_id;
    card.innerHTML = `<h3>${p.name}</h3><div class="meta"><span>${inr.format(p.price)}</span>
      <span data-total="${p.product_id}">${num.format(p.total_available)} ${p.unit_label}s in stock</span></div>`;
    const sizes = document.createElement("div");
    sizes.className = "sizes";
    p.variants.forEach((v) => sizes.appendChild(sizeButton(p, v)));
    card.appendChild(sizes);
    grid.appendChild(card);
  }
  applyFocus();
}

/* after an ad click: scroll to the advertised product, mark the advertised size, and say so if it is sold out */
function applyFocus() {
  const f = pendingFocus;
  if (!f || !products.length) return;
  pendingFocus = null;
  const card = document.querySelector(`#grid .card[data-pid="${f.product_id}"]`);
  if (card) {
    card.classList.add("from-ad");
    const btn = card.querySelector(`.size-btn[data-sku="${CSS.escape(f.sku)}"]`);
    if (btn) btn.classList.add("wanted");
    requestAnimationFrame(() => card.scrollIntoView({ behavior: "smooth", block: "center" }));
  }
  const msg = $("#stock-msg");
  msg.hidden = f.in_stock;
  if (!f.in_stock) {
    $("#stock-msg-text").textContent = `${f.product_name} in ${f.size} is sold out right now. Try another size, or check back soon.`;
    toast("Stock not available", true);
  }
}

async function buy(p, v, btn) {
  btn.disabled = true;
  try {
    const res = await fetch("/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: [{ sku: v.sku, qty: 1 }], channel: "store_demo", city: $("#city").value,
                             idempotency_key: crypto.randomUUID() }),
    });
    const body = await res.json();
    if (!res.ok) { toast(body.message || "Order failed", true); return; }
    toast(`Order #${body.order_id}: ${p.name} ${v.size}`);
  } catch (e) {
    toast("Network error", true);
  } finally {
    const cur = findVariant(v.sku);
    btn.disabled = !cur || cur.available <= 0;
  }
}

function findVariant(sku) {
  for (const p of products) for (const v of p.variants) if (v.sku === sku) return v;
  return null;
}

function applyInventory(d) {
  for (const p of products) {
    const v = p.variants.find((x) => x.sku === d.sku);
    if (!v) continue;
    p.total_available += d.available - v.available;
    v.available = d.available;
    v.on_hand = d.on_hand;
    const old = document.querySelector(`.size-btn[data-sku="${CSS.escape(d.sku)}"]`);
    if (old) { const nb = sizeButton(p, v); nb.classList.add("flash"); old.replaceWith(nb); }
    const total = document.querySelector(`[data-total="${p.product_id}"]`);
    if (total) total.textContent = `${num.format(p.total_available)} ${p.unit_label}s in stock`;
  }
}

function addFeed(d, ts) {
  const feed = $("#feed");
  if (feed.firstElementChild && feed.firstElementChild.classList.contains("t")) feed.innerHTML = "";
  const li = document.createElement("li");
  const items = d.lines.map((l) => `${l.qty}× ${l.product_name} ${l.size}`).join(", ");
  li.innerHTML = `<div>${items}</div><div class="t">${ts.slice(11, 19)} · ${d.city} · ${inr.format(d.total)}</div>`;
  feed.prepend(li);
  while (feed.children.length > 40) feed.lastElementChild.remove();
}

function connect() {
  const es = new EventSource("/stream");
  es.onopen = () => { $("#live-dot").className = "dot live"; $("#live-label").textContent = "live"; };
  es.onerror = () => { $("#live-dot").className = "dot"; $("#live-label").textContent = "reconnecting…"; };
  es.addEventListener("sale", (e) => { const ev = JSON.parse(e.data); setToday(ev.data.today); addFeed(ev.data, ev.ts); });
  es.addEventListener("inventory", (e) => applyInventory(JSON.parse(e.data).data));
  es.addEventListener("campaign", () => { if (onAds) loadAds(); });  // a paused campaign stops serving ads
}

/* ---------------- ads for you (mock ad serving) ---------------- */
const AD_ART = ["#7c4dff,#ff7ab8", "#2a78d6,#7fd4ff", "#ec835a,#fab219", "#0ca30c,#7fdca0", "#5a2fc2,#2a78d6", "#d03b3b,#ff9a76"];
let ads = [];

async function loadAds() {
  const grid = $("#ad-grid");
  try {
    ads = await fetch("/api/ads?n=8").then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
  } catch (e) {
    grid.innerHTML = '<p class="faint">Ads could not be loaded.</p>';
    return;
  }
  grid.innerHTML = ads.length ? ads.map((a, i) => {
    const art = AD_ART[a.product_id % AD_ART.length].split(",");
    const icon = a.sku_group === "accessory" ? "i-tag" : "i-shoe";
    return `<button class="ad-card" type="button" data-i="${i}" aria-label="Ad: ${esc(a.model)} ${esc(a.color)}, ${esc(a.size)}, ${inr.format(a.price)}. Shop now">
      <span class="ad-top"><span class="ad-chip">Ad</span><span>Sponsored · ${esc(a.platform_label)}</span></span>
      <span class="ad-art" style="--a:${art[0]};--b:${art[1]}"><svg><use href="#${icon}"/></svg><span class="ad-size">${esc(a.size)}</span></span>
      <span class="ad-body"><b>${esc(a.model)}</b><span class="faint">${esc(a.color)}</span>
        <span class="ad-tag">${esc(a.tagline)}</span>
        <span class="ad-foot"><b>${inr.format(a.price)}</b><span class="ad-cta">Shop now →</span></span></span>
    </button>`;
  }).join("") : '<p class="faint">No ads running right now.</p>';
}

async function clickAd(a, el) {
  el.disabled = true;
  try {
    const res = await fetch("/api/ads/click", { method: "POST", headers: { "Content-Type": "application/json" },
                                               body: JSON.stringify({ campaign_id: a.campaign_id, sku: a.sku }) });
    const body = await res.json();
    if (!res.ok) { toast(body.detail || "That ad is no longer available", true); return; }
    pendingFocus = body;
    window.Shell.navigate(`#/customer/${body.sku_group}`);
    applyFocus();  // in case the store page was already showing that group
  } catch (e) {
    toast("Network error", true);
  } finally { el.disabled = false; }
}

function showAds() {
  onAds = true;
  loadAds();
}

const GROUP_TITLE = { hero: "Hero sneakers", core: "Core sneakers", accessory: "Accessories", slow: "Last season", all: "All products" };

function setGroup(g) {
  onAds = false;
  if (!pendingFocus) $("#stock-msg").hidden = true;
  if (!GROUP_TITLE[g]) g = "hero";
  group = g;
  $("#store-group-title").textContent = GROUP_TITLE[g];
  if (products.length) render();
}

async function init() {
  const [cat, today] = await Promise.all([fetch("/api/catalog").then((r) => r.json()), fetch("/api/stats/today").then((r) => r.json())]);
  products = cat;
  setToday(today);
  render();
  connect();
}

$("#ad-grid").addEventListener("click", (e) => {
  const b = e.target.closest(".ad-card");
  if (b) clickAd(ads[Number(b.dataset.i)], b);
});
$("#btn-more-ads").addEventListener("click", loadAds);
$("#stock-msg-close").addEventListener("click", () => { $("#stock-msg").hidden = true; });

let started = false;
window.StoreApp = { start() { if (!started) { started = true; init(); } }, setGroup, showAds };
})();
