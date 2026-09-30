from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from sys import path

path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from quant_lab.backtest import DatedTarget, run_backtest, run_backtest_targets
from quant_lab.cost_model import FEE_RATE_PER_SIDE
from quant_lab.paper_execution import execute_target
from quant_lab.strategies import SmaTrendStrategy


class StrategyAgnosticExecutionTest(unittest.TestCase):
    def test_backtest_accepts_dated_targets_from_any_producer(self) -> None:
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        prices = {'A': [10, 11, 12, 11, 13], 'B': [20, 19, 18, 20, 19]}
        decisions = [DatedTarget(dates[0], {'A': 1}),
                     DatedTarget(dates[1], {'A': 0.5, 'B': 0.5}),
                     DatedTarget(dates[2], {'B': 1}),
                     DatedTarget(dates[3], {})]
        result = run_backtest_targets('any-producer', dates, prices, decisions)
        self.assertEqual(len(result.points), len(dates))
        self.assertEqual(result.points[1].weights, {'A': 1.0})
        self.assertEqual(result.points[3].weights, {'B': 1.0})
        self.assertGreater(result.metrics['turnover'], 0)
        with self.assertRaisesRegex(ValueError, 'decision date'):
            run_backtest_targets('bad', dates, prices,
                                 [DatedTarget(dates[1], d.weights) for d in decisions])

    def test_default_fee_is_five_bps_on_each_rebalance_side(self) -> None:
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(3)]
        prices = {'A': [10.0, 10.0, 10.0]}
        decisions = [DatedTarget(dates[0], {'A': 1.0}),
                     DatedTarget(dates[1], {})]
        result = run_backtest_targets('fee-check', dates, prices, decisions)
        expected = (1 - FEE_RATE_PER_SIDE) ** 2
        self.assertEqual(FEE_RATE_PER_SIDE, 0.0005)
        self.assertAlmostEqual(result.points[-1].equity, expected)

    def test_single_strategy_adapter_matches_precomputed_targets(self) -> None:
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(6)]
        prices = {'A': [10, 11, 12, 13, 14, 15]}
        strategy = SmaTrendStrategy(fast_window=2, slow_window=3)
        direct = run_backtest(strategy, dates, prices)
        decisions = [DatedTarget(dates[i], strategy.weights({'A': prices['A'][:i + 1]}))
                     for i in range(len(dates) - 1)]
        generic = run_backtest_targets(strategy.name, dates, prices, decisions)
        self.assertEqual(direct.points, generic.points)
        self.assertEqual(direct.metrics, generic.metrics)

    def test_paper_execution_handles_budget_without_strategy(self) -> None:
        account = {'cash': 10000.0, 'shares': {'A': 0, 'B': 0},
                   'equity': 10000.0, 'cost_basis': {'A': 0.0, 'B': 0.0}}
        bars = {'A': {'close': 10.0, 'volume_lots': 100},
                'B': {'close': 20.0, 'volume_lots': 100}}
        after, orders = execute_target(account, {'A': 0.5, 'B': 0.5}, bars, 0.001)
        self.assertEqual(after['shares'], {'A': 500, 'B': 200})
        self.assertAlmostEqual(after['equity'], 9991.0)
        self.assertEqual(len(orders), 2)
        with self.assertRaisesRegex(ValueError, 'untradable'):
            execute_target(account, {'A': 0.5, 'B': 0.5},
                           {**bars, 'B': {'close': 20.0, 'volume_lots': 0}}, 0.001)


if __name__ == '__main__':
    unittest.main()
