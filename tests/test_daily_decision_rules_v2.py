from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.daily_decision_rules_v2 import (
    build_daily_rule_proposal, combine_daily_rule_proposals, daily_rule_catalog_v2,
)
from quant_lab.daily_factors_v2 import FactorBar
from quant_lab.decision_ensemble import PortfolioBudget

_CHINA = ZoneInfo("Asia/Shanghai")


def panel(*, missing_amount: bool = False) -> dict[str, list[FactorBar]]:
    result: dict[str, list[FactorBar]] = {"stock:A": [], "stock:B": []}
    previous = {"stock:A": 10.0, "stock:B": 10.0}
    for index in range(70):
        day = date(2026, 1, 1) + timedelta(days=index)
        for key in result:
            if key == "stock:A":
                opening = previous[key] * 1.005
                closing = opening * 1.005
                low, high = opening * .97, closing * 1.03
            else:
                opening = previous[key] * 1.001
                closing = opening * .999
                low, high = closing * .999, opening * 1.001
            result[key].append(FactorBar(
                instrument_id=key, trade_date=day, open=opening, high=high,
                low=low, close=closing, volume_shares=1000000,
                amount_cny=None if missing_amount else (100000000 if key == "stock:A" else 10000000),
                price_basis="forward_adjusted",
                amount_unit="unavailable" if missing_amount else "CNY",
                available_at=datetime(day.year, day.month, day.day, 16, 0, tzinfo=_CHINA),
                source_id="synthetic_fixture"))
            previous[key] = closing
    return result


class DailyDecisionRuleV2Test(unittest.TestCase):
    def test_rules_are_explicitly_unvalidated(self) -> None:
        catalog = daily_rule_catalog_v2()
        self.assertEqual({"gap_followthrough", "low_range_risk", "volume_confirmed_momentum", "liquid_momentum"},
                         {item["rule_id"] for item in catalog})
        self.assertTrue(all(item["deployment_status"] == "research_only_unvalidated"
                            for item in catalog))

    def test_independent_rules_combine_with_one_budget(self) -> None:
        bars = panel()
        decision_at = bars["stock:A"][-1].available_at + timedelta(minutes=30)
        gap = build_daily_rule_proposal("gap_followthrough", bars, decision_at=decision_at)
        low_range = build_daily_rule_proposal("low_range_risk", bars, decision_at=decision_at)
        self.assertEqual(gap.target_weights, {"stock:A": 1.0, "stock:B": 0.0})
        self.assertEqual(low_range.target_weights, {"stock:A": 0.0, "stock:B": 1.0})
        self.assertEqual(gap.input_sha256, low_range.input_sha256)
        liquid = build_daily_rule_proposal("liquid_momentum", bars, decision_at=decision_at)
        self.assertEqual({"stock:A": 1.0, "stock:B": 0.0}, liquid.target_weights)
        combined = combine_daily_rule_proposals(
            ["gap_followthrough", "low_range_risk"], bars,
            {"gap_followthrough": 1, "low_range_risk": 1},
            PortfolioBudget(.8, .35), decision_at=decision_at)
        self.assertEqual(combined["target_weights"], {"stock:A": .35, "stock:B": .35})
        self.assertAlmostEqual(combined["cash_target_weight"], .3)
        self.assertEqual(combined["deployment_status"], "research_only_unvalidated")

    def test_volume_rule_runs_without_cny_amount(self) -> None:
        bars = panel(missing_amount=True)
        decision_at = bars["stock:A"][-1].available_at + timedelta(minutes=30)
        proposal = build_daily_rule_proposal(
            "volume_confirmed_momentum", bars, decision_at=decision_at)
        self.assertEqual({"stock:A": 1.0, "stock:B": 0.0}, proposal.target_weights)

    def test_liquidity_rule_rejects_missing_amount(self) -> None:
        bars = panel(missing_amount=True)
        decision_at = bars["stock:A"][-1].available_at + timedelta(minutes=30)
        with self.assertRaisesRegex(ValueError, "CNY amount"):
            build_daily_rule_proposal("liquid_momentum", bars, decision_at=decision_at)

    def test_future_bar_and_mixed_asof_are_rejected(self) -> None:
        bars = panel()
        decision_at = bars["stock:A"][-1].available_at - timedelta(days=1)
        with self.assertRaisesRegex(ValueError, "not available"):
            build_daily_rule_proposal("gap_followthrough", bars, decision_at=decision_at)
        bars["stock:B"].pop()
        decision_at = bars["stock:A"][-1].available_at + timedelta(minutes=30)
        with self.assertRaisesRegex(ValueError, "mixed dates"):
            build_daily_rule_proposal("gap_followthrough", bars, decision_at=decision_at)


if __name__ == "__main__":
    unittest.main()
