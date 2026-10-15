"""Festival calendar: loads the configurable event list and computes per-day demand multipliers."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class CalendarEventDef:
    name: str
    date: date
    window_before: int
    window_after: int
    shape: str  # ramp / flat
    uplift: dict[str, float]

    @property
    def start(self) -> date:
        return self.date - timedelta(days=self.window_before)

    @property
    def end(self) -> date:
        return self.date + timedelta(days=self.window_after)


def load_calendar(path: str | Path) -> list[CalendarEventDef]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    events: list[CalendarEventDef] = []
    for ev in raw["events"]:
        for d in ev["dates"]:
            events.append(
                CalendarEventDef(
                    name=ev["name"],
                    date=date.fromisoformat(d),
                    window_before=int(ev.get("window_before", 0)),
                    window_after=int(ev.get("window_after", 0)),
                    shape=ev.get("shape", "ramp"),
                    uplift={k: float(v) for k, v in ev["uplift"].items()},
                )
            )
    return sorted(events, key=lambda e: e.date)


def event_multiplier(ev: CalendarEventDef, day: date, tag: str) -> float:
    """Multiplier this single event contributes on `day` for a seasonality tag (1.0 = no effect)."""
    peak = ev.uplift.get(tag, 1.0)
    if day < ev.start or day > ev.end:
        return 1.0
    if ev.shape == "flat":
        return peak
    if day <= ev.date:
        frac = (ev.window_before - (ev.date - day).days + 1) / (ev.window_before + 1)
    else:
        frac = 1.0 - (day - ev.date).days / (ev.window_after + 1)
    return 1.0 + (peak - 1.0) * max(0.0, min(1.0, frac))


def festival_multiplier(events: list[CalendarEventDef], day: date, tag: str) -> float:
    """Combined multiplier of all events covering `day` (overlapping events stack multiplicatively)."""
    mult = 1.0
    for ev in events:
        mult *= event_multiplier(ev, day, tag)
    return mult


def is_sale_day(events: list[CalendarEventDef], day: date) -> bool:
    return any("Sale" in ev.name and ev.start <= day <= ev.end for ev in events)
