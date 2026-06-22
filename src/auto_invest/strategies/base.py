from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Mapping, Protocol, Sequence

from auto_invest.domain.models import Bar, TargetWeight
from auto_invest.domain.portfolio import Portfolio


@dataclass
class BarHistory:
    bars_by_instrument: Dict[str, List[Bar]] = field(default_factory=dict)

    def append(self, bar: Bar) -> None:
        self.bars_by_instrument.setdefault(bar.instrument_id, []).append(bar)

    def latest(self, instrument_id: str) -> Bar:
        bars = self.bars_by_instrument[instrument_id]
        return bars[-1]

    def closes(self, instrument_id: str, lookback: int = 0) -> List[Decimal]:
        bars = self.bars_by_instrument.get(instrument_id, [])
        selected = bars[-lookback:] if lookback else bars
        return [bar.close for bar in selected]

    def latest_prices(self) -> Dict[str, Decimal]:
        return {
            instrument_id: bars[-1].close
            for instrument_id, bars in self.bars_by_instrument.items()
            if bars
        }


@dataclass(frozen=True)
class MarketSnapshot:
    timestamp: datetime
    bars: Mapping[str, Bar]
    history: BarHistory
    prices: Mapping[str, Decimal]


class Strategy(Protocol):
    name: str

    def on_bar(
        self,
        snapshot: MarketSnapshot,
        portfolio: Portfolio,
    ) -> Sequence[TargetWeight]:
        ...
