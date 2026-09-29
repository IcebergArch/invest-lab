from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from quant_lab.backtest import run_backtest
from quant_lab.data_sources import EastMoneyDailySource, parse_eastmoney_kline
from quant_lab.models import DailyBar, Instrument
from quant_lab.research import research_report
from quant_lab.storage import MarketStore
from quant_lab.strategies import (
    CrossSectionalMomentumStrategy,
    MeanReversionZScoreStrategy,
    SmaTrendStrategy,
)
from quant_lab.verification import verify_formulas


class FormulaTest(unittest.TestCase):
    def test_all_formula_oracles_pass(self) -> None:
        checks = verify_formulas()
        self.assertTrue(checks)
        self.assertEqual([], [check.detail for check in checks if not check.passed])

    def test_sma_requires_only_available_history(self) -> None:
        strategy = SmaTrendStrategy(fast_window=2, slow_window=3)
        self.assertEqual({}, strategy.weights({"asset": [1.0, 2.0]}))
        self.assertEqual({"asset": 1.0}, strategy.weights({"asset": [1.0, 2.0, 3.0]}))

    def test_momentum_tie_break_is_stable(self) -> None:
        strategy = CrossSectionalMomentumStrategy(lookback=1, top_n=1)
        weights = strategy.weights({"b": [1.0, 2.0], "a": [1.0, 2.0]})
        self.assertEqual({"a": 1.0}, weights)

    def test_flat_series_does_not_trigger_mean_reversion(self) -> None:
        strategy = MeanReversionZScoreStrategy(window=3, entry_z=1.0)
        self.assertEqual({}, strategy.weights({"asset": [10.0, 10.0, 10.0]}))


class BacktestTimingTest(unittest.TestCase):
    def test_signal_uses_previous_close_for_next_period(self) -> None:
        class BuyAfterTwoObservations:
            name = "timing-oracle"

            def weights(
                self, history: Mapping[str, Sequence[float]]
            ) -> Mapping[str, float]:
                return {"asset": 1.0} if len(history["asset"]) >= 2 else {}

        result = run_backtest(
            BuyAfterTwoObservations(),
            [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)],
            {"asset": [100.0, 200.0, 220.0]},
            cost_rate=0.0,
        )
        self.assertAlmostEqual(1.0, result.points[-1].equity)
        self.assertEqual({}, result.points[1].weights)
        self.assertEqual({"asset": 1.0}, result.points[2].weights)

    def test_rejects_leveraged_weights(self) -> None:
        class Leveraged:
            name = "invalid"

            def weights(self, history):
                return {"asset": 1.1}

        with self.assertRaisesRegex(ValueError, "sum above 1"):
            run_backtest(
                Leveraged(),
                [date(2026, 1, 1), date(2026, 1, 2)],
                {"asset": [100.0, 101.0]},
            )

    def test_turnover_uses_drifted_pretrade_weights(self) -> None:
        class EqualWeight:
            name = "equal-weight"

            def weights(self, history):
                return {"a": 0.5, "b": 0.5}

        result = run_backtest(
            EqualWeight(),
            [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)],
            {"a": [100.0, 100.0, 200.0], "b": [100.0, 100.0, 100.0]},
            cost_rate=0.0,
        )
        self.assertAlmostEqual(1.0 / 3.0, result.points[-1].turnover)


class DataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.instrument = Instrument(
            instrument_id="stock:002475.SZ",
            symbol="002475.SZ",
            name="立讯精密",
            asset_type="stock",
            family="focus_stock",
            source_id="eastmoney_kline",
            provider_code="0.002475",
            exchange="SZSE",
            adjustment="qfq",
        )

    def test_eastmoney_fields_are_mapped_in_documented_order(self) -> None:
        bar = parse_eastmoney_kline(
            self.instrument,
            "2026-09-01,57.55,56.92,57.66,56.30,687842,3913861741.34,2.36,-1.21,-0.70,0.94",
            "abc",
        )
        self.assertEqual(date(2026, 9, 1), bar.trade_date)
        self.assertEqual(57.55, bar.open)
        self.assertEqual(56.92, bar.close)
        self.assertEqual(57.66, bar.high)
        self.assertEqual(56.30, bar.low)
        self.assertEqual("qfq", bar.adjustment)

    def test_eastmoney_name_spacing_and_fullwidth_letters_preserve_code_check(self) -> None:
        class Provider:
            def __init__(self, code: str):
                self.code = code

            def get(self, _endpoint, _params):
                return {"data": {"code": self.code, "name": "万  科Ａ", "klines": [
                    "2026-09-28,10.00,10.20,10.30,9.90,100000,1020000"
                ]}}, "payload-hash"

        instrument = Instrument("stock:000002.SZ", "000002.SZ", "万科A", "stock",
                                "current_stock", "eastmoney_kline", "0.000002",
                                "SZSE", "none")
        bars = EastMoneyDailySource(Provider("000002")).fetch(
            instrument, date(2026, 9, 28), date(2026, 9, 28)
        )
        self.assertEqual(1, len(bars))
        with self.assertRaisesRegex(ValueError, "provider code"):
            EastMoneyDailySource(Provider("000003")).fetch(
                instrument, date(2026, 9, 28), date(2026, 9, 28)
            )

    def test_store_upsert_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            store.upsert_instruments([self.instrument])
            bar = DailyBar(
                instrument_id=self.instrument.instrument_id,
                trade_date=date(2026, 9, 1),
                open=10.0,
                high=11.0,
                low=9.0,
                close=10.5,
                volume=100.0,
                amount=1000.0,
                volume_unit="lot",
                amount_unit="CNY",
                adjustment="qfq",
                source_id="eastmoney_kline",
                payload_hash="hash",
            )
            store.upsert_bars([bar])
            store.upsert_bars([bar])
            summary = store.instrument_summary()
            self.assertEqual(1, summary[0]["rows"])

    def test_report_asof_excludes_future_bars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            store.upsert_instruments([self.instrument])
            bars = [DailyBar(
                instrument_id=self.instrument.instrument_id,
                trade_date=date(2026, 1, day),
                open=float(10 + day), high=float(11 + day),
                low=float(9 + day), close=float(10 + day),
                volume=100.0, amount=1000.0, volume_unit="lot",
                amount_unit="CNY", adjustment="qfq",
                source_id="eastmoney_kline", payload_hash=str(day),
            ) for day in range(1, 5)]
            store.upsert_bars(bars)
            early = research_report(store, "sma-trend", date(2026, 1, 3))
            store.upsert_bars([DailyBar(
                instrument_id=self.instrument.instrument_id,
                trade_date=date(2026, 1, 4), open=1000.0, high=1001.0,
                low=999.0, close=1000.0, volume=100.0, amount=1000.0,
                volume_unit="lot", amount_unit="CNY", adjustment="qfq",
                source_id="eastmoney_kline", payload_hash="changed",
            )])
            rerun = research_report(store, "sma-trend", date(2026, 1, 3))
            self.assertEqual(early["asof"], rerun["asof"])
            self.assertEqual(early["signals"], rerun["signals"])
            self.assertEqual(early["backtest"], rerun["backtest"])


if __name__ == "__main__":
    unittest.main()
