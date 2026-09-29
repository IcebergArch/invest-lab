from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from threading import Lock
from typing import Callable, Sequence
from uuid import uuid4

from quant_lab.data_sources import PublicMarketDailySource, SwResearchDailySource
from quant_lab.models import DailyBar, Instrument
from quant_lab.storage import MarketStore
from quant_lab.universe import select_core


@dataclass(frozen=True)
class SyncResult:
    run_id: str
    instrument_count: int
    row_count: int


def sync_database(
    store: MarketStore,
    start: date,
    end: date,
    groups: Sequence[str] = (),
    workers: int = 4,
    progress: Callable[[str], None] | None = None,
    stock_ids: Sequence[str] = (),
) -> SyncResult:
    if start > end:
        raise ValueError("start date must not be after end date")
    if workers < 1 or workers > 8:
        raise ValueError("workers must be between 1 and 8")

    requested_stock_ids = tuple(dict.fromkeys(stock_ids))
    if len(requested_stock_ids) > 30:
        raise ValueError("at most 30 explicit stocks may be synced in one run")
    # Preserve the historical default, while allowing an explicit stock-only run.
    selected_groups = tuple(groups) or (() if requested_stock_ids else ("all",))
    valid_groups = {"all", "core", "stocks", "broad", "industry"}
    unknown = sorted(set(selected_groups) - valid_groups)
    if unknown:
        raise ValueError(f"unknown groups: {unknown}")

    run_id = uuid4().hex
    store.initialize()
    # The run record names the explicit targets as well as broad selection groups.
    # Each saved bar separately carries its actual source, payload hash, and run ID.
    store.start_run(run_id, start, end, (*selected_groups, *requested_stock_ids))
    instrument_count = 0
    row_count = 0

    try:
        core = select_core(selected_groups) if selected_groups else []
        include_industry = "all" in selected_groups or "industry" in selected_groups
        industries: list[Instrument] = []
        sw_source = SwResearchDailySource()
        if include_industry:
            if progress:
                progress("正在核对申万一级行业清单")
            industries = sw_source.list_level_one()

        targeted: list[Instrument] = []
        if requested_stock_ids:
            with store.connect() as connection:
                for instrument_id in requested_stock_ids:
                    row = connection.execute(
                        """SELECT instrument_id, symbol, name, asset_type, family,
                                  source_id, provider_code, exchange, adjustment
                           FROM instruments WHERE instrument_id=? AND asset_type='stock'""",
                        (instrument_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError(
                            f"{instrument_id}: stock is not registered in the main store"
                        )
                    item = Instrument(**dict(row))
                    if item.source_id != "eastmoney_kline":
                        raise ValueError(
                            f"{instrument_id}: targeted sync requires an eastmoney_kline stock"
                        )
                    targeted.append(item)

        # A requested core stock is fetched only once. Its registered definition
        # wins so a stale hard-coded core entry cannot replace current metadata.
        instruments_by_id = {item.instrument_id: item for item in (*core, *industries)}
        instruments_by_id.update({item.instrument_id: item for item in targeted})
        instruments = list(instruments_by_id.values())
        if not instruments:
            raise ValueError("no instruments selected")
        store.upsert_instruments(instruments)
        instrument_count = len(instruments)

        market_source = PublicMarketDailySource()
        eastmoney_lock = Lock()

        def fetch(item: Instrument) -> tuple[Instrument, list[DailyBar]]:
            if item.source_id == "eastmoney_kline":
                # This public endpoint frequently terminates parallel requests.
                # Serialize only this source; SW industry downloads remain parallel.
                with eastmoney_lock:
                    return item, market_source.fetch(item, start, end)
            if item.source_id == "sw_research":
                return item, sw_source.fetch(item, start, end)
            raise ValueError(f"no fetcher for source {item.source_id}")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch, item): item for item in instruments}
            for future in as_completed(futures):
                item, bars = future.result()
                if not bars:
                    raise ValueError(
                        f"{item.instrument_id}: no rows returned for {start}..{end}"
                    )
                inserted = store.upsert_bars(bars, run_id=run_id)
                row_count += inserted
                if progress:
                    progress(
                        f"{item.instrument_id} {item.name}: {inserted} rows "
                        f"({bars[0].trade_date}..{bars[-1].trade_date})"
                    )

        store.finish_run(run_id, "success", instrument_count, row_count)
        return SyncResult(run_id, instrument_count, row_count)
    except BaseException as exc:
        store.finish_run(run_id, "failed", instrument_count, row_count, str(exc))
        raise
