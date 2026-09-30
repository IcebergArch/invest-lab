"""One dated-target contract for single and ensemble research strategies."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Mapping, Sequence

from quant_lab.decision_ensemble import (PortfolioBudget, apply_budget,
                                         combine_decisions, proposal_from_strategy)
from quant_lab.strategy_catalog import get_strategy_definition

PIPELINE_VERSION = 'dated-target-v1'


def sha256_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_policy(policy: dict) -> None:
    if policy.get('schema_version') != 1 or not isinstance(policy.get('policy_id'), str):
        raise ValueError('invalid decision policy identity')
    date.fromisoformat(policy['start_asof'])
    universe = policy.get('universe')
    ids = policy.get('strategy_ids')
    if not isinstance(universe, list) or not universe or len(set(universe)) != len(universe):
        raise ValueError('invalid policy universe')
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise ValueError('invalid strategy list')
    mode = policy.get('mode')
    if mode not in ('single', 'ensemble') or (mode == 'single' and len(ids) != 1) or (mode == 'ensemble' and len(ids) < 2):
        raise ValueError('mode and strategy count disagree')
    for sid in ids:
        get_strategy_definition(sid)
    if mode == 'ensemble':
        weights = policy.get('function_weights')
        if not isinstance(weights, dict) or set(weights) != set(ids):
            raise ValueError('ensemble requires one weight per function')
        if any(not isinstance(w, (int, float)) or not math.isfinite(w) or w <= 0 for w in weights.values()):
            raise ValueError('invalid function weights')
    budget = policy.get('budget')
    if not isinstance(budget, dict):
        raise ValueError('missing portfolio budget')
    apply_budget({key: 0.0 for key in universe}, PortfolioBudget(**budget))
    if (not isinstance(policy.get('initial_cash'), (int, float)) or not math.isfinite(policy['initial_cash'])
            or policy['initial_cash'] <= 0 or not isinstance(policy.get('cost_rate'), (int, float))
            or not 0 <= policy['cost_rate'] < 0.5 or not isinstance(policy.get('buy_lot'), int)
            or policy['buy_lot'] <= 0):
        raise ValueError('invalid execution budget')
    if (not isinstance(policy.get('stress_cost_rate'), (int, float))
            or not math.isfinite(policy['stress_cost_rate'])
            or not policy['cost_rate'] < policy['stress_cost_rate'] < 0.5):
        raise ValueError('invalid stress transaction cost')
    validation = policy.get('validation')
    if (not isinstance(validation, dict)
            or not isinstance(validation.get('min_forward_sessions'), int)
            or validation['min_forward_sessions'] <= 0
            or validation.get('require_positive_excess_return') is not True
            or validation.get('require_no_worse_drawdown') is not True
            or validation.get('require_stress_excess_return') is not True):
        raise ValueError('incomplete forward validation policy')


def decide(policy: dict, asof: str, panel: Mapping[str, Sequence[float]]) -> dict:
    """Return frozen, auditable targets; no execution or outcome data is read."""
    validate_policy(policy)
    date.fromisoformat(asof)
    if set(panel) != set(policy['universe']):
        raise ValueError('signal universe differs from frozen policy')
    if not panel or len({len(v) for v in panel.values()}) != 1:
        raise ValueError('incomplete common-date factor panel')
    if any(not values or any(not math.isfinite(float(v)) or v <= 0 for v in values)
           for values in panel.values()):
        raise ValueError('invalid signal panel')
    input_hash = sha256_json(panel)
    proposals = [proposal_from_strategy(sid, asof, panel, input_hash)
                 for sid in policy['strategy_ids']]
    budget = PortfolioBudget(**policy['budget'])
    if policy['mode'] == 'ensemble':
        combined = combine_decisions(proposals, policy['function_weights'], budget)
        targets = combined['target_weights']
        explanation = {'policy_version': combined['policy_version'],
                       'function_weights': combined['function_weights'],
                       'contributions': combined['contributions'],
                       'unconstrained_targets': combined['unconstrained_targets']}
    else:
        targets = apply_budget(proposals[0].target_weights, budget)
        explanation = {'policy_version': 'single-target-v1',
                       'unconstrained_targets': dict(proposals[0].target_weights)}
    record = {'pipeline_version': PIPELINE_VERSION, 'policy_id': policy['policy_id'],
              'policy_sha256': sha256_json(policy), 'asof': asof, 'mode': policy['mode'],
              'signal_panel_sha256': input_hash,
              'proposals': [{'function_id': p.function_id, 'function_version': p.function_version,
                             'factor_ids': list(p.factor_ids), 'factor_values': dict(p.factor_values),
                             'proposed_targets': dict(p.target_weights)} for p in proposals],
              'budget': policy['budget'], 'aggregation': explanation,
              'target_weights': targets, 'cash_target_weight': 1 - sum(targets.values()),
              'status': 'research_only_unvalidated'}
    record['decision_sha256'] = sha256_json(record)
    return record
