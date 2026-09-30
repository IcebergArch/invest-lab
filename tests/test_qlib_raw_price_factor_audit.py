from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

from qlib_raw_price_factor_audit import price_comparison


class PriceFactorAuditTest(unittest.TestCase):
    def test_split_like_factor_jump_explains_raw_price_drop(self) -> None:
        row = price_comparison(100.0, 50.0, 10.0, 10.0, .1, .2)
        self.assertEqual(row['raw_return'], -.5)
        self.assertEqual(row['adjusted_return'], 0.0)
        self.assertEqual(row['adjusted_to_raw_gross_return_ratio'], 2.0)
        self.assertEqual(row['factor_ratio'], 2.0)
        self.assertEqual(row['factor_explanation_residual'], 0.0)

    def test_nonpositive_or_invalid_input_fails(self) -> None:
        with self.assertRaises(ValueError):
            price_comparison(100.0, 0.0, 10.0, 10.0, .1, .2)
        with self.assertRaises(ValueError):
            price_comparison(100.0, float('nan'), 10.0, 10.0, .1, .2)


if __name__ == '__main__':
    unittest.main()
