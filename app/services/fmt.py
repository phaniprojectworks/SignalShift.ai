"""Deterministic formatting shared by the engine summaries and the template reporter."""
from __future__ import annotations

import math


def indian_group(n: int) -> str:
    """12345678 -> 1,23,45,678 (Indian digit grouping)."""
    sign, s = ("-", str(-n)) if n < 0 else ("", str(n))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts + [tail])


def inr(x: float) -> str:
    return f"₹{indian_group(int(round(x)))}"


def pct(share: float) -> str:
    return f"{round(share * 100)}%"


def hours(h: float | None) -> str:
    """Durations round DOWN (cover is never overstated)."""
    if h is None:
        return "no demand"
    if h < 1:
        return f"{max(1, math.floor(h * 60))} min"
    if h < 10:
        return f"{math.floor(h * 10) / 10:.1f} h"
    if h < 48:
        return f"{math.floor(h)} h"
    return f"{math.floor(h / 24)} days"


def size_list(sizes: list[str]) -> str:
    if len(sizes) <= 1:
        return "".join(sizes)
    return ", ".join(sizes[:-1]) + " and " + sizes[-1]
