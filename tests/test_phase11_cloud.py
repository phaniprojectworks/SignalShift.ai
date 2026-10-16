"""Phase 11: Google Cloud integrations - health probe, Cloud Logging format, trace links, Vertex AI Gemini."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.logging_setup import TRACE_KEY, JsonFormatter, reset_request_trace, set_request_trace
from app.main import create_app
from app.services import reporter as reporter_mod
from app.services.reporter import Reporter

QUIET = {"scheduler_enabled": False, "startup_sweep": False, "ad_sim_enabled": False}


def _line(msg: str = "hello", exc: bool = False, **extra: object) -> dict:
    rec = logging.LogRecord("signalshift.test", logging.ERROR if exc else logging.INFO, __file__, 1, msg, None, None)
    if exc:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            rec.exc_info = sys.exc_info()
    for k, v in extra.items():
        setattr(rec, k, v)
    return json.loads(JsonFormatter().format(rec))


def test_health_endpoint(work_db: Path) -> None:
    app = create_app(str(work_db), settings=Settings(_env_file=None, **QUIET))
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["database"] == "ok"


def test_log_lines_carry_cloud_logging_fields_and_keep_the_old_ones() -> None:
    d = _line("alert decision", alert_id=7)
    assert d["severity"] == "INFO" and d["message"] == "alert decision"  # what Cloud Logging reads
    assert d["level"] == "INFO" and d["msg"] == "alert decision" and d["alert_id"] == 7  # unchanged fields
    assert TRACE_KEY not in d


def test_an_alerts_own_severity_does_not_overwrite_the_log_level() -> None:
    d = _line("alert decision", severity="critical", decision="alert")
    assert d["severity"] == "INFO" and d["alert_severity"] == "critical" and d["decision"] == "alert"


def test_errors_carry_a_stack_trace_for_error_reporting() -> None:
    d = _line("crash", exc=True)
    assert d["severity"] == "ERROR" and d["stack_trace"].startswith("Traceback") and "ValueError: boom" in d["stack_trace"]


def test_trace_is_attached_only_with_a_project_and_header() -> None:
    token = set_request_trace("105445aa7843bc8bf206b12000100000/1;o=1", "my-proj")
    try:
        assert _line()[TRACE_KEY] == "projects/my-proj/traces/105445aa7843bc8bf206b12000100000"
    finally:
        reset_request_trace(token)
    assert TRACE_KEY not in _line()  # reset after the request
    assert set_request_trace("abc/1", None) is None and set_request_trace(None, "p") is None


def test_gemini_configured_needs_a_model_and_a_way_to_authenticate() -> None:
    def cfg(**kw: object) -> bool:
        return Settings(**{"_env_file": None, "gemini_api_key": None, "gemini_model": None, **kw}).gemini_configured

    assert not cfg(gemini_model="m")
    assert cfg(gemini_model="m", gemini_api_key="k")
    assert not cfg(gemini_model="m", gemini_use_vertexai=True)  # no project
    assert cfg(gemini_model="m", gemini_use_vertexai=True, google_cloud_project="p")
    assert not cfg(gemini_use_vertexai=True, google_cloud_project="p")  # no model


@pytest.mark.parametrize("vertex", [True, False])
def test_reporter_builds_the_right_gemini_client(monkeypatch: pytest.MonkeyPatch, vertex: bool) -> None:
    calls: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    import google.genai
    monkeypatch.setattr(google.genai, "Client", FakeClient)
    st = Settings(_env_file=None, gemini_model="gemini-x", gemini_api_key=None if vertex else "key",
                  gemini_use_vertexai=vertex, google_cloud_project="proj" if vertex else None,
                  google_cloud_location="asia-south1")
    rep = Reporter(st)
    assert rep.llm_enabled and isinstance(rep.client, reporter_mod.GeminiClient)
    kw = calls[0]
    if vertex:
        assert kw["vertexai"] is True and kw["project"] == "proj" and kw["location"] == "asia-south1"
        assert "api_key" not in kw
    else:
        assert kw["api_key"] == "key" and "vertexai" not in kw


def test_no_gemini_settings_means_template_reports() -> None:
    assert not Reporter(Settings(_env_file=None, gemini_api_key=None, gemini_model=None)).llm_enabled
