"""Optional CPU Chronos-Bolt tiny adapter for comparable close-price forecasts.

The heavy dependencies and pinned model weights are loaded only for a live
forecast.  Tests may inject a runner that returns the Chronos-Bolt quantile
tensor layout: [one series, nine quantiles, prediction horizon].
"""
from __future__ import annotations

import re
from importlib import import_module
from typing import Callable, Optional, Sequence

from .forecast import ForecastOutput, ForecastUnavailable, _check_output, _check_request


CHRONOS_BOLT_TINY_MODEL_ID = "amazon/chronos-bolt-tiny"
CHRONOS_BOLT_TINY_CONTEXT_LENGTH = 2048
CHRONOS_BOLT_TINY_PREDICTION_LENGTH = 64


class ChronosBoltTinyProvider:
    """Univariate zero-shot forecast using the native Bolt .1/.5/.9 quantiles.

    ``runner`` accepts the capped close history and horizon.  It permits
    deterministic adapter tests without installing Torch or downloading model
    weights.  A live run requires a full Hugging Face commit SHA so historical
    experiment results do not silently change when ``main`` moves.
    """

    name = "chronos-bolt-tiny"
    model_id = CHRONOS_BOLT_TINY_MODEL_ID

    def __init__(
        self,
        model_revision: Optional[str] = None,
        max_context: int = CHRONOS_BOLT_TINY_CONTEXT_LENGTH,
        runner: Optional[Callable[[Sequence[float], int], object]] = None,
    ) -> None:
        if not isinstance(max_context, int) or not 1 <= max_context <= CHRONOS_BOLT_TINY_CONTEXT_LENGTH:
            raise ValueError("max_context must be in [1, 2048]")
        self.model_revision = model_revision
        self.max_context = max_context
        self._runner = runner
        self._model = None
        self._torch = None

    def _load(self) -> None:
        if self.model_revision is None or not re.fullmatch(r"[0-9a-fA-F]{40}", self.model_revision):
            raise ForecastUnavailable(
                "missing_model_revision",
                "Set model_revision to a 40-character Hugging Face commit SHA.",
            )
        try:
            torch = import_module("torch")
            chronos = import_module("chronos")
        except ModuleNotFoundError as exc:
            raise ForecastUnavailable(
                "missing_optional_dependency",
                f"Install chronos-forecasting and torch in a separate model environment: {exc.name} is missing.",
            ) from exc
        if not hasattr(chronos, "BaseChronosPipeline") or not hasattr(torch, "float32"):
            raise ForecastUnavailable(
                "incompatible_optional_dependency",
                "Installed chronos-forecasting or torch lacks the Chronos-Bolt API.",
            )
        self._model = chronos.BaseChronosPipeline.from_pretrained(
            self.model_id,
            revision=self.model_revision,
            device_map="cpu",
            torch_dtype=torch.float32,
        )
        self._torch = torch

    def forecast(self, history: Sequence[float], horizon: int) -> ForecastOutput:
        _check_request(history, horizon)
        if horizon > CHRONOS_BOLT_TINY_PREDICTION_LENGTH:
            raise ValueError("horizon exceeds Chronos-Bolt tiny's native 64-step prediction length")
        context = tuple(float(value) for value in history[-self.max_context:])
        if self._runner is not None:
            quantiles = self._runner(context, horizon)
        else:
            if self._model is None:
                self._load()
            quantiles = self._model.predict(
                self._torch.tensor(context, dtype=self._torch.float32),
                prediction_length=horizon,
            )
        try:
            if len(quantiles) != 1 or len(quantiles[0]) != 9:
                raise ValueError
            if any(len(row) != horizon for row in quantiles[0]):
                raise ValueError
            output = ForecastOutput(
                values=tuple(float(value) for value in quantiles[0][4]),
                p10=tuple(float(value) for value in quantiles[0][0]),
                p90=tuple(float(value) for value in quantiles[0][8]),
            )
        except (IndexError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Chronos-Bolt returned an unexpected forecast shape") from exc
        return _check_output(output, horizon)
