from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from sys import path

path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from quant_lab.paper_status import _digest, summarize_paper_pools


class PaperStatusTest(unittest.TestCase):
    def test_read_only_summary_and_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'paper' / 'ensemble-focus-three-v2-five-bps-runtime2-2026-09-29'
            sessions = root / 'sessions'
            sessions.mkdir(parents=True)
            policy = {'initial_cash': 100.0, 'cost_rate': 0.0005, 'stress_cost_rate': 0.0015,
                      'validation': {'min_forward_sessions': 126}}
            (root / 'config.json').write_text(json.dumps({
                'kind': 'forward_strategy_agnostic_paper_pool',
                'policy': policy, 'policy_sha256': _digest(policy)}))
            decision = {'target_weights': {'stock:A': 0.5}, 'asof': '2026-09-29'}
            decision['decision_sha256'] = _digest(decision)
            start = {'date': '2026-09-29', 'prior_sha256': None,
                     'strategy': {'equity': 100.0}, 'baseline': {'equity': 100.0},
                     'stress_strategy': {'equity': 100.0}, 'stress_baseline': {'equity': 100.0},
                     'pending_decision': decision}
            (sessions / '2026-09-29.json').write_text(json.dumps(start))
            later = {'date': '2026-09-30', 'prior_sha256': _digest(start),
                     'strategy': {'equity': 101.0}, 'baseline': {'equity': 100.0},
                     'stress_strategy': {'equity': 100.5}, 'stress_baseline': {'equity': 99.5},
                     'pending_decision': decision,
                     'executed_decision_sha256': decision['decision_sha256']}
            (sessions / '2026-09-30.json').write_text(json.dumps(later))
            result = summarize_paper_pools(Path(tmp))
            ensemble = result['pools'][1]
            self.assertEqual(ensemble['status'], 'ready')
            self.assertEqual(ensemble['observed_sessions'], 1)
            self.assertEqual(ensemble['assistance_status'], 'research_only')
            self.assertAlmostEqual(ensemble['stress_strategy_return'], 0.005)
            start['strategy']['equity'] = 105.0
            (sessions / '2026-09-29.json').write_text(json.dumps(start))
            broken = summarize_paper_pools(Path(tmp))['pools'][1]
            self.assertEqual(broken['status'], 'unavailable')
            self.assertNotIn('equity', json.dumps(broken))


if __name__ == '__main__':
    unittest.main()
