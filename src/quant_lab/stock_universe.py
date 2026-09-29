"""Current EastMoney stock universe and deliberately bounded history ingestion.

This is a *current* quote-list snapshot.  It must never be used as a historical
point-in-time universe: delisted stocks and old listing states are not present.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4

from quant_lab.data_sources import EastMoneyDailySource, HttpJsonClient, PublicMarketDailySource
from quant_lab.models import DailyBar, Instrument
from quant_lab.storage import MarketStore
from quant_lab.universe import CORE_INSTRUMENTS


class JsonClient(Protocol):
    def get(self, endpoint: str, params: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]: ...


LIST_ENDPOINT = "https://push2.eastmoney.com/api/qt/clist/get"
LIST_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FOCUS_IDS = {item.instrument_id for item in CORE_INSTRUMENTS if item.asset_type == "stock"}


@dataclass(frozen=True)
class StockListing:
    instrument: Instrument
    # These quote fields are optional and are never treated as historical values.
    # Keep the provider field names until a live payload and units are reconciled.
    provider_f6: float | None
    provider_f20: float | None
    provider_f21: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument.instrument_id,
            "symbol": self.instrument.symbol,
            "name": self.instrument.name,
            "exchange": self.instrument.exchange,
            "provider_code": self.instrument.provider_code,
            "provider_f6": self.provider_f6,
            "provider_f20": self.provider_f20,
            "provider_f21": self.provider_f21,
        }


@dataclass(frozen=True)
class StockUniverseSnapshot:
    collected_at: str
    expected_total: int
    listings: tuple[StockListing, ...]
    page_hashes: tuple[str, ...]
    page_size: int

    @property
    def snapshot_id(self) -> str:
        canonical = json.dumps(
            {
                "collected_at": self.collected_at,
                "listings": [listing.to_dict() for listing in self.listings],
                "page_hashes": self.page_hashes,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "source_id": "eastmoney_clist",
            "endpoint": LIST_ENDPOINT,
            "filter": LIST_FILTER,
            "collected_at": self.collected_at,
            "expected_total": self.expected_total,
            "retrieved_total": len(self.listings),
            "page_size": self.page_size,
            "page_hashes": list(self.page_hashes),
            "scope": "provider current Shanghai, Shenzhen and Beijing stock quote list",
            "historical_point_in_time": False,
            "listings": [listing.to_dict() for listing in self.listings],
        }


def _optional_number(raw: object, field: str) -> float | None:
    if raw is None or raw == "-":
        return None
    if isinstance(raw, bool):
        raise ValueError(f"invalid {field}: {raw!r}")
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid {field}: {raw!r}")
    return value


def parse_stock_listing(row: Mapping[str, Any]) -> StockListing:
    code = str(row.get("f12") or "")
    name = str(row.get("f14") or "").strip()
    market = row.get("f13")
    if len(code) != 6 or not code.isdigit() or not name:
        raise ValueError(f"invalid stock code/name in provider row: {row!r}")
    if isinstance(market, bool) or not isinstance(market, int):
        raise ValueError(f"invalid provider market identifier: {market!r}")
    if market == 1 and code.startswith("6"):
        suffix, exchange = "SH", "SSE"
    elif market == 0 and code.startswith(("0", "3")):
        suffix, exchange = "SZ", "SZSE"
    elif market == 0 and code.startswith(("4", "8", "92")):
        suffix, exchange = "BJ", "BSE"
    else:
        # Fail rather than silently discard a new venue/code range, B share, ETF,
        # or an unexpected provider market identifier.
        raise ValueError(f"unexpected stock market/code: f13={market!r} f12={code!r}")
    instrument_id = f"stock:{code}.{suffix}"
    instrument = Instrument(
        instrument_id=instrument_id,
        symbol=f"{code}.{suffix}",
        name=name,
        asset_type="stock",
        family="focus_stock" if instrument_id in FOCUS_IDS else "current_stock",
        source_id="eastmoney_kline",
        provider_code=f"{market}.{code}",
        exchange=exchange,
        adjustment="qfq",
    )
    return StockListing(
        instrument,
        _optional_number(row.get("f6"), "f6"),
        _optional_number(row.get("f20"), "f20"),
        _optional_number(row.get("f21"), "f21"),
    )


class EastMoneyStockUniverseSource:
    """Enumerate the provider's current stock list; reject partial page sets."""

    def __init__(self, http: JsonClient | None = None):
        self.http = http or HttpJsonClient(timeout=20.0, retries=3)

    @staticmethod
    def _params(page: int, page_size: int) -> dict[str, str]:
        return {
            "pn": str(page),
            "pz": str(page_size),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": LIST_FILTER,
            "fields": "f12,f13,f14,f6,f20,f21",
        }

    def list_current(
        self,
        *,
        page_size: int = 500,
        max_pages: int = 30,
        minimum_total: int = 4000,
    ) -> StockUniverseSnapshot:
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000")
        if max_pages < 1 or minimum_total < 0:
            raise ValueError("invalid max_pages/minimum_total")

        collected_at = datetime.now(timezone.utc).isoformat()
        all_listings: list[StockListing] = []
        page_hashes: list[str] = []
        seen: set[str] = set()
        expected_total: int | None = None
        first_codes: tuple[str, ...] = ()
        page = 1
        while True:
            payload, payload_hash = self.http.get(LIST_ENDPOINT, self._params(page, page_size))
            if payload.get("rc") not in (0, "0"):
                raise ValueError(f"stock list provider error on page {page}: {payload.get('rc')!r}")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("diff"), list):
                raise ValueError(f"stock list missing page {page}")
            total = data.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < minimum_total:
                raise ValueError(f"implausible stock list total on page {page}: {total!r}")
            if expected_total is None:
                expected_total = total
                if math.ceil(total / page_size) > max_pages:
                    raise ValueError(f"stock list needs more than {max_pages} pages")
            elif total != expected_total:
                raise ValueError(f"stock list total changed during pagination: {expected_total} -> {total}")
            remaining = expected_total - len(all_listings)
            required_rows = min(page_size, remaining)
            if len(data["diff"]) != required_rows:
                raise ValueError(
                    f"stock list page {page} has {len(data['diff'])} rows; expected {required_rows}"
                )
            codes: list[str] = []
            for row in data["diff"]:
                if not isinstance(row, dict):
                    raise ValueError(f"stock list non-object row on page {page}")
                listing = parse_stock_listing(row)
                key = listing.instrument.instrument_id
                if key in seen:
                    raise ValueError(f"stock list duplicate across pages: {key}")
                seen.add(key)
                codes.append(key)
                all_listings.append(listing)
            if page == 1:
                first_codes = tuple(codes)
            page_hashes.append(payload_hash)
            if len(all_listings) == expected_total:
                break
            page += 1

        # Quotes are sorted by a moving field.  A changed first page means the
        # multi-request result cannot be treated as a coherent list snapshot.
        if page > 1:
            check, _ = self.http.get(LIST_ENDPOINT, self._params(1, page_size))
            check_data = check.get("data")
            if (
                check.get("rc") not in (0, "0")
                or not isinstance(check_data, dict)
                or check_data.get("total") != expected_total
                or not isinstance(check_data.get("diff"), list)
                or tuple(parse_stock_listing(row).instrument.instrument_id for row in check_data["diff"])
                != first_codes
            ):
                raise ValueError("stock list first page changed during pagination")

        return StockUniverseSnapshot(
            collected_at=collected_at,
            expected_total=expected_total,
            listings=tuple(sorted(all_listings, key=lambda item: item.instrument.instrument_id)),
            page_hashes=tuple(page_hashes),
            page_size=page_size,
        )


def write_stock_snapshot(snapshot: StockUniverseSnapshot, output: str | Path) -> Path:
    """Persist a complete snapshot atomically, never a partial page set."""
    if len(snapshot.listings) != snapshot.expected_total:
        raise ValueError("refusing to save incomplete stock universe")
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def read_stock_snapshot(path: str | Path) -> StockUniverseSnapshot:
    """Reload an archived provider snapshot, checking its ID and contents."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("source_id") != "eastmoney_clist"
            or data.get("endpoint") != LIST_ENDPOINT or data.get("filter") != LIST_FILTER):
        raise ValueError("stock snapshot source/filter mismatch")
    if not isinstance(data.get("listings"), list):
        raise ValueError("stock snapshot missing listings")
    listings = []
    for record in data["listings"]:
        if not isinstance(record, dict):
            raise ValueError("stock snapshot contains non-object listing")
        market, code = str(record.get("provider_code", "")).split(".", 1)
        listing = parse_stock_listing({
            "f12": code,
            "f13": int(market),
            "f14": record.get("name"),
            "f6": record.get("provider_f6"),
            "f20": record.get("provider_f20"),
            "f21": record.get("provider_f21"),
        })
        if listing.to_dict() != record:
            raise ValueError(f"stock snapshot listing mismatch: {code}")
        listings.append(listing)
    expected_total = data.get("expected_total")
    if (isinstance(expected_total, bool) or not isinstance(expected_total, int)
            or len(listings) != expected_total
            or len({item.instrument.instrument_id for item in listings}) != expected_total
            or data.get("retrieved_total") != expected_total):
        raise ValueError("stock snapshot incomplete or duplicated")
    snapshot = StockUniverseSnapshot(
        collected_at=str(data["collected_at"]),
        expected_total=expected_total,
        listings=tuple(listings),
        page_hashes=tuple(data["page_hashes"]),
        page_size=int(data["page_size"]),
    )
    if snapshot.snapshot_id != data.get("snapshot_id"):
        raise ValueError("stock snapshot ID mismatch")
    return snapshot


@dataclass(frozen=True)
class StockPilotSyncResult:
    run_id: str
    instrument_count: int
    row_count: int
    snapshot_id: str


def sync_stock_pilot(
    store: MarketStore,
    snapshot: StockUniverseSnapshot,
    selected_ids: Sequence[str],
    start: date,
    end: date,
    *,
    max_instruments: int = 30,
    market_source: PublicMarketDailySource | None = None,
    bj_source: EastMoneyDailySource | None = None,
) -> StockPilotSyncResult:
    """Sync only explicitly selected IDs; no implicit whole-market download."""
    if start > end or (end - start).days > 550:
        raise ValueError("pilot date span must be 0..550 calendar days")
    if not selected_ids or len(selected_ids) > max_instruments or len(set(selected_ids)) != len(selected_ids):
        raise ValueError(f"select 1..{max_instruments} distinct stock IDs")
    if len(snapshot.listings) != snapshot.expected_total:
        raise ValueError("pilot requires a complete current-universe snapshot")
    by_id = {item.instrument.instrument_id: item.instrument for item in snapshot.listings}
    if len(by_id) != snapshot.expected_total:
        raise ValueError("pilot snapshot contains duplicate IDs")
    unknown = sorted(set(selected_ids) - by_id.keys())
    if unknown:
        raise ValueError(f"stocks absent from current-universe snapshot: {unknown}")

    instruments = [by_id[key] for key in selected_ids]
    market_source = market_source or PublicMarketDailySource()
    bj_source = bj_source or EastMoneyDailySource(HttpJsonClient(timeout=20.0, retries=2))
    run_id = uuid4().hex
    store.initialize()
    store.start_run(run_id, start, end, ("stock_pilot", f"universe:{snapshot.snapshot_id}"))
    row_count = 0
    try:
        store.upsert_instruments(instruments)
        for item in instruments:
            # Tencent's fallback maps market 0 to SZ; that is incorrect for BSE.
            source = bj_source if item.exchange == "BSE" else market_source
            bars: list[DailyBar] = source.fetch(item, start, end)
            if not bars:
                raise ValueError(f"{item.instrument_id}: no daily bars in pilot date range")
            if any(bar.instrument_id != item.instrument_id
                   or not start <= bar.trade_date <= end
                   or bar.amount is None or bar.amount_unit != "CNY"
                   for bar in bars):
                raise ValueError(
                    f"{item.instrument_id}: pilot bars outside request or missing CNY amount"
                )
            row_count += store.upsert_bars(bars, run_id=run_id)
        store.finish_run(run_id, "success", len(instruments), row_count)
    except BaseException as exc:
        store.finish_run(run_id, "failed", len(instruments), row_count, str(exc))
        raise
    return StockPilotSyncResult(run_id, len(instruments), row_count, snapshot.snapshot_id)
