from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.baostock_source import BaoStockUniverseSnapshot, SOURCE_ID
from quant_lab.models import DailyBar, Instrument
from quant_lab.recommendations import build_recommendations
from quant_lab.storage import MarketStore


ASOF = date(2026, 9, 28)


class ThreeStockRecommendationGateTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = MarketStore(Path(temporary.name) / "market.sqlite3")
        self.store.initialize()
        self.ids = [f"stock:{600000 + i}.SH" for i in range(500)]
        self.instruments = tuple(
            Instrument(key, key[6:], key[6:], "stock", "current_stock", SOURCE_ID,
                       f"sh.{key[6:12]}", "SSE", "qfq")
            for key in self.ids
        )
        self.snapshot = BaoStockUniverseSnapshot(
            "2026-09-29T03:00:00+00:00", "test", 500, 0,
            self.instruments, "raw-hash",
        )
        self.store.upsert_instruments(self.instruments[:4])
        self.run_id = "source-run"
        self.store.start_run(self.run_id, ASOF, ASOF, ["test"])
        self.store.upsert_bars([
            DailyBar(key, ASOF, 10, 11, 9, 10, 1_000_000,
                     50_000_000, "share", "CNY", "qfq", SOURCE_ID, f"hash-{key}")
            for key in self.ids[:4]
        ], run_id=self.run_id)
        self.store.finish_run(self.run_id, "success", 4, 4)
        with self.store.connect() as connection:
            connection.execute("""
                CREATE TABLE baostock_daily_status (
                    instrument_id TEXT NOT NULL,trade_date TEXT NOT NULL,
                    tradestatus INTEGER NOT NULL,is_st INTEGER NOT NULL,
                    raw_amount_cny REAL,payload_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL,PRIMARY KEY(instrument_id,trade_date)
                )
            """)
            connection.executemany("""
                INSERT INTO baostock_daily_status VALUES (?,?,?,?,?,?,?)
            """, [(key, ASOF.isoformat(), 1, 0, 50_000_000, f"hash-{key}", self.run_id)
                   for key in self.ids[:4]])
        groups = ["科技信息", "科技信息", "资源能源", "消费健康"]
        self.candidates = [
            {
                "instrument_id": key, "name": key, "industry_group": group,
                "score": 10 - index,
                "metrics": {"annualized_volatility_20d": 0.2, "max_drawdown_60d": 0.08},
                "latest_bar_lineage": {"source_id": SOURCE_ID, "payload_hash": f"hash-{key}",
                                       "run_id": self.run_id, "adjustment": "qfq"},
            }
            for index, (key, group) in enumerate(zip(self.ids[:4], groups))
        ]
        self.screen = {
            "status": "ready", "screen_version": "stock-risk-return-v1",
            "asof": ASOF.isoformat(), "universe": self.ids,
            "expected_universe_count": 500,
            "universe_snapshot_id": self.snapshot.snapshot_id,
            "data_ready_count": 500, "coverage_rate": 1.0,
            "candidates": self.candidates,
        }
        self.research = {"recommendation_validation": {
            "status": "passed", "validation_type": "stock-selection-walk-forward",
            "universe_snapshot_id": self.snapshot.snapshot_id,
            "screen_version": "stock-risk-return-v1",
            "point_in_time_universe": True, "independent_out_of_sample": True,
            "costs_included": True, "tradability_modeled": True,
            "run_id": "walk-forward-run", "oos_sessions": 126,
            "selection_events": 12, "excess_return_after_costs": 0.02,
            "max_drawdown": -0.20, "data_end": ASOF.isoformat(),
        }}
        self.forecast = {
            "model_id": "model-a",
            "recommendation_validation": {
                "status": "passed", "validation_type": "stock-selection-forecast-oos",
                "universe_snapshot_id": self.snapshot.snapshot_id,
                "model_id": "model-a", "model_revision": "fixed-1",
                "horizon_sessions": 20, "oos_origin_count": 30,
                "mae_return_skill_vs_random_walk": 0.03,
                "data_end": ASOF.isoformat(),
                "predictions": [
                    {"instrument_id": key, "origin_date": ASOF.isoformat(),
                     "expected_return_20d": 0.04, "p10_return_20d": -0.10}
                    for key in self.ids[:4]
                ],
            },
        }

    def report(self):
        return build_recommendations(
            self.store, self.screen, self.research, self.forecast, self.snapshot, ASOF
        )

    def test_current_style_focus_reports_cannot_turn_three_names_into_recommendations(self) -> None:
        self.screen["universe"] = self.ids[:3]
        self.screen["data_ready_count"] = 3
        self.screen["candidates"] = self.candidates[:3]
        self.research = {"universe": self.ids[:3], "backtest": {"prospective_out_of_sample": {
            "status": "pending"
        }}}
        self.forecast = {"model_id": "last-observed-close", "universe_scope": "focus-stock-pilot"}
        result = self.report()
        self.assertEqual("blocked_insufficient_evidence", result["status"])
        self.assertEqual(0, result["recommendation_count"])
        self.assertEqual([], result["recommendations"])
        failed = {item["key"] for item in result["checks"] if not item["passed"]}
        self.assertTrue({"universe_coverage", "marketwide_backtest", "forecast_validation"} <= failed)

    def test_exactly_three_distinct_groups_only_after_all_gates(self) -> None:
        result = self.report()
        self.assertEqual("ready_for_human_review", result["status"])
        self.assertEqual(3, result["recommendation_count"])
        self.assertEqual([self.ids[0], self.ids[2], self.ids[3]],
                         [item["instrument_id"] for item in result["recommendations"]])

    def test_st_status_and_future_prediction_origin_block_publication(self) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE baostock_daily_status SET is_st=1 WHERE instrument_id=?",
                (self.ids[2],),
            )
        result = self.report()
        self.assertEqual([], result["recommendations"])
        self.assertFalse(next(item for item in result["checks"]
                              if item["key"] == "source_status")["passed"])
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE baostock_daily_status SET is_st=0 WHERE instrument_id=?",
                (self.ids[2],),
            )
        self.forecast["recommendation_validation"]["predictions"][0]["origin_date"] = "2026-09-29"
        result = self.report()
        self.assertEqual([], result["recommendations"])
        self.assertFalse(next(item for item in result["checks"]
                              if item["key"] == "forecast_validation")["passed"])


if __name__ == "__main__":
    unittest.main()
