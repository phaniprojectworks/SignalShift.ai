"""Order application: atomic stock decrement, movement log, and event emission.

Never-oversell guarantee: each line is a conditional UPDATE
    on_hand = on_hand - qty  WHERE on_hand - reserved >= qty
inside one BEGIN IMMEDIATE transaction. If any line cannot be filled, the whole order rolls back.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Database
from app.models import Inventory, InventoryMovement, Order, OrderItem, Product, Variant
from app.schemas import OrderIn, OrderLineOut, OrderOut, ProductStock, VariantStock
from app.services.clock import now as clock_now
from app.services.dashboards import sales_today
from app.services.events import EventBus

log = logging.getLogger("signalshift.inventory")


class OrderError(Exception):
    status_code = 400
    code = "order_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class UnknownSku(OrderError):
    status_code = 404
    code = "unknown_sku"


class InsufficientStock(OrderError):
    status_code = 409
    code = "insufficient_stock"


def apply_order(db: Database, order: OrderIn, now: datetime | None = None) -> OrderOut:
    """Apply an order atomically. Raises UnknownSku / InsufficientStock; nothing is written on failure."""
    ts = now or clock_now()
    try:
        with db.write() as s:
            if order.idempotency_key:
                existing = s.scalar(select(Order).where(Order.idempotency_key == order.idempotency_key))
                if existing is not None:
                    return _load_order(s, existing, replayed=True)

            qty_by_sku = Counter[str]()
            for item in order.items:
                qty_by_sku[item.sku] += item.qty
            rows = s.execute(
                select(Variant, Product).join(Product, Product.id == Variant.product_id)
                .where(Variant.sku.in_(qty_by_sku))
            ).all()
            found = {v.sku: (v, p) for v, p in rows}
            missing = sorted(set(qty_by_sku) - set(found))
            if missing:
                raise UnknownSku(f"Unknown SKU: {', '.join(missing)}", skus=missing)

            # Lock order by variant id keeps multi-line orders deterministic.
            lines = sorted(qty_by_sku.items(), key=lambda kv: found[kv[0]][0].id)
            for sku, qty in lines:
                variant = found[sku][0]
                res = s.execute(
                    update(Inventory)
                    .where(Inventory.variant_id == variant.id, Inventory.on_hand - Inventory.reserved >= qty)
                    .values(on_hand=Inventory.on_hand - qty, updated_at=ts)
                )
                if res.rowcount != 1:
                    available = s.scalar(
                        select(Inventory.on_hand - Inventory.reserved).where(Inventory.variant_id == variant.id))
                    raise InsufficientStock(
                        f"Only {max(available or 0, 0)} left of {sku}, requested {qty}",
                        sku=sku, requested=qty, available=max(available or 0, 0))

            total = sum(found[sku][1].price * qty for sku, qty in lines)
            o = Order(ts=ts, channel=order.channel, city=order.city, total=total,
                      idempotency_key=order.idempotency_key)
            s.add(o)
            s.flush()
            for sku, qty in lines:
                variant, product = found[sku]
                s.add(OrderItem(order_id=o.id, ts=ts, variant_id=variant.id, qty=qty, price=product.price))
                s.add(InventoryMovement(ts=ts, variant_id=variant.id, type="sale", qty=qty, ref=f"order:{o.id}"))
            s.flush()
            return _load_order(s, o, replayed=False)
    except IntegrityError:
        # Lost a race on the same idempotency key: return the order that won.
        if not order.idempotency_key:
            raise
        with db.session() as s:
            existing = s.scalar(select(Order).where(Order.idempotency_key == order.idempotency_key))
            if existing is None:
                raise
            return _load_order(s, existing, replayed=True)


def _load_order(s: Session, o: Order, replayed: bool) -> OrderOut:
    rows = s.execute(
        select(OrderItem, Variant, Product, Inventory)
        .join(Variant, Variant.id == OrderItem.variant_id)
        .join(Product, Product.id == Variant.product_id)
        .join(Inventory, Inventory.variant_id == Variant.id)
        .where(OrderItem.order_id == o.id)
        .order_by(Variant.id)
    ).all()
    lines = [
        OrderLineOut(variant_id=v.id, sku=v.sku, product_id=p.id, product_name=p.name, size=v.size, qty=it.qty,
                     unit_price=it.price, on_hand_after=inv.on_hand, available_after=inv.on_hand - inv.reserved)
        for it, v, p, inv in rows
    ]
    return OrderOut(order_id=o.id, ts=o.ts, total=o.total, channel=o.channel, city=o.city, lines=lines,
                    replayed=replayed)


def place_order(db: Database, bus: EventBus, order: OrderIn, now: datetime | None = None) -> OrderOut:
    """Apply an order, then publish `sale` + per-variant `inventory` events (skipped for idempotent replays)."""
    result = apply_order(db, order, now)
    if result.replayed:
        log.info("idempotent order replay", extra={"order_id": result.order_id})
        return result
    with db.session() as s:
        today = sales_today(s, result.ts)
    bus.publish(
        "sale",
        {
            "order_id": result.order_id,
            "total": result.total,
            "channel": result.channel,
            "city": result.city,
            "variant_ids": [ln.variant_id for ln in result.lines],
            "lines": [ln.model_dump() for ln in result.lines],
            "today": today.model_dump(),
        },
        key=f"order:{result.order_id}",
    )
    for ln in result.lines:
        bus.publish("inventory", {"variant_id": ln.variant_id, "sku": ln.sku, "product_id": ln.product_id,
                                  "size": ln.size, "on_hand": ln.on_hand_after, "available": ln.available_after})
    log.info("order applied", extra={"order_id": result.order_id, "variant_ids": [ln.variant_id for ln in result.lines]})
    return result


def catalog(s: Session) -> list[ProductStock]:
    rows = s.execute(
        select(Product, Variant, Inventory)
        .join(Variant, Variant.product_id == Product.id)
        .join(Inventory, Inventory.variant_id == Variant.id)
        .order_by(Product.id, Variant.size_rank)
    ).all()
    out: dict[int, ProductStock] = {}
    for p, v, inv in rows:
        ps = out.get(p.id)
        if ps is None:
            ps = out[p.id] = ProductStock(product_id=p.id, name=p.name, model=p.model, sku_group=p.sku_group,
                                          price=p.price, unit_label=p.unit_label, total_available=0, variants=[])
        avail = inv.on_hand - inv.reserved
        ps.variants.append(VariantStock(variant_id=v.id, sku=v.sku, size=v.size, on_hand=inv.on_hand, available=avail))
        ps.total_available += avail
    return list(out.values())


def total_on_hand(s: Session) -> int:
    return int(s.scalar(select(func.coalesce(func.sum(Inventory.on_hand), 0))) or 0)
