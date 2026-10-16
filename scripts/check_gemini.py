"""Live Gemini smoke test: builds a Critical alert's facts from the demo data and asks Gemini for the report.

Prints the path taken (gemini / gemini_fallback_model / template), every attempt with its guardrail
violations, and the final report. Uses a temporary copy of the database, so the demo state is untouched.

Usage:  python scripts/check_gemini.py        (needs GEMINI_API_KEY and GEMINI_MODEL in .env)
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.schemas import OrderIn, OrderItemIn  # noqa: E402
from app.services.calendar import load_calendar  # noqa: E402
from app.services.engine import Engine  # noqa: E402
from app.services.inventory import apply_order  # noqa: E402
from app.services.reporter import ReportService  # noqa: E402


def main() -> None:
    st = get_settings()
    if not st.gemini_configured:
        sys.exit("Set GEMINI_MODEL plus GEMINI_API_KEY (or GEMINI_USE_VERTEXAI=true and GOOGLE_CLOUD_PROJECT) first.")
    tmp = Path(tempfile.mkdtemp()) / "check.db"
    src, dst = sqlite3.connect(st.database_path), sqlite3.connect(tmp)
    src.backup(dst)
    src.close()
    dst.close()
    db = Database(str(tmp))
    anchor = sqlite3.connect(tmp).execute("SELECT value FROM app_meta WHERE key='anchor'").fetchone()[0]
    from datetime import datetime
    now = datetime.fromisoformat(anchor) + timedelta(minutes=5)
    eng = Engine(db, None, st, load_calendar(st.calendar_file), clock=lambda: now)
    eng.recalibrate(now)
    sku9 = "SN-FWS-WG-UK9"
    for i in range(3):
        apply_order(db, OrderIn(items=[OrderItemIn(sku=sku9, qty=1)]), now + timedelta(minutes=i))
    decisions = eng.evaluate_variants([16], "order", now=now + timedelta(minutes=4), include_opportunity=False)
    facts = next(d.facts for d in decisions if d.facts is not None)
    svc = ReportService.for_database(db, None, st, background=False)
    result = svc.reporter.build(facts)
    print(f"severity: {facts.severity}   path: {result.source}")
    for a in result.attempts:
        print("attempt:", json.dumps(a, ensure_ascii=False))
    print(json.dumps(result.report.model_dump(), indent=2, ensure_ascii=False))
    db.dispose()
    shutil.rmtree(tmp.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
