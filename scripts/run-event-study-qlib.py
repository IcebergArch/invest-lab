#!/usr/bin/env python3
"""Reproduce the fixed-release 40-stock policy-event study locally.

Run from the repository root with ``PYTHONPATH=src python3 scripts/run-event-study-qlib.py``.
No data is fetched or modified; each experiment creates new report files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from quant_lab.event_study import append_event_study_record
from quant_lab.event_study_qlib import evaluate_qlib_event_cohort


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive fixed Qlib cohort policy-event report")
    parser.add_argument("--qlib-root", type=Path,
                        default=Path("data/quant/qlib-releases/2026-09-28/published"))
    parser.add_argument("--manifest", type=Path,
                        default=Path("data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json"))
    parser.add_argument("--expected-tag", default="2026-09-28")
    parser.add_argument("--main-db", type=Path,
                        default=Path("data/quant/market.sqlite3"))
    parser.add_argument("--events", type=Path,
                        default=Path("data/quant/reviewed-policy-events.json"))
    parser.add_argument("--out", type=Path,
                        default=Path("reports/quant/event-studies-qlib"))
    args = parser.parse_args()
    result = evaluate_qlib_event_cohort(
        args.qlib_root, args.manifest, args.expected_tag,
        args.main_db, args.events,
    )
    saved = append_event_study_record(args.out, result)
    summary = {
        "status": "archived",
        "release_tag": result["qlib_release"]["release"]["tag"],
        "selected_stocks": result["qlib_selection"]["selected_count"],
        "observed_stocks": result["qlib_selection"]["observed_stock_count"],
        "event_count": len(result["events"]),
        "event_coverage": [
            {"event_id": event["event_id"], "status": event["status"],
             "post_5_complete": (
                 event["cohorts"]["all_selected_stocks"]["windows"]["post_5"]["complete_count"]
                 if event["status"] == "ready" else 0)}
            for event in result["events"]
        ],
        "input_fingerprint_sha256": result["input_fingerprint_sha256"],
        **saved,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
