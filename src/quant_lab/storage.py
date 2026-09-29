from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional, Sequence

from quant_lab.models import DailyBar, Instrument


SCHEMA_VERSION = "3"


class MarketStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS instruments (
                    instrument_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    family TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    provider_code TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_bars (
                    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    amount REAL,
                    volume_unit TEXT NOT NULL,
                    amount_unit TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    run_id TEXT REFERENCES sync_runs(run_id),
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (instrument_id, trade_date, adjustment)
                );

                CREATE INDEX IF NOT EXISTS idx_daily_bars_date
                    ON daily_bars(trade_date);

                CREATE TABLE IF NOT EXISTS sync_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    requested_start TEXT NOT NULL,
                    requested_end TEXT NOT NULL,
                    groups_json TEXT NOT NULL,
                    instrument_count INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS screen_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    asof_date TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    metrics_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    subject_type TEXT NOT NULL DEFAULT 'group',
                    instrument_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_decisions (
                    decision_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL UNIQUE REFERENCES screen_candidates(candidate_id),
                    choice TEXT NOT NULL CHECK(choice IN ('watch','act','skip')),
                    note TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(daily_bars)").fetchall()
            }
            if "run_id" not in columns:
                connection.execute("ALTER TABLE daily_bars ADD COLUMN run_id TEXT")
            candidate_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(screen_candidates)").fetchall()
            }
            if "subject_type" not in candidate_columns:
                connection.execute("ALTER TABLE screen_candidates ADD COLUMN subject_type TEXT NOT NULL DEFAULT 'group'")
            if "instrument_id" not in candidate_columns:
                connection.execute("ALTER TABLE screen_candidates ADD COLUMN instrument_id TEXT")
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    def upsert_instruments(self, instruments: Iterable[Instrument]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                item.instrument_id,
                item.symbol,
                item.name,
                item.asset_type,
                item.family,
                item.source_id,
                item.provider_code,
                item.exchange,
                item.adjustment,
                now,
            )
            for item in instruments
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO instruments(
                    instrument_id, symbol, name, asset_type, family, source_id,
                    provider_code, exchange, adjustment, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_id) DO UPDATE SET
                    symbol=excluded.symbol,
                    name=excluded.name,
                    asset_type=excluded.asset_type,
                    family=excluded.family,
                    source_id=excluded.source_id,
                    provider_code=excluded.provider_code,
                    exchange=excluded.exchange,
                    adjustment=excluded.adjustment,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def upsert_bars(self, bars: Iterable[DailyBar], run_id: Optional[str] = None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        materialized = list(bars)
        for bar in materialized:
            bar.validate()
        rows = [
            (
                bar.instrument_id,
                bar.trade_date.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.amount,
                bar.volume_unit,
                bar.amount_unit,
                bar.adjustment,
                bar.source_id,
                bar.payload_hash,
                run_id,
                now,
            )
            for bar in materialized
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO daily_bars(
                    instrument_id, trade_date, open, high, low, close, volume,
                    amount, volume_unit, amount_unit, adjustment, source_id,
                    payload_hash, run_id, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_id, trade_date, adjustment) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    volume_unit=excluded.volume_unit,
                    amount_unit=excluded.amount_unit,
                    source_id=excluded.source_id,
                    payload_hash=excluded.payload_hash,
                    run_id=excluded.run_id,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def start_run(
        self, run_id: str, start: date, end: date, groups: Sequence[str]
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sync_runs(
                    run_id, started_at, status, requested_start, requested_end,
                    groups_json
                ) VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (
                    run_id,
                    datetime.now(timezone.utc).isoformat(),
                    start.isoformat(),
                    end.isoformat(),
                    json.dumps(list(groups), ensure_ascii=False),
                ),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        instrument_count: int,
        row_count: int,
        error: Optional[str] = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE sync_runs
                SET finished_at=?, status=?, instrument_count=?, row_count=?, error=?
                WHERE run_id=?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    status,
                    instrument_count,
                    row_count,
                    error,
                    run_id,
                ),
            )

    def instrument_summary(self) -> list[Mapping[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.instrument_id, i.name, i.family,
                       COALESCE(GROUP_CONCAT(DISTINCT b.source_id), i.source_id) AS source_id,
                       COUNT(b.trade_date) AS rows,
                       MIN(b.trade_date) AS first_date,
                       MAX(b.trade_date) AS last_date
                FROM instruments i
                LEFT JOIN daily_bars b ON b.instrument_id = i.instrument_id
                GROUP BY i.instrument_id
                ORDER BY i.family, i.instrument_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_instrument_ids(self, family: Optional[str] = None) -> list[str]:
        sql = "SELECT instrument_id FROM instruments"
        params: tuple[object, ...] = ()
        if family:
            sql += " WHERE family=?"
            params = (family,)
        sql += " ORDER BY instrument_id"
        with self.connect() as connection:
            return [row[0] for row in connection.execute(sql, params).fetchall()]

    def list_stock_ids(self) -> list[str]:
        with self.connect() as connection:
            return [row[0] for row in connection.execute(
                "SELECT instrument_id FROM instruments WHERE asset_type='stock' ORDER BY instrument_id"
            )]

    def load_close_panel(
        self,
        instrument_ids: Sequence[str],
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> tuple[list[date], dict[str, list[float]]]:
        if not instrument_ids:
            raise ValueError("instrument_ids cannot be empty")
        placeholders = ",".join("?" for _ in instrument_ids)
        clauses = [f"instrument_id IN ({placeholders})"]
        params: list[object] = list(instrument_ids)
        if start:
            clauses.append("trade_date >= ?")
            params.append(start.isoformat())
        if end:
            clauses.append("trade_date <= ?")
            params.append(end.isoformat())
        sql = (
            "SELECT instrument_id, trade_date, close FROM daily_bars WHERE "
            + " AND ".join(clauses)
            + " ORDER BY trade_date, instrument_id"
        )
        by_instrument: dict[str, dict[date, float]] = {
            instrument_id: {} for instrument_id in instrument_ids
        }
        with self.connect() as connection:
            for row in connection.execute(sql, params):
                by_instrument[row["instrument_id"]][date.fromisoformat(row["trade_date"])] = float(
                    row["close"]
                )
        missing = [key for key, values in by_instrument.items() if not values]
        if missing:
            raise ValueError(f"no price data for: {missing}")
        common_dates = sorted(set.intersection(*(set(v) for v in by_instrument.values())))
        panel = {
            instrument_id: [by_instrument[instrument_id][day] for day in common_dates]
            for instrument_id in instrument_ids
        }
        return common_dates, panel
