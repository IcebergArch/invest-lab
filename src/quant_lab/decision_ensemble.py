"""Versioned interface for combining multiple decision functions before sizing.

This is a research component. Existing single-strategy paper accounts do not use it.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from quant_lab.factor_library import get_factor
from quant_lab.strategy_catalog import get_strategy_definition

POLICY_VERSION = 'weighted-targets-v1'


def _fingerprint(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True)
class DecisionProposal:
    function_id: str
    function_version: str
    asof: str
    input_sha256: str
    factor_ids: tuple[str, ...]
    target_weights: Mapping[str, float]
    factor_values: Mapping[str, Mapping[str, float | None]] = field(default_factory=dict)


@dataclass(frozen=True)
class PortfolioBudget:
    max_gross_weight: float
    max_stock_weight: float


def apply_budget(weights: Mapping[str, float], budget: PortfolioBudget) -> dict[str, float]:
    """Apply long-only single-name and gross caps to any strategy target."""
    if (not math.isfinite(budget.max_gross_weight) or not math.isfinite(budget.max_stock_weight)
            or not 0 < budget.max_gross_weight <= 1 or not 0 < budget.max_stock_weight <= 1):
        raise ValueError('invalid portfolio budget')
    values = {key: float(value) for key, value in weights.items()}
    if (not values or any(not math.isfinite(value) or value < 0 for value in values.values())
            or sum(values.values()) > 1 + 1e-12):
        raise ValueError('invalid proposed target weights')
    capped = {key: min(value, budget.max_stock_weight) for key, value in values.items()}
    gross = sum(capped.values())
    scale = min(1.0, budget.max_gross_weight / gross) if gross else 1.0
    return {key: capped[key] * scale for key in sorted(capped)}


def proposal_from_strategy(strategy_id: str, asof: str,
                           panel: Mapping[str, Sequence[float]],
                           input_sha256: str) -> DecisionProposal:
    """Adapt a registered factor-driven strategy to the proposal contract."""
    definition = get_strategy_definition(strategy_id)
    strategy = definition.build()
    weights = strategy.weights(panel)
    factors: dict[str, dict[str, float | None]] = {}
    for key in sorted(panel):
        prices = panel[key]
        if strategy_id == 'sma-trend':
            factor = get_factor('sma')
            factors[key] = {'sma_fast': factor.calculate(prices, strategy.fast_window),
                            'sma_slow': factor.calculate(prices, strategy.slow_window)}
        elif strategy_id == 'cross-sectional-momentum':
            factors[key] = {'momentum': get_factor('momentum').calculate(prices, strategy.lookback)}
        elif strategy_id == 'mean-reversion-zscore':
            factors[key] = {'zscore': get_factor('zscore').calculate(prices, strategy.window)}
        else:
            raise ValueError(f'factor snapshot unavailable for {strategy_id}')
    return DecisionProposal(strategy_id, definition.version, asof, input_sha256,
                            definition.factor_ids,
                            {key: float(weights.get(key, 0.0)) for key in sorted(panel)}, factors)


def combine_decisions(proposals: Sequence[DecisionProposal],
                      function_weights: Mapping[str, float], budget: PortfolioBudget,
                      *, current_weights: Mapping[str, float] | None = None) -> dict:
    """Require >=2 complete proposals, then apply one explicit combination policy.

    Targets are a weighted average of each function's targets, capped by the
    position and gross budgets. Unallocated value remains cash. This policy is
    an experimental default, not a claim of predictive validity.
    """
    if len(proposals) < 2:
        raise ValueError('at least two decision functions are required')
    ids = [p.function_id for p in proposals]
    if len(set(ids)) != len(ids) or set(ids) != set(function_weights):
        raise ValueError('duplicate function or missing function weight')
    asof = proposals[0].asof
    universe = set(proposals[0].target_weights)
    if not universe or any(p.asof != asof or set(p.target_weights) != universe for p in proposals):
        raise ValueError('proposals must share date and complete universe')
    if len({p.input_sha256 for p in proposals}) != 1:
        raise ValueError('proposals must use the same point-in-time input snapshot')
    for p in proposals:
        values = list(p.target_weights.values())
        if any(not math.isfinite(float(v)) or v < 0 for v in values) or sum(values) > 1 + 1e-12:
            raise ValueError('invalid function target weights')
    mix = {key: float(value) for key, value in function_weights.items()}
    if any(not math.isfinite(value) or value <= 0 for value in mix.values()):
        raise ValueError('function weights must be finite and positive')
    total_mix = sum(mix.values())
    contributions = {key: {p.function_id: mix[p.function_id] / total_mix * p.target_weights[key]
                           for p in proposals} for key in sorted(universe)}
    uncapped = {key: sum(contributions[key].values()) for key in sorted(universe)}
    target = apply_budget(uncapped, budget)
    if current_weights is not None and set(current_weights) != universe:
        raise ValueError('current positions must cover the decision universe')
    actions = ({key: 'BUY' if target[key] > float(current_weights[key]) + 1e-9
                else 'SELL' if target[key] < float(current_weights[key]) - 1e-9 else 'HOLD'
                for key in sorted(universe)} if current_weights is not None else None)
    record = {'asof': asof, 'policy_version': POLICY_VERSION,
              'input_sha256': proposals[0].input_sha256,
              'function_weights': mix,
              'proposals': [{'function_id': p.function_id, 'function_version': p.function_version,
                             'factor_ids': list(p.factor_ids),
                             'factor_values': dict(p.factor_values),
                             'target_weights': dict(p.target_weights)} for p in proposals],
              'budget': {'max_gross_weight': budget.max_gross_weight,
                         'max_stock_weight': budget.max_stock_weight},
              'contributions': contributions, 'unconstrained_targets': uncapped,
              'target_weights': target, 'cash_target_weight': 1 - sum(target.values()),
              'actions_vs_current': actions,
              'status': 'research_only_unvalidated'}
    record['decision_sha256'] = _fingerprint(record)
    return record
