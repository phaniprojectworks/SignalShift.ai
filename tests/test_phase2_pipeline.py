"""Phase 2: order -> inventory -> event pipeline."""
from __future__ import annotations

import asyncio
import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.schemas import OrderIn, OrderItemIn
from app.services.events import EventBus
from app.services.inventory import InsufficientStock, UnknownSku, apply_order
from tests.conftest import TEST_ANCHOR

NOW = TEST_ANCHOR.replace(minute=5)


def fw_sku(path: Path, size: str) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT v.sku FROM variants v JOIN products p ON p.id = v.product_id "
                           "WHERE p.model = 'Festive White Sneaker' AND v.size = ?", (size,)).fetchone()[0]
    finally:
        con.close()


def q1(path: Path, sql: str, *args: object) -> object:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


def on_hand(path: Path, sku: str) -> int:
    return q1(path, "SELECT i.on_hand FROM inventory i JOIN variants v ON v.id = i.variant_id WHERE v.sku = ?", sku)


def order(*items: tuple[str, int], key: str | None = None) -> OrderIn:
    return OrderIn(items=[OrderItemIn(sku=s, qty=q) for s, q in items], idempotency_key=key)


@pytest.fixture()
def db(work_db: Path) -> Database:
    d = Database(str(work_db))
    yield d
    d.dispose()


# --- service level ---------------------------------------------------------------

def test_apply_order_decrements_and_logs(db: Database, work_db: Path) -> None:
    sku = fw_sku(work_db, "UK 9")
    orders_before = q1(work_db, "SELECT COUNT(*) FROM orders")
    res = apply_order(db, order((sku, 1)), NOW)
    assert res.lines[0].on_hand_after == 3 and on_hand(work_db, sku) == 3
    assert res.total == 5499
    assert q1(work_db, "SELECT COUNT(*) FROM orders") == orders_before + 1
    assert q1(work_db, "SELECT COUNT(*) FROM inventory_movements WHERE ref = ? AND type = 'sale' AND qty = 1",
              f"order:{res.order_id}") == 1
    assert q1(work_db, "SELECT COUNT(*) FROM order_items WHERE order_id = ?", res.order_id) == 1


def test_unknown_sku_writes_nothing(db: Database, work_db: Path) -> None:
    before = q1(work_db, "SELECT COUNT(*) FROM orders")
    with pytest.raises(UnknownSku):
        apply_order(db, order(("NOPE-1", 1)), NOW)
    assert q1(work_db, "SELECT COUNT(*) FROM orders") == before


def test_oversell_rejected_and_multi_line_rolls_back(db: Database, work_db: Path) -> None:
    uk9, uk8 = fw_sku(work_db, "UK 9"), fw_sku(work_db, "UK 8")
    with pytest.raises(InsufficientStock) as exc:
        apply_order(db, order((uk9, 5)), NOW)
    assert exc.value.details["available"] == 4
    # UK 8 line is fine, UK 9 line is not -> nothing at all may change
    with pytest.raises(InsufficientStock):
        apply_order(db, order((uk8, 2), (uk9, 5)), NOW)
    assert on_hand(work_db, uk8) == 11 and on_hand(work_db, uk9) == 4


def test_duplicate_skus_in_one_order_are_merged(db: Database, work_db: Path) -> None:
    uk9 = fw_sku(work_db, "UK 9")
    with pytest.raises(InsufficientStock):
        apply_order(db, order((uk9, 3), (uk9, 2)), NOW)  # 5 > 4 in total
    res = apply_order(db, order((uk9, 2), (uk9, 2)), NOW)
    assert res.lines[0].qty == 4 and on_hand(work_db, uk9) == 0


def test_reserved_stock_is_not_sellable(db: Database, work_db: Path) -> None:
    uk9 = fw_sku(work_db, "UK 9")
    con = sqlite3.connect(work_db)
    con.execute("UPDATE inventory SET reserved = 3 WHERE variant_id = (SELECT id FROM variants WHERE sku = ?)", (uk9,))
    con.commit()
    con.close()
    apply_order(db, order((uk9, 1)), NOW)
    with pytest.raises(InsufficientStock):
        apply_order(db, order((uk9, 1)), NOW)


def test_concurrent_orders_never_oversell(db: Database, work_db: Path) -> None:
    uk9 = fw_sku(work_db, "UK 9")
    sales_before = q1(work_db, "SELECT COUNT(*) FROM inventory_movements WHERE type='sale'")
    barrier = threading.Barrier(24)

    def attempt(_: int) -> bool:
        barrier.wait()
        try:
            apply_order(db, order((uk9, 1)), NOW)
            return True
        except InsufficientStock:
            return False

    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(attempt, range(24)))
    assert sum(results) == 4
    assert on_hand(work_db, uk9) == 0
    assert q1(work_db, "SELECT COUNT(*) FROM inventory_movements WHERE type='sale'") == sales_before + 4


def test_concurrent_mixed_quantities_reconcile(db: Database, work_db: Path) -> None:
    uk8 = fw_sku(work_db, "UK 8")
    rng = random.Random(7)
    qtys = [rng.randint(1, 3) for _ in range(20)]

    def attempt(q: int) -> int:
        try:
            return apply_order(db, order((uk8, q)), NOW).lines[0].qty
        except InsufficientStock:
            return 0

    with ThreadPoolExecutor(max_workers=10) as pool:
        sold = sum(pool.map(attempt, qtys))
    assert sold <= 11
    assert on_hand(work_db, uk8) == 11 - sold


def test_idempotency_key_sequential_and_concurrent(db: Database, work_db: Path) -> None:
    uk8 = fw_sku(work_db, "UK 8")
    first = apply_order(db, order((uk8, 1), key="abc"), NOW)
    again = apply_order(db, order((uk8, 1), key="abc"), NOW)
    assert again.replayed and again.order_id == first.order_id
    assert on_hand(work_db, uk8) == 10

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = set(pool.map(lambda _: apply_order(db, order((uk8, 1), key="race"), NOW).order_id, range(8)))
    assert len(ids) == 1
    assert on_hand(work_db, uk8) == 9


# --- event bus -------------------------------------------------------------------

def test_event_bus_fanout_dedupe_and_handlers() -> None:
    async def scenario() -> tuple[list[str], list[int]]:
        bus = EventBus()
        handled: list[int] = []
        bus.on("sale", lambda ev: handled.append(ev.data["n"]))
        await bus.start()
        q = bus.subscribe()
        # publish from another thread (as the threadpool-run service code does)
        t = threading.Thread(target=lambda: [bus.publish("sale", {"n": 1}, key="order:1"),
                                             bus.publish("sale", {"n": 1}, key="order:1"),  # duplicate delivery
                                             bus.publish("inventory", {"n": 2})])
        t.start()
        t.join()
        got = [(await asyncio.wait_for(q.get(), 1)).type for _ in range(3)]
        for _ in range(50):
            if handled:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        await bus.stop()
        assert [e.id for e in bus.replay_since(1)] == [2, 3]
        return got, handled

    got, handled = asyncio.run(scenario())
    assert got == ["sale", "sale", "inventory"]  # subscribers see every delivery
    assert handled == [1]  # the consumer handles a keyed event once


# --- API -------------------------------------------------------------------------

@pytest.fixture()
def client_and_calls(work_db: Path):
    calls: list[tuple[list[int], str]] = []
    settings = Settings(scheduler_enabled=False, startup_sweep=False)
    app = create_app(str(work_db), evaluate=lambda ids, trig: calls.append((ids, trig)), settings=settings,
                     clock=lambda: NOW)
    with TestClient(app) as client:
        yield client, calls


def test_api_order_flow_and_errors(client_and_calls, work_db: Path) -> None:
    client, calls = client_and_calls
    uk9 = fw_sku(work_db, "UK 9")
    r = client.post("/orders", json={"items": [{"sku": uk9, "qty": 1}], "city": "Pune"})
    assert r.status_code == 201
    body = r.json()
    assert body["lines"][0]["available_after"] == 3

    assert client.post("/orders", json={"items": [{"sku": "NOPE", "qty": 1}]}).status_code == 404
    r = client.post("/orders", json={"items": [{"sku": uk9, "qty": 9}]})
    assert r.status_code == 409 and r.json()["details"]["available"] == 3
    assert client.post("/orders", json={"items": []}).status_code == 422
    assert client.post("/orders", json={"items": [{"sku": uk9, "qty": 0}]}).status_code == 422

    # the consumer triggers the engine for only the affected variant
    vid = body["lines"][0]["variant_id"]
    deadline = time.time() + 3
    while not calls and time.time() < deadline:
        time.sleep(0.02)
    assert calls == [([vid], "order")]


def test_sse_stream_replays_sale_and_inventory(client_and_calls, work_db: Path) -> None:
    client, _ = client_and_calls
    uk8 = fw_sku(work_db, "UK 8")
    client.post("/orders", json={"items": [{"sku": uk8, "qty": 2}]})
    with client.stream("GET", "/stream?max_events=2", headers={"Last-Event-ID": "0"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    assert "event: sale" in text and "event: inventory" in text
    assert '"available": 9' in text
    assert '"units":' in text  # live sales counter payload


def test_catalog_and_store_page(client_and_calls) -> None:
    client, _ = client_and_calls
    cat = client.get("/api/catalog").json()
    fw = next(p for p in cat if p["model"] == "Festive White Sneaker")
    assert fw["total_available"] == 160 and len(fw["variants"]) == 6
    assert client.get("/store").status_code == 200
    assert "EventSource" in client.get("/static/store.js").text
    today = client.get("/api/stats/today").json()
    assert set(today) == {"date", "orders", "units", "revenue"}
