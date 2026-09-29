"""Versioned catalogue and computation boundary for point-in-time price factors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from quant_lab.features import FEATURE_VERSION, momentum, sma, zscore


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    version: str
    parameter_name: str
    minimum_history: str
    output_unit: str
    description: str
    evaluator: Callable[[Sequence[float], int], float | None]

    def calculate(self, prices: Sequence[float], parameter: int) -> float | None:
        # The underlying factor enforces its parameter range and returns None
        # when history is too short.  The input is a same-basis close series.
        return self.evaluator(prices, parameter)


_FACTORS = (
    FactorDefinition("sma", FEATURE_VERSION, "window", "window", "price",
                     "过去 window 个收盘价的算术均值", sma),
    FactorDefinition("momentum", FEATURE_VERSION, "lookback", "lookback + 1", "ratio",
                     "收盘价相对 lookback 个交易日前的涨跌幅", momentum),
    FactorDefinition("zscore", FEATURE_VERSION, "window", "window", "standard_deviation",
                     "当前收盘价相对窗口均值的总体标准差倍数", zscore),
)
_INDEX = {item.factor_id: item for item in _FACTORS}


def get_factor(factor_id: str) -> FactorDefinition:
    try:
        return _INDEX[factor_id]
    except KeyError as exc:
        raise ValueError(f"unknown factor: {factor_id}") from exc


def factor_catalog() -> list[dict[str, str]]:
    """Public metadata; factor calculations remain in this library."""
    return [{
        "factor_id": item.factor_id,
        "version": item.version,
        "input_field": "close",
        "input_requirement": "同一标的、同一价格口径、按交易日递增的收盘价",
        "parameter_name": item.parameter_name,
        "minimum_history": item.minimum_history,
        "output_unit": item.output_unit,
        "description": item.description,
    } for item in _FACTORS]
