"""Private, append-only human feedback cases and outcome review records.

Feedback describes what the user actually decided after seeing a preserved
research report.  It never changes the original report or places an order.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import fcntl
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from quant_lab.storage import MarketStore


CASE_SCHEMA_VERSION = 1
REVIEW_SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 1_000_000
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
_CHOICES = frozenset({"watch", "skip", "buy", "sell"})


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _iso_day(value: str, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return parsed


def _read_record(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("record exceeds size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("record is not an object")
    return value


def _write_record(directory: Path, record: dict[str, Any], record_id: str) -> Path:
    body = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False).encode("utf-8") + b"\n"
    if len(body) > MAX_RECORD_BYTES:
        raise ValueError("record exceeds size limit")
    now = record["created_at"]
    folder = directory / now[:10]
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    timestamp = now.replace("-", "").replace(":", "").replace(".", "")
    final_path = folder / f"{timestamp}-{record_id}.json"
    temporary = folder / f".{record_id}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, final_path)
    finally:
        temporary.unlink(missing_ok=True)
    return final_path


def _journal_report(journal_dir: str | Path, record_date: str,
                    record_id: str) -> dict[str, Any]:
    _iso_day(record_date, "research_record_date")
    if not isinstance(record_id, str) or not _UUID.fullmatch(record_id):
        raise ValueError("invalid research_record_id")
    path = Path(journal_dir) / record_date / f"{record_id}.json"
    try:
        record = _read_record(path)
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("research record is missing or unreadable") from exc
    if record.get("record_id") != record_id or record.get("schema_version") != 1:
        raise ValueError("research record identity mismatch")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("research record has no payload")
    if hashlib.sha256(_canonical(payload)).hexdigest() != record.get("payload_sha256"):
        raise ValueError("research record hash mismatch")
    report = payload.get("report")
    if not isinstance(report, dict) or report.get("status") not in ("ready", "partial_data"):
        raise ValueError("research record has no successful report")
    if not isinstance(report.get("instrument_id"), str) or not report["instrument_id"]:
        raise ValueError("research report has no instrument")
    _iso_day(report.get("asof"), "report asof")
    return {"record": record, "report": report}


def append_feedback_case(
    cases_dir: str | Path,
    journal_dir: str | Path,
    *,
    research_record_id: str,
    research_record_date: str,
    decision: str,
    observed_date: str,
    executed_price: float | None = None,
    quantity: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Persist one human decision linked to the exact report the user saw."""
    if decision not in _CHOICES:
        raise ValueError("decision must be watch, skip, buy, or sell")
    observed = _iso_day(observed_date, "observed_date")
    if observed > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ValueError("observed_date cannot be in the future")
    if not isinstance(note, str) or len(note) > 500:
        raise ValueError("note must be at most 500 characters")
    if executed_price is not None:
        if isinstance(executed_price, bool) or not isinstance(executed_price, (int, float)):
            raise ValueError("executed_price must be a finite positive number")
        try:
            numeric_price = float(executed_price)
        except OverflowError as exc:
            raise ValueError("executed_price must be a finite positive number") from exc
        if not math.isfinite(numeric_price) or not 0 < numeric_price <= 1_000_000_000:
            raise ValueError("executed_price must be a finite positive number")
    if quantity is not None and (
        isinstance(quantity, bool) or not isinstance(quantity, int) or not 0 < quantity <= 1_000_000_000
    ):
        raise ValueError("quantity must be a positive integer")
    if decision in ("watch", "skip") and (executed_price is not None or quantity is not None):
        raise ValueError("watch/skip cannot contain execution details")
    linked = _journal_report(journal_dir, research_record_date, research_record_id)
    report = linked["report"]
    if observed < _iso_day(report["asof"], "report asof"):
        raise ValueError("observed_date cannot precede report date")
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    case_id = str(uuid4())
    decision_info = report.get("decision") if isinstance(report.get("decision"), dict) else {}
    payload = {
        "research_record_id": research_record_id,
        "research_record_date": research_record_date,
        "research_payload_sha256": linked["record"]["payload_sha256"],
        "instrument_id": report["instrument_id"],
        "name": report.get("name"),
        "report_asof": report["asof"],
        "report_latest_close": report.get("latest_close"),
        "source_provenance": decision_info.get("input_provenance"),
        "rule_version": decision_info.get("rule_version"),
        "decision": decision,
        "observed_date": observed_date,
        "executed_price": executed_price,
        "quantity": quantity,
        "note": note,
    }
    record = {
        "schema_version": CASE_SCHEMA_VERSION,
        "case_id": case_id,
        "created_at": created_at,
        "payload_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
        "payload": payload,
    }
    path = _write_record(Path(cases_dir), record, case_id)
    return {
        "case_id": case_id, "created_at": created_at, "instrument_id": report["instrument_id"],
        "decision": decision, "review_status": "awaiting_outcome", "path": str(path.resolve()),
    }


def _recent_paths(directory: str | Path, limit: int) -> list[Path]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    paths: list[Path] = []
    for path in _all_paths(directory):
        paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def _all_paths(directory: str | Path):
    """Iterate every saved record; display limits must not truncate reviews."""
    root = Path(directory)
    if not root.exists():
        return
    for day in sorted((item for item in root.iterdir()
                       if item.is_dir() and not item.is_symlink()), reverse=True):
        for path in sorted(day.glob("*.json"), reverse=True):
            if path.is_file() and not path.is_symlink():
                yield path


def list_feedback_cases(directory: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    """Public metadata only; notes and actual fills stay in private files."""
    summaries = []
    for path in _recent_paths(directory, limit):
        record = _read_record(path)
        payload = record.get("payload")
        if (record.get("schema_version") != CASE_SCHEMA_VERSION
                or not isinstance(payload, dict)
                or hashlib.sha256(_canonical(payload)).hexdigest() != record.get("payload_sha256")):
            raise ValueError(f"invalid feedback case: {path.name}")
        summaries.append({
            "case_id": record["case_id"], "created_at": record["created_at"],
            "research_record_id": payload["research_record_id"],
            "instrument_id": payload["instrument_id"], "name": payload.get("name"),
            "report_asof": payload["report_asof"], "decision": payload["decision"],
            "observed_date": payload["observed_date"],
            "review_status": "awaiting_outcome",
        })
    return summaries


def list_case_reviews(directory: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    summaries = []
    for path in _recent_paths(directory, limit):
        record = _load_review(path)
        payload = record["payload"]
        summaries.append({
            "review_id": record["review_id"], "case_id": payload["case_id"],
            "created_at": record["created_at"], "status": payload["status"],
            "horizon_sessions": payload.get("horizon_sessions"),
            "market_asof": payload.get("market_asof"),
            "outcome": payload.get("outcome"),
            "reason": payload.get("reason"),
        })
    return summaries


def _load_case(path: Path) -> dict[str, Any]:
    record = _read_record(path)
    payload = record.get("payload")
    if (record.get("schema_version") != CASE_SCHEMA_VERSION
            or not isinstance(payload, dict)
            or hashlib.sha256(_canonical(payload)).hexdigest() != record.get("payload_sha256")
            or not isinstance(record.get("case_id"), str)):
        raise ValueError(f"invalid feedback case: {path.name}")
    return record


def _load_review(path: Path) -> dict[str, Any]:
    record = _read_record(path)
    payload = record.get("payload")
    if (record.get("schema_version") != REVIEW_SCHEMA_VERSION
            or not isinstance(payload, dict)
            or hashlib.sha256(_canonical(payload)).hexdigest() != record.get("payload_sha256")
            or not isinstance(payload.get("case_id"), str)):
        raise ValueError(f"invalid case review: {path.name}")
    return record


def _review_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    """Fields that change only when a horizon or its evidence changes."""
    return {key: payload.get(key) for key in (
        "case_payload_sha256", "instrument_id", "report_asof", "status",
        "horizon_sessions", "outcome", "reason", "signal_source",
    )}


def _outcome_source(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    return {key: item.get(key) for key in (
        "status", "trade_date", "future_close", "source_id",
        "source_payload_hash", "source_run_id",
    )}


@contextmanager
def _review_lock(directory: str | Path):
    """Serialize read/compare/append across CLI and API processes."""
    root = Path(directory)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(root / ".review.lock", os.O_RDWR | os.O_CREAT |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def review_feedback_cases(
    store: MarketStore,
    cases_dir: str | Path,
    reviews_dir: str | Path,
    *,
    asof: date | None = None,
) -> dict[str, int | str | None]:
    """Append due 5/20/60-session price outcomes for saved feedback cases.

    The comparison starts from the report's signal close, not the user's actual
    trade.  If that saved close was revised, the outcome is blocked.  No real
    account return is inferred from a daily close series.
    """
    with _review_lock(reviews_dir):
        return _review_feedback_cases_locked(store, cases_dir, reviews_dir, asof=asof)


def _review_feedback_cases_locked(
    store: MarketStore,
    cases_dir: str | Path,
    reviews_dir: str | Path,
    *,
    asof: date | None,
) -> dict[str, int | str | None]:
    # Read every case and review.  The 20/100-record limits belong to the
    # display API, not the outcome engine or its idempotency check.
    existing_reviews = [_load_review(path) for path in _all_paths(reviews_dir)]
    latest_by_case: dict[str, dict[str, Any]] = {}
    first_outcome_by_case: dict[str, dict[str, dict[str, Any] | None]] = {}
    for review in reversed(existing_reviews):
        old = review["payload"]
        case_id = old["case_id"]
        latest_by_case[case_id] = old
        baseline = first_outcome_by_case.setdefault(case_id, {})
        if isinstance(old.get("outcome"), dict):
            for horizon, item in old["outcome"].items():
                baseline.setdefault(horizon, _outcome_source(item))
    case_paths = list(_all_paths(cases_dir))
    saved = 0
    pending = 0
    unavailable = 0
    with store.connect() as connection:
        row = connection.execute(
            "SELECT MAX(trade_date) AS day FROM daily_bars WHERE instrument_id='index:000300.SH'"
            + (" AND trade_date<=?" if asof else ""),
            (asof.isoformat(),) if asof else (),
        ).fetchone()
        market_asof = row["day"] if row else None
        if not market_asof:
            return {"status": "no_market_calendar", "market_asof": None,
                    "saved": 0, "pending": len(case_paths), "unavailable": 0}
        for path in case_paths:
            case = _load_case(path)
            payload = case["payload"]
            case_id = case["case_id"]
            report_asof = payload["report_asof"]
            index_dates = [item[0] for item in connection.execute(
                "SELECT trade_date FROM daily_bars WHERE instrument_id='index:000300.SH' "
                "AND trade_date>? AND trade_date<=? ORDER BY trade_date",
                (report_asof, market_asof),
            ).fetchall()]
            horizon_dates = {horizon: index_dates[horizon - 1]
                             for horizon in (5, 20, 60) if len(index_dates) >= horizon}
            provenance = payload.get("source_provenance")
            source_ok = (isinstance(provenance, dict)
                         and provenance.get("source_kind") == "main_store"
                         and provenance.get("price_basis") == "qfq")
            reason: str | None = None
            outcomes: dict[str, Any] = {}
            signal_source: dict[str, Any] | None = None
            if not source_ok:
                reason = "此研究记录不是已核验的主库前复权价格，暂不计算后续价格变化。"
            else:
                original = connection.execute(
                    "SELECT close,source_id,payload_hash,run_id FROM daily_bars WHERE instrument_id=? "
                    "AND trade_date=? AND adjustment='qfq'",
                    (payload["instrument_id"], report_asof),
                ).fetchone()
                if original is not None:
                    signal_source = {"close": float(original["close"]),
                                     "source_id": original["source_id"],
                                     "payload_hash": original["payload_hash"],
                                     "run_id": original["run_id"]}
                saved_close = payload.get("report_latest_close")
                if (original is None or isinstance(saved_close, bool)
                        or not isinstance(saved_close, (int, float))
                        or not math.isclose(float(original["close"]), float(saved_close),
                                            rel_tol=1e-8, abs_tol=1e-6)):
                    reason = "信号日收盘价已缺失或修订，不能把当前复权价格与当时报告直接比较。"
                else:
                    for horizon, target_day in horizon_dates.items():
                        target = connection.execute(
                            "SELECT close,source_id,payload_hash,run_id FROM daily_bars "
                            "WHERE instrument_id=? AND trade_date=? AND adjustment='qfq'",
                            (payload["instrument_id"], target_day),
                        ).fetchone()
                        if target is None:
                            outcomes[str(horizon)] = {
                                "status": "missing_stock_bar", "trade_date": target_day,
                            }
                        else:
                            outcomes[str(horizon)] = {
                                "status": "matured", "trade_date": target_day,
                                "signal_close": float(saved_close),
                                "future_close": float(target["close"]),
                                "price_return": float(target["close"]) / float(saved_close) - 1.0,
                                "source_id": target["source_id"],
                                "source_payload_hash": target["payload_hash"],
                                "source_run_id": target["run_id"],
                            }
                    source_changed = any(
                        provenance.get(saved) is not None
                        and original[current] != provenance[saved]
                        for saved, current in (
                            ("source_id", "source_id"),
                            ("source_run_id", "run_id"),
                            ("source_payload_hash", "payload_hash"),
                        )
                    )
                    if source_changed:
                        reason = "信号日来源哈希、提供方或采集批次已变化；记录价格未变，结果需人工复核。"
                    baseline = first_outcome_by_case.get(case_id, {})
                    revised = [horizon for horizon, item in outcomes.items()
                               if horizon in baseline
                               and _outcome_source(item) != baseline[horizon]]
                    if revised:
                        reason = ("到期价格或来源已相对首次复盘修订（窗口 "
                                  + "/".join(sorted(revised, key=int)) + "）；结果需人工复核。")
            if not horizon_dates and reason is None:
                pending += 1
                continue
            if reason and not outcomes:
                status = "unavailable"
                unavailable += 1
            elif reason:
                status = "needs_source_review"
                unavailable += 1
            else:
                status = ("complete" if len(outcomes) == 3
                          and all(item.get("status") == "matured" for item in outcomes.values())
                          else "partial")
            latest_horizon = max((int(key) for key in outcomes), default=None)
            review_payload = {
                "case_id": case_id,
                "case_payload_sha256": case["payload_sha256"],
                "research_record_id": payload["research_record_id"],
                "instrument_id": payload["instrument_id"],
                "report_asof": report_asof,
                "decision": payload["decision"],
                "observed_date": payload["observed_date"],
                "market_asof": market_asof,
                "status": status,
                "horizon_sessions": latest_horizon,
                "outcome": outcomes,
                "reason": reason,
                "signal_source": signal_source,
                "comparison_scope": "报告信号日后价格变化；不是用户实际成交收益",
            }
            previous = latest_by_case.get(case_id)
            if previous is not None and _review_evidence(previous) == _review_evidence(review_payload):
                continue
            created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            review_id = str(uuid4())
            record = {
                "schema_version": REVIEW_SCHEMA_VERSION,
                "review_id": review_id,
                "created_at": created_at,
                "payload_sha256": hashlib.sha256(_canonical(review_payload)).hexdigest(),
                "payload": review_payload,
            }
            _write_record(Path(reviews_dir), record, review_id)
            latest_by_case[case_id] = review_payload
            baseline = first_outcome_by_case.setdefault(case_id, {})
            for horizon, item in outcomes.items():
                baseline.setdefault(horizon, _outcome_source(item))
            saved += 1
    return {"status": "reviewed", "market_asof": market_asof,
            "saved": saved, "pending": pending, "unavailable": unavailable}
