"""Phase 10: customer "Ads for you" page - mock ad serving and ad clicks feeding wasted spend."""
from __future__ import annotations

import random
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.services.ads import serve_ads
from tests.conftest import TEST_ANCHOR

NOW = TEST_ANCHOR + timedelta(minutes=5)
UK9 = "SN-FWS-WG-UK9"
UK10 = "SN-FWS-WG-UK10"


@pytest.fixture()
def client(work_db: Path):
    settings = Settings(scheduler_enabled=False, startup_sweep=False, ad_sim_enabled=False)
    app = create_app(str(work_db), settings=settings, clock=lambda: NOW)
    with TestClient(app) as c:
        app.state.engine.recalibrate(NOW)
        yield c


def _exec(path: Path, sql: str, *args: object) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def _sell_out(path: Path, sku: str) -> None:
    _exec(path, "UPDATE inventory SET on_hand = reserved WHERE variant_id = (SELECT id FROM variants WHERE sku = ?)", sku)


def _fws_campaign(client: TestClient) -> int:
    camps = client.get("/api/dashboard/campaigns").json()["campaigns"]
    return next(c["campaign_id"] for c in camps if c["product_name"].startswith("Festive White") and c["status"] == "active")


def test_ads_come_from_active_campaigns_and_hide_stock(client: TestClient, work_db: Path) -> None:
    ads = client.get("/api/ads?n=8").json()
    assert 1 <= len(ads) <= 8 and len({a["sku"] for a in ads}) == len(ads)  # one ad per SKU
    camps = {c["campaign_id"]: c for c in client.get("/api/dashboard/campaigns").json()["campaigns"]}
    assert all(camps[a["campaign_id"]]["status"] == "active" for a in ads)
    assert all("available" not in a and "in_stock" not in a for a in ads)  # the ad does not know the stock


def test_ads_include_a_sold_out_size_when_one_exists(work_db: Path) -> None:
    _sell_out(work_db, UK9)
    db = Database(str(work_db))
    try:
        for seed in range(5):
            ads = serve_ads(db, 6, random.Random(seed))
            assert UK9 in {a.sku for a in ads}  # the only sold-out advertised size is always in the mix
    finally:
        db.dispose()


def test_click_on_sold_out_size_is_wasted_and_reaches_the_owner_tile(client: TestClient, work_db: Path) -> None:
    cid = _fws_campaign(client)
    before = client.get("/api/dashboard/campaigns").json()
    _sell_out(work_db, UK9)
    r = client.post("/api/ads/click", json={"campaign_id": cid, "sku": UK9})
    assert r.status_code == 200
    d = r.json()
    assert d["in_stock"] is False and d["charged"] and d["wasted"] and d["cost"] > 0 and d["sku_group"] == "hero"
    after = client.get("/api/dashboard/campaigns").json()
    assert after["wasted_spend_today"] == pytest.approx(before["wasted_spend_today"] + d["cost"], abs=0.02)
    assert after["spend_today"] == pytest.approx(before["spend_today"] + d["cost"], abs=0.02)


def test_click_on_in_stock_size_costs_but_is_not_wasted(client: TestClient) -> None:
    cid = _fws_campaign(client)
    before = client.get("/api/dashboard/campaigns").json()
    d = client.post("/api/ads/click", json={"campaign_id": cid, "sku": UK10}).json()
    assert d["in_stock"] and d["charged"] and not d["wasted"]
    after = client.get("/api/dashboard/campaigns").json()
    assert after["wasted_spend_today"] == pytest.approx(before["wasted_spend_today"], abs=0.01)
    assert after["spend_today"] > before["spend_today"]


def test_click_validation_and_paused_campaigns(client: TestClient, work_db: Path) -> None:
    cid = _fws_campaign(client)
    assert client.post("/api/ads/click", json={"campaign_id": 999, "sku": UK9}).status_code == 404
    assert client.post("/api/ads/click", json={"campaign_id": cid, "sku": "SN-RR8-BG-UK6"}).status_code == 400
    _exec(work_db, "UPDATE campaigns SET status = 'paused' WHERE id = ?", cid)
    d = client.post("/api/ads/click", json={"campaign_id": cid, "sku": UK9}).json()
    assert d["charged"] is False and d["cost"] == 0 and not d["wasted"]  # a paused campaign serves nothing


def test_ads_page_is_in_the_single_page_app(client: TestClient) -> None:
    html = client.get("/dashboard").text
    assert 'data-view="ads"' in html and 'id="stock-msg"' in html and "Stock not available" in html
    assert "/api/ads/click" in client.get("/static/store.js").text
    assert "ad_click" in client.get("/static/app.js").text
