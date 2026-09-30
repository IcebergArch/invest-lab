from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

from random_entry_baseline import episode
from raw_action_aware_hold_stress import Action
from raw_action_aware_sma_stress import simulate_rebalanced


class RawActionAwareSmaStressTest(unittest.TestCase):
    def fixture(self):
        dates = ['2021-01-04', '2021-01-05', '2021-01-06', '2021-01-07']
        panel = {'stock:000001.SZ': [10.0, 10.0, 11.0, 12.0],
                 'stock:000002.SZ': [10.0, 10.0, 9.0, 8.0],
                 'stock:000003.SZ': [10.0] * 4}
        actions = {symbol: [] for symbol in panel}
        return dates, panel, actions

    def test_no_action_fractional_replay_matches_frozen_target_engine(self) -> None:
        dates, panel, actions = self.fixture()
        target = {symbol: .8 / 3 for symbol in panel}
        targets = {day: target for day in dates[:-1]}
        raw = simulate_rebalanced(dates, panel, actions, targets,
                                  entry=1, exit_index=3, lot_size=None)
        frozen = episode(dates, panel, [target] * 3, 0, 3, .0005)
        self.assertEqual(raw['status'], 'fully_liquidated_raw_action_proxy')
        self.assertLess(abs(raw['net_return'] - frozen['return']), 1e-6)
        self.assertEqual(len(raw['first_orders']), 3)

    def test_pending_bonus_cannot_be_sold_before_listing(self) -> None:
        dates, panel, actions = self.fixture()
        panel['stock:000001.SZ'] = [100.0, 100.0, 50.0, 50.0]
        actions['stock:000001.SZ'] = [Action(
            'stock:000001.SZ', dates[1], dates[2], None, dates[3], 0.0, 1.0)]
        targets = {dates[0]: {'stock:000001.SZ': .35},
                   dates[1]: {}, dates[2]: {}}
        raw = simulate_rebalanced(dates, panel, actions, targets,
                                  entry=1, exit_index=3, lot_size=100)
        self.assertEqual(raw['status'], 'fully_liquidated_raw_action_proxy')
        self.assertEqual(raw['pending_sale_shortfall_count'], 1)
        self.assertEqual(raw['eligible_action_count'], 1)

    def test_latest_signal_is_executed_on_next_close(self) -> None:
        dates, panel, actions = self.fixture()
        targets = {dates[0]: {'stock:000001.SZ': .35},
                   dates[1]: {}, dates[2]: {}}
        raw = simulate_rebalanced(dates, panel, actions, targets,
                                  entry=1, exit_index=3, lot_size=None)
        self.assertEqual(raw['first_orders'][0]['side'], 'buy')
        self.assertEqual(raw['first_orders'][0]['raw_close'], 10.0)
        self.assertEqual(raw['order_count'], 2)
        self.assertGreater(raw['sell_gross'], raw['buy_gross'])


if __name__ == '__main__':
    unittest.main()
