from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from app.schemas import AdClickIn, AdClickOut, AdOut
from app.services.ads import AdError, record_click, serve_ads

router = APIRouter(tags=["ads"])


@router.get("/api/ads", response_model=list[AdOut])
async def ads(request: Request, n: int = Query(8, ge=1, le=24)) -> list[AdOut]:
    """MOCK ad serving for the customer Ads page: random ads from active campaigns, by size click mix."""
    return await run_in_threadpool(serve_ads, request.app.state.db, n)


@router.post("/api/ads/click", response_model=AdClickOut)
async def click(payload: AdClickIn, request: Request) -> AdClickOut:
    """A shopper clicked an ad. Charged at the campaign's CPC; wasted when the advertised size is sold out."""
    try:
        return await run_in_threadpool(record_click, request.app.state.db, request.app.state.bus,
                                       request.app.state.clock(), payload.campaign_id, payload.sku)
    except AdError as e:
        raise HTTPException(e.status_code, e.message) from e
