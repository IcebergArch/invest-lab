from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.algorithm_catalog import REGISTRY_VERSION, algorithm_catalog
from quant_lab.chronos_adapter import CHRONOS_BOLT_TINY_MODEL_ID
from quant_lab.forecast_benchmark import BENCHMARK_VERSION, append_benchmark_record
from quant_lab.forecast_qlib import (
    BENCHMARK_VERSION as QLIB_BENCHMARK_VERSION, append_qlib_benchmark_record,
)
from quant_lab.forecast_stability import STABILITY_VERSION, append_stability_record


def by_id(root: Path) -> dict[str, dict]:
    return {item["algorithm_id"]: item for item in algorithm_catalog(root)}


def chronos_payload(version: str, status: str = "partial_failure") -> dict:
    return {
        "benchmark_version": version,
        "research_only": True,
        "providers": [{
            "name": "chronos-bolt-tiny",
            "model_id": CHRONOS_BOLT_TINY_MODEL_ID,
            "model_revision": "a" * 40,
            "status": status,
            "pooled": {"5": {"status": "ready", "count": 11},
                       "20": {"status": "ready", "count": 9}},
            "per_stock": [{"private_forecast_row": "must-not-leak"}],
        }],
    }


def stability_payload(source_id: str) -> dict:
    return {
        "stability_version": STABILITY_VERSION,
        "model_name": "chronos-bolt-tiny",
        "model_revision": "a" * 40,
        "source_benchmark_record_id": source_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "price_basis": "qlib_adjusted",
        "horizons": {"5": {
            "tier": "coarse_or_unstable",
            "all_cases": {"count": 11,
                          "paired_mae_return_skill_vs_random_walk": -0.02,
                          "block_bootstrap_95pct": [None, None]},
            "core": {"count": 10}, "event_stress": {"count": 1},
            "broad_validation_reasons": [], "scope_validation_reasons": [],
        }},
        "limitations": [],
    }


class AlgorithmCatalogTest(unittest.TestCase):
    def test_registers_families_without_inventing_strategy_bindings_or_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rows = by_id(Path(temporary))
        self.assertEqual("1", REGISTRY_VERSION)
        self.assertEqual({
            "random-walk", "momentum-20-extrapolation", "tft", "timesfm-2.5",
            "chronos", "chronos-bolt-tiny", "chronos-2", "moirai", "moirai-2",
            "tiny-time-mixers", "granite-ts", "time-moe", "timer",
        }, set(rows))
        self.assertEqual("adapter_only", rows["timesfm-2.5"]["status"])
        self.assertEqual("candidate", rows["moirai-2"]["status"])
        self.assertEqual("not_evaluated", rows["chronos-bolt-tiny"]["research_status"])
        self.assertTrue(all(item["strategy_ids"] == [] for item in rows.values()))
        self.assertTrue(all(item["role"] == "forecast_provider" for item in rows.values()))

    def test_verified_partial_run_and_linked_audit_are_exposed_then_tamper_revoked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved_qlib = append_qlib_benchmark_record(
                root / "forecast-experiments-qlib",
                chronos_payload(QLIB_BENCHMARK_VERSION),
            )
            saved_focus = append_benchmark_record(
                root / "forecast-experiments",
                chronos_payload(BENCHMARK_VERSION, "ready"),
            )
            saved_audit = append_stability_record(
                root / "forecast-stability", stability_payload(saved_qlib["record_id"]),
            )
            first = by_id(root)["chronos-bolt-tiny"]
            self.assertEqual("experimented", first["status"])
            self.assertEqual("archived_experiment", first["research_status"])
            self.assertEqual([saved_qlib["record_id"], saved_focus["record_id"],
                              saved_audit["record_id"]],
                             [item["record_id"] for item in first["evidence"]])
            self.assertEqual("partial_failure", first["evidence"][0]["provider_status"])
            self.assertEqual(11, first["evidence"][0]["horizons"]["5"]["sample_count"])
            self.assertEqual([], first["strategy_ids"])
            self.assertNotIn("must-not-leak", json.dumps(first))
            self.assertNotIn(str(root), json.dumps(first))

            # A same-size edit must invalidate the cache and fail the SHA check.
            path = Path(saved_qlib["path"])
            damaged = bytearray(path.read_bytes())
            damaged[-8] ^= 1
            path.write_bytes(damaged)
            after = by_id(root)["chronos-bolt-tiny"]
            self.assertEqual([saved_focus["record_id"]],
                             [item["record_id"] for item in after["evidence"]])

    def test_unpinned_or_wrong_model_does_not_count_as_evaluated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = chronos_payload(BENCHMARK_VERSION)
            payload["providers"][0]["model_revision"] = "main"
            append_benchmark_record(root / "forecast-experiments", payload)
            payload["providers"][0]["model_revision"] = "a" * 40
            payload["providers"][0]["model_id"] = "unregistered-weight"
            append_benchmark_record(root / "forecast-experiments", payload)
            row = by_id(root)["chronos-bolt-tiny"]
            self.assertEqual("adapter_only", row["status"])
            self.assertEqual([], row["evidence"])


if __name__ == "__main__":
    unittest.main()
