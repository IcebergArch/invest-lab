from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

from raw_action_aware_hold_stress import Action, simulate_hold
from random_entry_baseline import cash_matched_hold_episode


class ActionAwareHoldTest(unittest.TestCase):
    def fixture(self, prices):
        dates = ['2021-01-04', '2021-01-05', '2021-01-06']
        panel = {'stock:000001.SZ': prices,
                 'stock:000002.SZ': [100.0] * 3,
                 'stock:000003.SZ': [100.0] * 3}
        actions = {key: [] for key in panel}
        return dates, panel, actions

    def test_flat_prices_pay_buy_and_sell_five_bps(self) -> None:
        dates, panel, actions = self.fixture([100.0] * 3)
        row = simulate_hold(dates, panel, actions, entry=0, exit_index=2,
                            lot_size=None)
        self.assertEqual(row['status'], 'fully_liquidated_raw_action_proxy')
        self.assertAlmostEqual(row['net_return'], -.8 * .0005 * 2)
        self.assertAlmostEqual(row['initial_gross_weight'], .8)

    def test_no_action_fractional_ledger_replays_existing_hold_proxy(self) -> None:
        dates, panel, actions = self.fixture([100.0, 110.0, 121.0])
        raw = simulate_hold(dates, panel, actions, entry=1, exit_index=2,
                            lot_size=None)
        frozen_formula = cash_matched_hold_episode(
            dates, panel, start=0, horizon=2, fee=.0005)
        self.assertAlmostEqual(raw['net_return'], frozen_formula['return'])

    def test_buy_lots_keep_unspent_cash(self) -> None:
        dates, panel, actions = self.fixture([100.0] * 3)
        row = simulate_hold(dates, panel, actions, entry=0, exit_index=2,
                            lot_size=100)
        self.assertEqual(row['status'], 'fully_liquidated_raw_action_proxy')
        self.assertEqual([fill['shares'] for fill in row['initial_fills']],
                         [200, 200, 200])
        self.assertAlmostEqual(row['initial_gross_weight'], .6)
        self.assertAlmostEqual(row['net_return'], -.6 * .0005 * 2)

    def test_record_entitlement_and_delayed_bonus_delivery(self) -> None:
        dates, panel, actions = self.fixture([100.0, 50.0, 50.0])
        actions['stock:000001.SZ'] = [Action(
            'stock:000001.SZ', dates[0], dates[1], None, dates[2], 0.0, 1.0)]
        unsettled = simulate_hold(dates, panel, actions, entry=0, exit_index=1,
                                  lot_size=100)
        self.assertEqual(unsettled['status'], 'unsettled_corporate_action_at_horizon')
        self.assertIsNone(unsettled['net_return'])
        settled = simulate_hold(dates, panel, actions, entry=0, exit_index=2,
                                lot_size=100)
        self.assertEqual(settled['status'], 'fully_liquidated_raw_action_proxy')
        self.assertEqual(settled['eligible_action_count'], 1)
        self.assertGreater(settled['bonus_shares_credited'], 0)
        self.assertAlmostEqual(settled['net_return'],
                               -settled['total_fee'] / 100_000)

    def test_cash_dividend_offsets_ex_price_drop(self) -> None:
        dates, panel, actions = self.fixture([100.0, 90.0, 90.0])
        actions['stock:000001.SZ'] = [Action(
            'stock:000001.SZ', dates[0], dates[1], dates[1], None, 10.0, 0.0)]
        row = simulate_hold(dates, panel, actions, entry=0, exit_index=2,
                            lot_size=None)
        self.assertEqual(row['status'], 'fully_liquidated_raw_action_proxy')
        self.assertGreater(row['cash_dividend_before_tax'], 0)
        self.assertAlmostEqual(row['net_return'],
                               -row['total_fee'] / 100_000)

    def test_registered_but_not_yet_operated_right_stays_unsettled(self) -> None:
        dates, panel, actions = self.fixture([100.0, 100.0, 90.0])
        actions['stock:000001.SZ'] = [Action(
            'stock:000001.SZ', dates[0], dates[2], dates[2], None, 10.0, 0.0)]
        row = simulate_hold(dates, panel, actions, entry=0, exit_index=1,
                            lot_size=None)
        self.assertEqual(row['status'], 'unsettled_corporate_action_at_horizon')
        self.assertEqual(row['pending_claim_count'], 1)
        self.assertIsNone(row['net_return'])
        self.assertIsNone(row['marked_return_at_horizon'])

    def test_ex_date_buyer_does_not_receive_prior_record_entitlement(self) -> None:
        dates, panel, actions = self.fixture([100.0, 90.0, 90.0])
        actions['stock:000001.SZ'] = [Action(
            'stock:000001.SZ', dates[0], dates[1], dates[1], None, 10.0, 0.0)]
        row = simulate_hold(dates, panel, actions, entry=1, exit_index=2,
                            lot_size=None)
        self.assertEqual(row['eligible_action_count'], 0)
        self.assertEqual(row['cash_dividend_before_tax'], 0)
        self.assertAlmostEqual(row['net_return'], -.8 * .0005 * 2)


if __name__ == '__main__':
    unittest.main()
