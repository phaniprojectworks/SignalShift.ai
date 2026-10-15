"""Counterfactual replay: synthetic history with vs without SignalShift, scored against the hidden ground truth.

Usage:  python scripts/run_replay.py [--days 30] [--approval-delay 1] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import Database, init_db  # noqa: E402
from app.services.calendar import load_calendar  # noqa: E402
from app.services.fmt import inr  # noqa: E402
from app.services.forecast import load_factors, recalibrate  # noqa: E402
from app.services.replay import ReplayConfig, run_replay  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--approval-delay", type=float, default=1.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    st = get_settings()
    db = Database(st.database_path)
    init_db(db.engine)
    events = load_calendar(st.calendar_file)
    with db.session() as s:
        if not load_factors(s):
            from datetime import datetime
            from app.models import AppMeta
            from sqlalchemy import select
            recalibrate(s, events, datetime.fromisoformat(s.scalar(select(AppMeta.value).where(AppMeta.key == "anchor"))))
    r = run_replay(db, st, events, ReplayConfig(days=args.days, approval_delay_hours=args.approval_delay))
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    d, wo, wi = r["detection"], r["without_signalshift"], r["with_signalshift"]
    print(f"Counterfactual replay {r['window']['start']} -> {r['window']['end']} "
          f"({r['window']['days']} days, {r['window']['variants_replayed']} variants)  [SYNTHETIC SAMPLE DATA]\n")
    print(f"{'':34}{'without':>16}{'with SignalShift':>20}")
    print(f"{'Wasted on out-of-stock clicks':34}{inr(wo['wasted_on_out_of_stock_clicks']):>16}"
          f"{inr(wi['wasted_on_out_of_stock_clicks']):>20}")
    print(f"{'Stockouts':34}{wo['stockouts']:>16}{wo['stockouts'] - wi['stockouts_prevented']:>20}")
    print(f"{'Lost sales from stockouts':34}{inr(wo['lost_sales']):>16}{inr(wo['lost_sales'] - wi['sales_protected']):>20}")
    print(f"\nAd spend saved: {inr(wi['ad_spend_saved'])} · stockouts prevented: {wi['stockouts_prevented']} · "
          f"sales protected: {inr(wi['sales_protected'])}")
    print(f"Detection vs hidden ground truth: recall {d['recall']} ({d['detected']}/{d['ground_truth_stockouts']}, "
          f"{d['detected_before_stockout']} before the stockout), precision {d['precision']} "
          f"({d['true_positive_episodes']}/{d['critical_alert_episodes']} critical alert episodes)")
    print(f"Early warning: {d['warned_in_advance']}/{d['ground_truth_stockouts']} stockouts were warned in advance "
          f"(median {d['median_warning_lead_days']} days ahead). {d['note']}\n")
    print(f"{'sku':18}{'stockout':18}{'first critical':18}{'lead h':>7}{'warn lead d':>12}  preventable")
    for g in r["ground_truth"]:
        print(f"{g['sku']:18}{g['stockout_ts']:18}{(g['first_critical'] or '-'):18}"
              f"{str(g['critical_lead_hours'] if g['critical_lead_hours'] is not None else '-'):>7}"
              f"{str(g['warning_lead_days'] if g['warning_lead_days'] is not None else '-'):>12}  {g['preventable']}")
    print("\nAssumptions:\n- " + "\n- ".join(r["assumptions"]))


if __name__ == "__main__":
    main()
