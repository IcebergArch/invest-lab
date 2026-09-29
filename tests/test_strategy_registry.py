from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.strategy_registry import append_strategy_snapshot, list_strategy_snapshots


def sample_report() -> dict:
    return {
        "strategy_id": "sma-trend", "strategy_version": "1", "asof": "2026-09-29",
        "strategy_parameters": {"fast_window": 20, "slow_window": 60},
        "cost_rate": 0.001, "universe": ["stock:000338.SZ"],
        "backtest": {
            "metrics": {"total_return": 0.1, "max_drawdown": -0.2},
            "input_fingerprint_sha256": "a" * 64,
            "equity_curve": [{"date": "2026-09-29", "equity": 1.1}],
        },
    }


class StrategyRegistryTest(unittest.TestCase):
    def test_append_read_and_detect_modified_research(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "strategy-runs"
            saved = append_strategy_snapshot(
                root, sample_report(), source_run_id="b" * 32, origin="pipeline_run",
            )
            records = list_strategy_snapshots(root)
            self.assertEqual(1, len(records))
            self.assertEqual(saved["record_id"], records[0]["record_id"])
            self.assertEqual("b" * 32, records[0]["source_run_id"])
            self.assertEqual(0.1, records[0]["research"]["backtest"]["metrics"]["total_return"])
            path = Path(saved["path"])
            changed = json.loads(path.read_text(encoding="utf-8"))
            changed["research"]["backtest"]["metrics"]["total_return"] = 0.9
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid strategy snapshot"):
                list_strategy_snapshots(root)

    def test_reject_incomplete_report_before_creating_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "strategy-runs"
            broken = sample_report()
            broken["backtest"]["equity_curve"] = []
            with self.assertRaises(ValueError):
                append_strategy_snapshot(root, broken, origin="baseline_capture")
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
