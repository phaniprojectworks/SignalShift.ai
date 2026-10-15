"use strict";

const LOW_STOCK = 5;
const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const num = new Intl.NumberFormat("en-IN");
let products = [];
let group = "hero";

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
    card.innerHTML = `<h3>${p.name}</h3><div class="meta"><span>${inr.format(p.price)}</span>
      <span data-total="${p.product_id}">${num.format(p.total_available)} ${p.unit_label}s in stock</span></div>`;
    const sizes = document.createElement("div");
    sizes.className = "sizes";
    p.variants.forEach((v) => sizes.appendChild(sizeButton(p, v)));
    card.appendChild(sizes);
    grid.appendChild(card);
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
}

async function init() {
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    group = t.dataset.group;
    render();
  }));
  const [cat, today] = await Promise.all([fetch("/api/catalog").then((r) => r.json()), fetch("/api/stats/today").then((r) => r.json())]);
  products = cat;
  setToday(today);
  render();
  connect();
}

init();
