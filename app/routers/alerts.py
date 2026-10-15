from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.models import Alert
from app.schemas import AlertOut, DecisionOut, Facts
from app.services.actions import DecisionError
from app.services.engine import SEVERITY_RANK

router = APIRouter(tags=["alerts"])


def to_alert_out(a: Alert) -> AlertOut:
    return AlertOut(id=a.id, ts=a.ts, variant_id=a.variant_id, campaign_id=a.campaign_id, severity=a.severity,
                    headline=a.headline, status=a.status, hours_of_cover=a.hours_of_cover,
                    cooldown_until=a.cooldown_until, report_source=a.report_source, decided_by=a.decided_by,
                    decided_at=a.decided_at,
                    facts=Facts.model_validate_json(a.facts_json),
                    report=json.loads(a.report_text) if a.report_text else None)


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(request: Request, status: str | None = "open", severity: str | None = None,
                      limit: int = 50) -> list[AlertOut]:
    def _q() -> list[AlertOut]:
        with request.app.state.db.session() as s:
            q = select(Alert)
            if status:
                q = q.where(Alert.status == status)
            if severity:
                q = q.where(Alert.severity == severity)
            rows = list(s.scalars(q.order_by(Alert.ts.desc()).limit(500)))
            rows.sort(key=lambda a: (-SEVERITY_RANK.get(a.severity, 0), -a.ts.timestamp()))
            return [to_alert_out(a) for a in rows[:limit]]
    return await run_in_threadpool(_q)


@router.get("/alerts/{alert_id}", response_model=AlertOut)
async def get_alert(alert_id: int, request: Request) -> AlertOut:
    def _q() -> AlertOut | None:
        with request.app.state.db.session() as s:
            a = s.get(Alert, alert_id)
            return to_alert_out(a) if a else None
    out = await run_in_threadpool(_q)
    if out is None:
        raise HTTPException(404, "alert not found")
    return out


@router.post("/engine/run")
async def run_engine(request: Request, scope: str = "all") -> dict:
    """Manual sweep: scope=all (every variant) or scope=live (variants with live campaigns)."""
    engine = request.app.state.engine
    decisions = await run_in_threadpool(engine.sweep, "manual", scope == "live")
    alerts = [d for d in decisions if d.decision == "alert"]
    return {"evaluated": len(decisions), "alerts_created": len(alerts),
            "suppressed": sum(d.decision == "suppressed" for d in decisions),
            "alerts": [{"alert_id": d.alert_id, "severity": d.severity} for d in alerts]}


@router.post("/engine/recalibrate")
async def recalibrate(request: Request) -> dict:
    return await run_in_threadpool(request.app.state.engine.recalibrate)


def _decide(request: Request, alert_id: int, verb: str) -> DecisionOut:
    svc = request.app.state.actions
    try:
        return svc.approve(alert_id, "dashboard") if verb == "approve" else svc.dismiss(alert_id, "dashboard")
    except DecisionError as e:
        raise HTTPException(e.status_code, str(e)) from e


@router.post("/alerts/{alert_id}/approve", response_model=DecisionOut)
async def approve(alert_id: int, request: Request) -> DecisionOut:
    """Run the alert's recommended actions through the MOCK ad/supplier APIs and record savings."""
    return await run_in_threadpool(_decide, request, alert_id, "approve")


@router.post("/alerts/{alert_id}/dismiss", response_model=DecisionOut)
async def dismiss(alert_id: int, request: Request) -> DecisionOut:
    return await run_in_threadpool(_decide, request, alert_id, "dismiss")
