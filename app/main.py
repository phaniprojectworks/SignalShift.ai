"""FastAPI app factory: routers, SSE stream, event bus (and, from Phase 3, the scheduler).

Run:  uvicorn app.main:create_app --factory      (or: python run.py)
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Header, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, Settings, get_settings
from app.db import Database, init_db
from app.logging_setup import setup_logging
from app.routers import alerts, ask, dashboards, demo, orders, store, telegram
from app.services.demo import DemoService
from app.services.ask import AskService
from app.services.actions import ActionService
from app.services.delivery import Delivery, TelegramClient
from app.services.ad_simulator import AdSimulator
from app.services.calendar import load_calendar
from app.services.clock import now as clock_now
from app.services.engine import Engine
from app.services.events import Event, EventBus
from app.services.reporter import LLMClient, ReportService
from app.services.triggers import EvaluateFn, build_scheduler, register_event_triggers

log = logging.getLogger("signalshift.app")

SSE_HEARTBEAT_SECONDS = 15.0


def _sse(ev: Event) -> str:
    return f"id: {ev.id}\nevent: {ev.type}\ndata: {json.dumps(ev.as_dict(), default=str)}\n\n"


def create_app(db_path: str | None = None, evaluate: EvaluateFn | None = None, settings: Settings | None = None,
               clock: Callable[[], datetime] = clock_now, llm_client: LLMClient | None = None,
               telegram_client: TelegramClient | None = None) -> FastAPI:
    """`evaluate` overrides the engine for order events, `llm_client` the Gemini client and `telegram_client`
    the Telegram client (tests); `clock` lets tests run at the data's anchor time."""
    setup_logging()
    settings = settings or get_settings()
    db = Database(db_path or settings.database_path)
    init_db(db.engine)
    bus = EventBus()
    engine = Engine(db, bus, settings, load_calendar(settings.calendar_file), clock=clock)
    reports = ReportService.for_database(db, bus, settings, client=llm_client)
    engine.hooks.append(reports.on_alert)
    actions = ActionService(db, bus, clock)
    delivery = Delivery(db, settings, actions, client=telegram_client)
    reports.on_saved.append(delivery.on_report)
    ask_service = AskService(db, engine, reports.reporter.client if reports.reporter.llm_enabled else None,
                             reports.reporter._models() if reports.reporter.llm_enabled else [], clock)
    actions.on_decided.append(delivery.on_decided)

    def _warm_up() -> None:
        try:
            engine.ensure_calibrated()
            if settings.startup_sweep:
                engine.sweep("startup_sweep")
        except Exception:
            log.exception("startup warm-up failed")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await bus.start()
        register_event_triggers(bus, evaluate or (lambda ids, trig: engine.evaluate_variants(ids, trig)),
                                on_campaign_activated=engine.campaign_activated)
        bus.on("briefing", delivery.on_briefing)
        delivery.start_polling()
        simulator = AdSimulator(db, bus, settings, clock) if settings.ad_sim_enabled else None
        scheduler = build_scheduler(engine, bus, settings, simulator) if settings.scheduler_enabled else None
        if scheduler:
            scheduler.start()
        if evaluate is None:
            threading.Thread(target=_warm_up, name="engine-warm-up", daemon=True).start()
        yield
        if scheduler:
            scheduler.shutdown(wait=False)
        delivery.stop()
        reports.shutdown()
        await bus.stop()
        db.dispose()

    app = FastAPI(title="SignalShift AI", version="0.3.0", lifespan=lifespan)
    app.state.db = db
    app.state.bus = bus
    app.state.engine = engine
    app.state.reports = reports
    app.state.actions = actions
    app.state.delivery = delivery
    app.state.ask = ask_service
    app.state.demo = DemoService(db, bus, engine, settings, clock, on_reset=[delivery._texts.clear])
    app.state.replay_cache = None
    app.state.settings = settings
    app.state.clock = clock
    app.include_router(orders.router)
    app.include_router(store.router)
    app.include_router(alerts.router)
    app.include_router(dashboards.router)
    app.include_router(telegram.router)
    app.include_router(ask.router)
    app.include_router(demo.router)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "web"), name="static")
    http_log = logging.getLogger("signalshift.http")

    @app.middleware("http")
    async def timing(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        response = await call_next(request)
        if request.url.path.startswith(("/api/", "/alerts", "/orders", "/engine")):
            http_log.info("request", extra={"path": request.url.path, "status": response.status_code,
                                            "ms": round((time.perf_counter() - started) * 1000)})
        return response

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    @app.get("/stream", tags=["events"])
    async def stream(request: Request, last_event_id: str | None = Header(default=None),
                     max_events: int | None = None) -> StreamingResponse:
        """Server-Sent Events: sale, inventory and alert updates. Honours Last-Event-ID for replay.
        `max_events` closes the stream after N events (useful for tests and curl)."""
        queue = bus.subscribe()  # subscribe before replay so nothing falls in the gap

        async def gen() -> AsyncIterator[str]:
            sent, last = 0, 0
            try:
                yield "retry: 3000\n\n"
                if last_event_id and last_event_id.isdigit():
                    for ev in bus.replay_since(int(last_event_id)):
                        yield _sse(ev)
                        sent, last = sent + 1, ev.id
                        if max_events and sent >= max_events:
                            return
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if ev.id <= last:
                        continue
                    yield _sse(ev)
                    sent, last = sent + 1, ev.id
                    if max_events and sent >= max_events:
                        return
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
