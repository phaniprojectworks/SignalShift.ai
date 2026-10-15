"""Phase 4: Gemini report with structured output, guardrails, retry, fallback model and template."""
from __future__ import annotations

import sqlite3
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import Database
from app.main import create_app
from app.schemas import Facts, OrderIn, OrderItemIn, Report, ReportRecommendation
from app.services.calendar import load_calendar
from app.services.engine import Engine
from app.services.guardrails import check_report, check_text
from app.services.inventory import apply_order
from app.services.reporter import SYSTEM_PROMPT, LLMError, Reporter, ReportService, template_report
from tests.conftest import TEST_ANCHOR

NOW = TEST_ANCHOR + timedelta(minutes=5)
UK9, UK8 = "SN-FWS-WG-UK9", "SN-FWS-WG-UK8"
LLM_SETTINGS = dict(gemini_api_key="test-key", gemini_model="model-primary", gemini_fallback_model="model-fallback",
                    scheduler_enabled=False, startup_sweep=False)


GOOD_FROM_PROMPT = object()  # sentinel: answer with a valid report built from the facts embedded in the prompt


class FakeClient:
    """Scripted stand-in for Gemini: each call pops the next response (JSON text, an exception, or
    GOOD_FROM_PROMPT)."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def generate(self, model: str, system: str, prompt: str, schema: dict) -> str:
        self.calls.append({"model": model, "system": system, "prompt": prompt, "schema": schema})
        r = self.responses.pop(0)
        if r is GOOD_FROM_PROMPT:
            facts_json = prompt.split("FACTS:\n", 1)[1].split("\n\nDISPLAY:", 1)[0]
            return _good(Facts.model_validate_json(facts_json))
        if isinstance(r, Exception):
            raise r
        return str(r)


def _copy(src: Path, dst: Path) -> Path:
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    a.backup(b)
    a.close()
    b.close()
    return dst


@pytest.fixture(scope="module")
def scenario(seeded_db: tuple[Path, dict], tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Engine facts for all three severities from the demo data (startup sweep + rush on UK 8/9)."""
    path = _copy(seeded_db[0], tmp_path_factory.mktemp("p4") / "scenario.db")
    st = get_settings()
    db = Database(str(path))
    eng = Engine(db, None, st, load_calendar(st.calendar_file), clock=lambda: NOW)
    eng.recalibrate(NOW)
    decisions = eng.sweep("startup_sweep", now=NOW)
    for i in range(3):
        apply_order(db, OrderIn(items=[OrderItemIn(sku=UK9, qty=1)]), NOW + timedelta(minutes=i))
    for i in range(5):
        apply_order(db, OrderIn(items=[OrderItemIn(sku=UK8, qty=1)]), NOW + timedelta(minutes=i))
    decisions += eng.evaluate_variants([16], "order", now=NOW + timedelta(minutes=6))
    svc = ReportService.for_database(db, None, st, background=False)
    by_sev = {}
    for d in decisions:
        if d.facts is not None:
            by_sev.setdefault(d.severity, d.facts)
    db.dispose()
    return {"facts": by_sev, "all": [d.facts for d in decisions if d.facts is not None],
            "products": svc.reporter.known_products, "campaigns": svc.reporter.known_campaigns}


@pytest.fixture()
def critical(scenario: dict) -> Facts:
    return scenario["facts"]["critical"]


def _reporter(scenario: dict, client: FakeClient | None, **overrides: object) -> Reporter:
    st = Settings(**{**LLM_SETTINGS, **overrides})
    return Reporter(st, client, scenario["products"], scenario["campaigns"])


def _good(facts: Facts) -> str:
    t = template_report(facts)
    return t.model_copy(update={"summary": "Heads up. " + t.summary}).model_dump_json()


def _bad(facts: Facts) -> str:
    t = template_report(facts)
    return t.model_copy(update={"summary": "UK 9 sells out in about 5 hours and wastes ₹9,999 a day."}).model_dump_json()


# --- reporter paths --------------------------------------------------------------

def test_gemini_pass(scenario: dict, critical: Facts) -> None:
    client = FakeClient(_good(critical))
    res = _reporter(scenario, client).build(critical)
    assert res.source == "gemini" and res.report.summary.startswith("Heads up.")
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "model-primary"
    assert call["system"] == SYSTEM_PROMPT and "ONLY numbers" in call["system"]
    assert "FACTS:" in call["prompt"] and "DISPLAY:" in call["prompt"] and '"severity":"critical"' in call["prompt"]
    assert call["schema"]["properties"]["why"]["maxItems"] == 3  # structured output schema from the Report model


def test_fail_then_retry_with_violations_listed(scenario: dict, critical: Facts) -> None:
    client = FakeClient(_bad(critical), _good(critical))
    res = _reporter(scenario, client).build(critical)
    assert res.source == "gemini"
    assert len(client.calls) == 2
    retry_prompt = client.calls[1]["prompt"]
    assert "Problems found" in retry_prompt and "9,999" in retry_prompt and "Previous draft" in retry_prompt
    assert res.attempts[0]["violations"] and res.attempts[1]["ok"]


def test_fails_twice_falls_back_to_template(scenario: dict, critical: Facts) -> None:
    client = FakeClient(_bad(critical), _bad(critical))
    res = _reporter(scenario, client).build(critical)
    assert res.source == "template"
    assert res.report == template_report(critical)
    assert len(client.calls) == 2  # guardrail failure does not burn the fallback model


def test_primary_error_uses_fallback_model(scenario: dict, critical: Facts) -> None:
    client = FakeClient(LLMError("503 overloaded"), _good(critical))
    res = _reporter(scenario, client).build(critical)
    assert res.source == "gemini_fallback_model"
    assert [c["model"] for c in client.calls] == ["model-primary", "model-fallback"]


def test_invalid_json_counts_as_model_error(scenario: dict, critical: Facts) -> None:
    client = FakeClient('{"headline": "x"}', _good(critical))  # schema-invalid -> fallback model
    assert _reporter(scenario, client).build(critical).source == "gemini_fallback_model"


def test_both_models_error_gives_template(scenario: dict, critical: Facts) -> None:
    client = FakeClient(LLMError("timeout"), LLMError("timeout"))
    res = _reporter(scenario, client).build(critical)
    assert res.source == "template" and len(res.attempts) == 2


def test_no_api_key_uses_template_without_calling(scenario: dict, critical: Facts) -> None:
    st = Settings(gemini_api_key=None, gemini_model=None, scheduler_enabled=False)
    res = Reporter(st, None).build(critical)
    assert res.source == "template"
    assert res.attempts[0]["reason"] == "no API key/model"


# --- guardrails ------------------------------------------------------------------

def test_hallucinated_number_is_rejected(scenario: dict, critical: Facts) -> None:
    from app.services.guardrails import allowed_values

    # precondition: nothing in the facts is a 37-hour duration (other sizes' covers are real facts, so a
    # hallucination must pick a number that no fact supports)
    assert not any(36 <= h < 38 for h in allowed_values(critical)["h"])
    v = check_text("UK 9 will sell out in about 37 hours.", critical)
    assert any("'37 hours'" in x for x in v)
    # units are not interchangeable: UK 11's 5% click share must not legitimise "5 hours"
    assert any(sc.click_share == pytest.approx(0.05) for sc in critical.sizes)
    assert check_text("5% of clicks want UK 11.", critical) == []
    assert check_text("about 70% of clicks", critical)  # 68% may not be rounded to 70%
    from app.services.guardrails import _matches

    assert _matches(4.0, [4.3]) and not _matches(5.0, [4.3])  # round/floor allowed, never rounded up
    assert _matches(1800.0, [1768.0]) and not _matches(2000.0, [1768.0])
    report = Report.model_validate_json(_bad(critical))
    violations = check_report(report, critical, scenario["products"], scenario["campaigns"])
    assert any("9,999" in x for x in violations)


def test_units_and_formatting_are_accepted(critical: Facts) -> None:
    cover = critical.hours_of_cover  # 1.1 h in the demo
    budget = critical.campaigns[0].daily_budget
    share = round(critical.at_risk_click_share * 100)
    for text in [f"about {cover} h", f"about {cover} hours", f"roughly {round(cover * 60)} minutes",
                 f"₹{budget:,.0f}/day", f"Rs {budget:,.0f} a day", f"Rs. {budget / 1000:g}k per day",
                 f"{share}% of clicks", f"{share} percent of clicks", f"{critical.available} pair left"]:
        assert check_text(text, critical) == [], text


def test_sizes_products_and_actions_must_exist(scenario: dict, critical: Facts) -> None:
    assert check_text("UK 12 is also low.", critical)
    assert check_text("UK 9 and UK 8 are low.", critical) == []
    products = scenario["products"]
    assert check_text("Move budget to Canvas High-Top.", critical, products)  # not a target in the facts
    assert check_text("Move budget to Everyday Runner.", critical, products) == []  # a facts target
    assert check_text("Retro Runner '86 is doing fine.", critical, products)  # wrong product, digits not counted
    t = template_report(critical)
    bad = t.model_copy(update={"recommendations": [ReportRecommendation(action_type="monitor", text="Wait."),
                                                   *t.recommendations[:1]]})
    assert check_report(bad, critical, products) == []  # "monitor" is always allowed
    critical_no_reorder = critical.model_copy(update={"actions": [a for a in critical.actions if a.type != "reorder"]})
    with_reorder = t.model_copy(update={"recommendations": [ReportRecommendation(action_type="reorder", text="Reorder.")]})
    assert any("reorder" in x for x in check_report(with_reorder, critical_no_reorder, products))


def test_template_reports_pass_guardrails_for_every_alert(scenario: dict) -> None:
    assert {"critical", "warning", "opportunity"} <= set(scenario["facts"])
    for facts in scenario["all"]:
        report = template_report(facts)
        assert check_report(report, facts, scenario["products"], scenario["campaigns"]) == [], facts.product_name
        assert 2 <= len(report.why) <= 3


def test_critical_template_reads_like_an_assistant(critical: Facts) -> None:
    r = template_report(critical)
    assert "UK 9" in r.headline and "1 pair left" in r.summary
    assert "68%" in r.summary and "UK 8 and UK 9" in r.summary
    assert [x.action_type for x in r.recommendations] == [a.type for a in critical.actions]
    assert "₹" in r.estimated_impact


# --- persistence + app wiring ----------------------------------------------------------

def test_report_service_saves_and_indexes(work_db: Path, critical: Facts) -> None:
    db = Database(str(work_db))
    con = sqlite3.connect(work_db)
    con.execute("INSERT INTO alerts (id, ts, variant_id, severity, facts_json, status) VALUES (9001, ?, 16, 'critical', ?, 'open')",
                (NOW.strftime("%Y-%m-%d %H:%M:%S.%f"), critical.model_dump_json()))
    con.execute("INSERT INTO alert_fts (alert_id, headline, report_text, facts_text) VALUES (9001, 'h', '', 'f')")
    con.commit()
    client = FakeClient(_good(critical))
    svc = ReportService.for_database(db, None, Settings(**LLM_SETTINGS), client=client, background=False)
    res = svc.on_alert(9001, critical)
    assert res.source == "gemini"
    row = con.execute("SELECT report_source, report_text FROM alerts WHERE id = 9001").fetchone()
    assert row[0] == "gemini" and Report.model_validate_json(row[1]).summary.startswith("Heads up.")
    assert con.execute("SELECT COUNT(*) FROM alert_fts WHERE alert_fts MATCH 'heads'").fetchone()[0] == 1
    con.close()
    db.dispose()


def test_app_attaches_gemini_report_to_new_alert(work_db: Path) -> None:
    client = FakeClient(*[GOOD_FROM_PROMPT] * 50)
    clock = {"t": NOW}
    app = create_app(str(work_db), settings=Settings(**LLM_SETTINGS), clock=lambda: clock["t"], llm_client=client)
    with TestClient(app) as http:
        app.state.engine.recalibrate(NOW)
        for i in range(3):
            clock["t"] = NOW + timedelta(minutes=i)
            http.post("/orders", json={"items": [{"sku": UK9, "qty": 1}]})
        deadline, alert = time.time() + 15, None
        while time.time() < deadline:
            alerts = http.get("/alerts", params={"severity": "critical"}).json()
            if alerts and alerts[0]["report_source"] == "gemini":
                alert = alerts[0]
                break
            time.sleep(0.1)
        assert alert is not None, "critical alert never got a Gemini report"
        assert alert["report"]["summary"].startswith("Heads up.")
        assert any(e.type == "report" for e in app.state.bus.replay_since(0))

