"""MOCK ad serving for the customer "Ads for you" page. Synthetic: not connected to Google Ads or Meta.

Ads come only from ACTIVE campaigns. Each ad features one size from the campaign's size click mix (weighted the
same way real clicks are), so some ads point at sizes that are sold out: the ad itself never knows.

A click is charged to the campaign's current hour in `ad_metrics_hourly` at its recent CPC, exactly like the mock
ad simulator. When the featured size has nothing available, the click is also recorded as wasted, so the owner's
"Wasted on out-of-stock clicks" tile moves the moment a shopper lands on a sold-out size.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime

from sqlalchemy import select

from app.db import Database, sql_ts
from app.models import Campaign, CampaignSizeMix, Inventory, Product, Variant
from app.schemas import AdClickOut, AdOut
from app.services.ad_simulator import UPSERT, recent_cpc_cvr
from app.services.events import EventBus

log = logging.getLogger("signalshift.ads")

TAGLINES = {
    "hero": ["Diwali drop: step out in style", "The festive pair everyone is wearing", "Limited festive edition"],
    "core": ["Everyday comfort, all day long", "Your new daily favourite", "Light, easy, made to walk"],
    "accessory": ["The gift that fits every budget", "Festive gifting, sorted", "Add a little extra to the season"],
    "slow": ["Last season, lower price", "Clearance picks before they go"],
}
PLATFORM_LABEL = {"google_ads": "Google", "meta_ads": "Instagram"}


class AdError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code, self.message = status_code, message


def serve_ads(db: Database, n: int = 8, rng: random.Random | None = None) -> list[AdOut]:
    """Up to `n` ads, one per SKU, sampled from active campaigns by their size click mix.

    When any advertised size is sold out, at least one such ad is included, and at least one in-stock ad too,
    so the page shows both cases. Which is which is not revealed to the shopper."""
    rng = rng or random.Random()
    with db.session() as s:
        rows = s.execute(
            select(Campaign, Product, Variant, CampaignSizeMix.click_share, Inventory.on_hand - Inventory.reserved)
            .join(Product, Product.id == Campaign.product_id)
            .join(CampaignSizeMix, CampaignSizeMix.campaign_id == Campaign.id)
            .join(Variant, (Variant.product_id == Product.id) & (Variant.size == CampaignSizeMix.size))
            .join(Inventory, Inventory.variant_id == Variant.id)
            .where(Campaign.status == "active")).all()
    pool = [(c, p, v, max(share, 0.01), avail) for c, p, v, share, avail in rows]
    picked: list[tuple] = []
    seen: set[str] = set()

    def take(cands: list[tuple]) -> None:
        cands = [x for x in cands if x[2].sku not in seen]
        if not cands:
            return
        x = rng.choices(cands, weights=[w for *_, w, _ in cands])[0]
        picked.append(x)
        seen.add(x[2].sku)

    take([x for x in pool if x[4] <= 0])  # one ad for a sold-out size, when there is one
    take([x for x in pool if x[4] > 0])  # and one that can be bought
    while len(picked) < n and len(seen) < len({x[2].sku for x in pool}):
        take(pool)
    rng.shuffle(picked)
    return [AdOut(campaign_id=c.id, platform=c.platform, platform_label=PLATFORM_LABEL.get(c.platform, c.platform),
                  product_id=p.id, product_name=p.name, model=p.model, color=v.color, sku_group=p.sku_group,
                  sku=v.sku, size=v.size, price=p.price, tagline=rng.choice(TAGLINES.get(p.sku_group, TAGLINES["core"])))
            for c, p, v, _, _ in picked[:n]]


def record_click(db: Database, bus: EventBus | None, now: datetime, campaign_id: int, sku: str) -> AdClickOut:
    """Charge one click to the campaign; record it as wasted when the advertised size has nothing available."""
    with db.write() as s:
        c = s.get(Campaign, campaign_id)
        v = s.scalar(select(Variant).where(Variant.sku == sku))
        if c is None or v is None:
            raise AdError(404, "unknown campaign or SKU")
        if v.product_id != c.product_id:
            raise AdError(400, "that SKU is not advertised by this campaign")
        p = s.get(Product, v.product_id)
        inv = s.get(Inventory, v.id)
        available = (inv.on_hand - inv.reserved) if inv else 0
        in_stock = available > 0
        charged = c.status == "active"  # a paused campaign serves no ads, so a stale click costs nothing
        cost = 0.0
        if charged:
            cpc, _ = recent_cpc_cvr(s, c, now)
            cost = round(cpc, 2)
            s.execute(UPSERT, {"ts": sql_ts(now.replace(minute=0, second=0, microsecond=0)), "cid": c.id, "imp": 1,
                               "clk": 1, "cpc": cost, "spend": cost, "conv": 0, "wclk": 0 if in_stock else 1,
                               "wspend": 0.0 if in_stock else cost})
    out = AdClickOut(campaign_id=c.id, campaign_name=c.name, platform=c.platform, product_id=p.id,
                     product_name=p.name, sku_group=p.sku_group, sku=v.sku, size=v.size, available=max(available, 0),
                     in_stock=in_stock, charged=charged, cost=cost, wasted=charged and not in_stock)
    log.info("ad click", extra={"campaign_id": c.id, "sku": v.sku, "in_stock": in_stock, "cost": cost, "wasted": out.wasted})
    if bus is not None and charged:
        bus.publish("ads", {"ticks": [{"campaign_id": c.id, "clicks": 1, "spend": cost,
                                       "wasted_spend": out.cost if out.wasted else 0.0}]})
        bus.publish("ad_click", out.model_dump())
    return out
