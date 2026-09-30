from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from sys import path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
path.insert(0, str(ROOT / 'src'))
path.insert(0, str(ROOT / 'scripts'))

from paper_portfolio import digest
from quant_lab.decision_pipeline import sha256_json
from quant_lab.paper_execution import execute_target
from quant_paper_pool import runtime_hash
import paper_same_budget_evaluator
from paper_same_budget_evaluator import evaluate_account


class MiddayShanghai(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 30, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        return value.astimezone(tz) if tz else value


class AfterCloseShanghai(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 30, 16, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
        return value.astimezone(tz) if tz else value


class SameBudgetShadowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'account'
        (self.root / 'sessions').mkdir(parents=True)
        self.policy = json.loads((ROOT / 'policies' /
            'ensemble-focus-three-v2-five-bps.json').read_text())
        self.policy['start_asof'] = '2026-09-25'
        self.keys = sorted(self.policy['universe'])
        self.initial = float(self.policy['initial_cash'])
        self.start = self.policy['start_asof']
        self.target = dict(zip(self.keys, (0.3, 0.3, 0.2)))
        self.config = {
            'schema_version': 1,
            'kind': 'forward_strategy_agnostic_paper_pool',
            'policy': self.policy,
            'created_at': '2026-09-25T08:00:00+00:00',
            'policy_sha256': sha256_json(self.policy),
            'runtime_sha256': runtime_hash(),
            'start_asof': self.start,
            'universe': self.keys,
            'signal_price_basis': 'qfq',
            'fill_price_basis': 'EastMoney unadjusted daily close',
            'fill_assumption': 'decision at close t, proxy fill at close t+1',
        }
        self._write_config()
        self.opening = {
            'date': self.start,
            'prior_sha256': None,
            'pending_decision': self._decision(self.start),
            'qfq_lineage': self._qfq(self.start, (10, 20, 40)),
            'raw_bars': self._raw(self.start, (10, 20, 40)),
            'strategy': self._empty(True),
            'baseline': self._empty(False),
            'stress_strategy': self._empty(True),
            'stress_baseline': self._empty(False),
            'orders': [],
            'baseline_orders': [],
            'status': 'pending_first_future_session',
        }
        self._write_session(self.opening)

    def _write_config(self) -> None:
        (self.root / 'config.json').write_text(json.dumps(self.config))

    def _write_session(self, row: dict) -> None:
        (self.root / 'sessions' / (row['date'] + '.json')).write_text(json.dumps(row))

    def _empty(self, basis: bool) -> dict:
        result = {'cash': self.initial, 'shares': {key: 0 for key in self.keys},
                  'equity': self.initial}
        if basis:
            result['cost_basis'] = {key: 0.0 for key in self.keys}
        return result

    def _decision(self, day: str) -> dict:
        result = {'pipeline_version': 'dated-target-v1',
                  'policy_id': self.policy['policy_id'],
                  'policy_sha256': sha256_json(self.policy),
                  'asof': day, 'mode': self.policy['mode'],
                  'budget': self.policy['budget'],
                  'signal_panel_sha256': 'a' * 64,
                  'target_weights': self.target,
                  'cash_target_weight': 1 - sum(self.target.values())}
        result['decision_sha256'] = sha256_json(result)
        return result

    def _raw(self, day: str, prices: tuple[int, int, int]) -> dict:
        return {key: {'close': float(price), 'volume_lots': 10000.0,
                      'adjustment': 'none', 'source_id': 'eastmoney_kline',
                      'payload_hash': 'b' * 64,
                      'fetched_at': f'{day}T08:00:00+00:00'}
                for key, price in zip(self.keys, prices)}

    def _qfq(self, day: str, prices: tuple[int, int, int]) -> dict:
        return {key: {'instrument_id': key, 'trade_date': day,
                      'close': float(price), 'adjustment': 'qfq',
                      'source_id': 'eastmoney_kline', 'payload_hash': 'c' * 64,
                      'run_id': 'synthetic',
                      'fetched_at': f'{day}T08:00:00+00:00'}
                for key, price in zip(self.keys, prices)}

    def _observed(self, previous: dict, day: str, prices: tuple[int, int, int],
                  first: bool) -> dict:
        raw = self._raw(day, prices)
        target = previous['pending_decision']['target_weights']
        strategy, orders = execute_target(previous['strategy'], target, raw, .0005,
                                          buy_lot=100)
        stress_strategy, stress_orders = execute_target(previous['stress_strategy'],
            target, raw, .0015, buy_lot=100)
        if first:
            baseline_target = {key: 1 / len(self.keys) for key in self.keys}
            baseline, baseline_orders = execute_target(previous['baseline'],
                baseline_target, raw, .0005, buy_lot=100, track_cost_basis=False)
            stress_baseline, stress_baseline_orders = execute_target(
                previous['stress_baseline'], baseline_target, raw, .0015,
                buy_lot=100, track_cost_basis=False)
        else:
            baseline = {**previous['baseline'], 'equity': previous['baseline']['cash']
                        + sum(previous['baseline']['shares'][key] * raw[key]['close']
                              for key in self.keys)}
            stress_baseline = {**previous['stress_baseline'],
                'equity': previous['stress_baseline']['cash']
                + sum(previous['stress_baseline']['shares'][key] * raw[key]['close']
                      for key in self.keys)}
            baseline_orders, stress_baseline_orders = [], []
        row = {'date': day, 'prior_sha256': digest(previous), 'status': 'observed',
               'executed_decision_sha256': previous['pending_decision']['decision_sha256'],
               'executed_decision_asof': previous['date'], 'executed_targets': target,
               'orders': orders, 'stress_orders': stress_orders,
               'baseline_orders': baseline_orders,
               'stress_baseline_orders': stress_baseline_orders,
               'strategy': strategy, 'stress_strategy': stress_strategy,
               'baseline': baseline, 'stress_baseline': stress_baseline,
               'pending_decision': self._decision(day),
               'qfq_lineage': self._qfq(day, prices), 'raw_bars': raw}
        self._write_session(row)
        return row

    def test_pending_is_not_a_zero_return_observation(self) -> None:
        result = evaluate_account(self.root)
        self.assertEqual(result['status'], 'pending_first_future_session')
        self.assertEqual(result['observed_sessions'], 0)
        self.assertIsNone(result['net_return'])
        self.assertIsNone(result['max_drawdown'])
        self.assertIsNone(result['strategy_minus_shadow'])
        self.assertEqual(result['shadow_first_day_orders']['normal'], [])
        self.assertEqual(result['matched_exposure_equal_order_counts']['normal'], 0)

    def test_first_day_fee_and_whole_lots(self) -> None:
        self._observed(self.opening, '2026-09-28', (10, 20, 40), True)
        result = evaluate_account(self.root)
        self.assertEqual(result['shadow_shares']['normal'], dict(zip(self.keys,
                          (2600, 1300, 600))))
        self.assertEqual(result['fees_paid']['shadow'], 38.0)
        self.assertEqual(result['fees_paid']['stress_shadow'], 114.0)
        self.assertEqual(result['fees_paid']['matched_exposure_equal'], 38.0)
        self.assertEqual(result['equity']['shadow'], 99962.0)
        self.assertEqual(result['equity']['stress_shadow'], 99886.0)
        self.assertEqual(len(result['shadow_first_day_orders']['normal']), 3)
        self.assertAlmostEqual(result['shadow_exit_fee_if_liquidated_now']['normal'], 38.0)

    def test_second_day_marks_drift_without_rebalancing(self) -> None:
        first = self._observed(self.opening, '2026-09-28', (10, 20, 40), True)
        self._observed(first, '2026-09-29', (20, 20, 40), False)
        result = evaluate_account(self.root)
        self.assertEqual(result['shadow_shares']['normal'], dict(zip(self.keys,
                          (2600, 1300, 600))))
        self.assertEqual(result['fees_paid']['shadow'], 38.0)
        self.assertEqual(result['equity']['shadow'], 125962.0)
        self.assertNotEqual(result['matched_exposure_equal_shares']['normal'],
                            result['shadow_shares']['normal'])
        self.assertGreater(result['fees_paid']['matched_exposure_equal'],
                           result['fees_paid']['shadow'])
        self.assertGreater(result['matched_exposure_equal_order_counts']['normal'], 3)
        self.assertEqual(result['daily_comparison'][1]['executed_target_gross'], .8)
        self.assertEqual(result['daily_comparison'][1]['fees_paid_today']['shadow'], 0)
        self.assertGreater(result['daily_comparison'][1]['fees_paid_today']['matched_exposure_equal'], 0)
        self.assertGreater(result['shadow_shares']['normal'][self.keys[0]] * 20,
                           result['shadow_shares']['normal'][self.keys[1]] * 20)
        self.assertEqual(len(result['daily_comparison']), 2)

    def test_bad_hash_chain_fails_closed(self) -> None:
        row = self._observed(self.opening, '2026-09-28', (10, 20, 40), True)
        row['prior_sha256'] = '0' * 64
        self._write_session(row)
        with self.assertRaisesRegex(ValueError, 'broken paper chain'):
            evaluate_account(self.root)

    def test_missing_raw_bar_and_replayed_ledger_fail_closed(self) -> None:
        row = self._observed(self.opening, '2026-09-28', (10, 20, 40), True)
        missing = copy.deepcopy(row)
        del missing['raw_bars'][self.keys[0]]
        self._write_session(missing)
        with self.assertRaisesRegex(ValueError, 'incomplete raw bars'):
            evaluate_account(self.root)
        row['strategy']['cash'] += 1
        self._write_session(row)
        with self.assertRaisesRegex(ValueError, 'strategy ledger or orders mismatch'):
            evaluate_account(self.root)

    def test_runtime_hash_mismatch_fails_closed(self) -> None:
        self.config['runtime_sha256'] = '0' * 64
        self._write_config()
        with self.assertRaisesRegex(ValueError, 'frozen runtime hash mismatch'):
            evaluate_account(self.root)

    def test_bar_fetched_before_session_close_fails_closed(self) -> None:
        row = self._observed(self.opening, '2026-09-28', (10, 20, 40), True)
        row['raw_bars'][self.keys[0]]['fetched_at'] = '2026-09-28T07:04:59+00:00'
        self._write_session(row)
        with self.assertRaisesRegex(ValueError, 'fetched before session close cutoff'):
            evaluate_account(self.root)
        row['raw_bars'][self.keys[0]]['fetched_at'] = '2026-09-28T07:05:00+00:00'
        row['qfq_lineage'][self.keys[0]]['fetched_at'] = '2026-09-28T07:04:59+00:00'
        self._write_session(row)
        with self.assertRaisesRegex(ValueError, 'fetched before session close cutoff'):
            evaluate_account(self.root)
        row['qfq_lineage'][self.keys[0]]['fetched_at'] = '2026-09-28T07:05:00+00:00'
        self._write_session(row)
        self.assertEqual(evaluate_account(self.root)['observed_sessions'], 1)

    def test_future_fetched_timestamp_fails_even_when_pending(self) -> None:
        self.opening['raw_bars'][self.keys[0]]['fetched_at'] = '2099-01-01T00:00:00+00:00'
        self._write_session(self.opening)
        with self.assertRaisesRegex(ValueError, 'fetched in the future'):
            evaluate_account(self.root)
        self.opening['raw_bars'][self.keys[0]]['fetched_at'] = f'{self.start}T08:00:00+00:00'
        self.opening['qfq_lineage'][self.keys[0]]['fetched_at'] = '2099-01-01T00:00:00+00:00'
        self._write_session(self.opening)
        with self.assertRaisesRegex(ValueError, 'fetched in the future'):
            evaluate_account(self.root)

    def test_opening_inputs_must_precede_account_creation(self) -> None:
        self.opening['raw_bars'][self.keys[0]]['fetched_at'] = '2026-09-25T08:00:01+00:00'
        self._write_session(self.opening)
        with self.assertRaisesRegex(ValueError, 'opening input fetched after account creation'):
            evaluate_account(self.root)
        self.opening['raw_bars'][self.keys[0]]['fetched_at'] = '2026-09-25T08:00:00+00:00'
        self.opening['qfq_lineage'][self.keys[0]]['fetched_at'] = '2026-09-25T08:00:01+00:00'
        self._write_session(self.opening)
        with self.assertRaisesRegex(ValueError, 'opening input fetched after account creation'):
            evaluate_account(self.root)

    def test_account_creation_must_precede_first_fill(self) -> None:
        self._observed(self.opening, '2026-09-28', (10, 20, 40), True)
        self.config['created_at'] = '2026-09-28T07:00:00+00:00'
        self._write_config()
        with self.assertRaisesRegex(ValueError, 'account created after first proxy fill close'):
            evaluate_account(self.root)

    def test_prior_input_late_for_second_fill_fails(self) -> None:
        first = self._observed(self.opening, '2026-09-28', (10, 20, 40), True)
        second = self._observed(first, '2026-09-29', (20, 20, 40), False)
        first['qfq_lineage'][self.keys[0]]['fetched_at'] = '2026-09-29T07:00:00+00:00'
        self._write_session(first)
        second['prior_sha256'] = digest(first)
        self._write_session(second)
        with self.assertRaisesRegex(ValueError, 'prior session input fetched after proxy fill close'):
            evaluate_account(self.root)

    def test_unfinished_and_future_session_dates_fail_closed(self) -> None:
        first = self._observed(self.opening, '2026-09-30', (10, 20, 40), True)
        with patch.object(paper_same_budget_evaluator, 'datetime', MiddayShanghai):
            with self.assertRaisesRegex(ValueError, 'future or not yet final'):
                evaluate_account(self.root)
        self._observed(first, '2026-10-01', (10, 20, 40), False)
        with patch.object(paper_same_budget_evaluator, 'datetime', AfterCloseShanghai):
            with self.assertRaisesRegex(ValueError, 'future or not yet final'):
                evaluate_account(self.root)


if __name__ == '__main__':
    unittest.main()
