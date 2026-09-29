"""Transparent, versioned 0–10 research score for return and drawdown.

This is a retrospective comparison against the *same-stock-pool* equal-weight
hold baseline.  It is not a probability, a live return estimate, or a strategy
promotion gate.  Independent forward evidence remains a separate condition.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping


POLICY_VERSION = "return-drawdown-relative-v1"
RETURN_WEIGHT = 0.5
DRAWDOWN_WEIGHT = 0.5
RETURN_ANCHOR = 0.20     # +20 percentage points of annual return -> 10
DRAWDOWN_ANCHOR = 0.50   # +50 percentage points of maximum drawdown -> 10


def _bounded(value: float) -> float:
    return max(0.0, min(10.0, value))


def _one_decimal(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def score_backtest(
    strategy_metrics: Mapping[str, float],
    benchmark_metrics: Mapping[str, float],
) -> dict[str, object]:
    """Compute a one-decimal score using equal 50% return/drawdown components.

    A same-period, same-universe equal-weight hold benchmark scores 5.0 by
    construction.  Clipping prevents extreme historical results from dominating
    the scale.  The anchors are policy parameters, not learned from this sample.
    """
    values: dict[str, float] = {}
    for prefix, metrics in (("strategy", strategy_metrics), ("benchmark", benchmark_metrics)):
        for key in ("cagr", "max_drawdown"):
            value = metrics.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{prefix} {key} missing")
            try:
                number = float(value)
            except OverflowError as exc:
                raise ValueError(f"{prefix} {key} invalid") from exc
            if not math.isfinite(number):
                raise ValueError(f"{prefix} {key} invalid")
            values[f"{prefix}_{key}"] = number
    for prefix in ("strategy", "benchmark"):
        if not -1.0 <= values[f"{prefix}_max_drawdown"] <= 0.0:
            raise ValueError("max_drawdown must be between -1 and 0")
        if values[f"{prefix}_cagr"] < -1.0:
            raise ValueError("cagr cannot be below -100%")

    return_delta = values["strategy_cagr"] - values["benchmark_cagr"]
    drawdown_delta = values["strategy_max_drawdown"] - values["benchmark_max_drawdown"]
    return_component = _bounded(5.0 + 5.0 * return_delta / RETURN_ANCHOR)
    drawdown_component = _bounded(5.0 + 5.0 * drawdown_delta / DRAWDOWN_ANCHOR)
    score = _one_decimal(RETURN_WEIGHT * return_component + DRAWDOWN_WEIGHT * drawdown_component)
    diagnostics: dict[str, dict[str, float | None]] = {}
    for key in ("total_return", "annual_volatility", "sharpe_rf0", "turnover"):
        pair: dict[str, float | None] = {}
        for label, source in (("strategy", strategy_metrics), ("benchmark", benchmark_metrics)):
            value = source.get(key)
            try:
                numeric = float(value) if value is not None and not isinstance(value, bool) else None
            except (TypeError, ValueError, OverflowError):
                numeric = None
            pair[label] = numeric if numeric is not None and math.isfinite(numeric) else None
        diagnostics[key] = pair
    return {
        "policy_version": POLICY_VERSION,
        "status": "retrospective_diagnostic_only",
        "score": score,
        "scale": {"minimum": 0.0, "maximum": 10.0, "decimal_places": 1},
        "benchmark": "same_universe_equal_weight_buy_and_hold",
        "components": {
            "return": {
                "metric": "cagr", "weight": RETURN_WEIGHT,
                "strategy_value": values["strategy_cagr"],
                "benchmark_value": values["benchmark_cagr"],
                "difference": return_delta,
                "normalized_score": _one_decimal(return_component),
            },
            "drawdown": {
                "metric": "max_drawdown", "weight": DRAWDOWN_WEIGHT,
                "strategy_value": values["strategy_max_drawdown"],
                "benchmark_value": values["benchmark_max_drawdown"],
                "difference": drawdown_delta,
                "normalized_score": _one_decimal(drawdown_component),
            },
        },
        "normalization": {
            "neutral_component_score": 5.0,
            "return_difference_for_full_scale": RETURN_ANCHOR,
            "drawdown_difference_for_full_scale": DRAWDOWN_ANCHOR,
            "clamp": [0.0, 10.0],
        },
        "additional_input_diagnostics": diagnostics,
        "future_weight_candidates": ["annual_volatility", "turnover", "cost_sensitivity",
                                     "independent_out_of_sample"],
        "additional_inputs_weighted": False,
    }
