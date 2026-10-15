"""Demo controls: restore the seeded demo state, and fire an order rush (influencer post / Diwali spike)."""
from __future__ import annotations

import logging
import math
import random
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.models import Product, Variant
from app.schemas import OrderIn, OrderItemIn
from app.services.clock import now as clock_now
from app.services.engine import Engine
from app.services.events import EventBus
from app.services.inventory import OrderError, place_order

log = logging.getLogger("signalshift.demo")

DEFAULT_RUSH_WEIGHTS = {"UK 8": 0.5, "UK 9": 0.2, "UK 7": 0.1, "UK 10": 0.1, "UK 11": 0.1}


class RushIn(BaseModel):
    orders: int = Field(default=10, ge=1, le=200)
    duration_seconds: float = Field(default=15, ge=0, le=600)
    product: str = "Festive White Sneaker"
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_RUSH_WEIGHTS))
    seed: int = 7


def allocate(weights: dict[str, float], n: int) -> dict[str, int]:
    """Largest-remainder split of n orders across sizes (deterministic, sums to n)."""
    total = sum(w for w in weights.values() if w > 0)
    raw = {k: n * w / total for k, w in weights.items() if w > 0}
    out = {k: math.floor(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: n - sum(out.values())]:
        out[k] += 1
    return out


class DemoService:
    def __init__(self, db: Database, bus: EventBus, engine: Engine, settings: Settings,
                 clock: Callable[[], datetime] = clock_now, on_reset: list[Callable[[], None]] | None = None) -> None:
        self.db, self.bus, self.engine, self.settings, self.clock = db, bus, engine, settings, clock
        self.on_reset = on_reset or []
        self._rush_lock = threading.Lock()
        self.rush_status: dict = {"running": False}
        self._cancel = threading.Event()

    # ---------------------------------------------------------------- reset
    def reset(self, anchor: datetime | None = None, warm_up: bool = True) -> dict:
        """Rebuild the seeded demo data (anchored to the current hour by default) into the LIVE database."""
        from scripts.generate_data import default_anchor, generate  # generator lives with the scripts

        self._cancel.set()
        anchor = anchor or default_anchor(self.settings.timezone)
        started = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            seed_path = Path(tmp) / "seed.db"
            summary = generate(str(seed_path), self.settings.seed, anchor, self.settings.calendar_file, reset=True,
                               quiet=True)
            with self.engine._lock:  # no evaluation runs while the data is swapped
                src, dst = sqlite3.connect(seed_path), sqlite3.connect(self.db.path, timeout=30)
                try:
                    src.backup(dst)  # online backup: running connections see the new content
                finally:
                    src.close()
                    dst.close()
                self.engine._snap = None
        for cb in self.on_reset:
            cb()
        self._cancel.clear()
        if warm_up:
            threading.Thread(target=self._warm_up, name="demo-reset-warm-up", daemon=True).start()
        result = {"anchor": anchor.isoformat(), "seconds": round(time.time() - started, 1),
                  "festive_white_stock": summary["festive_white_stock"], "orders": summary["orders"]}
        log.info("demo reset", extra=result)
        self.bus.publish("reset", result)
        return result

    def _warm_up(self) -> None:
        try:
            self.engine.recalibrate()
            self.engine.sweep("startup_sweep")
        except Exception:
            log.exception("post-reset warm-up failed")

    # ---------------------------------------------------------------- rush
    def plan_rush(self, req: RushIn) -> list[str]:
        """Ordered list of SKUs for the rush (allocation by weights, shuffled with a fixed seed)."""
        with self.db.session() as s:
            product = s.scalar(select(Product).where(Product.model == req.product)) or s.scalar(
                select(Product).where(Product.name == req.product))
            if product is None:
                raise ValueError(f"unknown product {req.product!r}")
            skus = {v.size: v.sku for v in s.scalars(select(Variant).where(Variant.product_id == product.id))}
        counts = allocate({k: w for k, w in req.weights.items() if k in skus}, req.orders)
        seq = [skus[size] for size, n in counts.items() for _ in range(n)]
        random.Random(req.seed).shuffle(seq)
        return seq

    def simulate_rush(self, req: RushIn, background: bool = True) -> dict:
        seq = self.plan_rush(req)
        if not self._rush_lock.acquire(blocking=False):
            raise RuntimeError("a rush is already running")
        job = uuid.uuid4().hex[:8]
        self.rush_status = {"running": True, "job": job, "planned": len(seq), "placed": 0, "sold_out": 0}

        def run() -> None:
            try:
                gap = req.duration_seconds / max(len(seq), 1)
                for i, sku in enumerate(seq):
                    if self._cancel.is_set():
                        break
                    try:
                        place_order(self.db, self.bus, OrderIn(items=[OrderItemIn(sku=sku, qty=1)], channel="simulator",
                                                               city=random.Random(i).choice(["Mumbai", "Delhi", "Bengaluru"]),
                                                               idempotency_key=f"rush-{job}-{i}"), self.clock())
                        self.rush_status["placed"] += 1
                    except OrderError:
                        self.rush_status["sold_out"] += 1  # the size sold out mid-rush, as in real life
                    self.bus.publish("rush", dict(self.rush_status))
                    if gap and i < len(seq) - 1:
                        self._cancel.wait(gap)
            finally:
                self.rush_status["running"] = False
                self.bus.publish("rush", dict(self.rush_status))
                self._rush_lock.release()
                log.info("rush finished", extra=dict(self.rush_status))

        if background:
            threading.Thread(target=run, name=f"rush-{job}", daemon=True).start()
        else:
            run()
        return {"job": job, "planned": len(seq), "sizes": {s: seq.count(s) for s in dict.fromkeys(seq)}}
