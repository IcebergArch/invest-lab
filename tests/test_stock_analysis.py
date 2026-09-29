from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.models import DailyBar, Instrument
from quant_lab.stock_analysis import analyze_stock
from quant_lab.storage import MarketStore


def _days(count: int) -> list[date]:
    result = []
    day = date(2026, 1, 1)
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day)
        day += timedelta(days=1)
    return result


def _bar(instrument_id: str, day: date, close: float, amount: float | None,
         adjustment: str = "qfq") -> DailyBar:
    return DailyBar(instrument_id, day, close, close * 1.01, close * 0.99,
                    close, 1_000_000, amount, "share", "CNY" if amount is not None
                    else "unavailable", adjustment, "test_source", f"{instrument_id}:{day}")


class StockAnalysisTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MarketStore(Path(temp.name) / "market.sqlite3")
        self.store.initialize()
        self.days = _days(135)
        self.key = "stock:002475.SZ"
        self.store.upsert_instruments([Instrument(
            self.key, "002475.SZ", "立讯精密", "stock", "test", "test_source",
            "0.002475", exchange="SZSE", adjustment="qfq",
        )])

    def add_prices(self, missing_amount_tail: int = 0) -> None:
        prices = [10 * 1.002 ** index for index in range(len(self.days))]
        self.store.upsert_bars([
            _bar(self.key, day, close,
                 None if index >= len(self.days) - missing_amount_tail else 80_000_000)
            for index, (day, close) in enumerate(zip(self.days, prices))
        ])

    def test_report_resolves_code_name_and_cost_without_claiming_account_pnl(self) -> None:
        self.add_prices()
        report = analyze_stock(self.store, "002475", cost_price="12.5", asof=self.days[-1])
        self.assertEqual("ready", report["status"])
        self.assertEqual(self.key, report["instrument_id"])
        self.assertEqual("立讯精密", report["name"])
        self.assertEqual(self.days[-1].isoformat(), report["asof"])
        self.assertAlmostEqual(report["latest_close"] / 12.5 - 1, report["cost_return"])
        self.assertIn("不是账户盈亏", report["summary"])
        self.assertEqual("historical_diagnostic_only", report["backtest"]["status"])
        self.assertFalse(report["backtest"]["independent_out_of_sample"])
        self.assertEqual("baseline_only", report["forecast"]["status"])
        self.assertIsNone(report["forecast"]["direction_probability"])
        self.assertGreater(report["forecast"]["rolling_validation"]["5"]["count"], 0)
        self.assertEqual(report["latest_close"], report["forecast"]["baseline_close_5d"])
        self.assertEqual(self.key, analyze_stock(self.store, "立讯精密")["instrument_id"])
        self.assertEqual(self.key, analyze_stock(self.store, "sz.002475")["instrument_id"])

    def test_requested_asof_excludes_future_and_reports_missing_amount(self) -> None:
        self.add_prices(missing_amount_tail=12)
        cutoff = self.days[-6]
        original = analyze_stock(self.store, self.key, asof=cutoff)
        self.store.upsert_bars([_bar(self.key, self.days[-1] + timedelta(days=1), 5000, 1e9)])
        later = analyze_stock(self.store, self.key, asof=cutoff)
        self.assertEqual("partial_data", original["status"])
        self.assertIsNone(original["risk"]["metrics"]["avg_amount_20d_cny"])
        self.assertIn("成交额不完整", original["risk"]["summary"])
        self.assertEqual(original["asof"], later["asof"])
        self.assertEqual(original["latest_close"], later["latest_close"])
        self.assertEqual(original["backtest"], later["backtest"])

    def test_missing_market_sessions_are_explicit(self) -> None:
        self.add_prices()
        index = "index:000300.SH"
        self.store.upsert_instruments([Instrument(
            index, "000300.SH", "沪深300", "index", "test", "test_source",
            "1.000300", adjustment="none",
        )])
        self.store.upsert_bars([
            _bar(index, day, 100, None, "none") for day in self.days
        ] + [_bar(index, self.days[-1] + timedelta(days=1), 100, None, "none")])
        result = analyze_stock(self.store, self.key, asof=self.days[-1] + timedelta(days=1))
        self.assertEqual("partial_data", result["status"])
        self.assertEqual(1, result["sources"][0]["missing_recent_market_sessions"])
        self.assertIn("落后 1 个交易日", result["summary"])

    def test_unavailable_and_invalid_inputs_are_explicit(self) -> None:
        unknown = analyze_stock(self.store, "600000")
        self.assertEqual("unknown_stock", unknown["status"])
        self.assertIsNone(unknown["latest_close"])
        no_bars = analyze_stock(self.store, "002475.SZ", asof=self.days[-1])
        self.assertEqual("no_bars", no_bars["status"])
        with self.assertRaisesRegex(ValueError, "成本价"):
            analyze_stock(self.store, "002475", cost_price="nan")
        with self.assertRaisesRegex(ValueError, "请输入"):
            analyze_stock(self.store, " ")


if __name__ == "__main__":
    unittest.main()
