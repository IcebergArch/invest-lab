from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.forecast_evidence import evaluate_record
from quant_lab.typed_routing import classify_prices
from quant_lab.typed_strategy import TypedRouterStrategy
from quant_lab.strategy_catalog import get_strategy_definition


def frozen() -> dict:
    return {"record_id": "origin-a", "generated_at": "2026-09-30T08:00:00Z",
            "payload": {"report": {
                "instrument_id": "stock:000001.SZ", "asof": "2026-09-30",
                "sources": [{"latest_payload_hash": "anchor-hash"}],
                "chart": {"history": [{"date": "2026-09-30", "close": 10.0}],
                          "forecast": {"model_id": "test", "model_version": "1",
                                       "price_basis": "qfq_cny",
                                       "points": [{"step": i, "median": 10.0 + i * .1}
                                                  for i in range(1, 21)]}}}}}


def rows(count: int) -> list[dict]:
    return ([{"trade_date": "2026-09-30", "close": 10.0,
              "payload_hash": "anchor-hash"}]
            + [{"trade_date": f"2026-10-{i + 1:02d}", "close": 10.0 + i * .2,
                "payload_hash": f"future-{i}"} for i in range(1, count + 1)])


class ForecastEvidenceTest(unittest.TestCase):
    def test_forward_origin_is_pending_then_scored_only_as_facts_arrive(self) -> None:
        first = evaluate_record(frozen(), rows(0))
        self.assertEqual(("pending", 0), (first["status"], first["observed_steps"]))
        partial = evaluate_record(frozen(), rows(2))
        self.assertEqual(("pending", 2), (partial["status"], partial["observed_steps"]))
        self.assertAlmostEqual(.01, partial["points"][0]["absolute_return_error"])
        complete = evaluate_record(frozen(), rows(20))
        self.assertEqual(("complete", 20), (complete["status"], complete["observed_steps"]))

    def test_late_or_revised_origin_is_not_forward_evidence(self) -> None:
        late = frozen()
        late["generated_at"] = "2026-10-01T08:00:00Z"
        self.assertEqual("late_issued_not_forward", evaluate_record(late, rows(20))["status"])
        revised = rows(20)
        revised[0]["payload_hash"] = "changed"
        self.assertEqual("anchor_source_revision", evaluate_record(frozen(), revised)["status"])
        revised[0]["close"] = 9.0
        self.assertEqual("anchor_revised_or_missing", evaluate_record(frozen(), revised)["status"])

    def test_type_router_uses_only_available_closes_and_abstains_on_high_risk(self) -> None:
        up = [10.0 + .02 * i for i in range(100)]
        route = classify_prices(up)
        self.assertEqual("uptrend", route["type"])
        self.assertEqual("sma-trend", route["decision_method"])
        self.assertEqual("momentum-20", route["forecast_method"])
        risky = [10.0 if i % 2 else 14.0 for i in range(100)]
        self.assertEqual("high_volatility", classify_prices(risky)["type"])
        self.assertIsNone(classify_prices(risky)["decision_method"])

    def test_dev_strategy_activates_routes_without_entering_frozen_catalog(self) -> None:
        up = [10.0 + .02 * i for i in range(100)]
        risky = [10.0 if i % 2 else 14.0 for i in range(100)]
        self.assertEqual({"up": 1.0}, TypedRouterStrategy().weights({"up": up, "risky": risky}))
        with self.assertRaises(ValueError):
            get_strategy_definition("typed-router")


if __name__ == "__main__":
    unittest.main()
