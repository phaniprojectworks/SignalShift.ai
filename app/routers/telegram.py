from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.get("/status")
def status(request: Request) -> dict:
    d = request.app.state.delivery
    return {"enabled": d.enabled, "mode": request.app.state.settings.telegram_mode,
            "min_severity": request.app.state.settings.telegram_min_severity}


@router.post("/webhook")
async def webhook(request: Request,
                  x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict:
    """Webhook mode (TELEGRAM_MODE=webhook): Telegram posts updates here. The secret header must match."""
    st = request.app.state.settings
    if not request.app.state.delivery.enabled or st.telegram_mode != "webhook":
        raise HTTPException(404, "webhook mode is not enabled")
    if not st.telegram_webhook_secret or x_telegram_bot_api_secret_token != st.telegram_webhook_secret:
        raise HTTPException(403, "bad secret token")
    outcome = await run_in_threadpool(request.app.state.delivery.handle_update, await request.json())
    return {"ok": True, "outcome": outcome}
