from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Sequence

from auto_invest.domain.models import TargetWeight, as_decimal
from auto_invest.domain.portfolio import Portfolio
from auto_invest.strategies.base import MarketSnapshot


class BuyAndHoldStrategy:
    name = "buy-and-hold"

    def __init__(self, weights: Mapping[str, Decimal]):
        total = sum((as_decimal(value) for value in weights.values()), Decimal("0"))
        if total > Decimal("1"):
            raise ValueError("buy-and-hold weights cannot sum above 1")
        self.weights = {
            instrument_id: as_decimal(weight)
            for instrument_id, weight in weights.items()
        }

    def on_bar(
        self,
        snapshot: MarketSnapshot,
        portfolio: Portfolio,
    ) -> Sequence[TargetWeight]:
        return [
            TargetWeight(instrument_id=instrument_id, weight=weight)
            for instrument_id, weight in self.weights.items()
            if instrument_id in snapshot.prices
        ]
