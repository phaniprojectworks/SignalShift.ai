"""Read-side queries for the dashboards and live counters."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import sql_ts
from app.models import Action, Alert, Campaign, CampaignSizeMix, Order, OrderItem, Product, SavingsLedger
from app.schemas import (
    CampaignRow,
    CampaignSize,
    CampaignsOverview,
    DayPoint,
    HourPoint,
    LedgerEntry,
    OrderLine,
    OrderLog,
    OrderLogRow,
    ProductStockRow,
    SalesOverview,
    SalesToday,
    SavingsOverview,
    SkuDaily,
    SkuTrend,
    SkuTrendRow,
    TopProduct,
)


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def sales_today(s: Session, now: datetime) -> SalesToday:
    start = _day_start(now)
    end = start + timedelta(days=1)
    orders, revenue = s.execute(
        select(func.count(Order.id), func.coalesce(func.sum(Order.total), 0.0))
        .where(Order.ts >= start, Order.ts < end)
    ).one()
    units = s.scalar(select(func.coalesce(func.sum(OrderItem.qty), 0)).where(OrderItem.ts >= start, OrderItem.ts < end))
    return SalesToday(date=start.date().isoformat(), orders=int(orders), units=int(units or 0), revenue=float(revenue))


def _by_hour(s: Session, start: datetime) -> list[HourPoint]:
    rows = dict((int(h), (n, r)) for h, n, r in s.execute(text(
        "SELECT CAST(strftime('%H', ts) AS INTEGER), COUNT(*), SUM(total) FROM orders "
        "WHERE ts >= :a AND ts < :b GROUP BY 1"), {"a": sql_ts(start), "b": sql_ts(start + timedelta(days=1))}))
    return [HourPoint(hour=h, orders=int(rows.get(h, (0, 0))[0]), revenue=float(rows.get(h, (0, 0.0))[1] or 0))
            for h in range(24)]


def sales_overview(s: Session, now: datetime, top_n: int = 8) -> SalesOverview:
    start = _day_start(now)
    top = s.execute(text(
        "SELECT p.id, p.name, SUM(oi.qty), SUM(oi.qty * oi.price) FROM order_items oi "
        "JOIN variants v ON v.id = oi.variant_id JOIN products p ON p.id = v.product_id "
        "WHERE oi.ts >= :a GROUP BY p.id ORDER BY 4 DESC LIMIT :n"), {"a": sql_ts(start), "n": top_n}).all()
    return SalesOverview(
        as_of=now, today=sales_today(s, now), by_hour_today=_by_hour(s, start),
        by_hour_yesterday=_by_hour(s, start - timedelta(days=1)),
        top_products=[TopProduct(product_id=pid, name=name, units=int(u), revenue=float(r)) for pid, name, u, r in top])


def campaigns_overview(s: Session, now: datetime, stock: list[ProductStockRow]) -> CampaignsOverview:
    start = _day_start(now)
    status_by_product = {r.product_id: {c.size: c.status for c in r.sizes} for r in stock}
    today = {cid: (sp, cl, cv, ws) for cid, sp, cl, cv, ws in s.execute(text(
        "SELECT campaign_id, SUM(spend), SUM(clicks), SUM(conversions), SUM(wasted_spend) FROM ad_metrics_hourly "
        "WHERE ts >= :a GROUP BY 1"), {"a": sql_ts(start)})}
    month = dict(s.execute(text(
        "SELECT campaign_id, SUM(wasted_spend) FROM ad_metrics_hourly WHERE ts >= :a GROUP BY 1"),
        {"a": sql_ts(now - timedelta(days=30))}).all())
    rows: list[CampaignRow] = []
    for c, pname in s.execute(select(Campaign, Product.name).join(Product, Product.id == Campaign.product_id)
                              .order_by(Campaign.status, Campaign.id)):
        statuses = status_by_product.get(c.product_id, {})
        sizes = [CampaignSize(size=m.size, click_share=m.click_share, status=statuses.get(m.size, "ok"))
                 for m in s.scalars(select(CampaignSizeMix).where(CampaignSizeMix.campaign_id == c.id))]
        sizes.sort(key=lambda x: (len(x.size), x.size))
        sp, cl, cv, ws = today.get(c.id, (0.0, 0, 0, 0.0))
        rows.append(CampaignRow(
            campaign_id=c.id, name=c.name, platform=c.platform, product_id=c.product_id, product_name=pname,
            status=c.status, daily_budget=c.daily_budget, spend_today=round(sp or 0, 2), clicks_today=int(cl or 0),
            conversions_today=int(cv or 0), wasted_spend_today=round(ws or 0, 2),
            wasted_spend_30d=round(month.get(c.id) or 0, 2),
            at_risk_click_share=round(sum(x.click_share for x in sizes if x.status in ("out", "low")), 3),
            sizes=sizes))
    return CampaignsOverview(
        as_of=now, spend_today=round(sum(r.spend_today for r in rows), 2),
        clicks_today=sum(r.clicks_today for r in rows),
        wasted_spend_today=round(sum(r.wasted_spend_today for r in rows), 2),
        wasted_spend_30d=round(sum(r.wasted_spend_30d for r in rows), 2), campaigns=rows)


def savings_overview(s: Session, recent: int = 10) -> SavingsOverview:
    avoided, protected = s.execute(select(func.coalesce(func.sum(SavingsLedger.ad_spend_avoided), 0.0),
                                          func.coalesce(func.sum(SavingsLedger.sales_protected), 0.0))).one()
    prevented = s.scalar(select(func.count(func.distinct(SavingsLedger.alert_id)))
                         .join(Alert, Alert.id == SavingsLedger.alert_id)
                         .where(Alert.severity.in_(("critical", "warning")))) or 0
    executed = s.scalar(select(func.count(Action.id)).where(Action.executed_at.is_not(None))) or 0
    entries = [LedgerEntry(alert_id=e.alert_id, ts=e.ts, ad_spend_avoided=e.ad_spend_avoided,
                           sales_protected=e.sales_protected, explanation=e.explanation)
               for e in s.scalars(select(SavingsLedger).order_by(SavingsLedger.ts.desc()).limit(recent))]
    return SavingsOverview(ad_spend_avoided=float(avoided), sales_protected=float(protected),
                           stockouts_prevented=int(prevented), actions_executed=int(executed), entries=entries)


# --- Sales page ------------------------------------------------------------------------

def _ts(v: str | datetime) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)


def order_log(s: Session, now: datetime, limit: int = 50, offset: int = 0, q: str | None = None,
              channel: str | None = None, days: int | None = None) -> OrderLog:
    """Every order, newest first, with its lines. `q` matches an order number, SKU, product, size or city."""
    where, params = ["1 = 1"], {}
    if days:
        where.append("o.ts >= :since")
        params["since"] = sql_ts(_day_start(now) - timedelta(days=days - 1))
    if channel:
        where.append("o.channel = :channel")
        params["channel"] = channel
    q = (q or "").strip()
    if q.lstrip("#").isdigit():
        where.append("o.id = :oid")
        params["oid"] = int(q.lstrip("#"))
    elif q:
        where.append("(o.city LIKE :like OR o.id IN (SELECT qi.order_id FROM order_items qi WHERE qi.variant_id IN ("
                     "SELECT qv.id FROM variants qv JOIN products qp ON qp.id = qv.product_id "
                     "WHERE qv.sku LIKE :like OR qp.name LIKE :like OR qv.size LIKE :like)))")
        params["like"] = f"%{q}%"
    w = " AND ".join(where)
    total, revenue = s.execute(text(f"SELECT COUNT(*), COALESCE(SUM(o.total), 0) FROM orders o WHERE {w}"), params).one()
    units = s.scalar(text(f"SELECT COALESCE(SUM(oi.qty), 0) FROM order_items oi JOIN orders o ON o.id = oi.order_id "
                          f"WHERE {w}"), params)
    head = s.execute(text(f"SELECT o.id, o.ts, o.channel, o.city, o.total FROM orders o WHERE {w} "
                          "ORDER BY o.ts DESC, o.id DESC LIMIT :limit OFFSET :offset"),
                     {**params, "limit": limit, "offset": offset}).all()
    lines: dict[int, list[OrderLine]] = {oid: [] for oid, *_ in head}
    if head:
        ids = ",".join(str(int(oid)) for oid in lines)
        for oid, sku, name, size, qty, price in s.execute(text(
                "SELECT oi.order_id, v.sku, p.name, v.size, oi.qty, oi.price FROM order_items oi "
                "JOIN variants v ON v.id = oi.variant_id JOIN products p ON p.id = v.product_id "
                f"WHERE oi.order_id IN ({ids}) ORDER BY oi.id")):
            lines[oid].append(OrderLine(sku=sku, product_name=name, size=size, qty=qty, price=price))
    rows = [OrderLogRow(order_id=oid, ts=_ts(ts), channel=ch, city=city, total=round(tot, 2),
                        units=sum(x.qty for x in lines[oid]), lines=lines[oid]) for oid, ts, ch, city, tot in head]
    return OrderLog(total=int(total), limit=limit, offset=offset, revenue=round(float(revenue), 2),
                    units=int(units or 0), rows=rows)


def sku_trend(s: Session, now: datetime, weeks: int = 13) -> SkuTrend:
    """Units per SKU over the last `weeks` weeks (default ~3 months), in 7-day and 30-day windows ending now."""
    days = weeks * 7
    sold: dict[int, dict[int, tuple[int, float]]] = {}
    for vid, age, units, revenue in s.execute(text(
            "SELECT variant_id, CAST(julianday(:now) - julianday(ts) AS INTEGER), SUM(qty), SUM(qty * price) "
            "FROM order_items WHERE ts >= :start AND ts <= :now GROUP BY 1, 2"),
            {"now": sql_ts(now), "start": sql_ts(now - timedelta(days=days))}):
        sold.setdefault(vid, {})[max(0, int(age))] = (int(units), float(revenue))
    rows: list[SkuTrendRow] = []
    for vid, sku, size, pid, pname, group in s.execute(text(
            "SELECT v.id, v.sku, v.size, p.id, p.name, p.sku_group FROM variants v JOIN products p ON p.id = v.product_id "
            "ORDER BY p.name, v.size_rank, v.id")):
        weekly, monthly, units, revenue = [0] * weeks, [0, 0, 0], 0, 0.0
        for age, (u, r) in sold.get(vid, {}).items():
            if age >= days:
                continue
            weekly[weeks - 1 - age // 7] += u
            if age < 90:
                monthly[2 - age // 30] += u
            units += u
            revenue += r
        change = round((monthly[2] - monthly[1]) / monthly[1] * 100, 1) if monthly[1] else None
        rows.append(SkuTrendRow(variant_id=vid, sku=sku, product_id=pid, product_name=pname, sku_group=group, size=size,
                                weekly_units=weekly, units_by_month=monthly, units=units, revenue=round(revenue, 2),
                                change_pct=change))
    return SkuTrend(as_of=now, days=days, week_starts=[now - timedelta(days=7 * (weeks - i)) for i in range(weeks)],
                    rows=rows)


def sku_daily(s: Session, now: datetime, variant_id: int, days: int = 91) -> SkuDaily | None:
    """Daily units and revenue for one SKU (zero-filled), for the Sales page detail chart."""
    head = s.execute(text("SELECT v.sku, p.name, v.size FROM variants v JOIN products p ON p.id = v.product_id "
                          "WHERE v.id = :v"), {"v": variant_id}).first()
    if head is None:
        return None
    first = _day_start(now) - timedelta(days=days - 1)
    got = {d: (int(u), float(r)) for d, u, r in s.execute(text(
        "SELECT date(ts), SUM(qty), SUM(qty * price) FROM order_items WHERE variant_id = :v AND ts >= :a AND ts <= :b "
        "GROUP BY 1"), {"v": variant_id, "a": sql_ts(first), "b": sql_ts(now)})}
    out = []
    for i in range(days):
        d = (first + timedelta(days=i)).date().isoformat()
        u, r = got.get(d, (0, 0.0))
        out.append(DayPoint(date=d, units=u, revenue=round(r, 2)))
    return SkuDaily(variant_id=variant_id, sku=head[0], product_name=head[1], size=head[2], days=out)

