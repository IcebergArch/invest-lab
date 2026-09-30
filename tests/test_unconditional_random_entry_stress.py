from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

from unconditional_random_entry_stress import sampled_dates


class UnconditionalRandomEntryStressTest(unittest.TestCase):
    def test_calendar_sample_does_not_depend_on_policy_signal(self) -> None:
        dates = [(date(2021, 1, 1) + timedelta(days=i)).isoformat()
                 for i in range(20)]
        first = sampled_dates(dates, dates[0], dates[-1], 5, 202, 10)
        second = sampled_dates(dates, dates[0], dates[-1], 5, 202, 10)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 10)
        self.assertTrue(all(dates.index(day) + 5 < len(dates) for day in first))
        self.assertEqual(first, sorted(first))

    def test_sample_rejects_nonrectangular_calendar(self) -> None:
        with self.assertRaisesRegex(ValueError, 'calendar'):
            sampled_dates(['2021-01-01', '2021-01-01'],
                          '2021-01-01', '2021-01-02', 2, 202, 1)


if __name__ == '__main__':
    unittest.main()
