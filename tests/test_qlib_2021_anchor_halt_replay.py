import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from qlib_2021_anchor_halt_replay import audit_halted_carry, simulate_rebalanced_with_halts
from raw_action_aware_sma_stress import simulate_rebalanced


class SuspensionAwareLedgerTests(unittest.TestCase):
    def setUp(self):
        self.dates = ['2021-01-04', '2021-01-05', '2021-01-06', '2021-01-07']
        self.symbols = ['stock:000001.SZ', 'stock:000002.SZ', 'stock:000003.SZ']
        self.panel = {symbol: [10.0] * 4 for symbol in self.symbols}
        self.panel[self.symbols[0]][3] = 5.0
        self.actions = {symbol: [] for symbol in self.symbols}
        self.targets = {
            self.dates[0]: {self.symbols[0]: .3,
                            self.symbols[1]: .25, self.symbols[2]: .25},
            self.dates[1]: {self.symbols[1]: .25, self.symbols[2]: .25},
            self.dates[2]: {self.symbols[1]: .25, self.symbols[2]: .25},
        }

    def test_all_tradable_matches_existing_ledger(self):
        mask = {symbol: [True] * 4 for symbol in self.symbols}
        new = simulate_rebalanced_with_halts(
            self.dates, self.panel, self.actions, self.targets, mask,
            entry=1, exit_index=3)
        old = simulate_rebalanced(
            self.dates, self.panel, self.actions, self.targets,
            entry=1, exit_index=3, lot_size=100)
        for field in ('net_return', 'max_drawdown', 'fee_cny'):
            self.assertAlmostEqual(new[field], old[field])

    def test_halted_sale_waits_for_reopening(self):
        all_trading = {symbol: [True] * 4 for symbol in self.symbols}
        halted = {symbol: list(values) for symbol, values in all_trading.items()}
        halted[self.symbols[0]][2] = False
        without_halt = simulate_rebalanced_with_halts(
            self.dates, self.panel, self.actions, self.targets, all_trading,
            entry=1, exit_index=3)
        with_halt = simulate_rebalanced_with_halts(
            self.dates, self.panel, self.actions, self.targets, halted,
            entry=1, exit_index=3)
        self.assertEqual(with_halt['blocked_order_days'], 1)
        self.assertEqual(with_halt['halted_stock_days'], 1)
        self.assertLess(with_halt['net_return'], without_halt['net_return'])
        self.assertEqual(with_halt['status'], 'fully_liquidated_raw_action_proxy')

    def test_halted_mark_must_equal_prior_tradable_close(self):
        raw = {'stock:000001.SZ': {'2021-01-05': 10.0, '2021-01-06': 9.0}}
        status = {'stock:000001.SZ': {'2021-01-05': True, '2021-01-06': False}}
        with self.assertRaisesRegex(ValueError, 'not carried prior close'):
            audit_halted_carry(self.dates[1:3], raw, status)


if __name__ == '__main__':
    unittest.main()
