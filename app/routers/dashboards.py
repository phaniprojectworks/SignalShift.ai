from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.config import BASE_DIR
from app.schemas import (CampaignsOverview, OrderLog, ProductStockRow, SalesOverview, SavingsOverview, SkuDaily, SkuTrend)
from app.services.dashboards import (campaigns_overview, order_log, sales_overview, savings_overview, sku_daily, sku_trend)

router = APIRouter(tags=["dashboards"])
WEB = BASE_DIR / "app" / "web"


@router.get("/dashboard", include_in_schema=False)
def dashboard_page() -> FileResponse:
    return FileResponse(WEB / "index.html")


@router.get("/api/dashboard/sales", response_model=SalesOverview)
async def sales(request: Request) -> SalesOverview:
    def _q() -> SalesOverview:
        with request.app.state.db.session() as s:
            return sales_overview(s, request.app.state.clock())
    return await run_in_threadpool(_q)


@router.get("/api/dashboard/inventory", response_model=list[ProductStockRow])
async def inventory(request: Request) -> list[ProductStockRow]:
    return await run_in_threadpool(request.app.state.engine.stock_snapshot)


@router.get("/api/dashboard/campaigns", response_model=CampaignsOverview)
async def campaigns(request: Request) -> CampaignsOverview:
    def _q() -> CampaignsOverview:
        stock = request.app.state.engine.stock_snapshot()
        with request.app.state.db.session() as s:
            return campaigns_overview(s, request.app.state.clock(), stock)
    return await run_in_threadpool(_q)


@router.get("/api/dashboard/savings", response_model=SavingsOverview)
async def savings(request: Request) -> SavingsOverview:
    def _q() -> SavingsOverview:
        with request.app.state.db.session() as s:
            return savings_overview(s)
    return await run_in_threadpool(_q)


@router.get("/api/dashboard/orders", response_model=OrderLog)
async def orders(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                 q: str | None = Query(None, max_length=80), channel: str | None = Query(None, max_length=24),
                 days: int | None = Query(None, ge=1, le=3650)) -> OrderLog:
    """Order log for the Sales page, newest first. `q` matches an order number, SKU, product, size or city."""
    def _q() -> OrderLog:
        with request.app.state.db.session() as s:
            return order_log(s, request.app.state.clock(), limit, offset, q, channel, days)
    return await run_in_threadpool(_q)


@router.get("/api/dashboard/sku-trend", response_model=SkuTrend)
async def trend(request: Request, weeks: int = Query(13, ge=4, le=26)) -> SkuTrend:
    """Units sold per SKU, by week and by 30-day window, over the last `weeks` weeks (13 = about 3 months)."""
    def _q() -> SkuTrend:
        with request.app.state.db.session() as s:
            return sku_trend(s, request.app.state.clock(), weeks)
    return await run_in_threadpool(_q)


@router.get("/api/dashboard/sku-trend/{variant_id}", response_model=SkuDaily)
async def trend_daily(variant_id: int, request: Request, days: int = Query(91, ge=7, le=365)) -> SkuDaily:
    def _q() -> SkuDaily | None:
        with request.app.state.db.session() as s:
            return sku_daily(s, request.app.state.clock(), variant_id, days)
    out = await run_in_threadpool(_q)
    if out is None:
        raise HTTPException(404, "unknown variant")
    return out

