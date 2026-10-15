"""Pydantic contracts for the API (and, from Phase 3/4, the engine facts and Gemini report)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Channel = Literal["web", "app", "instagram_shop", "store_demo", "simulator"]


# --- Orders --------------------------------------------------------------------

class OrderItemIn(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    qty: int = Field(gt=0, le=20)


class OrderIn(BaseModel):
    items: list[OrderItemIn] = Field(min_length=1, max_length=20)
    channel: Channel = "web"
    city: str = Field(default="Bengaluru", max_length=40)
    idempotency_key: str | None = Field(default=None, max_length=64)


class OrderLineOut(BaseModel):
    variant_id: int
    sku: str
    product_id: int
    product_name: str
    size: str
    qty: int
    unit_price: float
    on_hand_after: int
    available_after: int


class OrderOut(BaseModel):
    order_id: int
    ts: datetime
    total: float
    channel: str
    city: str
    lines: list[OrderLineOut]
    replayed: bool = False  # True when an idempotency key matched an existing order


class ErrorOut(BaseModel):
    code: str
    message: str
    details: dict = Field(default_factory=dict)


# --- Catalog / stats -------------------------------------------------------------

class VariantStock(BaseModel):
    variant_id: int
    sku: str
    size: str
    on_hand: int
    available: int


class ProductStock(BaseModel):
    product_id: int
    name: str
    model: str
    sku_group: str
    price: float
    unit_label: str
    total_available: int
    variants: list[VariantStock]


class SalesToday(BaseModel):
    date: str
    orders: int
    units: int
    revenue: float


# --- Engine facts (the ONLY numbers a report may mention) -------------------------

Severity = Literal["critical", "warning", "opportunity"]
ActionType = Literal["pause_campaign", "shift_budget", "reorder"]


class ForecastFacts(BaseModel):
    base_velocity_per_hr: float  # product-level, festival/campaign effects removed
    seasonal_uplift: float
    campaign_uplift: float
    size_mix_factor: float
    demand_per_hr: float  # expected (surge-adjusted when a spike is detected)
    demand_per_hr_low: float
    demand_per_hr_high: float
    demand_per_day: float
    recent_rate_per_hr: float  # observed in the surge window
    surge: bool
    band_pct: float  # +/- band used for low/high


class SizeCover(BaseModel):
    variant_id: int
    size: str
    available: int
    in_transit: int
    demand_per_hr: float
    hours_of_cover: float | None  # None when there is no demand
    click_share: float | None  # share of campaign clicks wanting this size
    status: Literal["out", "low", "ok"]


class CampaignFacts(BaseModel):
    campaign_id: int
    name: str
    platform: str
    daily_budget: float
    clicks_per_hr: float
    avg_cpc: float
    expected_roas: float
    at_risk_click_share: float  # clicks landing on sizes that are low or out
    healthy_click_share: float
    at_risk_spend_per_day: float
    recommended: Literal["pause_campaign", "shift_budget", "none"]
    freed_budget: float


class BudgetTarget(BaseModel):
    product_id: int
    product_name: str
    campaign_id: int
    campaign_name: str
    current_budget: float
    amount: float
    cover_days: float  # projected (festival-aware), including stock in transit
    cover_days_on_hand: float  # on-hand stock at today's demand
    expected_roas: float
    margin_pct: float
    score: float


class ReorderLine(BaseModel):
    variant_id: int
    size: str
    qty: int
    projected_demand: float
    available: int
    in_transit: int


class EventFacts(BaseModel):
    name: str
    date: str
    days_until: int
    window_end: str
    peak_uplift: float


class RecommendedAction(BaseModel):
    type: ActionType
    summary: str  # deterministic, built from facts
    campaign_id: int | None = None
    amount: float | None = None
    targets: list[BudgetTarget] = Field(default_factory=list)
    lines: list[ReorderLine] = Field(default_factory=list)


class Facts(BaseModel):
    severity: Severity
    rule: str  # which rule fired, in words
    trigger: str  # order / hourly_refresh / daily_briefing / campaign_activated / restock_delayed / ...
    anomalies: list[str] = Field(default_factory=list)
    generated_at: datetime
    currency: str = "INR"

    product_id: int
    product_name: str
    variant_id: int
    sku: str
    size: str
    unit_label: str
    price: float
    margin_pct: float

    available: int
    on_hand: int
    reserved: int
    in_transit: int
    lead_time_days: int
    restock_eta: datetime | None = None
    restock_overdue: bool = False
    product_total_available: int

    forecast: ForecastFacts | None = None
    hours_of_cover: float | None = None
    hours_of_cover_low: float | None = None  # pessimistic (high demand)
    hours_of_cover_high: float | None = None  # optimistic (low demand)
    days_of_cover: float | None = None
    stockout_eta: datetime | None = None

    sizes: list[SizeCover] = Field(default_factory=list)
    at_risk_sizes: list[str] = Field(default_factory=list)
    at_risk_click_share: float | None = None
    campaigns: list[CampaignFacts] = Field(default_factory=list)

    wasted_spend_per_hr: float | None = None  # once out of stock, while the ad keeps running
    wasted_spend_horizon_hours: float | None = None
    wasted_spend_estimate: float | None = None
    lost_units_estimate: float | None = None  # demand for at-risk sizes that can't be served before restock (bounded)
    lost_sales_estimate: float | None = None  # lost_units_estimate x price

    freed_budget: float | None = None
    budget_moved: float | None = None
    budget_held_back: float | None = None  # freed but not reassigned (targets are capped at +100% budget)
    budget_targets: list[BudgetTarget] = Field(default_factory=list)
    reorder: list[ReorderLine] = Field(default_factory=list)
    reorder_total: int | None = None
    event: EventFacts | None = None
    projected_demand: float | None = None
    projected_supply: float | None = None  # available + in-transit arriving before the event ends
    projected_shortfall: float | None = None

    thresholds: dict[str, float] = Field(default_factory=dict)
    actions: list[RecommendedAction] = Field(default_factory=list)


class AlertOut(BaseModel):
    id: int
    ts: datetime
    variant_id: int
    campaign_id: int | None
    severity: str
    headline: str | None
    status: str
    hours_of_cover: float | None
    cooldown_until: datetime | None
    report_source: str | None
    decided_by: str | None = None
    decided_at: datetime | None = None
    facts: Facts
    report: dict | None = None


# --- Gemini report (language only; every number must come from Facts) -----------------

class ReportRecommendation(BaseModel):
    action_type: Literal["pause_campaign", "shift_budget", "reorder", "monitor"] = Field(
        description="Which engine action this recommendation corresponds to.")
    text: str = Field(description="One short imperative sentence for the shop owner.")


class Report(BaseModel):
    headline: str = Field(description="At most 12 words. States the problem and the size.")
    summary: str = Field(description="2-3 short sentences in plain language, like a personal assistant.")
    why: list[str] = Field(min_length=2, max_length=3, description="2-3 short reasons, each grounded in the facts.")
    recommendations: list[ReportRecommendation] = Field(
        min_length=1, max_length=4, description="Ordered actions, in the same order as the facts' actions.")
    estimated_impact: str = Field(description="One sentence on money/sales at stake, using only facts numbers.")


# --- Dashboards ----------------------------------------------------------------------

StockStatus = Literal["out", "low", "short", "ok"]  # short = runs out before restock can land


class SizeCell(BaseModel):
    variant_id: int
    sku: str
    size: str
    on_hand: int
    available: int
    in_transit: int
    restock_eta: datetime | None
    demand_per_day: float
    hours_of_cover: float | None
    click_share: float | None
    status: StockStatus


class ProductStockRow(BaseModel):
    product_id: int
    name: str
    model: str
    sku_group: str
    unit_label: str
    price: float
    total_available: int
    demand_per_day: float
    cover_days: float | None
    lead_time_days: int
    campaign_live: bool
    worst_status: StockStatus
    sizes: list[SizeCell]


class HourPoint(BaseModel):
    hour: int
    orders: int
    revenue: float


class TopProduct(BaseModel):
    product_id: int
    name: str
    units: int
    revenue: float


class SalesOverview(BaseModel):
    as_of: datetime
    today: SalesToday
    by_hour_today: list[HourPoint]
    by_hour_yesterday: list[HourPoint]
    top_products: list[TopProduct]


class CampaignSize(BaseModel):
    size: str
    click_share: float
    status: StockStatus


class CampaignRow(BaseModel):
    campaign_id: int
    name: str
    platform: str
    product_id: int
    product_name: str
    status: str
    daily_budget: float
    spend_today: float
    clicks_today: int
    conversions_today: int
    wasted_spend_today: float
    wasted_spend_30d: float
    at_risk_click_share: float
    sizes: list[CampaignSize]


class CampaignsOverview(BaseModel):
    as_of: datetime
    spend_today: float
    clicks_today: int
    wasted_spend_today: float
    wasted_spend_30d: float
    campaigns: list[CampaignRow]


# --- Sales page: order log + per-SKU trend ---------------------------------------------

class OrderLine(BaseModel):
    sku: str
    product_name: str
    size: str
    qty: int
    price: float


class OrderLogRow(BaseModel):
    order_id: int
    ts: datetime
    channel: str
    city: str
    total: float
    units: int
    lines: list[OrderLine]


class OrderLog(BaseModel):
    total: int  # matching orders, before paging
    limit: int
    offset: int
    revenue: float  # of all matching orders
    units: int
    rows: list[OrderLogRow]


class SkuTrendRow(BaseModel):
    variant_id: int
    sku: str
    product_id: int
    product_name: str
    sku_group: str
    size: str
    weekly_units: list[int]  # oldest -> newest, one per 7-day window ending now
    units_by_month: list[int]  # 30-day windows, oldest -> newest (days 61-90, 31-60, last 30)
    units: int
    revenue: float
    change_pct: float | None  # last 30 days vs the 30 before; None when the earlier window had no sales


class SkuTrend(BaseModel):
    as_of: datetime
    days: int
    week_starts: list[datetime]
    rows: list[SkuTrendRow]


class DayPoint(BaseModel):
    date: str
    units: int
    revenue: float


class SkuDaily(BaseModel):
    variant_id: int
    sku: str
    product_name: str
    size: str
    days: list[DayPoint]


class LedgerEntry(BaseModel):
    alert_id: int
    ts: datetime
    ad_spend_avoided: float
    sales_protected: float
    explanation: str | None


class SavingsOverview(BaseModel):
    ad_spend_avoided: float
    sales_protected: float
    stockouts_prevented: int
    actions_executed: int
    entries: list[LedgerEntry]


# --- Actions / decisions --------------------------------------------------------------

class ExecutedAction(BaseModel):
    type: str
    campaign_id: int | None = None
    request: dict
    response: dict
    mock: bool = True  # executed against a MOCK API (no real ad platform / supplier is contacted)


class DecisionOut(BaseModel):
    alert_id: int
    status: Literal["approved", "dismissed"]
    actor: str
    decided_at: datetime
    actions: list[ExecutedAction] = Field(default_factory=list)
    ledger: LedgerEntry | None = None


# --- Customer ads (mock ad serving) ----------------------------------------------------

class AdOut(BaseModel):
    campaign_id: int
    platform: str
    platform_label: str
    product_id: int
    product_name: str
    model: str
    color: str
    sku_group: str
    sku: str
    size: str  # the size the ad features; whether it is in stock is deliberately not part of the ad
    price: float
    tagline: str


class AdClickIn(BaseModel):
    campaign_id: int
    sku: str = Field(min_length=1, max_length=40)


class AdClickOut(BaseModel):
    campaign_id: int
    campaign_name: str
    platform: str
    product_id: int
    product_name: str
    sku_group: str
    sku: str
    size: str
    available: int
    in_stock: bool
    charged: bool  # False when the campaign is paused (it no longer serves ads)
    cost: float
    wasted: bool  # charged for a size that cannot be bought
