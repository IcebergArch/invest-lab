from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from pool_corporate_action_alignment import join_events


class CorporateActionAlignmentTest(unittest.TestCase):
    def fixture(self):
        identifiers = [f'stock:{index:06d}.SZ' for index in range(36)]
        snapshots = []
        for year in (2021, 2022, 2023):
            responses = [{'instrument_id': identifier, 'row_count': 0, 'rows': []}
                         for identifier in identifiers]
            snapshots.append({
                'snapshot_version': 'baostock-36-pool-corporate-actions-v1',
                'status': 'provider_response_captured_not_pit',
                'operate_year': year, 'requested_stock_count': 36,
                'year_type': 'operate', 'response_row_count': 0,
                'responses': responses,
            })
        action = {field: '' for field in (
            'dividPlanAnnounceDate', 'dividRegistDate', 'dividOperateDate',
            'dividPayDate', 'dividCashStock', 'dividCashPsBeforeTax',
            'dividStocksPs', 'dividReserveToStockPs')}
        action['dividOperateDate'] = '2021-06-15'
        action['dividCashStock'] = '10派1元'
        snapshots[0]['responses'][0]['rows'] = [action]
        snapshots[0]['responses'][0]['row_count'] = 1
        snapshots[0]['response_row_count'] = 1
        price = {'study_version': 'qlib-raw-price-factor-audit-v1',
                 'stock_count': 36, 'return_difference_over_1pct_count': 1,
                 'factor_jump_events_over_1pct': [
                     {'instrument_id': identifiers[0], 'date': '2021-06-15',
                      'adjusted_to_raw_gross_return_ratio': 1.1,
                      'factor_ratio': 1.1}]}
        return price, snapshots

    def test_same_day_join_and_missing_action_failure(self) -> None:
        price, snapshots = self.fixture()
        joined, smaller = join_events(price, snapshots)
        self.assertEqual(len(joined), 1)
        self.assertEqual(smaller, 0)
        self.assertEqual(joined[0]['provider_record']['dividCashStock'], '10派1元')
        damaged = copy.deepcopy(snapshots)
        damaged[0]['responses'][0]['rows'][0]['dividOperateDate'] = '2021-06-16'
        with self.assertRaisesRegex(ValueError, 'lack same-day supplier actions'):
            join_events(price, damaged)


if __name__ == '__main__':
    unittest.main()
