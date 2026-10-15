"""Phase 1: schema + synthetic data generator."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.config import get_settings
from app.services.calendar import event_multiplier, festival_multiplier, load_calendar
from scripts.generate_data import FESTIVE_WHITE_DEMO_STOCK, generate
from tests.conftest import TEST_ANCHOR

FOOT = ("UK 6", "UK 7", "UK 8", "UK 9", "UK 10", "UK 11")


def one(con: sqlite3.Connection, sql: str, *args: object) -> object:
    return con.execute(sql, args).fetchone()[0]


# --- schema ------------------------------------------------------------------

def test_all_tables_exist(con: sqlite3.Connection) -> None:
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"products", "variants", "inventory", "inventory_movements", "orders", "order_items", "campaigns",
                "campaign_size_mix", "ad_metrics_hourly", "site_events", "calendar_events", "alerts", "actions",
                "savings_ledger", "ask_log", "ground_truth", "alert_fts", "app_meta"}
    assert expected <= names


def test_catalog_shape_and_prices(con: sqlite3.Connection) -> None:
    products = one(con, "SELECT COUNT(*) FROM products")
    variants = one(con, "SELECT COUNT(*) FROM variants")
    assert 38 <= products <= 46
    assert 210 <= variants <= 260
    assert one(con, "SELECT COUNT(DISTINCT sku) FROM variants") == variants
    groups = {r[0] for r in con.execute("SELECT DISTINCT sku_group FROM products")}
    assert groups == {"hero", "core", "slow", "accessory"}
    # INR prices with sensible gross margins (30%-70%)
    for price, cost in con.execute("SELECT price, cost FROM products"):
        assert 399 <= price <= 9999
        assert 0.3 <= 1 - cost / price <= 0.7
    # every product has inventory per variant
    assert one(con, "SELECT COUNT(*) FROM inventory") == variants


# --- demand shape --------------------------------------------------------------

def test_size_curve_uk8_uk9_about_55_percent(con: sqlite3.Connection) -> None:
    df = pd.read_sql("SELECT v.size, SUM(oi.qty) units FROM order_items oi JOIN variants v ON v.id = oi.variant_id "
                     f"WHERE v.size IN {FOOT} GROUP BY v.size", con).set_index("size")["units"]
    share = (df["UK 8"] + df["UK 9"]) / df.sum()
    assert 0.52 <= share <= 0.58, share


def _daily_units(con: sqlite3.Connection, where: str, start: str, end: str) -> float:
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    units = one(con, "SELECT COALESCE(SUM(oi.qty),0) FROM order_items oi JOIN variants v ON v.id = oi.variant_id "
                     f"JOIN products p ON p.id = v.product_id WHERE {where} AND oi.ts >= ? AND oi.ts < ?", start, end)
    return units / days


# Baselines are quiet periods with no calendar event (2024's pre-festive period is before the history starts).
@pytest.mark.parametrize("diwali,pre_start,base_start,base_end", [
    ("2025-10-20", "2025-10-13", "2025-08-20", "2025-09-16"),
    ("2024-11-01", "2024-10-25", "2024-11-12", "2024-12-10"),
])
def test_diwali_uplift_hero_and_gifting(con: sqlite3.Connection, diwali: str, pre_start: str,
                                        base_start: str, base_end: str) -> None:
    for where, lo, hi in [("p.sku_group = 'hero'", 2.5, 6.0), ("p.seasonality_tag = 'gifting'", 2.5, 6.5)]:
        ratio = _daily_units(con, where, pre_start, diwali) / _daily_units(con, where, base_start, base_end)
        assert lo <= ratio <= hi, (where, ratio)
    # slow movers barely react
    slow = _daily_units(con, "p.sku_group='slow'", pre_start, diwali) / _daily_units(
        con, "p.sku_group='slow'", base_start, base_end)
    assert slow < 2.0


def test_history_spans_two_years_and_ads_30_days(con: sqlite3.Connection) -> None:
    first = datetime.fromisoformat(one(con, "SELECT MIN(ts) FROM orders"))
    last = datetime.fromisoformat(one(con, "SELECT MAX(ts) FROM orders"))
    assert (last - first).days >= 725
    assert last < TEST_ANCHOR
    ad_first = datetime.fromisoformat(one(con, "SELECT MIN(ts) FROM ad_metrics_hourly"))
    assert (TEST_ANCHOR - ad_first) <= timedelta(days=30)


def test_ad_spend_tracks_daily_budget(con: sqlite3.Connection) -> None:
    df = pd.read_sql("SELECT substr(a.ts,1,10) d, a.campaign_id, SUM(a.spend) spend, c.daily_budget "
                     "FROM ad_metrics_hourly a JOIN campaigns c ON c.id = a.campaign_id "
                     "GROUP BY 1, 2 HAVING COUNT(*) = 24", con)
    ratio = (df["spend"] / df["daily_budget"]).median()
    assert 0.85 <= ratio <= 1.15


# --- stock ledger --------------------------------------------------------------

def _ledger(con: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql("SELECT ts, variant_id, type, qty FROM inventory_movements ORDER BY variant_id, ts, id", con)
    df["delta"] = df["qty"].where(df["type"] != "sale", -df["qty"])
    df["balance"] = df.groupby("variant_id")["delta"].cumsum()
    return df


def test_movements_reconcile_to_on_hand_and_never_negative(con: sqlite3.Connection) -> None:
    df = _ledger(con)
    assert (df["balance"] >= 0).all()
    final = df.groupby("variant_id")["balance"].last()
    on_hand = pd.read_sql("SELECT variant_id, on_hand FROM inventory", con).set_index("variant_id")["on_hand"]
    assert final.sort_index().equals(on_hand.sort_index().astype(final.dtype))


def test_sale_movements_match_order_items(con: sqlite3.Connection) -> None:
    assert one(con, "SELECT SUM(qty) FROM inventory_movements WHERE type='sale'") == one(
        con, "SELECT SUM(qty) FROM order_items")


# --- hidden truth ---------------------------------------------------------------

def test_planted_stockouts(con: sqlite3.Connection) -> None:
    gt = pd.read_sql("SELECT g.*, p.sku_group FROM ground_truth g JOIN variants v ON v.id = g.variant_id "
                     "JOIN products p ON p.id = v.product_id", con)
    assert 4 <= len(gt) <= 5
    assert (gt["sku_group"] == "hero").all()
    for row in gt.itertuples():
        # campaign was live (spending) during the outage
        spend = one(con, "SELECT COALESCE(SUM(spend),0) FROM ad_metrics_hourly WHERE campaign_id=? AND ts>=? AND ts<?",
                    row.campaign_id, row.stockout_ts, row.restock_ts)
        assert spend > 0
        assert row.wasted_clicks > 0 and row.wasted_spend > 0 and row.lost_units > 0
        # nothing sold while out of stock, and a restock arrived at the end
        assert one(con, "SELECT COUNT(*) FROM inventory_movements WHERE variant_id=? AND type='sale' AND ts>=? AND ts<?",
                   row.variant_id, row.stockout_ts, row.restock_ts) == 0
        assert one(con, "SELECT COUNT(*) FROM inventory_movements WHERE variant_id=? AND type='restock' AND ts>=? "
                        "AND ts<datetime(?, '+1 hour')", row.variant_id, row.restock_ts, row.restock_ts) == 1
        # within the last 30 days
        assert TEST_ANCHOR - datetime.fromisoformat(row.stockout_ts) <= timedelta(days=30)


def test_no_unplanted_stockouts(con: sqlite3.Connection) -> None:
    df = _ledger(con)
    zeros = df[(df["balance"] == 0)]
    gt = pd.read_sql("SELECT variant_id, stockout_ts, restock_ts FROM ground_truth", con)
    for row in zeros.itertuples():
        hits = gt[(gt.variant_id == row.variant_id) & (gt.stockout_ts <= row.ts) & (gt.restock_ts > row.ts)]
        # a zero balance can sit just before the window opens (last sale of the hour preceding the stockout)
        near = gt[(gt.variant_id == row.variant_id)
                  & (pd.to_datetime(gt.stockout_ts) - pd.to_datetime(row.ts) <= pd.Timedelta(hours=1))
                  & (pd.to_datetime(gt.stockout_ts) >= pd.to_datetime(row.ts))]
        assert len(hits) or len(near), f"unplanted stockout: variant {row.variant_id} at {row.ts}"


# --- demo state -------------------------------------------------------------------

def test_demo_state_festive_white(con: sqlite3.Connection) -> None:
    stock = dict(con.execute("SELECT v.size, i.on_hand FROM inventory i JOIN variants v ON v.id = i.variant_id "
                             "JOIN products p ON p.id = v.product_id WHERE p.model = 'Festive White Sneaker'"))
    assert stock == FESTIVE_WHITE_DEMO_STOCK
    assert stock["UK 9"] == 4 and stock["UK 8"] == 11
    assert sum(stock.values()) >= 120  # looks healthy in aggregate
    active = one(con, "SELECT COUNT(*) FROM campaigns c JOIN products p ON p.id = c.product_id "
                      "WHERE p.model = 'Festive White Sneaker' AND c.status = 'active'")
    assert active >= 1
    mix = dict(con.execute("SELECT m.size, m.click_share FROM campaign_size_mix m JOIN campaigns c ON c.id = m.campaign_id "
                           "JOIN products p ON p.id = c.product_id WHERE p.model = 'Festive White Sneaker' "
                           "AND c.platform = 'google_ads'"))
    assert mix["UK 8"] + mix["UK 9"] == pytest.approx(0.68)
    assert sum(mix.values()) == pytest.approx(1.0)


def test_demo_state_everyday_runner_cover(con: sqlite3.Connection) -> None:
    start = (TEST_ANCHOR - timedelta(days=14)).isoformat(sep=" ")
    on_hand = one(con, "SELECT SUM(i.on_hand) FROM inventory i JOIN variants v ON v.id = i.variant_id "
                       "JOIN products p ON p.id = v.product_id WHERE p.name = 'Everyday Runner — Black'")
    sold = one(con, "SELECT SUM(oi.qty) FROM order_items oi JOIN variants v ON v.id = oi.variant_id "
                    "JOIN products p ON p.id = v.product_id WHERE p.name = 'Everyday Runner — Black' AND oi.ts >= ?", start)
    cover = on_hand / (sold / 14)
    assert 23 <= cover <= 27, cover  # 25 days of recent sales; the engine's festival-aware forecast reads ~22
    assert one(con, "SELECT COUNT(*) FROM campaigns c JOIN products p ON p.id = c.product_id "
                    "WHERE p.name = 'Everyday Runner — Black' AND c.status = 'active'") == 1


def test_sample_data_is_labelled(con: sqlite3.Connection) -> None:
    assert one(con, "SELECT value FROM app_meta WHERE key='data_label'") == "synthetic sample data"


# --- generator behaviour -----------------------------------------------------------

def test_generator_refuses_without_reset(seeded_db: tuple[Path, dict]) -> None:
    with pytest.raises(SystemExit):
        generate(str(seeded_db[0]), 2026, TEST_ANCHOR, get_settings().calendar_file, reset=False, quiet=True)


def test_generator_is_reproducible(seeded_db: tuple[Path, dict], tmp_path: Path) -> None:
    other = generate(str(tmp_path / "again.db"), 2026, TEST_ANCHOR, get_settings().calendar_file,
                     reset=True, quiet=True)
    first = seeded_db[1]
    for key in ("orders", "units_sold", "revenue_inr", "movements", "ad_metric_rows", "festive_white_stock"):
        assert other[key] == first[key], key


# --- calendar ------------------------------------------------------------------------

def test_calendar_multiplier_shapes() -> None:
    events = load_calendar(get_settings().calendar_file)
    diwali = next(e for e in events if e.name == "Diwali" and e.date == date(2026, 11, 8))
    assert event_multiplier(diwali, date(2026, 11, 8), "festive_hero") == pytest.approx(3.8)
    assert event_multiplier(diwali, date(2026, 10, 1), "festive_hero") == 1.0
    assert 1.0 < event_multiplier(diwali, date(2026, 10, 25), "festive_hero") < 3.8
    # Dhanteras stacks on top of the Diwali ramp
    assert festival_multiplier(events, date(2026, 11, 6), "festive_hero") > event_multiplier(
        diwali, date(2026, 11, 6), "festive_hero")
    assert festival_multiplier(events, date(2026, 3, 1), "core") == 1.0
