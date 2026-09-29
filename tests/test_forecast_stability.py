from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.forecast_stability import (
    StabilityCriteria, _scope_filter, append_stability_record,
    evaluate_forecast_stability, read_stability_record,
)


def benchmark(*, bad_stock: str | None = None, bad_event: bool = False) -> dict:
    baseline = []
    model = []
    for stock in ("stock:A", "stock:B"):
        reference_rows = []
        model_rows = []
        for year in (2021, 2022):
            for month in (1, 2, 3):
                origin = f"{year}-{month:02d}-01"
                target = f"{year}-{month:02d}-06"
                row = {"origin_date": origin, "target_date": target,
                       "horizon_sessions": 5, "origin_close": 100.0,
                       "actual_close": 110.0, "absolute_return_error": 0.1,
                       "direction_correct": True, "interval_10_90_covered": None}
                reference_rows.append(row)
                candidate = dict(row)
                candidate["absolute_return_error"] = (
                    0.5 if ((bad_stock == stock) or
                            (bad_event and month == 2)) else 0.05)
                model_rows.append(candidate)
        baseline.append({"instrument_id": stock, "records": reference_rows})
        model.append({"instrument_id": stock, "records": model_rows})
    return {
        "benchmark_version": "canonical-walk-forward-v1",
        "experiment_key_sha256": "a" * 64,
        "input_fingerprint_sha256": "b" * 64,
        "price_basis": "forward_adjusted",
        "configuration": {"horizons": [5]},
        "point_in_time_validated": True,
        "prospective_out_of_sample": True,
        "providers": [
            {"name": "random-walk", "model_revision": "v1", "status": "ready",
             "per_stock": baseline},
            {"name": "test-model", "model_revision": "frozen-v1", "status": "ready",
             "per_stock": model},
        ],
    }


def evidence(*, scope: dict | None = None, declared_at: str = "2020-01-01T00:00:00+00:00",
             regimes: dict[str, str] | None = None) -> dict:
    _, scope_hash = _scope_filter(scope)
    return {
        "record_id": "validated-record-1",
        "declaration_record_id": scope["declaration_record_id"] if scope else "broad-protocol-1",
        "source_experiment_key_sha256": "a" * 64,
        "source_input_fingerprint_sha256": "b" * 64,
        "model_name": "test-model", "model_revision": "frozen-v1",
        "horizon_sessions": 5,
        "declared_at": declared_at,
        "model_frozen_at": "2020-01-01T00:00:00+00:00",
        "validated_at": "2022-04-01T00:00:00+00:00",
        "first_origin_date": "2021-01-01", "last_target_date": "2022-03-06",
        "point_in_time_validated": True,
        "prospective_out_of_sample": True,
        "scope_sha256": scope_hash,
        "stress_taxonomy_sha256": (hashlib.sha256(json.dumps(
            regimes, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode()).hexdigest() if regimes else None),
        "stress_taxonomy_declared_at": "2020-01-01T00:00:00+00:00",
    }


class ForecastStabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.criteria = StabilityCriteria(
            min_core_samples=4, min_stocks=1, min_years=2, min_blocks=2,
            min_coverage=0.9, min_stress_samples=2, min_stress_blocks=2,
            min_stress_regimes=2, min_samples_per_stress_regime=2,
            bootstrap_draws=100, seed=7)

    def evaluate(self, data: dict, **kwargs) -> dict:
        return evaluate_forecast_stability(data, "test-model", criteria=self.criteria, **kwargs)

    def test_event_exclusion_cannot_rescue_negative_all_case_performance(self) -> None:
        data = benchmark(bad_event=True)
        result = self.evaluate(data, event_stress_days=["2021-02-03", "2022-02-03"],
                               validation_evidence=evidence())
        item = result["horizons"]["5"]
        self.assertEqual("coarse_or_unstable", item["tier"])
        self.assertGreater(item["core"]["paired_mae_return_skill_vs_random_walk"], 0)
        self.assertLess(item["all_cases"]["paired_mae_return_skill_vs_random_walk"], 0)
        self.assertEqual(4, item["retrospectively_excluded_from_core"])

    def test_broad_grade_needs_stress_and_prospective_validation(self) -> None:
        data = benchmark()
        event_days = ["2021-02-03", "2022-02-03"]
        regimes = {"2021-02-03": "policy", "2022-02-03": "liquidity"}
        without_evidence = self.evaluate(data, event_stress_days=event_days)
        self.assertEqual("coarse_or_unstable", without_evidence["horizons"]["5"]["tier"])
        without_stress = self.evaluate(data, validation_evidence=evidence(),
                                       validation_evidence_verified=True)
        self.assertEqual("coarse_or_unstable", without_stress["horizons"]["5"]["tier"])
        without_provenance = self.evaluate(
            data, event_stress_days=event_days, event_stress_regimes=regimes,
            validation_evidence=evidence(regimes=regimes))
        self.assertEqual("coarse_or_unstable", without_provenance["horizons"]["5"]["tier"])
        result = self.evaluate(data, event_stress_days=event_days,
                               event_stress_regimes=regimes,
                               event_taxonomy_verified=True,
                               validation_evidence_verified=True,
                               validation_evidence=evidence(regimes=regimes))
        self.assertEqual("broadly_stable", result["horizons"]["5"]["tier"])
        self.assertTrue(result["horizons"]["5"]["event_stress_check_passed"])
        same_regime = {day: "policy" for day in event_days}
        one_type = self.evaluate(data, event_stress_days=event_days,
                                 event_stress_regimes=same_regime,
                                 event_taxonomy_verified=True,
                                 validation_evidence_verified=True,
                                 validation_evidence=evidence(regimes=same_regime))
        self.assertEqual("coarse_or_unstable", one_type["horizons"]["5"]["tier"])
        self.assertFalse(one_type["horizons"]["5"]["event_taxonomy_check_passed"])

    def test_correlated_stocks_share_calendar_year_bootstrap_blocks(self) -> None:
        result = self.evaluate(benchmark())
        item = result["horizons"]["5"]["all_cases"]
        self.assertEqual(2, item["block_count"])
        self.assertEqual(4, item["stock_year_block_count"])
        stricter = StabilityCriteria(min_core_samples=4, min_stocks=1, min_years=2,
                                     min_blocks=3, min_stress_samples=2,
                                     min_stress_blocks=2, min_stress_regimes=2,
                                     bootstrap_draws=100)
        audited = evaluate_forecast_stability(benchmark(), "test-model", criteria=stricter)
        self.assertFalse(audited["horizons"]["5"]["broad_statistical_check_passed"])

    def test_scope_needs_real_filter_prior_declaration_and_matching_validation(self) -> None:
        data = benchmark(bad_stock="stock:B")
        scope = {"declaration_record_id": "scope-1",
                 "declared_at": "2020-01-01T00:00:00+00:00",
                 "instrument_ids": ["stock:A"]}
        with self.assertRaisesRegex(ValueError, "timestamp"):
            self.evaluate(data, predeclared_feasible_scope={"declaration_record_id": "scope-1"})
        absent = self.evaluate(data, predeclared_feasible_scope=scope)
        self.assertEqual("coarse_or_unstable", absent["horizons"]["5"]["tier"])
        mismatched = evidence(scope=scope)
        mismatched["scope_sha256"] = "0" * 64
        self.assertEqual("coarse_or_unstable", self.evaluate(
            data, predeclared_feasible_scope=scope,
            validation_evidence=mismatched)["horizons"]["5"]["tier"])
        validated = self.evaluate(data, predeclared_feasible_scope=scope,
                                  validation_evidence=evidence(scope=scope),
                                  validation_evidence_verified=True,
                                  scope_declaration_verified=True)
        item = validated["horizons"]["5"]
        self.assertEqual("conditional_scope_only", item["tier"])
        self.assertEqual(6, item["scope_all_cases"]["count"])
        self.assertTrue(item["scope_is_strict_subset"])
        self.assertLess(item["all_cases"]["paired_mae_return_skill_vs_random_walk"], 0)
        late_scope = dict(scope, declared_at="2021-02-01T00:00:00+00:00")
        late = self.evaluate(data, predeclared_feasible_scope=late_scope,
                             validation_evidence=evidence(scope=late_scope,
                                                          declared_at=late_scope["declared_at"]),
                             validation_evidence_verified=True,
                             scope_declaration_verified=True)
        self.assertEqual("coarse_or_unstable", late["horizons"]["5"]["tier"])
        self.assertIn("validation_not_prior_and_matured",
                      late["horizons"]["5"]["scope_validation_reasons"])

    def test_scope_still_counts_event_losses_inside_scope(self) -> None:
        data = benchmark(bad_event=True)
        scope = {"declaration_record_id": "scope-1",
                 "declared_at": "2020-01-01T00:00:00+00:00",
                 "instrument_ids": ["stock:A"]}
        result = self.evaluate(data, event_stress_days=["2021-02-03", "2022-02-03"],
                               predeclared_feasible_scope=scope,
                               validation_evidence=evidence(scope=scope))
        self.assertEqual("coarse_or_unstable", result["horizons"]["5"]["tier"])
        self.assertFalse(result["horizons"]["5"]["scope_statistical_check_passed"])

    def test_record_and_markdown_integrity(self) -> None:
        result = self.evaluate(benchmark())
        with tempfile.TemporaryDirectory() as directory:
            paths = append_stability_record(directory, result)
            path = Path(paths["json_path"])
            self.assertEqual(result, read_stability_record(path)["payload"])
            self.assertIn("全部配对样本", Path(paths["report_path"]).read_text())
            record = json.loads(path.read_text())
            record["payload"]["horizons"]["5"]["tier"] = "broadly_stable"
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "integrity"):
                read_stability_record(path)


if __name__ == "__main__":
    unittest.main()
