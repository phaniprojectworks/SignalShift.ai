from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.schemas import ErrorOut, OrderIn, OrderOut
from app.services.inventory import OrderError, place_order

router = APIRouter(tags=["orders"])


@router.post("/orders", response_model=OrderOut, status_code=201,
             responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}})
async def create_order(payload: OrderIn, request: Request) -> OrderOut | JSONResponse:
    try:
        return await run_in_threadpool(place_order, request.app.state.db, request.app.state.bus, payload,
                                       request.app.state.clock())
    except OrderError as e:
        return JSONResponse(status_code=e.status_code,
                            content=ErrorOut(code=e.code, message=e.message, details=e.details).model_dump())
