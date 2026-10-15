"""Phase 8: demo controls (reset, rush), counterfactual replay, and the full 3-minute demo script end to end."""
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
from app.services.calendar import load_calendar
from app.services.demo import RushIn, allocate
from app.services.forecast import recalibrate
from app.services.replay import run_replay
from tests.conftest import TEST_ANCHOR

NOW = TEST_ANCHOR + timedelta(minutes=5)
UK9, UK8 = "SN-FWS-WG-UK9", "SN-FWS-WG-UK8"
SETTINGS = dict(scheduler_enabled=False, startup_sweep=False, ad_sim_enabled=False)


def q(path: Path, sql: str, *args: object) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def wait_for(pred, timeout: float = 20.0):  # type: ignore[no-untyped-def]
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.1)
    return None


# --- rush planning --------------------------------------------------------------------

def test_allocate_is_exact_and_proportional() -> None:
    out = allocate({"UK 8": 0.5, "UK 9": 0.2, "UK 7": 0.1, "UK 10": 0.1, "UK 11": 0.1}, 10)
    assert out == {"UK 8": 5, "UK 9": 2, "UK 7": 1, "UK 10": 1, "UK 11": 1}
    assert sum(allocate({"a": 1, "b": 1, "c": 1}, 7).values()) == 7


def test_rush_places_orders_and_counts_sold_out(work_db: Path) -> None:
    app = create_app(str(work_db), settings=Settings(**SETTINGS), clock=lambda: NOW)
    with TestClient(app):
        demo = app.state.demo
        plan = demo.plan_rush(RushIn())
        assert plan == demo.plan_rush(RushIn())  # deterministic for a predictable demo
        res = demo.simulate_rush(RushIn(orders=10, duration_seconds=0, weights={"UK 9": 0.6, "UK 8": 0.4}),
                                 background=False)
        assert res["sizes"] == {UK9: 6, UK8: 4}
        st = demo.rush_status
        assert st["placed"] == 8 and st["sold_out"] == 2  # only 4 UK 9 in stock: 2 attempts hit "sold out"
        assert q(work_db, "SELECT on_hand FROM inventory i JOIN variants v ON v.id = i.variant_id WHERE v.sku = ?", UK9)[0][0] == 0
        with pytest.raises(ValueError):
            demo.plan_rush(RushIn(product="Unicorn Slipper"))


# --- reset ----------------------------------------------------------------------------

def test_reset_restores_seeded_state_in_the_live_database(work_db: Path) -> None:
    app = create_app(str(work_db), settings=Settings(**SETTINGS), clock=lambda: NOW)
    with TestClient(app) as http:
        app.state.engine.recalibrate(NOW)
        for _ in range(4):
            http.post("/orders", json={"items": [{"sku": UK9, "qty": 1}]})
        assert wait_for(lambda: q(work_db, "SELECT COUNT(*) FROM alerts")[0][0])
        r = app.state.demo.reset(anchor=TEST_ANCHOR, warm_up=False)
        assert r["festive_white_stock"]["UK 9"] == 4
        # the running app sees the restored data through its existing connections
        cat = http.get("/api/catalog").json()
        fw = next(p for p in cat if p["model"] == "Festive White Sneaker")
        assert {v["size"]: v["available"] for v in fw["variants"]}["UK 9"] == 4
        assert http.get("/alerts").json() == []
        assert any(e.type == "reset" for e in app.state.bus.replay_since(0))


# --- replay -----------------------------------------------------------------------------

def test_counterfactual_replay_against_ground_truth(work_db: Path) -> None:
    st = get_settings()
    db = Database(str(work_db))
    events = load_calendar(st.calendar_file)
    with db.write() as s:
        recalibrate(s, events, TEST_ANCHOR)
    r = run_replay(db, st, events)
    d, wo, wi = r["detection"], r["without_signalshift"], r["with_signalshift"]
    assert d["ground_truth_stockouts"] == 5
    assert d["recall"] == 1.0 and d["precision"] >= 0.8
    assert d["warned_in_advance"] == 5 and d["median_warning_lead_days"] > 3
    assert wo["wasted_on_out_of_stock_clicks"] > 0
    assert wi["ad_spend_saved"] > 0.8 * wo["wasted_on_out_of_stock_clicks"]  # most of the waste is avoided
    assert 0 <= wi["stockouts_prevented"] <= 5 and wi["sales_protected"] <= wo["lost_sales"]
    assert all(g["first_critical"] for g in r["ground_truth"])
    assert any("look-ahead" in a for a in r["assumptions"])
    db.dispose()


# --- the 3-minute demo script, end to end -------------------------------------------------

def test_demo_script_end_to_end(work_db: Path) -> None:
    clock = {"t": NOW}

    def tick() -> object:  # each call moves the clock forward a little, like real time
        clock["t"] += timedelta(seconds=20)
        return clock["t"]

    app = create_app(str(work_db), settings=Settings(**SETTINGS), clock=tick)  # type: ignore[arg-type]
    with TestClient(app) as http:
        # 0. reset to the seeded demo state
        assert http.post("/demo/reset", json={"anchor": TEST_ANCHOR.isoformat()}).status_code == 200
        wait_for(lambda: q(work_db, "SELECT COUNT(*) FROM forecast_factors")[0][0], 30)
        # 1. dashboard: Festive White looks healthy in total, UK 9 nearly gone, campaign live
        fw = next(r for r in http.get("/api/dashboard/inventory").json() if r["model"] == "Festive White Sneaker")
        assert fw["total_available"] == 160 and fw["campaign_live"]
        assert {c["size"]: c["status"] for c in fw["sizes"]}["UK 9"] == "low"
        # 2. buy a pair of UK 9 in the store
        assert http.post("/orders", json={"items": [{"sku": UK9, "qty": 1}], "channel": "store_demo"}).status_code == 201
        # 3. simulate the rush
        assert http.post("/demo/simulate-rush", json={"duration_seconds": 0}).status_code == 200
        assert wait_for(lambda: not app.state.demo.rush_status["running"])
        # 4. a critical alert arrives with a report and a pause recommendation
        crit = wait_for(lambda: [a for a in http.get("/alerts", params={"severity": "critical"}).json()
                                 if a["report"] and "pause_campaign" in [x["type"] for x in a["facts"]["actions"]]])
        assert crit, "expected a critical alert recommending a pause"
        a = crit[0]
        assert a["facts"]["at_risk_click_share"] >= 0.6 and a["report_source"] == "template"
        # 5. approve: campaigns pause (mock API) and the ledger updates
        assert http.post(f"/alerts/{a['id']}/approve").status_code == 200
        camps = {c["campaign_id"]: c["status"] for c in http.get("/api/dashboard/campaigns").json()["campaigns"]}
        assert all(camps[c["campaign_id"]] == "paused" for c in a["facts"]["campaigns"])
        saved = http.get("/api/dashboard/savings").json()["ad_spend_avoided"]
        assert saved > 0
        # 6. ask mode
        ans = http.post("/ask", json={"question": "How much ad spend did we save today?"}).json()
        assert ans["evidence"]["values"]["ad_spend_avoided"] == pytest.approx(saved)
        assert "saved ₹" in ans["answer"]
        # replay endpoint is available too
        rep = http.get("/savings/replay").json()
        assert rep["detection"]["recall"] == 1.0
