"""Point-in-time price features shared by strategies and reports."""
from __future__ import annotations

import math
from typing import Mapping, Optional, Sequence

PriceHistory = Mapping[str, Sequence[float]]
FEATURE_VERSION = "price-v1"


def sma(values: Sequence[float], window: int) -> Optional[float]:
    if window <= 0:
        raise ValueError("window must be positive")
    return sum(values[-window:]) / window if len(values) >= window else None


def momentum(values: Sequence[float], lookback: int) -> Optional[float]:
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    return values[-1] / values[-lookback - 1] - 1.0 if len(values) > lookback else None


def zscore(values: Sequence[float], window: int) -> Optional[float]:
    if window < 2:
        raise ValueError("window must be at least 2")
    if len(values) < window:
        return None
    sample = values[-window:]
    mean = sum(sample) / window
    sigma = math.sqrt(sum((value - mean) ** 2 for value in sample) / window)
    return (sample[-1] - mean) / sigma if sigma else None


def batch_momentum(history: PriceHistory, lookback: int) -> dict[str, float]:
    return {key: value for key, prices in history.items()
            if (value := momentum(prices, lookback)) is not None}
