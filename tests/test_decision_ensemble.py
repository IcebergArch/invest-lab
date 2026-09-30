from __future__ import annotations

import unittest
from pathlib import Path
from sys import path

path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from quant_lab.decision_ensemble import DecisionProposal, PortfolioBudget, combine_decisions


class DecisionEnsembleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.a = DecisionProposal('trend', '1', '2026-09-29', 'input-hash', ('sma',),
                                  {'stock:A': 1.0, 'stock:B': 0.0})
        self.b = DecisionProposal('momentum', '1', '2026-09-29', 'input-hash', ('momentum',),
                                  {'stock:A': 0.0, 'stock:B': 1.0})

    def test_both_functions_and_budget_determine_targets(self) -> None:
        result = combine_decisions([self.a, self.b], {'trend': 1, 'momentum': 1},
                                   PortfolioBudget(0.8, 0.35),
                                   current_weights={'stock:A': 0.0, 'stock:B': 0.5})
        self.assertEqual(result['target_weights'], {'stock:A': 0.35, 'stock:B': 0.35})
        self.assertAlmostEqual(result['cash_target_weight'], 0.3)
        self.assertEqual(result['actions_vs_current'], {'stock:A': 'BUY', 'stock:B': 'SELL'})
        self.assertEqual(result['contributions']['stock:A'], {'trend': 0.5, 'momentum': 0.0})

    def test_one_function_cannot_finalize_decision(self) -> None:
        with self.assertRaisesRegex(ValueError, 'at least two'):
            combine_decisions([self.a], {'trend': 1}, PortfolioBudget(1, 1))

    def test_mismatched_input_snapshot_is_rejected(self) -> None:
        other = DecisionProposal('momentum', '1', '2026-09-29', 'later-input', ('momentum',),
                                 self.b.target_weights)
        with self.assertRaisesRegex(ValueError, 'same point-in-time'):
            combine_decisions([self.a, other], {'trend': 1, 'momentum': 1}, PortfolioBudget(1, 1))

    def test_unequal_function_weights_change_decision(self) -> None:
        result = combine_decisions([self.a, self.b], {'trend': 3, 'momentum': 1}, PortfolioBudget(1, 1))
        self.assertEqual(result['target_weights'], {'stock:A': 0.75, 'stock:B': 0.25})


if __name__ == '__main__':
    unittest.main()
