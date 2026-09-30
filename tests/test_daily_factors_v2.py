from __future__ import annotations

import math
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.daily_factors_v2 import FactorBar, calculate_factor, factor_catalog_v2

CHINA = ZoneInfo("Asia/Shanghai")


def bar(day: int, open_: float, high: float, low: float, close: float,
        amount: float | None) -> FactorBar:
    trade_date = date(2026, 9, day)
    return FactorBar("stock:000338.SZ", trade_date, open_, high, low, close,
                     1000.0, amount, "forward_adjusted",
                     "CNY" if amount is not None else "unavailable",
                     datetime.combine(trade_date, time(15, 5), CHINA), "test_daily")


DAY = datetime(2026, 9, 4, 16, 0, tzinfo=CHINA)
BARS = [bar(1, 10, 11, 9, 10, 1000),
        bar(2, 11, 12, 10, 12, 2400),
        bar(3, 10, 13, 9, 11, 1100),
        bar(4, 12, 12, 11, 12, 3600)]


class DailyFactorsV2Test(unittest.TestCase):
    def test_daily_factor_math_and_explicit_range_definition(self) -> None:
        self.assertAlmostEqual(12 / 11 - 1,
                               calculate_factor("overnight_gap", BARS, decision_at=DAY))
        self.assertAlmostEqual(0.0,
                               calculate_factor("intraday_return", BARS, decision_at=DAY))
        range_value = math.sqrt(mean([math.log(13 / 9) ** 2,
                                           math.log(12 / 11) ** 2]) / (4 * math.log(2)))
        self.assertAlmostEqual(range_value, calculate_factor(
            "rolling_range_volatility", BARS, decision_at=DAY, window=2))
        amihud = mean([abs(11 / 12 - 1) / 1100, abs(12 / 11 - 1) / 3600])
        self.assertAlmostEqual(amihud, calculate_factor(
            "amihud_illiquidity", BARS, decision_at=DAY, window=2))
        self.assertAlmostEqual(3600 / mean([2400, 1100]), calculate_factor(
            "relative_amount", BARS, decision_at=DAY, window=2))

    def test_relative_volume_uses_prior_window_only(self) -> None:
        changed = [*BARS[:-1], replace(BARS[-1], volume_shares=2000)]
        self.assertAlmostEqual(2.0, calculate_factor(
            "relative_volume", changed, decision_at=DAY, window=2))

    def test_registry_keeps_unvalidated_and_intraday_data_required(self) -> None:
        catalog = {item["factor_id"]: item for item in factor_catalog_v2()}
        self.assertEqual(9, len(catalog))
        self.assertEqual("daily", catalog["relative_volume"]["frequency"])
        self.assertEqual("daily", catalog["rolling_range_volatility"]["frequency"])
        self.assertIn("high", catalog["rolling_range_volatility"]["required_fields"])
        self.assertEqual("implemented", catalog["amihud_illiquidity"]["availability_status"])
        self.assertEqual("research_unvalidated", catalog["amihud_illiquidity"]["research_status"])
        self.assertTrue(all(not item["active_for_decision"] for item in catalog.values()))
        for factor_id in ("ofi", "queue_imbalance", "intraday_realized_volatility"):
            self.assertEqual("data_required", catalog[factor_id]["availability_status"])
            with self.assertRaisesRegex(ValueError, "intraday data required"):
                calculate_factor(factor_id, BARS, decision_at=DAY)

    def test_history_shortage_returns_none_but_invalid_window_fails(self) -> None:
        self.assertIsNone(calculate_factor("overnight_gap", BARS[:1],
                                           decision_at=DAY))
        self.assertIsNone(calculate_factor("rolling_range_volatility", BARS[:1],
                                           decision_at=DAY, window=2))
        self.assertIsNone(calculate_factor("amihud_illiquidity", BARS[:2],
                                           decision_at=DAY, window=2))
        with self.assertRaisesRegex(ValueError, "window >= 2"):
            calculate_factor("rolling_range_volatility", BARS,
                             decision_at=DAY, window=1)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            calculate_factor("relative_amount", BARS, decision_at=DAY, window=0)

    def test_missing_or_zero_amount_fails_closed_only_when_required(self) -> None:
        no_amount = [replace(item, amount_cny=None, amount_unit="unavailable") for item in BARS]
        self.assertAlmostEqual(12 / 11 - 1,
                               calculate_factor("overnight_gap", no_amount, decision_at=DAY))
        for factor_id in ("amihud_illiquidity", "relative_amount"):
            with self.assertRaisesRegex(ValueError, "requires positive CNY amount"):
                calculate_factor(factor_id, no_amount, decision_at=DAY, window=2)
        with self.assertRaisesRegex(ValueError, "amount_cny"):
            calculate_factor("intraday_return", [*BARS[:-1], replace(BARS[-1], amount_cny=0)],
                             decision_at=DAY)
        with self.assertRaisesRegex(ValueError, "requires verified CNY"):
            calculate_factor("intraday_return", [*BARS[:-1], replace(
                BARS[-1], amount_unit="unavailable")], decision_at=DAY)

    def test_zero_volume_or_bad_prices_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "volume_shares"):
            calculate_factor("intraday_return", [*BARS[:-1], replace(
                BARS[-1], volume_shares=0)], decision_at=DAY)
        with self.assertRaisesRegex(ValueError, "OHLC"):
            calculate_factor("intraday_return", [*BARS[:-1], replace(
                BARS[-1], high=11)], decision_at=DAY)

    def test_trade_dates_and_adjustment_basis_must_be_consistent(self) -> None:
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            calculate_factor("overnight_gap", [BARS[1], BARS[0]], decision_at=DAY)
        with self.assertRaisesRegex(ValueError, "mixed instrument, price basis"):
            calculate_factor("overnight_gap", [BARS[0], replace(
                BARS[1], price_basis="raw_unadjusted")], decision_at=DAY)

    def test_final_bar_must_be_known_at_decision_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "not available"):
            calculate_factor("intraday_return", BARS, decision_at=DAY - timedelta(days=1))
        with self.assertRaisesRegex(ValueError, "before session close"):
            calculate_factor("intraday_return", [*BARS[:-1], replace(
                BARS[-1], available_at=datetime(2026, 9, 4, 14, 59, tzinfo=CHINA))],
                decision_at=DAY)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            calculate_factor("intraday_return", BARS,
                             decision_at=datetime(2026, 9, 4, 16, 0))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            calculate_factor("intraday_return", [*BARS[:-1], replace(
                BARS[-1], available_at=None)], decision_at=DAY)


if __name__ == "__main__":
    unittest.main()
