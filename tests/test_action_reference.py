from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.action_reference import RULE_VERSION, build_action_reference


def report(*, qlib: bool = False) -> dict:
    source = {
        "latest_source_id": "investment_data_qlib_release" if qlib else "eastmoney_kline",
        "latest_run_id": None if qlib else "run-123",
        "latest_payload_hash": None if qlib else "sha-123",
        "adjustment": "qlib_adjusted" if qlib else "qfq",
        "missing_recent_market_sessions": None if qlib else 0,
    }
    if qlib:
        source.update(release_tag="2026-09-28", release_target_trade_date="2026-09-28",
                      factor_verified=True, amount_verified=True)
    return {
        "status": "ready", "asof": "2026-09-28", "instrument_id": "stock:603993.SH",
        "latest_close": 16.83, "cost_price": 20.334, "cost_return": -0.172,
        "trend": {"metrics": {"return_20d": -0.133, "return_60d": -0.087}},
        "risk": {"metrics": {
            "return_20d": -0.133, "return_60d": -0.087,
            "annualized_volatility_20d": 0.293, "max_drawdown_60d": 0.189,
            "avg_amount_20d_cny": 2_803_360_750,
        }},
        "forecast": {"status": "baseline_only"},
        "sources": [source],
    }


class ActionReferenceTest(unittest.TestCase):
    def test_realistic_downtrend_produces_conditional_reduce_not_cost_stop(self) -> None:
        result = build_action_reference(report(), today=date(2026, 9, 29))
        self.assertEqual(RULE_VERSION, result["rule_version"])
        self.assertEqual("insufficient_evidence", result["status"])
        self.assertEqual("wait", result["buy"]["stance"])
        self.assertEqual("consider_reduce", result["sell"]["stance"])
        self.assertIn("上一交易日", result["sell"]["reason"])
        self.assertIn("不是账户盈亏", result["cost_context"])
        self.assertNotIn("-17.2%", str(result))
        self.assertEqual("run-123", result["input_provenance"]["source_run_id"])
        self.assertEqual("2026-09-28", result["signals"][0]["asof"])

    def test_same_day_low_risk_confluence_can_be_considered_without_forecast_claim(self) -> None:
        sample = report()
        sample["risk"]["metrics"].update(return_20d=0.06, return_60d=0.12,
                                           annualized_volatility_20d=0.25,
                                           max_drawdown_60d=0.08)
        result = build_action_reference(sample, today=date(2026, 9, 28))
        self.assertEqual("ready", result["status"])
        self.assertEqual("consider", result["buy"]["stance"])
        self.assertEqual("hold", result["sell"]["stance"])
        self.assertIn("不是收益预测", result["buy"]["reason"])
        self.assertIn("尚无经独立验证", " ".join(result["risks"]))
        next_day = build_action_reference(sample, today=date(2026, 9, 29))
        self.assertEqual("wait", next_day["buy"]["stance"])

    def test_multiple_severe_risks_and_low_liquidity(self) -> None:
        sample = report()
        sample["risk"]["metrics"].update(return_20d=-0.22, return_60d=-0.24,
                                           annualized_volatility_20d=0.65,
                                           max_drawdown_60d=0.32,
                                           avg_amount_20d_cny=5_000_000)
        result = build_action_reference(sample, today=date(2026, 9, 28))
        self.assertEqual("avoid", result["buy"]["stance"])
        self.assertEqual("review_exit", result["sell"]["stance"])
        self.assertTrue(any("流动性" in item for item in result["risks"]))

    def test_unverified_qlib_never_uses_adjusted_close_as_cost_or_trade_cue(self) -> None:
        sample = report(qlib=True)
        sample["sources"][0]["factor_verified"] = False
        sample["sources"][0]["amount_verified"] = False
        sample["latest_close"] = None
        sample["adjusted_close"] = 6.56
        sample["cost_return"] = None
        result = build_action_reference(sample, today=date(2026, 9, 28))
        self.assertEqual("insufficient_evidence", result["status"])
        self.assertEqual("wait", result["buy"]["stance"])
        self.assertEqual("unavailable", result["sell"]["stance"])
        self.assertIn("复权价", result["cost_context"])
        self.assertEqual("unverified_adjusted_price", result["input_provenance"]["price_basis"])
        self.assertEqual([], result["signals"])

    def test_ended_qlib_interval_avoids_buy_and_does_not_claim_sellability(self) -> None:
        sample = report(qlib=True)
        sample["asof"] = "2002-04-26"
        result = build_action_reference(sample, today=date(2026, 9, 29))
        self.assertEqual("avoid", result["buy"]["stance"])
        self.assertEqual("unavailable", result["sell"]["stance"])

    def test_qlib_amount_or_release_date_missing_blocks_trade_reference(self) -> None:
        sample = report(qlib=True)
        sample["sources"][0]["amount_verified"] = False
        result = build_action_reference(sample, today=date(2026, 9, 28))
        self.assertEqual("wait", result["buy"]["stance"])
        self.assertEqual("unavailable", result["sell"]["stance"])
        sample["sources"][0]["amount_verified"] = True
        del sample["sources"][0]["release_target_trade_date"]
        result = build_action_reference(sample, today=date(2026, 9, 28))
        self.assertEqual("insufficient_evidence", result["status"])
        self.assertEqual("unavailable", result["buy"]["stance"])

    def test_stale_missing_and_malformed_metrics_fail_closed(self) -> None:
        old = build_action_reference(report(), today=date(2026, 10, 3))
        self.assertEqual("wait", old["buy"]["stance"])
        self.assertEqual("unavailable", old["sell"]["stance"])
        missing = report()
        missing["sources"][0]["missing_recent_market_sessions"] = 1
        self.assertEqual("wait", build_action_reference(missing, today=date(2026, 9, 28))["buy"]["stance"])
        invalid = report()
        invalid["risk"]["metrics"]["annualized_volatility_20d"] = float("nan")
        result = build_action_reference(invalid, today=date(2026, 9, 28))
        self.assertEqual("insufficient_evidence", result["status"])
        self.assertEqual([], result["signals"])
        future = report()
        future["asof"] = "2026-09-30"
        self.assertEqual("unavailable", build_action_reference(future, today=date(2026, 9, 29))["buy"]["stance"])

    def test_pure_deterministic_and_invalid_today(self) -> None:
        sample = report()
        original = deepcopy(sample)
        first = build_action_reference(sample, today=date(2026, 9, 29))
        self.assertEqual(first, build_action_reference(sample, today=date(2026, 9, 29)))
        self.assertEqual(original, sample)
        with self.assertRaises(TypeError):
            build_action_reference(sample, today="2026-09-29")


if __name__ == "__main__":
    unittest.main()
