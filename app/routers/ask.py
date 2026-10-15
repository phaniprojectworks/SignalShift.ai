from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.models import AskLog
from app.services.ask import AskOut

router = APIRouter(tags=["ask"])


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)


@router.post("/ask", response_model=AskOut)
async def ask(payload: AskIn, request: Request) -> AskOut:
    return await run_in_threadpool(request.app.state.ask.ask, payload.question)


@router.get("/ask/log")
async def ask_log(request: Request, limit: int = 20) -> list[dict]:
    def _q() -> list[dict]:
        with request.app.state.db.session() as s:
            return [{"id": r.id, "ts": r.ts, "question": r.question, "route": r.route, "answer": r.answer}
                    for r in s.scalars(select(AskLog).order_by(AskLog.id.desc()).limit(min(limit, 100)))]
    return await run_in_threadpool(_q)
