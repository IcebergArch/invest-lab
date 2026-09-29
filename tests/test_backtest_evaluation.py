from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.backtest import period_metrics, run_buy_and_hold
from quant_lab.models import DailyBar, Instrument
from quant_lab.research import research_report, write_research_report
from quant_lab.storage import MarketStore


class BuyAndHoldAccountingTest(unittest.TestCase):
    def test_next_close_entry_avoids_earning_pre_entry_jump(self) -> None:
        dates = [date(2026, 1, day) for day in (1, 2, 3)]
        result = run_buy_and_hold(dates, {"asset": [100.0, 200.0, 220.0]},
                                  cost_rate=0.01)
        self.assertAlmostEqual(1.0, result.points[0].equity)
        self.assertAlmostEqual(0.99, result.points[1].equity)
        self.assertAlmostEqual(1.089, result.points[2].equity)
        self.assertEqual([0.0, 1.0, 0.0], [point.turnover for point in result.points])

    def test_weights_drift_without_unstated_rebalancing(self) -> None:
        dates = [date(2026, 1, day) for day in (1, 2, 3)]
        result = run_buy_and_hold(dates,
                                  {"a": [100.0, 100.0, 200.0],
                                   "b": [100.0, 100.0, 100.0]}, cost_rate=0.0)
        self.assertAlmostEqual(1.5, result.points[-1].equity)
        self.assertAlmostEqual(2.0 / 3.0, result.points[-1].weights["a"])
        self.assertAlmostEqual(1.0, result.metrics["turnover"])
        recent = period_metrics(result, 1, 2)
        self.assertAlmostEqual(0.5, recent["total_return"])
        self.assertAlmostEqual(0.0, recent["turnover"])


class ResearchEvidenceTest(unittest.TestCase):
    def test_report_emits_aligned_index_reference_and_readable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            stock_id = "stock:000001.SZ"
            index_id = "index:000300.SH"
            store.upsert_instruments([
                Instrument(stock_id, "000001.SZ", "样本股", "stock", "focus_stock",
                           "test", "0.000001", "SZSE", "qfq"),
                Instrument(index_id, "000300.SH", "沪深300", "index", "csi",
                           "test", "1.000300", "SSE", "none"),
            ])
            first = date(2026, 1, 1)
            dates = [first + timedelta(days=day) for day in range(4)]
            bars = []
            for instrument_id, closes, adjustment in (
                (stock_id, [100.0, 200.0, 220.0, 242.0], "qfq"),
                (index_id, [100.0, 110.0, 121.0, 133.1], "none"),
            ):
                for trade_date, close in zip(dates, closes):
                    bars.append(DailyBar(
                        instrument_id=instrument_id, trade_date=trade_date,
                        open=close, high=close, low=close, close=close,
                        volume=100.0, amount=1000.0, volume_unit="lot",
                        amount_unit="CNY", adjustment=adjustment,
                        source_id="test", payload_hash=f"{instrument_id}-{trade_date}",
                    ))
            store.upsert_bars(bars)
            report = research_report(store, "sma-trend", dates[-1], cost_rate=0.0)
            backtest = report["backtest"]
            self.assertEqual("available", backtest["benchmarks"]["csi300_price_reference"]["status"])
            self.assertAlmostEqual(0.21, backtest["benchmarks"]["csi300_price_reference"]
                                   ["metrics"]["total_return"])
            self.assertAlmostEqual(0.21, backtest["benchmarks"]["focus_equal_weight_hold"]
                                   ["metrics"]["total_return"])
            self.assertEqual("insufficient_history", backtest["retrospective_split"]["status"])
            self.assertEqual(64, len(backtest["input_fingerprint_sha256"]))

            path = Path(directory) / "research.json"
            write_research_report(report, path)
            self.assertEqual(report["asof"], json.loads(path.read_text())["asof"])
            markdown = (Path(directory) / "backtest.md").read_text()
            self.assertIn("沪深300价格指数", markdown)
            self.assertIn("真正时间外样本：**尚无**", markdown)


if __name__ == "__main__":
    unittest.main()
