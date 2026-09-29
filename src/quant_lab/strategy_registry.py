"""Immutable strategy research snapshots, separate from mutable latest reports.

Each snapshot retains the complete research report, including the equity curve,
data fingerprint, parameters, benchmark, and evaluation limitations.  It is a
research record, not evidence of live or independent out-of-sample performance.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 2_000_000
_ORIGINS = frozenset({"pipeline_run", "baseline_capture", "published_import"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _validate_report(report: Mapping[str, Any]) -> None:
    if not isinstance(report.get("strategy_id"), str) or not report["strategy_id"]:
        raise ValueError("strategy report lacks strategy_id")
    if not isinstance(report.get("strategy_version"), str) or not report["strategy_version"]:
        raise ValueError("strategy report lacks strategy_version")
    try:
        asof = report["asof"]
        if date.fromisoformat(asof).isoformat() != asof:
            raise ValueError("invalid asof")
        backtest = report["backtest"]
        if not isinstance(backtest, dict) or not isinstance(backtest["metrics"], dict):
            raise ValueError("backtest metrics missing")
        if not _SHA256.fullmatch(backtest["input_fingerprint_sha256"]):
            raise ValueError("invalid input fingerprint")
        if not isinstance(backtest["equity_curve"], list) or not backtest["equity_curve"]:
            raise ValueError("equity curve missing")
        if not isinstance(report["universe"], list) or not report["universe"]:
            raise ValueError("universe missing")
        if not isinstance(report["strategy_parameters"], dict):
            raise ValueError("strategy parameters missing")
        if not isinstance(report["cost_rate"], (int, float)) or isinstance(report["cost_rate"], bool):
            raise ValueError("cost rate missing")
        if not math.isfinite(report["cost_rate"]) or report["cost_rate"] < 0:
            raise ValueError("invalid cost rate")
        _canonical(report)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid strategy research report") from exc


def append_strategy_snapshot(
    directory: str | Path,
    research: Mapping[str, Any],
    *,
    source_run_id: str | None = None,
    origin: str,
) -> dict[str, Any]:
    """Atomically append a complete, hash-addressed strategy result."""
    _validate_report(research)
    if origin not in _ORIGINS:
        raise ValueError("unknown strategy snapshot origin")
    if source_run_id is not None and not _RUN_ID.fullmatch(source_run_id):
        raise ValueError("invalid source run ID")
    report = dict(research)
    report_hash = hashlib.sha256(_canonical(report)).hexdigest()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    record_id = uuid4().hex
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "generated_at": generated_at,
        "source_run_id": source_run_id,
        "origin": origin,
        "research_sha256": report_hash,
        "research": report,
    }
    body = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False).encode("utf-8") + b"\n"
    if len(body) > MAX_RECORD_BYTES:
        raise ValueError("strategy snapshot exceeds size limit")
    target_dir = Path(directory) / generated_at[:10]
    target_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    filename = generated_at.replace("-", "").replace(":", "").replace(".", "")
    final_path = target_dir / f"{filename}-{record_id}.json"
    temp_path = target_dir / f".{record_id}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_path, final_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return {key: record[key] for key in (
        "record_id", "generated_at", "source_run_id", "origin", "research_sha256",
    )} | {"path": str(final_path.resolve())}


def list_strategy_snapshots(directory: str | Path, limit: int = 30) -> list[dict[str, Any]]:
    """Read the newest immutable reports and verify each research payload hash."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    root = Path(directory)
    if not root.exists():
        return []
    paths: list[Path] = []
    for day in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True):
        for path in sorted(day.glob("*.json"), reverse=True):
            paths.append(path)
            if len(paths) >= limit:
                break
        if len(paths) >= limit:
            break
    return [_read_snapshot(path) for path in paths]


def _read_snapshot(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError(f"oversize strategy snapshot: {path.name}")
    try:
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("invalid schema")
        research = record["research"]
        _validate_report(research)
        digest = hashlib.sha256(_canonical(research)).hexdigest()
        if digest != record.get("research_sha256"):
            raise ValueError("research hash mismatch")
        if record.get("origin") not in _ORIGINS:
            raise ValueError("invalid origin")
        if not isinstance(record.get("record_id"), str) or not _RUN_ID.fullmatch(record["record_id"]):
            raise ValueError("invalid record ID")
        if record.get("source_run_id") is not None and not _RUN_ID.fullmatch(record["source_run_id"]):
            raise ValueError("invalid source run ID")
        return record
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid strategy snapshot: {path.name}") from exc


def list_latest_strategy_snapshots(
    directory: str | Path, strategy_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Find the newest verified snapshot per strategy across the full archive.

    A recent-N window can hide a strategy after many runs of another.  This
    reader walks all older files as needed and stops once every requested
    strategy is found.  Without a requested set it inspects the full archive.
    """
    root = Path(directory)
    if not root.exists():
        return []
    if strategy_ids is not None and not all(isinstance(item, str) and item for item in strategy_ids):
        raise ValueError("strategy IDs must be nonempty strings")
    remaining = set(strategy_ids) if strategy_ids is not None else None
    latest: dict[str, dict[str, Any]] = {}
    for day in sorted((item for item in root.iterdir()
                       if item.is_dir() and not item.is_symlink()), reverse=True):
        for path in sorted(day.glob("*.json"), reverse=True):
            if path.is_symlink():
                raise ValueError("strategy snapshot must not be a symlink")
            record = _read_snapshot(path)
            strategy_id = record["research"]["strategy_id"]
            if strategy_id not in latest and (remaining is None or strategy_id in remaining):
                latest[strategy_id] = record
                if remaining is not None:
                    remaining.discard(strategy_id)
                    if not remaining:
                        return list(latest.values())
    return list(latest.values())
