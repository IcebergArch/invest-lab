"""Bounded, resumable SH/SZ A-share daily archive from 2000 onward.

The archive uses a *dedicated* SQLite database and raw (unadjusted) OHLC.
Current adjusted prices can be revised after corporate actions; raw bars avoid
silently rewriting an old backtest input, but still need a corporate-action
ledger before total-return or split-aware strategy backtests.

One invocation performs at most ``max_windows`` three-calendar-year requests in one
BaoStock session, with a delay between queries.  The EastMoney adapter is a
switchable fallback for raw OHLC and amount, without BaoStock ST/status fields.
Neither source's current catalogue proves historical point-in-time membership.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence, Union
from uuid import uuid4

from quant_lab.baostock_source import (
    BaoStockDailyBatch, BaoStockHistoricalListing, BaoStockHistoricalSnapshot,
    BaoStockSource, BaoStockUniverseSnapshot, SOURCE_ID,
    read_baostock_historical_snapshot, read_baostock_snapshot,
    write_baostock_historical_snapshot,
)
from quant_lab.data_sources import EastMoneyDailySource, HttpJsonClient
from quant_lab.models import DailyBar, Instrument
from quant_lab.storage import MarketStore


EARLIEST_DATE = date(2000, 1, 1)
DEFAULT_INTERVAL_SECONDS = 1.5
DEFAULT_MAX_WINDOWS = 10
HARD_MAX_WINDOWS = 50
WINDOW_YEARS = 3
BACKFILL_VERSION = "sh-sz-raw-daily-archive-v1"
SOURCE_IDS = {"baostock": SOURCE_ID, "eastmoney": "eastmoney_kline"}


class BaoBatchSource(Protocol):
    def batch_session(self): ...
    def fetch_in_session(self, instrument: Instrument, start: date,
                         end: date) -> BaoStockDailyBatch: ...


@dataclass(frozen=True)
class BackfillConfig:
    start: date
    end: date
    source: str = "baostock"
    max_windows: int = DEFAULT_MAX_WINDOWS
    min_interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    retry_failed_after_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.start < EARLIEST_DATE or self.start > self.end:
            raise ValueError("backfill date range must start on/after 2000-01-01")
        if self.end >= date.today():
            raise ValueError("end must be before today, after the daily bar is complete")
        if self.source not in SOURCE_IDS:
            raise ValueError("source must be baostock or eastmoney")
        if isinstance(self.max_windows, bool) or not 1 <= self.max_windows <= HARD_MAX_WINDOWS:
            raise ValueError(f"max_windows must be between 1 and {HARD_MAX_WINDOWS}")
        if (not math.isfinite(self.min_interval_seconds)
                or self.min_interval_seconds < 1.0):
            raise ValueError("min_interval_seconds must be at least 1.0")
        if (not math.isfinite(self.retry_failed_after_seconds)
                or self.retry_failed_after_seconds < 0):
            raise ValueError("retry_failed_after_seconds must be nonnegative")


@dataclass(frozen=True)
class BackfillWindow:
    instrument_id: str
    start: date
    end: date


@dataclass(frozen=True)
class BackfillPlan:
    snapshot_id: str
    catalogue_scope: str
    selected_stock_count: int
    outstanding_window_count: int
    deferred_failure_count: int
    windows: tuple[BackfillWindow, ...]


@dataclass(frozen=True)
class BackfillResult:
    run_id: str | None
    snapshot_id: str
    catalogue_scope: str
    source_id: str
    requested_window_count: int
    completed_window_count: int
    empty_window_count: int
    failed_window_count: int
    bar_count: int
    status_count: int
    remaining_window_count: int
    deferred_failure_count: int
    elapsed_seconds: float
    error: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "backfill_version": BACKFILL_VERSION,
            **self.__dict__,
            "historical_point_in_time_universe": False,
            "bse_included": False,
        }


Catalogue = Union[BaoStockHistoricalSnapshot, BaoStockUniverseSnapshot]


def _catalogue_parts(catalogue: Catalogue) -> tuple[str, tuple[BaoStockHistoricalListing, ...]]:
    if isinstance(catalogue, BaoStockHistoricalSnapshot):
        return "provider-active-and-inactive-sh-sz-a", catalogue.listings
    if isinstance(catalogue, BaoStockUniverseSnapshot):
        return "current-active-sh-sz-a-only", tuple(
            BaoStockHistoricalListing(item, None, None, "1") for item in catalogue.instruments
        )
    raise TypeError("catalogue must be a validated BaoStock snapshot")


def _instrument(listing: BaoStockHistoricalListing, source: str) -> Instrument:
    item = listing.instrument
    if source == "baostock":
        return replace(item, adjustment="none",
                       family="historical_stock" if listing.provider_status == "0" else "current_stock")
    exchange, code = item.provider_code.split(".", 1)
    market = "1" if exchange == "sh" else "0"
    return replace(item, source_id="eastmoney_kline", provider_code=f"{market}.{code}",
                   adjustment="none",
                   family="historical_stock" if listing.provider_status == "0" else "current_stock")


def _initialize_archive(store: MarketStore, catalogue: Catalogue, source: str) -> None:
    """Reject any existing main/pilot market series before writing the archive."""
    store.initialize()
    source_id = SOURCE_IDS[source]
    with store.connect() as connection:
        incompatible = connection.execute(
            "SELECT 1 FROM daily_bars WHERE source_id!=? OR adjustment!='none' LIMIT 1",
            (source_id,),
        ).fetchone()
        if incompatible:
            raise ValueError("archive DB already has a different provider or adjusted bars; use a dedicated DB")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS backfill_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                provider_version TEXT NOT NULL,
                raw_rows_hash TEXT NOT NULL,
                listing_count INTEGER NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backfill_listings (
                snapshot_id TEXT NOT NULL REFERENCES backfill_snapshots(snapshot_id),
                instrument_id TEXT NOT NULL,
                provider_code TEXT NOT NULL,
                name TEXT NOT NULL,
                ipo_date TEXT,
                out_date TEXT,
                provider_status TEXT NOT NULL,
                PRIMARY KEY(snapshot_id,instrument_id)
            );
            CREATE TABLE IF NOT EXISTS backfill_windows (
                source_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                adjustment TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                snapshot_id TEXT NOT NULL REFERENCES backfill_snapshots(snapshot_id),
                status TEXT NOT NULL CHECK(status IN ('success','empty','failed')),
                attempts INTEGER NOT NULL,
                run_id TEXT NOT NULL REFERENCES sync_runs(run_id),
                bar_count INTEGER NOT NULL,
                status_count INTEGER NOT NULL,
                first_bar_date TEXT,
                last_bar_date TEXT,
                last_error TEXT,
                checked_at TEXT NOT NULL,
                PRIMARY KEY(source_id,instrument_id,adjustment,window_start,window_end)
            );
            CREATE INDEX IF NOT EXISTS idx_backfill_windows_status
                ON backfill_windows(source_id,status,checked_at);
            CREATE TABLE IF NOT EXISTS baostock_daily_status (
                instrument_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                tradestatus INTEGER NOT NULL,
                is_st INTEGER NOT NULL,
                raw_amount_cny REAL,
                has_valid_bar INTEGER NOT NULL,
                payload_hash TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES sync_runs(run_id),
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (instrument_id,trade_date)
            );
        """)
        scope, listings = _catalogue_parts(catalogue)
        snapshot_id = catalogue.snapshot_id
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("""
            INSERT OR IGNORE INTO backfill_snapshots VALUES (?,?,?,?,?,?,?)
        """, (snapshot_id, scope, catalogue.collected_at,
              catalogue.provider_version, catalogue.raw_rows_hash, len(listings), now))
        connection.executemany("""
            INSERT OR IGNORE INTO backfill_listings VALUES (?,?,?,?,?,?,?)
        """, [(snapshot_id, item.instrument.instrument_id,
               item.instrument.provider_code, item.instrument.name, item.ipo_date,
               item.out_date, item.provider_status) for item in listings])


def _calendar_windows(listing: BaoStockHistoricalListing,
                      start: date, end: date) -> list[BackfillWindow]:
    """Three-year calendar buckets; <=784 weekdays, below the 1,000-row cap."""
    first = max(start, date.fromisoformat(listing.ipo_date)) if listing.ipo_date else start
    last = min(end, date.fromisoformat(listing.out_date)) if listing.out_date else end
    if first > last:
        return []
    first_bucket = EARLIEST_DATE.year + (
        (first.year - EARLIEST_DATE.year) // WINDOW_YEARS
    ) * WINDOW_YEARS
    return [BackfillWindow(
        listing.instrument.instrument_id,
        max(first, date(year, 1, 1)),
        min(last, date(year + WINDOW_YEARS - 1, 12, 31)),
    ) for year in range(first_bucket, last.year + 1, WINDOW_YEARS)]


def _subtract_covered(window: BackfillWindow,
                      covered: Sequence[tuple[date, date]]) -> list[BackfillWindow]:
    remaining = [(window.start, window.end)]
    for left, right in sorted(covered):
        updated = []
        for start, end in remaining:
            if right < start or left > end:
                updated.append((start, end))
            else:
                if start < left:
                    updated.append((start, left - timedelta(days=1)))
                if right < end:
                    updated.append((right + timedelta(days=1), end))
        remaining = updated
    return [BackfillWindow(window.instrument_id, start, end)
            for start, end in remaining]


def plan_backfill(
    store: MarketStore, catalogue: Catalogue, config: BackfillConfig,
    selected_ids: Sequence[str] | None = None,
    *, now: datetime | None = None,
) -> BackfillPlan:
    """Plan only uncovered year-bounded ranges; do not contact a data source."""
    _initialize_archive(store, catalogue, config.source)
    scope, all_listings = _catalogue_parts(catalogue)
    by_id = {item.instrument.instrument_id: item for item in all_listings}
    if len(by_id) != len(all_listings):
        raise ValueError("catalogue contains duplicate stock IDs")
    ids = sorted(set(selected_ids)) if selected_ids is not None else sorted(by_id)
    if not ids or any(key not in by_id for key in ids):
        raise ValueError("select one or more stock IDs from the snapshot")
    source_id = SOURCE_IDS[config.source]
    with store.connect() as connection:
        rows = connection.execute("""
            SELECT instrument_id,window_start,window_end,status,checked_at
            FROM backfill_windows WHERE source_id=? AND adjustment='none'
        """, (source_id,)).fetchall()
    covered: dict[str, list[tuple[date, date]]] = {}
    recent_failure: dict[tuple[str, date, date], datetime] = {}
    for row in rows:
        key = row["instrument_id"]
        left = date.fromisoformat(row["window_start"])
        right = date.fromisoformat(row["window_end"])
        if row["status"] in ("success", "empty"):
            covered.setdefault(key, []).append((left, right))
        elif row["status"] == "failed":
            recent_failure[(key, left, right)] = datetime.fromisoformat(row["checked_at"])
    now = now or datetime.now(timezone.utc)
    outstanding: list[BackfillWindow] = []
    deferred = 0
    for key in ids:
        for calendar_bucket in _calendar_windows(by_id[key], config.start, config.end):
            for missing in _subtract_covered(calendar_bucket, covered.get(key, [])):
                failed_at = recent_failure.get((key, missing.start, missing.end))
                if failed_at and (now - failed_at).total_seconds() < config.retry_failed_after_seconds:
                    deferred += 1
                else:
                    outstanding.append(missing)
    # Recent observations are useful to the first report; older history still
    # progresses deterministically on subsequent bounded runs.
    outstanding.sort(key=lambda item: (-item.end.toordinal(), item.instrument_id,
                                       item.start.toordinal()))
    return BackfillPlan(
        catalogue.snapshot_id, scope, len(ids), len(outstanding), deferred,
        tuple(outstanding[:config.max_windows]),
    )


def _validate_batch(batch: BaoStockDailyBatch, window: BackfillWindow) -> None:
    if len(batch.statuses) > 1000 or len(batch.bars) > 1000:
        raise ValueError("provider interval exceeded the 1,000-row safety cap")
    statuses = {item.trade_date: item for item in batch.statuses}
    if len(statuses) != len(batch.statuses):
        raise ValueError("provider returned duplicate status dates")
    for day, item in statuses.items():
        if item.instrument_id != window.instrument_id or not window.start <= day <= window.end:
            raise ValueError("provider status outside requested stock/window")
    seen: set[date] = set()
    for bar in batch.bars:
        if (bar.instrument_id != window.instrument_id or bar.trade_date in seen
                or not window.start <= bar.trade_date <= window.end
                or bar.adjustment != "none" or bar.source_id != SOURCE_ID
                or bar.trade_date not in statuses
                or bar.payload_hash != statuses[bar.trade_date].payload_hash):
            raise ValueError("provider bar/status mismatch")
        seen.add(bar.trade_date)
        bar.validate()


def _record_window(
    store: MarketStore, catalogue: Catalogue, source: str, window: BackfillWindow,
    run_id: str, bars: Sequence[DailyBar], statuses: Sequence[object],
    status: str, error: str | None = None,
) -> None:
    """Commit bars, trading states and checkpoint in one SQLite transaction."""
    now = datetime.now(timezone.utc).isoformat()
    source_id = SOURCE_IDS[source]
    with store.connect() as connection:
        for bar in bars:
            connection.execute("""
                INSERT INTO daily_bars(
                    instrument_id,trade_date,open,high,low,close,volume,amount,
                    volume_unit,amount_unit,adjustment,source_id,payload_hash,run_id,fetched_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instrument_id,trade_date,adjustment) DO UPDATE SET
                    open=excluded.open,high=excluded.high,low=excluded.low,
                    close=excluded.close,volume=excluded.volume,amount=excluded.amount,
                    volume_unit=excluded.volume_unit,amount_unit=excluded.amount_unit,
                    source_id=excluded.source_id,payload_hash=excluded.payload_hash,
                    run_id=excluded.run_id,fetched_at=excluded.fetched_at
            """, (bar.instrument_id, bar.trade_date.isoformat(), bar.open, bar.high,
                  bar.low, bar.close, bar.volume, bar.amount, bar.volume_unit,
                  bar.amount_unit, bar.adjustment, bar.source_id, bar.payload_hash,
                  run_id, now))
        if source == "baostock":
            for item in statuses:
                connection.execute("""
                    INSERT INTO baostock_daily_status(
                        instrument_id,trade_date,tradestatus,is_st,raw_amount_cny,
                        has_valid_bar,payload_hash,run_id,fetched_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(instrument_id,trade_date) DO UPDATE SET
                        tradestatus=excluded.tradestatus,is_st=excluded.is_st,
                        raw_amount_cny=excluded.raw_amount_cny,
                        has_valid_bar=excluded.has_valid_bar,
                        payload_hash=excluded.payload_hash,run_id=excluded.run_id,
                        fetched_at=excluded.fetched_at
                """, (item.instrument_id, item.trade_date.isoformat(),
                      item.tradestatus, item.is_st, item.raw_amount_cny,
                      int(item.has_valid_bar), item.payload_hash, run_id, now))
        previous = connection.execute("""
            SELECT attempts FROM backfill_windows WHERE source_id=? AND instrument_id=?
              AND adjustment='none' AND window_start=? AND window_end=?
        """, (source_id, window.instrument_id, window.start.isoformat(),
              window.end.isoformat())).fetchone()
        connection.execute("""
            INSERT INTO backfill_windows(
                source_id,instrument_id,adjustment,window_start,window_end,snapshot_id,
                status,attempts,run_id,bar_count,status_count,first_bar_date,last_bar_date,
                last_error,checked_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id,instrument_id,adjustment,window_start,window_end)
            DO UPDATE SET snapshot_id=excluded.snapshot_id,status=excluded.status,
                attempts=excluded.attempts,run_id=excluded.run_id,
                bar_count=excluded.bar_count,status_count=excluded.status_count,
                first_bar_date=excluded.first_bar_date,last_bar_date=excluded.last_bar_date,
                last_error=excluded.last_error,checked_at=excluded.checked_at
        """, (source_id, window.instrument_id, "none", window.start.isoformat(),
              window.end.isoformat(), catalogue.snapshot_id, status,
              (previous["attempts"] if previous else 0) + 1, run_id,
              len(bars), len(statuses),
              min((bar.trade_date.isoformat() for bar in bars), default=None),
              max((bar.trade_date.isoformat() for bar in bars), default=None),
              error, now))


def _eastmoney_fetch(
    source: EastMoneyDailySource, instrument: Instrument,
    window: BackfillWindow,
) -> tuple[DailyBar, ...]:
    bars = tuple(source.fetch(instrument, window.start, window.end))
    if len(bars) > 1000 or len({bar.trade_date for bar in bars}) != len(bars):
        raise ValueError("EastMoney interval exceeded cap or returned duplicate dates")
    if any(bar.instrument_id != window.instrument_id
           or bar.source_id != "eastmoney_kline" or bar.adjustment != "none"
           or not window.start <= bar.trade_date <= window.end
           or bar.amount is None or bar.amount_unit != "CNY"
           for bar in bars):
        raise ValueError("EastMoney bar or amount is inconsistent")
    return bars


def run_backfill(
    store: MarketStore, catalogue: Catalogue, config: BackfillConfig,
    selected_ids: Sequence[str] | None = None,
    *, bao_source: BaoBatchSource | None = None,
    eastmoney_source: EastMoneyDailySource | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> BackfillResult:
    """Run one bounded batch; stop on the first source error to avoid hammering."""
    plan = plan_backfill(store, catalogue, config, selected_ids)
    source_id = SOURCE_IDS[config.source]
    if not plan.windows:
        return BackfillResult(None, catalogue.snapshot_id, plan.catalogue_scope,
                              source_id, 0, 0, 0, 0, 0, 0,
                              plan.outstanding_window_count + plan.deferred_failure_count,
                              plan.deferred_failure_count, 0.0, None)
    _, listings = _catalogue_parts(catalogue)
    by_id = {item.instrument.instrument_id: item for item in listings}
    instruments = {_id: _instrument(by_id[_id], config.source)
                   for _id in {window.instrument_id for window in plan.windows}}
    store.upsert_instruments(instruments.values())
    run_id = uuid4().hex
    store.start_run(run_id, config.start, config.end,
                    (f"backfill:{config.source}", f"snapshot:{catalogue.snapshot_id}",
                     f"version:{BACKFILL_VERSION}"))
    completed = empty = failed = bars_total = statuses_total = 0
    started = monotonic()
    last_request: float | None = None
    error: str | None = None

    def execute(fetch_bao: Callable[[Instrument, date, date], BaoStockDailyBatch] | None) -> None:
        nonlocal completed, empty, failed, bars_total, statuses_total, last_request, error
        for window in plan.windows:
            if last_request is not None:
                wait = config.min_interval_seconds - (monotonic() - last_request)
                if wait > 0:
                    sleep(wait)
            last_request = monotonic()
            try:
                instrument = instruments[window.instrument_id]
                if config.source == "baostock":
                    assert fetch_bao is not None
                    batch = fetch_bao(instrument, window.start, window.end)
                    _validate_batch(batch, window)
                    bars = batch.bars
                    statuses = batch.statuses
                else:
                    assert eastmoney_source is not None
                    bars = _eastmoney_fetch(eastmoney_source, instrument, window)
                    statuses = ()
                state = "success" if bars or statuses else "empty"
                _record_window(store, catalogue, config.source, window, run_id,
                               bars, statuses, state)
                if state == "success":
                    completed += 1
                else:
                    empty += 1
                bars_total += len(bars)
                statuses_total += len(statuses)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                _record_window(store, catalogue, config.source, window, run_id,
                               (), (), "failed", error)
                failed += 1
                break

    try:
        if config.source == "baostock":
            bao_source = bao_source or BaoStockSource()
            with bao_source.batch_session() as session:
                execute(session.fetch_in_session)
        else:
            eastmoney_source = eastmoney_source or EastMoneyDailySource(
                HttpJsonClient(timeout=12.0, retries=1)
            )
            execute(None)
    except Exception as exc:
        # A failed login happens before any window request.  The run itself is
        # retained, while no window is falsely marked complete.
        error = f"{type(exc).__name__}: {exc}"
        failed += 1
    finally:
        store.finish_run(run_id, "failed" if failed and not completed else
                         "partial" if failed else "success",
                         len(instruments), bars_total, error)
    remaining = plan.outstanding_window_count + plan.deferred_failure_count - completed - empty
    return BackfillResult(
        run_id, catalogue.snapshot_id, plan.catalogue_scope, source_id, len(plan.windows),
        completed, empty, failed, bars_total, statuses_total,
        max(0, remaining), plan.deferred_failure_count, monotonic() - started, error,
    )


def _load_catalogue(path: str | Path, *, allow_current_only: bool) -> Catalogue:
    try:
        return read_baostock_historical_snapshot(path)
    except ValueError:
        if not allow_current_only:
            raise
    return read_baostock_snapshot(path)


def archive_status(path: str | Path) -> dict[str, object]:
    """Read one consistent SQLite snapshot without initializing or writing a DB."""
    db_path = Path(path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"archive DB does not exist: {db_path}")
    # WAL databases without -shm/-wal sidecars cannot be opened with mode=ro.
    # A local writable handle permits SQLite to create those sidecars; all
    # statements on this connection remain read-only after query_only is set.
    connection = sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        required = {"backfill_snapshots", "backfill_windows", "daily_bars", "sync_runs"}
        if not required <= tables:
            raise ValueError("archive DB is not initialized by quant_lab.baostock_backfill")
        latest = connection.execute("""
            SELECT run_id,status,started_at,finished_at,error,groups_json
            FROM sync_runs WHERE groups_json LIKE '%backfill:%'
            ORDER BY started_at DESC LIMIT 1
        """).fetchone()
        run = dict(latest) if latest else None
        snapshot_id = None
        if run:
            for group in json.loads(run.pop("groups_json")):
                if group.startswith("snapshot:"):
                    snapshot_id = group.split(":", 1)[1]
                    break
        if snapshot_id:
            catalogue = connection.execute("""
                SELECT snapshot_id,scope,collected_at,provider_version,listing_count
                FROM backfill_snapshots WHERE snapshot_id=?
            """, (snapshot_id,)).fetchone()
        else:
            catalogue = connection.execute("""
                SELECT snapshot_id,scope,collected_at,provider_version,listing_count
                FROM backfill_snapshots ORDER BY recorded_at DESC LIMIT 1
            """).fetchone()
        bars = [dict(row) for row in connection.execute("""
            SELECT source_id,COUNT(DISTINCT instrument_id) AS stock_count,
                   COUNT(*) AS bar_count,MIN(trade_date) AS first_date,
                   MAX(trade_date) AS last_date
            FROM daily_bars GROUP BY source_id ORDER BY source_id
        """)]
        windows = {row["status"]: {"count": row["windows"], "bars": row["bars"]}
                   for row in connection.execute("""
                       SELECT status,COUNT(*) AS windows,COALESCE(SUM(bar_count),0) AS bars
                       FROM backfill_windows GROUP BY status
                   """)}
        return {
            "db": str(db_path),
            "catalogue": dict(catalogue) if catalogue else None,
            "bars_by_source": bars,
            "windows": {key: windows.get(key, {"count": 0, "bars": 0})
                        for key in ("success", "empty", "failed")},
            "latest_run": run,
            "historical_point_in_time_universe": False,
            "bse_included": False,
        }
    finally:
        connection.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Bounded SH/SZ A-share raw daily archive")
    commands = root.add_subparsers(dest="command", required=True)
    universe = commands.add_parser("universe", help="fetch active and inactive SH/SZ catalogue")
    universe.add_argument("--out", required=True)
    status = commands.add_parser("status", help="read-only archive coverage and latest run")
    status.add_argument("--db", required=True)
    for name in ("plan", "run"):
        command = commands.add_parser(name)
        command.add_argument("--snapshot", required=True)
        command.add_argument("--db", required=True)
        command.add_argument("--start", type=date.fromisoformat, default=EARLIEST_DATE)
        command.add_argument("--end", type=date.fromisoformat, required=True)
        command.add_argument("--source", choices=sorted(SOURCE_IDS), default="baostock")
        command.add_argument("--max-windows", type=int, default=DEFAULT_MAX_WINDOWS)
        command.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
        command.add_argument("--retry-failed-after-seconds", type=float, default=3600.0)
        command.add_argument("--stock", action="append")
        command.add_argument("--allow-current-only", action="store_true")
        if name == "run":
            command.add_argument("--result-json", help="atomically publish the batch result JSON")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(archive_status(args.db), ensure_ascii=False, indent=2))
            return 0
        if args.command == "universe":
            snapshot = BaoStockSource().list_historical_coverage()
            path = write_baostock_historical_snapshot(snapshot, args.out)
            print(json.dumps({"snapshot": str(path.resolve()), "snapshot_id": snapshot.snapshot_id,
                              "active": sum(x.provider_status == "1" for x in snapshot.listings),
                              "inactive": sum(x.provider_status == "0" for x in snapshot.listings),
                              "bse_excluded": snapshot.bse_excluded_count}, ensure_ascii=False))
            return 0
        catalogue = _load_catalogue(args.snapshot, allow_current_only=args.allow_current_only)
        config = BackfillConfig(args.start, args.end, args.source,
                                args.max_windows, args.interval_seconds,
                                args.retry_failed_after_seconds)
        store = MarketStore(args.db)
        if args.command == "plan":
            plan = plan_backfill(store, catalogue, config, args.stock)
            print(json.dumps({**plan.__dict__,
                              "windows": [{"instrument_id": item.instrument_id,
                                           "start": item.start.isoformat(),
                                           "end": item.end.isoformat()}
                                          for item in plan.windows]}, ensure_ascii=False, indent=2))
            return 0
        result = run_backfill(store, catalogue, config, args.stock)
        result_payload = result.to_dict()
        if args.result_json:
            target = Path(args.result_json)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(result_payload, ensure_ascii=False,
                                                allow_nan=False) + "\n", encoding="utf-8")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        print(json.dumps(result_payload, ensure_ascii=False, indent=2))
        return 0 if result.error is None else 2
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"backfill failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
