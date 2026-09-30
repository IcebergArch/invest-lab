import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from qlib_2021_anchor_asof_reserve_audit import select_asof_slots


class AsOfReserveSelectionTests(unittest.TestCase):
    def test_preserves_original_slots_and_skips_unavailable_reserve(self):
        ranked = ['a', 'b', 'c', 'd', 'e', 'f']
        eligible = {'a', 'c', 'e', 'f'}
        self.assertEqual(select_asof_slots(ranked, lambda s: s in eligible, count=3),
                         ['a', 'e', 'c'])

    def test_multiple_missing_slots_receive_distinct_ranked_reserves(self):
        ranked = ['a', 'b', 'c', 'd', 'e', 'f']
        eligible = {'c', 'd', 'e', 'f'}
        self.assertEqual(select_asof_slots(ranked, lambda s: s in eligible, count=3),
                         ['d', 'e', 'c'])

    def test_missing_reserve_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, 'no eligible reserve'):
            select_asof_slots(['a', 'b', 'c'], lambda s: s == 'a', count=2)


if __name__ == '__main__':
    unittest.main()
