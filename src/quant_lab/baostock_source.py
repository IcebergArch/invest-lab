"""Optional BaoStock source for a bounded Shanghai/Shenzhen stock pilot.

The current stock list is not a historical point-in-time universe.  BaoStock's
Python package is imported only when a real source is constructed, so the core
research package does not require pandas or BaoStock to run its existing path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence
from uuid import uuid4

from quant_lab.models import DailyBar, Instrument
from quant_lab.storage import MarketStore
from quant_lab.universe import CORE_INSTRUMENTS


SOURCE_ID = "baostock_daily"
UNIVERSE_SOURCE_ID = "baostock_stock_basic"
DAILY_FIELDS = (
    "date,code,open,high,low,close,volume,amount,adjustflag,tradestatus,isST"
)
FOCUS_IDS = {item.instrument_id for item in CORE_INSTRUMENTS if item.asset_type == "stock"}
_CODE = re.compile(r"^(sh\.6\d{5}|sz\.[03]\d{5})$")


class BaoResult(Protocol):
    error_code: str
    error_msg: str
    fields: Sequence[str]

    def next(self) -> bool: ...

    def get_row_data(self) -> Sequence[str]: ...


class BaoClient(Protocol):
    def login(self) -> Any: ...

    def logout(self) -> Any: ...

    def query_stock_basic(self) -> BaoResult: ...

    def query_history_k_data_plus(self, code: str, fields: str, **kwargs: str) -> BaoResult: ...


def _hash(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _number(value: object, field: str, *, nullable: bool = False) -> float | None:
    if nullable and value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} is not a number: {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not a number: {value!r}") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{field} must be finite and nonnegative: {value!r}")
    return parsed


def _rows(result: BaoResult, required: Sequence[str], label: str, *, limit: int
          ) -> list[dict[str, str]]:
    if str(result.error_code) != "0":
        raise RuntimeError(f"BaoStock {label} failed: {result.error_code} {result.error_msg}")
    fields = list(result.fields)
    if len(fields) != len(set(fields)) or any(field not in fields for field in required):
        raise ValueError(f"BaoStock {label} fields changed: {fields!r}")
    collected = []
    while result.next():
        data = list(result.get_row_data())
        if len(data) != len(fields):
            raise ValueError(f"BaoStock {label} row width changed")
        collected.append(dict(zip(fields, data)))
        if len(collected) > limit:
            raise ValueError(f"BaoStock {label} exceeded safe row limit {limit}")
    return collected


def _instrument(row: Mapping[str, str]) -> Instrument:
    code = row["code"]
    if not _CODE.fullmatch(code):
        raise ValueError(f"not an SH/SZ A-share code: {code!r}")
    name = row["code_name"].strip()
    if not name:
        raise ValueError(f"missing stock name: {code}")
    exchange, bare_code = code.split(".", 1)
    suffix = "SH" if exchange == "sh" else "SZ"
    instrument_id = f"stock:{bare_code}.{suffix}"
    return Instrument(
        instrument_id=instrument_id,
        symbol=f"{bare_code}.{suffix}",
        name=name,
        asset_type="stock",
        family="focus_stock" if instrument_id in FOCUS_IDS else "current_stock",
        source_id=SOURCE_ID,
        provider_code=code,
        exchange="SSE" if exchange == "sh" else "SZSE",
        adjustment="qfq",
    )


def _listing_date(value: object, label: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"BaoStock {label} is not a date: {value!r}")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"BaoStock {label} is not a date: {value!r}") from exc


@dataclass(frozen=True)
class BaoStockHistoricalListing:
    instrument: Instrument
    ipo_date: str | None
    out_date: str | None
    provider_status: str


@dataclass(frozen=True)
class BaoStockHistoricalSnapshot:
    """Current provider metadata for active and inactive SH/SZ A-share codes.

    IPO/out dates are provider metadata, not archived daily membership evidence.
    In particular a reused code or a corrected listing date needs manual audit.
    """

    collected_at: str
    provider_version: str
    source_row_count: int
    bse_excluded_count: int
    other_excluded_count: int
    listings: tuple[BaoStockHistoricalListing, ...]
    raw_rows_hash: str

    @property
    def snapshot_id(self) -> str:
        return _hash({
            "source_id": UNIVERSE_SOURCE_ID,
            "scope": "active-and-inactive-sh-sz-a",
            "source_row_count": self.source_row_count,
            "bse_excluded_count": self.bse_excluded_count,
            "other_excluded_count": self.other_excluded_count,
            "listings": [asdict(item) for item in self.listings],
            "raw_rows_hash": self.raw_rows_hash,
        })

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "source_id": UNIVERSE_SOURCE_ID,
            "scope": "active-and-inactive-sh-sz-a",
            "historical_point_in_time": False,
            "collected_at": self.collected_at,
            "provider_version": self.provider_version,
            "source_row_count": self.source_row_count,
            "listed_count": len(self.listings),
            "active_count": sum(item.provider_status == "1" for item in self.listings),
            "inactive_count": sum(item.provider_status == "0" for item in self.listings),
            "bse_excluded_count": self.bse_excluded_count,
            "other_excluded_count": self.other_excluded_count,
            "raw_rows_hash": self.raw_rows_hash,
            "listings": [asdict(item) for item in self.listings],
        }


def write_baostock_historical_snapshot(
    snapshot: BaoStockHistoricalSnapshot, output: str | Path
) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_baostock_historical_snapshot(path: str | Path) -> BaoStockHistoricalSnapshot:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("source_id") != UNIVERSE_SOURCE_ID
            or data.get("scope") != "active-and-inactive-sh-sz-a"
            or data.get("historical_point_in_time") is not False
            or not isinstance(data.get("listings"), list)):
        raise ValueError("not a BaoStock historical-coverage snapshot")
    listings = tuple(BaoStockHistoricalListing(
        instrument=Instrument(**item["instrument"]),
        ipo_date=item["ipo_date"], out_date=item["out_date"],
        provider_status=item["provider_status"],
    ) for item in data["listings"])
    ids = [item.instrument.instrument_id for item in listings]
    if (data.get("listed_count") != len(listings) or len(ids) != len(set(ids))
            or data.get("active_count") != sum(item.provider_status == "1" for item in listings)
            or data.get("inactive_count") != sum(item.provider_status == "0" for item in listings)
            or any(item.instrument.source_id != SOURCE_ID
                   or item.instrument.adjustment != "qfq"
                   or not _CODE.fullmatch(item.instrument.provider_code)
                   or item.provider_status not in ("0", "1")
                   or _listing_date(item.ipo_date, "ipoDate") != item.ipo_date
                   or _listing_date(item.out_date, "outDate") != item.out_date
                   or (item.ipo_date and item.out_date and item.out_date < item.ipo_date)
                   for item in listings)):
        raise ValueError("BaoStock historical listing metadata is inconsistent")
    snapshot = BaoStockHistoricalSnapshot(
        collected_at=str(data["collected_at"]),
        provider_version=str(data["provider_version"]),
        source_row_count=int(data["source_row_count"]),
        bse_excluded_count=int(data["bse_excluded_count"]),
        other_excluded_count=int(data["other_excluded_count"]),
        listings=listings, raw_rows_hash=str(data["raw_rows_hash"]),
    )
    if snapshot.snapshot_id != data.get("snapshot_id"):
        raise ValueError("BaoStock historical snapshot ID mismatch")
    return snapshot


@dataclass(frozen=True)
class BaoStockUniverseSnapshot:
    collected_at: str
    provider_version: str
    source_row_count: int
    excluded_non_a_count: int
    instruments: tuple[Instrument, ...]
    raw_rows_hash: str

    @property
    def snapshot_id(self) -> str:
        return _hash({
            "source_id": UNIVERSE_SOURCE_ID,
            "source_row_count": self.source_row_count,
            "excluded_non_a_count": self.excluded_non_a_count,
            "instruments": [asdict(item) for item in self.instruments],
            "raw_rows_hash": self.raw_rows_hash,
        })

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "source_id": UNIVERSE_SOURCE_ID,
            "scope": "current listed Shanghai/Shenzhen A shares; Beijing excluded",
            "historical_point_in_time": False,
            "collected_at": self.collected_at,
            "provider_version": self.provider_version,
            "source_row_count": self.source_row_count,
            "listed_count": len(self.instruments),
            "excluded_non_a_count": self.excluded_non_a_count,
            "raw_rows_hash": self.raw_rows_hash,
            "instruments": [asdict(item) for item in self.instruments],
        }


def write_baostock_snapshot(snapshot: BaoStockUniverseSnapshot, output: str | Path) -> Path:
    """Archive the exact provider list used for a pilot, atomically."""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_baostock_snapshot(path: str | Path) -> BaoStockUniverseSnapshot:
    """Reject a modified or incomplete archived list before using it."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("source_id") != UNIVERSE_SOURCE_ID
            or data.get("historical_point_in_time") is not False
            or not isinstance(data.get("instruments"), list)):
        raise ValueError("not a BaoStock current-list snapshot")
    instruments = tuple(Instrument(**record) for record in data["instruments"])
    if (data.get("listed_count") != len(instruments)
            or len({item.instrument_id for item in instruments}) != len(instruments)
            or any(item.source_id != SOURCE_ID or item.adjustment != "qfq"
                   or not _CODE.fullmatch(item.provider_code) for item in instruments)):
        raise ValueError("BaoStock snapshot listings are inconsistent")
    snapshot = BaoStockUniverseSnapshot(
        collected_at=str(data["collected_at"]),
        provider_version=str(data["provider_version"]),
        source_row_count=int(data["source_row_count"]),
        excluded_non_a_count=int(data["excluded_non_a_count"]),
        instruments=instruments,
        raw_rows_hash=str(data["raw_rows_hash"]),
    )
    if snapshot.snapshot_id != data.get("snapshot_id"):
        raise ValueError("BaoStock snapshot ID mismatch")
    return snapshot


@dataclass(frozen=True)
class BaoStockDailyStatus:
    instrument_id: str
    trade_date: date
    tradestatus: int
    is_st: int
    raw_amount_cny: float | None
    payload_hash: str
    has_valid_bar: bool

    @property
    def recommendable(self) -> bool:
        return (self.tradestatus == 1 and self.is_st == 0 and self.has_valid_bar
                and self.raw_amount_cny is not None and self.raw_amount_cny > 0)


@dataclass(frozen=True)
class BaoStockDailyBatch:
    bars: tuple[DailyBar, ...]
    statuses: tuple[BaoStockDailyStatus, ...]


def _parse_daily_row(instrument: Instrument, row: Mapping[str, str]
                     ) -> tuple[DailyBar | None, BaoStockDailyStatus]:
    if row["code"] != instrument.provider_code:
        raise ValueError(f"BaoStock daily code mismatch: {row['code']!r}")
    expected_adjustflag = {"qfq": "2", "none": "3"}.get(instrument.adjustment)
    if expected_adjustflag is None or row["adjustflag"] != expected_adjustflag:
        raise ValueError(f"BaoStock returned unexpected adjustment: {row['adjustflag']!r}")
    if row["tradestatus"] not in ("0", "1") or row["isST"] not in ("0", "1"):
        raise ValueError("BaoStock trade/ST status is missing or unknown")
    trade_date = date.fromisoformat(row["date"])
    tradestatus, is_st = int(row["tradestatus"]), int(row["isST"])
    amount = _number(row["amount"], "amount", nullable=True)
    volume = _number(row["volume"], "volume", nullable=True)
    payload_hash = _hash(dict(row))
    valid_prices = all(row[field] not in (None, "") for field in ("open", "high", "low", "close"))
    bar: DailyBar | None = None
    prices = ({field: _number(row[field], field) for field in ("open", "high", "low", "close")}
              if valid_prices else None)
    if prices is not None and all(value > 0 for value in prices.values()):
        bar = DailyBar(
            instrument_id=instrument.instrument_id,
            trade_date=trade_date,
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            volume=volume if volume is not None else 0.0,
            # A suspended or ST day cannot pass the downstream liquidity gate.
            amount=amount if tradestatus == 1 and is_st == 0 else None,
            volume_unit="share",
            amount_unit="CNY" if tradestatus == 1 and is_st == 0 else "ineligible",
            adjustment=instrument.adjustment,
            source_id=SOURCE_ID,
            payload_hash=payload_hash,
        )
        bar.validate()
    elif tradestatus == 1 and is_st == 0:
        raise ValueError(f"{instrument.instrument_id}: active day has no OHLC")
    if tradestatus == 1 and is_st == 0 and (volume is None or amount is None):
        raise ValueError(f"{instrument.instrument_id}: active day has no volume/amount")
    status = BaoStockDailyStatus(
        instrument.instrument_id, trade_date, tradestatus, is_st, amount,
        payload_hash, bar is not None,
    )
    return bar, status


class BaoStockSource:
    """BaoStock client wrapper; each public method owns its login session."""

    def __init__(self, client: BaoClient | None = None, *, provider_version: str | None = None):
        if client is None:
            try:
                import baostock as client  # type: ignore[no-redef]
                from importlib.metadata import version
                provider_version = version("baostock")
            except ImportError as exc:
                raise RuntimeError("install the optional baostock and pandas packages") from exc
        self.client = client
        self.provider_version = provider_version or "unknown"

    @contextmanager
    def _session(self) -> Iterator[None]:
        login = self.client.login()
        if str(login.error_code) != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
        try:
            yield
        finally:
            self.client.logout()

    def list_current(self, *, minimum_count: int = 4000) -> BaoStockUniverseSnapshot:
        """Filter BaoStock stock_basic strictly to type=1, status=1, SH/SZ A codes."""
        with self._session():
            raw = _rows(
                self.client.query_stock_basic(),
                ("code", "code_name", "type", "status"),
                "query_stock_basic",
                limit=15_000,
            )
        instruments: list[Instrument] = []
        excluded_non_a = 0
        seen: set[str] = set()
        for row in raw:
            if row["type"] != "1" or row["status"] != "1":
                continue
            if not _CODE.fullmatch(row["code"]):
                excluded_non_a += 1
                continue
            item = _instrument(row)
            if item.instrument_id in seen:
                raise ValueError(f"BaoStock duplicate listed stock: {item.instrument_id}")
            seen.add(item.instrument_id)
            instruments.append(item)
        if len(instruments) < minimum_count:
            raise ValueError(
                f"BaoStock returned only {len(instruments)} listed SH/SZ A stocks; "
                f"minimum is {minimum_count}"
            )
        return BaoStockUniverseSnapshot(
            collected_at=datetime.now(timezone.utc).isoformat(),
            provider_version=self.provider_version,
            source_row_count=len(raw),
            excluded_non_a_count=excluded_non_a,
            instruments=tuple(sorted(instruments, key=lambda item: item.instrument_id)),
            raw_rows_hash=_hash(raw),
        )

    def list_historical_coverage(
        self, *, minimum_count: int = 4000
    ) -> BaoStockHistoricalSnapshot:
        """Keep current and inactive SH/SZ A-share codes from stock_basic.

        This is a present-day provider catalogue with IPO/out metadata.  It is
        more complete for a long backfill than ``list_current``, but still not
        an archived daily membership table or proof of provider coverage.
        """
        with self._session():
            raw = _rows(self.client.query_stock_basic(),
                        ("code", "code_name", "ipoDate", "outDate", "type", "status"),
                        "query_stock_basic", limit=15_000)
        listings: list[BaoStockHistoricalListing] = []
        bse_excluded = other_excluded = 0
        seen: set[str] = set()
        for row in raw:
            if row["type"] != "1":
                continue
            code = row["code"]
            if not _CODE.fullmatch(code):
                if code.startswith("bj."):
                    bse_excluded += 1
                else:
                    other_excluded += 1
                continue
            status = row["status"]
            if status not in ("0", "1"):
                raise ValueError(f"BaoStock unknown listing status for {code}: {status!r}")
            ipo = _listing_date(row["ipoDate"], "ipoDate")
            out = _listing_date(row["outDate"], "outDate")
            if ipo and out and out < ipo:
                raise ValueError(f"BaoStock outDate precedes ipoDate for {code}")
            instrument = _instrument(row)
            if instrument.instrument_id in seen:
                raise ValueError(f"BaoStock duplicate historical code: {code}")
            seen.add(instrument.instrument_id)
            listings.append(BaoStockHistoricalListing(instrument, ipo, out, status))
        if len(listings) < minimum_count:
            raise ValueError(f"BaoStock historical catalogue has only {len(listings)} SH/SZ A codes")
        return BaoStockHistoricalSnapshot(
            collected_at=datetime.now(timezone.utc).isoformat(),
            provider_version=self.provider_version,
            source_row_count=len(raw),
            bse_excluded_count=bse_excluded,
            other_excluded_count=other_excluded,
            listings=tuple(sorted(listings, key=lambda item: item.instrument.instrument_id)),
            raw_rows_hash=_hash(raw),
        )

    @contextmanager
    def batch_session(self) -> Iterator["BaoStockSource"]:
        """One login for a caller-bounded sequence of throttled daily queries."""
        with self._session():
            yield self

    def fetch(self, instrument: Instrument, start: date, end: date) -> BaoStockDailyBatch:
        with self._session():
            return self.fetch_in_session(instrument, start, end)

    def fetch_in_session(self, instrument: Instrument, start: date, end: date) -> BaoStockDailyBatch:
        """Fetch one <=1,000-row interval while ``batch_session`` is active."""
        if start > end:
            raise ValueError("start date must not follow end date")
        if instrument.source_id != SOURCE_ID or instrument.adjustment not in ("qfq", "none"):
            raise ValueError("BaoStock source requires its own qfq or raw stock instrument")
        if not _CODE.fullmatch(instrument.provider_code):
            raise ValueError("BaoStock source only supports SH/SZ A shares")
        raw = _rows(
            self.client.query_history_k_data_plus(
                instrument.provider_code,
                DAILY_FIELDS,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag={"qfq": "2", "none": "3"}[instrument.adjustment],
            ),
            DAILY_FIELDS.split(","),
            "query_history_k_data_plus",
            limit=1_000,
        )
        bars: list[DailyBar] = []
        statuses: list[BaoStockDailyStatus] = []
        seen: set[date] = set()
        for row in raw:
            bar, status = _parse_daily_row(instrument, row)
            if not start <= status.trade_date <= end or status.trade_date in seen:
                raise ValueError(f"BaoStock daily date outside range or repeated: {status.trade_date}")
            seen.add(status.trade_date)
            statuses.append(status)
            if bar is not None:
                bars.append(bar)
        return BaoStockDailyBatch(
            tuple(sorted(bars, key=lambda item: item.trade_date)),
            tuple(sorted(statuses, key=lambda item: item.trade_date)),
        )


def _save_statuses(store: MarketStore, statuses: Sequence[BaoStockDailyStatus], run_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with store.connect() as connection:
        connection.execute("""
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
                PRIMARY KEY (instrument_id, trade_date)
            )
        """)
        connection.executemany("""
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
        """, [
            (item.instrument_id, item.trade_date.isoformat(), item.tradestatus,
             item.is_st, item.raw_amount_cny, int(item.has_valid_bar),
             item.payload_hash, run_id, now)
            for item in statuses
        ])


@dataclass(frozen=True)
class BaoStockPilotResult:
    run_id: str
    snapshot_id: str
    instrument_count: int
    row_count: int
    status_count: int
    recommendable_ids: tuple[str, ...]


def sync_baostock_pilot(
    store: MarketStore,
    snapshot: BaoStockUniverseSnapshot,
    selected_ids: Sequence[str],
    start: date,
    end: date,
    *,
    source: BaoStockSource | None = None,
) -> BaoStockPilotResult:
    """Sync only explicit IDs, <=30 stocks and <=550 calendar days.

    Use a dedicated pilot database.  The existing daily_bars key does not keep
    two providers' qfq versions side by side for the same stock and date.
    """
    if start > end or (end - start).days > 550:
        raise ValueError("pilot date span must be 0..550 calendar days")
    if not selected_ids or len(selected_ids) > 30 or len(set(selected_ids)) != len(selected_ids):
        raise ValueError("select 1..30 distinct SH/SZ stock IDs")
    by_id = {item.instrument_id: item for item in snapshot.instruments}
    if len(by_id) != len(snapshot.instruments) or len(by_id) < 1:
        raise ValueError("pilot requires a nonempty unique stock snapshot")
    missing = sorted(set(selected_ids) - by_id.keys())
    if missing:
        raise ValueError(f"stock IDs absent from BaoStock snapshot: {missing}")
    source = source or BaoStockSource()
    instruments = [by_id[item] for item in selected_ids]
    run_id = uuid4().hex
    store.initialize()
    store.start_run(
        run_id, start, end,
        ("baostock_pilot", f"universe:{snapshot.snapshot_id}",
         f"provider_version:{snapshot.provider_version}"),
    )
    row_count = 0
    status_count = 0
    latest_by_id: dict[str, BaoStockDailyStatus] = {}
    try:
        # Fetch and validate the whole bounded request before writing any bars.
        batches: list[BaoStockDailyBatch] = []
        for item in instruments:
            batch = source.fetch(item, start, end)
            if not batch.statuses:
                raise ValueError(f"{item.instrument_id}: no BaoStock daily status rows")
            if any(bar.instrument_id != item.instrument_id or bar.amount_unit not in ("CNY", "ineligible")
                   for bar in batch.bars):
                raise ValueError(f"{item.instrument_id}: source returned inconsistent bars")
            batches.append(batch)
            latest_by_id[item.instrument_id] = batch.statuses[-1]
        store.upsert_instruments(instruments)
        for batch in batches:
            row_count += store.upsert_bars(batch.bars, run_id=run_id)
            _save_statuses(store, batch.statuses, run_id)
            status_count += len(batch.statuses)
        store.finish_run(run_id, "success", len(instruments), row_count)
    except BaseException as exc:
        store.finish_run(run_id, "failed", len(instruments), row_count, str(exc))
        raise
    recommendable = tuple(
        item.instrument_id for item in instruments
        if latest_by_id[item.instrument_id].trade_date == end
        and latest_by_id[item.instrument_id].recommendable
    )
    return BaoStockPilotResult(
        run_id, snapshot.snapshot_id, len(instruments), row_count, status_count,
        recommendable,
    )
