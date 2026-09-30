"""Read-only, bounded summaries of append-only paper strategy accounts."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

ACCOUNTS = (
    ('single_sma', 'sma-trend-raw-lot-v3-five-bps-2026-09-29'),
    ('ensemble', 'ensemble-focus-three-v2-five-bps-runtime2-2026-09-29'),
    ('risk_baseline', 'random-entry-risk-baseline-v1-runtime2-2026-09-29'),
)


def _digest(value: object) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(body).hexdigest()


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('paper record must be an object')
    return value


def _drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def summarize_account(root: Path, account_id: str) -> dict:
    config = _read(root / 'config.json')
    paths = sorted((root / 'sessions').glob('????-??-??.json'))
    if not paths or len(paths) > 2000:
        raise ValueError('missing or excessive paper sessions')
    rows = []
    previous = None
    for path in paths:
        row = _read(path)
        if row.get('date') != path.stem or row.get('prior_sha256') != (_digest(previous) if previous else None):
            raise ValueError('broken paper session chain')
        if previous and config['kind'] == 'forward_strategy_agnostic_paper_pool':
            if row.get('executed_decision_sha256') != previous['pending_decision']['decision_sha256']:
                raise ValueError('executed decision not linked to prior session')
        rows.append(row)
        previous = row
    ensemble = config.get('kind') == 'forward_strategy_agnostic_paper_pool'
    if not ensemble and config.get('kind') != 'forward_strategy_regression_pool':
        raise ValueError('unknown paper account kind')
    if ensemble:
        policy = config['policy']
        if _digest(policy) != config['policy_sha256']:
            raise ValueError('paper policy hash mismatch')
        initial = float(policy['initial_cash'])
        min_sessions = int(policy['validation']['min_forward_sessions'])
        decision = rows[-1]['pending_decision']
        if _digest({k: v for k, v in decision.items() if k != 'decision_sha256'}) != decision['decision_sha256']:
            raise ValueError('pending decision hash mismatch')
        decision_id = decision['decision_sha256']
        cost_rate = float(policy['cost_rate'])
        stress_cost_rate = float(policy['stress_cost_rate'])
    else:
        initial = float(config['initial_cash'])
        min_sessions = 126
        decision_id = None
        cost_rate = float(config['cost_rate'])
        stress_cost_rate = None
    if not math.isfinite(initial) or initial <= 0:
        raise ValueError('invalid paper initial capital')
    strategy = [float(row['strategy']['equity']) for row in rows]
    baseline = [float(row['baseline']['equity']) for row in rows]
    if any(not math.isfinite(value) or value <= 0 for value in strategy + baseline):
        raise ValueError('invalid paper equity')
    strategy_return = strategy[-1] / initial - 1
    baseline_return = baseline[-1] / initial - 1
    observed = len(rows) - 1
    basic_gate = (observed >= min_sessions and strategy_return > baseline_return
                  and _drawdown(strategy) >= _drawdown(baseline))
    stress_return = stress_baseline_return = None
    if ensemble:
        stress = [float(row['stress_strategy']['equity']) for row in rows]
        stress_baseline = [float(row['stress_baseline']['equity']) for row in rows]
        if any(not math.isfinite(value) or value <= 0 for value in stress + stress_baseline):
            raise ValueError('invalid stress paper equity')
        stress_return = stress[-1] / initial - 1
        stress_baseline_return = stress_baseline[-1] / initial - 1
        gate = basic_gate and stress_return > stress_baseline_return
    else:
        gate = basic_gate
    return {'account_id': account_id, 'status': 'ready', 'asof': rows[-1]['date'],
            'observed_sessions': observed, 'minimum_forward_sessions': min_sessions,
            'cost_rate': cost_rate, 'stress_cost_rate': stress_cost_rate,
            'strategy_return': strategy_return, 'baseline_return': baseline_return,
            'strategy_max_drawdown': _drawdown(strategy),
            'baseline_max_drawdown': _drawdown(baseline),
            'stress_strategy_return': stress_return,
            'stress_baseline_return': stress_baseline_return,
            'assistance_status': 'human_review_candidate' if gate else 'research_only',
            'reason': '数值门槛通过，仍需人工核对交易可行性及公司行动。' if gate
                      else '前瞻样本或收益、回撤、费用压力门槛尚未通过。',
            'decision_sha256': decision_id}


def summarize_paper_pools(report_root: Path) -> dict:
    root = report_root / 'paper'
    pools = []
    for account_id, name in ACCOUNTS:
        account = root / name
        if not account.exists():
            pools.append({'account_id': account_id, 'status': 'not_initialized'})
            continue
        try:
            pools.append(summarize_account(account, account_id))
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
            pools.append({'account_id': account_id, 'status': 'unavailable',
                          'reason': '模拟账本无法通过完整性校验。'})
    return {'status': 'ready' if all(item['status'] == 'ready' for item in pools) else 'partial',
            'pools': pools}
