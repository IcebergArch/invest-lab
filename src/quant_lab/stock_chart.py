"""Bounded, reproducible chart series for a single-stock research report.

Historical SMA transitions are signals observed at that day's close, not
executions.  The forward curve is an explicitly unvalidated research forecast.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Sequence

from quant_lab.forecast import EvaluationConfig, Momentum20Provider, evaluate_forecasts


CHART_VERSION = "single-stock-chart-v1"
SIGNAL_RULE_ID = "sma-20-60-close-transition"
SIGNAL_RULE_VERSION = "1"
HISTORY_LIMIT = 120
FORECAST_STEPS = 20


def _research_forecast(dates: Sequence[date], closes: Sequence[float],
                       instrument_id: str, price_basis: str) -> dict[str, Any]:
    provider = Momentum20Provider()
    evaluation = evaluate_forecasts(
        dates, closes, provider, instrument_id,
        config=EvaluationConfig(horizons=(FORECAST_STEPS,), min_context=120, step=20),
    )
    summary = evaluation["summary"].get(str(FORECAST_STEPS), {})
    projected = provider.forecast(closes, FORECAST_STEPS)
    metric = summary.get("mae_return")
    return {
        "status": "research_only",
        "price_basis": price_basis,
        "model_id": provider.model_id,
        "model_version": provider.model_revision,
        "asof": dates[-1].isoformat(),
        "horizon_trading_observations": FORECAST_STEPS,
        "validation": {
            "status": "exploratory",
            "method": "rolling_origin_same_history_not_independent_out_of_sample",
            "sample_count": summary.get("count", 0),
            "metric_label": "20-session mean absolute return error",
            "metric_value": metric if isinstance(metric, (int, float)) and math.isfinite(metric) else None,
            "skill_vs_random_walk": summary.get("mae_return_skill_vs_random_walk"),
        },
        "points": [
            {"step": step, "median": value}
            for step, value in enumerate(projected.values, start=1)
        ],
        "reason": "近 20 个交易观察点的增速外推，仅供比较；未通过独立时间外验证，不能作为买卖依据。",
    }


def build_stock_chart(
    dates: Sequence[date], closes: Sequence[float], *,
    instrument_id: str, price_basis: str,
    include_strategy_signals: bool,
    include_research_forecast: bool,
) -> dict[str, Any]:
    """Create display-bounded history while computing signals from all prior data.

    Only chronology visible to the caller is used. The caller must already
    have restricted the series to its report's as-of date and checked gaps.
    """
    if len(dates) != len(closes) or not dates:
        raise ValueError("chart requires matching nonempty dates and closes")
    if any(left >= right for left, right in zip(dates, dates[1:])):
        raise ValueError("chart dates must strictly increase")
    if any(not math.isfinite(float(close)) or float(close) <= 0 for close in closes):
        raise ValueError("chart closes must be finite and positive")
    if not price_basis:
        raise ValueError("chart price basis is required")

    start = max(0, len(dates) - HISTORY_LIMIT)
    history = [
        {"date": day.isoformat(), "close": float(close)}
        for day, close in zip(dates[start:], closes[start:])
    ]
    signals: list[dict[str, Any]] = []
    rule_position: list[dict[str, Any]] = []
    if include_strategy_signals and len(closes) >= 60:
        # Consecutive rolling sums allow the full history to be scanned once.
        fast_sum = sum(float(value) for value in closes[40:60])
        slow_sum = sum(float(value) for value in closes[:60])
        prior_position = 0
        for index in range(59, len(closes)):
            if index > 59:
                fast_sum += float(closes[index]) - float(closes[index - 20])
                slow_sum += float(closes[index]) - float(closes[index - 60])
            position = int(fast_sum / 20 > slow_sum / 60)
            if index >= start:
                day = dates[index].isoformat()
                rule_position.append({"date": day, "position": position})
                if position != prior_position:
                    signals.append({
                        "date": day,
                        "kind": "buy" if position else "sell",
                        "rule_id": SIGNAL_RULE_ID,
                        "rule_version": SIGNAL_RULE_VERSION,
                    })
            prior_position = position

    forecast: dict[str, Any] = {
        "status": "unavailable", "price_basis": price_basis,
        "model_id": None, "model_version": None,
        "asof": dates[-1].isoformat(), "points": [],
        "reason": "当前价格序列尚未完成可用于前瞻预测的验证。",
    }
    # At least one 20-session rolling-origin comparison must have matured.
    if include_research_forecast and len(closes) >= 140:
        forecast = _research_forecast(dates, closes, instrument_id, price_basis)

    return {
        "status": "ready" if len(history) >= 2 else "insufficient_data",
        "chart_version": CHART_VERSION,
        "price_basis": price_basis,
        "asof": dates[-1].isoformat(),
        "history": history,
        "signals": signals,
        "rule_position": rule_position,
        "signal_rule": {
            "rule_id": SIGNAL_RULE_ID,
            "rule_version": SIGNAL_RULE_VERSION,
            "status": "historical_diagnostic_only" if include_strategy_signals else "unavailable",
            "execution_assumption": "收盘形成信号，下一交易日收盘价代理成交；图中标记不是成交记录。",
        },
        "forecast": forecast,
    }
