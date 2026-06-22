from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence

from auto_invest.domain.models import (
    AssetType,
    Bar,
    Instrument,
    Market,
    as_decimal,
)


def parse_datetime(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


class CsvMarketData:
    def __init__(self, path: str):
        self.path = Path(path)

    def list_bars(
        self,
        instrument_ids: Sequence[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[Bar]:
        wanted = set(instrument_ids)
        bars = []
        with self.path.open("r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            for row in reader:
                instrument_id = row["instrument_id"]
                if wanted and instrument_id not in wanted:
                    continue

                timestamp = parse_datetime(row["timestamp"])
                if start is not None and timestamp < start:
                    continue
                if end is not None and timestamp > end:
                    continue

                bars.append(
                    Bar(
                        timestamp=timestamp,
                        instrument_id=instrument_id,
                        open=as_decimal(row["open"]),
                        high=as_decimal(row["high"]),
                        low=as_decimal(row["low"]),
                        close=as_decimal(row["close"]),
                        volume=as_decimal(row["volume"]),
                    )
                )

        return sorted(bars, key=lambda bar: (bar.timestamp, bar.instrument_id))


def load_instruments(path: str) -> Dict[str, Instrument]:
    with Path(path).open("r", encoding="utf-8") as file_obj:
        raw_items = json.load(file_obj)

    instruments: Dict[str, Instrument] = {}
    for raw in raw_items:
        market = Market(raw["market"])
        asset_type = AssetType(raw["asset_type"])
        instrument = Instrument.create(
            symbol=raw["symbol"],
            market=market,
            asset_type=asset_type,
            name=raw.get("name", ""),
            currency=raw.get("currency", ""),
            quantity_step=as_decimal(raw["quantity_step"])
            if "quantity_step" in raw
            else as_decimal("0"),
            min_quantity=as_decimal(raw["min_quantity"])
            if "min_quantity" in raw
            else as_decimal("0"),
            price_tick=as_decimal(raw["price_tick"])
            if "price_tick" in raw
            else as_decimal("0"),
            timezone=raw.get("timezone", ""),
        )
        instruments[instrument.instrument_id] = instrument

    return instruments
