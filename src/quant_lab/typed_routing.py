"""Frozen, price-only research routing shared by analysis and quant.

Types are descriptive regimes, not fitted labels or evidence of profit. Every
threshold is a versioned dev hypothesis. Unknown/risky regimes abstain.
"""
from __future__ import annotations

import math
from statistics import pstdev
from typing import Sequence

from quant_lab.factor_library import get_factor


ROUTER_VERSION = "typed-price-router-dev-v1"
MOMENTUM_LOOKBACK = 60
VOLATILITY_WINDOW = 20
TREND_FLOOR = 0.05
MAX_ANNUAL_VOLATILITY = 0.60


def classify_prices(closes: Sequence[float]) -> dict[str, object]:
    """Use only as-of closes; return a type and the activated method IDs."""
    if len(closes) < 61 or any(not math.isfinite(float(x)) or x <= 0 for x in closes):
        return {"type": "insufficient_history", "decision_method": None,
                "forecast_method": None, "factor_values": {}, "router_version": ROUTER_VERSION}
    momentum = get_factor("momentum").calculate(closes, MOMENTUM_LOOKBACK)
    returns = [closes[i] / closes[i - 1] - 1
               for i in range(len(closes) - VOLATILITY_WINDOW, len(closes))]
    annual_volatility = pstdev(returns) * math.sqrt(252)
    fast = get_factor("sma").calculate(closes, 20)
    slow = get_factor("sma").calculate(closes, 60)
    zscore = get_factor("zscore").calculate(closes, 20)
    factors = {"momentum_60": momentum, "annual_volatility_20": annual_volatility,
               "sma_20": fast, "sma_60": slow, "zscore_20": zscore}
    if annual_volatility > MAX_ANNUAL_VOLATILITY:
        kind, decision, forecast = "high_volatility", None, "last-observed-close"
    elif momentum is not None and momentum >= TREND_FLOOR:
        kind, decision, forecast = "uptrend", "sma-trend", "momentum-20"
    elif momentum is not None and abs(momentum) < TREND_FLOOR:
        kind, decision, forecast = "range", "mean-reversion-zscore", "last-observed-close"
    else:
        kind, decision, forecast = "downtrend", None, "last-observed-close"
    return {"type": kind, "decision_method": decision,
            "forecast_method": forecast, "factor_values": factors,
            "router_version": ROUTER_VERSION}
