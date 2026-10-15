"""Phase 6: approve/dismiss through mock Ads/supplier APIs, savings ledger, delivery (inbox + Telegram)."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import Database
from app.main import create_app
from app.schemas import Facts, OrderIn, OrderItemIn
from app.services.actions import ActionService, AlertNotOpen, MockGoogleAdsApi, MockMetaAdsApi, savings_for
from app.services.ad_simulator import AdSimulator
from app.services.calendar import load_calendar
from app.services.delivery import Delivery, TelegramClient, format_alert
from app.services.engine import Engine
from app.services.inventory import apply_order
from app.services.reporter import ReportResult, template_report
from tests.conftest import TEST_ANCHOR

NOW = TEST_ANCHOR + timedelta(minutes=5)
UK9, UK8 = "SN-FWS-WG-UK9", "SN-FWS-WG-UK8"
CHAT = "424242"


def q(path: Path, sql: str, *args: object) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


class FakeTelegram:
    def __init__(self, updates: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[dict] = []
        self.updates = updates or []
        self._mid = 100

    def send_message(self, chat_id: str, text: str, buttons=None) -> int:  # type: ignore[no-untyped-def]
        self._mid += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "message_id": self._mid})
        return self._mid

    def edit_message(self, chat_id: str, message_id: int, text: str, buttons=None) -> None:  # type: ignore[no-untyped-def]
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "buttons": buttons})

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.answers.append({"id": callback_id, "text": text})

    def get_updates(self, offset, timeout=25):  # type: ignore[no-untyped-def]
        out, self.updates = self.updates, []
        return out


@pytest.fixture()
def world(work_db: Path) -> dict:
    """Seeded copy with a rush on Festive White UK 8/9 -> one open Critical alert."""
    st = get_settings()
    db = Database(str(work_db))
    eng = Engine(db, None, st, load_calendar(st.calendar_file), clock=lambda: NOW)
    eng.recalibrate(NOW)
    for i in range(3):
        apply_order(db, OrderIn(items=[OrderItemIn(sku=UK9, qty=1)]), NOW + timedelta(minutes=i))
    for i in range(5):
        apply_order(db, OrderIn(items=[OrderItemIn(sku=UK8, qty=1)]), NOW + timedelta(minutes=i))
    later = NOW + timedelta(minutes=6)
    eng.clock = lambda: later
    crit = next(d for d in eng.evaluate_variants([16], "order", now=later) if d.severity == "critical")
    svc = ActionService(db, None, clock=lambda: later)
    yield {"db": db, "engine": eng, "actions": svc, "alert_id": crit.alert_id, "facts": crit.facts, "path": work_db,
           "now": later}
    db.dispose()


# --- mock API contracts --------------------------------------------------------------

def test_mock_api_request_shapes_mirror_the_real_contracts(world: dict) -> None:
    from sqlalchemy import select

    from app.models import Campaign

    with world["db"].session() as s:
        g = s.scalar(select(Campaign).where(Campaign.platform == "google_ads").limit(1))
        meta = s.scalar(select(Campaign).where(Campaign.platform == "meta_ads").limit(1))
        pause = MockGoogleAdsApi.pause_request(g)
        budget = MockGoogleAdsApi.budget_request(g, 2500.5)
        assert pause["operations"][0] == {"update": {"resource_name": g.external_ref, "status": "PAUSED"},
                                          "update_mask": "status"}
        assert pause["customer_id"] and g.external_ref.startswith("customers/")
        op = budget["operations"][0]
        assert op["update"]["amount_micros"] == 2_500_500_000 and op["update_mask"] == "amount_micros"
        assert "/campaignBudgets/" in op["update"]["resource_name"]
        assert MockMetaAdsApi.budget_request(meta, 1500)["params"] == {"daily_budget": 150000}  # minor units


# --- approve ----------------------------------------------------------------------

def test_approve_pauses_shifts_reorders_and_writes_ledger(world: dict) -> None:
    path, f = world["path"], world["facts"]
    assert {c.recommended for c in f.campaigns} == {"pause_campaign"}
    budgets_before = dict(q(path, "SELECT id, daily_budget FROM campaigns"))
    in_transit_before = dict(q(path, "SELECT variant_id, in_transit FROM inventory"))

    d = world["actions"].approve(world["alert_id"], "dashboard")
    assert d.status == "approved" and all(a.mock for a in d.actions)
    types = [a.type for a in d.actions]
    assert types.count("pause_campaign") == 2 and "shift_budget" in types and "reorder" in types

    status = dict(q(path, "SELECT id, status FROM campaigns"))
    assert all(status[c.campaign_id] == "paused" for c in f.campaigns)
    for t in f.budget_targets:
        assert dict(q(path, "SELECT id, daily_budget FROM campaigns"))[t.campaign_id] == pytest.approx(
            budgets_before[t.campaign_id] + t.amount)
    for ln in f.reorder:
        assert dict(q(path, "SELECT variant_id, in_transit FROM inventory"))[ln.variant_id] == \
            in_transit_before[ln.variant_id] + ln.qty
        assert q(path, "SELECT restock_eta FROM inventory WHERE variant_id = ?", ln.variant_id)[0][0] is not None

    rows = q(path, "SELECT type, payload_json, response_json FROM actions WHERE alert_id = ?", world["alert_id"])
    assert len(rows) == len(d.actions) and all(json.loads(p)["mock"] for _, p, _ in rows)
    avoided, protected, why = q(path, "SELECT ad_spend_avoided, sales_protected, explanation FROM savings_ledger "
                                      "WHERE alert_id = ?", world["alert_id"])[0]
    assert avoided == pytest.approx(f.wasted_spend_estimate) and avoided > 0
    assert protected == pytest.approx(f.reorder_total * f.price)
    assert "capped at 3 days" in why and "Reorder" not in why.split("Sales protected")[0]
    assert q(path, "SELECT status, decided_by FROM alerts WHERE id = ?", world["alert_id"])[0] == ("approved", "dashboard")

    with pytest.raises(AlertNotOpen):
        world["actions"].approve(world["alert_id"], "dashboard")  # idempotent: no double execution


def test_after_approval_spend_stops_and_alert_does_not_return(world: dict) -> None:
    world["actions"].approve(world["alert_id"], "dashboard")
    sim = AdSimulator(world["db"], None, get_settings(), clock=lambda: world["now"], seed=3)
    ticks = sim.tick(seconds=3600)
    fw_campaigns = {c.campaign_id for c in world["facts"].campaigns}
    assert not fw_campaigns & {t["campaign_id"] for t in ticks}  # paused campaigns no longer spend
    later = world["now"] + timedelta(minutes=10)
    again = world["engine"].evaluate_variants([16], "order", now=later)
    assert not [d for d in again if d.severity == "critical" and d.decision == "alert"]  # no live campaign now


def test_partial_budget_cut_when_only_one_size_is_at_risk(work_db: Path) -> None:
    st = get_settings()
    db = Database(str(work_db))
    eng = Engine(db, None, st, load_calendar(st.calendar_file), clock=lambda: NOW)
    eng.recalibrate(NOW)
    for i in range(3):
        apply_order(db, OrderIn(items=[OrderItemIn(sku=UK9, qty=1)]), NOW + timedelta(minutes=i))
    crit = next(d for d in eng.evaluate_variants([16], "order", now=NOW + timedelta(minutes=4))
                if d.severity == "critical")
    f = crit.facts
    assert {c.recommended for c in f.campaigns} == {"shift_budget"}  # 35% at risk: trim, don't pause
    before = dict(q(work_db, "SELECT id, daily_budget FROM campaigns"))
    ActionService(db, None, clock=lambda: NOW + timedelta(minutes=5)).approve(crit.alert_id, "api")
    after = dict(q(work_db, "SELECT id, daily_budget FROM campaigns"))
    for c in f.campaigns:  # each source gives up its own at-risk share and keeps running
        assert after[c.campaign_id] == pytest.approx(before[c.campaign_id] - c.freed_budget, abs=0.01)
    assert dict(q(work_db, "SELECT id, status FROM campaigns"))[f.campaigns[0].campaign_id] == "active"
    db.dispose()


def test_dismiss_records_decision_and_cooldown_applies(world: dict) -> None:
    d = world["actions"].dismiss(world["alert_id"], "dashboard")
    assert d.status == "dismissed" and not d.actions
    assert q(world["path"], "SELECT COUNT(*) FROM savings_ledger")[0][0] == 0
    again = world["engine"].evaluate_variants([16], "order", now=world["now"] + timedelta(minutes=5))
    crit = next(x for x in again if x.variant_id == 16 and x.severity == "critical")
    assert crit.decision == "suppressed" and "cooldown" in crit.reason


def test_savings_only_count_executed_actions(world: dict) -> None:
    f: Facts = world["facts"]
    assert savings_for(f, ["reorder"])[0] == 0  # no ad action -> no ad spend avoided
    assert savings_for(f, ["pause_campaign"])[1] == 0  # no reorder -> no sales protected
    assert savings_for(f, []) == (0.0, 0.0, "No money-moving action was executed.")


# --- delivery --------------------------------------------------------------------

def _delivery(world: dict, fake: FakeTelegram | None, **st: object) -> Delivery:
    settings = Settings(telegram_bot_token="t" if fake else None, telegram_chat_id=CHAT if fake else None, **st)
    return Delivery(world["db"], settings, world["actions"], client=fake)


def test_inbox_only_when_telegram_not_configured(world: dict) -> None:
    d = _delivery(world, None)
    d.on_report(world["alert_id"], ReportResult(template_report(world["facts"]), "template"))
    assert not d.enabled  # nothing to send; the alert row + report already is the inbox


def test_telegram_sends_critical_with_inline_buttons(world: dict) -> None:
    fake = FakeTelegram()
    d = _delivery(world, fake)
    d.on_report(world["alert_id"], ReportResult(template_report(world["facts"]), "gemini"))
    msg = fake.sent[0]
    assert msg["chat_id"] == CHAT and "CRITICAL" in msg["text"] and "UK 9" in msg["text"]
    assert [b["callback_data"] for b in msg["buttons"][0]] == [f"approve:{world['alert_id']}",
                                                                 f"dismiss:{world['alert_id']}"]
    assert q(world["path"], "SELECT telegram_message_id FROM alerts WHERE id = ?", world["alert_id"])[0][0] == 101


def test_telegram_skips_below_min_severity(world: dict) -> None:
    fake = FakeTelegram()
    d = _delivery(world, fake, telegram_min_severity="critical")
    con = sqlite3.connect(world["path"])
    con.execute("UPDATE alerts SET severity = 'warning' WHERE id = ?", (world["alert_id"],))
    con.commit()
    con.close()
    d.on_report(world["alert_id"], ReportResult(template_report(world["facts"]), "template"))
    assert fake.sent == []


def test_telegram_approve_button_runs_actions_and_edits_message(world: dict) -> None:
    fake = FakeTelegram()
    d = _delivery(world, fake)
    world["actions"].on_decided.append(d.on_decided)
    d.on_report(world["alert_id"], ReportResult(template_report(world["facts"]), "template"))
    fake.updates = [{"update_id": 7, "callback_query": {"id": "cb1", "data": f"approve:{world['alert_id']}",
                                                         "message": {"chat": {"id": int(CHAT)}}}}]
    assert d.poll_once() == 1
    assert q(world["path"], "SELECT status, decided_by FROM alerts WHERE id = ?", world["alert_id"])[0] == \
        ("approved", "telegram")
    assert "Approved" in fake.answers[0]["text"]
    assert fake.edits and "Approved" in fake.edits[0]["text"] and "Ad spend avoided" in fake.edits[0]["text"]
    assert d._offset == 8
    # pressing again is harmless
    fake.updates = [{"update_id": 8, "callback_query": {"id": "cb2", "data": f"approve:{world['alert_id']}",
                                                         "message": {"chat": {"id": int(CHAT)}}}}]
    d.poll_once()
    assert "already approved" in fake.answers[-1]["text"]


def test_telegram_ignores_callbacks_from_other_chats(world: dict) -> None:
    fake = FakeTelegram()
    d = _delivery(world, fake)
    out = d.handle_update({"update_id": 1, "callback_query": {"id": "x", "data": f"approve:{world['alert_id']}",
                                                               "message": {"chat": {"id": 999}}}})
    assert out == "ignored" and fake.answers == []
    assert q(world["path"], "SELECT status FROM alerts WHERE id = ?", world["alert_id"])[0][0] == "open"


def test_telegram_text_is_html_escaped(world: dict) -> None:
    r = template_report(world["facts"]).model_copy(update={"summary": "<b>x</b> & <script>"})
    assert "&lt;script&gt;" in format_alert("critical", r, 1, "gemini") and "<script>" not in format_alert(
        "critical", r, 1, "gemini")


def test_real_telegram_client_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 55}})

    client = TelegramClient("123:ABC", http=httpx.Client(transport=httpx.MockTransport(handler)))
    mid = client.send_message(CHAT, "<b>hi</b>", [[{"text": "Approve", "callback_data": "approve:1"}]])
    assert mid == 55
    assert str(seen[0].url) == "https://api.telegram.org/bot123:ABC/sendMessage"
    body = json.loads(seen[0].content)
    assert body["parse_mode"] == "HTML" and body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve:1"


# --- API ------------------------------------------------------------------------

def test_api_approve_dismiss_and_errors(work_db: Path) -> None:
    fake = FakeTelegram()
    settings = Settings(scheduler_enabled=False, startup_sweep=False, ad_sim_enabled=False,
                        telegram_bot_token="t", telegram_chat_id=CHAT, telegram_mode="webhook",
                        telegram_webhook_secret="s3cret")
    clock = {"t": NOW}
    app = create_app(str(work_db), settings=settings, clock=lambda: clock["t"], telegram_client=fake)
    with TestClient(app) as http:
        app.state.engine.recalibrate(NOW)
        for i in range(3):
            clock["t"] = NOW + timedelta(minutes=i)
            http.post("/orders", json={"items": [{"sku": UK9, "qty": 1}]})
        deadline, crit = time.time() + 10, []
        while time.time() < deadline and not (crit and fake.sent):
            crit = http.get("/alerts", params={"severity": "critical"}).json()
            time.sleep(0.05)
        assert crit and fake.sent, "critical alert should reach the inbox and Telegram"
        aid = crit[0]["id"]
        r = http.post(f"/alerts/{aid}/approve")
        assert r.status_code == 200 and r.json()["ledger"]["ad_spend_avoided"] > 0
        assert http.post(f"/alerts/{aid}/approve").status_code == 409
        assert http.post("/alerts/999999/dismiss").status_code == 404
        savings = http.get("/api/dashboard/savings").json()
        assert savings["stockouts_prevented"] == 1 and savings["actions_executed"] >= 1
        assert any(e.type == "action" for e in app.state.bus.replay_since(0))
        # webhook mode: secret is enforced
        assert http.post("/telegram/webhook", json={}).status_code == 403
        ok = http.post("/telegram/webhook", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
        assert ok.status_code == 200
        assert http.get("/telegram/status").json() == {"enabled": True, "mode": "webhook", "min_severity": "warning"}


# --- open alerts stay fresh -----------------------------------------------------------

def test_open_alert_is_refreshed_when_the_plan_changes(work_db: Path) -> None:
    """3 UK 9 orders -> Critical (35% at risk: trim budgets). Then UK 8 drops too (68% at risk: pause).
    The cooldown blocks a second notification, but the open alert must carry the new plan before approval."""
    st = get_settings()
    db = Database(str(work_db))
    eng = Engine(db, None, st, load_calendar(st.calendar_file), clock=lambda: NOW)
    eng.recalibrate(NOW)
    hooked: list[int] = []
    eng.hooks.append(lambda aid, facts: hooked.append(aid))
    for i in range(3):
        apply_order(db, OrderIn(items=[OrderItemIn(sku=UK9, qty=1)]), NOW + timedelta(minutes=i))
    first = next(d for d in eng.evaluate_variants([16], "order", now=NOW + timedelta(minutes=3))
                 if d.severity == "critical")
    assert {c.recommended for c in first.facts.campaigns} == {"shift_budget"}
    for i in range(5):
        apply_order(db, OrderIn(items=[OrderItemIn(sku=UK8, qty=1)]), NOW + timedelta(minutes=4 + i))
    # an order for UK 8 (variant 15) must also re-check UK 9, which has the open alert
    again = next(d for d in eng.evaluate_variants([15], "order", now=NOW + timedelta(minutes=10))
                 if d.variant_id == 16 and d.severity == "critical")
    assert again.decision == "refreshed" and again.alert_id == first.alert_id
    stored = Facts.model_validate_json(q(work_db, "SELECT facts_json FROM alerts WHERE id = ?", first.alert_id)[0][0])
    assert stored.at_risk_sizes == ["UK 8", "UK 9"] and {c.recommended for c in stored.campaigns} == {"pause_campaign"}
    assert q(work_db, "SELECT COUNT(*) FROM alerts WHERE variant_id = 16 AND severity = 'critical'")[0][0] == 1
    assert hooked.count(first.alert_id) == 2  # report regenerated for the refreshed facts
    # unchanged plan -> plain suppression, no churn
    same = next(d for d in eng.evaluate_variants([16], "order", now=NOW + timedelta(minutes=11))
                if d.variant_id == 16 and d.severity == "critical")
    assert same.decision == "suppressed"
    d = ActionService(db, None, clock=lambda: NOW + timedelta(minutes=12)).approve(first.alert_id, "dashboard")
    assert [a.type for a in d.actions].count("pause_campaign") == 2  # approval runs the fresh plan
    db.dispose()


def test_refreshed_alert_edits_the_telegram_message(world: dict) -> None:
    fake = FakeTelegram()
    d = _delivery(world, fake)
    report = ReportResult(template_report(world["facts"]), "template")
    d.on_report(world["alert_id"], report)
    d.on_report(world["alert_id"], report)  # second report for the same (refreshed) alert
    assert len(fake.sent) == 1 and len(fake.edits) == 1
    assert fake.edits[0]["buttons"] and fake.edits[0]["message_id"] == fake.sent[0]["message_id"]


def test_approval_refreshes_sibling_alerts_through_the_app(work_db: Path) -> None:
    settings = Settings(scheduler_enabled=False, startup_sweep=False, ad_sim_enabled=False)
    clock = {"t": NOW}
    app = create_app(str(work_db), settings=settings, clock=lambda: clock["t"])
    with TestClient(app) as http:
        app.state.engine.recalibrate(NOW)
        app.state.engine.sweep("startup_sweep", now=NOW)  # UK 8 + UK 9 warnings
        for i, sku in enumerate([UK9] * 3 + [UK8] * 5):
            clock["t"] = NOW + timedelta(minutes=i)
            http.post("/orders", json={"items": [{"sku": sku, "qty": 1}]})

        def wait_for(pred, timeout=10.0):  # type: ignore[no-untyped-def]
            end = time.time() + timeout
            while time.time() < end:
                v = pred()
                if v:
                    return v
                time.sleep(0.05)
            return None

        # wait until the open critical has been refreshed by the UK 8 orders (68% of clicks at risk -> pause)
        crit = wait_for(lambda: [a for a in http.get("/alerts", params={"severity": "critical"}).json()
                                 if a["facts"]["at_risk_sizes"] == ["UK 8", "UK 9"]], timeout=20)
        assert crit, "the open critical should pick up UK 8 once it drops"
        clock["t"] = NOW + timedelta(minutes=10)
        assert http.post(f"/alerts/{crit[0]['id']}/approve").status_code == 200

        def uk8_warning_without_live_ads():  # type: ignore[no-untyped-def]
            w = [a for a in http.get("/alerts", params={"severity": "warning"}).json() if a["facts"]["size"] == "UK 8"]
            return w if w and not w[0]["facts"]["campaigns"] else None

        refreshed = wait_for(uk8_warning_without_live_ads, timeout=20)
        state = [(a["id"], a["severity"], a["facts"]["size"], a["status"], len(a["facts"]["campaigns"]))
                 for a in http.get("/alerts", params={"status": ""}).json() if a["severity"] != "opportunity"]
        assert refreshed, f"UK 8 warning should be refreshed: its campaigns were paused by the approval {state}"
        assert refreshed[0]["facts"]["in_transit"] > 0
        # ...and approving UK 9's critical must not spawn a fresh UK 9 warning (its cooldown still applies)
        uk9_open = [a for a in http.get("/alerts").json() if a["facts"]["size"] == "UK 9" and a["severity"] != "opportunity"]
        assert uk9_open == []


def test_approving_one_critical_retires_the_sibling_critical(work_db: Path) -> None:
    """Rush pushes UK 9 AND UK 8 below the ad-pause lag -> two open criticals. Approving one pauses the ads and
    reorders both sizes; the other critical no longer holds (no live ads) and must be expired, not left open
    with a stale plan that would reorder the same stock twice."""
    st = get_settings()
    db = Database(str(work_db))
    eng = Engine(db, None, st, load_calendar(st.calendar_file), clock=lambda: NOW)
    eng.recalibrate(NOW)
    for i, sku in enumerate([UK9] * 3 + [UK8] * 7):
        apply_order(db, OrderIn(items=[OrderItemIn(sku=sku, qty=1)]), NOW + timedelta(minutes=i))
    t = NOW + timedelta(minutes=12)
    eng.evaluate_variants([15, 16], "order", now=t)
    crits = q(work_db, "SELECT id, variant_id FROM alerts WHERE severity = 'critical' AND status = 'open' ORDER BY id")
    assert {v for _, v in crits} == {15, 16}, crits
    ActionService(db, None, clock=lambda: t).approve(crits[-1][0], "dashboard")
    eng.evaluate_variants([crits[-1][1]], "decision", now=t + timedelta(minutes=1))  # what the action event triggers
    other = q(work_db, "SELECT status, decided_by FROM alerts WHERE id = ?", crits[0][0])[0]
    assert other == ("expired", "engine")
    assert q(work_db, "SELECT COUNT(*) FROM alerts WHERE severity = 'critical' AND status = 'open'")[0][0] == 0
    db.dispose()
