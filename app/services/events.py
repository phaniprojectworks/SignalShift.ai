"""In-process event bus: an asyncio queue feeding a consumer, plus fan-out to SSE subscribers.

`publish()` is thread-safe, so synchronous service code (running in FastAPI's threadpool)
can emit events. Each event gets a monotonically increasing id, used for SSE `Last-Event-ID`
replay. The consumer de-duplicates on `key`, so re-delivered events are handled once.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.clock import now

log = logging.getLogger("signalshift.events")

Handler = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    id: int
    type: str  # sale / inventory / alert / action / ...
    ts: str
    data: dict[str, Any] = field(default_factory=dict)
    key: str | None = None  # idempotency key, e.g. "order:123"

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "ts": self.ts, "data": self.data}


class EventBus:
    def __init__(self, history: int = 500, subscriber_buffer: int = 200, seen_keys: int = 5000) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._history: deque[Event] = deque(maxlen=history)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._subscriber_buffer = subscriber_buffer
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_max = seen_keys
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[Event] | None = None
        self._task: asyncio.Task[None] | None = None

    # --- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._consume(), name="event-consumer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._loop = None

    # --- publishing ------------------------------------------------------------
    def publish(self, type: str, data: dict[str, Any], key: str | None = None) -> Event:
        with self._lock:
            ev = Event(id=self._next_id, type=type, ts=now().isoformat(timespec="seconds"), data=data, key=key)
            self._next_id += 1
            self._history.append(ev)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                self._dispatch(ev)
            else:
                loop.call_soon_threadsafe(self._dispatch, ev)
        return ev

    def _dispatch(self, ev: Event) -> None:
        for q in list(self._subscribers):
            if q.full():  # slow client: drop its oldest event rather than block everyone
                q.get_nowait()
            q.put_nowait(ev)
        if self._queue is not None:
            self._queue.put_nowait(ev)

    # --- SSE subscribers -----------------------------------------------------
    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._subscriber_buffer)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    def replay_since(self, last_id: int) -> list[Event]:
        with self._lock:
            return [e for e in self._history if e.id > last_id]

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._next_id - 1

    # --- consumer ------------------------------------------------------------
    def on(self, type: str, handler: Handler) -> None:
        """Register a synchronous handler; it runs in a worker thread. Use "*" for every event."""
        self._handlers[type].append(handler)

    def _first_time(self, key: str | None) -> bool:
        if key is None:
            return True
        if key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > self._seen_max:
            self._seen.popitem(last=False)
        return True

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            ev = await self._queue.get()
            if not self._first_time(ev.key):
                log.info("duplicate event skipped", extra={"event_key": ev.key})
                continue
            for handler in [*self._handlers.get(ev.type, []), *self._handlers.get("*", [])]:
                try:
                    await asyncio.to_thread(handler, ev)
                except Exception:  # a failing handler must never stop the pipeline
                    log.exception("event handler failed", extra={"event_type": ev.type, "event_id": ev.id})
