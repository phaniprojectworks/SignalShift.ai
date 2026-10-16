"""Report delivery: the in-app inbox always; Telegram as well when TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set.

In-app inbox: every alert row with its report IS the inbox (SSE pushes `alert`/`report` events to the dashboard).
Telegram: alerts at or above TELEGRAM_MIN_SEVERITY are sent with inline Approve / Dismiss buttons; the daily
briefing is sent as a summary. Button presses arrive as callback queries, handled by long polling (default -
works on a laptop with no public URL) or by the /telegram/webhook route (TELEGRAM_MODE=webhook). Only callbacks
from the configured chat are honoured.
"""
from __future__ import annotations

import html
import json
import logging
import threading
from typing import Any

import httpx
from sqlalchemy import select, update

from app.config import Settings
from app.db import Database
from app.models import Alert
from app.schemas import DecisionOut, Report
from app.services.actions import ActionService, DecisionError
from app.services.engine import SEVERITY_RANK
from app.services.events import Event
from app.services.fmt import inr
from app.services.reporter import ReportResult

log = logging.getLogger("signalshift.delivery")

SEV_TAG = {"critical": "🔴 CRITICAL", "warning": "🟠 WARNING", "opportunity": "🔵 OPPORTUNITY"}


class TelegramClient:
    """Minimal Telegram Bot API client over httpx."""

    def __init__(self, token: str, base: str = "https://api.telegram.org", http: httpx.Client | None = None) -> None:
        self._url = f"{base.rstrip('/')}/bot{token}"
        self._http = http or httpx.Client(timeout=httpx.Timeout(10.0, read=35.0))

    def _call(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
        r = self._http.post(f"{self._url}/{method}", json=payload, timeout=timeout)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description', r.status_code)}")
        return body["result"]

    def send_message(self, chat_id: str, text: str, buttons: list[list[dict]] | None = None) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return int(self._call("sendMessage", payload)["message_id"])

    def edit_message(self, chat_id: str, message_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
        self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text,
                                       "parse_mode": "HTML", "reply_markup": {"inline_keyboard": buttons or []}})

    def answer_callback(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            payload["offset"] = offset
        return list(self._call("getUpdates", payload, timeout=timeout + 10))


# --- message formatting -----------------------------------------------------------------

def format_alert(severity: str, report: Report, alert_id: int, source: str) -> str:
    e = html.escape
    lines = [f"<b>{SEV_TAG.get(severity, severity.upper())}</b>", f"<b>{e(report.headline)}</b>", "", e(report.summary)]
    lines += ["", "<b>Why</b>"] + [f"• {e(w)}" for w in report.why]
    lines += ["", "<b>What I recommend</b>"] + [f"{i}. {e(r.text)}" for i, r in enumerate(report.recommendations, 1)]
    lines += ["", f"<i>{e(report.estimated_impact)}</i>", "",
              f"<code>alert #{alert_id} · text: {e(source)} · numbers: SignalShift.ai engine · sample data</code>"]
    return "\n".join(lines)


def format_decision(base: str, d: DecisionOut) -> str:
    if d.status == "dismissed":
        tail = f"✖ <b>Dismissed</b> via {html.escape(d.actor)} at {d.decided_at:%H:%M}. I'll stay quiet on this for a while."
    else:
        done = ", ".join(sorted({a.type.replace('_', ' ') for a in d.actions})) or "nothing to execute"
        tail = f"✅ <b>Approved</b> via {html.escape(d.actor)} at {d.decided_at:%H:%M}. Done (mock APIs): {done}."
        if d.ledger:
            tail += f" Ad spend avoided {inr(d.ledger.ad_spend_avoided)}."
    return f"{base}\n\n{tail}"


def format_briefing(summary: dict) -> str:
    counts = summary.get("counts", {})
    lines = ["<b>☀️ Good morning — SignalShift.ai daily briefing</b>",
             f"Open alerts: {counts.get('critical', 0)} critical, {counts.get('warning', 0)} warning, "
             f"{counts.get('opportunity', 0)} opportunities."]
    for item in summary.get("top", []):
        lines.append(f"• [{html.escape(item['severity'])}] {html.escape(item['headline'] or '')} (#{item['alert_id']})")
    lines.append("Open the dashboard to review. (sample data)")
    return "\n".join(lines)


# --- delivery service -------------------------------------------------------------------

class Delivery:
    def __init__(self, db: Database, settings: Settings, actions: ActionService,
                 client: TelegramClient | None = None) -> None:
        self.db, self.settings, self.actions = db, settings, actions
        self.enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        self.chat_id = str(settings.telegram_chat_id or "")
        if client is None and self.enabled:
            client = TelegramClient(settings.telegram_bot_token or "", settings.telegram_api_base)
        self.client = client
        self._texts: dict[int, str] = {}  # alert_id -> sent text (for editing after a decision)
        self._offset: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # outgoing ------------------------------------------------------------------------
    def on_report(self, alert_id: int, result: ReportResult) -> None:
        """Called once per alert with its final report. The inbox already has it; Telegram is optional."""
        with self.db.session() as s:
            alert = s.get(Alert, alert_id)
            if alert is None or alert.status != "open":
                return
            severity, existing_message = alert.severity, alert.telegram_message_id
        log.info("delivered", extra={"alert_id": alert_id, "channel": "inbox", "report_source": result.source})
        if not self.enabled or self.client is None:
            return
        if SEVERITY_RANK.get(severity, 0) < SEVERITY_RANK.get(self.settings.telegram_min_severity, 2):
            return
        text = format_alert(severity, result.report, alert_id, result.source)
        buttons = [[{"text": "✅ Approve", "callback_data": f"approve:{alert_id}"},
                    {"text": "✖ Dismiss", "callback_data": f"dismiss:{alert_id}"}]]
        try:
            if existing_message:  # the alert was refreshed in place: update the message, don't send another
                self.client.edit_message(self.chat_id, existing_message, text, buttons)
                self._texts[alert_id] = text
                log.info("delivered", extra={"alert_id": alert_id, "channel": "telegram", "edited": existing_message})
                return
            message_id = self.client.send_message(self.chat_id, text, buttons)
        except Exception:
            log.exception("telegram send failed (inbox still has the alert)", extra={"alert_id": alert_id})
            return
        self._texts[alert_id] = text
        with self.db.write() as s:
            s.execute(update(Alert).where(Alert.id == alert_id).values(telegram_message_id=message_id))
        log.info("delivered", extra={"alert_id": alert_id, "channel": "telegram", "message_id": message_id})

    def on_briefing(self, ev: Event) -> None:
        if self.enabled and self.client is not None:
            try:
                self.client.send_message(self.chat_id, format_briefing(ev.data))
            except Exception:
                log.exception("telegram briefing failed")

    def on_decided(self, d: DecisionOut) -> None:
        """Reflect decisions taken anywhere (dashboard or Telegram) on the Telegram message."""
        if not (self.enabled and self.client):
            return
        with self.db.session() as s:
            message_id = s.scalar(select(Alert.telegram_message_id).where(Alert.id == d.alert_id))
        if message_id:
            base = self._texts.get(d.alert_id, f"Alert #{d.alert_id}")
            try:
                self.client.edit_message(self.chat_id, message_id, format_decision(base, d))
            except Exception:
                log.exception("telegram edit failed", extra={"alert_id": d.alert_id})

    # incoming ------------------------------------------------------------------------
    def handle_update(self, update: dict) -> str | None:
        """Process one Telegram update. Returns the outcome (for logs/tests)."""
        cq = update.get("callback_query")
        if not cq:
            msg = update.get("message") or {}
            if str(msg.get("chat", {}).get("id")) == self.chat_id and self.client and msg.get("text", "").startswith("/start"):
                self.client.send_message(self.chat_id, "SignalShift.ai is connected. I'll send stock and ad alerts here.")
                return "start"
            return None
        chat = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
        if chat != self.chat_id:
            log.warning("ignored callback from unknown chat", extra={"chat": chat})
            return "ignored"
        verb, _, raw_id = (cq.get("data") or "").partition(":")
        if verb not in ("approve", "dismiss") or not raw_id.isdigit():
            return "ignored"
        try:
            d = self.actions.approve(int(raw_id), "telegram") if verb == "approve" \
                else self.actions.dismiss(int(raw_id), "telegram")
            reply = "Approved — actions executed (mock APIs)." if verb == "approve" else "Dismissed."
            outcome = d.status
        except DecisionError as e:
            reply, outcome = str(e), "error"
        if self.client:
            self.client.answer_callback(cq["id"], reply)
        return outcome

    def poll_once(self, timeout: int = 25) -> int:
        assert self.client is not None
        updates = self.client.get_updates(self._offset, timeout)
        for u in updates:
            self._offset = int(u["update_id"]) + 1
            try:
                self.handle_update(u)
            except Exception:
                log.exception("telegram update failed", extra={"update": json.dumps(u)[:500]})
        return len(updates)

    def start_polling(self) -> None:
        if not (self.enabled and self.settings.telegram_mode == "polling") or self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception:
                    log.exception("telegram polling error; retrying")
                    self._stop.wait(5)

        self._thread = threading.Thread(target=loop, name="telegram-poller", daemon=True)
        self._thread.start()
        log.info("telegram long polling started")

    def stop(self) -> None:
        self._stop.set()
