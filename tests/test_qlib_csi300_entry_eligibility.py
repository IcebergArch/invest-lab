from __future__ import annotations

import math
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from qlib_csi300_entry_eligibility import (
    FloatSeries, audit_entry, build_membership_masks, evaluate,
    sampled_entry_indices, selected_members,
)


class QlibCsi300EntryEligibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = tuple(date(2021, 1, 1) + timedelta(days=i) for i in range(12))
        dates = self.calendar
        self.intervals = {
            'SHA': ((dates[0], dates[5]), (dates[6], dates[11])),
            'SHB': ((dates[0], dates[11]),),
            'SHC': ((dates[0], dates[5]),),
            'SHD': ((dates[4], dates[11]),),
        }
        self.masks = build_membership_masks(dates, self.intervals)
        closes = {symbol: [10.0] * len(dates) for symbol in self.intervals}
        volumes = {symbol: [100.0] * len(dates) for symbol in self.intervals}
        closes['SHA'][2] = math.nan
        volumes['SHB'][4] = 0.0
        closes['SHC'][6] = math.nan
        self.features = {
            symbol: {'close': FloatSeries(0, closes[symbol]),
                     'volume': FloatSeries(0, volumes[symbol])}
            for symbol in self.intervals
        }

    def test_selection_uses_only_t_membership_and_never_replaces_failed_names(self) -> None:
        selected, member_count = selected_members(self.masks, self.calendar, 3, 202, 3)
        self.assertEqual(set(selected), {'SHA', 'SHB', 'SHC'})
        self.assertEqual(member_count, 3)
        before = audit_entry(self.calendar, self.masks, self.features, 3, 4,
                             202, pick_count=3, warmup=3)
        self.features['SHA']['close'].values[4] = math.nan
        self.features['SHB']['close'].values[5] = math.nan
        after = audit_entry(self.calendar, self.masks, self.features, 3, 4,
                            202, pick_count=3, warmup=3)
        self.assertEqual(before['selected'], after['selected'])
        self.assertNotIn('SHD', after['selected'])
        self.assertTrue(after['flags_by_symbol']['SHA']['next_missing_close'])

    def test_warmup_next_session_holding_gap_and_review_roll_are_distinct(self) -> None:
        row = audit_entry(self.calendar, self.masks, self.features, 3, 4,
                          202, pick_count=3, warmup=3)
        flags = row['flags_by_symbol']
        self.assertTrue(flags['SHA']['warmup_missing_close'])
        self.assertFalse(flags['SHA']['constituent_exited'])
        self.assertTrue(flags['SHB']['next_nonpositive_volume'])
        self.assertTrue(flags['SHC']['holding_missing_close_after_entry'])
        self.assertTrue(flags['SHC']['constituent_exited'])
        self.assertEqual(sum(item['continuous_member_and_price_volume_coverage']
                             for item in flags.values()), 0)

    def test_seeded_calendar_sampling_and_aggregate_denominators(self) -> None:
        dates = self.calendar
        indices = sampled_entry_indices(dates, dates[3].isoformat(),
                                         dates[7].isoformat(), 4, 202, 1)
        self.assertEqual(indices, [3])
        report = evaluate(dates, self.masks, self.features,
                          segments={'one': (dates[3].isoformat(), dates[7].isoformat())},
                          horizons=(4,), seeds=(202,), sample_count=1,
                          pick_count=3, warmup=3)
        case = report['cases']['one']['4']['202']
        self.assertEqual(case['selected_name_count'], 3)
        self.assertEqual(case['sampled_entry_dates'], [dates[3].isoformat()])
        self.assertEqual(set(case['selection_examples'][0]['selected']),
                         {'SHA', 'SHB', 'SHC'})
        self.assertEqual(case['name_counts']['warmup_missing_close'], 1)
        self.assertEqual(case['name_counts']['next_nonpositive_volume'], 1)
        self.assertEqual(case['name_counts']['holding_missing_close_after_entry'], 1)
        self.assertEqual(case['name_counts']['constituent_exited'], 1)
        self.assertEqual(case['name_counts']['continuous_member_and_price_volume_coverage'], 0)
        self.assertAlmostEqual(case['name_fractions_of_selected']['constituent_exited'], 1 / 3)
        self.assertFalse(report['point_in_time_confirmed'])
        self.assertFalse(report['official_daily_membership_confirmed'])
        self.assertIn('forward-filled', report['selection']['source'])
        self.assertFalse(report['return_or_pnl_computed'])
        self.assertNotIn('return', case)


if __name__ == '__main__':
    unittest.main()
