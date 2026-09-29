"""Read-only inventory of saved source observations and their actual coverage.

SQLite sources are read in one transaction against a live WAL snapshot.  The
published Qlib release is inspected through its small index and verified text
metadata; this module never scans all feature binaries or fetches new data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from quant_lab.qlib_archive import FEATURE_RE, QlibArchiveError, parse_calendar, parse_instruments
from quant_lab.qlib_local import INDEX_NAME, _load_index, _verified_file


INVENTORY_VERSION = "raw-observation-inventory-v1"
QLIB_SOURCE_FIELDS = {"open", "high", "low", "close", "volume", "amount"}
QLIB_TRANSFORM_FIELDS = {"adjclose", "change", "factor", "vwap"}
SQLITE_COLUMNS = {
    "daily_bars": {"instrument_id", "trade_date", "open", "high", "low", "close",
                   "volume", "amount", "volume_unit", "amount_unit", "adjustment",
                   "source_id", "payload_hash", "run_id", "fetched_at"},
    "instruments": {"instrument_id", "asset_type", "exchange"},
}


def _sha256_json(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_state(path: Path) -> dict[str, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _db_file_state(path: Path) -> dict[str, object]:
    return {"database": _file_state(path), "wal": _file_state(Path(str(path) + "-wal"))}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _group_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute("""
        SELECT b.source_id, b.adjustment, i.asset_type,
               b.volume_unit, b.amount_unit,
               COUNT(*) AS bar_rows, COUNT(DISTINCT b.instrument_id) AS instrument_count,
               MIN(b.trade_date) AS first_trade_date,
               MAX(b.trade_date) AS last_trade_date,
               SUM(CASE WHEN b.open > 0 AND b.high >= b.low AND b.low > 0
                             AND b.close > 0 THEN 1 ELSE 0 END) AS plausible_ohlc_rows,
               SUM(CASE WHEN b.volume >= 0 THEN 1 ELSE 0 END) AS stored_volume_rows,
               SUM(CASE WHEN b.volume = 0 THEN 1 ELSE 0 END) AS zero_stored_volume_rows,
               SUM(CASE WHEN b.amount IS NOT NULL THEN 1 ELSE 0 END) AS amount_observed_rows,
               SUM(CASE WHEN b.amount IS NULL THEN 1 ELSE 0 END) AS amount_null_rows,
               SUM(CASE WHEN b.run_id IS NOT NULL THEN 1 ELSE 0 END) AS run_id_rows,
               SUM(CASE WHEN b.payload_hash IS NOT NULL THEN 1 ELSE 0 END) AS payload_hash_rows
        FROM daily_bars b JOIN instruments i USING (instrument_id)
        GROUP BY b.source_id, b.adjustment, i.asset_type, b.volume_unit, b.amount_unit
        ORDER BY b.source_id, b.adjustment, i.asset_type, b.volume_unit, b.amount_unit
    """).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        row["stored_zero_may_represent_source_null"] = row["source_id"] == "baostock_daily"
        if row["source_id"] == "baostock_daily":
            row["source_volume_caveat"] = (
                "BaoStock adapter maps nullable source volume to stored zero on "
                "ineligible days; exact source null count was not preserved"
            )
    return result


def _year_exchange_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute("""
        SELECT substr(b.trade_date, 1, 4) AS year,
               CASE WHEN upper(i.exchange) IN ('SH', 'SSE') THEN 'SH'
                    WHEN upper(i.exchange) IN ('SZ', 'SZSE') THEN 'SZ'
                    WHEN upper(i.exchange) IN ('BJ', 'BSE') THEN 'BJ'
                    ELSE COALESCE(NULLIF(upper(i.exchange), ''), 'UNKNOWN') END AS exchange,
               COUNT(*) AS bar_rows,
               COUNT(DISTINCT b.instrument_id) AS instrument_count,
               MIN(b.trade_date) AS first_trade_date,
               MAX(b.trade_date) AS last_trade_date
        FROM daily_bars b JOIN instruments i USING (instrument_id)
        WHERE i.asset_type = 'stock'
        GROUP BY substr(b.trade_date, 1, 4), exchange
        ORDER BY year, exchange
    """).fetchall()
    return [dict(row) for row in rows]


def _fill_year_exchange(rows: list[dict[str, object]], *,
                        start_year: int, end_year: int,
                        exchanges: tuple[str, ...] = ("SH", "SZ")) -> list[dict[str, object]]:
    """Expose zero years explicitly; a global min/max is not market coverage."""
    found = {(int(row["year"]), str(row["exchange"])): row for row in rows}
    result: list[dict[str, object]] = []
    for year in range(start_year, end_year + 1):
        for exchange in exchanges:
            result.append(found.get((year, exchange), {
                "year": str(year), "exchange": exchange, "bar_rows": 0,
                "instrument_count": 0, "first_trade_date": None,
                "last_trade_date": None,
            }))
    result.extend(row for row in rows
                  if str(row["exchange"]) not in exchanges
                  or not start_year <= int(row["year"]) <= end_year)
    return result


def _status_rows(connection: sqlite3.Connection) -> dict[str, object] | None:
    columns = _table_columns(connection, "baostock_daily_status")
    required = {"instrument_id", "trade_date", "tradestatus", "is_st",
                "raw_amount_cny", "has_valid_bar", "payload_hash", "run_id"}
    if not required <= columns:
        return None
    row = connection.execute("""
        SELECT COUNT(*) AS status_rows,
               COUNT(DISTINCT s.instrument_id) AS status_instruments,
               MIN(s.trade_date) AS first_trade_date,
               MAX(s.trade_date) AS last_trade_date,
               SUM(CASE WHEN s.tradestatus IS NOT NULL THEN 1 ELSE 0 END)
                   AS trade_status_observed_rows,
               SUM(CASE WHEN s.is_st IS NOT NULL THEN 1 ELSE 0 END)
                   AS st_flag_observed_rows,
               SUM(CASE WHEN s.raw_amount_cny IS NOT NULL THEN 1 ELSE 0 END)
                   AS raw_amount_cny_observed_rows,
               SUM(CASE WHEN s.has_valid_bar = 1 THEN 1 ELSE 0 END)
                   AS valid_bar_flag_rows,
               SUM(CASE WHEN s.tradestatus = 0 OR s.is_st = 1 THEN 1 ELSE 0 END)
                   AS ineligible_status_rows,
               SUM(CASE WHEN (s.tradestatus = 0 OR s.is_st = 1)
                             AND s.raw_amount_cny IS NOT NULL THEN 1 ELSE 0 END)
                   AS ineligible_status_raw_amount_observed_rows,
               SUM(CASE WHEN b.instrument_id IS NOT NULL THEN 1 ELSE 0 END)
                   AS matching_bar_rows,
               SUM(CASE WHEN b.instrument_id IS NOT NULL AND b.amount IS NULL
                             AND s.raw_amount_cny IS NOT NULL THEN 1 ELSE 0 END)
                   AS canonical_amount_null_but_raw_amount_observed_rows
        FROM baostock_daily_status s
        LEFT JOIN daily_bars b ON b.instrument_id=s.instrument_id
                              AND b.trade_date=s.trade_date AND b.adjustment='none'
    """).fetchone()
    return dict(row)


def _read_sqlite(path: str | Path, *, role: str,
                 start_year: int = 2000) -> dict[str, object]:
    db = Path(path).resolve()
    if not db.is_file():
        return {"status": "unavailable", "path": str(db), "reason": "database file is absent"}
    before = _db_file_state(db)
    connection: sqlite3.Connection | None = None
    mode = "ro"
    try:
        try:
            connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            if connection is not None:
                connection.close()
            # WAL without sidecars cannot be opened read-only.  Existing
            # archive_status uses this same fallback.  No SQL writes follow;
            # SQLite itself may create -wal/-shm sidecars.
            mode = "rw_query_only"
            connection = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, timeout=5)
            connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN")
        quick_check = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        if quick_check != ["ok"]:
            raise ValueError(f"SQLite quick_check failed: {quick_check[:3]}")
        for table, required in SQLITE_COLUMNS.items():
            if not required <= _table_columns(connection, table):
                raise ValueError(f"{table} lacks required inventory columns")
        groups = _group_rows(connection)
        stock_instrument_count = int(connection.execute("""
            SELECT COUNT(DISTINCT b.instrument_id)
            FROM daily_bars b JOIN instruments i USING (instrument_id)
            WHERE i.asset_type='stock'
        """).fetchone()[0])
        observed_years = _year_exchange_rows(connection)
        all_rows = [row for row in observed_years if int(row["bar_rows"]) > 0]
        end_year = max([start_year] + [int(row["year"]) for row in all_rows])
        if role == "baostock_archive":
            coverage = _fill_year_exchange(observed_years, start_year=start_year,
                                           end_year=end_year)
        else:
            coverage = observed_years
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        latest = None
        if "sync_runs" in tables:
            row = connection.execute("""
                SELECT run_id,status,started_at,finished_at
                FROM sync_runs ORDER BY started_at DESC LIMIT 1
            """).fetchone()
            latest = dict(row) if row else None
        catalogue = None
        if role == "baostock_archive" and "backfill_snapshots" in tables:
            row = connection.execute("""
                SELECT snapshot_id,scope,collected_at,listing_count
                FROM backfill_snapshots ORDER BY recorded_at DESC LIMIT 1
            """).fetchone()
            catalogue = dict(row) if row else None
        window_counts = None
        if role == "baostock_archive" and "backfill_windows" in tables:
            window_counts = {str(row["status"]): int(row["n"])
                             for row in connection.execute("""
                                 SELECT status, COUNT(*) AS n
                                 FROM backfill_windows GROUP BY status
                             """)}
        status = _status_rows(connection) if role == "baostock_archive" else None
        metadata = None
        if "metadata" in tables:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            metadata = row[0] if row else None
        connection.execute("COMMIT")
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {"status": "unavailable", "path": str(db),
                "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        if connection is not None:
            connection.close()
    after = _db_file_state(db)
    stock_rows = sum(int(row["bar_rows"]) for row in groups
                     if row["asset_type"] == "stock")
    zero_years = [str(year) for year in range(start_year, end_year + 1)
                  if not any(row["year"] == str(year) and int(row["bar_rows"]) > 0
                             for row in coverage)] if role == "baostock_archive" else []
    summary = {"schema_version": metadata, "groups": groups,
               "year_exchange": coverage, "status_fields": status,
               "catalogue": catalogue, "window_counts": window_counts,
               "latest_run": latest}
    return {
        "status": "ready", "path": str(db), "role": role,
        "snapshot": {"method": "single_sqlite_read_transaction", "open_mode": mode,
                     "quick_check": "ok",
                     "summary_sha256": _sha256_json(summary),
                     "summary_hash_is_full_database_hash": False,
                     "file_state_before": before, "file_state_after": after,
                     "file_changed_while_reading": before != after,
                     "main_file_changed_while_reading": before["database"] != after["database"],
                     "wal_state_changed_while_reading": before["wal"] != after["wal"],
                     "file_state_change_does_not_invalidate_transaction_snapshot": True,
                     "sql_writes": False,
                     "sqlite_may_create_wal_sidecars": mode == "rw_query_only"},
        "schema_version": metadata,
        "bar_groups": groups,
        "year_exchange_coverage": coverage,
        "zero_stock_years": zero_years,
        "stock_bar_rows": stock_rows,
        "stock_instrument_count": stock_instrument_count,
        "status_fields": status,
        "catalogue": catalogue,
        "backfill_window_counts": window_counts,
        "latest_run": latest,
        "archive_complete": False if role == "baostock_archive" else None,
        "point_in_time_eligible": False,
        "interpretation": (
            "This is a moving partial archive. Zero years, catalogue gaps, and "
            "unfinished windows must be resolved before broad historical inference."
            if role == "baostock_archive" else
            "Rows are saved observations for a selected universe, not complete A-share history."
        ),
    }


def _qlib_feature_counts(index: Mapping[str, object]) -> dict[str, dict[str, object]]:
    files = index["files"]
    if not isinstance(files, dict):
        raise ValueError("Qlib index files are malformed")
    symbols_by_field: dict[str, set[str]] = defaultdict(set)
    for name in files:
        match = FEATURE_RE.fullmatch(name)
        if match:
            symbol, field = match.groups()
            symbols_by_field[field].add(symbol)
    result = {}
    for field in sorted(symbols_by_field):
        result[field] = {
            "feature_file_count": len(symbols_by_field[field]),
            "instrument_file_count": len(symbols_by_field[field]),
            "field_kind": ("source_supplied_observation" if field in QLIB_SOURCE_FIELDS
                           else "source_supplied_transform" if field in QLIB_TRANSFORM_FIELDS
                           else "unknown"),
            "valid_value_count": None,
            "unit": ("adjusted_or_unknown_source_unit" if field == "volume"
                     else "source_native_unverified" if field == "amount"
                     else "qlib_adjusted" if field in {"open", "high", "low", "close", "adjclose"}
                     else "not_assigned"),
        }
    return result


def _read_qlib(root: str | Path, manifest: str | Path,
               expected_tag: str) -> dict[str, object]:
    directory = Path(root).resolve()
    manifest_path = Path(manifest).resolve()
    index_path = directory / INDEX_NAME
    try:
        index_before = _file_state(index_path)
        index = _load_index(directory, manifest_path, expected_tag)
        raw_index = index_path.read_bytes()
        index_digest = hashlib.sha256(raw_index).hexdigest()
        with _verified_file(directory, index, "qlib_bin/calendars/day.txt") as stream:
            calendar = parse_calendar(stream.read(), "day calendar")
        with _verified_file(directory, index, "qlib_bin/instruments/all.txt") as stream:
            instruments = parse_instruments(stream.read(), "all instruments")
        index_after = _file_state(index_path)
        if index_before != index_after:
            raise ValueError("Qlib release index changed during inventory")
        feature_counts = _qlib_feature_counts(index)
        exchanges = Counter(symbol[:2] for symbol in instruments)
        first = min(span[0][0] for span in instruments.values()) if instruments else None
        last = max(span[-1][1] for span in instruments.values()) if instruments else None
        return {
            "status": "ready", "path": str(directory),
            "release": index["release"],
            "snapshot": {"method": "fixed_manifest_and_published_index",
                         "manifest_sha256": index["release"]["manifest_sha256"],
                         "index_sha256": index_digest,
                         "archive_sha256": index["release"]["archive_sha256"],
                         "full_feature_content_reverified_by_inventory": False},
            "calendar": {"first": calendar[0].isoformat(),
                         "last": calendar[-1].isoformat(), "sessions": len(calendar)},
            "instrument_intervals": {"instrument_count": len(instruments),
                                     "exchange_prefix_counts": dict(sorted(exchanges.items())),
                                     "earliest_start": first.isoformat() if first else None,
                                     "latest_end": last.isoformat() if last else None,
                                     "intervals_are_valid_bar_coverage": False},
            "features": feature_counts,
            "price_basis": "qlib_adjusted",
            "field_coverage_is_file_presence_only": True,
            "valid_value_counts_require_binary_scan": True,
            "point_in_time_eligible": False,
        }
    except (OSError, ValueError, QlibArchiveError, KeyError, TypeError) as exc:
        return {"status": "unavailable", "path": str(directory),
                "reason": f"{type(exc).__name__}: {exc}"}


def build_raw_data_inventory(
    *, main_db: str | Path,
    historical_db: str | Path,
    qlib_root: str | Path,
    qlib_manifest: str | Path,
    qlib_tag: str,
) -> dict[str, object]:
    """Summarize what is persisted, without creating a source store or fetching."""
    sources = {
        "main_market_store": _read_sqlite(main_db, role="main_store"),
        "historical_baostock_archive": _read_sqlite(historical_db,
                                                    role="baostock_archive"),
        "qlib_release": _read_qlib(qlib_root, qlib_manifest, qlib_tag),
    }
    return {
        "inventory_version": INVENTORY_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "collection_priority": [
            {"dimension": "daily_ohlcv", "reason": "Independent market observations; retain original units and price basis"},
            {"dimension": "daily_turnover_amount", "reason": "Observed traded value; cannot be exactly recovered from OHLC and volume"},
            {"dimension": "trade_and_st_status", "reason": "Tradability observations; cannot be inferred reliably from zero volume"},
            {"dimension": "corporate_actions", "reason": "Cash dividends, splits and rights issues need original event records and publication times"},
            {"dimension": "instrument_history", "reason": "Listing, delisting, name and sector membership histories need dated source records"},
        ],
        "computed_not_core_collection": ["returns", "moving_averages", "momentum",
                                         "volatility", "technical_factors", "signals"],
        "uncollected_core_fields_in_inspected_stores": [
            "source_preclose",
            "dated_corporate_action_events",
            "historical_total_and_free_float_shares",
            "raw_financial_statement_facts_with_disclosure_time",
            "dated_industry_membership_with_publication_time",
            "exchange_margin_financing_balances",
            "rule_versioned_northbound_flow_observations",
        ],
        "global_limitations": [
            "No source currently supplies verified original publication time for every observation.",
            "A stored adjusted series is not an original transaction price or original volume.",
            "The Qlib index proves feature file presence only; this inventory does not scan values.",
            "The BaoStock archive is moving and incomplete; date endpoints do not prove year or exchange coverage.",
        ],
    }


def write_inventory(path: str | Path, report: Mapping[str, object]) -> Path:
    """Atomically publish JSON outside the source stores."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(report, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, staging_name = tempfile.mkstemp(prefix=f".{target.name}.",
                                               suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging_name, target)
    finally:
        if os.path.exists(staging_name):
            os.unlink(staging_name)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory raw daily observations without scraping")
    parser.add_argument("--main-db", type=Path, default=Path("data/quant/market.sqlite3"))
    parser.add_argument("--historical-db", type=Path,
                        default=Path("data/quant/historical-baostock-raw.sqlite3"))
    parser.add_argument("--qlib-root", type=Path,
                        default=Path("data/quant/qlib-releases/2026-09-28/published"))
    parser.add_argument("--qlib-manifest", type=Path,
                        default=Path("data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json"))
    parser.add_argument("--qlib-tag", default="2026-09-28")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build_raw_data_inventory(main_db=args.main_db,
                                      historical_db=args.historical_db,
                                      qlib_root=args.qlib_root,
                                      qlib_manifest=args.qlib_manifest,
                                      qlib_tag=args.qlib_tag)
    output = write_inventory(args.out, report)
    print(json.dumps({"output": str(output.resolve()),
                      "source_statuses": {name: result["status"] for name, result
                                          in report["sources"].items()}},
                     ensure_ascii=False))
    return 0 if all(result["status"] == "ready" for result in report["sources"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
