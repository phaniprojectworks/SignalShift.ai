"""Turns engine Facts into a short owner-facing Report.

Design rule: code does the math, Gemini does the language. Gemini receives the Facts JSON (plus pre-formatted
display strings) and returns a structured Report; guardrails verify every number, size, product and action.

Path per report (logged, and stored in alerts.report_source):
  gemini                 primary model output passed the guardrails (possibly after one corrective retry)
  gemini_fallback_model  the primary model errored / returned invalid JSON; the fallback model's output passed
  template               no API key / no model configured, both models errored, or the guardrails still failed
                         after the retry -> deterministic report built straight from the facts
Gemini never computes numbers or decides severity.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy import select, text, update

from app.config import Settings
from app.db import Database
from app.models import Alert, Campaign, Product
from app.schemas import Facts, Report, ReportRecommendation
from app.services.events import EventBus
from app.services.fmt import hours, inr, pct, size_list
from app.services.guardrails import check_report

log = logging.getLogger("signalshift.reporter")

SYSTEM_PROMPT = """You write alerts for the owner of a small Indian online shoe shop. The owner is busy and reads \
these on a phone, like messages from a personal assistant.

You receive FACTS (JSON) computed by the shop's inventory and ads engine, plus DISPLAY strings that are already \
formatted. Turn them into a short report.

Hard rules:
1. Use ONLY numbers that appear in FACTS or DISPLAY. Never invent, estimate, add up, subtract, or convert numbers. \
If a number is not given, describe it in words instead (for example "most of the clicks", "soon").
2. Copy money, hours, days and percentages exactly as written in DISPLAY (for example "₹2,600/day", "1.1 h", "68%").
3. Do not decide or change the severity. The severity is given in FACTS.
4. Only mention products, sizes and campaigns that appear in FACTS. Write sizes exactly as given (for example "UK 9").
5. recommendations must follow the order and the action types of FACTS.actions (one item per action). Do not add \
actions the engine did not recommend.
6. Plain, warm, direct English. Short sentences. No jargon such as SKU, ROAS, velocity or EWMA; say "sales", \
"return on ad spend" or "cover" instead. No markdown, no emojis.
7. "why" has 2 or 3 short reasons taken from FACTS (for example demand spike, share of clicks on low sizes, \
restock timing)."""


# --- LLM client ------------------------------------------------------------------

class LLMError(Exception):
    """The model call failed (network/API error, empty or non-JSON output)."""


class LLMClient(Protocol):
    def generate(self, model: str, system: str, prompt: str, schema: dict) -> str: ...


class GeminiClient:
    """google-genai `generate_content` with JSON-schema structured output (response_json_schema)."""

    def __init__(self, api_key: str | None, timeout_s: float = 30.0, temperature: float = 0.2,
                 vertex_project: str | None = None, vertex_location: str = "global") -> None:
        """With `vertex_project`, calls Gemini on Vertex AI using Application Default Credentials (on Cloud Run:
        the service account, which needs roles/aiplatform.user); otherwise uses the Gemini API key."""
        from google import genai
        from google.genai import types

        self._types = types
        http = types.HttpOptions(timeout=int(timeout_s * 1000))
        if vertex_project:
            self._client = genai.Client(vertexai=True, project=vertex_project, location=vertex_location, http_options=http)
        else:
            self._client = genai.Client(api_key=api_key, http_options=http)
        self._temperature = temperature

    @classmethod
    def from_settings(cls, settings: Settings) -> GeminiClient:
        vertex = settings.gemini_use_vertexai and settings.google_cloud_project
        return cls(None if vertex else settings.gemini_api_key, settings.gemini_timeout_seconds,
                   settings.gemini_temperature, vertex_project=settings.google_cloud_project if vertex else None,
                   vertex_location=settings.google_cloud_location)

    def generate(self, model: str, system: str, prompt: str, schema: dict) -> str:
        try:
            resp = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config=self._types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    temperature=self._temperature,
                    automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True),  # no tools
                ),
            )
        except Exception as e:  # google.genai.errors.APIError, httpx errors, timeouts
            raise LLMError(f"{type(e).__name__}: {e}") from e
        if not resp.text:
            raise LLMError("empty response")
        return resp.text


# --- display strings & template ---------------------------------------------------------

def display_strings(f: Facts) -> dict[str, str]:
    """Pre-formatted values Gemini can quote verbatim (all derived from facts, no new numbers)."""
    d: dict[str, str] = {"available": f"{f.available} {f.unit_label}{'' if f.available == 1 else 's'}",
                         "product_total_available": f"{f.product_total_available} {f.unit_label}s"}
    if f.hours_of_cover is not None:
        d["hours_of_cover"] = hours(f.hours_of_cover)
    if f.hours_of_cover_low is not None and f.hours_of_cover_high is not None:
        d["cover_range"] = f"{hours(f.hours_of_cover_low)} to {hours(f.hours_of_cover_high)}"
    if f.at_risk_click_share is not None:
        d["at_risk_click_share"] = pct(f.at_risk_click_share)
    if f.forecast:
        d["demand_per_day"] = f"{f.forecast.demand_per_day:g} a day"
        if f.forecast.surge:
            d["recent_sales_rate"] = f"{f.forecast.recent_rate_per_hr:g} an hour"
    for c in f.campaigns:
        d[f"campaign_{c.campaign_id}_budget"] = f"{inr(c.daily_budget)}/day"
        d[f"campaign_{c.campaign_id}_at_risk_spend"] = f"{inr(c.at_risk_spend_per_day)}/day"
    for key in ("wasted_spend_per_hr", "wasted_spend_estimate", "lost_sales_estimate", "freed_budget",
                "budget_moved", "budget_held_back"):
        v = getattr(f, key)
        if v:
            d[key] = inr(v) + ("/hour" if key == "wasted_spend_per_hr" else "/day" if "budget" in key else "")
    if f.wasted_spend_horizon_hours:
        d["horizon"] = hours(f.wasted_spend_horizon_hours)
    for t in f.budget_targets:
        d[f"target_{t.product_id}"] = (f"{inr(t.amount)}/day to {t.product_name} "
                                       f"({math.floor(t.cover_days_on_hand)} days of cover)")
    if f.reorder_total:
        d["reorder"] = f"{f.reorder_total} {f.unit_label}s (" + ", ".join(f"{r.size}: {r.qty}" for r in f.reorder) + ")"
    if f.restock_eta:
        d["restock_eta"] = f"{f.restock_eta:%d %b}"
    if f.event:
        d["event"] = f"{f.event.name} in {f.event.days_until} days"
    return d


def template_report(f: Facts) -> Report:
    """Deterministic report built straight from the facts (also the guardrail-safe fallback)."""
    from app.services.engine import Engine  # local import: engine imports nothing from here

    unit = f.unit_label + ("" if f.available == 1 else "s")
    recs = [ReportRecommendation(action_type=a.type, text=a.summary) for a in f.actions] or [
        ReportRecommendation(action_type="monitor", text="Keep an eye on sales; no action is needed yet.")]
    why: list[str] = []
    restock = (f"The next delivery is due {f.restock_eta:%d %b}." if f.restock_eta and f.in_transit
               else f"New stock takes {f.lead_time_days} days to arrive.")

    if f.severity == "opportunity" and f.event:
        summary = (f"{f.event.name} is {f.event.days_until} days away. Expected demand for {f.product_name} up to "
                   f"then is about {round(f.projected_demand or 0)} {f.unit_label}s, but only "
                   f"{round(f.projected_supply or 0)} are in stock or on the way.")
        why = [f"Festival demand for this range peaks at {f.event.peak_uplift:g}x normal around {f.event.name}.",
               f"The gap is about {round(f.projected_shortfall or 0)} {f.unit_label}s across sizes.",
               restock]
        impact = (f"Ordering now covers about {round(f.projected_shortfall or 0)} {f.unit_label}s of festival "
                  f"demand that would otherwise go unserved.")
    else:
        live = len(f.campaigns)
        ads = f" while {live} ad campaign{'s keep' if live != 1 else ' keeps'} spending" if live else ""
        if f.available <= 0:
            summary = f"{f.size} of {f.product_name} is sold out{ads.replace(' while', ', yet')}."
        else:
            summary = (f"{f.size} of {f.product_name} has {f.available} {unit} left and will sell out in about "
                       f"{hours(f.hours_of_cover)}{ads}.")
        if f.at_risk_click_share:
            verb = "are" if len(f.at_risk_sizes) > 1 else "is"
            summary += (f" {pct(f.at_risk_click_share)} of the ad clicks are for {size_list(f.at_risk_sizes)}, "
                        f"which {verb} low or sold out.")
        if f.forecast and f.forecast.surge:
            why.append(f"Sales jumped to {f.forecast.recent_rate_per_hr:g} an hour in the last "
                       f"{f.thresholds.get('surge_window_hours', 2):g} hours, well above the usual pace.")
        else:
            why.append(f"{f.size} is selling about {f.forecast.demand_per_day:g} a day." if f.forecast
                       else "Sales are steady.")
        if f.at_risk_click_share and f.campaigns:
            c = max(f.campaigns, key=lambda x: x.daily_budget)
            why.append(f"{pct(c.at_risk_click_share)} of the clicks on '{c.name}' want sizes that are low or out, "
                       f"about {inr(c.at_risk_spend_per_day)}/day of its budget.")
        why.append(restock)
        parts = []
        if f.wasted_spend_estimate:
            parts.append(f"avoids about {inr(f.wasted_spend_estimate)} of ad spend on clicks that cannot convert "
                         f"over the next {hours(f.wasted_spend_horizon_hours)}")
        if f.lost_sales_estimate:
            parts.append(f"protects about {inr(f.lost_sales_estimate)} of sales with a timely reorder")
        impact = ("Acting now " + " and ".join(parts) + ".") if parts else "Acting now keeps this size available."
    return Report(headline=Engine.headline(f), summary=summary, why=why[:3], recommendations=recs[:4],
                  estimated_impact=impact)


# --- reporter ------------------------------------------------------------------

@dataclass
class ReportResult:
    report: Report
    source: str  # gemini / gemini_fallback_model / template
    attempts: list[dict] = field(default_factory=list)


def build_prompt(f: Facts, violations: list[str] | None = None, previous: Report | None = None) -> str:
    facts_json = f.model_dump_json(exclude_none=True)
    out = (f"FACTS:\n{facts_json}\n\nDISPLAY:\n{json.dumps(display_strings(f), ensure_ascii=False)}\n\n"
           f"Write the report for this {f.severity} alert.")
    if violations:
        out += ("\n\nYour previous draft broke the rules. Problems found:\n- " + "\n- ".join(violations)
                + "\nRewrite it using only numbers, sizes and products from FACTS/DISPLAY.")
        if previous is not None:
            out += f"\nPrevious draft:\n{previous.model_dump_json()}"
    return out


class Reporter:
    def __init__(self, settings: Settings, client: LLMClient | None = None,
                 known_products: list[str] | None = None, known_campaigns: list[str] | None = None) -> None:
        self.settings = settings
        self.known_products = known_products or []
        self.known_campaigns = known_campaigns or []
        if client is None and settings.gemini_configured:
            client = GeminiClient.from_settings(settings)
        self.client = client
        self.schema = Report.model_json_schema()

    @property
    def llm_enabled(self) -> bool:
        return self.client is not None and bool(self.settings.gemini_model)

    def _models(self) -> list[tuple[str, str]]:
        models = [(self.settings.gemini_model, "gemini")]
        if self.settings.gemini_fallback_model:
            models.append((self.settings.gemini_fallback_model, "gemini_fallback_model"))
        return [(m, src) for m, src in models if m]

    def _call(self, model: str, prompt: str) -> Report:
        assert self.client is not None
        raw = self.client.generate(model, SYSTEM_PROMPT, prompt, self.schema)
        try:
            return Report.model_validate_json(raw)
        except ValidationError as e:
            raise LLMError(f"output did not match the Report schema: {e.error_count()} errors") from e

    def build(self, facts: Facts) -> ReportResult:
        attempts: list[dict] = []
        if not self.llm_enabled:
            return ReportResult(template_report(facts), "template", [{"path": "template", "reason": "no API key/model"}])
        for model, source in self._models():
            try:
                draft = self._call(model, build_prompt(facts))
            except LLMError as e:
                attempts.append({"model": model, "ok": False, "error": str(e)})
                continue  # model failure -> try the fallback model
            violations = check_report(draft, facts, self.known_products, self.known_campaigns)
            attempts.append({"model": model, "ok": not violations, "violations": violations})
            if not violations:
                return ReportResult(draft, source, attempts)
            try:  # one corrective retry, listing the violations
                retry = self._call(model, build_prompt(facts, violations, draft))
            except LLMError as e:
                attempts.append({"model": model, "retry": True, "ok": False, "error": str(e)})
                break
            violations = check_report(retry, facts, self.known_products, self.known_campaigns)
            attempts.append({"model": model, "retry": True, "ok": not violations, "violations": violations})
            if not violations:
                return ReportResult(retry, source, attempts)
            break  # guardrails failed twice -> deterministic template
        return ReportResult(template_report(facts), "template", attempts)


# --- persistence / async upgrade --------------------------------------------------------

class ReportService:
    """Writes the template report immediately (the inbox is never empty), then upgrades it with Gemini
    in a background worker when an API key is configured."""

    def __init__(self, db: Database, bus: EventBus | None, reporter: Reporter, background: bool = True) -> None:
        self.db, self.bus, self.reporter = db, bus, reporter
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="reporter") if background else None
        self.on_saved: list[Callable[[int, ReportResult], None]] = []

    @classmethod
    def for_database(cls, db: Database, bus: EventBus | None, settings: Settings, client: LLMClient | None = None,
                     background: bool = True) -> ReportService:
        with db.session() as s:
            products = sorted({n for p in s.scalars(select(Product)) for n in (p.name, p.model)})
            campaigns = list(s.scalars(select(Campaign.name)))
        return cls(db, bus, Reporter(settings, client, products, campaigns), background)

    def on_alert(self, alert_id: int, facts: Facts) -> Future[ReportResult] | ReportResult | None:
        template = ReportResult(template_report(facts), "template", [{"path": "template", "reason": "initial"}])
        self._save(alert_id, template, publish=not self.reporter.llm_enabled)
        if not self.reporter.llm_enabled:
            return template
        if self._pool is not None:
            fut = self._pool.submit(self.generate, alert_id, facts)
            fut.add_done_callback(lambda f: f.exception() and log.error(
                "report generation failed", exc_info=f.exception(), extra={"alert_id": alert_id}))
            return fut
        return self.generate(alert_id, facts)

    def generate(self, alert_id: int, facts: Facts) -> ReportResult:
        result = self.reporter.build(facts)
        self._save(alert_id, result, publish=True)
        return result

    def _save(self, alert_id: int, result: ReportResult, publish: bool) -> None:
        body = result.report.model_dump_json()
        with self.db.write() as s:
            s.execute(update(Alert).where(Alert.id == alert_id).values(report_text=body, report_source=result.source))
            s.execute(text("UPDATE alert_fts SET report_text = :r WHERE alert_id = :i"),
                      {"r": " ".join([result.report.headline, result.report.summary, *result.report.why,
                                      *(r.text for r in result.report.recommendations),
                                      result.report.estimated_impact]), "i": alert_id})
        log.info("report saved", extra={"alert_id": alert_id, "report_source": result.source,
                                        "attempts": result.attempts})
        if publish:
            if self.bus is not None:
                self.bus.publish("report", {"alert_id": alert_id, "source": result.source,
                                            "headline": result.report.headline})
            for cb in self.on_saved:
                try:
                    cb(alert_id, result)
                except Exception:
                    log.exception("report callback failed", extra={"alert_id": alert_id})

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
