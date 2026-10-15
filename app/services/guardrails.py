"""Guardrails for generated text: every number, size, product and action must be grounded in the facts.

Number check
    Every number in the text (currency, percentage, duration, count, plain) must match a value from the facts
    JSON: within 2% (+0.05), or the value rounded / rounded down to a whole number (never rounded UP, which
    would overstate cover). Matching is unit-aware: a number written with a unit must match a fact of that
    unit - currency (₹/Rs/INR) only money fields; % only share/percent fields (0.68 -> 68%); hours/days/minutes
    only duration fields, with conversions (72 h -> 3 days, 1.1 h -> 66 min, 12 days -> 288 h). A bare number
    may match any fact. Dates/times contribute their parts and list lengths allow counts. IDs and internal
    scores are never allowed. Amounts written with k / lakh / crore are expanded first. Names of known products and campaigns are masked so the
    digits inside them ("Retro Runner '86", "3-Pack") are not mistaken for figures.

Entity check
    Every size mentioned ("UK 9", "UK 6-8") must be a size in the facts; every catalog product mentioned
    must be one the facts talk about (the alert's product or a budget-shift target).

Claim check
    Every recommendation's action_type must be one of the facts' recommended actions (or "monitor").
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from typing import Any

from app.schemas import Facts, Report

TOLERANCE_REL = 0.02
TOLERANCE_ABS = 0.05
EXCLUDED_KEYS = ("_id", "score", "size_rank")

SIZE_RE = re.compile(r"\bUK\s?(\d{1,2})(?:\s?[-–]\s?(\d{1,2}))?\b", re.IGNORECASE)
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
NUM_RE = re.compile(
    r"(?P<cur>₹|\bRs\.?|\bINR)?\s?"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s?(?P<mult>lakh|lakhs|crore|crores|k\b|K\b)?"
    r"\s?(?P<unit>%|percent\b|per cent\b|hours?\b|hrs?\b|h\b|minutes?\b|mins?\b|days?\b)?",
)
MULTIPLIERS = {"lakh": 1e5, "lakhs": 1e5, "crore": 1e7, "crores": 1e7, "k": 1e3}
MONEY_KEYS = {"daily_budget", "current_budget", "at_risk_spend_per_day", "freed_budget", "price", "avg_cpc",
              "wasted_spend_per_hr", "wasted_spend_estimate", "lost_sales_estimate", "budget_moved",
              "budget_held_back", "amount"}


def _normalize_size(m: re.Match[str]) -> str:
    return f"UK {m.group(1)}-{m.group(2)}" if m.group(2) else f"UK {m.group(1)}"


def _walk(obj: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    """Yield (field name, leaf value); list lengths are yielded as ("<len>", n)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, k)
    elif isinstance(obj, list):
        yield "<len>", len(obj)
        for v in obj:
            yield from _walk(v, key)
    else:
        yield key, obj


def allowed_values(facts: Facts) -> dict[str, set[float]]:
    """Every number the text may use, keyed by unit: plain / currency / percent / h / d / min."""
    return allowed_values_from(facts.model_dump(mode="json"))


def allowed_values_from(evidence: Any) -> dict[str, set[float]]:
    """Same as allowed_values, for any JSON-like evidence (Ask mode query results, retrieved alerts)."""
    out: dict[str, set[float]] = {k: set() for k in ("plain", "currency", "percent", "h", "d", "min")}

    def add(unit: str, x: float) -> None:
        out[unit].add(round(x, 4))

    for key, v in _walk(evidence):
        if isinstance(v, bool) or v is None or key.endswith(EXCLUDED_KEYS):
            continue
        if isinstance(v, (int, float)):
            v = float(v)
            add("plain", v)
            if key in MONEY_KEYS or key.endswith(("revenue", "spend", "avoided", "protected", "_inr")):
                add("currency", v)
            if "share" in key or key.endswith("pct"):
                add("percent", v * 100)
            if "hours" in key:  # durations in hours (not "_per_hr" rates)
                add("h", v)
                add("d", v / 24)
                add("min", v * 60)
            if "days" in key:
                add("d", v)
                add("h", v * 24)
        elif isinstance(v, str):
            for parsed in _parse_temporal(v):
                for part in parsed:
                    add("plain", float(part))
    return out


def _unit_of(m: re.Match[str]) -> str:
    if m.group("cur"):
        return "currency"
    u = (m.group("unit") or "").lower()
    if not u:
        return "plain"
    if u.startswith(("%", "percent", "per cent")):
        return "percent"
    if u.startswith(("hour", "hr")) or u == "h":
        return "h"
    if u.startswith("min"):
        return "min"
    return "d"


def _parse_temporal(s: str) -> Iterator[tuple[int, ...]]:
    try:
        if len(s) >= 16 and s[4] == "-" and "T" in s:
            dt = datetime.fromisoformat(s)
            yield (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.hour % 12 or 12)
        elif len(s) == 10 and s[4] == "-":
            d = date.fromisoformat(s)
            yield (d.year, d.month, d.day)
    except ValueError:
        return


def _matches(n: float, allowed: Iterable[float]) -> bool:
    for v in allowed:
        if abs(n - v) <= TOLERANCE_REL * abs(v) + TOLERANCE_ABS:
            return True
        if n.is_integer() and n in (round(v), math.floor(v)):  # "about 4 hours" for 4.3 h, never "5 hours"
            return True
    return False


def _mask_names(text: str, names: Iterable[str]) -> str:
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    return text


def report_texts(report: Report) -> list[str]:
    return [report.headline, report.summary, *report.why, *(r.text for r in report.recommendations),
            report.estimated_impact]


def check_text(text: str, facts: Facts, known_products: Iterable[str] = (), known_campaigns: Iterable[str] = ()
               ) -> list[str]:
    violations: list[str] = []
    fact_sizes = {sc.size for sc in facts.sizes} | {facts.size}
    fact_products = {facts.product_name, *(t.product_name for t in facts.budget_targets)}
    campaign_names = {c.name for c in facts.campaigns} | {t.campaign_name for t in facts.budget_targets}

    # Entities: sizes
    for m in SIZE_RE.finditer(text):
        size = _normalize_size(m)
        if size not in fact_sizes:
            violations.append(f"size '{m.group(0)}' is not in the facts")
    # Entities: catalog products the facts do not mention
    lowered = text.lower()
    for name in known_products:
        if name.lower() in lowered and not any(name.lower() in fp.lower() for fp in fact_products):
            violations.append(f"product '{name}' is not part of this alert's facts")

    masked = SIZE_RE.sub(" ", _mask_names(text, [*fact_products, *campaign_names, *known_products, *known_campaigns]))
    allowed = allowed_values(facts)
    every = set().union(*allowed.values())
    for m in TIME_RE.finditer(masked):
        for part in (int(m.group(1)), int(m.group(2))):
            if part not in allowed["plain"] and part != 0:
                violations.append(f"time '{m.group(0)}' does not appear in the facts")
    masked = TIME_RE.sub(" ", masked)
    for m in NUM_RE.finditer(masked):
        raw = m.group(0).strip()
        n = float(m.group("num").replace(",", ""))
        if m.group("mult"):
            n *= MULTIPLIERS[m.group("mult").lower()]
        unit = _unit_of(m)
        if not _matches(n, every if unit == "plain" else allowed[unit]):
            what = "number" if unit == "plain" else f"{unit} value"
            violations.append(f"{what} '{raw}' does not appear in the facts")
    return violations


def check_report(report: Report, facts: Facts, known_products: Iterable[str] = (),
                 known_campaigns: Iterable[str] = ()) -> list[str]:
    """All guardrail violations for a generated report (empty list = pass)."""
    products, campaigns = list(known_products), list(known_campaigns)
    violations: list[str] = []
    for text in report_texts(report):
        violations.extend(check_text(text, facts, products, campaigns))
    allowed_actions = {a.type for a in facts.actions} | {"monitor"}
    for r in report.recommendations:
        if r.action_type not in allowed_actions:
            violations.append(f"recommendation type '{r.action_type}' is not one of the engine's actions")
    return list(dict.fromkeys(violations))  # de-duplicate, keep order


def check_numbers(text: str, evidence: Any, known_names: Iterable[str] = ()) -> list[str]:
    """Number-only guardrail against arbitrary evidence (used by Ask mode)."""
    allowed = allowed_values_from(evidence)
    every = set().union(*allowed.values())
    masked = SIZE_RE.sub(" ", _mask_names(text, known_names))
    masked = TIME_RE.sub(" ", masked)
    violations = []
    for m in NUM_RE.finditer(masked):
        n = float(m.group("num").replace(",", ""))
        if m.group("mult"):
            n *= MULTIPLIERS[m.group("mult").lower()]
        unit = _unit_of(m)
        if not _matches(n, every if unit == "plain" else allowed[unit]):
            violations.append(f"{'number' if unit == 'plain' else unit + ' value'} '{m.group(0).strip()}' "
                              "is not in the evidence")
    return violations
