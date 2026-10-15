"""Rules engine: days of cover, severity, size-aware click share, wasted spend, budget shift, reorder, cooldown.

Pure functions first (unit-tested directly); `Engine` orchestrates them against the database,
persists alerts, logs every decision with its facts, and publishes `alert` events.
"""
from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from datetime import datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Database, sql_ts
from app.models import Alert, Campaign, CampaignSizeMix, Inventory, Product, Variant
from app.schemas import (
    BudgetTarget,
    ProductStockRow,
    SizeCell,
    CampaignFacts,
    EventFacts,
    Facts,
    ForecastFacts,
    RecommendedAction,
    ReorderLine,
    SizeCover,
)
from app.services.calendar import CalendarEventDef
from app.services.clock import now as clock_now
from app.services.events import EventBus
from app.services.fmt import hours as fmt_hours
from app.services.fmt import inr, pct, size_list
from app.services.forecast import DemandModel, VariantForecast, cover_days, recalibrate

log = logging.getLogger("signalshift.engine")

SEVERITY_RANK = {"opportunity": 1, "warning": 2, "critical": 3}


# =============================================================================
# Pure rule functions
# =============================================================================

def hours_of_cover(available: int, demand_per_hr: float) -> float | None:
    """available / demand_per_hr; None means no measurable demand (infinite cover)."""
    if demand_per_hr <= 1e-9:
        return None
    return max(available, 0) / demand_per_hr


def size_status(available: int, cover: float | None, low_cover_hours: float) -> str:
    if available <= 0:
        return "out"
    if cover is not None and cover < low_cover_hours:
        return "low"
    return "ok"


def click_share_split(sizes: Iterable[SizeCover]) -> tuple[float, float, list[str]]:
    """(at_risk_share, healthy_share, at_risk_sizes): healthy = clicks wanting sizes that still have cover."""
    at_risk, healthy, names = 0.0, 0.0, []
    for sc in sizes:
        share = sc.click_share or 0.0
        if sc.status in ("out", "low"):
            at_risk += share
            names.append(sc.size)
        else:
            healthy += share
    return at_risk, healthy, names


def restock_deadline(now: datetime, restock_eta: datetime | None, in_transit: int, lead_time_days: int,
                     buffer_days: float) -> tuple[datetime, bool]:
    """When new stock can realistically land, and whether a known restock is overdue."""
    if in_transit > 0 and restock_eta is not None:
        if restock_eta < now:
            return now + timedelta(days=buffer_days), True
        return restock_eta, False
    return now + timedelta(days=lead_time_days + buffer_days), False


def classify_severity(cover: float | None, has_active_campaign: bool, stockout_eta: datetime | None,
                      deadline: datetime, ad_pause_lag_hours: float) -> tuple[str | None, str]:
    if cover is not None and cover < ad_pause_lag_hours and has_active_campaign:
        return "critical", (f"cover ({fmt_hours(cover)}) is shorter than the {ad_pause_lag_hours:g} h it takes to "
                            "pause ads, and an active campaign targets this product")
    if stockout_eta is not None and stockout_eta < deadline:
        return "warning", "projected stockout comes before stock can be replenished"
    return None, "enough cover until restock"


def wasted_spend(clicks_per_hr: float, avg_cpc: float, at_risk: Iterable[tuple[float, float | None]],
                 horizon_hours: float) -> tuple[float, float]:
    """(spend per hour on at-risk sizes once they are out, projected waste over the horizon).

    at_risk = [(click_share, hours_of_cover)]; a size wastes clicks_per_hr * avg_cpc * share for every hour
    the ad stays live after that size sells out, bounded by the horizon.
    """
    per_hr, total = 0.0, 0.0
    for share, cover in at_risk:
        rate = clicks_per_hr * avg_cpc * share
        per_hr += rate
        live_after_stockout = max(0.0, horizon_hours - (cover or 0.0))
        total += rate * live_after_stockout
    return per_hr, total


def campaign_recommendation(at_risk_share: float, daily_budget: float, pause_share: float) -> tuple[str, float]:
    """Pause outright when most clicks want low/out sizes; otherwise trim the at-risk part of the budget."""
    if at_risk_share >= pause_share:
        return "pause_campaign", daily_budget
    if at_risk_share > 0:
        return "shift_budget", daily_budget * at_risk_share
    return "none", 0.0


@dataclass(frozen=True)
class BudgetCandidate:
    product_id: int
    product_name: str
    campaign_id: int
    campaign_name: str
    current_budget: float
    cover_days: float
    expected_roas: float
    margin_pct: float
    at_risk_click_share: float
    required_cover_days: float = 0.0  # e.g. must last through an upcoming festival
    cover_days_on_hand: float = 0.0

    @property
    def score(self) -> float:
        return self.expected_roas * self.margin_pct


def rank_budget_targets(candidates: Iterable[BudgetCandidate], freed: float, min_cover_days: float,
                        max_targets: int, max_at_risk_share: float = 0.2, step: float = 100.0) -> list[BudgetTarget]:
    """Rank by expected ROAS x margin among products with enough cover; split freed budget by score.
    Cover must reach both the global minimum and the candidate's own requirement (e.g. through a festival).
    Each target gets at most +100% of its current budget; amounts round down to the step."""
    eligible = [c for c in candidates if c.cover_days >= max(min_cover_days, c.required_cover_days)
                and c.at_risk_click_share < max_at_risk_share and c.expected_roas > 0]
    eligible.sort(key=lambda c: (-c.score, c.product_id))
    top = eligible[:max_targets]
    if not top or freed <= 0:
        return []
    total = sum(c.score for c in top)
    out: list[BudgetTarget] = []
    for c in top:
        raw = freed * c.score / total
        amount = math.floor(min(raw, c.current_budget) / step) * step
        if amount <= 0:
            continue
        out.append(BudgetTarget(product_id=c.product_id, product_name=c.product_name, campaign_id=c.campaign_id,
                                campaign_name=c.campaign_name, current_budget=c.current_budget, amount=amount,
                                cover_days=round(c.cover_days, 1), cover_days_on_hand=round(c.cover_days_on_hand, 1),
                                expected_roas=round(c.expected_roas, 2),
                                margin_pct=round(c.margin_pct, 3), score=round(c.score, 3)))
    return out


@dataclass(frozen=True)
class PriorAlert:
    severity: str
    hours_of_cover: float | None
    cooldown_until: datetime | None
    ts: datetime


def cooldown_decision(new_severity: str, new_cover: float | None, prior: Iterable[PriorAlert], now: datetime,
                      redrop_pct: float) -> tuple[bool, str]:
    """Allow a new alert unless one of equal/higher severity is cooling down — except when cover fell sharply."""
    active = [p for p in prior if p.cooldown_until is not None and p.cooldown_until > now]
    if not active:
        return True, "no active cooldown"
    top = max(active, key=lambda p: (SEVERITY_RANK[p.severity], p.ts))
    if SEVERITY_RANK[new_severity] > SEVERITY_RANK[top.severity]:
        return True, f"escalation from {top.severity}"
    if SEVERITY_RANK[new_severity] < SEVERITY_RANK[top.severity]:
        # a higher-severity alert is still cooling down: never re-alert at a lower level, even if cover fell
        return False, f"{top.severity} alert cooling down until {top.cooldown_until:%Y-%m-%d %H:%M}"
    same = sorted((p for p in active if p.severity == new_severity), key=lambda p: p.ts)
    if same and new_cover is not None and same[-1].hours_of_cover:
        ref = same[-1].hours_of_cover
        if new_cover < ref * (1 - redrop_pct):
            return True, f"cover dropped {round((1 - new_cover / ref) * 100)}% since the last alert"
    return False, f"cooldown active until {top.cooldown_until:%Y-%m-%d %H:%M}"


def material_change(old: Facts, new: Facts) -> bool:
    """Would the owner now be asked to do something different? (at-risk sizes or the action plan changed)"""
    def plan(f: Facts) -> tuple:
        return (tuple(f.at_risk_sizes),
                tuple(sorted((a.type, a.campaign_id or 0) for a in f.actions)),
                tuple(sorted((c.campaign_id, c.recommended) for c in f.campaigns)))
    return plan(old) != plan(new)


def reorder_qty(projected_demand: float, available: int, in_transit: int, safety_pct: float = 0.0) -> int:
    # round first so float noise (100 * 1.1 = 110.00000000000001) does not add a phantom unit
    return max(0, math.ceil(round(projected_demand * (1 + safety_pct) - max(available, 0) - in_transit, 6)))


# =============================================================================
# Orchestration
# =============================================================================

@dataclass
class Decision:
    variant_id: int
    product_id: int
    decision: str  # alert / suppressed / no_issue
    severity: str | None
    reason: str
    alert_id: int | None = None
    facts: Facts | None = None


@dataclass
class _CampaignStats:
    campaign: Campaign
    mix: dict[str, float]
    clicks_per_hr: float
    avg_cpc: float
    expected_roas: float


@dataclass
class _ProductCtx:
    product: Product
    variants: list[Variant]
    inventory: dict[int, Inventory]
    forecasts: dict[int, VariantForecast]
    sizes: list[SizeCover]
    campaigns: list[_CampaignStats]


AlertHook = Callable[[int, Facts], None]


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def _cover(x: float | None) -> float | None:
    """Cover is rounded DOWN to 0.1 h: conservative, and 3.97 h never displays as "4.0 h" under a 4 h rule."""
    return None if x is None else math.floor(x * 10) / 10


class Engine:
    def __init__(self, db: Database, bus: EventBus | None, settings: Settings, events: list[CalendarEventDef],
                 clock: Callable[[], datetime] = clock_now) -> None:
        self.db, self.bus, self.settings, self.events, self.clock = db, bus, settings, events, clock
        self._lock = threading.RLock()
        self.hooks: list[AlertHook] = []
        self._snap_lock = threading.Lock()
        self._snap: tuple[datetime, list[ProductStockRow]] | None = None  # (computation start, result)

    # ---------------------------------------------------------------- entry points
    def evaluate_variants(self, variant_ids: list[int], trigger: str, now: datetime | None = None,
                          anomalies: list[str] | None = None, include_opportunity: bool = True) -> list[Decision]:
        with self._lock:
            # Read the clock only once we hold the lock: a long sweep may have delayed us, and a stale "now"
            # would hide orders that committed meanwhile from the surge window.
            now = now or self.clock()
            with self.db.session() as s:
                dm = DemandModel(s, self.settings, self.events, now)
                rows = list(s.execute(select(Variant).where(Variant.id.in_(variant_ids))).scalars())
                # Size-aware facts are product-level (UK 8 selling out changes UK 9's at-risk click share), so
                # sibling sizes that already have an OPEN alert are re-checked too, keeping those alerts current.
                touched = {v.product_id for v in rows}
                requested = {v.id for v in rows}
                rows += [v for v in s.scalars(
                    select(Variant).join(Alert, Alert.variant_id == Variant.id)
                    .where(Variant.product_id.in_(touched), Alert.status == "open", Alert.severity != "opportunity")
                    .distinct()) if v.id not in requested]
                by_product: dict[int, list[Variant]] = {}
                for v in rows:
                    by_product.setdefault(v.product_id, []).append(v)
                pending: list[Decision] = []
                for pid, targets in by_product.items():
                    ctx = self._product_ctx(s, dm, pid, now)
                    for v in targets:
                        pending.append(self._assess_variant(s, dm, ctx, v, trigger, now, list(anomalies or [])))
                    if include_opportunity:
                        opp = self._assess_opportunity(dm, ctx, trigger, now)
                        # "no_issue" at opportunity level lets a resolved festival shortfall expire its alert
                        pending.append(opp or Decision(ctx.variants[0].id, pid, "no_issue", "opportunity",
                                                       "no festival shortfall"))
            return [self._commit_decision(d, now) for d in pending]

    def sweep(self, trigger: str, live_campaigns_only: bool = False, now: datetime | None = None) -> list[Decision]:
        with self.db.session() as s:
            q = select(Variant.id)
            if live_campaigns_only:
                q = q.join(Campaign, Campaign.product_id == Variant.product_id).where(Campaign.status == "active")
            ids = sorted(set(s.scalars(q).all()))
        return self.evaluate_variants(ids, trigger, now=now)

    def recalibrate(self, now: datetime | None = None) -> dict[str, float]:
        with self._lock, self.db.write() as s:
            return recalibrate(s, self.events, now or self.clock())

    def stock_snapshot(self, now: datetime | None = None) -> list[ProductStockRow]:
        """Size-level cover for every product (dashboard heatmap) - the same context the rules use.

        Concurrent callers share one computation: a result whose computation STARTED after this call
        arrived is at least as fresh as computing it again, so it is reused (single-flight)."""
        if now is not None:
            return self._compute_snapshot(now)
        requested = datetime.now()
        with self._snap_lock:
            if self._snap is not None and self._snap[0] >= requested:
                return self._snap[1]
            started = datetime.now()
            rows = self._compute_snapshot(self.clock())
            self._snap = (started, rows)
            return rows

    def _compute_snapshot(self, now: datetime) -> list[ProductStockRow]:
        st = self.settings
        rank = {"ok": 0, "short": 1, "low": 2, "out": 3}
        rows: list[ProductStockRow] = []
        with self.db.session() as s:
            dm = DemandModel(s, st, self.events, now)
            for p in s.scalars(select(Product).order_by(Product.id)):
                ctx = self._product_ctx(s, dm, p.id, now)
                cells: list[SizeCell] = []
                for sc in ctx.sizes:
                    inv = ctx.inventory[sc.variant_id]
                    status = sc.status
                    if status == "ok" and sc.hours_of_cover is not None:
                        deadline, _ = restock_deadline(now, inv.restock_eta, inv.in_transit, inv.lead_time_days,
                                                       st.warning_buffer_days)
                        if now + timedelta(hours=sc.hours_of_cover) < deadline:
                            status = "short"
                    cells.append(SizeCell(
                        variant_id=sc.variant_id, sku=next(v.sku for v in ctx.variants if v.id == sc.variant_id),
                        size=sc.size, on_hand=inv.on_hand, available=sc.available, in_transit=inv.in_transit,
                        restock_eta=inv.restock_eta, demand_per_day=round(sc.demand_per_hr * 24, 1),
                        hours_of_cover=sc.hours_of_cover, click_share=sc.click_share, status=status))
                demand_day = sum(c.demand_per_day for c in cells)
                total = sum(c.available for c in cells)
                rows.append(ProductStockRow(
                    product_id=p.id, name=p.name, model=p.model, sku_group=p.sku_group, unit_label=p.unit_label,
                    price=p.price, total_available=total, demand_per_day=round(demand_day, 1),
                    cover_days=_cover(total / demand_day) if demand_day > 0 else None,
                    lead_time_days=max(ctx.inventory[c.variant_id].lead_time_days for c in cells),
                    campaign_live=bool(ctx.campaigns),
                    worst_status=max((c.status for c in cells), key=lambda x: rank[x]), sizes=cells))
        return rows

    def ensure_calibrated(self) -> None:
        with self.db.session() as s:
            has = s.scalar(text("SELECT COUNT(*) FROM forecast_factors"))
        if not has:
            self.recalibrate()

    def delayed_restocks(self, now: datetime | None = None) -> list[Decision]:
        now = now or self.clock()
        with self.db.session() as s:
            ids = list(s.scalars(select(Inventory.variant_id).where(
                Inventory.in_transit > 0, Inventory.restock_eta.is_not(None), Inventory.restock_eta < now)))
        return self.evaluate_variants(ids, "restock_delayed", now=now, anomalies=["restock_delayed"]) if ids else []

    def campaign_activated(self, campaign_id: int, now: datetime | None = None) -> list[Decision]:
        with self.db.session() as s:
            c = s.get(Campaign, campaign_id)
            if c is None or c.status != "active":
                return []
            ids = list(s.scalars(select(Variant.id).where(Variant.product_id == c.product_id)))
        return self.evaluate_variants(ids, "campaign_activated", now=now, anomalies=["campaign_switched_on"])

    # ---------------------------------------------------------------- context
    def _campaign_stats(self, s: Session, c: Campaign, price: float, now: datetime) -> _CampaignStats:
        mix = {m.size: m.click_share for m in s.scalars(select(CampaignSizeMix).where(CampaignSizeMix.campaign_id == c.id))}
        clicks, spend, conv, hrs = s.execute(text(
            "SELECT COALESCE(SUM(clicks),0), COALESCE(SUM(spend),0), COALESCE(SUM(conversions),0), "
            "COUNT(CASE WHEN spend > 0 THEN 1 END) FROM ad_metrics_hourly "
            "WHERE campaign_id = :cid AND ts >= :a AND ts < :b"),
            {"cid": c.id, "a": sql_ts(now - timedelta(days=7)), "b": sql_ts(now)}).one()
        if clicks > 0 and hrs > 0:
            cph, cpc = clicks / hrs, spend / clicks
            roas = conv * price / spend if spend > 0 else 0.0
        else:  # no history yet: assume the budget is spent evenly at the bid
            cpc = c.bid
            cph = c.daily_budget / max(c.bid, 0.01) / 24
            roas = 0.0
        return _CampaignStats(campaign=c, mix=mix, clicks_per_hr=cph, avg_cpc=cpc, expected_roas=roas)

    def _product_ctx(self, s: Session, dm: DemandModel, product_id: int, now: datetime) -> _ProductCtx:
        product = s.get(Product, product_id)
        assert product is not None
        variants = list(s.scalars(select(Variant).where(Variant.product_id == product_id).order_by(Variant.size_rank)))
        inventory = {i.variant_id: i for i in s.scalars(select(Inventory).where(
            Inventory.variant_id.in_([v.id for v in variants])))}
        fallback = 1.0 / max(len(variants), 1)
        forecasts = {v.id: dm.variant(v, fallback) for v in variants}
        campaigns = [self._campaign_stats(s, c, product.price, now) for c in dm.active_campaigns(product_id)]
        total_budget = sum(cs.campaign.daily_budget for cs in campaigns)
        sizes: list[SizeCover] = []
        for v in variants:
            inv = inventory[v.id]
            avail = inv.on_hand - inv.reserved
            cover = hours_of_cover(avail, forecasts[v.id].demand_per_hr)
            share = (sum(cs.mix.get(v.size, 0.0) * cs.campaign.daily_budget for cs in campaigns) / total_budget
                     if total_budget > 0 else None)
            sizes.append(SizeCover(variant_id=v.id, size=v.size, available=avail, in_transit=inv.in_transit,
                                   demand_per_hr=round(forecasts[v.id].demand_per_hr, 3), hours_of_cover=_cover(cover),
                                   click_share=_r(share, 3),
                                   status=size_status(avail, cover, self.settings.low_cover_hours)))  # type: ignore[arg-type]
        return _ProductCtx(product, variants, inventory, forecasts, sizes, campaigns)

    def _base_facts(self, ctx: _ProductCtx, v: Variant, dm: DemandModel, severity: str, rule: str, trigger: str,
                    anomalies: list[str], now: datetime) -> Facts:
        p, inv, vf = ctx.product, ctx.inventory[v.id], ctx.forecasts[v.id]
        pf = dm.product(p.id)
        avail = inv.on_hand - inv.reserved
        cover = hours_of_cover(avail, vf.demand_per_hr)
        st = self.settings
        return Facts(
            severity=severity, rule=rule, trigger=trigger, anomalies=sorted(set(anomalies)), generated_at=now,
            product_id=p.id, product_name=p.name, variant_id=v.id, sku=v.sku, size=v.size, unit_label=p.unit_label,
            price=p.price, margin_pct=round((p.price - p.cost) / p.price, 3),
            available=avail, on_hand=inv.on_hand, reserved=inv.reserved, in_transit=inv.in_transit,
            lead_time_days=inv.lead_time_days, restock_eta=inv.restock_eta,
            product_total_available=sum(sc.available for sc in ctx.sizes),
            forecast=ForecastFacts(
                base_velocity_per_hr=round(pf.base_velocity_per_hr, 4), seasonal_uplift=round(pf.seasonal_uplift, 2),
                campaign_uplift=round(pf.campaign_uplift, 2), size_mix_factor=round(vf.size_mix_factor, 3),
                demand_per_hr=round(vf.demand_per_hr, 3), demand_per_hr_low=round(vf.demand_low, 3),
                demand_per_hr_high=round(vf.demand_high, 3), demand_per_day=round(vf.demand_per_hr * 24, 1),
                recent_rate_per_hr=round(vf.recent_rate_per_hr, 2), surge=vf.surge, band_pct=round(pf.band_pct, 2)),
            hours_of_cover=_cover(cover),
            hours_of_cover_low=_cover(hours_of_cover(avail, vf.demand_high)),
            hours_of_cover_high=_cover(hours_of_cover(avail, vf.demand_low)),
            days_of_cover=_cover(cover / 24 if cover is not None else None),
            stockout_eta=(now + timedelta(hours=cover)).replace(microsecond=0) if cover is not None else None,
            sizes=ctx.sizes,
            thresholds={"ad_pause_lag_hours": st.ad_pause_lag_hours, "low_cover_hours": st.low_cover_hours,
                        "alert_cooldown_hours": st.alert_cooldown_hours, "waste_horizon_hours": st.waste_horizon_hours,
                        "budget_shift_min_cover_days": st.budget_shift_min_cover_days,
                        "pause_click_share": st.pause_click_share, "warning_buffer_days": st.warning_buffer_days,
                        "surge_window_hours": st.surge_window_minutes / 60},
        )

    # ---------------------------------------------------------------- variant rules
    def _assess_variant(self, s: Session, dm: DemandModel, ctx: _ProductCtx, v: Variant, trigger: str,
                        now: datetime, anomalies: list[str]) -> Decision:
        st = self.settings
        inv, vf = ctx.inventory[v.id], ctx.forecasts[v.id]
        avail = inv.on_hand - inv.reserved
        cover = hours_of_cover(avail, vf.demand_per_hr)
        stockout_eta = now + timedelta(hours=cover) if cover is not None else None
        deadline, overdue = restock_deadline(now, inv.restock_eta, inv.in_transit, inv.lead_time_days,
                                             st.warning_buffer_days)
        if vf.surge:
            anomalies.append("velocity_spike")
        if overdue:
            anomalies.append("restock_delayed")
        severity, rule = classify_severity(cover, bool(ctx.campaigns), stockout_eta, deadline, st.ad_pause_lag_hours)
        if severity is None:
            return Decision(v.id, ctx.product.id, "no_issue", None, rule)

        facts = self._base_facts(ctx, v, dm, severity, rule, trigger, anomalies, now)
        facts.restock_overdue = overdue
        _share, _healthy, at_risk_sizes = click_share_split(ctx.sizes)
        if v.size not in at_risk_sizes:  # the alerted size is always at risk, even if not yet "low"
            at_risk_sizes = [v.size, *at_risk_sizes]
        facts.at_risk_sizes = [sc.size for sc in ctx.sizes if sc.size in at_risk_sizes]
        # share and sizes must describe the same set, or the report would pair a number with the wrong sizes
        at_risk_share = sum(sc.click_share or 0.0 for sc in ctx.sizes if sc.size in facts.at_risk_sizes)
        facts.at_risk_click_share = round(at_risk_share, 3) if ctx.campaigns else None

        # Campaign facts, wasted spend, budget shift
        horizon = min(st.waste_horizon_hours, max(0.0, (deadline - now).total_seconds() / 3600))
        waste_hr, waste_total, freed = 0.0, 0.0, 0.0
        for cs in ctx.campaigns:
            c = cs.campaign
            c_risk = sum(cs.mix.get(sz, 0.0) for sz in facts.at_risk_sizes)
            rec, c_freed = (campaign_recommendation(c_risk, c.daily_budget, st.pause_click_share)
                            if severity == "critical" else ("none", 0.0))
            per_hr, total = wasted_spend(cs.clicks_per_hr, cs.avg_cpc,
                                         [(cs.mix.get(sc.size, 0.0), sc.hours_of_cover) for sc in ctx.sizes
                                          if sc.size in facts.at_risk_sizes], horizon)
            waste_hr += per_hr
            waste_total += total
            freed += c_freed
            facts.campaigns.append(CampaignFacts(
                campaign_id=c.id, name=c.name, platform=c.platform, daily_budget=c.daily_budget,
                clicks_per_hr=round(cs.clicks_per_hr, 1), avg_cpc=round(cs.avg_cpc, 2),
                expected_roas=round(cs.expected_roas, 2), at_risk_click_share=round(c_risk, 3),
                healthy_click_share=round(1 - c_risk, 3), at_risk_spend_per_day=round(c.daily_budget * c_risk),
                recommended=rec, freed_budget=round(c_freed)))  # type: ignore[arg-type]
        lost_units = sum(sc.demand_per_hr * max(0.0, horizon - (sc.hours_of_cover or 0.0))
                         for sc in ctx.sizes if sc.size in facts.at_risk_sizes)
        facts.lost_units_estimate = round(lost_units, 1)
        facts.lost_sales_estimate = round(lost_units * ctx.product.price)
        if ctx.campaigns:
            facts.wasted_spend_per_hr = round(waste_hr)
            facts.wasted_spend_horizon_hours = round(horizon, 1)
            facts.wasted_spend_estimate = round(waste_total)

        # Reorder for the at-risk sizes
        n_days = inv.lead_time_days + st.reorder_cover_days
        for sc in ctx.sizes:
            if sc.size not in facts.at_risk_sizes:
                continue
            var = next(x for x in ctx.variants if x.id == sc.variant_id)
            projected = sum(dm.projected_variant_daily(var, ctx.forecasts[var.id].size_mix_factor, n_days))
            qty = reorder_qty(projected, sc.available, sc.in_transit)
            if qty > 0:
                facts.reorder.append(ReorderLine(variant_id=sc.variant_id, size=sc.size, qty=qty,
                                                 projected_demand=round(projected, 1), available=sc.available,
                                                 in_transit=sc.in_transit))
        facts.reorder_total = sum(r.qty for r in facts.reorder) or None

        if severity == "critical":
            facts.freed_budget = round(freed)
            if freed > 0:
                facts.budget_targets = rank_budget_targets(
                    self._budget_candidates(s, dm, ctx.product.id, now), freed, st.budget_shift_min_cover_days,
                    st.budget_shift_max_targets)
                facts.budget_moved = sum(t.amount for t in facts.budget_targets)
                facts.budget_held_back = round(freed - facts.budget_moved)
        facts.actions = self._actions(facts)
        return Decision(v.id, ctx.product.id, "alert", severity, rule, facts=facts)

    def _budget_candidates(self, s: Session, dm: DemandModel, exclude_product_id: int,
                           now: datetime) -> list[BudgetCandidate]:
        out: list[BudgetCandidate] = []
        for c in dm.campaigns():
            if c.status != "active" or c.product_id == exclude_product_id:
                continue
            p = s.get(Product, c.product_id)
            assert p is not None
            ctx = self._product_ctx(s, dm, p.id, now)
            supply = sum(sc.available + sc.in_transit for sc in ctx.sizes)
            daily = [0.0] * 60
            for var in ctx.variants:
                for i, d in enumerate(dm.projected_variant_daily(var, ctx.forecasts[var.id].size_mix_factor, 60)):
                    daily[i] += d
            cs = next(x for x in ctx.campaigns if x.campaign.id == c.id)
            at_risk, _h, _n = click_share_split(
                [sc.model_copy(update={"click_share": cs.mix.get(sc.size, 0.0)}) for sc in ctx.sizes])
            ev = self._next_event(p.seasonality_tag, now)
            required = float((ev.end - now.date()).days + 1) if ev else 0.0
            demand_day = sum(ctx.forecasts[var.id].demand_per_hr for var in ctx.variants) * 24
            on_hand_days = sum(sc.available for sc in ctx.sizes) / demand_day if demand_day > 0 else 999.0
            out.append(BudgetCandidate(product_id=p.id, product_name=p.name, campaign_id=c.id, campaign_name=c.name,
                                       current_budget=c.daily_budget, cover_days=cover_days(supply, daily),
                                       expected_roas=cs.expected_roas, margin_pct=(p.price - p.cost) / p.price,
                                       at_risk_click_share=at_risk, required_cover_days=required,
                                       cover_days_on_hand=on_hand_days))
        return out

    # ---------------------------------------------------------------- opportunity rule
    def _next_event(self, tag: str, now: datetime) -> CalendarEventDef | None:
        today = now.date()
        horizon = today + timedelta(days=self.settings.opportunity_lookahead_days)
        upcoming = [e for e in self.events if e.start <= horizon and e.end >= today and e.uplift.get(tag, 1.0) > 1.05]
        return max(upcoming, key=lambda e: (e.uplift.get(tag, 1.0), -e.date.toordinal())) if upcoming else None

    def _assess_opportunity(self, dm: DemandModel, ctx: _ProductCtx, trigger: str, now: datetime) -> Decision | None:
        p = ctx.product
        ev = self._next_event(p.seasonality_tag, now)
        if ev is None:
            return None
        n_days = (ev.end - now.date()).days + 1
        lines: list[tuple[ReorderLine, float]] = []
        total_projected, total_short, total_supply = 0.0, 0.0, 0.0
        for var in ctx.variants:
            inv = ctx.inventory[var.id]
            projected = sum(dm.projected_variant_daily(var, ctx.forecasts[var.id].size_mix_factor, n_days))
            arrives = inv.restock_eta is None or inv.restock_eta.date() <= ev.end
            supply = max(inv.on_hand - inv.reserved, 0) + (inv.in_transit if arrives else 0)
            short = projected - supply
            total_projected += projected
            total_supply += supply
            if short > 0:
                total_short += short
                lines.append((ReorderLine(variant_id=var.id, size=var.size,
                                          qty=reorder_qty(projected, supply, 0, safety_pct=0.1),
                                          projected_demand=round(projected, 1), available=inv.on_hand - inv.reserved,
                                          in_transit=inv.in_transit), short))
        if total_short < max(self.settings.opportunity_min_shortfall, 0.1 * total_projected):
            return None
        worst = max(lines, key=lambda t: t[1])[0]
        v = next(x for x in ctx.variants if x.id == worst.variant_id)
        rule = (f"{ev.name} is {(ev.date - now.date()).days} days away and projected demand through "
                f"{ev.end:%d %b} exceeds available plus in-transit stock")
        facts = self._base_facts(ctx, v, dm, "opportunity", rule, trigger, [], now)
        facts.event = EventFacts(name=ev.name, date=ev.date.isoformat(), days_until=(ev.date - now.date()).days,
                                 window_end=ev.end.isoformat(), peak_uplift=ev.uplift.get(p.seasonality_tag, 1.0))
        facts.projected_demand = round(total_projected)
        facts.projected_supply = round(total_supply)
        facts.projected_shortfall = round(total_short)
        facts.reorder = [ln for ln, _ in lines if ln.qty > 0]
        facts.reorder_total = sum(r.qty for r in facts.reorder)
        facts.actions = self._actions(facts)
        return Decision(v.id, p.id, "alert", "opportunity", rule, facts=facts)

    # ---------------------------------------------------------------- actions
    @staticmethod
    def _actions(f: Facts) -> list[RecommendedAction]:
        out: list[RecommendedAction] = []
        for c in f.campaigns:
            if c.recommended == "pause_campaign":
                out.append(RecommendedAction(
                    type="pause_campaign", campaign_id=c.campaign_id, amount=c.daily_budget,
                    summary=(f"Pause '{c.name}': {pct(c.at_risk_click_share)} of its clicks want "
                             f"{size_list(f.at_risk_sizes)}, which are low or out. Frees {inr(c.daily_budget)}/day.")))
        if f.budget_targets:
            src = next((c for c in f.campaigns if c.recommended != "none"), None)
            moves = " and ".join(f"{inr(t.amount)}/day to {t.product_name} ({math.floor(t.cover_days_on_hand)} days of cover)"
                                 for t in f.budget_targets)
            if f.budget_held_back:
                moves += f"; hold back {inr(f.budget_held_back)}/day"
            if any(c.recommended == "pause_campaign" for c in f.campaigns):
                lead = "Re-invest the freed budget:"
            else:
                lead = (f"Cut {inr(f.freed_budget or 0)}/day of ad spend aimed at {size_list(f.at_risk_sizes)} "
                        "and move")
            out.append(RecommendedAction(
                type="shift_budget", campaign_id=src.campaign_id if src else None,
                amount=sum(t.amount for t in f.budget_targets), targets=f.budget_targets,
                summary=f"{lead} {moves}."))
        else:
            for c in f.campaigns:
                if c.recommended == "shift_budget":
                    out.append(RecommendedAction(
                        type="shift_budget", campaign_id=c.campaign_id, amount=c.freed_budget,
                        summary=f"Cut '{c.name}' by {inr(c.freed_budget)}/day (the share aimed at "
                                f"{size_list(f.at_risk_sizes)})."))
        if f.reorder:
            parts = ", ".join(f"{r.size}: {r.qty}" for r in f.reorder)
            out.append(RecommendedAction(
                type="reorder", lines=f.reorder, amount=float(sum(r.qty for r in f.reorder)),
                summary=f"Reorder {sum(r.qty for r in f.reorder)} {f.unit_label}s ({parts})."))
        return out

    # ---------------------------------------------------------------- persistence
    @staticmethod
    def headline(f: Facts) -> str:
        name = f"{f.product_name} {f.size}"
        if f.severity == "critical":
            if f.available <= 0:
                return f"{name} is sold out while ads are still live"
            return f"{name} sells out in ~{fmt_hours(f.hours_of_cover)} while ads are live"
        if f.severity == "warning":
            if f.available <= 0:
                return f"{name} is sold out until restock"
            return f"{name} will run out before restock (~{fmt_hours(f.hours_of_cover)} of cover)"
        assert f.event is not None
        return (f"{f.event.name} in {f.event.days_until} days: {f.product_name} is short by "
                f"~{round(f.projected_shortfall or 0)} {f.unit_label}s")

    def _prior(self, s: Session, d: Decision) -> list[PriorAlert]:
        q = select(Alert)
        if d.severity == "opportunity":  # opportunity alerts are product-level
            q = q.join(Variant, Variant.id == Alert.variant_id).where(
                Variant.product_id == d.product_id, Alert.severity == "opportunity")
        else:
            q = q.where(Alert.variant_id == d.variant_id, Alert.severity != "opportunity")
        return [PriorAlert(a.severity, a.hours_of_cover, a.cooldown_until, a.ts) for a in s.scalars(q)]

    def _scope(self, d: Decision) -> Any:
        """Open alerts this decision speaks for: product-level for opportunity, variant-level otherwise."""
        q = select(Alert).where(Alert.status == "open")
        if d.severity == "opportunity":
            return q.join(Variant, Variant.id == Alert.variant_id).where(
                Variant.product_id == d.product_id, Alert.severity == "opportunity")
        return q.where(Alert.variant_id == d.variant_id, Alert.severity != "opportunity")

    def _expire_above(self, s: Session, d: Decision, rank: int) -> list[int]:
        """The situation de-escalated (or resolved): open alerts above `rank` no longer describe reality."""
        stale = [a for a in s.scalars(self._scope(d)) if SEVERITY_RANK.get(a.severity, 0) > rank]
        for a in stale:
            a.status, a.decided_by = "expired", "engine"
        return [a.id for a in stale]

    def _commit_decision(self, d: Decision, now: datetime) -> Decision:
        """Cooldown check and alert write in ONE transaction: create, refresh in place, suppress, and retire
        open alerts whose condition no longer holds."""
        if d.decision != "alert" or d.facts is None:
            with self.db.write() as s:
                expired = self._expire_above(s, d, 0)
            if expired:
                self._announce_expired(d, expired)
            return d
        facts = d.facts
        rank = SEVERITY_RANK[d.severity or "opportunity"]
        with self.db.write() as s:
            expired = self._expire_above(s, d, rank)
            # Opportunity is a product-level shortfall, so the cover-drop exception does not apply to it.
            cover = None if d.severity == "opportunity" else facts.hours_of_cover
            allow, why = cooldown_decision(d.severity or "", cover, self._prior(s, d), now,
                                           self.settings.cooldown_redrop_pct)
            if allow:
                d.alert_id = self._create_alert(s, d, facts, now)
                d.reason = f"{d.reason} ({why})"
            else:
                # The cooldown limits notifications, not freshness: if the matching alert is still open and
                # what we would recommend has changed, refresh it in place so an approval never runs stale actions.
                refreshed = self._refresh_open_alert(s, d, facts)
                d.decision = "refreshed" if refreshed else "suppressed"
                d.reason = f"{why}; open alert #{refreshed} updated" if refreshed else why
                d.alert_id = refreshed
        if expired:
            self._announce_expired(d, expired)
        log.info("alert decision", extra={"decision": d.decision, "alert_id": d.alert_id, "severity": d.severity,
                                          "variant_id": d.variant_id, "reason": d.reason,
                                          "facts": facts.model_dump(mode="json")})
        if d.decision in ("alert", "refreshed"):
            self._notify(d, facts, refreshed=d.decision == "refreshed")
        return d

    def _announce_expired(self, d: Decision, alert_ids: list[int]) -> None:
        log.info("alert decision", extra={"decision": "expired", "alert_ids": alert_ids, "variant_id": d.variant_id,
                                          "reason": f"condition no longer holds (now: {d.severity or 'no issue'})"})
        if self.bus is not None:
            for aid in alert_ids:
                self.bus.publish("alert", {"alert_id": aid, "status": "expired", "variant_id": d.variant_id,
                                           "product_id": d.product_id})

    def _create_alert(self, s: Session, d: Decision, facts: Facts, now: datetime) -> int:
        # Supersede open alerts of equal/lower severity for the same scope.
        q = select(Alert.id).where(Alert.status == "open")
        if d.severity == "opportunity":
            q = q.join(Variant, Variant.id == Alert.variant_id).where(
                Variant.product_id == d.product_id, Alert.severity == "opportunity")
        else:
            q = q.where(Alert.variant_id == d.variant_id, Alert.severity != "opportunity")
        stale = list(s.scalars(q))
        if stale:
            s.execute(update(Alert).where(Alert.id.in_(stale)).values(status="expired"))
        headline = self.headline(facts)
        alert = Alert(ts=now, variant_id=d.variant_id,
                      campaign_id=facts.campaigns[0].campaign_id if facts.campaigns else None,
                      severity=d.severity or "", headline=headline, facts_json=facts.model_dump_json(),
                      hours_of_cover=facts.hours_of_cover, status="open",
                      cooldown_until=now + timedelta(hours=self.settings.alert_cooldown_hours))
        s.add(alert)
        s.flush()
        s.execute(text("INSERT INTO alert_fts (alert_id, headline, report_text, facts_text) VALUES (:i, :h, '', :f)"),
                  {"i": alert.id, "h": headline, "f": f"{facts.rule}. " + " ".join(a.summary for a in facts.actions)})
        return alert.id

    def _refresh_open_alert(self, s: Session, d: Decision, facts: Facts) -> int | None:
        q = select(Alert).where(Alert.status == "open", Alert.severity == d.severity)
        if d.severity == "opportunity":
            q = q.join(Variant, Variant.id == Alert.variant_id).where(Variant.product_id == d.product_id)
        else:
            q = q.where(Alert.variant_id == d.variant_id)
        alert = s.scalars(q.order_by(Alert.ts.desc())).first()
        if alert is None or not material_change(Facts.model_validate_json(alert.facts_json), facts):
            return None
        headline = self.headline(facts)
        alert.facts_json, alert.hours_of_cover, alert.headline = facts.model_dump_json(), facts.hours_of_cover, headline
        alert.campaign_id = facts.campaigns[0].campaign_id if facts.campaigns else None
        s.execute(text("UPDATE alert_fts SET headline = :h, facts_text = :f WHERE alert_id = :i"),
                  {"i": alert.id, "h": headline, "f": f"{facts.rule}. " + " ".join(a.summary for a in facts.actions)})
        return alert.id

    def _notify(self, d: Decision, facts: Facts, refreshed: bool) -> None:
        if self.bus is not None:
            self.bus.publish("alert", {"alert_id": d.alert_id, "severity": d.severity, "headline": self.headline(facts),
                                       "variant_id": d.variant_id, "product_id": d.product_id, "size": facts.size,
                                       "product_name": facts.product_name, "hours_of_cover": facts.hours_of_cover,
                                       "refreshed": refreshed},
                             key=None if refreshed else f"alert:{d.alert_id}")
        for hook in self.hooks:
            try:
                hook(d.alert_id, facts)  # type: ignore[arg-type]
            except Exception:
                log.exception("alert hook failed", extra={"alert_id": d.alert_id})
