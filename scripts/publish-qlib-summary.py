#!/usr/bin/env python3
"""Publish a small dashboard summary from an inspected, locally published release."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def read_json(path: Path, maximum: int) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if not raw or len(raw) > maximum:
        raise ValueError(f"invalid JSON file size: {path}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def summary_from(inspection: dict, index: dict) -> dict:
    if inspection.get("status") != "inspected" or inspection.get("source_id") != "investment_data_qlib_release":
        raise ValueError("inspection is not a completed Qlib release check")
    release = inspection["release"]
    published = index["release"]
    for key in ("tag", "archive_sha256", "manifest_sha256", "target_trade_date"):
        if release[key] != published[key]:
            raise ValueError(f"published release differs from inspection: {key}")
    if index.get("source_id") != inspection["source_id"] or index.get("index_version") != 1:
        raise ValueError("published release index has the wrong source or version")
    calendar = inspection["calendar"]
    instruments = inspection["instruments"]
    daily = inspection["daily_close"]
    reference = inspection["reference_coverage"]
    count = daily["stocks_with_valid_close"]
    bars = daily["valid_bar_count"]
    inactive = reference["inactive_with_valid_close"]
    if (not all(type(value) is int and value > 0 for value in (count, bars, inactive))
            or count != instruments["count"] or bars < count
            or inactive > reference["inactive_count"]
            or inspection["price_adjustment"] != "qlib_adjusted; factor must be validated before any raw-price comparison"
            or inspection["point_in_time_universe"] is not False
            or calendar["last"] != release["target_trade_date"]):
        raise ValueError("inspection coverage or adjustment check failed")
    return {
        "status": "inspected",
        "source_id": inspection["source_id"],
        "release_tag": release["tag"],
        "calendar_first": calendar["first"],
        "calendar_last": calendar["last"],
        "stock_count_with_valid_close": count,
        "bar_count": bars,
        "inactive_reference_covered": inactive,
        "adjustment": "qlib_adjusted",
        "eligible_for_screening": False,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspection", type=Path, required=True)
    parser.add_argument("--published-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inspection = read_json(args.inspection, 10 * 1024 * 1024)
    index = read_json(args.published_index, 64 * 1024 * 1024)
    summary = summary_from(inspection, index)
    target = args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(summary, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
