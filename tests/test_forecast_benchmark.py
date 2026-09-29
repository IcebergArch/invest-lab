from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.canonical_data import CanonicalClosePanel
from quant_lab.forecast import (
    EvaluationConfig, ForecastOutput, ForecastUnavailable, RandomWalkProvider,
)
from quant_lab.forecast_benchmark import (
    append_benchmark_record, evaluate_focus_benchmark, evaluate_panel_benchmark,
    read_benchmark_record,
)
from quant_lab.storage import MarketStore


def panel(n: int = 60) -> CanonicalClosePanel:
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(n))
    return CanonicalClosePanel(
        dates=dates,
        closes={"stock:TEST": tuple(float(100 + i) for i in range(n)),
                "stock:OTHER": tuple(float(50 + i * 0.25) for i in range(n))},
        price_basis="forward_adjusted",
        price_unit="CNY/share",
        input_fingerprint_sha256="a" * 64,
    )


class RecordingProvider:
    name = "recording"
    model_id = "fixed-plus-one"
    model_revision = "v1"

    def __init__(self) -> None:
        self.contexts: list[tuple[float, ...]] = []

    def forecast(self, history, horizon):
        self.contexts.append(tuple(history))
        return ForecastOutput((float(history[-1]) + 1,) * horizon)


class UnavailableProvider:
    name = "optional-unavailable"
    model_id = "weights-required"
    model_revision = "pinned"

    def forecast(self, history, horizon):
        raise ForecastUnavailable("missing_optional_dependency", "Model package absent")


class BrokenProvider:
    name = "broken"
    model_id = "wrong-shape"
    model_revision = "v1"

    def forecast(self, history, horizon):
        raise ValueError("private model path should not be saved")


class ForecastBenchmarkTest(unittest.TestCase):
    def test_same_origins_and_context_only_up_to_each_origin(self) -> None:
        provider = RecordingProvider()
        result = evaluate_panel_benchmark(
            panel(), [provider, RandomWalkProvider()],
            config=EvaluationConfig(horizons=(5, 20), min_context=10, step=10),
        )
        self.assertEqual("ready", result["status"])
        self.assertEqual(["random-walk", "recording"],
                         [item["name"] for item in result["providers"]])
        self.assertEqual(18, len(provider.contexts))
        self.assertEqual([10, 20, 30, 40, 50],
                         [len(context) for context in provider.contexts[:5]])
        self.assertTrue(all(len(context) < 60 for context in provider.contexts))
        baseline, candidate = result["providers"]
        self.assertEqual("same_as_random_walk", candidate["sample_alignment"])
        for horizon in (5, 20):
            coverage = candidate["comparison_coverage"][str(horizon)]
            self.assertEqual(1.0, coverage["fraction"])
            self.assertEqual(baseline["pooled"][str(horizon)]["count"],
                             candidate["pooled"][str(horizon)]["count"])
        self.assertFalse(result["point_in_time_validated"])
        self.assertFalse(result["prospective_out_of_sample"])
        self.assertEqual("unknown", candidate["pretraining_overlap_status"])

    def test_optional_provider_failure_is_isolated_and_not_partially_scored(self) -> None:
        result = evaluate_panel_benchmark(
            panel(), [RandomWalkProvider(), UnavailableProvider(), BrokenProvider()],
            config=EvaluationConfig(horizons=(5,), min_context=10, step=10),
        )
        self.assertEqual("ready", result["providers"][0]["status"])
        self.assertEqual("missing_optional_dependency", result["providers"][1]["status"])
        self.assertEqual(0, result["providers"][1]["pooled"]["5"]["count"])
        self.assertEqual(0.0, result["providers"][1]["comparison_coverage"]["5"]["fraction"])
        self.assertEqual("provider_runtime_error", result["providers"][2]["status"])
        self.assertEqual("ValueError", result["providers"][2]["reason"])
        self.assertNotIn("private model path", json.dumps(result))

    def test_record_is_hash_verified_and_append_only(self) -> None:
        result = evaluate_panel_benchmark(
            panel(), [RandomWalkProvider()],
            config=EvaluationConfig(horizons=(5,), min_context=10, step=10),
        )
        with tempfile.TemporaryDirectory() as directory:
            saved = append_benchmark_record(directory, result)
            path = Path(saved["path"])
            self.assertEqual(result, read_benchmark_record(path)["payload"])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["payload"]["providers"][0]["pooled"]["5"]["mae_return"] = 0
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "integrity"):
                read_benchmark_record(path)

    def test_rejects_wrong_price_basis_and_missing_baseline(self) -> None:
        original = panel()
        raw = CanonicalClosePanel(original.dates, original.closes, "raw_unadjusted",
                                  original.price_unit, original.input_fingerprint_sha256)
        with self.assertRaisesRegex(ValueError, "adjusted"):
            evaluate_panel_benchmark(raw)
        with self.assertRaisesRegex(ValueError, "random-walk"):
            evaluate_panel_benchmark(original, [RecordingProvider()])

    def test_focus_database_uses_qfq_when_raw_and_qfq_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "market.sqlite3"
            with sqlite3.connect(db) as connection:
                connection.executescript("""
                    CREATE TABLE instruments(instrument_id TEXT, family TEXT, asset_type TEXT);
                    CREATE TABLE daily_bars(
                        instrument_id TEXT, trade_date TEXT, open REAL, high REAL,
                        low REAL, close REAL, volume REAL, amount REAL,
                        volume_unit TEXT, amount_unit TEXT, adjustment TEXT,
                        source_id TEXT, payload_hash TEXT, run_id TEXT, fetched_at TEXT
                    );
                """)
                connection.execute("INSERT INTO instruments VALUES ('stock:TEST','focus_stock','stock')")
                for offset in range(4):
                    day = (date(2026, 1, 1) + timedelta(days=offset)).isoformat()
                    for adjustment, close in (("none", float(20 + offset)),
                                              ("qfq", float(10 + offset))):
                        connection.execute(
                            "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("stock:TEST", day, close, close, close, close,
                             100.0, 1000.0, "share", "CNY", adjustment,
                             "test", "a" * 64, "run-1", "2026-01-05T00:00:00+00:00"),
                        )
            result = evaluate_focus_benchmark(
                MarketStore(db), [RandomWalkProvider()],
                config=EvaluationConfig(horizons=(1,), min_context=2, step=1),
            )
            self.assertEqual("ready", result["status"])
            self.assertEqual("forward_adjusted", result["price_basis"])
            self.assertEqual(2, result["providers"][0]["pooled"]["1"]["count"])
            self.assertEqual(11.0,
                             result["providers"][0]["per_stock"][0]["records"][0]["origin_close"])


if __name__ == "__main__":
    unittest.main()
