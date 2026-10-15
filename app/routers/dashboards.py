from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.config import BASE_DIR
from app.schemas import CampaignsOverview, ProductStockRow, SalesOverview, SavingsOverview
from app.services.dashboards import campaigns_overview, sales_overview, savings_overview

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
