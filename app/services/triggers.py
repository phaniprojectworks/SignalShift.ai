"""Engine triggers.

- Event-driven: each order re-evaluates only the variants it touched; a campaign switched on re-evaluates its product.
- Scheduled (APScheduler, business timezone): hourly refresh of variants with live campaigns plus a delayed-restock
  check, a daily briefing at 08:00, and a nightly recalibration of the learned forecast factors.
- Anomaly-driven: velocity spikes (> 2x forecast) are detected inside each evaluation; campaign switch-on and
  overdue restocks arrive through the paths above and are tagged as anomalies in the facts.
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import Settings
from app.models import Alert
from app.services.ad_simulator import AdSimulator
from app.services.engine import SEVERITY_RANK, Engine
from app.services.events import Event, EventBus

log = logging.getLogger("signalshift.triggers")

# evaluate(variant_ids, trigger) -> None
EvaluateFn = Callable[[list[int], str], object]


def log_only_evaluate(variant_ids: list[int], trigger: str) -> None:
    log.info("no engine wired; would evaluate", extra={"variant_ids": variant_ids, "trigger": trigger})


def register_event_triggers(bus: EventBus, evaluate: EvaluateFn,
                            on_campaign_activated: Callable[[int], object] | None = None) -> None:
    def on_sale(ev: Event) -> None:
        evaluate(sorted(set(ev.data["variant_ids"])), "order")

    def on_campaign(ev: Event) -> None:
        if on_campaign_activated is not None and ev.data.get("status") == "active":
            on_campaign_activated(int(ev.data["campaign_id"]))

    def on_decision(ev: Event) -> None:
        # Approving pauses ads / books stock: re-check the product so sibling open alerts are refreshed.
        evaluate([int(ev.data["variant_id"])], "decision")

    bus.on("sale", on_sale)
    bus.on("campaign", on_campaign)
    bus.on("action", on_decision)


# --- scheduled jobs -------------------------------------------------------------

def hourly_refresh(engine: Engine) -> None:
    engine.sweep("hourly_refresh", live_campaigns_only=True)
    engine.delayed_restocks()


def daily_briefing(engine: Engine, bus: EventBus | None) -> dict:
    engine.sweep("daily_briefing")
    with engine.db.session() as s:
        open_alerts = list(s.scalars(select(Alert).where(Alert.status == "open")))
    open_alerts.sort(key=lambda a: (-SEVERITY_RANK.get(a.severity, 0), a.ts))
    summary = {
        "counts": dict(Counter(a.severity for a in open_alerts)),
        "top": [{"alert_id": a.id, "severity": a.severity, "headline": a.headline} for a in open_alerts[:5]],
    }
    log.info("daily briefing", extra=summary)
    if bus is not None:
        bus.publish("briefing", summary)
    return summary


def nightly_recalibration(engine: Engine) -> None:
    engine.recalibrate()


def build_scheduler(engine: Engine, bus: EventBus | None, settings: Settings,
                    simulator: AdSimulator | None = None) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=settings.timezone, job_defaults={"coalesce": True, "max_instances": 1,
                                                                          "misfire_grace_time": 300})
    sched.add_job(hourly_refresh, CronTrigger(minute=0, timezone=settings.timezone), args=[engine],
                  id="hourly_refresh", name="Hourly refresh: variants with live campaigns + delayed restocks")
    sched.add_job(daily_briefing, CronTrigger(hour=settings.briefing_hour, minute=0, timezone=settings.timezone),
                  args=[engine, bus], id="daily_briefing", name="Daily owner briefing")
    sched.add_job(nightly_recalibration,
                  CronTrigger(hour=settings.recalibration_hour, minute=30, timezone=settings.timezone),
                  args=[engine], id="nightly_recalibration", name="Nightly forecast recalibration")
    if simulator is not None:
        sched.add_job(simulator.tick, IntervalTrigger(seconds=settings.ad_sim_interval_seconds),
                      id="ad_simulator", name="MOCK live ad delivery (synthetic)")
    return sched
