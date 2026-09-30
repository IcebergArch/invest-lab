from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from robust_candidate_screen import (CANDIDATES, candidate_targets,
                                     eligible_entries, evaluate_panel,
                                     first_buy_after_observation,
                                     sampled_entries)
from random_entry_baseline import episode, exposure_matched_targets


def fixture(days: int = 1400) -> tuple[list[str], dict[str, list[float]]]:
    calendar = []
    day = date(2021, 1, 4)
    while len(calendar) < days:
        if day.weekday() < 5:
            calendar.append(day.isoformat())
        day += timedelta(days=1)
    prices = {
        "stock:A": [10 * (1 + .0004 * i) * (1 + .015 * ((i % 20) / 20))
                    for i in range(days)],
        "stock:B": [20 * (1 + .0002 * i) * (1 + .02 * ((i % 35) / 35))
                    for i in range(days)],
        "stock:C": [30 * (1 + .0001 * i) * (1 + .01 * ((i % 13) / 13))
                    for i in range(days)],
    }
    return calendar, prices


class RobustCandidateScreenTest(unittest.TestCase):
    def test_future_prices_cannot_change_past_targets(self) -> None:
        _, original = fixture(400)
        changed = {key: list(values) for key, values in original.items()}
        changed["stock:A"][201:] = [1000] * 199
        before = candidate_targets(original)
        after = candidate_targets(changed)
        self.assertEqual(set(CANDIDATES), set(before))
        for name in CANDIDATES:
            self.assertEqual(before[name][:201], after[name][:201])
            self.assertTrue(all(sum(target.values()) <= .8 + 1e-12
                                and max(target.values()) <= .35 + 1e-12
                                for target in before[name]))

    def test_year_cohorts_exit_inside_split_and_disjoint_subset(self) -> None:
        dates, _ = fixture()
        eligible = eligible_entries(dates, year=2023, horizon=126)
        self.assertTrue(eligible)
        self.assertTrue(all(dates[i + 126] <= "2023-12-29" for i in eligible))
        sample, disjoint = sampled_entries(eligible, seed=202, count=24, horizon=126)
        self.assertEqual((sample, disjoint), sampled_entries(
            eligible, seed=202, count=24, horizon=126))
        self.assertTrue(all(b > a + 126 for a, b in zip(disjoint, disjoint[1:])))
        self.assertLessEqual(len(disjoint), len(sample))

    def test_paired_exposure_reference_uses_same_dates_and_fees(self) -> None:
        dates, panel = fixture(400)
        all_targets = candidate_targets(panel)
        self.assertEqual(
            all_targets["trend_exposure_equal_all"],
            exposure_matched_targets(all_targets["sma20_60"], sorted(panel)))
        targets = all_targets["trend_momentum_blend"]
        matched = exposure_matched_targets(targets, sorted(panel))
        self.assertEqual([sum(x.values()) for x in targets],
                         [sum(x.values()) for x in matched])
        index = 130
        strategy = episode(dates, panel, targets, index, 126, .0005)
        reference = episode(dates, panel, matched, index, 126, .0005)
        self.assertTrue(all(key in strategy and key in reference
                            for key in ("return", "max_drawdown", "turnover")))

    def test_random_observation_tracks_first_actual_buy(self) -> None:
        dates, _ = fixture(400)
        targets = [{"stock:A": 0.0} for _ in range(399)]
        targets[133] = {"stock:A": .35}
        result = first_buy_after_observation(dates, targets, 130, 126)
        self.assertEqual(result["wait_sessions"], 3)
        self.assertEqual(result["first_positive_decision_date"], dates[133])
        self.assertEqual(result["proxy_buy_fill_date"], dates[134])
        self.assertIsNone(first_buy_after_observation(
            dates, targets, 134, 126)["wait_sessions"])
        targets[255] = {"stock:A": .35}  # final prior-close decision exits, so no new buy
        self.assertIsNone(first_buy_after_observation(
            dates, targets, 134, 122)["wait_sessions"])

    def test_report_keeps_all_candidates_and_small_disjoint_count(self) -> None:
        dates, panel = fixture()
        result = evaluate_panel(dates, panel, horizons=(126,), seeds=(202,),
                                sample_per_year=2, fee=.0005)
        self.assertEqual(result["fee_rate_per_side"], .0005)
        rows = result["results_by_horizon_seed_entry_year"]["126"]["202"]
        self.assertEqual(set(CANDIDATES), set(rows["2022"]["candidates"]))
        self.assertLessEqual(rows["2022"]["observation_disjoint_count"], 2)
        self.assertEqual(
            rows["2022"]["candidates"]["sma20_60"]["random_observation"]["all"]["episodes"],
            rows["2022"]["observation_sample_count"])
        self.assertEqual(
            rows["2022"]["candidates"]["sma20_60"]["random_observation"]["disjoint"]["episodes"],
            rows["2022"]["observation_disjoint_count"])
        for candidate in rows["2022"]["candidates"].values():
            buy = candidate["actual_buy_signal"]
            self.assertEqual(buy["sample_count"], buy["all"]["episodes"])
            self.assertEqual(len(buy["sampled_decision_dates"]), buy["sample_count"])
        self.assertEqual(
            rows["2022"]["candidates"]["sma20_60"]["actual_buy_signal"]["sampled_decision_dates"],
            rows["2022"]["candidates"]["trend_exposure_equal_all"]["actual_buy_signal"]["sampled_decision_dates"])
        split = result["results_by_horizon_seed_segment"]["126"]["202"]["selection"]
        self.assertLessEqual(
            split["sma20_60"]["random_observation"]["segment_disjoint"]["episodes"],
            split["sma20_60"]["observation_count"])


if __name__ == "__main__":
    unittest.main()
