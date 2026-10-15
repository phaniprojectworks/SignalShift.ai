"""Ask mode: the owner asks questions about the shop's data.

Safety model - the LLM never writes SQL:
  1. ROUTE   question -> AskPlan {route, template, params}. Gemini (structured output) only *chooses* among the
             whitelisted templates below and extracts parameters; without Gemini a deterministic keyword router
             does the same. Parameters are validated (enums, bounded ints) and product names are resolved
             against the catalog in code.
  2. EXECUTE numbers -> a whitelisted, parameterized SQL template / Python function.
             why/how -> FTS5 retrieval over alerts + reports (sanitized tokens, bound parameter) + product filter.
  3. ANSWER  Gemini phrases the answer from the evidence only and the guardrail checks every number against the
             evidence; otherwise (or on failure) a deterministic sentence is used. Evidence is returned with
             every answer and everything is logged in ask_log.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import Database, sql_ts
from app.models import Alert, AskLog, Product, Variant
from app.schemas import Facts
from app.services.clock import now as clock_now
from app.services.engine import Engine
from app.services.fmt import hours as fmt_hours
from app.services.fmt import indian_group, inr
from app.services.guardrails import SIZE_RE, check_numbers
from app.services.reporter import LLMClient, LLMError

log = logging.getLogger("signalshift.ask")

Period = Literal["today", "yesterday", "this_week", "last_week", "last_7_days", "this_month", "last_30_days"]
TemplateName = Literal["units_sold", "revenue", "top_products", "stock_level", "stockout_forecast", "savings",
                       "ad_spend", "open_alerts", "none"]
PERIOD_LABEL = {"today": "today", "yesterday": "yesterday", "this_week": "this week", "last_week": "last week",
                "last_7_days": "in the last 7 days", "this_month": "this month", "last_30_days": "in the last 30 days"}

TEMPLATE_HELP = {
    "units_sold": "units/orders/revenue sold for a product and/or size in a period",
    "revenue": "total orders, units and revenue in a period",
    "top_products": "best-selling products in a period",
    "stock_level": "current stock (on hand, in transit, cover) for a product and/or size",
    "stockout_forecast": "which products/sizes are projected to run out within N days",
    "savings": "ad spend avoided and sales protected by approved actions in a period",
    "ad_spend": "ad spend, clicks and spend wasted on out-of-stock clicks in a period, optionally for a product",
    "open_alerts": "open alerts, optionally by severity",
}


class AskPlan(BaseModel):
    """What the router decided. Gemini fills this via structured output; code validates it."""
    route: Literal["numbers", "why", "unsupported"]
    template: TemplateName = "none"
    product: str | None = Field(default=None, description="product words exactly as the owner wrote them")
    size: str | None = Field(default=None, description="size such as 'UK 9', if mentioned")
    period: Period | None = None
    horizon_days: int | None = Field(default=None, ge=1, le=60)
    severity: Literal["critical", "warning", "opportunity"] | None = None
    limit: int | None = Field(default=None, ge=1, le=20)


class AskOut(BaseModel):
    id: int
    question: str
    route: str
    answer: str
    answer_source: str  # gemini / gemini_fallback_model / template
    plan_source: str  # gemini / keywords
    evidence: dict


# =============================================================================
# Entity + period resolution (code, not the LLM)
# =============================================================================

STOP = {"the", "a", "an", "of", "for", "did", "we", "how", "many", "much", "sell", "sold", "today", "week", "this",
        "what", "which", "will", "run", "out", "is", "are", "our", "my", "in", "on", "to", "do", "you", "why",
        "recommend", "recommended", "pausing", "pause", "campaign", "ads", "ad", "uk", "size", "pairs", "pair",
        "left", "stock", "have", "there", "last", "month", "yesterday", "spend", "spent", "save", "saved", "and"}


@dataclass
class ProductMatch:
    label: str
    product_ids: list[int]


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def resolve_product(s: Session, phrase: str | None) -> ProductMatch | None:
    """Best catalog match for free text ('white sneaker', 'Festive White', 'retro runner')."""
    if not phrase:
        return None
    words = {t for t in _tokens(phrase) if t not in STOP and not t.isdigit()}
    if not words:
        return None
    best: list[tuple[float, Product]] = []
    for p in s.scalars(select(Product)):
        model_t = {t for t in _tokens(p.model)}
        colour_t = {t for t in _tokens(p.name.replace(p.model, ""))}
        score = 2 * len(words & model_t) + len(words & colour_t)
        if score:
            best.append((score, p))
    if not best:
        return None
    top = max(sc for sc, _ in best)
    if top < 2:
        return None
    winners = [p for sc, p in best if sc == top]
    if len(winners) == 1:
        return ProductMatch(winners[0].name, [winners[0].id])
    models = sorted({p.model for p in winners})
    # tie (colour or edition not specified): include every match and say so, rather than guess one
    label = f"{models[0]} (all colourways)" if len(models) == 1 else " / ".join(models) + " (all colourways)"
    return ProductMatch(label, [p.id for p in winners])


def normalize_size(raw: str | None) -> str | None:
    if not raw:
        return None
    m = SIZE_RE.search(raw if raw.upper().startswith("UK") else f"UK {raw}")
    if not m:
        return None
    return f"UK {m.group(1)}-{m.group(2)}" if m.group(2) else f"UK {m.group(1)}"


def period_range(period: str, now: datetime) -> tuple[datetime, datetime]:
    """[start, end) - periods that run up to the present end at the end of today, so a record stamped
    exactly 'now' (an approval, an order) is included."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = day + timedelta(days=1)
    monday = day - timedelta(days=day.weekday())
    return {
        "today": (day, tomorrow),
        "yesterday": (day - timedelta(days=1), day),
        "this_week": (monday, tomorrow),
        "last_week": (monday - timedelta(days=7), monday),
        "last_7_days": (now - timedelta(days=7), tomorrow),
        "this_month": (day.replace(day=1), tomorrow),
        "last_30_days": (now - timedelta(days=30), tomorrow),
    }[period]


# =============================================================================
# Deterministic keyword router (offline fallback; also handles the demo questions)
# =============================================================================

def keyword_plan(q: str) -> AskPlan:
    t = q.lower()
    size = normalize_size(q)
    period: str | None = None
    for key, words in [("yesterday", ["yesterday"]), ("last_week", ["last week"]),
                       ("last_7_days", ["last 7 days", "past week", "last seven days"]),
                       ("this_week", ["this week", "week"]), ("last_30_days", ["last 30 days", "past month"]),
                       ("this_month", ["this month", "month"]), ("today", ["today", "so far"])]:
        if any(w in t for w in words):
            period = key
            break
    horizon = 1 if "today" in t else 2 if "tomorrow" in t else 30 if "month" in t else 7
    product = q
    if re.search(r"\b(why|how come|reason|explain)\b", t) or "what made you" in t:
        return AskPlan(route="why", product=product, size=size)
    if re.search(r"\b(sav(e|ed|ing|ings)|avoid(ed)?)\b", t):
        return AskPlan(route="numbers", template="savings", period=period or "this_week")
    if re.search(r"(run|sell|stock)(ning)? ?out|out of stock|stockout|sell-?out", t):
        return AskPlan(route="numbers", template="stockout_forecast", horizon_days=horizon)
    if re.search(r"\b(alert|alerts|attention|urgent)\b", t):
        sev = next((s for s in ("critical", "warning", "opportunity") if s in t), None)
        return AskPlan(route="numbers", template="open_alerts", severity=sev)
    if re.search(r"\b(spend|spent|wasted|cost per click|cpc|clicks)\b", t):
        return AskPlan(route="numbers", template="ad_spend", period=period or "today", product=product)
    if re.search(r"\b(left|in stock|stock level|inventory|on hand)\b", t):
        return AskPlan(route="numbers", template="stock_level", product=product, size=size)
    if re.search(r"\b(top|best[- ]?sell|bestsell|most popular)\b", t):
        return AskPlan(route="numbers", template="top_products", period=period or "today", limit=5)
    if re.search(r"how many|units|pairs|sold|sell", t):
        return AskPlan(route="numbers", template="units_sold", product=product, size=size, period=period or "today")
    if re.search(r"\b(revenue|sales|orders|turnover)\b", t):
        return AskPlan(route="numbers", template="revenue", period=period or "today")
    return AskPlan(route="unsupported")


# =============================================================================
# Whitelisted templates (parameterized; the only way Ask touches the database)
# =============================================================================

@dataclass
class TemplateResult:
    template: str
    params: dict
    values: dict  # headline numbers (also the guardrail's evidence)
    rows: list[dict] = field(default_factory=list)
    answer: str = ""  # deterministic answer


def _scope_sql(pm: ProductMatch | None, size: str | None, params: dict) -> str:
    clauses = []
    if pm:
        ids = ",".join(str(int(i)) for i in pm.product_ids)  # ints from the catalog, not user text
        clauses.append(f"p.id IN ({ids})")
    if size:
        clauses.append("v.size = :size")
        params["size"] = size
    return "".join(f" AND {c}" for c in clauses)


def t_units_sold(s: Session, plan: AskPlan, now: datetime, pm: ProductMatch | None, **_: Any) -> TemplateResult:
    period = plan.period or "today"
    a, b = period_range(period, now)
    size = normalize_size(plan.size)
    params: dict[str, Any] = {"a": sql_ts(a), "b": sql_ts(b)}
    where = _scope_sql(pm, size, params)
    units, revenue, orders = s.execute(text(
        "SELECT COALESCE(SUM(oi.qty),0), COALESCE(SUM(oi.qty*oi.price),0), COUNT(DISTINCT oi.order_id) "
        "FROM order_items oi JOIN variants v ON v.id = oi.variant_id JOIN products p ON p.id = v.product_id "
        f"WHERE oi.ts >= :a AND oi.ts < :b{where}"), params).one()
    rows = [{"product": r[0], "size": r[1], "units": int(r[2]), "revenue": float(r[3])} for r in s.execute(text(
        "SELECT p.name, v.size, SUM(oi.qty), SUM(oi.qty*oi.price) FROM order_items oi JOIN variants v ON v.id = oi.variant_id "
        f"JOIN products p ON p.id = v.product_id WHERE oi.ts >= :a AND oi.ts < :b{where} "
        "GROUP BY p.id, v.id ORDER BY 3 DESC LIMIT 10"), params)]
    what = " ".join(x for x in [pm.label if pm else "all products", size or ""] if x)
    unit = "pairs" if pm else "units"
    answer = (f"You sold {indian_group(int(units))} {unit if units != 1 else unit[:-1]} of {what} {PERIOD_LABEL[period]} "
              f"across {indian_group(int(orders))} order{'s' if orders != 1 else ''}, worth {inr(revenue)}.")
    return TemplateResult("units_sold", {"product": pm.label if pm else None, "size": size, "period": period},
                          {"units": int(units), "orders": int(orders), "revenue": round(float(revenue), 2)}, rows, answer)


def t_revenue(s: Session, plan: AskPlan, now: datetime, **_: Any) -> TemplateResult:
    period = plan.period or "today"
    a, b = period_range(period, now)
    orders, revenue = s.execute(text("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE ts >= :a AND ts < :b"),
                                {"a": sql_ts(a), "b": sql_ts(b)}).one()
    units = s.scalar(text("SELECT COALESCE(SUM(qty),0) FROM order_items WHERE ts >= :a AND ts < :b"),
                     {"a": sql_ts(a), "b": sql_ts(b)})
    rows = [{"day": d, "orders": int(n), "revenue": float(r)} for d, n, r in s.execute(text(
        "SELECT substr(ts,1,10), COUNT(*), SUM(total) FROM orders WHERE ts >= :a AND ts < :b GROUP BY 1 ORDER BY 1"),
        {"a": sql_ts(a), "b": sql_ts(b)})]
    answer = (f"Revenue {PERIOD_LABEL[period]} is {inr(revenue)} from {indian_group(int(orders))} orders "
              f"({indian_group(int(units or 0))} units).")
    return TemplateResult("revenue", {"period": period},
                          {"orders": int(orders), "units": int(units or 0), "revenue": round(float(revenue), 2)}, rows, answer)


def t_top_products(s: Session, plan: AskPlan, now: datetime, **_: Any) -> TemplateResult:
    period, n = plan.period or "today", plan.limit or 5
    a, b = period_range(period, now)
    rows = [{"rank": i + 1, "product": r[0], "units": int(r[1]), "revenue": float(r[2])} for i, r in enumerate(s.execute(text(
        "SELECT p.name, SUM(oi.qty), SUM(oi.qty*oi.price) FROM order_items oi JOIN variants v ON v.id = oi.variant_id "
        "JOIN products p ON p.id = v.product_id WHERE oi.ts >= :a AND oi.ts < :b GROUP BY p.id ORDER BY 3 DESC LIMIT :n"),
        {"a": sql_ts(a), "b": sql_ts(b), "n": n}))]
    if not rows:
        answer = f"No sales recorded {PERIOD_LABEL[period]} yet."
    else:
        answer = f"Top sellers {PERIOD_LABEL[period]}: " + "; ".join(
            f"{r['rank']}. {r['product']} ({indian_group(r['units'])} units, {inr(r['revenue'])})" for r in rows) + "."
    return TemplateResult("top_products", {"period": period, "limit": n}, {"products": len(rows)}, rows, answer)


def t_stock_level(s: Session, plan: AskPlan, now: datetime, pm: ProductMatch | None, engine: Engine,
                  **_: Any) -> TemplateResult:
    size = normalize_size(plan.size)
    if pm is None:
        return TemplateResult("stock_level", {"product": None, "size": size}, {}, [],
                              "Which product do you mean? For example: \"How many Festive White UK 9 are left?\"")
    snap = [r for r in engine.stock_snapshot() if r.product_id in pm.product_ids]
    rows = [{"product": r.name, "size": c.size, "available": c.available, "in_transit": c.in_transit,
             "hours_of_cover": c.hours_of_cover, "status": c.status}
            for r in snap for c in r.sizes if size is None or c.size == size]
    total = sum(r["available"] for r in rows)
    if size and rows:
        answer = " ".join(f"{r['product']} {size}: {r['available']} available, {r['in_transit']} in transit, "
                          f"about {fmt_hours(r['hours_of_cover'])} of cover." for r in rows)
    else:
        low = [f"{r['size']} ({r['available']})" for r in rows if r["status"] in ("out", "low")]
        answer = f"{pm.label}: {indian_group(total)} available across sizes." + (
            f" Running low: {', '.join(low)}." if low else "")
    return TemplateResult("stock_level", {"product": pm.label, "size": size}, {"available": total}, rows, answer)


def t_stockout_forecast(s: Session, plan: AskPlan, now: datetime, engine: Engine, **_: Any) -> TemplateResult:
    days = plan.horizon_days or 7
    rows = []
    for r in engine.stock_snapshot():
        for c in r.sizes:
            if c.hours_of_cover is not None and c.hours_of_cover < days * 24:
                rows.append({"product": r.name, "size": c.size, "available": c.available,
                             "hours_of_cover": c.hours_of_cover, "in_transit": c.in_transit,
                             "ads_live": r.campaign_live})
    rows.sort(key=lambda x: x["hours_of_cover"])
    products = sorted({x["product"] for x in rows})
    if not rows:
        answer = f"Nothing is projected to run out within {days} days at the current pace."
    else:
        listed = "; ".join(f"{x['product']} {x['size']} (~{fmt_hours(x['hours_of_cover'])}"
                           f"{', ads live' if x['ads_live'] else ''})" for x in rows[:6])
        answer = (f"{len(rows)} size{'s' if len(rows) != 1 else ''} across {len(products)} product"
                  f"{'s' if len(products) != 1 else ''} could run out within {days} days: {listed}"
                  + (f", and {len(rows) - 6} more." if len(rows) > 6 else "."))
    return TemplateResult("stockout_forecast", {"horizon_days": days},
                          {"sizes": len(rows), "products": len(products), "horizon_days": days}, rows, answer)


def t_savings(s: Session, plan: AskPlan, now: datetime, **_: Any) -> TemplateResult:
    period = plan.period or "this_week"
    a, b = period_range(period, now)
    avoided, protected, n = s.execute(text(
        "SELECT COALESCE(SUM(ad_spend_avoided),0), COALESCE(SUM(sales_protected),0), COUNT(*) FROM savings_ledger "
        "WHERE ts >= :a AND ts < :b"), {"a": sql_ts(a), "b": sql_ts(b)}).one()
    rows = [{"alert_id": int(r[0]), "ts": r[1], "ad_spend_avoided": float(r[2]), "sales_protected": float(r[3])}
            for r in s.execute(text("SELECT alert_id, ts, ad_spend_avoided, sales_protected FROM savings_ledger "
                                    "WHERE ts >= :a AND ts < :b ORDER BY ts DESC LIMIT 10"),
                               {"a": sql_ts(a), "b": sql_ts(b)})]
    if not n:
        answer = f"No savings recorded {PERIOD_LABEL[period]} yet. Approving an alert starts the ledger."
    else:
        answer = (f"{PERIOD_LABEL[period].capitalize()} Ad-Assist saved {inr(avoided)} in ad spend and protected "
                  f"{inr(protected)} of sales across {n} approved alert{'s' if n != 1 else ''}.")
    return TemplateResult("savings", {"period": period},
                          {"ad_spend_avoided": round(float(avoided), 2), "sales_protected": round(float(protected), 2),
                           "approved_alerts": int(n)}, rows, answer)


def t_ad_spend(s: Session, plan: AskPlan, now: datetime, pm: ProductMatch | None, **_: Any) -> TemplateResult:
    period = plan.period or "today"
    a, b = period_range(period, now)
    params: dict[str, Any] = {"a": sql_ts(a), "b": sql_ts(b)}
    where = ""
    if pm:
        where = f" AND c.product_id IN ({','.join(str(int(i)) for i in pm.product_ids)})"
    rows = [{"campaign": r[0], "spend": float(r[1]), "clicks": int(r[2]), "wasted_spend": float(r[3])}
            for r in s.execute(text(
                "SELECT c.name, COALESCE(SUM(m.spend),0), COALESCE(SUM(m.clicks),0), COALESCE(SUM(m.wasted_spend),0) "
                "FROM ad_metrics_hourly m JOIN campaigns c ON c.id = m.campaign_id "
                f"WHERE m.ts >= :a AND m.ts < :b{where} GROUP BY c.id ORDER BY 2 DESC"), params)]
    spend = sum(r["spend"] for r in rows)
    wasted = sum(r["wasted_spend"] for r in rows)
    clicks = sum(r["clicks"] for r in rows)
    scope = f" on {pm.label}" if pm else ""
    answer = (f"Ad spend{scope} {PERIOD_LABEL[period]} is {inr(spend)} for {indian_group(clicks)} clicks; "
              f"{inr(wasted)} of it went on clicks for sizes that were out of stock.")
    return TemplateResult("ad_spend", {"period": period, "product": pm.label if pm else None},
                          {"spend": round(spend, 2), "clicks": clicks, "wasted_spend": round(wasted, 2)}, rows, answer)


def t_open_alerts(s: Session, plan: AskPlan, now: datetime, **_: Any) -> TemplateResult:
    q = select(Alert).where(Alert.status == "open")
    if plan.severity:
        q = q.where(Alert.severity == plan.severity)
    alerts = list(s.scalars(q.order_by(Alert.ts.desc()).limit(50)))
    rank = {"critical": 0, "warning": 1, "opportunity": 2}
    alerts.sort(key=lambda a: rank.get(a.severity, 3))
    rows = [{"alert_id": a.id, "severity": a.severity, "headline": a.headline} for a in alerts[:10]]
    counts = {k: sum(1 for a in alerts if a.severity == k) for k in ("critical", "warning", "opportunity")}
    if not alerts:
        answer = "There are no open alerts" + (f" of severity {plan.severity}." if plan.severity else ".")
    else:
        answer = (f"{len(alerts)} open alert{'s' if len(alerts) != 1 else ''}: {counts['critical']} critical, "
                  f"{counts['warning']} warning, {counts['opportunity']} opportunity. Most urgent: {rows[0]['headline']}.")
    return TemplateResult("open_alerts", {"severity": plan.severity}, {"open": len(alerts), **counts}, rows, answer)


TEMPLATES: dict[str, Callable[..., TemplateResult]] = {
    "units_sold": t_units_sold, "revenue": t_revenue, "top_products": t_top_products, "stock_level": t_stock_level,
    "stockout_forecast": t_stockout_forecast, "savings": t_savings, "ad_spend": t_ad_spend,
    "open_alerts": t_open_alerts,
}


# =============================================================================
# Why / how: retrieval over alerts and reports
# =============================================================================

FTS_STOP = {"the", "and", "did", "you", "why", "how", "what", "was", "were", "our", "for", "this", "that", "with",
            "are", "can", "does", "explain", "reason", "come"}


def fts_query(question: str) -> str | None:
    """Sanitized FTS5 query: alphanumeric tokens only, each quoted, OR-joined (no operators from user text)."""
    words = [w for w in _tokens(question) if len(w) > 2 and w not in FTS_STOP and w.isalnum()]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words)) or None


def retrieve_alerts(s: Session, question: str, pm: ProductMatch | None, size: str | None, k: int = 3) -> list[dict]:
    ids: list[int] = []
    q = fts_query(question)
    if q:
        ids = [int(r[0]) for r in s.execute(text(
            "SELECT alert_id FROM alert_fts WHERE alert_fts MATCH :q ORDER BY rank LIMIT 50"), {"q": q})]
    base = select(Alert, Variant).join(Variant, Variant.id == Alert.variant_id)
    if pm:
        base = base.where(Variant.product_id.in_(pm.product_ids))
    if size:
        base = base.where(Variant.size == size)
    candidates = list(s.execute(base.order_by(Alert.ts.desc()).limit(200)))
    order = {aid: i for i, aid in enumerate(ids)}
    sev = {"critical": 0, "warning": 1, "opportunity": 2}
    # FTS rank first; among equals prefer acted-on alerts, higher severity and recency
    candidates.sort(key=lambda r: (order.get(r[0].id, 999), r[0].status not in ("approved", "dismissed"),
                                   sev.get(r[0].severity, 3), -r[0].ts.timestamp()))
    if ids and not pm:
        candidates = [r for r in candidates if r[0].id in order] or candidates
    out = []
    for a, v in candidates[:k]:
        facts = Facts.model_validate_json(a.facts_json)
        report = json.loads(a.report_text) if a.report_text else None
        out.append({"alert_id": a.id, "ts": a.ts.isoformat(timespec="minutes"), "severity": a.severity,
                    "status": a.status, "decided_by": a.decided_by, "headline": a.headline, "rule": facts.rule,
                    "actions": [x.summary for x in facts.actions],
                    "why": report["why"] if report else [], "facts": facts.model_dump(mode="json")})
    return out


def why_answer(alerts: list[dict]) -> str:
    if not alerts:
        return "I couldn't find an alert about that. Try naming the product or size, e.g. \"Why pause the Festive White ads?\""
    a = alerts[0]
    did = {"approved": "You approved it", "dismissed": "You dismissed it", "open": "It is waiting for your decision",
           "expired": "It was later superseded"}.get(a["status"], "")
    plan = " ".join(a["actions"][:2])
    reasons = " ".join(a["why"][:2])
    return (f"Alert #{a['alert_id']} ({a['severity']}, {a['ts'][:10]} {a['ts'][11:16]}): {a['headline']}. "
            f"The rule: {a['rule']}. {reasons} What I recommended: {plan} {did}.").replace("..", ".")


# =============================================================================
# Service
# =============================================================================

PLANNER_PROMPT = """You route a shop owner's question to a data template. You do NOT answer it.
Pick route "numbers" with one template, route "why" for why/how/explain questions about past alerts or
recommendations, or "unsupported". Templates:
{templates}
Copy product words exactly as written (do not invent products). Periods: today, yesterday, this_week, last_week,
last_7_days, this_month, last_30_days."""

ANSWER_PROMPT = """You answer a busy shop owner's question using ONLY the EVIDENCE JSON. Rules: use only numbers that
appear in EVIDENCE (copy them; never compute new ones); one to three short plain sentences; mention the period;
no markdown. If the evidence does not answer the question, say so."""


class AskAnswer(BaseModel):
    answer: str


class AskService:
    def __init__(self, db: Database, engine: Engine, llm: LLMClient | None, models: list[tuple[str, str]],
                 clock: Callable[[], datetime] = clock_now) -> None:
        self.db, self.engine, self.llm, self.models, self.clock = db, engine, llm, models, clock

    # -- routing ------------------------------------------------------------------
    def plan(self, question: str) -> tuple[AskPlan, str]:
        if self.llm is not None:
            prompt = PLANNER_PROMPT.format(templates="\n".join(f"- {k}: {v}" for k, v in TEMPLATE_HELP.items()))
            for model, _src in self.models:
                try:
                    raw = self.llm.generate(model, prompt, f"QUESTION: {question}", AskPlan.model_json_schema())
                    p = AskPlan.model_validate_json(raw)
                    if p.route == "numbers" and p.template == "none":
                        raise ValueError("numbers route without a template")
                    return p, "gemini"
                except (LLMError, ValidationError, ValueError) as e:
                    log.info("planner fallback", extra={"model": model, "error": str(e)[:200]})
        return keyword_plan(question), "keywords"

    def _phrase(self, question: str, evidence: dict, fallback: str, names: list[str]) -> tuple[str, str]:
        if self.llm is None:
            return fallback, "template"
        for model, src in self.models:
            try:
                raw = self.llm.generate(model, ANSWER_PROMPT, f"QUESTION: {question}\n\nEVIDENCE:\n"
                                        f"{json.dumps(evidence, default=str, ensure_ascii=False)}",
                                        AskAnswer.model_json_schema())
                ans = AskAnswer.model_validate_json(raw).answer
            except (LLMError, ValidationError) as e:
                log.info("answer fallback", extra={"model": model, "error": str(e)[:200]})
                continue
            violations = check_numbers(ans, evidence, names)
            if not violations:
                return ans, src
            log.info("answer rejected by guardrail", extra={"violations": violations})
            break
        return fallback, "template"

    # -- main entry ---------------------------------------------------------------
    def ask(self, question: str) -> AskOut:
        question = question.strip()[:500]
        now = self.clock()
        plan, plan_source = self.plan(question)
        with self.db.session() as s:
            names = [n for p in s.scalars(select(Product)) for n in (p.name, p.model)]
            pm = resolve_product(s, plan.product or (question if plan.route == "why" else None))
            size = normalize_size(plan.size) or (normalize_size(question) if plan.route == "why" else None)
            if plan.route == "numbers" and plan.template in TEMPLATES:
                res = TEMPLATES[plan.template](s, plan, now, pm=pm, engine=self.engine)
                evidence = {"type": "sql_template", "template": res.template, "params": res.params,
                            "values": res.values, "rows": res.rows[:20]}
                answer, source = self._phrase(question, evidence, res.answer, names)
                route = f"numbers:{res.template}"
            elif plan.route == "why":
                alerts = retrieve_alerts(s, question, pm, size)
                evidence = {"type": "alert_retrieval", "alert_ids": [a["alert_id"] for a in alerts],
                            "product": pm.label if pm else None, "size": size, "alerts": alerts}
                answer, source = self._phrase(question, evidence, why_answer(alerts), names)
                route = "why"
            else:
                evidence = {"type": "none", "can_answer": list(TEMPLATE_HELP.values())}
                answer, source = ("I can answer questions about sales, stock, ad spend, savings and alerts, and explain "
                                  "my recommendations. Try: \"How much ad spend did we save this week?\"", "template")
                route = "unsupported"
            evidence["plan"] = plan.model_dump()
            evidence["plan_source"] = plan_source
        with self.db.write() as s:
            row = AskLog(ts=now, question=question, route=route, answer=answer,
                         evidence_json=json.dumps(evidence, default=str, ensure_ascii=False))
            s.add(row)
            s.flush()
            log_id = row.id
        log.info("ask", extra={"ask_id": log_id, "route": route, "answer_source": source, "plan_source": plan_source})
        return AskOut(id=log_id, question=question, route=route, answer=answer, answer_source=source,
                      plan_source=plan_source, evidence=evidence)
