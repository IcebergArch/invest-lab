from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[1]
path.insert(0, str(ROOT / "src"))

from auto_invest.domain.models import Bar
from auto_invest.domain.portfolio import Portfolio
from auto_invest.strategies.base import BarHistory, MarketSnapshot
from auto_invest.strategies.moving_average import MovingAverageCrossStrategy


class MovingAverageStrategyTest(unittest.TestCase):
    def test_ma_cross_targets_active_instrument(self) -> None:
        instrument_id = "US:AAPL"
        history = BarHistory()
        for index, close in enumerate(["10", "11", "12"]):
            history.append(
                Bar(
                    timestamp=datetime(2026, 1, index + 1, tzinfo=timezone.utc),
                    instrument_id=instrument_id,
                    open=Decimal(close),
                    high=Decimal(close),
                    low=Decimal(close),
                    close=Decimal(close),
                    volume=Decimal("1000"),
                )
            )

        snapshot = MarketSnapshot(
            timestamp=datetime(2026, 1, 3, tzinfo=timezone.utc),
            bars={instrument_id: history.latest(instrument_id)},
            history=history,
            prices=history.latest_prices(),
        )
        strategy = MovingAverageCrossStrategy([instrument_id], fast_window=1, slow_window=3)

        targets = strategy.on_bar(snapshot, Portfolio(cash=Decimal("1000")))

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].instrument_id, instrument_id)
        self.assertEqual(targets[0].weight, Decimal("1"))


if __name__ == "__main__":
    unittest.main()
