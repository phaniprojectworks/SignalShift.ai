"""SQLAlchemy models. Timestamps are naive datetimes in the business timezone (see config)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# --- Catalog -----------------------------------------------------------------

class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku_group: Mapped[str] = mapped_column(String(32))  # hero / core / slow / accessory
    name: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(80))  # e.g. "Festive White Sneaker"
    category: Mapped[str] = mapped_column(String(40))  # sneakers / accessories
    price: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    seasonality_tag: Mapped[str] = mapped_column(String(32))  # festive_hero / core / slow / gifting / accessory
    unit_label: Mapped[str] = mapped_column(String(16), default="unit")  # market-specific "stock unit": pair / pack / unit


class Variant(Base):
    __tablename__ = "variants"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    size: Mapped[str] = mapped_column(String(16))  # "UK 9", "UK 6-8", "One Size"
    size_rank: Mapped[int] = mapped_column(Integer, default=0)
    color: Mapped[str] = mapped_column(String(40))
    sku: Mapped[str] = mapped_column(String(40), unique=True)


class Inventory(Base):
    __tablename__ = "inventory"
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    in_transit: Mapped[int] = mapped_column(Integer, default=0)
    reorder_point: Mapped[int] = mapped_column(Integer, default=0)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=7)
    restock_eta: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"))
    type: Mapped[str] = mapped_column(String(16))  # sale / restock / return
    qty: Mapped[int] = mapped_column(Integer)  # always positive; direction comes from type
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (Index("ix_mov_variant_ts", "variant_id", "ts"),)


# --- Sales -------------------------------------------------------------------

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    channel: Mapped[str] = mapped_column(String(24))
    city: Mapped[str] = mapped_column(String(40))
    total: Mapped[float] = mapped_column(Float)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime)  # denormalised from orders for fast velocity queries
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"))
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)  # unit price paid
    __table_args__ = (Index("ix_items_variant_ts", "variant_id", "ts"), Index("ix_items_ts", "ts"))


# --- Marketing ---------------------------------------------------------------

class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(24))  # google_ads / meta_ads
    name: Mapped[str] = mapped_column(String(120))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    daily_budget: Mapped[float] = mapped_column(Float)
    bid: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))  # active / paused
    started_at: Mapped[datetime] = mapped_column(DateTime)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)  # mock Ads API resource name


class CampaignSizeMix(Base):
    __tablename__ = "campaign_size_mix"
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), primary_key=True)
    size: Mapped[str] = mapped_column(String(16), primary_key=True)
    click_share: Mapped[float] = mapped_column(Float)


class AdMetricHourly(Base):
    __tablename__ = "ad_metrics_hourly"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    impressions: Mapped[int] = mapped_column(Integer)
    clicks: Mapped[int] = mapped_column(Integer)
    cpc: Mapped[float] = mapped_column(Float)
    spend: Mapped[float] = mapped_column(Float)
    conversions: Mapped[int] = mapped_column(Integer)
    wasted_clicks: Mapped[int] = mapped_column(Integer, default=0)  # clicks wanting sizes that were out of stock
    wasted_spend: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (UniqueConstraint("campaign_id", "ts"),)


class SiteEvent(Base):
    __tablename__ = "site_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime)  # daily bucket (00:00)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"))
    views: Mapped[int] = mapped_column(Integer)
    add_to_cart: Mapped[int] = mapped_column(Integer)
    oos_page_views: Mapped[int] = mapped_column(Integer)
    __table_args__ = (Index("ix_site_variant_ts", "variant_id", "ts"),)


class CalendarEvent(Base):
    __tablename__ = "calendar_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(80))
    category_uplift: Mapped[str] = mapped_column(Text)  # JSON {seasonality_tag: peak multiplier}
    window_before: Mapped[int] = mapped_column(Integer, default=0)
    window_after: Mapped[int] = mapped_column(Integer, default=0)
    shape: Mapped[str] = mapped_column(String(8), default="ramp")  # ramp / flat


# --- Engine output -----------------------------------------------------------

class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    severity: Mapped[str] = mapped_column(String(16))  # critical / warning / opportunity
    headline: Mapped[str | None] = mapped_column(String(200), nullable=True)
    facts_json: Mapped[str] = mapped_column(Text)
    report_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # Report JSON
    report_source: Mapped[str | None] = mapped_column(String(32), nullable=True)  # gemini / gemini_fallback_model / template
    hours_of_cover: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / approved / dismissed / expired
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)  # dashboard / telegram / api
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Action(Base):
    __tablename__ = "actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    type: Mapped[str] = mapped_column(String(24))  # pause_campaign / shift_budget / reorder
    payload_json: Mapped[str] = mapped_column(Text)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SavingsLedger(Base):
    __tablename__ = "savings_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    ad_spend_avoided: Mapped[float] = mapped_column(Float)
    sales_protected: Mapped[float] = mapped_column(Float)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime)


class AskLog(Base):
    __tablename__ = "ask_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    question: Mapped[str] = mapped_column(Text)
    route: Mapped[str] = mapped_column(String(32))
    answer: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)


class ForecastFactor(Base):
    """Learned forecast factors (written by nightly recalibration, read by the engine)."""
    __tablename__ = "forecast_factors"
    factor_type: Mapped[str] = mapped_column(String(24), primary_key=True)  # seasonal_scale / campaign / size_mix
    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # tag / campaign id / variant id
    value: Mapped[float] = mapped_column(Float)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


# --- Hidden validation data (the engine must never read this) -----------------

class GroundTruth(Base):
    __tablename__ = "ground_truth"
    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="planted_stockout")
    stockout_ts: Mapped[datetime] = mapped_column(DateTime)
    restock_ts: Mapped[datetime] = mapped_column(DateTime)
    lost_units: Mapped[int] = mapped_column(Integer)
    wasted_clicks: Mapped[int] = mapped_column(Integer)
    wasted_spend: Mapped[float] = mapped_column(Float)


class AppMeta(Base):
    __tablename__ = "app_meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
