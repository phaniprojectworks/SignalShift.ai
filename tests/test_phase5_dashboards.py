"""Phase 5: dashboard APIs, mock live ad delivery, stock snapshot."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import Database
from app.main import create_app
from app.services.ad_simulator import AdSimulator
from app.services.calendar import load_calendar
from app.services.engine import Engine
from tests.conftest import TEST_ANCHOR

NOW = TEST_ANCHOR + timedelta(minutes=5)
UK9 = "SN-FWS-WG-UK9"


@pytest.fixture()
def client(work_db: Path):
    settings = Settings(scheduler_enabled=False, startup_sweep=False, ad_sim_enabled=False)
    app = create_app(str(work_db), settings=settings, clock=lambda: NOW)
    with TestClient(app) as c:
        app.state.engine.recalibrate(NOW)
        yield c


def _q(path: Path, sql: str, *args: object) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def test_sales_overview_matches_database(client: TestClient, work_db: Path) -> None:
    d = client.get("/api/dashboard/sales").json()
    day = NOW.strftime("%Y-%m-%d")
    orders, revenue = _q(work_db, "SELECT COUNT(*), SUM(total) FROM orders WHERE substr(ts,1,10) = ?", day)[0]
    assert d["today"]["orders"] == orders and d["today"]["revenue"] == pytest.approx(revenue)
    assert len(d["by_hour_today"]) == 24 and len(d["by_hour_yesterday"]) == 24
    assert sum(p["orders"] for p in d["by_hour_today"]) == orders
    revs = [p["revenue"] for p in d["top_products"]]
    assert revs == sorted(revs, reverse=True)


def test_inventory_heatmap_shows_size_level_risk(client: TestClient) -> None:
    rows = client.get("/api/dashboard/inventory").json()
    assert len(rows) == 44
    fw = next(r for r in rows if r["model"] == "Festive White Sneaker")
    assert fw["total_available"] == 160 and fw["campaign_live"]  # healthy-looking total ...
    status = {c["size"]: c["status"] for c in fw["sizes"]}
    assert status["UK 9"] == "low" and status["UK 8"] == "short"  # ... while the popular sizes are not
    assert fw["worst_status"] == "low"
    uk9 = next(c for c in fw["sizes"] if c["size"] == "UK 9")
    assert uk9["available"] == 4 and 8 < uk9["hours_of_cover"] < 24 and uk9["click_share"] == pytest.approx(0.35)


def test_campaign_overview_size_mix_and_wasted(client: TestClient, work_db: Path) -> None:
    d = client.get("/api/dashboard/campaigns").json()
    assert len(d["campaigns"]) == 12
    google_fw = next(c for c in d["campaigns"] if c["name"].startswith("Shopping | Festive White"))
    assert google_fw["at_risk_click_share"] == pytest.approx(0.35)  # UK 9 is low
    assert {s["size"]: s["status"] for s in google_fw["sizes"]}["UK 9"] == "low"
    wasted_30d = _q(work_db, "SELECT SUM(wasted_spend) FROM ad_metrics_hourly WHERE ts >= ?",
                    (NOW - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S.%f"))[0][0]
    assert d["wasted_spend_30d"] == pytest.approx(wasted_30d, rel=1e-6) and wasted_30d > 0


def test_savings_empty_before_any_approval(client: TestClient) -> None:
    d = client.get("/api/dashboard/savings").json()
    assert d["ad_spend_avoided"] == 0 and d["stockouts_prevented"] == 0 and d["entries"] == []


def test_dashboard_page_is_self_contained(client: TestClient) -> None:
    html = client.get("/dashboard").text
    assert "/static/vendor/plotly-basic.min.js" in html and "Synthetic sample data" in html
    assert client.get("/static/vendor/plotly-basic.min.js").status_code == 200  # works without internet
    js = client.get("/static/app.js").text
    assert "const esc =" in js and "en-IN" in js  # escaping of server/Gemini text; Indian digit grouping
    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"


def test_generated_wasted_spend_matches_planted_stockouts(seeded_db: tuple[Path, dict]) -> None:
    rows = _q(seeded_db[0], "SELECT g.campaign_id, g.stockout_ts, g.restock_ts, g.wasted_spend FROM ground_truth g")
    per_campaign: dict[int, int] = {}
    for cid, *_ in rows:
        per_campaign[cid] = per_campaign.get(cid, 0) + 1
    for cid, start, end, truth in rows:
        recorded = _q(seeded_db[0], "SELECT SUM(wasted_spend) FROM ad_metrics_hourly WHERE campaign_id = ? "
                                    "AND ts >= ? AND ts < ?", cid, start, end)[0][0]
        if per_campaign[cid] == 1:
            assert recorded == pytest.approx(truth, rel=0.02)
        else:  # two planted sizes of one product overlap: hourly waste counts both missing sizes
            assert recorded >= truth * 0.98


# --- mock live ad delivery ------------------------------------------------------------

@pytest.fixture()
def sim(work_db: Path) -> tuple[AdSimulator, Database]:
    db = Database(str(work_db))
    yield AdSimulator(db, None, get_settings(), clock=lambda: NOW, seed=1), db
    db.dispose()


def _hour_row(path: Path, cid: int) -> tuple:
    return _q(path, "SELECT clicks, spend, wasted_clicks, wasted_spend FROM ad_metrics_hourly WHERE campaign_id = ? AND ts = ?",
              cid, NOW.replace(minute=0).strftime("%Y-%m-%d %H:%M:%S.%f"))[0] if _q(
        path, "SELECT 1 FROM ad_metrics_hourly WHERE campaign_id = ? AND ts = ?", cid,
        NOW.replace(minute=0).strftime("%Y-%m-%d %H:%M:%S.%f")) else (0, 0.0, 0, 0.0)


def test_simulator_accrues_active_campaigns_only(sim, work_db: Path) -> None:
    simulator, _ = sim
    active = {r[0] for r in _q(work_db, "SELECT id FROM campaigns WHERE status = 'active'")}
    paused = {r[0] for r in _q(work_db, "SELECT id FROM campaigns WHERE status = 'paused'")}
    ticks = simulator.tick(seconds=1800)
    assert {t["campaign_id"] for t in ticks} <= active and ticks
    before = {cid: _hour_row(work_db, cid) for cid in active}
    simulator.tick(seconds=1800)
    assert any(_hour_row(work_db, cid)[0] > before[cid][0] for cid in active)  # upsert accumulates
    assert all(_hour_row(work_db, cid)[0] == 0 for cid in paused)


def test_simulator_records_wasted_clicks_for_out_of_stock_sizes(sim, work_db: Path) -> None:
    simulator, _ = sim
    cid = _q(work_db, "SELECT c.id FROM campaigns c JOIN products p ON p.id = c.product_id "
                      "WHERE p.model = 'Festive White Sneaker' AND c.platform = 'google_ads'")[0][0]
    w0 = _hour_row(work_db, cid)[2]
    simulator.tick(seconds=3600)
    assert _hour_row(work_db, cid)[2] == w0  # every size in stock -> nothing wasted
    con = sqlite3.connect(work_db)
    con.execute("UPDATE inventory SET on_hand = 0 WHERE variant_id = (SELECT id FROM variants WHERE sku = ?)", (UK9,))
    con.commit()
    con.close()
    simulator.tick(seconds=3600)
    clicks, spend, wclicks, wspend = _hour_row(work_db, cid)
    assert wclicks > w0 and 0 < wspend < spend  # ~35% of clicks want UK 9


def test_simulator_respects_daily_budget(sim, work_db: Path) -> None:
    simulator, _ = sim
    for _ in range(5):
        simulator.tick(seconds=86400 * 3)
    day = NOW.strftime("%Y-%m-%d")
    for cid, spent, budget in _q(work_db, "SELECT a.campaign_id, SUM(a.spend), c.daily_budget FROM ad_metrics_hourly a "
                                          "JOIN campaigns c ON c.id = a.campaign_id WHERE substr(a.ts,1,10) = ? "
                                          "AND c.status = 'active' GROUP BY 1", day):
        assert spent <= budget * 1.15, cid  # one tick may overshoot slightly; then delivery stops


# --- snapshot performance / sharing -----------------------------------------------------

def test_stock_snapshot_concurrent_callers_share_work(work_db: Path) -> None:
    st = get_settings()
    db = Database(str(work_db))
    eng = Engine(db, None, st, load_calendar(st.calendar_file), clock=lambda: NOW)
    eng.recalibrate(NOW)
    calls = {"n": 0}
    original = eng._compute_snapshot

    def counting(now):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return original(now)

    eng._compute_snapshot = counting  # type: ignore[method-assign]
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda _: eng.stock_snapshot(), range(6)))
    assert calls["n"] <= 2  # one in flight + at most one fresher recomputation
    assert all(r == results[0] for r in results)
    db.dispose()
