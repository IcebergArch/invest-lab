from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.stock_chart import HISTORY_LIMIT, build_stock_chart


def days(count: int) -> list[date]:
    result: list[date] = []
    current = date(2025, 1, 1)
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


class StockChartTest(unittest.TestCase):
    def test_first_sixty_session_signal_uses_latest_twenty_closes(self) -> None:
        calendar = days(61)
        closes = [10.0] * 40 + [20.0] * 20 + [5.0]
        chart = build_stock_chart(
            calendar, closes, instrument_id="stock:603993.SH",
            price_basis="qfq_cny", include_strategy_signals=True,
            include_research_forecast=False,
        )
        self.assertEqual(calendar[59].isoformat(), chart["signals"][0]["date"])
        self.assertEqual("buy", chart["signals"][0]["kind"])
        self.assertEqual(1, chart["rule_position"][0]["position"])
        self.assertEqual(1, chart["rule_position"][1]["position"])

    def test_history_signals_and_research_curve_are_dated_and_bounded(self) -> None:
        calendar = days(180)
        closes = [10.0] * 70 + [10.0 + 0.08 * step for step in range(1, 111)]
        chart = build_stock_chart(
            calendar, closes, instrument_id="stock:603993.SH",
            price_basis="qfq_cny", include_strategy_signals=True,
            include_research_forecast=True,
        )
        self.assertEqual(len(calendar), len(chart["history"]))
        self.assertEqual(calendar[-1].isoformat(), chart["asof"])
        self.assertEqual("buy", chart["signals"][0]["kind"])
        positions = {point["date"]: point["position"] for point in chart["rule_position"]}
        self.assertEqual(1, positions[chart["signals"][0]["date"]])
        self.assertEqual("historical_diagnostic_only", chart["signal_rule"]["status"])
        forecast = chart["forecast"]
        self.assertEqual("research_only", forecast["status"])
        self.assertEqual("qfq_cny", forecast["price_basis"])
        self.assertEqual(chart["asof"], forecast["asof"])
        self.assertEqual(list(range(1, 21)), [item["step"] for item in forecast["points"]])
        self.assertGreater(forecast["validation"]["sample_count"], 0)
        self.assertEqual("exploratory", forecast["validation"]["status"])

    def test_loaded_history_is_bounded_for_multi_year_chart_range(self) -> None:
        calendar = days(HISTORY_LIMIT + 20)
        chart = build_stock_chart(
            calendar, [10.0] * len(calendar), instrument_id="stock:603993.SH",
            price_basis="qfq_cny", include_strategy_signals=False,
            include_research_forecast=False,
        )
        self.assertEqual(HISTORY_LIMIT, len(chart["history"]))
        self.assertEqual(calendar[-HISTORY_LIMIT].isoformat(), chart["history"][0]["date"])

    def test_only_prior_bars_can_affect_a_snapshot(self) -> None:
        calendar = days(200)
        closes = [10 + 0.01 * i for i in range(180)]
        earlier = build_stock_chart(
            calendar[:180], closes, instrument_id="stock:603993.SH",
            price_basis="qfq_cny", include_strategy_signals=True,
            include_research_forecast=True,
        )
        later_input = closes + [1000.0] * 20
        replay = build_stock_chart(
            calendar[:180], later_input[:180], instrument_id="stock:603993.SH",
            price_basis="qfq_cny", include_strategy_signals=True,
            include_research_forecast=True,
        )
        self.assertEqual(earlier, replay)

    def test_qlib_chart_keeps_adjusted_prices_separate_from_trade_signals(self) -> None:
        chart = build_stock_chart(
            days(80), [7 + index * 0.01 for index in range(80)],
            instrument_id="stock:600000.SH", price_basis="qlib_adjusted",
            include_strategy_signals=False, include_research_forecast=False,
        )
        self.assertEqual("qlib_adjusted", chart["price_basis"])
        self.assertEqual([], chart["signals"])
        self.assertEqual([], chart["rule_position"])
        self.assertEqual("unavailable", chart["forecast"]["status"])


if __name__ == "__main__":
    unittest.main()
