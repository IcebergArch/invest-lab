from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.canonical_data import CanonicalClosePanel
from quant_lab.optimizer import (append_optimization_record, list_optimization_records,
                                 optimize_panel)


def panel(tail_factor: float) -> CanonicalClosePanel:
    dates = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(300))
    first = tuple(10 + index * 0.01 for index in range(210))
    second = tuple(first[-1] * tail_factor ** ((index + 1) / 90) for index in range(90))
    other = tuple(12 + index * 0.004 for index in range(300))
    return CanonicalClosePanel(dates, {"stock:a": first + second, "stock:b": other},
                               "forward_adjusted", "CNY/share", "a" * 64)


class OptimizerTest(unittest.TestCase):
    def test_holdout_prices_cannot_change_training_selection(self) -> None:
        rising = optimize_panel(panel(1.4), "sma-trend")
        falling = optimize_panel(panel(0.6), "sma-trend")
        self.assertEqual(rising["selected_candidate_index"], falling["selected_candidate_index"])
        self.assertEqual(rising["candidate_grid"], falling["candidate_grid"])
        self.assertNotEqual(rising["historical_holdout"]["metrics"],
                            falling["historical_holdout"]["metrics"])
        self.assertEqual("retrospective_holdout_only", rising["historical_holdout"]["status"])

    def test_optimization_archive_detects_modified_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "optimizations"
            result = optimize_panel(panel(1.2), "mean-reversion-zscore")
            saved = append_optimization_record(root, result)
            loaded = list_optimization_records(root)
            self.assertEqual(saved["optimization_id"], loaded[0]["optimization_id"])
            self.assertEqual(result["selected_parameters"],
                             loaded[0]["payload"]["selected_parameters"])
            path = Path(saved["path"])
            modified = json.loads(path.read_text(encoding="utf-8"))
            modified["payload"]["selected_candidate_index"] = 99
            path.write_text(json.dumps(modified), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "integrity"):
                list_optimization_records(root)


if __name__ == "__main__":
    unittest.main()
