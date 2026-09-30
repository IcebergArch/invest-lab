from __future__ import annotations

import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from quant_lab.decision_pipeline import decide
from random_entry_baseline import (candidate_targets, cash_matched_hold_episode,
                                   entry_indices, episode, exposure_matched_targets,
                                   hold_episode)


class RandomEntryBaselineTest(unittest.TestCase):
    def test_dates_are_seeded_and_exit_inside_segment(self) -> None:
        days = [(date(2021, 1, 1) + timedelta(days=i)).isoformat() for i in range(500)]
        first = entry_indices(days, days[0], days[399], 126, 17, 30)
        self.assertEqual(first, entry_indices(days, days[0], days[399], 126, 17, 30))
        self.assertEqual(len(first), 30)
        self.assertTrue(all(days[i + 126] <= days[399] for i in first))
        self.assertEqual(entry_indices(days, days[0], days[399], 126, 0, None),
                         list(range(274)))

    def test_signals_use_only_history_through_decision_date(self) -> None:
        keys = ['stock:000338.SZ', 'stock:002475.SZ', 'stock:002714.SZ']
        original = {key: [10 + n * (i + 1) * .01 for n in range(320)]
                    for i, key in enumerate(keys)}
        revised = {key: list(values) for key, values in original.items()}
        revised[keys[0]][201:] = [1000.0] * 119
        before = candidate_targets(original)
        after = candidate_targets(revised)
        for name in before:
            self.assertEqual(before[name][:200], after[name][:200])

    def test_baseline_policy_matches_strategy_adapter(self) -> None:
        policy = json.loads((ROOT / 'policies/random-entry-risk-baseline-v1.json').read_text())
        panel = {key: [10 + i * .001 * day for day in range(320)]
                 for i, key in enumerate(policy['universe'], 1)}
        from_study = candidate_targets(panel)['sma'][100]
        from_pipeline = decide(policy, '2021-06-01',
                               {key: prices[:101] for key, prices in panel.items()})
        self.assertEqual(from_study, from_pipeline['target_weights'])

    def test_multi_function_targets_match_decision_pipeline(self) -> None:
        policy = json.loads((ROOT / 'policies/ensemble-focus-three-v2-five-bps.json').read_text())
        panel = {key: [10 + i * .001 * day for day in range(320)]
                 for i, key in enumerate(policy['universe'], 1)}
        from_study = candidate_targets(panel)['all_three'][100]
        from_pipeline = decide(policy, '2021-06-01',
                               {key: prices[:101] for key, prices in panel.items()})
        self.assertEqual(from_study, from_pipeline['target_weights'])

    def test_cash_matched_hold_pays_both_sides_and_keeps_cash(self) -> None:
        days = ['2021-01-01', '2021-01-02']
        panel = {'A': [10.0, 10.0], 'B': [20.0, 20.0]}
        result = cash_matched_hold_episode(days, panel, 0, 1, .0005, .8)
        self.assertAlmostEqual(result['return'], -.8 * .0005 * 2)
        self.assertAlmostEqual(result['turnover'], .8 + .8 / (1 - .8 * .0005))
        self.assertAlmostEqual(result['max_drawdown'], -.8 * .0005 * 2)

    def test_dynamic_reference_keeps_same_daily_gross(self) -> None:
        targets = [{'A': .35, 'B': 0.0}, {'A': .35, 'B': .35},
                   {'A': 0.0, 'B': 0.0}]
        matched = exposure_matched_targets(targets, ['A', 'B'])
        self.assertEqual(matched, [{'A': .175, 'B': .175},
                                   {'A': .35, 'B': .35},
                                   {'A': 0.0, 'B': 0.0}])
        self.assertEqual([sum(x.values()) for x in matched],
                         [sum(x.values()) for x in targets])

    def test_exit_close_does_not_rebalance_then_sell(self) -> None:
        days = ['2021-01-01', '2021-01-02', '2021-01-03']
        panel = {'A': [10.0] * 3, 'B': [10.0] * 3}
        result = episode(days, panel, [{'A': 1.0, 'B': 0.0},
                                        {'A': 0.0, 'B': 1.0}], 0, 2, .0005)
        self.assertAlmostEqual(result['return'], (1 - .0005) ** 2 - 1)
        self.assertAlmostEqual(result['turnover'], 2.0)

    def test_entry_and_exit_both_pay_five_basis_points(self) -> None:
        days = ['2021-01-01', '2021-01-02', '2021-01-03']
        panel = {'A': [10.0, 10.0, 10.0]}
        target = [{'A': 1.0}, {'A': 1.0}]
        strategy = episode(days, panel, target, 0, 2, .0005)
        hold = hold_episode(days, panel, 0, 2, .0005)
        self.assertAlmostEqual(strategy['return'], (1 - .0005) ** 2 - 1)
        self.assertAlmostEqual(strategy['return'], hold['return'])


if __name__ == '__main__':
    unittest.main()
