"""MOCK live ad delivery. Synthetic: this is NOT connected to Google Ads or Meta.

The seeded data has hourly ad metrics only up to the generation time. While the app runs, this simulator
plays the ad platform: every tick it accrues spend, clicks and conversions for each ACTIVE campaign into the
current hour's `ad_metrics_hourly` row, following the campaign's budget and the intraday ad curve.

Clicks for sizes that are out of stock right now are recorded as wasted (`wasted_clicks` / `wasted_spend`),
using the campaign's size click mix, so the "wasted on out-of-stock clicks" counter is live. Pausing a
campaign (Phase 6) stops its spend. `AD_SIM_SPEED` compresses time for short demos.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Database, sql_ts
from app.models import Campaign, CampaignSizeMix, Inventory, Variant
from app.services.clock import now as clock_now
from app.services.events import EventBus
from app.services.profiles import AD_CURVE

log = logging.getLogger("signalshift.ad_simulator")

UPSERT = text(
    "INSERT INTO ad_metrics_hourly (ts, campaign_id, impressions, clicks, cpc, spend, conversions, wasted_clicks, "
    "wasted_spend) VALUES (:ts, :cid, :imp, :clk, :cpc, :spend, :conv, :wclk, :wspend) "
    "ON CONFLICT(campaign_id, ts) DO UPDATE SET impressions = impressions + excluded.impressions, "
    "clicks = clicks + excluded.clicks, spend = round(spend + excluded.spend, 2), "
    "conversions = conversions + excluded.conversions, wasted_clicks = wasted_clicks + excluded.wasted_clicks, "
    "wasted_spend = round(wasted_spend + excluded.wasted_spend, 2), "
    "cpc = CASE WHEN clicks + excluded.clicks > 0 THEN round((spend + excluded.spend) / (clicks + excluded.clicks), 2) "
    "ELSE cpc END"
)


def recent_cpc_cvr(s: Session, c: Campaign, now: datetime) -> tuple[float, float]:
    """The campaign's cost per click and conversion rate over the last 7 days (bid-based defaults when new)."""
    clicks7, spend7, conv7 = s.execute(text(
        "SELECT COALESCE(SUM(clicks),0), COALESCE(SUM(spend),0), COALESCE(SUM(conversions),0) "
        "FROM ad_metrics_hourly WHERE campaign_id = :c AND ts >= :a"),
        {"c": c.id, "a": sql_ts(now - timedelta(days=7))}).one()
    cpc = spend7 / clicks7 if clicks7 else c.bid * 0.85
    cvr = conv7 / clicks7 if clicks7 else 0.03
    return cpc, cvr


class AdSimulator:
    def __init__(self, db: Database, bus: EventBus | None, settings: Settings,
                 clock: Callable[[], datetime] = clock_now, seed: int | None = None) -> None:
        self.db, self.bus, self.settings, self.clock = db, bus, settings, clock
        self.rng = np.random.default_rng(seed)

    def tick(self, now: datetime | None = None, seconds: float | None = None) -> list[dict]:
        """Accrue `seconds` of (speed-adjusted) ad delivery for every active campaign."""
        now = now or self.clock()
        sim_seconds = (seconds if seconds is not None else self.settings.ad_sim_interval_seconds) \
            * self.settings.ad_sim_speed
        hour = now.replace(minute=0, second=0, microsecond=0)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        out: list[dict] = []
        with self.db.write() as s:
            for c in s.scalars(select(Campaign).where(Campaign.status == "active")):
                spent_today = float(s.scalar(text(
                    "SELECT COALESCE(SUM(spend), 0) FROM ad_metrics_hourly WHERE campaign_id = :c AND ts >= :d"),
                    {"c": c.id, "d": sql_ts(day_start)}) or 0.0)
                if spent_today >= c.daily_budget:
                    continue  # daily budget exhausted
                cpc, cvr = recent_cpc_cvr(s, c, now)
                budget_left = c.daily_budget - spent_today
                expected_spend = min(c.daily_budget * AD_CURVE[now.hour] * sim_seconds / 3600, budget_left)
                clicks = int(self.rng.poisson(max(expected_spend / cpc, 0.0)))
                if clicks == 0:
                    continue
                mix = {m.size: m.click_share for m in
                       s.scalars(select(CampaignSizeMix).where(CampaignSizeMix.campaign_id == c.id))}
                stock = dict(s.execute(select(Variant.size, Inventory.on_hand - Inventory.reserved)
                                       .join(Inventory, Inventory.variant_id == Variant.id)
                                       .where(Variant.product_id == c.product_id)).all())
                oos_share = sum(share for size, share in mix.items() if stock.get(size, 0) <= 0)
                wasted = int(self.rng.binomial(clicks, min(1.0, oos_share)))
                conv = int(self.rng.binomial(clicks - wasted, min(1.0, cvr)))
                row = {"ts": sql_ts(hour), "cid": c.id, "imp": int(clicks / 0.02), "clk": clicks,
                       "cpc": round(cpc, 2), "spend": round(clicks * cpc, 2), "conv": conv, "wclk": wasted,
                       "wspend": round(wasted * cpc, 2)}
                s.execute(UPSERT, row)
                out.append({"campaign_id": c.id, "clicks": clicks, "spend": row["spend"], "wasted_spend": row["wspend"]})
        if out and self.bus is not None:
            self.bus.publish("ads", {"ticks": out})
        return out
