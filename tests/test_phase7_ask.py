"""Phase 7: Ask mode - routing, whitelisted templates, retrieval, guardrailed answers, logging."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import Database
from app.main import create_app
from app.schemas import OrderIn, OrderItemIn
from app.services.actions import ActionService
from app.services.ask import AskService, fts_query, keyword_plan, normalize_size, period_range, resolve_product
from app.services.calendar import load_calendar
from app.services.engine import Engine
from app.services.inventory import apply_order
from app.services.reporter import LLMError
from tests.conftest import TEST_ANCHOR

NOW = TEST_ANCHOR + timedelta(minutes=5)
UK9, UK8 = "SN-FWS-WG-UK9", "SN-FWS-WG-UK8"


def q(path: Path, sql: str, *args: object) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


# --- routing (pure) -----------------------------------------------------------------

@pytest.mark.parametrize("question,route,template,extra", [
    ("How many Festive White UK 9 did we sell today?", "numbers", "units_sold", {"size": "UK 9", "period": "today"}),
    ("Which products will run out this week?", "numbers", "stockout_forecast", {"horizon_days": 7}),
    ("How much ad spend did we save this week?", "numbers", "savings", {"period": "this_week"}),
    ("How much ad spend did we save today?", "numbers", "savings", {"period": "today"}),
    ("Why did you recommend pausing the white sneaker campaign?", "why", "none", {}),
    ("What was revenue yesterday?", "numbers", "revenue", {"period": "yesterday"}),
    ("How much did we spend on ads today?", "numbers", "ad_spend", {"period": "today"}),
    ("Any critical alerts?", "numbers", "open_alerts", {"severity": "critical"}),
    ("Top sellers this month", "numbers", "top_products", {"period": "this_month"}),
    ("Tell me a joke", "unsupported", "none", {}),
])
def test_keyword_router(question: str, route: str, template: str, extra: dict) -> None:
    p = keyword_plan(question)
    assert (p.route, p.template) == (route, template)
    for k, v in extra.items():
        assert getattr(p, k) == v, k


def test_periods_include_records_stamped_now() -> None:
    now = datetime(2026, 10, 15, 10, 17)  # a Thursday
    a, b = period_range("today", now)
    assert a <= now < b
    a, b = period_range("this_week", now)
    assert a == datetime(2026, 10, 12) and now < b
    assert period_range("yesterday", now) == (datetime(2026, 10, 14), datetime(2026, 10, 15))
    assert normalize_size("uk9") == "UK 9" and normalize_size("9") == "UK 9" and normalize_size(None) is None


def test_fts_query_is_sanitized() -> None:
    assert fts_query('pause" OR 1=1; DROP TABLE alerts; --') == '"pause" OR "drop" OR "table" OR "alerts"'
    assert fts_query("why?") is None


# --- scenario: rush on UK 8/9, critical approved ----------------------------------------

@pytest.fixture()
def world(work_db: Path) -> dict:
    st = get_settings()
    db = Database(str(work_db))
    eng = Engine(db, None, st, load_calendar(st.calendar_file), clock=lambda: NOW)
    eng.recalibrate(NOW)
    eng.sweep("startup_sweep", now=NOW)
    for i, sku in enumerate([UK9] * 3 + [UK8] * 5):
        apply_order(db, OrderIn(items=[OrderItemIn(sku=sku, qty=1)]), NOW + timedelta(minutes=i))
    later = NOW + timedelta(minutes=10)
    eng.clock = lambda: later
    eng.evaluate_variants([15, 16], "order", now=later)
    crit = q(work_db, "SELECT id FROM alerts WHERE severity = 'critical' AND status = 'open'")[0][0]
    ActionService(db, None, clock=lambda: later).approve(crit, "dashboard")
    svc = AskService(db, eng, None, [], clock=lambda: later)
    yield {"db": db, "svc": svc, "path": work_db, "crit": crit, "now": later}
    db.dispose()


def test_resolve_product(world: dict) -> None:
    with world["db"].session() as s:
        assert resolve_product(s, "white sneaker").label == "Festive White Sneaker — White/Gold"
        assert resolve_product(s, "How many Festive White UK 9?").label == "Festive White Sneaker — White/Gold"
        retro = resolve_product(s, "retro runner")
        assert len(retro.product_ids) == 3 and "all colourways" in retro.label  # tie: include all, don't guess
        assert len(resolve_product(s, "retro runner sail").product_ids) == 1
        assert resolve_product(s, "unicorn") is None


def test_units_sold_matches_database(world: dict) -> None:
    r = world["svc"].ask("How many Festive White UK 9 did we sell today?")
    assert r.route == "numbers:units_sold" and r.answer_source == "template" and r.plan_source == "keywords"
    day = world["now"].strftime("%Y-%m-%d")
    units = q(world["path"], "SELECT SUM(oi.qty) FROM order_items oi JOIN variants v ON v.id = oi.variant_id "
                             "WHERE v.sku = ? AND substr(oi.ts,1,10) = ?", UK9, day)[0][0]
    assert r.evidence["values"]["units"] == units and f"sold {units} pairs" in r.answer
    assert r.evidence["type"] == "sql_template" and r.evidence["params"]["size"] == "UK 9"


def test_stockout_forecast_lists_at_risk_sizes(world: dict) -> None:
    r = world["svc"].ask("Which products will run out this week?")
    rows = r.evidence["rows"]
    assert rows and all(x["hours_of_cover"] < 7 * 24 for x in rows)
    assert {"UK 8", "UK 9"} <= {x["size"] for x in rows if x["product"].startswith("Festive White")}
    assert "Festive White Sneaker" in r.answer


def test_savings_today_after_approval(world: dict) -> None:
    r = world["svc"].ask("How much ad spend did we save today?")
    avoided = q(world["path"], "SELECT SUM(ad_spend_avoided) FROM savings_ledger")[0][0]
    assert avoided > 0 and r.evidence["values"]["ad_spend_avoided"] == pytest.approx(avoided)
    assert "saved ₹" in r.answer and r.evidence["values"]["approved_alerts"] == 1


def test_why_answers_from_retrieved_alerts(world: dict) -> None:
    r = world["svc"].ask("Why did you recommend pausing the white sneaker campaign?")
    assert r.route == "why" and r.evidence["type"] == "alert_retrieval"
    assert r.evidence["alert_ids"][0] == world["crit"]  # the approved critical about Festive White
    assert f"#{world['crit']}" in r.answer and "Pause" in r.answer and "You approved it" in r.answer


def test_every_question_is_logged_with_evidence(world: dict) -> None:
    world["svc"].ask("What is our revenue today?")
    world["svc"].ask("Tell me a joke")
    rows = q(world["path"], "SELECT question, route, evidence_json FROM ask_log ORDER BY id")
    assert [r[1] for r in rows] == ["numbers:revenue", "unsupported"]
    assert json.loads(rows[0][2])["template"] == "revenue"


def test_sql_injection_attempt_is_harmless(world: dict) -> None:
    before = q(world["path"], "SELECT COUNT(*) FROM alerts")[0][0]
    for evil in ["Why did you pause'; DROP TABLE alerts; --", 'How many "UK 9" OR 1=1 -- did we sell today?']:
        world["svc"].ask(evil)
    assert q(world["path"], "SELECT COUNT(*) FROM alerts")[0][0] == before


# --- Gemini paths (mocked) ----------------------------------------------------------

class FakeLLM:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def generate(self, model: str, system: str, prompt: str, schema: dict) -> str:
        self.calls.append({"model": model, "system": system, "prompt": prompt, "schema": schema})
        r = self.responses.pop(0)
        if callable(r):
            r = r(prompt)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


def _svc(world: dict, llm: FakeLLM) -> AskService:
    return AskService(world["db"], world["svc"].engine, llm, [("m1", "gemini"), ("m2", "gemini_fallback_model")],
                      clock=lambda: world["now"])


def test_gemini_picks_template_and_phrases_answer(world: dict) -> None:
    def answer_from_evidence(prompt: str) -> dict:
        ev = json.loads(prompt.split("EVIDENCE:\n", 1)[1])
        return {"answer": f"Today you avoided ₹{ev['values']['ad_spend_avoided']:,.0f} of wasted ad spend."}

    llm = FakeLLM({"route": "numbers", "template": "savings", "period": "today"}, answer_from_evidence)
    r = _svc(world, llm).ask("what did signalshift save us today")
    assert r.plan_source == "gemini" and r.route == "numbers:savings" and r.answer_source == "gemini"
    assert llm.calls[0]["schema"]["properties"]["template"]  # structured output: enum of whitelisted templates
    assert "SELECT" not in llm.calls[0]["system"]  # the model never sees or writes SQL


def test_hallucinated_number_in_answer_falls_back_to_template(world: dict) -> None:
    llm = FakeLLM({"route": "numbers", "template": "savings", "period": "today"},
                  {"answer": "You saved ₹99,999 today."})
    r = _svc(world, llm).ask("savings today?")
    assert r.answer_source == "template" and "99,999" not in r.answer


def test_invalid_plan_falls_back_to_keywords(world: dict) -> None:
    # not a whitelisted template -> rejected; fallback model down -> keyword router; answer models down -> template
    llm = FakeLLM({"route": "numbers", "template": "drop_tables"}, LLMError("down"), LLMError("down"), LLMError("down"))
    r = _svc(world, llm).ask("How much ad spend did we save today?")
    assert r.plan_source == "keywords" and r.route == "numbers:savings"


# --- API -----------------------------------------------------------------------------

def test_ask_api(work_db: Path) -> None:
    app = create_app(str(work_db), settings=Settings(scheduler_enabled=False, startup_sweep=False, ad_sim_enabled=False),
                     clock=lambda: NOW)
    with TestClient(app) as http:
        r = http.post("/ask", json={"question": "What is our revenue today?"})
        assert r.status_code == 200 and r.json()["route"] == "numbers:revenue"
        assert http.post("/ask", json={"question": "?"}).status_code == 422
        assert http.get("/ask/log").json()[0]["route"] == "numbers:revenue"
        assert "ask-form" in http.get("/dashboard").text
