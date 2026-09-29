"""Read-only, streaming bridge from provider stores to a daily-bar research contract.

Extraction, normalization and panel construction are separate stages.  This
module does not rewrite the provider databases or claim that a later download
was available to a strategy at an earlier historical decision time.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Protocol, Sequence


CONTRACT_VERSION = "canonical-daily-bar-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VOLUME_UNITS = {"share", "lot", "source_native", "unavailable", "ineligible"}
_AMOUNT_UNITS = {"CNY", "source_native", "unavailable", "ineligible"}


@dataclass(frozen=True)
class CanonicalDailyBar:
    """One observed daily bar with explicit price and source semantics.

    ``observed_at`` is the saved fetch time, not the first public release time.
    ``source_published_at`` remains unknown for current adapters.  Historical
    point-in-time claims therefore require a separate verified publication log.
    """

    instrument_id: str
    asset_type: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    price_basis: str
    price_unit: str
    source_volume: float
    source_volume_unit: str
    volume_shares: float | None
    source_amount: float | None
    source_amount_unit: str
    amount_cny: float | None
    source_id: str
    run_id: str | None
    payload_hash: str
    observed_at: datetime
    source_published_at: datetime | None
    dataset_id: str
    trade_status: int | None = None
    is_st: int | None = None
    raw_amount_cny: float | None = None

    @property
    def tradability(self) -> str:
        if self.trade_status is None or self.is_st is None:
            return "unknown"
        return "tradable" if self.trade_status == 1 and self.is_st == 0 else "ineligible"

    @property
    def point_in_time_eligible(self) -> bool:
        # No current adapter has verified original publication timestamps and
        # immutable historical membership/adjustment snapshots.
        return False

    def validate(self) -> None:
        if not self.instrument_id or not self.source_id or not self.dataset_id:
            raise ValueError("instrument, source and dataset identities are required")
        if self.asset_type not in {"stock", "index", "industry_index"}:
            raise ValueError(f"unsupported asset type: {self.asset_type}")
        allowed_basis = ({"raw_unadjusted", "forward_adjusted"}
                         if self.asset_type == "stock" else {"index_points"})
        if self.price_basis not in allowed_basis:
            raise ValueError("price basis is incompatible with the asset type")
        expected_price_unit = "CNY/share" if self.asset_type == "stock" else "index_point"
        if self.price_unit != expected_price_unit:
            raise ValueError("price unit is incompatible with the asset type")
        prices = (self.open, self.high, self.low, self.close)
        if (any(isinstance(value, bool) or not math.isfinite(value) or value <= 0
                for value in prices)
                or self.low > min(self.open, self.close)
                or self.high < max(self.open, self.close)
                or self.low > self.high):
            raise ValueError(f"{self.instrument_id}: invalid finite positive OHLC")
        if self.source_volume_unit not in _VOLUME_UNITS:
            raise ValueError(f"unknown volume unit: {self.source_volume_unit}")
        if self.source_amount_unit not in _AMOUNT_UNITS:
            raise ValueError(f"unknown amount unit: {self.source_amount_unit}")
        for name in ("source_volume", "volume_shares", "source_amount",
                     "amount_cny", "raw_amount_cny"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not math.isfinite(value)
                                      or value < 0):
                raise ValueError(f"{self.instrument_id}: invalid {name}")
        if self.volume_shares is not None and self.asset_type != "stock":
            raise ValueError("index volume cannot be labelled as stock shares")
        expected_shares = (self.source_volume if self.source_volume_unit == "share"
                           else self.source_volume * 100 if self.source_volume_unit == "lot"
                           else None) if self.asset_type == "stock" else None
        if self.volume_shares != expected_shares:
            raise ValueError("normalized share volume disagrees with source volume")
        if self.source_amount_unit != "CNY" and self.amount_cny is not None:
            raise ValueError("unknown amount units cannot be converted to CNY")
        if (self.source_amount_unit == "CNY" and self.tradability != "ineligible"
                and self.amount_cny != self.source_amount):
            raise ValueError("normalized CNY amount disagrees with source amount")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must have a timezone")
        if self.source_published_at is not None and (
            self.source_published_at.tzinfo is None
            or self.source_published_at.utcoffset() is None
        ):
            raise ValueError("source_published_at must have a timezone")
        if not _SHA256.fullmatch(self.payload_hash):
            raise ValueError("payload_hash must be lowercase SHA-256")
        if (self.trade_status is None) != (self.is_st is None):
            raise ValueError("trade and ST statuses must be present together")
        if self.trade_status is not None and (
            self.trade_status not in (0, 1) or self.is_st not in (0, 1)
        ):
            raise ValueError("invalid BaoStock trade or ST status")
        if self.tradability == "ineligible" and self.amount_cny is not None:
            raise ValueError("ineligible bar cannot expose tradable CNY amount")


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must have a timezone")
    return parsed.astimezone(timezone.utc)


def _price_basis(asset_type: str, adjustment: str) -> tuple[str, str]:
    if asset_type == "stock":
        if adjustment == "none":
            return "raw_unadjusted", "CNY/share"
        if adjustment == "qfq":
            return "forward_adjusted", "CNY/share"
    elif asset_type in {"index", "industry_index"} and adjustment == "none":
        return "index_points", "index_point"
    raise ValueError(f"unsupported asset/adjustment pair: {asset_type}/{adjustment}")


def _row_get(row: Mapping[str, object], key: str) -> object:
    # sqlite3.Row has key lookup but does not implement Mapping.get().
    try:
        return row[key]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"missing required source field: {key}") from exc


def normalize_market_row(row: Mapping[str, object], *, dataset_id: str = "market_store"
                         ) -> CanonicalDailyBar:
    """Transform a ``daily_bars JOIN instruments`` row without guessing units."""
    asset_type = str(_row_get(row, "asset_type"))
    basis, price_unit = _price_basis(asset_type, str(_row_get(row, "adjustment")))
    volume_unit = str(_row_get(row, "volume_unit"))
    amount_unit = str(_row_get(row, "amount_unit"))
    if volume_unit not in _VOLUME_UNITS or amount_unit not in _AMOUNT_UNITS:
        raise ValueError("source supplied an unknown volume or amount unit")
    volume = float(_row_get(row, "volume"))
    source_amount = _row_get(row, "amount")
    amount = float(source_amount) if source_amount is not None else None
    volume_shares = (volume if volume_unit == "share" else volume * 100
                     if volume_unit == "lot" else None) if asset_type == "stock" else None
    result = CanonicalDailyBar(
        instrument_id=str(_row_get(row, "instrument_id")),
        asset_type=asset_type,
        trade_date=date.fromisoformat(str(_row_get(row, "trade_date"))),
        open=float(_row_get(row, "open")),
        high=float(_row_get(row, "high")),
        low=float(_row_get(row, "low")),
        close=float(_row_get(row, "close")),
        price_basis=basis,
        price_unit=price_unit,
        source_volume=volume,
        source_volume_unit=volume_unit,
        volume_shares=volume_shares,
        source_amount=amount,
        source_amount_unit=amount_unit,
        amount_cny=amount if amount_unit == "CNY" else None,
        source_id=str(_row_get(row, "source_id")),
        run_id=(str(_row_get(row, "run_id")) if _row_get(row, "run_id") is not None
                else None),
        payload_hash=str(_row_get(row, "payload_hash")),
        observed_at=_utc(_row_get(row, "fetched_at"), "fetched_at"),
        source_published_at=None,
        dataset_id=dataset_id,
    )
    result.validate()
    return result


def normalize_baostock_archive_row(row: Mapping[str, object], *,
                                   dataset_id: str = "baostock_raw_archive"
                                   ) -> CanonicalDailyBar:
    """Normalize a raw archive bar only when its BaoStock status row agrees."""
    if str(_row_get(row, "source_id")) != "baostock_daily":
        raise ValueError("BaoStock adapter requires baostock_daily source")
    if str(_row_get(row, "adjustment")) != "none":
        raise ValueError("BaoStock archive must contain unadjusted OHLC")
    if _row_get(row, "status_payload_hash") != _row_get(row, "payload_hash"):
        raise ValueError("BaoStock bar/status payload hashes differ")
    if _row_get(row, "status_run_id") != _row_get(row, "run_id"):
        raise ValueError("BaoStock bar/status run IDs differ")
    if _row_get(row, "status_trade_date") != _row_get(row, "trade_date"):
        raise ValueError("BaoStock bar/status dates differ")
    if _row_get(row, "status_instrument_id") != _row_get(row, "instrument_id"):
        raise ValueError("BaoStock bar/status instruments differ")
    if int(_row_get(row, "has_valid_bar")) != 1:
        raise ValueError("BaoStock status does not confirm a valid bar")
    base = normalize_market_row(row, dataset_id=dataset_id)
    status = int(_row_get(row, "tradestatus"))
    is_st = int(_row_get(row, "is_st"))
    raw_amount = _row_get(row, "raw_amount_cny")
    result = CanonicalDailyBar(
        **{**base.__dict__,
           "trade_status": status,
           "is_st": is_st,
           "raw_amount_cny": float(raw_amount) if raw_amount is not None else None,
           "amount_cny": base.amount_cny if status == 1 and is_st == 0 else None}
    )
    result.validate()
    if result.asset_type != "stock":
        raise ValueError("BaoStock raw archive contains a non-stock instrument")
    return result


class DailyBarAdapter(Protocol):
    def iter_bars(self, *, instrument_ids: Sequence[str] | None = None,
                  start: date | None = None, end: date | None = None,
                  batch_size: int = 1000) -> Iterator[CanonicalDailyBar]: ...


def _filters(instrument_ids: Sequence[str] | None, start: date | None,
             end: date | None, batch_size: int) -> tuple[str, list[object]]:
    if isinstance(batch_size, bool) or not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    if start and end and start > end:
        raise ValueError("start must be on or before end")
    clauses: list[str] = []
    params: list[object] = []
    if instrument_ids is not None:
        ids = sorted(set(instrument_ids))
        if not ids:
            raise ValueError("instrument_ids cannot be empty")
        if len(ids) > 1000:
            raise ValueError("select at most 1000 instruments per stream")
        clauses.append("b.instrument_id IN (" + ",".join("?" for _ in ids) + ")")
        params.extend(ids)
    if start:
        clauses.append("b.trade_date>=?")
        params.append(start.isoformat())
    if end:
        clauses.append("b.trade_date<=?")
        params.append(end.isoformat())
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


class _SQLiteAdapter:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _rows(self, sql: str, params: Sequence[object], batch_size: int
              ) -> Iterator[sqlite3.Row]:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        uri = self.path.resolve().as_uri()

        def open_snapshot(mode: str) -> tuple[sqlite3.Connection, sqlite3.Cursor]:
            connection = sqlite3.connect(uri + f"?mode={mode}", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                return connection, connection.execute(sql, params)
            except Exception:
                connection.close()
                raise

        # Prefer a genuinely read-only handle, which works on Docker's ro
        # main-data mount.  A WAL archive can lack -wal/-shm sidecars between
        # writer sessions; SQLite then needs a writable handle to recreate
        # those files.  query_only is set before any business-table query.
        try:
            connection, cursor = open_snapshot("ro")
        except sqlite3.OperationalError as exc:
            if "unable to open database file" not in str(exc):
                raise
            connection, cursor = open_snapshot("rw")
        try:
            while True:
                batch = cursor.fetchmany(batch_size)
                if not batch:
                    return
                yield from batch
        finally:
            connection.close()


class MarketStoreAdapter(_SQLiteAdapter):
    """Stream the existing main MarketStore without changing its schema."""

    def iter_bars(self, *, instrument_ids: Sequence[str] | None = None,
                  start: date | None = None, end: date | None = None,
                  batch_size: int = 1000) -> Iterator[CanonicalDailyBar]:
        where, params = _filters(instrument_ids, start, end, batch_size)
        sql = (
            "SELECT b.*,i.asset_type FROM daily_bars b "
            "JOIN instruments i ON i.instrument_id=b.instrument_id"
            + where + " ORDER BY b.instrument_id,b.trade_date,b.adjustment"
        )
        for row in self._rows(sql, params, batch_size):
            yield normalize_market_row(row)


class BaoStockArchiveAdapter(_SQLiteAdapter):
    """Stream raw BaoStock archive bars joined to their trade/ST evidence."""

    def iter_bars(self, *, instrument_ids: Sequence[str] | None = None,
                  start: date | None = None, end: date | None = None,
                  batch_size: int = 1000) -> Iterator[CanonicalDailyBar]:
        where, params = _filters(instrument_ids, start, end, batch_size)
        where += " AND b.source_id='baostock_daily'" if where else " WHERE b.source_id='baostock_daily'"
        sql = (
            "SELECT b.*,i.asset_type,s.instrument_id AS status_instrument_id,"
            "s.trade_date AS status_trade_date,s.tradestatus,s.is_st,"
            "s.raw_amount_cny,s.has_valid_bar,s.payload_hash AS status_payload_hash,"
            "s.run_id AS status_run_id FROM daily_bars b "
            "JOIN instruments i ON i.instrument_id=b.instrument_id "
            "LEFT JOIN baostock_daily_status s ON s.instrument_id=b.instrument_id "
            "AND s.trade_date=b.trade_date"
            + where + " ORDER BY b.instrument_id,b.trade_date"
        )
        for row in self._rows(sql, params, batch_size):
            yield normalize_baostock_archive_row(row)


@dataclass(frozen=True)
class CanonicalClosePanel:
    dates: tuple[date, ...]
    closes: Mapping[str, tuple[float, ...]]
    price_basis: str
    price_unit: str
    input_fingerprint_sha256: str


def build_close_panel(bars: Iterable[CanonicalDailyBar], *,
                      expected_price_basis: str) -> CanonicalClosePanel:
    """Build one same-basis common-date panel; reject duplicate/conflicting bars.

    This is an observational panel for research, not proof that each historical
    row was known at its trade date.  Downstream backtests must keep that limit.
    """
    by_instrument: dict[str, dict[date, CanonicalDailyBar]] = {}
    price_unit: str | None = None
    for bar in bars:
        bar.validate()
        if bar.price_basis != expected_price_basis:
            raise ValueError("cannot mix raw, forward-adjusted or index prices")
        if price_unit is None:
            price_unit = bar.price_unit
        elif bar.price_unit != price_unit:
            raise ValueError("cannot mix stock prices with index points")
        series = by_instrument.setdefault(bar.instrument_id, {})
        if bar.trade_date in series:
            raise ValueError(f"duplicate instrument/date: {bar.instrument_id}/{bar.trade_date}")
        series[bar.trade_date] = bar
    if not by_instrument:
        raise ValueError("panel requires at least one bar")
    common = sorted(set.intersection(*(set(rows) for rows in by_instrument.values())))
    if not common:
        raise ValueError("instruments have no common priced dates")
    closes = {key: tuple(rows[day].close for day in common)
              for key, rows in sorted(by_instrument.items())}
    digest = hashlib.sha256()
    digest.update(CONTRACT_VERSION.encode("ascii") + b"\n")
    for key, rows in sorted(by_instrument.items()):
        for day in common:
            bar = rows[day]
            digest.update(json.dumps(
                [key, day.isoformat(), bar.close, bar.price_basis, bar.source_id,
                 bar.run_id, bar.payload_hash, bar.observed_at.isoformat()],
                ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8") + b"\n")
    return CanonicalClosePanel(tuple(common), closes, expected_price_basis,
                               price_unit or "", digest.hexdigest())
