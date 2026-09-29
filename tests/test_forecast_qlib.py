from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from quant_lab.forecast import EvaluationConfig, RandomWalkProvider
from quant_lab.forecast_qlib import (
    append_qlib_benchmark_record, evaluate_qlib_cohort,
    read_qlib_benchmark_record, select_stratified_cohort,
)
from quant_lab.qlib_archive import QlibArchiveError
from quant_lab.qlib_local import publish_release_tree
from test_qlib_archive import feature, fixture_files, write_release


class QlibForecastBenchmarkTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        files = fixture_files()
        files["qlib_bin/features/sh600000/close.day.bin"] = feature(0, 10, 11, 12)
        self.archive, self.manifest = write_release(self.directory, files)
        self.root = self.directory / "published"
        publish_release_tree(self.archive, self.manifest, "2000-01-06", self.root)

    def test_cohort_is_stable_across_order_and_includes_ended_interval(self) -> None:
        instruments = {
            "SH600001": ((date(2000, 1, 1), date(2026, 9, 28)),),
            "SH600002": ((date(2000, 1, 1), date(2026, 9, 28)),),
            "SH600003": ((date(2000, 1, 1), date(2026, 9, 28)),),
            "SH600004": ((date(2000, 1, 1), date(2026, 9, 28)),),
            "SZ000003": ((date(2000, 1, 1), date(2010, 1, 1)),),
            "BJ430000": ((date(2023, 1, 1), date(2026, 9, 28)),),
        }
        target = date(2026, 9, 28)
        first, population = select_stratified_cohort(instruments, target, per_stratum=2)
        reversed_items = dict(reversed(list(instruments.items())))
        second, _ = select_stratified_cohort(reversed_items, target, per_stratum=2)
        self.assertEqual(first, second)
        self.assertEqual(4, len(first))
        self.assertIn("SZ000003", first)
        self.assertIn("BJ430000", first)
        self.assertEqual(4, population["SH/pre2010/active"])

    def test_per_stock_windows_keep_short_ended_stock_without_common_intersection(self) -> None:
        result = evaluate_qlib_cohort(
            self.root, self.manifest, "2000-01-06",
            providers=[RandomWalkProvider()],
            config=EvaluationConfig(horizons=(1,), min_context=2, step=1),
        )
        self.assertEqual("ready", result["status"])
        self.assertEqual("qlib_adjusted", result["price_basis"])
        self.assertEqual("valid_stock_close_observations", result["horizon_unit"])
        self.assertEqual(2, result["cohort_selection"]["selected_count"])
        self.assertEqual(1, result["providers"][0]["pooled"]["1"]["count"])
        self.assertEqual(1, result["providers"][0]["pooled"]["1"]["contributing_stocks"])
        self.assertEqual({"ready", "insufficient_history"},
                         {item["status"] for item in result["providers"][0]["per_stock"]})
        self.assertTrue(any(item["stratum"].endswith("ended") for item in result["cohort"]))
        self.assertTrue(all(item["file_sha256"].startswith("sha256:")
                            for item in result["cohort"]))
        self.assertFalse(result["point_in_time_validated"])
        self.assertIn("python_version", result["runtime"])

    def test_source_feature_hash_is_checked_and_archive_detects_tamper(self) -> None:
        result = evaluate_qlib_cohort(
            self.root, self.manifest, "2000-01-06",
            providers=[RandomWalkProvider()],
            config=EvaluationConfig(horizons=(1,), min_context=2, step=1),
        )
        saved = append_qlib_benchmark_record(self.directory / "records", result)
        path = Path(saved["path"])
        self.assertEqual(result, read_qlib_benchmark_record(path)["payload"])
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        record = json.loads(gzip.decompress(path.read_bytes()))
        record["payload"]["providers"][0]["pooled"]["1"]["mae_return"] = 0
        path.write_bytes(gzip.compress(json.dumps(record).encode(), mtime=0))
        with self.assertRaisesRegex(ValueError, "integrity"):
            read_qlib_benchmark_record(path)
        feature_path = self.root / "qlib_bin/features/sh600000/close.day.bin"
        with feature_path.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(QlibArchiveError):
            evaluate_qlib_cohort(
                self.root, self.manifest, "2000-01-06",
                providers=[RandomWalkProvider()],
                config=EvaluationConfig(horizons=(1,), min_context=2, step=1),
            )

    def test_model_failure_retains_first_origin_coordinates(self) -> None:
        class InvalidProvider:
            name = "invalid"
            model_id = "invalid-output"
            model_revision = "1"

            def forecast(self, history, horizon):
                raise ValueError("model returned a nonpositive forecast")

        result = evaluate_qlib_cohort(
            self.root, self.manifest, "2000-01-06",
            providers=[RandomWalkProvider(), InvalidProvider()],
            config=EvaluationConfig(horizons=(1,), min_context=2, step=1),
        )
        failed = next(item for item in result["providers"][1]["per_stock"]
                      if item["status"] == "provider_runtime_error")
        self.assertEqual("ValueError", failed["reason"])
        self.assertEqual({"origin_date": "2000-01-05",
                          "horizon_sessions": 1, "history_count": 2},
                         failed["first_failure"])
        self.assertEqual([], failed["records"])


if __name__ == "__main__":
    unittest.main()
