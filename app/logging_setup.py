"""JSON-lines logging so every alert decision (with the facts used) is machine-readable.

The lines are also shaped for Google Cloud Logging when the app runs on Cloud Run: `severity` and `message`
are the fields Cloud Logging reads, `stack_trace` lets Error Reporting group crashes, and a request's
`X-Cloud-Trace-Context` is attached as `logging.googleapis.com/trace` (when GOOGLE_CLOUD_PROJECT is set) so a
request's log lines are grouped in the Logs Explorer. The original `ts` / `level` / `msg` fields are kept.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar

_STD = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime", "taskName"}
_trace: ContextVar[str | None] = ContextVar("cloud_trace", default=None)
TRACE_KEY = "logging.googleapis.com/trace"


def set_request_trace(header: str | None, project: str | None):  # type: ignore[no-untyped-def]
    """Bind the Cloud Trace id from `X-Cloud-Trace-Context` ("TRACE_ID/SPAN_ID;o=1") to the current request.
    Returns a token for `reset_request_trace`, or None when there is nothing to bind."""
    trace_id = (header or "").split("/", 1)[0].strip()
    if not trace_id or not project:
        return None
    return _trace.set(f"projects/{project}/traces/{trace_id}")


def reset_request_trace(token) -> None:  # type: ignore[no-untyped-def]
    if token is not None:
        _trace.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                   "logger": record.name, "msg": msg,
                   "severity": record.levelname, "message": msg}  # Cloud Logging field names
        for k, v in vars(record).items():
            if k in _STD:
                continue
            # an extra named like a Cloud Logging field (e.g. an alert's "severity") must not overwrite the log
            # level, so it is kept as "alert_severity" / "extra_message"
            payload["alert_severity" if k == "severity" else "extra_message" if k == "message" else k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
            payload["stack_trace"] = payload["exc"]  # picked up by Cloud Error Reporting
        trace = _trace.get()
        if trace:
            payload[TRACE_KEY] = trace
        return json.dumps(payload, default=str)  # ASCII-safe on any console


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger("signalshift")
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
