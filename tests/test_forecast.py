from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.forecast import (
    EvaluationConfig,
    Momentum20Provider,
    RandomWalkProvider,
    TimesFM25Provider,
    evaluate_forecasts,
)


def sessions(n: int) -> list[date]:
    return [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]


class ForecastTest(unittest.TestCase):
    def test_rolling_origins_never_receive_future_or_post_asof_prices(self) -> None:
        seen = []

        def fake_runner(history, horizon):
            seen.append((tuple(history), horizon))
            value = history[-1]
            return (
                [[value + 1] * horizon],
                [[[value, value, value, value, value, value + 1,
                   value + 1, value + 1, value + 1, value + 2]
                  for _ in range(horizon)]],
            )

        provider = TimesFM25Provider(max_context=32, runner=fake_runner)
        dates = sessions(8)
        prices = [100, 101, 102, 103, 104, 105, 106, 100000]
        report = evaluate_forecasts(
            dates, prices, provider, "stock:TEST", asof=dates[6],
            config=EvaluationConfig(horizons=(2,), min_context=3, step=2),
        )
        self.assertEqual("ready", report["status"])
        self.assertEqual(2, len(seen))
        self.assertEqual([3, 5], [len(item[0]) for item in seen])
        self.assertEqual([102, 104], [item[0][-1] for item in seen])
        self.assertEqual([dates[2].isoformat(), dates[4].isoformat()],
                         [item["origin_date"] for item in report["records"]])
        self.assertEqual([dates[4].isoformat(), dates[6].isoformat()],
                         [item["target_date"] for item in report["records"]])
        self.assertTrue(all(100000 not in item[0] for item in seen))
        self.assertAlmostEqual(1.0, report["summary"]["2"]["interval_10_90_coverage"])

    def test_random_walk_has_auditable_5_and_20_session_baselines(self) -> None:
        dates = sessions(50)
        prices = [float(100 + i) for i in range(50)]
        report = evaluate_forecasts(
            dates, prices, RandomWalkProvider(), "stock:TEST",
            config=EvaluationConfig(horizons=(5, 20), min_context=10, step=10),
        )
        self.assertEqual("ready", report["status"])
        self.assertEqual(4, report["summary"]["5"]["count"])
        self.assertEqual(3, report["summary"]["20"]["count"])
        self.assertEqual(5.0, report["summary"]["5"]["mae_close"])
        self.assertEqual(20.0, report["summary"]["20"]["mae_close"])
        self.assertEqual(0.0, report["summary"]["5"]["mae_skill_vs_random_walk"])
        self.assertIsNone(report["summary"]["5"]["directional_hit_rate"])
        self.assertEqual(0.0, report["summary"]["5"]["directional_coverage"])
        self.assertTrue(all(item["origin_date"] < item["target_date"]
                            for item in report["records"]))

    def test_momentum_baseline_has_direction_and_same_random_walk_comparator(self) -> None:
        report = evaluate_forecasts(
            sessions(50), [100.0 * 1.01 ** i for i in range(50)],
            Momentum20Provider(), "stock:TEST",
            config=EvaluationConfig(horizons=(5, 20), min_context=21, step=10),
        )
        self.assertEqual("ready", report["status"])
        self.assertEqual(1.0, report["summary"]["5"]["directional_hit_rate"])
        self.assertEqual(1.0, report["summary"]["5"]["directional_coverage"])
        self.assertAlmostEqual(1.0, report["summary"]["5"]["mae_skill_vs_random_walk"])

    def test_missing_timesfm_dependency_is_a_status_without_partial_results(self) -> None:
        provider = TimesFM25Provider()
        with patch("quant_lab.forecast.metadata.version",
                   side_effect=metadata.PackageNotFoundError("timesfm")):
            report = evaluate_forecasts(
                sessions(6), [100.0] * 6, provider, "stock:TEST",
                config=EvaluationConfig(horizons=(1,), min_context=2, step=1),
            )
        self.assertEqual("missing_optional_dependency", report["status"])
        self.assertEqual([], report["records"])
        self.assertIn("timesfm", report["reason"])

    def test_timesfm_requires_an_immutable_weight_revision(self) -> None:
        with patch("quant_lab.forecast.metadata.version", return_value="2.0.2"):
            report = evaluate_forecasts(
                sessions(6), [100.0] * 6, TimesFM25Provider(model_revision="main"),
                "stock:TEST", config=EvaluationConfig(horizons=(1,), min_context=2),
            )
        self.assertEqual("missing_model_revision", report["status"])
        self.assertEqual([], report["records"])

    def test_malformed_model_output_is_rejected(self) -> None:
        provider = TimesFM25Provider(runner=lambda history, horizon: ([[1.0]], [[[0.0] * 10]]))
        with self.assertRaisesRegex(ValueError, "one finite positive price per step"):
            evaluate_forecasts(
                sessions(6), [100.0] * 6, provider, "stock:TEST",
                config=EvaluationConfig(horizons=(2,), min_context=2, step=1),
            )


if __name__ == "__main__":
    unittest.main()
