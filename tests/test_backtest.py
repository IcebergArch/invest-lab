from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[1]
path.insert(0, str(ROOT / "src"))

from auto_invest.adapters.csv_market_data import CsvMarketData, load_instruments
from auto_invest.backtest.engine import BacktestConfig, BacktestEngine
from auto_invest.domain.models import Side
from auto_invest.strategies.buy_and_hold import BuyAndHoldStrategy
from auto_invest.strategies.moving_average import MovingAverageCrossStrategy


class BacktestEngineTest(unittest.TestCase):
    def test_buy_and_hold_runs_across_all_markets(self) -> None:
        instruments = load_instruments(str(ROOT / "configs" / "universe.example.json"))
        instrument_ids = sorted(instruments)
        strategy = BuyAndHoldStrategy(
            {instrument_id: Decimal("0.25") for instrument_id in instrument_ids}
        )
        engine = BacktestEngine(
            market_data=CsvMarketData(str(ROOT / "data" / "sample" / "bars.csv")),
            instruments=instruments,
            config=BacktestConfig(initial_cash=Decimal("100000")),
        )

        result = engine.run(strategy, instrument_ids)

        self.assertGreater(len(result.equity_curve), 0)
        self.assertGreater(len(result.trades), 0)
        self.assertIn("total_return", result.metrics)
        self.assertIn("max_drawdown", result.metrics)

    def test_market_quantity_step_is_respected(self) -> None:
        instruments = load_instruments(str(ROOT / "configs" / "universe.example.json"))
        instrument_ids = ["CN_A:000001.SZ"]
        strategy = BuyAndHoldStrategy({"CN_A:000001.SZ": Decimal("1")})
        engine = BacktestEngine(
            market_data=CsvMarketData(str(ROOT / "data" / "sample" / "bars.csv")),
            instruments=instruments,
            config=BacktestConfig(initial_cash=Decimal("100000")),
        )

        result = engine.run(strategy, instrument_ids)
        buy_trades = [trade for trade in result.trades if trade.side == Side.BUY]

        self.assertTrue(buy_trades)
        for trade in buy_trades:
            self.assertEqual(trade.quantity % Decimal("100"), Decimal("0"))

    def test_ma_cross_produces_metrics(self) -> None:
        instruments = load_instruments(str(ROOT / "configs" / "universe.example.json"))
        instrument_ids = sorted(instruments)
        strategy = MovingAverageCrossStrategy(
            instrument_ids=instrument_ids,
            fast_window=2,
            slow_window=3,
        )
        engine = BacktestEngine(
            market_data=CsvMarketData(str(ROOT / "data" / "sample" / "bars.csv")),
            instruments=instruments,
            config=BacktestConfig(initial_cash=Decimal("100000")),
        )

        result = engine.run(strategy, instrument_ids)

        self.assertGreaterEqual(result.metrics["trade_count"], Decimal("0"))
        self.assertGreater(result.metrics["final_equity"], Decimal("0"))


if __name__ == "__main__":
    unittest.main()
