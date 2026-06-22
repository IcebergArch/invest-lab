from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from auto_invest.domain.models import TargetWeight, as_decimal
from auto_invest.domain.portfolio import Portfolio
from auto_invest.strategies.base import MarketSnapshot


def mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("cannot calculate mean of empty sequence")
    return sum(values, Decimal("0")) / Decimal(len(values))


class MovingAverageCrossStrategy:
    name = "ma-cross"

    def __init__(
        self,
        instrument_ids: Sequence[str],
        fast_window: int = 5,
        slow_window: int = 20,
        risk_fraction: Decimal = Decimal("1"),
    ):
        if fast_window <= 0 or slow_window <= 0:
            raise ValueError("moving-average windows must be positive")
        if fast_window >= slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        if not instrument_ids:
            raise ValueError("instrument_ids cannot be empty")
        normalized_risk = as_decimal(risk_fraction)
        if normalized_risk < 0 or normalized_risk > 1:
            raise ValueError("risk_fraction must be between 0 and 1")

        self.instrument_ids = list(instrument_ids)
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.risk_fraction = normalized_risk

    def on_bar(
        self,
        snapshot: MarketSnapshot,
        portfolio: Portfolio,
    ) -> Sequence[TargetWeight]:
        active = []
        for instrument_id in self.instrument_ids:
            closes = snapshot.history.closes(instrument_id, self.slow_window)
            if len(closes) < self.slow_window:
                continue

            fast_ma = mean(closes[-self.fast_window :])
            slow_ma = mean(closes)
            if fast_ma > slow_ma:
                active.append(instrument_id)

        if not active:
            return [
                TargetWeight(instrument_id=instrument_id, weight=Decimal("0"))
                for instrument_id in self.instrument_ids
            ]

        active_weight = self.risk_fraction / Decimal(len(active))
        return [
            TargetWeight(
                instrument_id=instrument_id,
                weight=active_weight if instrument_id in active else Decimal("0"),
            )
            for instrument_id in self.instrument_ids
        ]
