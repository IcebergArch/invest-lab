"""Manual capture and forward review of the loaded stock universe.

Usage:
  PYTHONPATH=src python scripts/forecast_evidence.py capture --asof YYYY-MM-DD
  PYTHONPATH=src python scripts/forecast_evidence.py review --out PATH
No scheduled work and no model or online parameter mutation occur here.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from quant_lab.forecast_evidence import capture_universe, review_journal
from quant_lab.storage import MarketStore


def main() -> None:
    parser = argparse.ArgumentParser(description="手动留存预测并用后续事实核验")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--asof", required=True)
    review = sub.add_parser("review")
    review.add_argument("--out")
    for item in (capture, review):
        item.add_argument("--db", default="data/quant/market.sqlite3")
        item.add_argument("--journal", default="reports/quant/research-journal")
    args = parser.parse_args()
    if args.command == "capture":
        result = capture_universe(MarketStore(args.db), args.journal,
                                  date.fromisoformat(args.asof))
    else:
        result = review_journal(args.journal, args.db)
        if args.out:
            path = Path(args.out)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                       allow_nan=False) + "\n", encoding="utf-8")
            temp.replace(path)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
