"""Dev-only type-routed strategy, separate from frozen paper runtimes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from quant_lab.typed_routing import ROUTER_VERSION, classify_prices


@dataclass(frozen=True)
class TypedRouterStrategy:
    """One research strategy that activates a method for each observed type.

    Registration in an online or frozen paper catalog requires a new release;
    importing this module never changes the active strategy catalog.
    """

    router_version: str = ROUTER_VERSION
    name: str = "typed-router"

    def weights(self, history: Mapping[str, Sequence[float]]) -> dict[str, float]:
        if self.router_version != ROUTER_VERSION:
            raise ValueError("router version differs from frozen implementation")
        active = []
        for instrument_id, prices in sorted(history.items()):
            route = classify_prices(prices)
            factors = route["factor_values"]
            if (route["decision_method"] == "sma-trend"
                    and factors["sma_20"] > factors["sma_60"]):
                active.append(instrument_id)
            elif (route["decision_method"] == "mean-reversion-zscore"
                  and factors["zscore_20"] is not None
                  and factors["zscore_20"] <= -1.5):
                active.append(instrument_id)
        return {instrument_id: 1.0 / len(active) for instrument_id in active}
