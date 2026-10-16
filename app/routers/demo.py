from __future__ import annotations

import threading
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.services.demo import RushIn
from app.services.replay import ReplayConfig, run_replay

router = APIRouter(tags=["demo"])
_replay_lock = threading.Lock()


class ResetIn(BaseModel):
    anchor: datetime | None = None  # default: the current hour


@router.post("/demo/reset")
async def reset(request: Request, payload: ResetIn | None = None) -> dict:
    """Restore the seeded demo state (fresh history up to now). Takes ~10 s."""
    request.app.state.replay_cache = None
    return await run_in_threadpool(request.app.state.demo.reset, payload.anchor if payload else None)


@router.post("/demo/simulate-rush")
async def simulate_rush(request: Request, payload: RushIn | None = None) -> dict:
    """Burst of orders weighted to UK 8 and UK 9 (influencer post / Diwali rush), placed in the background."""
    try:
        return await run_in_threadpool(request.app.state.demo.simulate_rush, payload or RushIn())
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/demo/status")
def status(request: Request) -> dict:
    return {"rush": request.app.state.demo.rush_status}


@router.get("/savings/replay")
async def replay(request: Request, days: int = 30, approval_delay_hours: float = 1.0, fresh: bool = False) -> dict:
    """Counterfactual replay of the synthetic history with vs without SignalShift.ai (cached ~5 min)."""
    def _run() -> dict:
        with _replay_lock:
            key = (days, approval_delay_hours)
            cached = request.app.state.replay_cache
            if cached and not fresh and cached[0] == key and time.time() - cached[1] < 300:
                return cached[2]
            result = run_replay(request.app.state.db, request.app.state.settings, request.app.state.engine.events,
                                ReplayConfig(days=days, approval_delay_hours=approval_delay_hours))
            request.app.state.replay_cache = (key, time.time(), result)
            return result
    return await run_in_threadpool(_run)
