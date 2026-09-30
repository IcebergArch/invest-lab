"""Freeze displayed research forecasts and compare later prices without retraining.

This module makes no trading decision. A recorded forecast is eligible for
forward evidence only when it was issued on its price as-of date, its original
anchor bar is unchanged, and later observations really follow issuance.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quant_lab.research_journal import append_stock_report
from quant_lab.stock_analysis import analyze_stock
from quant_lab.storage import MarketStore


AUDIT_VERSION = "forecast-forward-evidence-v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def capture_universe(store: MarketStore, journal_dir: str | Path, asof: date) -> dict[str, Any]:
    """Manually capture every eligible stock in the *loaded* store, not a watchlist.

    A missing or stale stock is reported explicitly. The store inventory is
    not a claim of full A-share or point-in-time historical universe coverage.
    """
    ids = store.list_stock_ids()
    saved: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for instrument_id in ids:
        report = analyze_stock(store, instrument_id, asof=asof)
        chart = report.get("chart") if isinstance(report.get("chart"), dict) else {}
        forecast = chart.get("forecast") if isinstance(chart.get("forecast"), dict) else {}
        if (report.get("status") not in ("ready", "partial_data")
                or report.get("asof") != asof.isoformat()
                or forecast.get("status") != "research_only"
                or forecast.get("price_basis") != "qfq_cny"):
            skipped.append({"instrument_id": instrument_id, "reason":
                            "missing_current_qfq_forecast_or_history"})
            continue
        record = append_stock_report(
            journal_dir, query=instrument_id, cost_price=None, report=report,
            versions={"capture": AUDIT_VERSION, "chart": chart.get("chart_version"),
                      "forecast_model_id": forecast["model_id"],
                      "forecast_model_version": forecast["model_version"]},
        )
        saved.append({"instrument_id": instrument_id,
                      "record_id": record["record_id"], "path": record["path"]})
    return {"version": AUDIT_VERSION, "asof": asof.isoformat(),
            "universe_scope": "all_stocks_currently_loaded_in_market_store",
            "inventory_count": len(ids), "saved": saved, "skipped": skipped}


def _read_journal(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"journal symlink: {path}")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema_version") != 1 or not isinstance(record.get("payload"), dict):
        raise ValueError(f"invalid journal schema: {path}")
    if hashlib.sha256(_canonical(record["payload"])).hexdigest() != record.get("payload_sha256"):
        raise ValueError(f"journal hash mismatch: {path}")
    return record


def _forecast_record(record: dict[str, Any]) -> bool:
    report = record["payload"].get("report", {})
    chart = report.get("chart") or {}
    forecast = chart.get("forecast") or {}
    points = forecast.get("points")
    return (report.get("status") in ("ready", "partial_data")
            and chart.get("price_basis") == "qfq_cny"
            and forecast.get("status") == "research_only"
            and forecast.get("price_basis") == "qfq_cny"
            and isinstance(points, list) and len(points) == 20
            and [item.get("step") for item in points] == list(range(1, 21))
            and all(isinstance(item.get("median"), (int, float))
                    and math.isfinite(item["median"]) and item["median"] > 0
                    for item in points)
            and forecast.get("asof") == report.get("asof")
            and chart.get("asof") == report.get("asof"))


def _key(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    report = record["payload"]["report"]
    forecast = report["chart"]["forecast"]
    return (report["instrument_id"], report["asof"], forecast["model_id"],
            forecast["model_version"], forecast["price_basis"])


def evaluate_record(record: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Score future observed sessions; reject backdated and revised anchors."""
    report = record["payload"]["report"]
    forecast = report["chart"]["forecast"]
    asof = report["asof"]
    issued_at = datetime.fromisoformat(record["generated_at"].replace("Z", "+00:00"))
    issued_day = issued_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    result: dict[str, Any] = {
        "record_id": record["record_id"], "instrument_id": report["instrument_id"],
        "asof": asof, "issued_at": record["generated_at"],
        "model_id": forecast["model_id"], "model_version": forecast["model_version"],
        "price_basis": forecast["price_basis"], "horizon": 20,
        "status": "pending", "observed_steps": 0, "points": [],
    }
    if issued_day > asof:
        result["status"] = "late_issued_not_forward"
        return result
    anchor = next((row for row in rows if row["trade_date"] == asof), None)
    original = report["chart"]["history"][-1]
    original_hash = report["sources"][0].get("latest_payload_hash")
    if (anchor is None or original.get("date") != asof
            or not math.isclose(float(anchor["close"]), float(original["close"]), rel_tol=1e-10)):
        result["status"] = "anchor_revised_or_missing"
        return result
    if anchor["payload_hash"] != original_hash:
        result["status"] = "anchor_source_revision"
        return result
    future = [row for row in rows if row["trade_date"] > asof]
    for projected, actual in zip(forecast["points"], future[:20]):
        if actual["trade_date"] <= issued_day:
            result["status"] = "preissued_outcome_in_store"
            result["points"] = []
            return result
        expected_return = projected["median"] / original["close"] - 1
        actual_return = actual["close"] / original["close"] - 1
        result["points"].append({
            "step": projected["step"], "actual_date": actual["trade_date"],
            "predicted_close": projected["median"], "actual_close": actual["close"],
            "actual_payload_hash": actual["payload_hash"],
            "absolute_return_error": abs(expected_return - actual_return),
            "unchanged_price_error": abs(actual_return),
            "direction_correct": (expected_return > 0) == (actual_return > 0)
                                 if expected_return != 0 and actual_return != 0 else None,
        })
    result["observed_steps"] = len(result["points"])
    result["status"] = "complete" if len(result["points"]) == 20 else "pending"
    if result["points"]:
        last = result["points"][-1]
        result["latest_step"] = last["step"]
        result["latest_absolute_return_error"] = last["absolute_return_error"]
        result["latest_unchanged_price_error"] = last["unchanged_price_error"]
        result["latest_direction_correct"] = last["direction_correct"]
    return result


def review_journal(journal_dir: str | Path, db_path: str | Path) -> dict[str, Any]:
    """Read all immutable research reports and evaluate unique forward origins."""
    root = Path(journal_dir)
    records = [_read_journal(path) for path in sorted(root.glob("*/*.json"))] if root.exists() else []
    eligible = sorted((item for item in records if _forecast_record(item)),
                      key=lambda item: (item["generated_at"], item["record_id"]))
    earliest: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for item in eligible:
        earliest.setdefault(_key(item), item)
    results = []
    connection = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        for key, item in sorted(earliest.items()):
            rows = [dict(row) for row in connection.execute(
                "SELECT trade_date,close,payload_hash FROM daily_bars "
                "WHERE instrument_id=? AND adjustment='qfq' AND trade_date>=? "
                "ORDER BY trade_date LIMIT 21", (key[0], key[1]))]
            results.append(evaluate_record(item, rows))
        asof_row = connection.execute("SELECT MAX(trade_date) FROM daily_bars").fetchone()
    finally:
        connection.close()
    complete = [item for item in results if item["status"] == "complete"]
    model_error = [item["points"][-1]["absolute_return_error"] for item in complete]
    baseline_error = [item["points"][-1]["unchanged_price_error"] for item in complete]
    return {"version": AUDIT_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "market_data_asof": asof_row[0],
            "scope": "unique_instrument_asof_model_version_qfq_origins_from_research_journal",
            "journal_record_count": len(records), "eligible_forecast_records": len(eligible),
            "duplicate_origins": len(eligible) - len(earliest),
            "origin_count": len(results), "complete_count": len(complete),
            "mean_20_step_absolute_return_error": sum(model_error) / len(model_error) if model_error else None,
            "unchanged_price_baseline_error": sum(baseline_error) / len(baseline_error) if baseline_error else None,
            "results": results}
