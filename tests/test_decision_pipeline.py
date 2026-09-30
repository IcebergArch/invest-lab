from __future__ import annotations

import json
import unittest
from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[1]
path.insert(0, str(ROOT / 'src'))
from quant_lab.decision_pipeline import decide, validate_policy


class DecisionPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = json.loads((ROOT / 'policies' / 'ensemble-focus-three-v1.json').read_text())
        self.panel = {
            'stock:000338.SZ': [20 + i * 0.1 for i in range(70)],
            'stock:002475.SZ': [30 - i * 0.1 for i in range(70)],
            'stock:002714.SZ': [40 + (i % 5) * 0.1 for i in range(70)],
        }

    def test_ensemble_records_factors_proposals_and_budget(self) -> None:
        first = decide(self.policy, '2026-09-29', self.panel)
        second = decide(self.policy, '2026-09-29', self.panel)
        self.assertEqual(first['decision_sha256'], second['decision_sha256'])
        self.assertEqual(len(first['proposals']), 3)
        self.assertEqual({p['function_id'] for p in first['proposals']}, set(self.policy['strategy_ids']))
        self.assertIsNotNone(first['proposals'][0]['factor_values']['stock:000338.SZ']['sma_fast'])
        self.assertLessEqual(sum(first['target_weights'].values()), 0.8 + 1e-12)
        self.assertTrue(all(value <= 0.35 for value in first['target_weights'].values()))
        self.assertEqual(first['status'], 'research_only_unvalidated')

    def test_single_and_ensemble_share_target_contract(self) -> None:
        single = {**self.policy, 'mode': 'single', 'strategy_ids': ['sma-trend']}
        single.pop('function_weights')
        outcome = decide(single, '2026-09-29', self.panel)
        self.assertEqual(set(outcome['target_weights']), set(self.panel))
        self.assertEqual(outcome['mode'], 'single')
        self.assertEqual(len(outcome['proposals']), 1)

    def test_missing_function_weight_fails_closed(self) -> None:
        broken = {**self.policy, 'function_weights': {'sma-trend': 1}}
        with self.assertRaisesRegex(ValueError, 'one weight'):
            validate_policy(broken)

    def test_incomplete_panel_fails_closed(self) -> None:
        bad = {**self.panel, 'stock:002714.SZ': self.panel['stock:002714.SZ'][:-1]}
        with self.assertRaisesRegex(ValueError, 'common-date'):
            decide(self.policy, '2026-09-29', bad)


if __name__ == '__main__':
    unittest.main()
