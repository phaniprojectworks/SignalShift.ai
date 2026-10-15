"""Phase 3: forecast, engine rules, cooldown, triggers."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import Database
from app.main import create_app
from app.schemas import Facts, OrderIn, OrderItemIn, SizeCover
from app.services.calendar import load_calendar
from app.services.engine import (
    BudgetCandidate,
    Engine,
    PriorAlert,
    campaign_recommendation,
    classify_severity,
    click_share_split,
    cooldown_decision,
    hours_of_cover,
    rank_budget_targets,
    reorder_qty,
    restock_deadline,
    size_status,
    wasted_spend,
)
from app.services.forecast import band_pct, cover_days, ewma_rate, fit_campaign_factors, fit_scale, load_factors
from app.services.inventory import apply_order
from app.services.triggers import build_scheduler, daily_briefing
from tests.conftest import TEST_ANCHOR

NOW = TEST_ANCHOR + timedelta(minutes=5)
UK9, UK8 = "SN-FWS-WG-UK9", "SN-FWS-WG-UK8"


# =============================================================================
# Pure rule functions
# =============================================================================

def test_hours_of_cover() -> None:
    assert hours_of_cover(4, 0.5) == 8.0
    assert hours_of_cover(0, 0.5) == 0.0
    assert hours_of_cover(-2, 0.5) == 0.0
    assert hours_of_cover(5, 0.0) is None


def test_size_status() -> None:
    assert size_status(0, 0.0, 24) == "out"
    assert size_status(3, 10.0, 24) == "low"
    assert size_status(30, 100.0, 24) == "ok"
    assert size_status(3, None, 24) == "ok"  # no demand -> no risk


def test_restock_deadline() -> None:
    now = datetime(2026, 10, 15, 10)
    eta = now + timedelta(days=3)
    assert restock_deadline(now, eta, 20, 10, 2) == (eta, False)
    assert restock_deadline(now, now - timedelta(days=1), 20, 10, 2) == (now + timedelta(days=2), True)
    assert restock_deadline(now, None, 0, 10, 2) == (now + timedelta(days=12), False)
    assert restock_deadline(now, eta, 0, 10, 2) == (now + timedelta(days=12), False)  # ETA without stock is ignored


def test_severity_rules() -> None:
    now = datetime(2026, 10, 15, 10)
    deadline = now + timedelta(days=12)
    eta = lambda h: now + timedelta(hours=h)  # noqa: E731
    assert classify_severity(3.0, True, eta(3), deadline, 4)[0] == "critical"
    assert classify_severity(4.0, True, eta(4), deadline, 4)[0] == "warning"  # boundary is not critical
    assert classify_severity(3.0, False, eta(3), deadline, 4)[0] == "warning"  # no campaign -> not critical
    assert classify_severity(400.0, True, eta(400), deadline, 4)[0] is None
    assert classify_severity(None, True, None, deadline, 4)[0] is None
    assert classify_severity(0.0, True, now, deadline, 4)[0] == "critical"  # already out while ads run


def _sc(size: str, status: str, share: float | None) -> SizeCover:
    return SizeCover(variant_id=1, size=size, available=1, in_transit=0, demand_per_hr=1, hours_of_cover=1,
                     click_share=share, status=status)  # type: ignore[arg-type]


def test_size_aware_click_share() -> None:
    sizes = [_sc("UK 7", "ok", 0.12), _sc("UK 8", "low", 0.33), _sc("UK 9", "out", 0.35), _sc("UK 10", "ok", 0.20)]
    at_risk, healthy, names = click_share_split(sizes)
    assert at_risk == pytest.approx(0.68)
    assert healthy == pytest.approx(0.32)
    assert names == ["UK 8", "UK 9"]
    assert click_share_split([_sc("UK 9", "ok", None)]) == (0.0, 0.0, [])


def test_wasted_spend_uses_size_click_share() -> None:
    per_hr, total = wasted_spend(10, 12, [(0.35, 2.0), (0.33, 10.0)], horizon_hours=72)
    assert per_hr == pytest.approx(10 * 12 * 0.68)
    assert total == pytest.approx(10 * 12 * 0.35 * 70 + 10 * 12 * 0.33 * 62)
    assert wasted_spend(10, 12, [(0.5, 100.0)], horizon_hours=72)[1] == 0.0  # sells out after the horizon


def test_campaign_recommendation() -> None:
    assert campaign_recommendation(0.68, 2600, 0.6) == ("pause_campaign", 2600)
    rec, freed = campaign_recommendation(0.35, 2600, 0.6)
    assert rec == "shift_budget" and freed == pytest.approx(910)
    assert campaign_recommendation(0.0, 2600, 0.6) == ("none", 0.0)


def _cand(pid: int, roas: float, margin: float, cover: float, budget: float = 2000, at_risk: float = 0.0,
          required: float = 0.0) -> BudgetCandidate:
    return BudgetCandidate(product_id=pid, product_name=f"P{pid}", campaign_id=pid, campaign_name=f"C{pid}",
                           current_budget=budget, cover_days=cover, expected_roas=roas, margin_pct=margin,
                           at_risk_click_share=at_risk, required_cover_days=required, cover_days_on_hand=cover)


def test_budget_shift_ranking_and_split() -> None:
    cands = [
        _cand(1, roas=10, margin=0.5, cover=30),             # score 5.0
        _cand(2, roas=20, margin=0.5, cover=30),             # score 10.0 -> first
        _cand(3, roas=50, margin=0.5, cover=5),              # best score but too little cover
        _cand(4, roas=40, margin=0.5, cover=30, at_risk=0.4),  # its own sizes are running low
        _cand(5, roas=40, margin=0.5, cover=20, required=26),  # would run short through the festival
    ]
    targets = rank_budget_targets(cands, freed=3000, min_cover_days=14, max_targets=2)
    assert [t.product_id for t in targets] == [2, 1]
    assert [t.amount for t in targets] == [2000, 1000]  # 3000 split 2:1, rounded to 100
    assert targets[0].score == pytest.approx(10.0)


def test_budget_shift_caps_and_edge_cases() -> None:
    capped = rank_budget_targets([_cand(1, 10, 0.5, 30, budget=500)], freed=4000, min_cover_days=14, max_targets=2)
    assert capped[0].amount == 500  # at most +100% of the current budget
    assert rank_budget_targets([_cand(1, 10, 0.5, 30)], freed=0, min_cover_days=14, max_targets=2) == []
    assert rank_budget_targets([_cand(1, 10, 0.5, 3)], freed=1000, min_cover_days=14, max_targets=2) == []


def test_cooldown_rules() -> None:
    now = datetime(2026, 10, 15, 12)
    warn = PriorAlert("warning", 15.0, now + timedelta(hours=4), now - timedelta(hours=2))
    crit = PriorAlert("critical", 3.0, now + timedelta(hours=5), now - timedelta(hours=1))
    expired = PriorAlert("critical", 3.0, now - timedelta(minutes=1), now - timedelta(hours=7))
    assert cooldown_decision("warning", 14.0, [], now, 0.3)[0] is True
    assert cooldown_decision("warning", 14.0, [warn], now, 0.3)[0] is False  # same severity, small drop
    assert cooldown_decision("warning", 9.0, [warn], now, 0.3)[0] is True  # cover dropped 40%
    assert cooldown_decision("critical", 3.0, [warn], now, 0.3) == (True, "escalation from warning")
    assert cooldown_decision("warning", 1.0, [crit], now, 0.3)[0] is False  # de-escalation stays quiet
    # regression: an older same-severity alert must not let a lower level through while a higher one cools down
    assert cooldown_decision("warning", 1.2, [warn, crit], now, 0.3)[0] is False
    assert cooldown_decision("critical", 2.5, [crit], now, 0.3)[0] is False  # 17% drop only
    assert cooldown_decision("critical", 3.0, [expired], now, 0.3)[0] is True
    assert cooldown_decision("opportunity", None, [PriorAlert("opportunity", 10, now + timedelta(hours=1), now)],
                             now, 0.3)[0] is False


def test_reorder_qty() -> None:
    assert reorder_qty(100.2, 20, 30) == 51
    assert reorder_qty(10, 20, 0) == 0
    assert reorder_qty(100, 0, 0, safety_pct=0.1) == 110


# =============================================================================
# Pure forecast functions
# =============================================================================

def test_ewma_rate() -> None:
    assert ewma_rate([2.0] * 50, 24) == pytest.approx(2.0)
    assert ewma_rate([], 24) == 0.0
    rising = ewma_rate([0.0] * 48 + [4.0] * 4, 24)
    assert 0 < rising < 4.0
    assert ewma_rate([0.0] * 48 + [4.0] * 4, 6) > rising  # shorter half-life reacts faster


def test_band_and_cover_days_and_fits() -> None:
    assert band_pct([10, 10, 10, 10]) == 0.15  # clamped minimum
    assert band_pct([1, 30, 2, 25]) == 0.5  # clamped maximum
    assert cover_days(25, [10, 10, 10]) == pytest.approx(2.5)
    assert cover_days(1000, [10, 10]) == 2.0
    assert fit_scale([1.0, 2.0], [1.1, 2.2]) == pytest.approx(1.1)
    rng = np.random.default_rng(1)
    active = np.zeros((60, 1))
    active[30:, 0] = 1.0
    y = np.log(10) + np.log(1.4) * active[:, 0] + rng.normal(0, 0.05, 60)
    assert fit_campaign_factors(y, active)[0] == pytest.approx(1.4, rel=0.05)
    assert fit_campaign_factors(y, np.ones((60, 1)))[0] is None  # always on -> not identifiable


# =============================================================================
# Integration on the seeded demo data
# =============================================================================

@pytest.fixture()
def engine(work_db: Path) -> Engine:
    st = get_settings()
    db = Database(str(work_db))
    eng = Engine(db, None, st, load_calendar(st.calendar_file), clock=lambda: NOW)
    eng.recalibrate(NOW)
    yield eng
    db.dispose()


def _vid(path: Path, sku: str) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT id FROM variants WHERE sku = ?", (sku,)).fetchone()[0]
    finally:
        con.close()


def test_recalibration_learns_factors(engine: Engine, work_db: Path) -> None:
    with engine.db.session() as s:
        f = load_factors(s)
    assert 0.7 <= f["seasonal_scale"]["festive_hero"] <= 1.5
    # Festive White's two campaigns were generated with a combined uplift of 1.35 x 1.15 = 1.55
    con = sqlite3.connect(work_db)
    ids = [r[0] for r in con.execute("SELECT c.id FROM campaigns c JOIN products p ON p.id = c.product_id "
                                     "WHERE p.model = 'Festive White Sneaker'")]
    con.close()
    combined = np.prod([f["campaign"][str(i)] for i in ids])
    assert 1.3 <= combined <= 1.9
    mix = f["size_mix"]
    assert mix[str(_vid(work_db, UK8))] + mix[str(_vid(work_db, UK9))] == pytest.approx(0.55, abs=0.06)


def test_demo_state_initially_warning_not_critical(engine: Engine, work_db: Path) -> None:
    decisions = engine.sweep("startup_sweep", now=NOW)
    alerts = [d for d in decisions if d.decision == "alert"]
    assert not [d for d in alerts if d.severity == "critical"]
    uk9 = next(d for d in decisions if d.variant_id == _vid(work_db, UK9) and d.severity != "opportunity")
    assert uk9.severity == "warning"
    assert 8 <= uk9.facts.hours_of_cover <= 24
    opportunities = [d for d in alerts if d.severity == "opportunity"]
    assert 1 <= len(opportunities) <= 10
    assert any("Festive White" in d.facts.product_name and d.facts.event.name == "Diwali" for d in opportunities)


def test_critical_for_festive_white_uk9_after_a_few_orders(engine: Engine, work_db: Path) -> None:
    engine.sweep("startup_sweep", now=NOW)
    for i in range(3):
        apply_order(engine.db, OrderIn(items=[OrderItemIn(sku=UK9, qty=1)]), NOW + timedelta(minutes=i))
    later = NOW + timedelta(minutes=4)
    decisions = engine.evaluate_variants([_vid(work_db, UK9)], "order", now=later)
    crit = next(d for d in decisions if d.severity == "critical")
    assert crit.decision == "alert" and "escalation" in crit.reason
    f: Facts = crit.facts
    assert f.available == 1 and f.hours_of_cover < get_settings().ad_pause_lag_hours
    assert "velocity_spike" in f.anomalies  # 3 pairs in minutes vs a forecast of ~0.25/h
    assert f.hours_of_cover_low <= f.hours_of_cover <= f.hours_of_cover_high
    assert "UK 9" in f.at_risk_sizes and f.campaigns
    assert f.wasted_spend_per_hr > 0 and f.wasted_spend_estimate > 0
    types = [a.type for a in f.actions]
    assert "reorder" in types and ("pause_campaign" in types or "shift_budget" in types)
    assert f.reorder_total and f.reorder_total > 0

    con = sqlite3.connect(work_db)
    row = con.execute("SELECT severity, status, cooldown_until, facts_json FROM alerts WHERE id = ?",
                      (crit.alert_id,)).fetchone()
    assert row[0] == "critical" and row[1] == "open" and row[2] is not None
    assert Facts.model_validate_json(row[3]).severity == "critical"
    assert con.execute("SELECT COUNT(*) FROM alert_fts WHERE alert_fts MATCH 'pause OR reorder'").fetchone()[0] >= 1
    # the earlier warning for the same variant is superseded
    assert con.execute("SELECT COUNT(*) FROM alerts WHERE variant_id = ? AND severity = 'warning' AND status = 'open'",
                       (_vid(work_db, UK9),)).fetchone()[0] == 0
    con.close()

    # cooldown: evaluating again straight away does not create a duplicate
    again = engine.evaluate_variants([_vid(work_db, UK9)], "order", now=later + timedelta(minutes=1))
    assert next(d for d in again if d.variant_id == _vid(work_db, UK9) and d.severity == "critical").decision \
        == "suppressed"


def test_rush_on_uk8_and_uk9_pauses_campaigns_and_shifts_to_everyday_runner(engine: Engine, work_db: Path) -> None:
    for i in range(3):
        apply_order(engine.db, OrderIn(items=[OrderItemIn(sku=UK9, qty=1)]), NOW + timedelta(minutes=i))
    for i in range(5):
        apply_order(engine.db, OrderIn(items=[OrderItemIn(sku=UK8, qty=1)]), NOW + timedelta(minutes=i))
    d = next(x for x in engine.evaluate_variants([_vid(work_db, UK9)], "order", now=NOW + timedelta(minutes=6))
             if x.severity == "critical")
    f = d.facts
    assert f.at_risk_sizes == ["UK 8", "UK 9"]
    assert f.at_risk_click_share == pytest.approx(0.68, abs=0.01)
    assert {c.recommended for c in f.campaigns} == {"pause_campaign"}
    assert f.freed_budget == sum(c.daily_budget for c in f.campaigns)
    assert f.budget_targets[0].product_name == "Everyday Runner — Black"
    assert 18 <= f.budget_targets[0].cover_days_on_hand <= 26
    assert f.budget_moved + f.budget_held_back == pytest.approx(f.freed_budget)


def test_campaign_activation_and_delayed_restock_triggers(engine: Engine, work_db: Path) -> None:
    con = sqlite3.connect(work_db)
    cid = con.execute("SELECT c.id FROM campaigns c JOIN products p ON p.id = c.product_id "
                      "WHERE p.model = 'Festive White Sneaker' AND c.platform = 'google_ads'").fetchone()[0]
    some = con.execute("SELECT variant_id FROM inventory WHERE in_transit > 0 LIMIT 1").fetchone()[0]
    con.execute("UPDATE inventory SET restock_eta = ? WHERE variant_id = ?",
                ((NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"), some))
    con.commit()
    con.close()
    ds = engine.campaign_activated(cid, now=NOW)
    uk9 = next(d for d in ds if d.variant_id == _vid(work_db, UK9) and d.severity != "opportunity")
    assert uk9.decision == "alert" and "campaign_switched_on" in uk9.facts.anomalies
    delayed = engine.delayed_restocks(now=NOW)
    assert [d.variant_id for d in delayed if d.severity != "opportunity"] == [some]


def test_scheduler_jobs_and_briefing(engine: Engine) -> None:
    sched = build_scheduler(engine, None, get_settings())
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {"hourly_refresh", "daily_briefing", "nightly_recalibration"}
    assert "hour='8'" in str(jobs["daily_briefing"].trigger)
    assert "minute='0'" in str(jobs["hourly_refresh"].trigger)
    engine.clock = lambda: NOW
    summary = daily_briefing(engine, None)
    assert summary["counts"].get("warning", 0) >= 1 and summary["top"]


def test_order_event_raises_critical_alert_through_the_app(work_db: Path) -> None:
    settings = Settings(scheduler_enabled=False, startup_sweep=False)
    clock_now = {"t": NOW}
    app = create_app(str(work_db), settings=settings, clock=lambda: clock_now["t"])
    with TestClient(app) as client:
        app.state.engine.recalibrate(NOW)
        for i in range(3):
            clock_now["t"] = NOW + timedelta(minutes=i)
            assert client.post("/orders", json={"items": [{"sku": UK9, "qty": 1}]}).status_code == 201
        deadline = time.time() + 10
        crit = []
        while time.time() < deadline and not crit:
            crit = [e for e in app.state.bus.replay_since(0) if e.type == "alert" and e.data["severity"] == "critical"]
            time.sleep(0.05)
        assert crit, "no critical alert event published"
        alerts = client.get("/alerts", params={"severity": "critical"}).json()
        assert alerts and alerts[0]["facts"]["size"] == "UK 9"
        assert client.get(f"/alerts/{alerts[0]['id']}").json()["severity"] == "critical"
        assert client.get("/alerts/999999").status_code == 404


def test_evaluation_reads_clock_after_waiting_for_engine_lock(engine: Engine, work_db: Path) -> None:
    """Regression: an evaluation queued behind a long sweep must not use the time it was queued at,
    or orders committed meanwhile fall outside the surge window and a spike goes unnoticed."""
    import threading

    clock = {"t": NOW}
    engine.clock = lambda: clock["t"]
    vid = _vid(work_db, UK9)
    result: list = []
    engine._lock.acquire()  # simulate a sweep in progress
    try:
        worker = threading.Thread(target=lambda: result.extend(engine.evaluate_variants([vid], "order")))
        worker.start()
        time.sleep(0.2)  # the worker is now blocked on the lock
        for i in range(3):
            apply_order(engine.db, OrderIn(items=[OrderItemIn(sku=UK9, qty=1)]), NOW + timedelta(minutes=i + 1))
        clock["t"] = NOW + timedelta(minutes=5)
    finally:
        engine._lock.release()
    worker.join(10)
    d = next(x for x in result if x.variant_id == vid and x.severity != "opportunity")
    assert d.severity == "critical"
    assert "velocity_spike" in d.facts.anomalies and d.facts.forecast.recent_rate_per_hr == pytest.approx(1.5)
