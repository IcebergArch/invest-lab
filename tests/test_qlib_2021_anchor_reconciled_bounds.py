import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from qlib_2021_anchor_reconciled_bounds import merge_cases


class ReconciledCaseTests(unittest.TestCase):
    def test_recovery_replaces_only_predeclared_halt_case(self):
        source = {'case_id': 'a', 'status': 'daily_raw_close_proxy_unavailable',
                  'segment': 'early', 'horizon_sessions': 126, 'seed': 202,
                  'pool': 'pool_01', 'factor_anomaly_overlap': False,
                  'returns': None}
        eligible = {'a': {'has_halt': True, 'has_supplier_outdate_gap': False,
                          'has_st': False, 'entry_all_three_normal_non_st': True,
                          'exit_all_three_normal_non_st': True}}
        outcome = {'status': 'fully_liquidated_raw_action_proxy',
                   'net_return': .01, 'max_drawdown': -.1}
        replay = {**source, 'status': 'fully_liquidated_halt_mark_proxy',
                  'returns': {key: outcome for key in (
                      'sma', 'same_exposure_equal', 'passive_fixed_80')},
                  'halted_stock_days': 2}
        merged = merge_cases([source], [replay], eligible)
        self.assertEqual(merged[0]['status'], 'fully_liquidated_halt_mark_proxy')
        self.assertEqual(merged[0]['halted_stock_days'], 2)

    def test_outdate_case_cannot_be_replaced(self):
        source = {'case_id': 'a', 'status': 'daily_raw_close_proxy_unavailable',
                  'segment': 'early', 'horizon_sessions': 126, 'seed': 202,
                  'pool': 'pool_01', 'returns': None}
        eligible = {'a': {'has_halt': True, 'has_supplier_outdate_gap': True,
                          'has_st': False, 'entry_all_three_normal_non_st': True,
                          'exit_all_three_normal_non_st': True}}
        with self.assertRaisesRegex(ValueError, 'outside predeclared'):
            merge_cases([source], [{**source, 'status': 'unsettled_corporate_action_at_horizon'}],
                        eligible)


if __name__ == '__main__':
    unittest.main()
