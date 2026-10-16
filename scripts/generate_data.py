"""Seeded synthetic data generator for the Solenest demo (fictional Indian D2C footwear brand).

All numbers produced here are SYNTHETIC SAMPLE DATA, not benchmarks.

How it works
------------
1. Build the catalog (products x size variants) and campaigns.
2. Draw hourly demand for 2 years per variant:
       base rate x day-of-week x trend x festival multiplier x campaign uplift x noise
   split across sizes by a size curve (UK 8 + 9 ~ 55% of footwear) and an intraday curve.
3. Simulate inventory *backwards* from the desired demo state at the anchor time. Walking back,
   stock grows by each hour's sales; when it would exceed the planner's cap, a restock arrival
   is inserted so the stock just before that arrival is a small safety floor (>= 1). This
   guarantees: the final state is exactly the demo state, stock never goes negative, and the
   ONLY stockouts are the deliberately planted ones (recorded in `ground_truth`).
4. Write orders, movements, hourly ad metrics (last 30 days), site events, calendar, inventory.

Usage:  python scripts/generate_data.py --reset [--seed 2026] [--anchor 2026-10-15T10:00] [--db path]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import init_db, make_engine  # noqa: E402
from app.services.calendar import festival_multiplier, is_sale_day, load_calendar  # noqa: E402
from app.services.profiles import AD_CURVE, INTRADAY  # noqa: E402

HISTORY_DAYS = 730
AD_HISTORY_DAYS = 30

FOOT_SIZES = ["UK 6", "UK 7", "UK 8", "UK 9", "UK 10", "UK 11"]
FOOT_SHARE = [0.06, 0.15, 0.27, 0.28, 0.16, 0.08]  # UK 8 + UK 9 = 55%
SOCK_SIZES = ["UK 6-8", "UK 9-11"]
SOCK_SHARE = [0.42, 0.58]
ONE_SIZE = ["One Size"]

DOW = [0.90, 0.88, 0.92, 0.95, 1.05, 1.18, 1.12]  # Mon..Sun

CITIES = ["Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Chennai", "Pune", "Kolkata", "Ahmedabad", "Jaipur", "Lucknow"]
CITY_W = [0.17, 0.17, 0.16, 0.11, 0.09, 0.09, 0.07, 0.06, 0.04, 0.04]
CHANNELS = ["web", "app", "instagram_shop"]
CHANNEL_W = [0.52, 0.36, 0.12]

TS_FMT = "%Y-%m-%d %H:%M:%S.%f"  # identical to SQLAlchemy's SQLite DateTime storage format


@dataclass
class ProductDef:
    model: str
    color: str
    group: str  # hero / core / slow / accessory
    tag: str  # seasonality tag
    price: float
    cost_ratio: float
    base_daily: float  # units/day at baseline (no festival, no campaign)
    lead_days: int
    kind: str  # footwear / socks / onesize
    sizes: list[str] = field(default_factory=list)
    shares: list[float] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.model} — {self.color}" if self.color else self.model

    @property
    def unit_label(self) -> str:
        return {"footwear": "pair", "socks": "pack"}.get(self.kind, "unit")


def _p(model: str, color: str, group: str, tag: str, price: float, cr: float, base: float, lead: int,
       kind: str = "footwear") -> ProductDef:
    sizes, shares = {"footwear": (FOOT_SIZES, FOOT_SHARE), "socks": (SOCK_SIZES, SOCK_SHARE),
                     "onesize": (ONE_SIZE, [1.0])}[kind]
    return ProductDef(model, color, group, tag, price, cr, base, lead, kind, list(sizes), list(shares))


CATALOG: list[ProductDef] = [
    # Hero sneakers (festive, campaign-driven)
    _p("Retro Runner '86", "Black/Gum", "hero", "festive_hero", 5999, 0.45, 9, 12),
    _p("Retro Runner '86", "Sail/Navy", "hero", "festive_hero", 5999, 0.45, 7, 12),
    _p("Festive White Sneaker", "White/Gold", "hero", "festive_hero", 5499, 0.42, 12, 10),
    _p("Chunky Trend Sneaker", "Cloud Grey", "hero", "festive_hero", 4999, 0.44, 8, 12),
    _p("Chunky Trend Sneaker", "Mocha", "hero", "festive_hero", 4999, 0.44, 6, 12),
    # Core sneakers
    _p("Everyday Runner", "Black", "core", "core", 3299, 0.48, 9, 8),
    _p("Everyday Runner", "Navy", "core", "core", 3299, 0.48, 7, 8),
    _p("Everyday Runner", "Grey", "core", "core", 3299, 0.48, 6, 8),
    _p("Everyday Runner", "White", "core", "core", 3299, 0.48, 6, 8),
    _p("Canvas Low-Top", "White", "core", "core", 2499, 0.45, 8, 7),
    _p("Canvas Low-Top", "Black", "core", "core", 2499, 0.45, 6, 7),
    _p("Canvas Low-Top", "Olive", "core", "core", 2499, 0.45, 4, 7),
    _p("Canvas Low-Top", "Navy", "core", "core", 2499, 0.45, 4, 7),
    _p("Canvas High-Top", "White", "core", "core", 2799, 0.46, 5, 7),
    _p("Canvas High-Top", "Black", "core", "core", 2799, 0.46, 4, 7),
    _p("Court Classic", "White/Green", "core", "core", 3499, 0.47, 5, 9),
    _p("Court Classic", "White/Navy", "core", "core", 3499, 0.47, 4, 9),
    _p("Court Classic", "All White", "core", "core", 3499, 0.47, 6, 9),
    _p("Slip-On Canvas", "Beige", "core", "core", 1999, 0.44, 4, 7),
    _p("Slip-On Canvas", "Black", "core", "core", 1999, 0.44, 4, 7),
    _p("Trail Lite", "Olive", "core", "core", 3999, 0.50, 3, 10),
    _p("Trail Lite", "Charcoal", "core", "core", 3999, 0.50, 3, 10),
    _p("Knit Walker", "Grey", "core", "core", 2999, 0.46, 4, 8),
    _p("Knit Walker", "Black", "core", "core", 2999, 0.46, 4, 8),
    # Slow movers (last-season colorways)
    _p("Retro Runner '85", "Teal (SS25)", "slow", "slow", 3999, 0.55, 0.6, 10),
    _p("Chunky Trend Sneaker", "Neon Lime (SS25)", "slow", "slow", 3499, 0.55, 0.5, 10),
    _p("Everyday Runner", "Lilac (SS25)", "slow", "slow", 2299, 0.60, 0.8, 10),
    _p("Everyday Runner", "Peach (SS25)", "slow", "slow", 2299, 0.60, 0.7, 10),
    _p("Canvas Low-Top", "Mustard (SS25)", "slow", "slow", 1699, 0.62, 0.9, 10),
    _p("Canvas Low-Top", "Coral (SS25)", "slow", "slow", 1699, 0.62, 0.6, 10),
    _p("Canvas High-Top", "Mint (SS25)", "slow", "slow", 1899, 0.60, 0.5, 10),
    _p("Court Classic", "Maroon (SS25)", "slow", "slow", 2499, 0.58, 0.6, 10),
    _p("Slip-On Canvas", "Sky (SS25)", "slow", "slow", 1499, 0.62, 0.7, 10),
    _p("Trail Lite", "Orange (SS25)", "slow", "slow", 2799, 0.60, 0.4, 10),
    _p("Knit Walker", "Sand (SS25)", "slow", "slow", 1999, 0.60, 0.6, 10),
    _p("Festive Velvet Sneaker", "Wine (Diwali '24)", "slow", "slow", 2999, 0.55, 0.5, 10),
    # Accessories (gifting ones get the big festive uplift)
    _p("Ankle Socks 3-Pack", "Assorted", "accessory", "gifting", 499, 0.35, 10, 6, "socks"),
    _p("Crew Socks 3-Pack", "Assorted", "accessory", "gifting", 549, 0.35, 7, 6, "socks"),
    _p("Festive Socks Gift Box", "Red/Gold", "accessory", "gifting", 899, 0.38, 2.5, 6, "socks"),
    _p("Sneaker Cleaning Kit", "", "accessory", "accessory", 699, 0.30, 4, 6, "onesize"),
    _p("Everyday Backpack", "Black", "accessory", "accessory", 2499, 0.45, 2, 8, "onesize"),
    _p("Everyday Backpack", "Olive", "accessory", "accessory", 2499, 0.45, 1.5, 8, "onesize"),
    _p("Logo Cap", "Black", "accessory", "accessory", 799, 0.35, 2.5, 6, "onesize"),
    _p("Logo Cap", "Beige", "accessory", "accessory", 799, 0.35, 1.5, 6, "onesize"),
]


@dataclass
class CampaignDef:
    platform: str
    name: str
    model: str
    color: str
    daily_budget: float
    bid: float
    uplift: float  # demand multiplier while active
    start_days_ago: int
    end_days_ago: int | None = None  # None -> still active
    size_mix: dict[str, float] | None = None  # click share per size; default derived from size curve
    cvr: float = 0.035  # click -> order conversion rate when every size is in stock


FESTIVE_WHITE_MIX = {"UK 6": 0.04, "UK 7": 0.12, "UK 8": 0.33, "UK 9": 0.35, "UK 10": 0.11, "UK 11": 0.05}

CAMPAIGNS: list[CampaignDef] = [
    CampaignDef("google_ads", "Shopping | Festive White Sneaker | Diwali", "Festive White Sneaker", "White/Gold",
                2600, 14, 1.35, 20, None, FESTIVE_WHITE_MIX),
    CampaignDef("meta_ads", "Reels | Festive White Sneaker | Festive drop", "Festive White Sneaker", "White/Gold",
                1400, 9, 1.15, 12, None, FESTIVE_WHITE_MIX),
    CampaignDef("google_ads", "Shopping | Retro Runner '86 Black/Gum", "Retro Runner '86", "Black/Gum",
                2000, 13, 1.3, 28),
    CampaignDef("meta_ads", "Carousel | Retro Runner '86 Sail/Navy", "Retro Runner '86", "Sail/Navy",
                1500, 9, 1.25, 26),
    CampaignDef("google_ads", "Shopping | Chunky Trend Cloud Grey", "Chunky Trend Sneaker", "Cloud Grey",
                1800, 12, 1.3, 25),
    CampaignDef("meta_ads", "Reels | Chunky Trend Mocha", "Chunky Trend Sneaker", "Mocha",
                1200, 8, 1.25, 24),
    CampaignDef("google_ads", "Search | Everyday Runner Black", "Everyday Runner", "Black",
                2000, 11, 1.2, 29, cvr=0.05),
    CampaignDef("meta_ads", "Feed | Everyday Runner Navy", "Everyday Runner", "Navy",
                800, 7, 1.15, 30, 10),
    CampaignDef("google_ads", "Shopping | Canvas Low-Top White", "Canvas Low-Top", "White",
                1000, 9, 1.2, 15, cvr=0.03),
    CampaignDef("meta_ads", "Gifting | Festive Socks Gift Box", "Festive Socks Gift Box", "Red/Gold",
                700, 6, 1.4, 6),
    CampaignDef("google_ads", "Shopping | Everyday Backpack Black", "Everyday Backpack", "Black",
                600, 10, 1.2, 30, 18),
    CampaignDef("meta_ads", "Clearance | SS25 Everyday Runner Lilac", "Everyday Runner", "Lilac (SS25)",
                500, 6, 1.4, 30, 20),
]

# Hidden truth: hero variants that deliberately stock out while their campaign is live.
# (model, color, size, days_ago, hour, outage_hours)
PLANTED_STOCKOUTS = [
    ("Retro Runner '86", "Black/Gum", "UK 9", 22, 14, 62),
    ("Retro Runner '86", "Black/Gum", "UK 8", 21, 20, 44),
    ("Chunky Trend Sneaker", "Cloud Grey", "UK 8", 12, 11, 48),
    ("Retro Runner '86", "Sail/Navy", "UK 9", 7, 19, 70),
    ("Chunky Trend Sneaker", "Mocha", "UK 9", 4, 13, 40),
]

# Demo state at the anchor: Festive White total looks healthy but UK 8 / UK 9 are nearly gone.
FESTIVE_WHITE_DEMO_STOCK = {"UK 6": 20, "UK 7": 48, "UK 8": 11, "UK 9": 4, "UK 10": 50, "UK 11": 27}
# Everyday Runner on-hand stock = 25 days of recent sales, which the engine's (festival-ramping) forecast
# reads as ~22 days of cover: the budget-shift target in the demo.
BUDGET_SHIFT_TARGET_COVER_DAYS = 25.0
FESTIVE_PLANNING_DAYS = 30
UNDERPREPARED_FOR_FESTIVAL = {
    ("Retro Runner '86", "Black/Gum"), ("Retro Runner '86", "Sail/Navy"), ("Festive White Sneaker", "White/Gold"),
    ("Chunky Trend Sneaker", "Cloud Grey"), ("Chunky Trend Sneaker", "Mocha"), ("Festive Socks Gift Box", "Red/Gold"),
}


def _code(text: str, n: int = 4) -> str:
    words = [w for w in "".join(c if c.isalnum() or c == " " else " " for c in text).split() if w]
    if not words:
        return "STD"
    if len(words) == 1:
        return words[0][:n].upper()
    return "".join(w[0] for w in words)[:n].upper()


def _size_code(size: str) -> str:
    return size.replace("UK ", "UK").replace(" ", "").replace("-", "").upper() if size != "One Size" else "OS"


def _fmt(ts: datetime) -> str:
    return ts.strftime(TS_FMT)


def default_anchor(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz)).replace(tzinfo=None, minute=0, second=0, microsecond=0)


def generate(db_path: str, seed: int, anchor: datetime, calendar_file: str, reset: bool, quiet: bool = False) -> dict:
    t0 = time.time()
    rng = np.random.default_rng(seed)
    path = Path(db_path)
    if path.exists():
        if not reset:
            raise SystemExit(f"{path} already exists. Re-run with --reset to rebuild it.")
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    engine = make_engine(str(path))
    init_db(engine)
    events = load_calendar(calendar_file)

    # ---------------- Catalog ----------------
    variants: list[dict] = []  # flat variant list, index = column in demand matrix
    for pi, p in enumerate(CATALOG):
        for si, (size, share) in enumerate(zip(p.sizes, p.shares)):
            variants.append({"pidx": pi, "size": size, "share": share, "rank": si,
                             "sku": f"SN-{_code(p.model)}-{_code(p.color) if p.color else 'STD'}-{_size_code(size)}"})
    V, P = len(variants), len(CATALOG)
    assert len({v["sku"] for v in variants}) == V, "SKU collision"
    v_pidx = np.array([v["pidx"] for v in variants])
    v_share = np.array([v["share"] for v in variants])
    v_lead = np.array([CATALOG[v["pidx"]].lead_days for v in variants])
    pkey = {(p.model, p.color): i for i, p in enumerate(CATALOG)}
    vkey = {(CATALOG[v["pidx"]].model, CATALOG[v["pidx"]].color, v["size"]): i for i, v in enumerate(variants)}

    # ---------------- Time axis ----------------
    start = anchor - timedelta(days=HISTORY_DAYS)
    H = HISTORY_DAYS * 24
    hour_ts = [start + timedelta(hours=h) for h in range(H)]
    first_day = start.date()
    n_days = (anchor.date() - first_day).days + 1
    days = [first_day + timedelta(days=i) for i in range(n_days)]
    h_day = np.array([(t.date() - first_day).days for t in hour_ts])
    h_hod = np.array([t.hour for t in hour_ts])

    # ---------------- Daily product rates ----------------
    tags = sorted({p.tag for p in CATALOG})
    fest = {tag: np.array([festival_multiplier(events, d, tag) for d in days]) for tag in tags}
    dow = np.array([DOW[d.weekday()] for d in days])
    trend = 0.85 + 0.15 * np.arange(n_days) / max(1, n_days - 1)  # mild brand growth
    base = np.array([p.base_daily for p in CATALOG])
    fest_pd = np.vstack([fest[p.tag] for p in CATALOG])  # (P, days)
    expected_daily = base[:, None] * trend[None, :] * fest_pd  # planner's view (no noise, no DOW)
    noise = rng.gamma(10.0, 0.1, size=(P, n_days))
    daily = expected_daily * dow[None, :] * noise

    # ---------------- Campaign activity (last 30 days only) ----------------
    ad_start_h = H - AD_HISTORY_DAYS * 24
    camp_active = np.zeros((len(CAMPAIGNS), H), dtype=bool)
    camp_uplift_ph = np.ones((P, H))
    for ci, c in enumerate(CAMPAIGNS):
        s = H - c.start_days_ago * 24 + int(rng.integers(8, 12))  # campaigns go live mid-morning
        e = H if c.end_days_ago is None else H - c.end_days_ago * 24 + int(rng.integers(8, 12))
        camp_active[ci, max(s, ad_start_h):e] = True
        pi = pkey[(c.model, c.color)]
        camp_uplift_ph[pi] *= np.where(camp_active[ci], c.uplift, 1.0)

    # ---------------- Hourly demand ----------------
    rate = daily[v_pidx][:, h_day].T * INTRADAY[h_hod][:, None] * v_share[None, :] * camp_uplift_ph[v_pidx].T
    demand = rng.poisson(rate).astype(np.int64)  # (H, V)

    # ---------------- Planted stockouts ----------------
    oos = np.zeros((H, V), dtype=bool)
    forced_arrival: dict[int, list[int]] = {}
    planted: list[tuple[int, int, int]] = []  # (v, ts_h, restock_h)
    for model, color, size, days_ago, hour, outage in PLANTED_STOCKOUTS:
        vi = vkey[(model, color, size)]
        ts = (anchor - timedelta(days=days_ago)).replace(hour=hour)
        ts_h = int((ts - start).total_seconds() // 3600)
        r_h = ts_h + outage
        oos[ts_h:r_h, vi] = True
        demand[ts_h - 1, vi] = max(demand[ts_h - 1, vi], 1)  # the last pair sells right before the stockout
        forced_arrival.setdefault(r_h, []).append(vi)
        planted.append((vi, ts_h, r_h))
    sales = np.where(oos, 0, demand)

    # Returns: ~3% of footwear / 1% of other sales come back 10 days later.
    ret_rate = np.array([0.03 if CATALOG[v["pidx"]].kind == "footwear" else 0.01 for v in variants])
    returns = np.zeros_like(sales)
    returns[240:] = rng.binomial(sales[:-240], ret_rate[None, :])
    returns[oos] = 0

    # ---------------- Planner caps / floors ----------------
    camp_day = np.ones((P, n_days))
    for p in range(P):
        camp_day[p] = np.bincount(h_day, weights=camp_uplift_ph[p], minlength=n_days) / np.maximum(
            np.bincount(h_day, minlength=n_days), 1)
    exp_v = (expected_daily * camp_day)[v_pidx] * v_share[:, None]  # (V, days) expected units/day
    max_lead = int(v_lead.max()) + 7
    padded = np.concatenate([exp_v, np.repeat(exp_v[:, -1:], max_lead, axis=1)], axis=1)
    csum = np.concatenate([np.zeros((V, 1)), np.cumsum(padded, axis=1)], axis=1)
    win = v_lead + 7
    idx = np.arange(n_days)
    fwd_mean = (csum[np.arange(V)[:, None], idx[None, :] + win[:, None]] - csum[:, idx]) / win[:, None]
    is_slow = np.array([CATALOG[v["pidx"]].group == "slow" for v in variants])
    cap = np.where(is_slow[:, None], np.maximum(8, 90 * fwd_mean), np.maximum(4, win[:, None] * fwd_mean * 1.25))

    # ---------------- Final (anchor) stock = demo state ----------------
    exp_now = exp_v[:, -1]
    recent_daily_sales = sales[-14 * 24:].sum(axis=0) / 14.0
    final = np.zeros(V, dtype=np.int64)
    for vi, v in enumerate(variants):
        p = CATALOG[v["pidx"]]
        if p.model == "Festive White Sneaker":
            final[vi] = FESTIVE_WHITE_DEMO_STOCK[v["size"]]
        elif p.model == "Everyday Runner" and p.group == "core":
            final[vi] = max(3, round(BUDGET_SHIFT_TARGET_COVER_DAYS * recent_daily_sales[vi]))
        elif p.group == "slow":
            final[vi] = max(3, round(rng.uniform(60, 150) * exp_now[vi]))
        else:
            final[vi] = max(3, round(rng.uniform(v_lead[vi] + 6, v_lead[vi] + 20) * exp_now[vi]))

    # ---------------- Backward inventory simulation ----------------
    stock_end = np.empty((H, V), dtype=np.int64)
    arrivals: list[tuple[int, int, int]] = []  # (h, v, qty)
    S = final.copy()
    for h in range(H - 1, -1, -1):
        stock_end[h] = S
        ret = np.minimum(returns[h], np.maximum(S + sales[h] - 1, 0))
        returns[h] = ret
        pre = S + sales[h] - ret  # stock at start of hour, after any arrival
        arr = np.zeros(V, dtype=np.int64)
        forced = forced_arrival.get(h, [])
        if forced:
            arr[forced] = pre[forced]  # stock was exactly 0 right before this delivery
        if h_hod[h] == 10:
            d = h_day[h]
            need = pre > cap[:, d]
            need[forced] = False
            need &= ~oos[h]
            if need.any():
                floor = np.maximum(1, np.round(rng.uniform(0.3, 0.6, V) * v_lead * fwd_mean[:, d])).astype(np.int64)
                arr[need] = pre[need] - np.minimum(floor[need], pre[need] - 1)
        for vi in np.nonzero(arr)[0]:
            arrivals.append((h, int(vi), int(arr[vi])))
        S = pre - arr
    opening = S
    assert (stock_end >= 0).all() and (opening >= 0).all()

    # ---------------- Write to DB ----------------
    raw = engine.raw_connection()
    cur = raw.cursor()
    cur.execute("BEGIN")  # one bulk transaction (autocommit is off at the driver level)

    cur.executemany(
        "INSERT INTO products (id, sku_group, name, model, category, price, cost, seasonality_tag, unit_label) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(i + 1, p.group, p.name, p.model, "accessories" if p.group == "accessory" else "sneakers",
          p.price, round(p.price * p.cost_ratio, 2), p.tag, p.unit_label) for i, p in enumerate(CATALOG)],
    )
    cur.executemany(
        "INSERT INTO variants (id, product_id, size, size_rank, color, sku) VALUES (?,?,?,?,?,?)",
        [(i + 1, v["pidx"] + 1, v["size"], v["rank"], CATALOG[v["pidx"]].color or "Standard", v["sku"])
         for i, v in enumerate(variants)],
    )

    # Calendar
    cal_rows = [(e.date.isoformat(), e.name, json.dumps(e.uplift), e.window_before, e.window_after, e.shape)
                for e in events]
    cur.executemany("INSERT INTO calendar_events (date, name, category_uplift, window_before, window_after, shape) "
                    "VALUES (?,?,?,?,?,?)", cal_rows)

    # Campaigns + size mix
    camp_rows, mix_rows = [], []
    for ci, c in enumerate(CAMPAIGNS):
        pi = pkey[(c.model, c.color)]
        p = CATALOG[pi]
        active_idx = np.nonzero(camp_active[ci])[0]
        started = hour_ts[active_idx[0]] if len(active_idx) else start
        status = "active" if c.end_days_ago is None else "paused"
        paused = hour_ts[active_idx[-1]] + timedelta(hours=1) if status == "paused" else None
        camp_rows.append((ci + 1, c.platform, c.name, pi + 1, c.daily_budget, c.bid, status, _fmt(started),
                          _fmt(paused) if paused else None,
                          f"customers/5550001234/campaigns/{9100 + ci}" if c.platform == "google_ads"
                          else f"act_7788001/campaigns/{23850000 + ci}"))
        mix = c.size_mix
        if mix is None:
            tilt = np.array(p.shares) ** 0.85  # clicks skew slightly more to popular sizes than sales do
            if p.kind == "footwear":
                tilt = tilt * np.array([0.9, 0.95, 1.08, 1.1, 0.95, 0.9])
            tilt = tilt / tilt.sum()
            mix = {s: round(float(x), 4) for s, x in zip(p.sizes, tilt)}
        for size, share in mix.items():
            mix_rows.append((ci + 1, size, share))
    cur.executemany("INSERT INTO campaigns (id, platform, name, product_id, daily_budget, bid, status, started_at, "
                    "paused_at, external_ref) VALUES (?,?,?,?,?,?,?,?,?,?)", camp_rows)
    cur.executemany("INSERT INTO campaign_size_mix (campaign_id, size, click_share) VALUES (?,?,?)", mix_rows)
    camp_mix = {}
    for cid, size, share in mix_rows:
        camp_mix.setdefault(cid - 1, {})[size] = share

    # Orders, order items, sale movements (one order per sold unit; socks sometimes bought as 2)
    hh, vv = np.nonzero(sales)
    counts = sales[hh, vv]
    hh = np.repeat(hh, counts)
    vv = np.repeat(vv, counts)
    n = len(hh)
    minute = rng.integers(0, 60, n)
    second = rng.integers(0, 60, n)
    order_sort = np.lexsort((second, minute, hh))
    hh, vv, minute, second = hh[order_sort], vv[order_sort], minute[order_sort], second[order_sort]
    city_idx = rng.choice(len(CITIES), size=n, p=CITY_W)
    chan_idx = rng.choice(len(CHANNELS), size=n, p=CHANNEL_W)
    sale_days = {d for d in days if is_sale_day(events, d)}

    order_rows, item_rows, mov_rows = [], [], []
    order_id = 0
    i = 0
    while i < n:
        h, vi = int(hh[i]), int(vv[i])
        qty = 1
        p = CATALOG[variants[vi]["pidx"]]
        if p.kind == "socks" and i + 1 < n and hh[i + 1] == h and vv[i + 1] == vi and rng.random() < 0.5:
            qty = 2
        ts = hour_ts[h].replace(minute=int(minute[i]), second=int(second[i]))
        disc = 1.0
        if ts.date() in sale_days:
            disc = {"slow": 0.7, "core": 0.85}.get(p.group, 1.0)
        unit_price = round(p.price * disc, 0)
        order_id += 1
        ts_s = _fmt(ts)
        order_rows.append((order_id, ts_s, CHANNELS[chan_idx[i]], CITIES[city_idx[i]], unit_price * qty))
        item_rows.append((order_id, ts_s, vi + 1, qty, unit_price))
        mov_rows.append((ts_s, vi + 1, "sale", qty, f"order:{order_id}"))
        i += qty

    # Restocks (opening balance + arrivals) and returns
    for vi in range(V):
        if opening[vi] > 0:
            mov_rows.append((_fmt(start), vi + 1, "restock", int(opening[vi]), "opening_balance"))
    for h, vi, q in arrivals:
        mov_rows.append((_fmt(hour_ts[h]), vi + 1, "restock", q, f"po:{h}-{vi}"))
    rh, rv = np.nonzero(returns)
    for h, vi in zip(rh, rv):
        mov_rows.append((_fmt(hour_ts[h]), int(vi) + 1, "return", int(returns[h, vi]), "rma"))
    # The simulation books deliveries and returns at the start of the hour, before that hour's sales.
    type_order = {"restock": 0, "return": 1, "sale": 2}
    mov_rows.sort(key=lambda r: (r[0], type_order[r[2]]))

    cur.executemany("INSERT INTO orders (id, ts, channel, city, total) VALUES (?,?,?,?,?)", order_rows)
    cur.executemany("INSERT INTO order_items (order_id, ts, variant_id, qty, price) VALUES (?,?,?,?,?)", item_rows)
    cur.executemany("INSERT INTO inventory_movements (ts, variant_id, type, qty, ref) VALUES (?,?,?,?,?)", mov_rows)

    # Hourly ad metrics for the last 30 days
    ad_rows = []
    ad_curve = AD_CURVE
    for ci, c in enumerate(CAMPAIGNS):
        pi = pkey[(c.model, c.color)]
        p_vis = [vi for vi, v in enumerate(variants) if v["pidx"] == pi]
        mix = camp_mix[ci]
        for h in range(ad_start_h, H):
            if not camp_active[ci, h]:
                continue
            d = h_day[h]
            fest_f = fest[CATALOG[pi].tag][d] ** 0.25
            spend = c.daily_budget * ad_curve[h_hod[h]] * float(rng.lognormal(0, 0.15))
            cpc = round(c.bid * float(rng.uniform(0.7, 1.0)) * fest_f, 2)
            clicks = int(round(spend / cpc))
            spend = round(clicks * cpc, 2)
            ctr = float(rng.uniform(0.012, 0.03))
            impressions = int(round(clicks / ctr)) if clicks else int(rng.integers(20, 80))
            stock_prev = stock_end[h - 1] if h > 0 else opening
            in_stock_share = sum(mix.get(variants[vi]["size"], 0.0) for vi in p_vis if stock_prev[vi] > 0)
            conv = int(rng.binomial(clicks, min(1.0, c.cvr * in_stock_share))) if clicks else 0
            oos_share = max(0.0, 1.0 - in_stock_share)
            ad_rows.append((_fmt(hour_ts[h]), ci + 1, impressions, clicks, cpc, spend, conv,
                            int(round(clicks * oos_share)), round(spend * oos_share, 2)))
    cur.executemany("INSERT INTO ad_metrics_hourly (ts, campaign_id, impressions, clicks, cpc, spend, conversions, "
                    "wasted_clicks, wasted_spend) VALUES (?,?,?,?,?,?,?,?,?)", ad_rows)
    ad_lookup = {(r[1] - 1, r[0]): r for r in ad_rows}

    # Daily site events for the last 30 days
    site_rows = []
    lost = demand - sales
    for d in range(n_days - AD_HISTORY_DAYS, n_days):
        mask = h_day == d
        if not mask.any():
            continue
        s_day = sales[mask].sum(axis=0)
        l_day = lost[mask].sum(axis=0)
        views = rng.poisson(s_day * 22 + l_day * 4 + 3)
        atc = np.minimum(views, rng.poisson(s_day * 2.3 + 0.5))
        oos_views = rng.poisson(l_day * 20)
        day_ts = _fmt(datetime.combine(days[d], datetime.min.time()))
        for vi in range(V):
            site_rows.append((day_ts, vi + 1, int(views[vi]), int(atc[vi]), int(oos_views[vi])))
    cur.executemany("INSERT INTO site_events (ts, variant_id, views, add_to_cart, oos_page_views) VALUES (?,?,?,?,?)",
                    site_rows)

    # Current inventory. A prepared brand has festive purchase orders in transit for most products;
    # the hero sneakers and the gift box are deliberately under-prepared for Diwali (Opportunity alerts).
    inv_rows = []
    fw_pi = pkey[("Festive White Sneaker", "White/Gold")]
    future = [anchor.date() + timedelta(days=i) for i in range(FESTIVE_PLANNING_DAYS)]
    for vi, v in enumerate(variants):
        p = CATALOG[v["pidx"]]
        lead = int(v_lead[vi])
        rop = int(math.ceil(lead * exp_now[vi] * 1.2))
        in_transit, eta = 0, None
        if p.group != "slow" and (p.model, p.color) not in UNDERPREPARED_FOR_FESTIVAL:
            projected = sum(base[v["pidx"]] * trend[-1] * festival_multiplier(events, d, p.tag) * camp_day[v["pidx"], -1]
                            for d in future) * v_share[vi]
            need = int(math.ceil(projected * rng.uniform(1.1, 1.3))) - int(final[vi])
            if need > 0:
                in_transit = need
                eta = (anchor + timedelta(days=int(rng.integers(3, min(lead, 9) + 1)))).replace(hour=10)
        inv_rows.append((vi + 1, int(final[vi]), 0, in_transit, rop, lead, _fmt(eta) if eta else None, _fmt(anchor)))
    cur.executemany("INSERT INTO inventory (variant_id, on_hand, reserved, in_transit, reorder_point, lead_time_days, "
                    "restock_eta, updated_at) VALUES (?,?,?,?,?,?,?,?)", inv_rows)

    # Hidden ground truth (engine never reads this table)
    gt_rows = []
    for vi, ts_h, r_h in planted:
        first_zero = ts_h  # the forced sale in hour ts_h-1 sells the last unit, so the shelf is empty from ts_h
        pi = v_pidx[vi]
        camp_ids = [ci for ci, c in enumerate(CAMPAIGNS) if pkey[(c.model, c.color)] == pi]
        ci = camp_ids[0] if camp_ids else None
        w_clicks, w_spend = 0, 0.0
        if ci is not None:
            share = camp_mix[ci].get(variants[vi]["size"], 0.0)
            for h in range(first_zero, r_h):
                row = ad_lookup.get((ci, _fmt(hour_ts[h])))
                if row:
                    w_clicks += row[3] * share
                    w_spend += row[5] * share
        gt_rows.append((vi + 1, ci + 1 if ci is not None else None, "planted_stockout", _fmt(hour_ts[first_zero]),
                        _fmt(hour_ts[r_h]), int(demand[ts_h:r_h, vi].sum()), int(round(w_clicks)),
                        round(w_spend, 2)))
    cur.executemany("INSERT INTO ground_truth (variant_id, campaign_id, kind, stockout_ts, restock_ts, lost_units, "
                    "wasted_clicks, wasted_spend) VALUES (?,?,?,?,?,?,?,?)", gt_rows)

    meta = {"anchor": anchor.isoformat(), "seed": str(seed), "history_days": str(HISTORY_DAYS),
            "generated_at": datetime.now().isoformat(timespec="seconds"), "data_label": "synthetic sample data"}
    cur.executemany("INSERT INTO app_meta (key, value) VALUES (?,?)", list(meta.items()))
    raw.commit()
    cur.close()
    raw.close()
    engine.dispose()

    fw_vis = [vi for vi, v in enumerate(variants) if v["pidx"] == fw_pi]
    summary = {
        "db": str(path),
        "anchor": anchor.isoformat(),
        "products": P,
        "variants": V,
        "orders": len(order_rows),
        "units_sold": int(sales.sum()),
        "revenue_inr": float(sum(r[4] for r in order_rows)),
        "movements": len(mov_rows),
        "campaigns": len(CAMPAIGNS),
        "ad_metric_rows": len(ad_rows),
        "site_event_rows": len(site_rows),
        "planted_stockouts": len(gt_rows),
        "festive_white_stock": {variants[vi]["size"]: int(final[vi]) for vi in fw_vis},
        "seconds": round(time.time() - t0, 1),
    }
    if not quiet:
        _print_summary(summary)
    return summary


def _inr(x: float) -> str:
    """Indian digit grouping: 12,34,567."""
    s = f"{int(round(x))}"
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def _print_summary(s: dict) -> None:
    print("SignalShift.ai - synthetic data for Solenest (SAMPLE DATA, not real benchmarks)")
    print(f"  database          : {s['db']}")
    print(f"  anchor ('now')    : {s['anchor']}  (history: {HISTORY_DAYS} days)")
    print(f"  catalog           : {s['products']} products, {s['variants']} size variants")
    print(f"  orders            : {_inr(s['orders'])} ({_inr(s['units_sold'])} units, Rs {_inr(s['revenue_inr'])})")
    print(f"  stock movements   : {_inr(s['movements'])}")
    print(f"  campaigns         : {s['campaigns']}  ({_inr(s['ad_metric_rows'])} hourly ad-metric rows)")
    print(f"  site event rows   : {_inr(s['site_event_rows'])}")
    print(f"  planted stockouts : {s['planted_stockouts']} (hidden in ground_truth)")
    fw = s["festive_white_stock"]
    print(f"  demo state        : Festive White Sneaker {sum(fw.values())} pairs total -> "
          + ", ".join(f"{k}: {v}" for k, v in fw.items()))
    print(f"  done in {s['seconds']}s")


def main() -> None:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="delete and rebuild the database")
    ap.add_argument("--seed", type=int, default=settings.seed)
    ap.add_argument("--anchor", type=str, default=None, help="'now' for the demo, e.g. 2026-10-15T10:00 (default: current hour)")
    ap.add_argument("--db", type=str, default=settings.database_path)
    ap.add_argument("--calendar", type=str, default=settings.calendar_file)
    args = ap.parse_args()
    anchor = (datetime.fromisoformat(args.anchor).replace(minute=0, second=0, microsecond=0)
              if args.anchor else default_anchor(settings.timezone))
    generate(args.db, args.seed, anchor, args.calendar, args.reset)


if __name__ == "__main__":
    main()
