"""The strategy library registers implementations, versions, and factor inputs.

Registration means a strategy can be calculated.  Validation state belongs to
actual archived research and future independent observations, never this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from quant_lab.strategies import (CrossSectionalMomentumStrategy,
                                  MeanReversionZScoreStrategy, SmaTrendStrategy,
                                  Strategy)


STRATEGY_VERSION = "1"


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    version: str
    factor_ids: tuple[str, ...]
    description: str
    factory: Callable[[], Strategy]

    def build(self) -> Strategy:
        return self.factory()

    def parameters(self) -> dict[str, int | float]:
        return {name: value for name, value in vars(self.build()).items() if name != "name"}


_STRATEGIES = (
    StrategyDefinition("sma-trend", STRATEGY_VERSION, ("sma",),
                       "20/60 日均线趋势；趋势满足时在有效股票中等权", SmaTrendStrategy),
    StrategyDefinition("cross-sectional-momentum", STRATEGY_VERSION, ("momentum",),
                       "60 日横截面动量；正动量股票中取最高的一只", CrossSectionalMomentumStrategy),
    StrategyDefinition("mean-reversion-zscore", STRATEGY_VERSION, ("zscore",),
                       "20 日 Z 分数低于负阈值时在触发股票中等权", MeanReversionZScoreStrategy),
)
_INDEX = {item.strategy_id: item for item in _STRATEGIES}


def get_strategy_definition(strategy_id: str) -> StrategyDefinition:
    try:
        return _INDEX[strategy_id]
    except KeyError as exc:
        raise ValueError(f"unknown strategy: {strategy_id}") from exc


def strategy_catalog() -> list[dict[str, object]]:
    return [{
        "strategy_id": item.strategy_id,
        "strategy_version": item.version,
        "factor_ids": list(item.factor_ids),
        "parameters": item.parameters(),
        "description": item.description,
        "implementation_status": "implemented",
    } for item in _STRATEGIES]
