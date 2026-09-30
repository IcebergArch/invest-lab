from __future__ import annotations

from quant_lab.cost_model import FEE_RATE_PER_SIDE

import math
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence

from quant_lab.strategies import Strategy


@dataclass(frozen=True)
class BacktestPoint:
    trade_date: date
    equity: float
    daily_return: float
    turnover: float
    weights: Mapping[str, float]


@dataclass(frozen=True)
class BacktestResult:
    strategy_name: str
    points: Sequence[BacktestPoint]
    metrics: Mapping[str, float]


@dataclass(frozen=True)
class DatedTarget:
    decision_date: date
    weights: Mapping[str, float]


def run_backtest_targets(
    name: str,
    dates: Sequence[date],
    close_panel: Mapping[str, Sequence[float]],
    decisions: Sequence[DatedTarget],
    cost_rate: float = FEE_RATE_PER_SIDE,
) -> BacktestResult:
    """Execute previously computed targets; the producer can be any strategy."""
    _validate_inputs(dates, close_panel, cost_rate)
    if len(decisions) != len(dates) - 1:
        raise ValueError("one decision is required for each date before the last")
    ids = sorted(close_panel)
    targets = []
    for index, decision in enumerate(decisions):
        if decision.decision_date != dates[index]:
            raise ValueError("decision date does not match market calendar")
        targets.append(_normalize_weights(decision.weights, ids))
    weights: dict[str, float] = {}
    equity = 1.0
    points = [BacktestPoint(dates[0], equity, 0.0, 0.0, {})]
    for index in range(1, len(dates)):
        previous_equity = equity
        growth = {key: close_panel[key][index] / close_panel[key][index - 1] for key in ids}
        gross_factor = 1.0 - sum(weights.values()) + sum(
            weights.get(key, 0.0) * growth[key] for key in ids
        )
        equity *= gross_factor
        pretrade = {key: weights[key] * growth[key] / gross_factor for key in weights}
        turnover = _turnover(pretrade, targets[index - 1])
        equity *= 1.0 - turnover * cost_rate
        weights = targets[index - 1]
        points.append(BacktestPoint(dates[index], equity,
                                    equity / previous_equity - 1.0, turnover, dict(weights)))
    return BacktestResult(name, points, calculate_metrics(points))


def run_backtest(
    strategy: Strategy,
    dates: Sequence[date],
    close_panel: Mapping[str, Sequence[float]],
    cost_rate: float = FEE_RATE_PER_SIDE,
) -> BacktestResult:
    """Adapt one strategy into dated targets, then use the generic evaluator."""
    _validate_inputs(dates, close_panel, cost_rate)
    decisions = [DatedTarget(dates[index], strategy.weights(
        {key: values[:index + 1] for key, values in close_panel.items()}))
        for index in range(len(dates) - 1)]
    return run_backtest_targets(strategy.name, dates, close_panel, decisions, cost_rate)

def run_buy_and_hold(
    dates: Sequence[date],
    close_panel: Mapping[str, Sequence[float]],
    cost_rate: float = FEE_RATE_PER_SIDE,
    name: str = "equal-weight-buy-and-hold",
) -> BacktestResult:
    """Equal-weight entry at the second close, then hold without rebalancing.

    This shares the strategy engine's next-close entry and cost convention.
    The index-1 close cannot earn the index-0 to index-1 return.
    """
    _validate_inputs(dates, close_panel, cost_rate)
    ids = sorted(close_panel)
    initial_weight = 1.0 / len(ids)
    weights: dict[str, float] = {}
    equity = 1.0
    points = [BacktestPoint(dates[0], equity, 0.0, 0.0, {})]
    for index in range(1, len(dates)):
        previous_equity = equity
        if index == 1:
            equity *= 1.0 - cost_rate
            weights = {key: initial_weight for key in ids}
            turnover = 1.0
        else:
            growth = {key: close_panel[key][index] / close_panel[key][index - 1] for key in ids}
            gross_factor = sum(weights[key] * growth[key] for key in ids)
            equity *= gross_factor
            weights = {key: weights[key] * growth[key] / gross_factor for key in ids}
            turnover = 0.0
        points.append(BacktestPoint(
            dates[index], equity, equity / previous_equity - 1.0, turnover, dict(weights)
        ))
    return BacktestResult(name, points, calculate_metrics(points))


def period_metrics(result: BacktestResult, start_index: int, end_index: int) -> dict[str, float]:
    """Rebase an already executed portfolio; preserve its state at the split."""
    if start_index < 0 or end_index >= len(result.points) or start_index >= end_index:
        raise ValueError("period must contain at least two ordered equity points")
    base = result.points[start_index].equity
    if base <= 0:
        raise ValueError("period start equity must be positive")
    selected = result.points[start_index:end_index + 1]
    rebased = [BacktestPoint(selected[0].trade_date, 1.0, 0.0, 0.0,
                             selected[0].weights)]
    rebased.extend(
        BacktestPoint(point.trade_date, point.equity / base, point.daily_return,
                      point.turnover, point.weights)
        for point in selected[1:]
    )
    return calculate_metrics(rebased)


def _validate_inputs(
    dates: Sequence[date], close_panel: Mapping[str, Sequence[float]], cost_rate: float
) -> None:
    if len(dates) < 2 or any(a >= b for a, b in zip(dates, dates[1:])):
        raise ValueError("at least two strictly increasing dates are required")
    if not close_panel:
        raise ValueError("close_panel cannot be empty")
    if not math.isfinite(cost_rate) or cost_rate < 0 or cost_rate >= 0.5:
        raise ValueError("cost_rate must be finite and between 0 and 0.5")
    for instrument_id, closes in close_panel.items():
        if len(closes) != len(dates):
            raise ValueError(f"{instrument_id}: close series length does not match dates")
        if any(value <= 0 or not math.isfinite(value) for value in closes):
            raise ValueError(f"{instrument_id}: close prices must be finite and positive")


def _normalize_weights(weights: Mapping[str, float], allowed: Sequence[str]) -> dict[str, float]:
    unknown = sorted(set(weights) - set(allowed))
    if unknown:
        raise ValueError(f"strategy returned unknown instruments: {unknown}")
    normalized = {key: float(value) for key, value in weights.items() if value != 0}
    if any(value < 0 or not math.isfinite(value) for value in normalized.values()):
        raise ValueError("weights must be finite and non-negative")
    if sum(normalized.values()) > 1.0 + 1e-12:
        raise ValueError("weights cannot sum above 1")
    return normalized


def _turnover(before: Mapping[str, float], after: Mapping[str, float]) -> float:
    return sum(abs(after.get(key, 0.0) - before.get(key, 0.0)) for key in set(before) | set(after))


def calculate_metrics(points: Sequence[BacktestPoint]) -> dict[str, float]:
    if not points:
        raise ValueError("points cannot be empty")
    returns = [point.daily_return for point in points[1:]]
    total_return = points[-1].equity - 1.0
    years = max((points[-1].trade_date - points[0].trade_date).days / 365.25, 1 / 365.25)
    cagr = points[-1].equity ** (1 / years) - 1.0 if points[-1].equity > 0 else -1.0
    average = sum(returns) / len(returns) if returns else 0.0
    variance = sum((value - average) ** 2 for value in returns) / len(returns) if returns else 0.0
    annual_volatility = math.sqrt(variance) * math.sqrt(252)
    sharpe = average / math.sqrt(variance) * math.sqrt(252) if variance > 0 else 0.0
    peak = points[0].equity
    max_drawdown = 0.0
    for point in points:
        peak = max(peak, point.equity)
        max_drawdown = min(max_drawdown, point.equity / peak - 1.0)
    return {
        "total_return": total_return,
        "cagr": cagr,
        "annual_volatility": annual_volatility,
        "sharpe_rf0": sharpe,
        "max_drawdown": max_drawdown,
        "turnover": sum(point.turnover for point in points),
    }
