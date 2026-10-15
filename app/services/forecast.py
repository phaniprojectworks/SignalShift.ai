"""Demand forecast: weighted velocity x learned seasonal/campaign factors x size mix, with a confidence band.

    demand_per_hr = base_velocity * seasonal_uplift * campaign_uplift * size_mix_factor

- base_velocity: product-level EWMA of hourly sales over the lookback, after dividing each hour by
  the seasonal and campaign factors in force at that hour (so they are not double-counted).
- seasonal_uplift: calendar multiplier for today, scaled by a per-tag factor learned from past festivals.
- campaign_uplift: product of learned factors of the product's currently active campaigns.
- size_mix_factor: the variant's share of product sales (last 90 days, smoothed toward the 2-year share).

Pure functions sit at the top; `recalibrate()` and `DemandModel` do the database work.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import sql_ts
from app.models import Campaign, ForecastFactor, Product, Variant
from app.services.calendar import CalendarEventDef, festival_multiplier

log = logging.getLogger("signalshift.forecast")

DEFAULT_CAMPAIGN_FACTOR = 1.2
BAND_MIN, BAND_MAX = 0.15, 0.5


# --- pure functions ----------------------------------------------------------------

def ewma_rate(counts: Sequence[float], half_life: float) -> float:
    """Exponentially weighted mean of an hourly series ordered oldest -> newest."""
    n = len(counts)
    if n == 0:
        return 0.0
    ages = np.arange(n - 1, -1, -1, dtype=float)
    w = 0.5 ** (ages / half_life)
    return float(np.dot(w, np.asarray(counts, dtype=float)) / w.sum())


def band_pct(daily_values: Sequence[float]) -> float:
    """Relative +/- band from day-to-day variation (coefficient of variation), clamped."""
    arr = np.asarray(daily_values, dtype=float)
    if len(arr) < 3 or arr.mean() <= 0:
        return BAND_MAX
    return float(min(BAND_MAX, max(BAND_MIN, arr.std(ddof=1) / arr.mean())))


def seasonal_uplift(events: list[CalendarEventDef], day: date, tag: str, scale: float = 1.0) -> float:
    return 1.0 + (festival_multiplier(events, day, tag) - 1.0) * scale


def projected_daily_demand(base_daily: float, start: date, n_days: int, tag: str, events: list[CalendarEventDef],
                           scale: float, campaign_uplift: float, share: float) -> list[float]:
    return [base_daily * seasonal_uplift(events, start + timedelta(days=i), tag, scale) * campaign_uplift * share
            for i in range(n_days)]


def cover_days(supply: float, daily_demand: Sequence[float]) -> float:
    """Days until cumulative projected demand exceeds supply (fractional; len(daily_demand) if never)."""
    remaining = supply
    for i, d in enumerate(daily_demand):
        if d <= 0:
            continue
        if remaining < d:
            return i + max(remaining, 0) / d
        remaining -= d
    return float(len(daily_demand))


def fit_scale(x: Sequence[float], y: Sequence[float], lo: float = 0.3, hi: float = 2.0) -> float:
    """Least-squares slope through the origin: y ~ scale * x (x = calendar uplift - 1, y = observed ratio - 1)."""
    xa, ya = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    denom = float(np.dot(xa, xa))
    if denom < 1e-6:
        return 1.0
    return float(min(hi, max(lo, np.dot(xa, ya) / denom)))


def fit_campaign_factors(log_units: Sequence[float], active: np.ndarray, lo: float = 1.0, hi: float = 3.0
                         ) -> list[float | None]:
    """OLS of log(daily units / seasonal) on per-campaign active fractions. None = not identifiable."""
    y = np.asarray(log_units, dtype=float)
    n_c = active.shape[1]
    identifiable = [float(active[:, j].std()) > 0.05 for j in range(n_c)]
    cols = [j for j in range(n_c) if identifiable[j]]
    out: list[float | None] = [None] * n_c
    if not cols:
        return out
    X = np.column_stack([np.ones(len(y)), active[:, cols]])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    for k, j in enumerate(cols):
        out[j] = float(min(hi, max(lo, math.exp(beta[k + 1]))))
    return out


# --- calibration (nightly) ----------------------------------------------------------

def _active_fraction_by_day(c: Campaign, days: list[date]) -> np.ndarray:
    out = np.zeros(len(days))
    start = c.started_at
    end = c.paused_at if c.status == "paused" and c.paused_at else None
    for i, d in enumerate(days):
        d0 = datetime.combine(d, datetime.min.time())
        d1 = d0 + timedelta(days=1)
        lo = max(d0, start)
        hi = min(d1, end) if end else d1
        if hi > lo:
            out[i] = (hi - lo).total_seconds() / 86400
    return out


def recalibrate(s: Session, events: list[CalendarEventDef], now: datetime) -> dict[str, float]:
    """Learn seasonal scales, campaign factors and size mix from history; store in forecast_factors."""
    rows: list[ForecastFactor] = []
    today = now.date()

    # 1) seasonal scale per tag, from completed festivals not overlapping recent (campaign-affected) weeks
    daily = pd.read_sql(text(
        "SELECT substr(oi.ts, 1, 10) AS d, p.seasonality_tag AS tag, SUM(oi.qty) AS units "
        "FROM order_items oi JOIN variants v ON v.id = oi.variant_id JOIN products p ON p.id = v.product_id "
        "WHERE oi.ts < :now GROUP BY 1, 2"), s.connection(), params={"now": sql_ts(now)})
    daily["d"] = pd.to_datetime(daily["d"]).dt.date
    in_any_event = {e.start + timedelta(days=i) for e in events for i in range((e.end - e.start).days + 1)}
    cutoff = today - timedelta(days=35)
    for tag, grp in daily.groupby("tag"):
        series = grp.set_index("d")["units"]
        xs, ys = [], []
        for ev in events:
            if ev.end >= cutoff or ev.start < series.index.min() + timedelta(days=28):
                continue
            base_days = [ev.start - timedelta(days=i) for i in range(1, 29)]
            base_vals = [series.get(d, 0) for d in base_days if d not in in_any_event]
            if len(base_vals) < 10 or np.mean(base_vals) <= 0:
                continue
            b = float(np.mean(base_vals))
            for i in range((ev.end - ev.start).days + 1):
                d = ev.start + timedelta(days=i)
                xs.append(festival_multiplier(events, d, str(tag)) - 1.0)
                ys.append(series.get(d, 0) / b - 1.0)
        scale = fit_scale(xs, ys)
        rows.append(ForecastFactor(factor_type="seasonal_scale", key=str(tag), value=round(scale, 4),
                                   sample_size=len(xs), updated_at=now,
                                   note="observed festive lift / calendar lift (1.0 = calendar is right)"))
    scales = {r.key: r.value for r in rows}

    # 2) campaign factors per product, from the last 60 days
    days = [today - timedelta(days=i) for i in range(60, 0, -1)]
    campaigns = s.scalars(select(Campaign)).all()
    by_product: dict[int, list[Campaign]] = {}
    for c in campaigns:
        by_product.setdefault(c.product_id, []).append(c)
    for pid, camps in by_product.items():
        tag = s.scalar(select(Product.seasonality_tag).where(Product.id == pid)) or "core"
        units = dict(s.execute(text(
            "SELECT substr(oi.ts, 1, 10), SUM(oi.qty) FROM order_items oi JOIN variants v ON v.id = oi.variant_id "
            "WHERE v.product_id = :pid AND oi.ts >= :a AND oi.ts < :b GROUP BY 1"),
            {"pid": pid, "a": sql_ts(datetime.combine(days[0], datetime.min.time())),
             "b": sql_ts(datetime.combine(today, datetime.min.time()))}).all())
        y = [math.log(units.get(d.isoformat(), 0) + 0.5) - math.log(seasonal_uplift(events, d, tag, scales.get(tag, 1.0)))
             for d in days]
        active = np.column_stack([_active_fraction_by_day(c, days) for c in camps])
        factors = fit_campaign_factors(y, active)
        for c, f in zip(camps, factors):
            rows.append(ForecastFactor(
                factor_type="campaign", key=str(c.id), value=round(f if f is not None else DEFAULT_CAMPAIGN_FACTOR, 4),
                sample_size=len(days), updated_at=now,
                note="learned: sales ratio on campaign days vs non-campaign days" if f is not None
                else "default: campaign has no on/off contrast in the last 60 days"))

    # 3) size mix per variant (90-day share, smoothed toward the 2-year share with 20 pseudo-units)
    mix = pd.read_sql(text(
        "SELECT v.id AS variant_id, v.product_id, "
        "SUM(CASE WHEN oi.ts >= :recent THEN oi.qty ELSE 0 END) AS recent, COALESCE(SUM(oi.qty), 0) AS total "
        "FROM variants v LEFT JOIN order_items oi ON oi.variant_id = v.id AND oi.ts < :now GROUP BY v.id"),
        s.connection(), params={"recent": sql_ts(now - timedelta(days=90)), "now": sql_ts(now)}).fillna(0)
    for pid, grp in mix.groupby("product_id"):
        n = len(grp)
        tot = grp["total"].sum()
        prior = grp["total"] / tot if tot > 0 else pd.Series(1.0 / n, index=grp.index)
        smoothed = (grp["recent"] + 20 * prior) / (grp["recent"].sum() + 20)
        for vid, share in zip(grp["variant_id"], smoothed):
            rows.append(ForecastFactor(factor_type="size_mix", key=str(int(vid)), value=round(float(share), 5),
                                       sample_size=int(grp["recent"].sum()), updated_at=now,
                                       note="share of product unit sales, last 90 days (smoothed)"))

    s.execute(delete(ForecastFactor))
    s.add_all(rows)
    s.flush()
    summary = {f"seasonal_scale:{k}": v for k, v in scales.items()}
    summary.update({f"campaign:{r.key}": r.value for r in rows if r.factor_type == "campaign"})
    log.info("recalibrated forecast factors", extra={"factors": summary, "rows": len(rows)})
    return summary


def load_factors(s: Session) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for f in s.scalars(select(ForecastFactor)):
        out.setdefault(f.factor_type, {})[f.key] = f.value
    return out


# --- runtime forecast ------------------------------------------------------------

@dataclass
class ProductForecast:
    product_id: int
    tag: str
    base_velocity_per_hr: float
    seasonal_uplift: float
    campaign_uplift: float
    band_pct: float
    seasonal_scale: float

    @property
    def product_demand_per_hr(self) -> float:
        return self.base_velocity_per_hr * self.seasonal_uplift * self.campaign_uplift


@dataclass
class VariantForecast:
    variant_id: int
    size_mix_factor: float
    demand_per_hr: float
    demand_low: float
    demand_high: float
    recent_rate_per_hr: float
    recent_units: int
    surge: bool


class DemandModel:
    """Computes forecasts against the database for one evaluation pass (caches per product)."""

    def __init__(self, s: Session, settings: Settings, events: list[CalendarEventDef], now: datetime) -> None:
        self.s, self.settings, self.events, self.now = s, settings, events, now
        self.factors = load_factors(s)
        self._products: dict[int, ProductForecast] = {}
        self._recent: dict[int, dict[int, int]] = {}
        self._seasonal_cache: dict[tuple[date, str, float], float] = {}
        self._campaigns: list[Campaign] | None = None

    def campaigns(self) -> list[Campaign]:
        if self._campaigns is None:
            self._campaigns = list(self.s.scalars(select(Campaign)))
        return self._campaigns

    def campaign_factor(self, campaign_id: int) -> float:
        return self.factors.get("campaign", {}).get(str(campaign_id), DEFAULT_CAMPAIGN_FACTOR)

    def seasonal_scale(self, tag: str) -> float:
        return self.factors.get("seasonal_scale", {}).get(tag, 1.0)

    def size_share(self, variant_id: int, fallback: float) -> float:
        return self.factors.get("size_mix", {}).get(str(variant_id), fallback)

    def active_campaigns(self, product_id: int) -> list[Campaign]:
        return [c for c in self.campaigns() if c.product_id == product_id and c.status == "active"]

    def _seasonal(self, day: date, tag: str, scale: float) -> float:
        key = (day, tag, scale)
        if key not in self._seasonal_cache:
            self._seasonal_cache[key] = seasonal_uplift(self.events, day, tag, scale)
        return self._seasonal_cache[key]

    def _campaign_mult_by_hour(self, product_id: int, start: datetime, hours: int) -> np.ndarray:
        """Product of learned factors of the campaigns live in each hour [start + i h]."""
        mult = np.ones(hours)
        for c in self.campaigns():
            if c.product_id != product_id:
                continue
            first = max(0, math.ceil((c.started_at - start).total_seconds() / 3600))
            last = hours
            if c.status == "paused" and c.paused_at:
                last = min(hours, max(0, math.ceil((c.paused_at - start).total_seconds() / 3600)))
            if first < last:
                mult[first:last] *= self.campaign_factor(c.id)
        return mult

    def product(self, product_id: int) -> ProductForecast:
        if product_id in self._products:
            return self._products[product_id]
        tag = self.s.scalar(select(Product.seasonality_tag).where(Product.id == product_id)) or "core"
        scale = self.seasonal_scale(tag)
        end = self.now.replace(minute=0, second=0, microsecond=0)  # exclude the partial current hour
        hours = self.settings.velocity_lookback_days * 24
        start = end - timedelta(hours=hours)
        counts = np.zeros(hours)
        for idx, qty in self.s.execute(text(  # SQLite computes the hour index (no per-row parsing in Python)
                "SELECT CAST((julianday(oi.ts) - julianday(:a)) * 24 + 1e-6 AS INTEGER) AS i, SUM(oi.qty) "
                "FROM order_items oi JOIN variants v ON v.id = oi.variant_id "
                "WHERE v.product_id = :pid AND oi.ts >= :a AND oi.ts < :b GROUP BY 1"),
                {"pid": product_id, "a": sql_ts(start), "b": sql_ts(end)}):
            if 0 <= idx < hours:
                counts[idx] = qty
        seasonal = np.array([self._seasonal(start.date() + timedelta(days=d), tag, scale)
                             for d in range(hours // 24 + 2)])
        day_of_hour = (start.hour + np.arange(hours)) // 24  # hour i falls on calendar day start.date() + this
        normalized = counts / (seasonal[day_of_hour] * self._campaign_mult_by_hour(product_id, start, hours))
        base = ewma_rate(normalized, self.settings.velocity_half_life_hours)
        daily_norm = [sum(normalized[i:i + 24]) for i in range(0, hours, 24)]
        camp_now = 1.0
        for c in self.active_campaigns(product_id):
            camp_now *= self.campaign_factor(c.id)
        pf = ProductForecast(product_id=product_id, tag=tag, base_velocity_per_hr=base,
                             seasonal_uplift=seasonal_uplift(self.events, self.now.date(), tag, scale),
                             campaign_uplift=camp_now, band_pct=band_pct(daily_norm), seasonal_scale=scale)
        self._products[product_id] = pf
        return pf

    def variant(self, variant: Variant, fallback_share: float) -> VariantForecast:
        pf = self.product(variant.product_id)
        share = self.size_share(variant.id, fallback_share)
        demand = pf.product_demand_per_hr * share
        window_h = self.settings.surge_window_minutes / 60
        recent_units = self._recent_units(variant.product_id).get(variant.id, 0)
        recent_rate = recent_units / window_h
        surge = recent_units >= self.settings.surge_min_units and recent_rate > self.settings.surge_multiplier * demand
        expected = (demand + recent_rate) / 2 if surge else demand
        low = demand * (1 - pf.band_pct)
        high = max(expected * (1 + pf.band_pct), recent_rate if surge else 0.0)
        return VariantForecast(variant_id=variant.id, size_mix_factor=share, demand_per_hr=expected, demand_low=low,
                               demand_high=high, recent_rate_per_hr=recent_rate, recent_units=recent_units, surge=surge)

    def _recent_units(self, product_id: int) -> dict[int, int]:
        """Units sold per variant of the product inside the surge window (one query per product)."""
        if product_id not in self._recent:
            self._recent[product_id] = {int(vid): int(q) for vid, q in self.s.execute(text(
                "SELECT oi.variant_id, SUM(oi.qty) FROM order_items oi JOIN variants v ON v.id = oi.variant_id "
                "WHERE v.product_id = :pid AND oi.ts >= :a AND oi.ts <= :b GROUP BY 1"),
                {"pid": product_id, "a": sql_ts(self.now - timedelta(minutes=self.settings.surge_window_minutes)),
                 "b": sql_ts(self.now)})}
        return self._recent[product_id]

    def projected_variant_daily(self, variant: Variant, share: float, n_days: int) -> list[float]:
        pf = self.product(variant.product_id)
        return projected_daily_demand(pf.base_velocity_per_hr * 24, self.now.date(), n_days, pf.tag, self.events,
                                      pf.seasonal_scale, pf.campaign_uplift, share)
