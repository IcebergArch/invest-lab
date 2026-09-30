import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from qlib_2021_anchor_missingness_bounds import median_bounds


class MedianBoundsTests(unittest.TestCase):
    def test_unknown_outcomes_bound_full_even_sample_median(self):
        bounds = median_bounds([1, 2, 3, 4, 5, 6], 8)
        self.assertEqual(bounds['unknown'], 2)
        self.assertEqual(bounds['lower'], 2.5)
        self.assertEqual(bounds['upper'], 4.5)

    def test_all_observed_collapses_to_exact_median(self):
        bounds = median_bounds([1, 2, 3, 4], 4)
        self.assertEqual(bounds['lower'], 2.5)
        self.assertEqual(bounds['upper'], 2.5)

    def test_too_few_observations_refuses_finite_bounds(self):
        with self.assertRaisesRegex(ValueError, 'too many missing'):
            median_bounds([1, 2, 3, 4], 8)


if __name__ == '__main__':
    unittest.main()
