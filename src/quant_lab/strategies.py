from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from quant_lab.factor_library import get_factor


PriceHistory = Mapping[str, Sequence[float]]
Weights = Mapping[str, float]
_SMA = get_factor("sma")
_MOMENTUM = get_factor("momentum")
_ZSCORE = get_factor("zscore")


class Strategy(Protocol):
    name: str

    def weights(self, history: PriceHistory) -> Weights:
        ...


def _equal_weight(instrument_ids: Sequence[str]) -> dict[str, float]:
    if not instrument_ids:
        return {}
    weight = 1.0 / len(instrument_ids)
    return {instrument_id: weight for instrument_id in instrument_ids}


@dataclass(frozen=True)
class SmaTrendStrategy:
    fast_window: int = 20
    slow_window: int = 60
    name: str = "sma-trend"

    def __post_init__(self) -> None:
        if self.fast_window <= 0 or self.slow_window <= 0:
            raise ValueError("SMA windows must be positive")
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")

    def weights(self, history: PriceHistory) -> Weights:
        active = []
        for instrument_id, values in sorted(history.items()):
            if len(values) < self.slow_window:
                continue
            fast = _SMA.calculate(values, self.fast_window)
            slow = _SMA.calculate(values, self.slow_window)
            if fast > slow:
                active.append(instrument_id)
        return _equal_weight(active)


@dataclass(frozen=True)
class CrossSectionalMomentumStrategy:
    lookback: int = 60
    top_n: int = 1
    name: str = "cross-sectional-momentum"

    def __post_init__(self) -> None:
        if self.lookback <= 0:
            raise ValueError("lookback must be positive")
        if self.top_n <= 0:
            raise ValueError("top_n must be positive")

    def scores(self, history: PriceHistory) -> dict[str, float]:
        return {key: value for key, prices in history.items()
                if (value := _MOMENTUM.calculate(prices, self.lookback)) is not None}

    def weights(self, history: PriceHistory) -> Weights:
        ranked = sorted(
            (
                (score, instrument_id)
                for instrument_id, score in self.scores(history).items()
                if score > 0
            ),
            key=lambda item: (-item[0], item[1]),
        )
        selected = [instrument_id for _, instrument_id in ranked[: self.top_n]]
        return _equal_weight(selected)


@dataclass(frozen=True)
class MeanReversionZScoreStrategy:
    window: int = 20
    entry_z: float = 1.5
    name: str = "mean-reversion-zscore"

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError("window must be at least 2")
        if self.entry_z <= 0:
            raise ValueError("entry_z must be positive")

    def zscores(self, history: PriceHistory) -> dict[str, float]:
        scores = {}
        for instrument_id, values in history.items():
            if len(values) < self.window:
                continue
            value = _ZSCORE.calculate(values, self.window)
            if value is not None:
                scores[instrument_id] = value
        return scores

    def weights(self, history: PriceHistory) -> Weights:
        active = sorted(
            instrument_id
            for instrument_id, score in self.zscores(history).items()
            if score <= -self.entry_z
        )
        return _equal_weight(active)


def default_strategies() -> list[Strategy]:
    return [
        SmaTrendStrategy(),
        CrossSectionalMomentumStrategy(),
        MeanReversionZScoreStrategy(),
    ]
