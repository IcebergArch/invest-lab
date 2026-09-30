from __future__ import annotations

import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from qlib_cross_pool_audit import (POOLS, evaluate, sma_targets,
                                   validate_frozen_policy, validate_stock_payloads)
from random_entry_baseline import candidate_targets, exposure_matched_targets


class QlibCrossPoolAuditTest(unittest.TestCase):
    @staticmethod
    def payloads() -> dict:
        days = [(date(2021, 1, 4) + timedelta(days=n)).isoformat() for n in range(80)]
        return {key: {'adjustment': 'qlib_adjusted', 'missing_fields': [],
                      'truncated_to_latest': False, 'fields': ['close', 'volume'],
                      'stock': {'instrument_id': key},
                      'release': {'tag': '2026-09-28', 'manifest_sha256': 'm',
                                  'archive_sha256': 'a'},
                      'calendar_rows_in_range': len(days),
                      'rows': [{'date': day, 'close': 10 + i * .01, 'volume': 1000.0}
                               for i, day in enumerate(days)]}
                for pool in POOLS.values() for key in pool}

    def test_reader_rejects_missing_or_nontradeable_proxy_without_dropping_day(self) -> None:
        base = self.payloads()
        days, panels = validate_stock_payloads(base)
        self.assertEqual(len(days), 80)
        self.assertTrue(all(len(prices) == 80 for pool in panels.values()
                            for prices in pool.values()))
        key = POOLS['user_named_transfer'][0]
        for change in ('missing_close', 'zero_volume', 'missing_row', 'misaligned_date',
                       'mixed_release', 'truncated'):
            bad = copy.deepcopy(base)
            if change == 'missing_close':
                bad[key]['rows'][30]['close'] = None
            elif change == 'zero_volume':
                bad[key]['rows'][30]['volume'] = 0.0
            elif change == 'missing_row':
                del bad[key]['rows'][30]
            elif change == 'misaligned_date':
                bad[key]['rows'][30]['date'] = '2021-01-01'
            elif change == 'mixed_release':
                bad[key]['release']['archive_sha256'] = 'different'
            else:
                bad[key]['truncated_to_latest'] = True
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_stock_payloads(bad)

    def test_frozen_policy_checks_budget_fee_and_strategy(self) -> None:
        policy = {'mode': 'single', 'strategy_ids': ['sma-trend'],
                  'universe': list(POOLS['frozen_baseline']),
                  'budget': {'max_gross_weight': .8, 'max_stock_weight': .35},
                  'cost_rate': .0005}
        validate_frozen_policy(policy)
        for field, value in (('cost_rate', .001), ('strategy_ids', ['cross-sectional-momentum'])):
            changed = copy.deepcopy(policy)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_frozen_policy(changed)

    def test_paired_dates_and_excess_are_computed_on_same_windows(self) -> None:
        days = [(date(2021, 1, 4) + timedelta(days=n)).isoformat() for n in range(400)]
        panels = {
            'frozen_baseline': {key: [10 * 1.001 ** n for n in range(400)]
                                for key in POOLS['frozen_baseline']},
            'user_named_transfer': {key: [10 * .999 ** n for n in range(400)]
                                    for key in POOLS['user_named_transfer']},
        }
        report = evaluate(days, panels, segments={'all': (days[0], days[-1])},
                          horizons=(126,), seeds=(202,), sample_count=8)
        block = report['cases']['all']['126']['202']
        rows = block['episodes']
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({row['entry_date'] for row in rows}), 8)
        self.assertTrue(all(set(row['pools']) == set(POOLS) for row in rows))
        self.assertTrue(all(row['exit_date'] > row['entry_date'] for row in rows))
        expected = [row['pools']['user_named_transfer']['sma']['return']
                    - row['pools']['frozen_baseline']['sma']['return'] for row in rows]
        self.assertAlmostEqual(
            block['summary']['paired_transfer_minus_baseline_sma_return']['median'],
            median(expected))
        self.assertEqual(report['fee_rate_per_side'], .0005)
        self.assertEqual(report['budget']['max_gross_weight'], .8)
        self.assertIn('same_cap_equal_hold_return', block['summary']['pools']['frozen_baseline'])
        self.assertIn('exposure_matched_equal_hold_return', block['summary']['pools']['frozen_baseline'])

    def test_sma_targets_keep_frozen_gross_and_single_name_caps(self) -> None:
        panel = {key: [10 * 1.001 ** n for n in range(80)]
                 for key in POOLS['frozen_baseline']}
        targets = sma_targets(panel)
        self.assertEqual(sum(targets[58].values()), 0)
        self.assertAlmostEqual(sum(targets[60].values()), .8)
        self.assertTrue(all(max(row.values()) <= .35 for row in targets))
        matched = exposure_matched_targets(targets, panel)
        self.assertTrue(all(abs(sum(a.values()) - sum(b.values())) < 1e-12
                            for a, b in zip(targets, matched)))

    def test_sma_targets_match_original_baseline_formula(self) -> None:
        panel = {key: [10 * (1 + (i + 1) * .0002) ** day for day in range(320)]
                 for i, key in enumerate(POOLS['frozen_baseline'])}
        self.assertEqual(sma_targets(panel), candidate_targets(panel)['sma'])


if __name__ == '__main__':
    unittest.main()
