"""Append-only local records of completed stock research reports.

The journal is deliberately separate from MarketStore.  A record contains the
exact report returned by analysis, so later reviews can inspect the decision,
chart, source lineage, and rule versions that the user actually saw.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


JOURNAL_SCHEMA_VERSION = 1
_SUCCESS_STATUSES = frozenset({"ready", "partial_data"})


def _canonical_json(value: object) -> bytes:
    """Serialize without ordering or whitespace differences affecting a hash."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_json_types(value: object) -> None:
    """Reject values JSON would silently coerce, especially dictionary keys."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("research record object keys must be strings")
            _require_json_types(item)
    elif isinstance(value, list):
        for item in value:
            _require_json_types(item)
    elif value is None or isinstance(value, (str, bool, int)):
        return
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("research record contains a nonfinite number")
    else:
        raise TypeError("research record contains a non-JSON or nonfinite value")


def _fsync_directory(directory: Path) -> None:
    """Persist the new directory entry when the filesystem supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


def append_stock_report(
    journal_dir: str | Path,
    *,
    query: str,
    cost_price: float | None,
    report: Mapping[str, Any],
    versions: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Save one successful stock report as a private, immutable JSON file.

    The SHA-256 covers only the canonical payload (request, full report and
    versions), so repeated reports have the same hash even though their record
    IDs and generation times differ.  Any validation or storage failure raises;
    callers must explicitly report that the record was not confirmed saved.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a nonempty string")
    if cost_price is not None and (
        isinstance(cost_price, bool) or not isinstance(cost_price, (int, float))
        or not math.isfinite(cost_price) or cost_price <= 0
    ):
        raise ValueError("cost_price must be a finite positive number or None")
    if not isinstance(report, Mapping) or report.get("status") not in _SUCCESS_STATUSES:
        raise ValueError("only successful stock reports can be journaled")
    if not isinstance(report.get("instrument_id"), str) or not report["instrument_id"]:
        raise ValueError("successful report needs instrument_id")
    asof = report.get("asof")
    if not isinstance(asof, str) or date.fromisoformat(asof).isoformat() != asof:
        raise ValueError("successful report needs ISO asof date")
    sources = report.get("sources")
    if not isinstance(sources, list) or not sources or not all(isinstance(item, Mapping) for item in sources):
        raise ValueError("successful report needs source lineage")
    if versions is not None and not isinstance(versions, Mapping):
        raise ValueError("versions must be a mapping")

    payload = {
        "request": {"query": query.strip(), "cost_price": cost_price},
        "report": dict(report),
        "versions": dict(versions) if versions is not None else {},
    }
    # Validate before any directory or file is created.  Reject NaN, Infinity,
    # non-JSON values and non-string map keys instead of silently changing data.
    _require_json_types(payload)
    canonical = _canonical_json(payload)
    payload_hash = hashlib.sha256(canonical).hexdigest()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    record_id = str(uuid4())
    record = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "record_id": record_id,
        "generated_at": generated_at,
        "payload_sha256": payload_hash,
        "payload": payload,
    }
    body = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False).encode("utf-8") + b"\n"

    journal_root = Path(journal_dir)
    journal_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(journal_root, 0o700)
    target_dir = journal_root / generated_at[:10]
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target_dir, 0o700)
    final_path = target_dir / f"{record_id}.json"
    temp_path = target_dir / f".{record_id}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link publishes the complete temporary file and fails if the
        # UUID target already exists; os.replace would silently overwrite it.
        os.link(temp_path, final_path)
        temp_path.unlink()
        _fsync_directory(target_dir)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)

    return {
        "record_id": record_id,
        "generated_at": generated_at,
        "payload_sha256": payload_hash,
        "path": str(final_path.resolve()),
    }
