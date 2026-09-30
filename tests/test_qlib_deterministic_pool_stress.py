from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

from qlib_deterministic_pool_stress_v2 import (
    ENTRY_FIRST, FIRST, LAST, buy_entry_indices, ordered_candidates,
)


class DeterministicPoolStressTest(unittest.TestCase):
    def test_warmup_precedes_2021_buy_window(self) -> None:
        self.assertLess(FIRST, ENTRY_FIRST)
        self.assertLess(ENTRY_FIRST, LAST)

    def test_symbol_selection_uses_span_and_hash_order(self) -> None:
        start, end = date.fromisoformat(FIRST), date.fromisoformat(LAST)
        candidates = {'SH600002': ((start, end),),
                      'SZ000001': ((start, end),),
                      'SZ000003': ((start + timedelta(days=1), end),),
                      'BJ430001': ((start, end),)}
        selected = ordered_candidates(candidates)
        self.assertEqual({'SH600002', 'SZ000001'}, set(selected))
        self.assertEqual(selected, ordered_candidates(dict(reversed(list(candidates.items())))))

    def test_random_entries_really_instruct_a_buy_and_exit_within_period(self) -> None:
        dates = [(date(2021, 1, 1) + timedelta(days=index)).isoformat()
                 for index in range(10)]
        targets = [{"stock:A": 1.0} if index in (1, 3, 7) else {"stock:A": 0.0}
                   for index in range(9)]
        picked = buy_entry_indices(dates, targets, dates[0], dates[-1],
                                   2, 202, 2)
        self.assertEqual(2, len(picked))
        self.assertTrue(set(picked) <= {1, 3, 7})
        self.assertEqual(picked, buy_entry_indices(
            dates, targets, dates[0], dates[-1], 2, 202, 2))
        self.assertTrue(all(index + 2 < len(dates) for index in picked))
        with self.assertRaisesRegex(ValueError, 'true buy-signal'):
            buy_entry_indices(dates, targets, dates[0], dates[-1], 2, 202, 4)


if __name__ == '__main__':
    unittest.main()
