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
    HourPoint,
    LedgerEntry,
    ProductStockRow,
    SalesOverview,
    SalesToday,
    SavingsOverview,
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
