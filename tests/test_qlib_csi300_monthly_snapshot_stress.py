from __future__ import annotations

import math
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from qlib_csi300_entry_eligibility import FloatSeries
from qlib_csi300_monthly_snapshot_stress import (
    evaluate, signal_day, simulate_episode, t_member_exit_events,
)


def fixture(days: int = 80):
    calendar = tuple(date(2020, 9, 1) + timedelta(days=i) for i in range(days))
    masks = {key: bytearray(b"\x01" * days) for key in ("SHA", "SHB", "SHC")}
    features = {key: {
        "close": FloatSeries(0, [10.0 + (.01 * i if key == "SHA" else 0.0)
                                 for i in range(days)]),
        "volume": FloatSeries(0, [100.0] * days),
    } for key in masks}
    return calendar, masks, features


def flat_targets(_index: int, policy: str) -> dict[str, float]:
    return {} if policy == "cash" else {key: .8 / 3 for key in ("SHA", "SHB", "SHC")}


class QlibCsi300MonthlySnapshotStressTest(unittest.TestCase):
    def test_signal_uses_only_t_values_and_t_membership(self) -> None:
        calendar, masks, features = fixture()
        t = 63
        before = signal_day(calendar, masks, features, t, seed=202, pick_count=3)
        features["SHA"]["close"].values[t + 1] = 9999.0
        masks["SHB"][t + 1] = 0
        after = signal_day(calendar, masks, features, t, seed=202, pick_count=3)
        self.assertEqual(before, after)
        self.assertEqual(before["member_count"], 3)
        self.assertIn("SHA", before["active_sma_members"])
        self.assertEqual(sum(before["targets"]["fixed_80_buy_hold_20"].values()), .8)
        self.assertAlmostEqual(before["sma_target_gross"], .35)
        self.assertEqual(sum(before["targets"]["sma_exposure_equal_20"].values()),
                         before["sma_target_gross"])
        fixed = tuple(before["episode_fixed_pool"])
        self.assertEqual(set(fixed), {"SHA", "SHB", "SHC"})
        later = signal_day(calendar, masks, features, t + 1, seed=202,
                           pick_count=3, fixed_symbols=fixed)
        self.assertEqual(tuple(later["episode_fixed_pool"]), fixed)
        self.assertIn("SHB", later["targets"]["fixed_80_buy_hold_20"])

    def test_flat_prices_pay_buy_and_sell_five_bps_each(self) -> None:
        calendar, masks, features = fixture()
        for fields in features.values():
            fields["close"].values[:] = [10.0] * len(calendar)
        row = simulate_episode(calendar, masks, features, flat_targets,
                               start=63, horizon=4, policy="fixed_80_buy_hold_20",
                               case_id="case", fee=.0005)
        self.assertEqual(row["status"], "priced_adjusted_close_proxy")
        self.assertAlmostEqual(row["proxy_return"], (1 - .8 * .0005) ** 2 - 1)
        self.assertAlmostEqual(row["turnover_with_exit"], 1.6)
        self.assertEqual(len(row["initial_orders"]), 3)
        self.assertTrue(all(x["status"] == "proxy_filled" for x in row["initial_orders"]))

    def test_buy_hold_allows_natural_weight_drift_above_initial_budget(self) -> None:
        calendar, masks, features = fixture()
        for fields in features.values():
            fields["close"].values[:] = [10.0] * 65 + [15.0] * (len(calendar) - 65)
        row = simulate_episode(calendar, masks, features, flat_targets,
                               start=63, horizon=4, policy="fixed_80_buy_hold_20",
                               case_id="drift")
        self.assertEqual(row["status"], "priced_adjusted_close_proxy")
        self.assertGreater(row["proxy_return"], .39)
        self.assertAlmostEqual(row["turnover_with_exit"], .8 + (1.2 / 1.4))

    def test_missing_next_close_retains_rejected_window_and_order(self) -> None:
        calendar, masks, features = fixture()
        features["SHA"]["close"].values[64] = math.nan
        row = simulate_episode(calendar, masks, features, flat_targets,
                               start=63, horizon=4, policy="fixed_80_buy_hold_20",
                               case_id="case")
        self.assertEqual(row["status"], "not_priceable_or_unfilled")
        self.assertIsNone(row["proxy_return"])
        self.assertEqual(row["first_failure"]["reason"], "unfillable_missing_close")
        self.assertEqual(row["first_failure"]["order_id"],
                         f"case:fixed_80_buy_hold_20:{calendar[63].isoformat()}:SHA")
        self.assertEqual(next(x for x in row["initial_orders"] if x["symbol"] == "SHA")["status"],
                         "proxy_rejected")
        self.assertEqual(next(x for x in row["initial_orders"] if x["symbol"] == "SHB")["status"],
                         "not_executed_due_to_batch_failure")
        self.assertEqual(row["proxy_order_count_before_failure"], 0)
        self.assertEqual(row["fee_paid_before_failure_equity_units"], 0)

    def test_missing_held_mark_is_unpriceable_without_last_price_carry(self) -> None:
        calendar, masks, features = fixture()
        features["SHB"]["close"].values[65] = math.nan
        row = simulate_episode(calendar, masks, features, flat_targets,
                               start=63, horizon=4, policy="fixed_80_buy_hold_20",
                               case_id="held")
        self.assertEqual(row["first_failure"]["reason"], "unpriceable_held_close")
        self.assertIsNone(row["proxy_return"])

    def test_constituent_exit_does_not_imply_sale_possible(self) -> None:
        calendar, masks, features = fixture()
        masks["SHA"][65:] = b"\x00" * (len(calendar) - 65)
        features["SHA"]["volume"].values[65] = 0.0

        def targets(index: int, policy: str) -> dict[str, float]:
            if policy == "cash":
                return {}
            if index >= 64:
                return {"SHB": .35, "SHC": .35}
            return {key: .8 / 3 for key in ("SHA", "SHB", "SHC")}

        row = simulate_episode(calendar, masks, features, targets,
                               start=63, horizon=4, policy="sma_active_equal",
                               case_id="exit")
        self.assertEqual(row["first_failure"]["reason"], "unfillable_nonpositive_volume")
        self.assertTrue(row["held_constituent_exit_events_before_failure"])
        self.assertEqual(len(t_member_exit_events(
            calendar, masks, ("SHA",), 63, 67)), 1)

    def test_evaluate_keeps_every_sampled_case_in_denominator(self) -> None:
        calendar = tuple(date(2020, 9, 1) + timedelta(days=i) for i in range(500))
        masks = {key: bytearray(b"\x01" * len(calendar))
                 for key in ("SHA", "SHB", "SHC")}
        features = {key: {
            "close": FloatSeries(0, [10 + .001 * i for i in range(len(calendar))]),
            "volume": FloatSeries(0, [100.0] * len(calendar)),
        } for key in masks}
        report = evaluate(calendar, masks, features, horizons=(2,), seeds=(202,),
                          pick_count=3, samples_per_entry_year=1)
        rows = report["cases"]["selection_history"]["2"]["202"]
        summary = report["summaries"]["selection_history"]["2"]["202"]["all"]
        self.assertEqual(summary["sample_count"], len(rows))
        self.assertEqual(summary["immediate_sma_buy_signal_count"], len(rows))
        self.assertEqual(summary["buy_signal_conditional_jointly_priceable_count"],
                         summary["jointly_priceable_count"])
        self.assertIn("paired_max_drawdown_change_vs_same_exposure", summary)
        self.assertIn("policy_metrics_on_jointly_priceable", summary)
        self.assertEqual(summary["sma_entry_target_at_80pct_count"], len(rows))
        self.assertEqual(summary["median_active_sma_names_at_entry"], 3)
        first = rows[0]
        symbol = first["signal_at_t"]["episode_fixed_pool"][0]
        features[symbol]["close"].values[first["start_index"] + 1] = math.nan
        damaged = evaluate(calendar, masks, features, horizons=(2,), seeds=(202,),
                           pick_count=3, samples_per_entry_year=1)
        damaged_rows = damaged["cases"]["selection_history"]["2"]["202"]
        damaged_summary = damaged["summaries"]["selection_history"]["2"]["202"]["all"]
        self.assertEqual([row["case_id"] for row in rows],
                         [row["case_id"] for row in damaged_rows])
        self.assertEqual(damaged_summary["sample_count"], summary["sample_count"])
        self.assertLess(damaged_summary["jointly_priceable_count"],
                        summary["jointly_priceable_count"])
        self.assertEqual(len({row["case_id"] for row in rows}), len(rows))
        self.assertFalse(report["historical_daily_index_membership_confirmed"])
        self.assertFalse(report["point_in_time_source_vintage_confirmed"])


if __name__ == "__main__":
    unittest.main()
