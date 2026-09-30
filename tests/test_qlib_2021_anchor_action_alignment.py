from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

from qlib_2021_anchor_action_alignment import reconcile_same_day_action


class AnchorActionReconciliationTest(unittest.TestCase):
    def row(self, gross: str) -> dict:
        return {'dividRegistDate': '2021-05-19',
                'dividOperateDate': '2021-05-20',
                'dividPayDate': '2021-05-20',
                'dividStockMarketDate': '',
                'dividCashPsBeforeTax': gross,
                'dividStocksPs': '0.000000',
                'dividReserveToStockPs': ''}

    def test_compatible_provider_revision_is_one_economic_action(self) -> None:
        merged, diagnostic = reconcile_same_day_action(
            'stock:002241.SZ', '2021-05-20',
            [self.row(''), self.row('0.15')])
        self.assertEqual(merged['dividCashPsBeforeTax'], '0.15')
        self.assertEqual(diagnostic['provider_row_count'], 2)

    def test_conflicting_cash_cannot_be_silently_deduplicated(self) -> None:
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            reconcile_same_day_action('stock:002241.SZ', '2021-05-20',
                                      [self.row('0.15'), self.row('0.30')])


if __name__ == '__main__':
    unittest.main()
