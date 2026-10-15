"""Counterfactual replay: run SignalShift's rules hour by hour over the synthetic history and compare
"without SignalShift" (what actually happened: ads kept running through stockouts) with "with SignalShift"
(ads paused after the first Critical alert plus an owner approval delay; stockouts counted as prevented when a
warning came at least one lead time ahead).

* Uses the SAME pure functions as the live engine (ewma_rate, seasonal_uplift, hours_of_cover, classify_severity,
  surge rule) with the learned forecast factors.
* Stock at each past hour is reconstructed backwards from the movement ledger.
* The hidden `ground_truth` table is read ONLY here, to score detection (precision / recall) - never by the engine.

Honest caveats (also returned in the result): factors are learned once on the full history (mild look-ahead);
historical purchase orders are not modelled, so the restock deadline is lead time + buffer; savings are bounded
by the planted episodes in a 30-day synthetic window.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Database, sql_ts
from app.models import AppMeta, Campaign, Inventory, Product, Variant
from app.services.calendar import CalendarEventDef
from app.services.engine import classify_severity, hours_of_cover
from app.services.forecast import DEFAULT_CAMPAIGN_FACTOR, load_factors, seasonal_uplift


@dataclass
class ReplayConfig:
    days: int = 30
    approval_delay_hours: float = 1.0  # owner reads the alert and taps Approve
    detection_lookback_hours: int = 72  # a Critical up to this long before a stockout counts as a detection
    episode_merge_hours: int = 6  # alert episodes closer than this are one alert (cooldown)
    match_window_hours: int = 48  # a Critical is a true positive if a stockout follows within this window


def _hour_index(s: Session, sql: str, params: dict) -> list[tuple]:
    return list(s.execute(text(sql), params))


def run_replay(db: Database, settings: Settings, events: list[CalendarEventDef],
               cfg: ReplayConfig | None = None) -> dict:
    cfg = cfg or ReplayConfig()
    with db.session() as s:
        anchor_raw = s.scalar(select(AppMeta.value).where(AppMeta.key == "anchor"))
        if not anchor_raw:
            raise RuntimeError("no data anchor found - generate the data first")
        end = datetime.fromisoformat(anchor_raw)
        start = end - timedelta(days=cfg.days)
        lookback = settings.velocity_lookback_days * 24
        H = cfg.days * 24
        hist_start = start - timedelta(hours=lookback)
        factors = load_factors(s)
        scales = factors.get("seasonal_scale", {})
        cfac = factors.get("campaign", {})
        mix = factors.get("size_mix", {})

        campaigns = [c for c in s.scalars(select(Campaign))
                     if c.started_at < end and (c.paused_at is None or c.paused_at > start or c.status == "active")]
        product_ids = sorted({c.product_id for c in campaigns})
        products = {p.id: p for p in s.scalars(select(Product).where(Product.id.in_(product_ids)))}
        variants = list(s.scalars(select(Variant).where(Variant.product_id.in_(product_ids))))
        inv = {i.variant_id: i for i in s.scalars(select(Inventory).where(Inventory.variant_id.in_([v.id for v in variants])))}
        vids = [v.id for v in variants]
        vidx = {vid: i for i, vid in enumerate(vids)}
        V = len(vids)

        # hourly sales per variant over [hist_start, end)
        total_hours = lookback + H
        sales = np.zeros((V, total_hours))
        for vid, h, q in _hour_index(s, (
                "SELECT variant_id, CAST((julianday(ts) - julianday(:a)) * 24 + 1e-6 AS INTEGER), SUM(qty) "
                f"FROM order_items WHERE ts >= :a AND ts < :b AND variant_id IN ({','.join(map(str, vids))}) GROUP BY 1, 2"),
                {"a": sql_ts(hist_start), "b": sql_ts(end)}):
            if 0 <= h < total_hours:
                sales[vidx[vid], h] = q

        # stock reconstruction: net stock change per hour in the window, and everything after the anchor
        delta = np.zeros((V, H))
        after = np.zeros(V)
        for vid, h, d in _hour_index(s, (
                "SELECT variant_id, CAST((julianday(ts) - julianday(:a)) * 24 + 1e-6 AS INTEGER), "
                "SUM(CASE WHEN type = 'sale' THEN -qty ELSE qty END) FROM inventory_movements "
                f"WHERE ts >= :a AND variant_id IN ({','.join(map(str, vids))}) GROUP BY 1, 2"), {"a": sql_ts(start)}):
            if h >= H:
                after[vidx[vid]] += d
            elif h >= 0:
                delta[vidx[vid], h] = d
        at_anchor = np.array([inv[vid].on_hand for vid in vids]) - after
        # stock at the START of hour h = stock at anchor minus every change from hour h onwards
        start_stock = at_anchor[:, None] - np.cumsum(delta[:, ::-1], axis=1)[:, ::-1]

        # hourly ad metrics in the window per campaign
        spend = {c.id: np.zeros(H) for c in campaigns}
        wasted = {c.id: np.zeros(H) for c in campaigns}
        for cid, h, sp, ws in _hour_index(s, (
                "SELECT campaign_id, CAST((julianday(ts) - julianday(:a)) * 24 + 1e-6 AS INTEGER), SUM(spend), "
                "SUM(wasted_spend) FROM ad_metrics_hourly WHERE ts >= :a AND ts < :b GROUP BY 1, 2"),
                {"a": sql_ts(start), "b": sql_ts(end)}):
            if cid in spend and 0 <= h < H:
                spend[cid][h], wasted[cid][h] = sp, ws

        gt = list(s.execute(text(
            "SELECT g.variant_id, g.stockout_ts, g.restock_ts, g.lost_units, v.sku, p.price, i.lead_time_days "
            "FROM ground_truth g JOIN variants v ON v.id = g.variant_id JOIN products p ON p.id = v.product_id "
            "JOIN inventory i ON i.variant_id = v.id")))

    def active(c: Campaign, t: datetime) -> bool:
        return c.started_at <= t and (c.paused_at is None or t < c.paused_at)

    # --- replay the rules hour by hour ------------------------------------------------
    w = 0.5 ** (np.arange(lookback - 1, -1, -1) / settings.velocity_half_life_hours)
    w /= w.sum()
    severity = np.zeros((V, H), dtype=np.int8)  # 0 none, 2 warning, 3 critical
    for pid in product_ids:
        p = products[pid]
        tag, scale = p.seasonality_tag, scales.get(p.seasonality_tag, 1.0)
        pcamps = [c for c in campaigns if c.product_id == pid]
        rows = [vidx[v.id] for v in variants if v.product_id == pid]
        hours = [hist_start + timedelta(hours=i) for i in range(total_hours)]
        seas = {d: seasonal_uplift(events, d, tag, scale) for d in {t.date() for t in hours}}
        camp_mult = np.array([math.prod(cfac.get(str(c.id), DEFAULT_CAMPAIGN_FACTOR) for c in pcamps if active(c, t))
                              for t in hours])
        norm = sales[rows].sum(axis=0) / (np.array([seas[t.date()] for t in hours]) * camp_mult)
        for h in range(H):
            t = start + timedelta(hours=h)
            base = float(np.dot(w, norm[h:h + lookback]))
            camp_live = any(active(c, t) for c in pcamps)
            per_hr_product = base * seas.get(t.date(), seasonal_uplift(events, t.date(), tag, scale)) * camp_mult[lookback + h]
            for r in rows:
                vid = vids[r]
                demand = per_hr_product * mix.get(str(vid), 1 / len(rows))
                recent = sales[r, lookback + h - settings.surge_window_minutes // 60:lookback + h].sum()
                rate = recent / (settings.surge_window_minutes / 60)
                if recent >= settings.surge_min_units and rate > settings.surge_multiplier * demand:
                    demand = (demand + rate) / 2
                avail = int(start_stock[r, h])
                cover = hours_of_cover(avail, demand)
                eta = t + timedelta(hours=cover) if cover is not None else None
                deadline = t + timedelta(days=inv[vid].lead_time_days + settings.warning_buffer_days)
                sev, _ = classify_severity(cover, camp_live, eta, deadline, settings.ad_pause_lag_hours)
                severity[r, h] = {"critical": 3, "warning": 2}.get(sev or "", 0)

    # --- score against the hidden ground truth --------------------------------------
    def hidx(ts: str) -> int:
        return int((datetime.fromisoformat(ts) - start).total_seconds() // 3600)

    episodes: list[tuple[int, int, int]] = []  # (row, start_h, end_h) of Critical alerts
    for r in range(V):
        hs = np.nonzero(severity[r] == 3)[0]
        if len(hs) == 0:
            continue
        s0 = prev = hs[0]
        for h in hs[1:]:
            if h - prev > cfg.episode_merge_hours:
                episodes.append((r, s0, prev))
                s0 = h
            prev = h
        episodes.append((r, s0, prev))

    gt_rows, paused = [], {pid: np.zeros(H, dtype=bool) for pid in product_ids}
    tp_episodes: set[int] = set()
    lost_baseline = protected = 0.0
    prevented = detected = early = 0
    for vid, so_ts, re_ts, lost_units, sku, price, lead_days in gt:
        r = vidx.get(vid)
        h_s, h_r = hidx(so_ts), hidx(re_ts)
        lost_baseline += lost_units * price
        row = {"sku": sku, "stockout_ts": so_ts[:16], "restock_ts": re_ts[:16], "lost_units": int(lost_units),
               "first_critical": None, "critical_lead_hours": None, "warning_lead_days": None, "preventable": False}
        if r is not None and 0 <= h_s < H:
            lo = max(0, h_s - cfg.detection_lookback_hours)
            crit = np.nonzero(severity[r, lo:h_s + 1] == 3)[0]
            if len(crit):
                h_det = lo + int(crit[0])
                detected += 1
                early += h_det < h_s
                row["first_critical"] = (start + timedelta(hours=h_det)).isoformat(timespec="minutes")
                row["critical_lead_hours"] = h_s - h_det
                t_eff = int(math.ceil(h_det + cfg.approval_delay_hours))
                paused[variants[r].product_id][max(t_eff, h_s):min(h_r, H)] = True
                for i, (er, es, _ee) in enumerate(episodes):
                    if er == r and es <= h_s and es >= lo:
                        tp_episodes.add(i)
            # warning lead: start of the at-risk run (warning or critical) that leads into the stockout
            h = h_s
            while h - 1 >= 0 and severity[r, h - 1] >= 2:
                h -= 1
            if severity[r, max(h_s - 1, 0)] >= 2 or severity[r, h_s] >= 2:
                lead_days_seen = (h_s - h) / 24
                row["warning_lead_days"] = round(lead_days_seen, 1)
                row["preventable"] = lead_days_seen >= lead_days
                if row["preventable"]:
                    prevented += 1
                    protected += lost_units * price
        gt_rows.append(row)
    for i, (er, es, _ee) in enumerate(episodes):  # TP also if the alert fired while the stockout was under way
        for vid, so_ts, re_ts, *_ in gt:
            if vidx.get(vid) == er and hidx(so_ts) - cfg.match_window_hours <= es <= hidx(re_ts):
                tp_episodes.add(i)

    waste_by_product = {pid: sum(wasted[c.id] for c in campaigns if c.product_id == pid) for pid in product_ids}
    waste_without = float(sum(float(np.sum(arr)) for arr in waste_by_product.values()))
    waste_avoided = float(sum(float(np.sum(waste_by_product[pid] * paused[pid])) for pid in product_ids))
    total_spend = float(sum(float(np.sum(a)) for a in spend.values()))
    n_gt, n_ep = len(gt_rows), len(episodes)
    warn_leads = [g["warning_lead_days"] for g in gt_rows if g["warning_lead_days"]]
    return {
        "window": {"start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes"),
                   "days": cfg.days, "variants_replayed": V, "hours": H},
        "detection": {
            "ground_truth_stockouts": n_gt, "detected": detected, "detected_before_stockout": int(early),
            "recall": round(detected / n_gt, 3) if n_gt else None,
            "critical_alert_episodes": n_ep, "true_positive_episodes": len(tp_episodes),
            "precision": round(len(tp_episodes) / n_ep, 3) if n_ep else None,
            "warned_in_advance": len(warn_leads),
            "median_warning_lead_days": float(np.median(warn_leads)) if warn_leads else None,
            "note": ("Critical = stockout before ads can be paused; for slow-selling sizes that is only true at the "
                     "stockout itself, so the early signal is the Warning (runs out before restock)."),
        },
        "without_signalshift": {"ad_spend_total": round(total_spend), "wasted_on_out_of_stock_clicks": round(waste_without),
                                "stockouts": n_gt, "lost_sales": round(lost_baseline)},
        "with_signalshift": {"wasted_on_out_of_stock_clicks": round(waste_without - waste_avoided),
                             "ad_spend_saved": round(waste_avoided), "stockouts_prevented": prevented,
                             "sales_protected": round(protected)},
        "ground_truth": gt_rows,
        "assumptions": [
            f"Ads pause {cfg.approval_delay_hours:g} h after the first Critical alert (owner approval delay).",
            "A stockout counts as prevented only if a warning was raised at least one lead time before it.",
            "Forecast factors are learned once on the full history (mild look-ahead).",
            "Historical purchase orders are not modelled: the restock deadline is lead time + buffer.",
            "Synthetic sample data: these are not real-world benchmarks.",
        ],
    }
