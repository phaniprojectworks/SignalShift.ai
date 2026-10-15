from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.config import BASE_DIR
from app.schemas import ProductStock, SalesToday
from app.services.clock import now
from app.services.dashboards import sales_today
from app.services.inventory import catalog

router = APIRouter(tags=["store"])
WEB = BASE_DIR / "app" / "web"


@router.get("/store", include_in_schema=False)
def store_page() -> FileResponse:
    return FileResponse(WEB / "store.html")


@router.get("/api/catalog", response_model=list[ProductStock])
async def get_catalog(request: Request) -> list[ProductStock]:
    def _q() -> list[ProductStock]:
        with request.app.state.db.session() as s:
            return catalog(s)
    return await run_in_threadpool(_q)


@router.get("/api/stats/today", response_model=SalesToday)
async def get_sales_today(request: Request) -> SalesToday:
    def _q() -> SalesToday:
        with request.app.state.db.session() as s:
            return sales_today(s, now())
    return await run_in_threadpool(_q)
