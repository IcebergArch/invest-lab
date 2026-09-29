from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.models import DailyBar, Instrument
from quant_lab.stock_screen import StockScreenConfig, build_stock_shortlist
from quant_lab.storage import MarketStore


def trading_days(count: int) -> list[date]:
    days = []
    day = date(2026, 1, 1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def instrument(key: str, asset_type: str = "stock") -> Instrument:
    return Instrument(key, key.split(":")[-1], key, asset_type, "test",
                      "test_source", key, adjustment="qfq" if asset_type == "stock" else "none")


def bar(key: str, day: date, price: float, amount: float = 80_000_000.0) -> DailyBar:
    return DailyBar(key, day, price, price * 1.01, price * 0.99, price,
                    1_000_000.0, amount, "lot", "CNY", "qfq",
                    "test_source", f"{key}:{day}")


class StockScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MarketStore(Path(self.temp.name) / "market.sqlite3")
        self.store.initialize()
        self.days = trading_days(70)
        self.config = StockScreenConfig(min_universe_size=1)

    def add_series(self, key: str, prices: list[float], amount: float = 80_000_000.0) -> None:
        self.store.upsert_instruments([instrument(key)])
        self.store.upsert_bars([bar(key, day, price, amount)
                                for day, price in zip(self.days, prices)])

    def test_ranks_explainable_return_and_risk_and_filters_bad_stocks(self) -> None:
        self.add_series("stock:FAST", [10 * 1.002 ** index for index in range(70)])
        self.add_series("stock:SLOW", [10 * 1.0012 ** index for index in range(70)])
        self.add_series("stock:ILLIQUID", [10 * 1.003 ** index for index in range(70)],
                        amount=1_000_000.0)
        self.add_series("stock:VOLATILE", [10 * 1.001 ** index *
                        (1.12 if index % 2 else 0.88) for index in range(70)])
        report = build_stock_shortlist(
            self.store, ["stock:SLOW", "stock:VOLATILE", "stock:FAST", "stock:ILLIQUID"],
            self.days[-1], self.config,
        )
        self.assertEqual("ready", report["status"])
        self.assertEqual(4, report["universe_count"])
        self.assertEqual(4, report["data_ready_count"])
        self.assertEqual(2, report["eligible_count"])
        self.assertEqual(["stock:FAST", "stock:SLOW"],
                         [item["instrument_id"] for item in report["candidates"]])
        self.assertIn("历史已实现涨跌", report["candidates"][0]["reason"])
        self.assertIn("不是未来预期收益", report["candidates"][0]["reason"])
        self.assertIsNone(report["candidates"][0]["forecast_probability"])
        self.assertEqual({"stock:ILLIQUID", "stock:VOLATILE"},
                         {item["instrument_id"] for item in report["excluded"]})

    def test_asof_excludes_future_price_and_amount(self) -> None:
        key = "stock:ANY_ID"
        self.add_series(key, [10 * 1.002 ** index for index in range(70)])
        original = build_stock_shortlist(self.store, [key], self.days[-1], self.config)
        future = self.days[-1] + timedelta(days=1)
        self.store.upsert_bars([bar(key, future, 10_000.0, amount=1_000_000_000.0)])
        rerun = build_stock_shortlist(self.store, [key], self.days[-1], self.config)
        self.assertEqual(original["asof"], rerun["asof"])
        self.assertEqual(original["candidates"], rerun["candidates"])

    def test_stale_and_incomplete_universe_do_not_emit_candidates(self) -> None:
        key = "stock:GOOD"
        self.add_series(key, [10 * 1.002 ** index for index in range(70)])
        stale = build_stock_shortlist(
            self.store, [key], self.days[-1] + timedelta(days=8), self.config,
        )
        self.assertEqual("blocked_stale_data", stale["status"])
        self.assertEqual(0, stale["candidate_count"])
        incomplete = build_stock_shortlist(
            self.store, [key, "stock:MISSING_A", "stock:MISSING_B"],
            self.days[-1], self.config,
        )
        self.assertEqual("blocked_universe_coverage", incomplete["status"])
        self.assertEqual(1, incomplete["data_ready_count"])
        self.assertEqual(0, incomplete["candidate_count"])

    def test_benchmark_calendar_exposes_missing_latest_stock_session(self) -> None:
        key = "stock:HALTED"
        self.store.upsert_instruments([instrument(key), instrument("index:000300.SH", "index")])
        self.store.upsert_bars([bar(key, day, 10 * 1.002 ** index)
                                for index, day in enumerate(self.days[:-1])])
        self.store.upsert_bars([
            DailyBar("index:000300.SH", day, 100.0, 101.0, 99.0, 100.0,
                     1_000_000.0, None, "lot", "unavailable", "none", "test_source", str(day))
            for day in self.days
        ])
        report = build_stock_shortlist(self.store, [key], self.days[-1], self.config)
        self.assertEqual("index:000300.SH", report["calendar_source"])
        self.assertEqual(self.days[-1].isoformat(), report["asof"])
        self.assertEqual("blocked_universe_coverage", report["status"])
        self.assertEqual(0, report["candidate_count"])

    def test_three_stock_pilot_and_unverified_market_coverage_are_blocked(self) -> None:
        ids = [f"stock:{index}" for index in range(3)]
        for key in ids:
            self.add_series(key, [10 * 1.002 ** index for index in range(70)])
        pilot = build_stock_shortlist(self.store, ids, self.days[-1])
        self.assertEqual("blocked_narrow_universe", pilot["status"])
        self.assertEqual(0, pilot["candidate_count"])
        incomplete = build_stock_shortlist(
            self.store, ids, self.days[-1],
            StockScreenConfig(min_universe_size=1, expected_universe_count=4),
        )
        self.assertEqual("blocked_expected_universe_coverage", incomplete["status"])
        self.assertEqual(0.75, incomplete["expected_universe_coverage_rate"])
        self.assertEqual(0, incomplete["candidate_count"])

    def test_no_qualified_stocks_is_explicit(self) -> None:
        key = "stock:DECLINE"
        self.add_series(key, [10 * 0.998 ** index for index in range(70)])
        report = build_stock_shortlist(self.store, [key], self.days[-1], self.config)
        self.assertEqual("no_qualified_stocks", report["status"])
        self.assertEqual(0, report["candidate_count"])
        self.assertIn("近20日走势未达到门槛", report["excluded"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
