"""Phase 9: Sales page (order log, per-SKU trend)."""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
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


def test_order_log_pages_newest_first_and_matches_database(client: TestClient, work_db: Path) -> None:
    d = client.get("/api/dashboard/orders?limit=20").json()
    (count, revenue), = _q(work_db, "SELECT COUNT(*), SUM(total) FROM orders")
    assert d["total"] == count and d["revenue"] == pytest.approx(revenue, abs=1)
    assert len(d["rows"]) == 20 and all(r["lines"] and r["units"] >= 1 for r in d["rows"])
    stamps = [r["ts"] for r in d["rows"]]
    assert stamps == sorted(stamps, reverse=True)
    nxt = client.get("/api/dashboard/orders?limit=20&offset=20").json()["rows"]
    assert not {r["order_id"] for r in nxt} & {r["order_id"] for r in d["rows"]}  # pages do not overlap


def test_order_log_filters(client: TestClient, work_db: Path) -> None:
    d = client.get("/api/dashboard/orders", params={"q": UK9, "days": 30}).json()
    assert d["total"] > 0 and all(any(l["sku"] == UK9 for l in r["lines"]) for r in d["rows"])
    one = client.get("/api/dashboard/orders", params={"q": f"#{d['rows'][0]['order_id']}"}).json()
    assert one["total"] == 1 and one["rows"][0]["order_id"] == d["rows"][0]["order_id"]
    app_only = client.get("/api/dashboard/orders", params={"channel": "app", "limit": 50}).json()
    (n_app,), = _q(work_db, "SELECT COUNT(*) FROM orders WHERE channel = 'app'")
    assert app_only["total"] == n_app and {r["channel"] for r in app_only["rows"]} == {"app"}
    assert client.get("/api/dashboard/orders", params={"q": "no-such-thing-xyz"}).json()["total"] == 0
    assert client.get("/api/dashboard/orders?limit=500").status_code == 422


def test_sku_trend_covers_every_sku_for_three_months(client: TestClient, work_db: Path) -> None:
    d = client.get("/api/dashboard/sku-trend").json()
    (n_variants,), = _q(work_db, "SELECT COUNT(*) FROM variants")
    assert len(d["rows"]) == n_variants and d["days"] == 91 and len(d["week_starts"]) == 13
    row = next(r for r in d["rows"] if r["sku"] == UK9)
    assert len(row["weekly_units"]) == 13 and sum(row["weekly_units"]) == row["units"]
    assert len(row["units_by_month"]) == 3
    start = NOW - timedelta(days=30)
    (last30,), = _q(work_db, "SELECT COALESCE(SUM(oi.qty), 0) FROM order_items oi JOIN variants v ON v.id = oi.variant_id "
                             "WHERE v.sku = ? AND oi.ts > ? AND oi.ts <= ?", UK9,
                    start.strftime("%Y-%m-%d %H:%M:%S.%f"), NOW.strftime("%Y-%m-%d %H:%M:%S.%f"))
    assert abs(row["units_by_month"][2] - last30) <= 2  # day-boundary rounding only


def test_sku_daily_is_zero_filled(client: TestClient) -> None:
    vid = next(r for r in client.get("/api/dashboard/sku-trend").json()["rows"] if r["sku"] == UK9)["variant_id"]
    d = client.get(f"/api/dashboard/sku-trend/{vid}").json()
    assert d["sku"] == UK9 and len(d["days"]) == 91 and d["days"][-1]["date"] == NOW.date().isoformat()
    assert client.get("/api/dashboard/sku-trend/999999").status_code == 404


def test_new_pages_are_in_the_single_page_app(client: TestClient) -> None:
    html = client.get("/dashboard").text
    for marker in ('data-view="sales"', 'id="order-rows"', 'id="trend-rows"', 'id="inv-sort"'):
        assert marker in html
    js = client.get("/static/app.js").text
    assert "/api/dashboard/orders" in js and "data-view=\"waste\"" not in html
