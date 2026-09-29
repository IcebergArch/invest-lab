from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    symbol: str
    name: str
    asset_type: str
    family: str
    source_id: str
    provider_code: str
    exchange: str = ""
    adjustment: str = "none"


@dataclass(frozen=True)
class DailyBar:
    instrument_id: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: Optional[float]
    volume_unit: str
    amount_unit: str
    adjustment: str
    source_id: str
    payload_hash: str

    def validate(self) -> None:
        if self.trade_date is None:
            raise ValueError("trade_date is required")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError(f"{self.instrument_id}: prices must be positive")
        if self.low > min(self.open, self.close):
            raise ValueError(f"{self.instrument_id}: low exceeds open/close")
        if self.high < max(self.open, self.close):
            raise ValueError(f"{self.instrument_id}: high is below open/close")
        if self.volume < 0 or (self.amount is not None and self.amount < 0):
            raise ValueError(f"{self.instrument_id}: volume/amount cannot be negative")
