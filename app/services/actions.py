"""Approve / dismiss alerts and execute the recommended actions.

MOCK EXECUTORS - nothing here talks to a real ad platform or supplier.
  * MockGoogleAdsApi mirrors the request/response shapes of the Google Ads API
    CampaignService.MutateCampaigns (status pause) and CampaignBudgetService.MutateCampaignBudgets
    (amount_micros), so a real client could be swapped in behind the same contract.
  * MockMetaAdsApi mirrors a Marketing API (Graph) campaign update: POST /{campaign_id} {status|daily_budget}.
  * MockSupplierApi creates a purchase order and books the quantity as in-transit stock.
The local `campaigns` / `inventory` tables play the role of the platform state, so a mock pause really
stops the (mock) ad simulator's spend and a mock PO really changes the engine's restock picture.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Database
from app.models import Action, Alert, Campaign, Inventory, SavingsLedger
from app.schemas import DecisionOut, ExecutedAction, Facts, LedgerEntry, RecommendedAction
from app.services.clock import now as clock_now
from app.services.events import EventBus
from app.services.fmt import hours, inr, size_list

log = logging.getLogger("signalshift.actions")

GOOGLE_CUSTOMER_ID = "5550001234"


class DecisionError(Exception):
    status_code = 400


class AlertNotFound(DecisionError):
    status_code = 404


class AlertNotOpen(DecisionError):
    status_code = 409


# =============================================================================
# Mock ad platform / supplier APIs
# =============================================================================

def _google_ids(c: Campaign) -> tuple[str, str]:
    """(campaign resource name, campaign budget resource name) in Google Ads format."""
    campaign_rn = c.external_ref or f"customers/{GOOGLE_CUSTOMER_ID}/campaigns/{c.id}"
    budget_rn = campaign_rn.replace("/campaigns/", "/campaignBudgets/")
    return campaign_rn, budget_rn


class MockGoogleAdsApi:
    """MOCK of Google Ads CampaignService / CampaignBudgetService mutate calls."""

    @staticmethod
    def pause_request(c: Campaign) -> dict:
        rn, _ = _google_ids(c)
        return {"customer_id": GOOGLE_CUSTOMER_ID,
                "operations": [{"update": {"resource_name": rn, "status": "PAUSED"}, "update_mask": "status"}]}

    @staticmethod
    def budget_request(c: Campaign, new_daily_budget: float) -> dict:
        _, brn = _google_ids(c)
        return {"customer_id": GOOGLE_CUSTOMER_ID,
                "operations": [{"update": {"resource_name": brn, "amount_micros": int(round(new_daily_budget * 1_000_000))},
                                "update_mask": "amount_micros"}]}

    def mutate_campaigns(self, s: Session, request: dict, now: datetime) -> dict:
        results = []
        for op in request["operations"]:
            rn = op["update"]["resource_name"]
            c = s.scalar(select(Campaign).where(Campaign.external_ref == rn))
            if c is None:
                raise DecisionError(f"campaign {rn} not found")
            if op["update"].get("status") == "PAUSED":
                c.status, c.paused_at = "paused", now
            results.append({"resource_name": rn})
        return {"results": results}

    def mutate_campaign_budgets(self, s: Session, request: dict) -> dict:
        results = []
        for op in request["operations"]:
            brn = op["update"]["resource_name"]
            c = s.scalar(select(Campaign).where(Campaign.external_ref == brn.replace("/campaignBudgets/", "/campaigns/")))
            if c is None:
                raise DecisionError(f"budget {brn} not found")
            c.daily_budget = round(op["update"]["amount_micros"] / 1_000_000, 2)
            results.append({"resource_name": brn})
        return {"results": results}


class MockMetaAdsApi:
    """MOCK of a Marketing API campaign update (POST /{campaign_id}); budgets in minor units (paise)."""

    @staticmethod
    def pause_request(c: Campaign) -> dict:
        return {"method": "POST", "path": f"/{(c.external_ref or str(c.id)).split('/')[-1]}", "params": {"status": "PAUSED"}}

    @staticmethod
    def budget_request(c: Campaign, new_daily_budget: float) -> dict:
        return {"method": "POST", "path": f"/{(c.external_ref or str(c.id)).split('/')[-1]}",
                "params": {"daily_budget": int(round(new_daily_budget * 100))}}

    def update_campaign(self, s: Session, c: Campaign, request: dict, now: datetime) -> dict:
        params = request["params"]
        if params.get("status") == "PAUSED":
            c.status, c.paused_at = "paused", now
        if "daily_budget" in params:
            c.daily_budget = round(params["daily_budget"] / 100, 2)
        return {"success": True}


class MockSupplierApi:
    """MOCK purchase order: books the quantity as in-transit stock arriving after the lead time."""

    def __init__(self) -> None:
        self._seq = 0

    def create_purchase_order(self, s: Session, lines: list[dict], now: datetime) -> dict:
        self._seq += 1
        po_number = f"PO-MOCK-{now:%Y%m%d}-{self._seq:04d}"
        out_lines = []
        for ln in lines:
            inv = s.get(Inventory, ln["variant_id"])
            if inv is None:
                raise DecisionError(f"variant {ln['variant_id']} not found")
            eta = (now + timedelta(days=inv.lead_time_days)).replace(minute=0, second=0, microsecond=0)
            if not (inv.in_transit > 0 and inv.restock_eta and inv.restock_eta > now):
                inv.restock_eta = eta  # an earlier open PO keeps its earlier ETA
            inv.in_transit += ln["qty"]
            out_lines.append({**ln, "eta": eta.isoformat()})
        return {"po_number": po_number, "status": "CONFIRMED", "lines": out_lines}


# =============================================================================
# Savings
# =============================================================================

def savings_for(facts: Facts, executed: list[str]) -> tuple[float, float, str]:
    """(ad_spend_avoided, sales_protected, explanation), computed only from the alert's facts.

    Ad spend avoided: hourly spend on clicks for the at-risk sizes once they are out of stock, times the hours
    the ads would have kept running before restock, bounded by the waste horizon (= facts.wasted_spend_estimate).
    Counted only when an ad action (pause or budget cut) was executed.
    Sales protected: projected demand the current stock cannot serve that the reorder now covers
    (reorder quantity x price). Counted only when the reorder was executed.
    """
    parts: list[str] = []
    avoided = 0.0
    if {"pause_campaign", "shift_budget"} & set(executed) and facts.wasted_spend_estimate:
        avoided = float(facts.wasted_spend_estimate)
        parts.append(f"Ad spend avoided: {inr(facts.wasted_spend_per_hr or 0)}/hour on clicks for "
                     f"{size_list(facts.at_risk_sizes)} once out of stock, over the "
                     f"{hours(facts.wasted_spend_horizon_hours)} the ads would have run before restock "
                     f"(capped at {hours(facts.thresholds.get('waste_horizon_hours', 72))}) = {inr(avoided)}.")
    protected = 0.0
    if "reorder" in executed and facts.reorder_total:
        protected = float(facts.reorder_total * facts.price)
        parts.append(f"Sales protected: reorder of {facts.reorder_total} {facts.unit_label}s x {inr(facts.price)} "
                     f"covers projected demand the current stock cannot serve = {inr(protected)}.")
    return round(avoided, 2), round(protected, 2), " ".join(parts) or "No money-moving action was executed."


# =============================================================================
# Service
# =============================================================================

class ActionService:
    def __init__(self, db: Database, bus: EventBus | None, clock: Callable[[], datetime] = clock_now) -> None:
        self.db, self.bus, self.clock = db, bus, clock
        self.google, self.meta, self.supplier = MockGoogleAdsApi(), MockMetaAdsApi(), MockSupplierApi()
        self.on_decided: list[Callable[[DecisionOut], None]] = []

    # ---------------------------------------------------------------- execution
    def _run(self, s: Session, a: RecommendedAction, facts: Facts, now: datetime) -> list[ExecutedAction]:
        out: list[ExecutedAction] = []
        if a.type == "pause_campaign" and a.campaign_id is not None:
            c = s.get(Campaign, a.campaign_id)
            if c is None or c.status != "active":
                return out  # already paused: nothing to do (idempotent)
            if c.platform == "google_ads":
                req = self.google.pause_request(c)
                resp = self.google.mutate_campaigns(s, req, now)
            else:
                req = self.meta.pause_request(c)
                resp = self.meta.update_campaign(s, c, req, now)
            out.append(ExecutedAction(type="pause_campaign", campaign_id=c.id, request=req, response=resp))
        elif a.type == "shift_budget":
            for t in a.targets:
                c = s.get(Campaign, t.campaign_id)
                if c is None or c.status != "active":
                    continue
                out.append(self._set_budget(s, c, c.daily_budget + t.amount, now, "shift_budget"))
            # Sources that keep running (not paused) give up the at-risk share of their own budget.
            cuts = ([(c.campaign_id, c.freed_budget) for c in facts.campaigns if c.recommended == "shift_budget"]
                    if a.targets else [(a.campaign_id, a.amount or 0.0)])
            for cid, cut in cuts:
                src = s.get(Campaign, cid) if cid is not None else None
                if src is not None and src.status == "active" and cut > 0:
                    out.append(self._set_budget(s, src, src.daily_budget - min(cut, src.daily_budget), now,
                                                "shift_budget"))
        elif a.type == "reorder" and a.lines:
            lines = [{"variant_id": ln.variant_id, "size": ln.size, "qty": ln.qty} for ln in a.lines if ln.qty > 0]
            req = {"supplier": "MOCK supplier", "lines": lines}
            resp = self.supplier.create_purchase_order(s, lines, now)
            out.append(ExecutedAction(type="reorder", request=req, response=resp))
        return out

    def _set_budget(self, s: Session, c: Campaign, new_budget: float, now: datetime, kind: str) -> ExecutedAction:
        new_budget = max(0.0, round(new_budget, 2))
        if c.platform == "google_ads":
            req = self.google.budget_request(c, new_budget)
            resp = self.google.mutate_campaign_budgets(s, req)
        else:
            req = self.meta.budget_request(c, new_budget)
            resp = self.meta.update_campaign(s, c, req, now)
        return ExecutedAction(type=kind, campaign_id=c.id, request=req, response=resp)

    # ---------------------------------------------------------------- decisions
    def _open_alert(self, s: Session, alert_id: int) -> Alert:
        alert = s.get(Alert, alert_id)
        if alert is None:
            raise AlertNotFound(f"alert {alert_id} not found")
        if alert.status != "open":
            raise AlertNotOpen(f"alert {alert_id} is already {alert.status}")
        return alert

    def approve(self, alert_id: int, actor: str = "dashboard") -> DecisionOut:
        now = self.clock()
        with self.db.write() as s:  # one transaction: all actions + ledger, or nothing
            alert = self._open_alert(s, alert_id)
            facts = Facts.model_validate_json(alert.facts_json)
            executed: list[ExecutedAction] = []
            for a in facts.actions:
                executed.extend(self._run(s, a, facts, now))
            for ex in executed:
                s.add(Action(alert_id=alert_id, type=ex.type, executed_at=now,
                             payload_json=json.dumps({"actor": actor, "campaign_id": ex.campaign_id, "mock": True,
                                                      "request": ex.request}),
                             response_json=json.dumps(ex.response)))
            avoided, protected, why = savings_for(facts, [ex.type for ex in executed])
            ledger = None
            if executed:
                entry = SavingsLedger(alert_id=alert_id, ad_spend_avoided=avoided, sales_protected=protected,
                                      explanation=why, ts=now)
                s.add(entry)
                ledger = LedgerEntry(alert_id=alert_id, ts=now, ad_spend_avoided=avoided,
                                     sales_protected=protected, explanation=why)
            alert.status, alert.decided_at, alert.decided_by = "approved", now, actor
            variant_id = alert.variant_id
            touched = sorted({ex.campaign_id for ex in executed if ex.campaign_id is not None})
            campaigns = {c.id: c.status for c in s.scalars(select(Campaign).where(Campaign.id.in_(touched)))}
        decision = DecisionOut(alert_id=alert_id, status="approved", actor=actor, decided_at=now, actions=executed,
                               ledger=ledger)
        log.info("alert approved", extra={"alert_id": alert_id, "actor": actor, "executed": [e.type for e in executed],
                                          "ad_spend_avoided": avoided, "sales_protected": protected})
        self._publish(decision, campaigns, variant_id)
        return decision

    def dismiss(self, alert_id: int, actor: str = "dashboard") -> DecisionOut:
        now = self.clock()
        with self.db.write() as s:
            alert = self._open_alert(s, alert_id)
            alert.status, alert.decided_at, alert.decided_by = "dismissed", now, actor
            variant_id = alert.variant_id
            # the alert keeps its cooldown_until, so the engine stays quiet for this variant/severity
        decision = DecisionOut(alert_id=alert_id, status="dismissed", actor=actor, decided_at=now)
        log.info("alert dismissed", extra={"alert_id": alert_id, "actor": actor})
        self._publish(decision, {}, variant_id)
        return decision

    def _publish(self, d: DecisionOut, campaigns: dict[int, str], variant_id: int) -> None:
        if self.bus is not None:
            # `variant_id` lets the engine re-check the product: sibling alerts may now be stale
            self.bus.publish("action", {"alert_id": d.alert_id, "variant_id": variant_id, "status": d.status,
                                        "actor": d.actor,
                                        "executed": [e.type for e in d.actions],
                                        "ad_spend_avoided": d.ledger.ad_spend_avoided if d.ledger else 0})
            for cid, status in campaigns.items():
                self.bus.publish("campaign", {"campaign_id": cid, "status": status, "source": "approval"})
        for cb in self.on_decided:
            try:
                cb(d)
            except Exception:
                log.exception("decision callback failed", extra={"alert_id": d.alert_id})

