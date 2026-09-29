from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.north_star import score_backtest


class NorthStarTest(unittest.TestCase):
    def test_equal_hold_is_neutral_and_weights_are_equal(self) -> None:
        metrics = {"cagr": 0.03, "max_drawdown": -0.30}
        result = score_backtest(metrics, metrics)
        self.assertEqual(5.0, result["score"])
        self.assertEqual(0.5, result["components"]["return"]["weight"])
        self.assertEqual(0.5, result["components"]["drawdown"]["weight"])

    def test_return_and_drawdown_can_improve_or_worsen_score(self) -> None:
        benchmark = {"cagr": 0.02, "max_drawdown": -0.5}
        better = score_backtest({"cagr": 0.12, "max_drawdown": -0.25}, benchmark)
        worse = score_backtest({"cagr": -0.08, "max_drawdown": -0.75}, benchmark)
        self.assertEqual(7.5, better["score"])
        self.assertEqual(2.5, worse["score"])

    def test_invalid_drawdown_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            score_backtest({"cagr": 0.1, "max_drawdown": -1.2},
                           {"cagr": 0.0, "max_drawdown": -0.2})


if __name__ == "__main__":
    unittest.main()
