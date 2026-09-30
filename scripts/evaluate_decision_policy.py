"""Retrospective diagnostic for a frozen dated-target decision policy.

This is not prospective validation or a live trading recommendation.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

from quant_lab.backtest import DatedTarget, period_metrics, run_backtest_targets, run_buy_and_hold
from quant_lab.decision_pipeline import decide, sha256_json, validate_policy
from paper_portfolio import load_panel, read_json


def evaluate(policy_path: Path, db: Path, end: str, stress_cost_rate: float | None = None) -> dict:
    policy = read_json(policy_path)
    validate_policy(policy)
    if date.fromisoformat(end) > datetime.now(timezone.utc).date():
        raise ValueError('cannot use future market date')
    dates, panel, _ = load_panel(db, sorted(policy['universe']), end)
    dated = [date.fromisoformat(day) for day in dates]
    decisions = [DatedTarget(dated[index], decide(policy, dates[index],
                  {key: values[:index + 1] for key, values in panel.items()})['target_weights'])
                 for index in range(len(dates) - 1)]
    strategy = run_backtest_targets(policy['policy_id'], dated, panel, decisions,
                                    policy['cost_rate'])
    baseline = run_buy_and_hold(dated, panel, policy['cost_rate'])
    stress_cost_rate = policy['stress_cost_rate'] if stress_cost_rate is None else stress_cost_rate
    if stress_cost_rate < policy['cost_rate'] or stress_cost_rate >= 0.5:
        raise ValueError('stress cost must be at least policy cost and below 50%')
    stress = run_backtest_targets(policy['policy_id'] + '-stress', dated, panel,
                                  decisions, stress_cost_rate)
    boundary = int((len(dates) - 1) * 0.7)
    return {'generated_at': datetime.now(timezone.utc).isoformat(),
            'status': 'retrospective_diagnostic_only',
            'independent_out_of_sample': False,
            'reason': 'policy and focus universe were selected with historical data already available',
            'policy_id': policy['policy_id'], 'policy_sha256': sha256_json(policy),
            'signal_panel_sha256': sha256_json(panel),
            'signal_price_basis': 'qfq',
            'execution_assumption': 'signal at close t, proxy rebalance at close t+1',
            'cost_rate': policy['cost_rate'], 'lot_size_modeled': False,
            'start': dates[0], 'end': dates[-1], 'sessions': len(dates),
            'strategy_metrics': dict(strategy.metrics),
            'same_pool_equal_hold_metrics': dict(baseline.metrics),
            'cost_stress': {'cost_rate': stress_cost_rate, 'strategy_metrics': dict(stress.metrics)},
            'early_period': {'start': dates[0], 'end': dates[boundary],
                             'strategy_metrics': period_metrics(strategy, 0, boundary),
                             'baseline_metrics': period_metrics(baseline, 0, boundary)},
            'recent_period': {'start': dates[boundary], 'end': dates[-1],
                              'strategy_metrics': period_metrics(strategy, boundary, len(dates) - 1),
                              'baseline_metrics': period_metrics(baseline, boundary, len(dates) - 1)}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', default='policies/ensemble-focus-three-v2-five-bps.json')
    parser.add_argument('--db', default='data/quant/market.sqlite3')
    parser.add_argument('--end', default='2026-09-29')
    parser.add_argument('--stress-cost-rate', type=float, default=None)
    parser.add_argument('--out', default='reports/quant/policy-evaluations/ensemble-focus-three-v2-five-bps-2026-09-29.json')
    args = parser.parse_args()
    result = evaluate(Path(args.policy), Path(args.db), args.end, args.stress_cost_rate)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError('evaluation file already exists; choose another --out')
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output.resolve()), 'status': result['status'],
                      'strategy_return': result['strategy_metrics']['total_return'],
                      'stress_return': result['cost_stress']['strategy_metrics']['total_return'],
                      'baseline_return': result['same_pool_equal_hold_metrics']['total_return']},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
