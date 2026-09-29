"""Chronology-limited rolling-origin forecasts for research, not trade signals.

The dependency-free random-walk baseline runs in the main environment.  The
TimesFM 2.5 adapter loads its optional package and weights only when called.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from importlib import import_module, metadata
from typing import Callable, Optional, Protocol, Sequence, Tuple


TIMESFM_PACKAGE_VERSION = "2.0.2"
TIMESFM_MODEL_ID = "google/timesfm-2.5-200m-pytorch"


class ForecastUnavailable(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class ForecastOutput:
    values: Tuple[float, ...]
    p10: Optional[Tuple[float, ...]] = None
    p90: Optional[Tuple[float, ...]] = None


class ForecastProvider(Protocol):
    name: str
    model_id: str
    model_revision: Optional[str]

    def forecast(self, history: Sequence[float], horizon: int) -> ForecastOutput:
        """Use only the history visible at the forecast origin."""


def _check_request(history: Sequence[float], horizon: int) -> None:
    if not isinstance(horizon, int) or horizon < 1:
        raise ValueError("horizon must be a positive integer")
    if not history or any(not math.isfinite(float(v)) or v <= 0 for v in history):
        raise ValueError("history must contain finite positive prices")


def _check_output(output: ForecastOutput, horizon: int) -> ForecastOutput:
    if len(output.values) != horizon or any(
        not math.isfinite(float(v)) or v <= 0 for v in output.values
    ):
        raise ValueError("forecast must contain one finite positive price per step")
    if (output.p10 is None) != (output.p90 is None):
        raise ValueError("forecast intervals require both p10 and p90")
    if output.p10 is not None and output.p90 is not None:
        if len(output.p10) != horizon or len(output.p90) != horizon:
            raise ValueError("forecast interval length must match horizon")
        for low, point, high in zip(output.p10, output.values, output.p90):
            if not all(math.isfinite(float(v)) for v in (low, point, high)):
                raise ValueError("forecast interval must be finite")
            if low > point or point > high:
                raise ValueError("forecast quantiles must be ordered")
    return output


class RandomWalkProvider:
    name = "random-walk"
    model_id = "last-observed-close"
    model_revision = "1"

    def forecast(self, history: Sequence[float], horizon: int) -> ForecastOutput:
        _check_request(history, horizon)
        return ForecastOutput(tuple(float(history[-1]) for _ in range(horizon)))


class Momentum20Provider:
    """Simple constant-drift extrapolation of the last 20-session return."""

    name = "momentum-20-extrapolation"
    model_id = "last-20-session-geometric-drift"
    model_revision = "1"

    def forecast(self, history: Sequence[float], horizon: int) -> ForecastOutput:
        _check_request(history, horizon)
        if len(history) < 21:
            raise ForecastUnavailable("insufficient_context", "Momentum20 requires 21 closes.")
        last = float(history[-1])
        daily_growth = (last / float(history[-21])) ** (1.0 / 20.0)
        return _check_output(ForecastOutput(tuple(
            last * daily_growth ** step for step in range(1, horizon + 1)
        )), horizon)


class TimesFM25Provider:
    """Optional zero-shot TimesFM 2.5 adapter, pinned to its 2.0.2 package API.

    ``runner`` is an injectable callable with the same output pair as the
    official ``model.forecast`` API: arrays shaped (1, horizon) and
    (1, horizon, 10).  It permits tests without model downloads.
    """

    name = "timesfm-2.5"
    model_id = TIMESFM_MODEL_ID

    def __init__(
        self,
        model_revision: Optional[str] = None,
        max_context: int = 512,
        max_horizon: int = 128,
        runner: Optional[Callable[[Sequence[float], int], Tuple[object, object]]] = None,
    ) -> None:
        if max_context < 32 or max_context > 16384:
            raise ValueError("max_context must be in [32, 16384]")
        if max_horizon < 1 or max_horizon > 1024:
            raise ValueError("max_horizon must be in [1, 1024]")
        compiled_context = math.ceil(max_context / 32) * 32
        compiled_horizon = math.ceil(max_horizon / 128) * 128
        if compiled_context + compiled_horizon > 16384:
            raise ValueError("rounded context plus horizon exceeds TimesFM 2.5 limit")
        self.model_revision = model_revision
        self.max_context = max_context
        self.max_horizon = max_horizon
        self._runner = runner
        self._model = None

    def _load(self) -> None:
        try:
            installed = metadata.version("timesfm")
        except metadata.PackageNotFoundError as exc:
            raise ForecastUnavailable(
                "missing_optional_dependency",
                "Install timesfm[torch]==2.0.2 in a separate Python >=3.10 environment.",
            ) from exc
        if installed != TIMESFM_PACKAGE_VERSION:
            raise ForecastUnavailable(
                "incompatible_optional_dependency",
                f"TimesFM 2.5 adapter requires timesfm==2.0.2; found {installed}.",
            )
        if self.model_revision is None or not re.fullmatch(r"[0-9a-fA-F]{40}", self.model_revision):
            raise ForecastUnavailable(
                "missing_model_revision",
                "Set model_revision to a 40-character Hugging Face commit SHA.",
            )
        try:
            timesfm = import_module("timesfm")
            numpy = import_module("numpy")
        except ModuleNotFoundError as exc:
            raise ForecastUnavailable(
                "missing_optional_dependency", f"TimesFM dependency is missing: {exc.name}"
            ) from exc
        if not hasattr(timesfm, "TimesFM_2p5_200M_torch") or not hasattr(timesfm, "ForecastConfig"):
            raise ForecastUnavailable(
                "incompatible_optional_dependency", "Installed timesfm lacks the 2.5 API."
            )
        model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
            TIMESFM_MODEL_ID, revision=self.model_revision, torch_compile=False
        )
        model.compile(timesfm.ForecastConfig(
            max_context=self.max_context,
            max_horizon=self.max_horizon,
            per_core_batch_size=4,
            normalize_inputs=True,
            use_continuous_quantile_head=True,
            infer_is_positive=True,
            fix_quantile_crossing=True,
        ))
        self._model = model
        self._numpy = numpy

    def forecast(self, history: Sequence[float], horizon: int) -> ForecastOutput:
        _check_request(history, horizon)
        if horizon > self.max_horizon:
            raise ValueError("horizon exceeds configured max_horizon")
        context = tuple(float(v) for v in history[-self.max_context:])
        if self._runner is not None:
            point, quantiles = self._runner(context, horizon)
        else:
            if self._model is None:
                self._load()
            point, quantiles = self._model.forecast(
                horizon=horizon,
                inputs=[self._numpy.asarray(context, dtype=self._numpy.float32)],
            )
        try:
            values = tuple(float(value) for value in point[0])
            p10 = tuple(float(row[1]) for row in quantiles[0])
            p90 = tuple(float(row[9]) for row in quantiles[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError("TimesFM returned an unexpected forecast shape") from exc
        return _check_output(ForecastOutput(values, p10, p90), horizon)


@dataclass(frozen=True)
class EvaluationConfig:
    horizons: Tuple[int, ...] = (5, 20)
    min_context: int = 120
    step: int = 20

    def __post_init__(self) -> None:
        if not self.horizons or any(not isinstance(h, int) or h < 1 for h in self.horizons):
            raise ValueError("horizons must be positive integers")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("horizons must be unique")
        if self.min_context < 2 or self.step < 1:
            raise ValueError("min_context must be >=2 and step >=1")


def _sign(value: float) -> int:
    if value > 1e-12:
        return 1
    if value < -1e-12:
        return -1
    return 0


def evaluate_forecasts(
    dates: Sequence[date],
    closes: Sequence[float],
    provider: ForecastProvider,
    instrument_id: str,
    asof: Optional[date] = None,
    config: EvaluationConfig = EvaluationConfig(),
) -> dict[str, object]:
    """Evaluate spaced forecast origins using only observations <= each origin.

    The target is the close ``h`` *trading observations* after the origin.  MAE
    skill is an error comparison to the random-walk baseline, not strategy P&L.
    """
    if len(dates) != len(closes):
        raise ValueError("dates and closes must have equal lengths")
    if any(left >= right for left, right in zip(dates, dates[1:])):
        raise ValueError("dates must be strictly increasing")
    _check_request(closes, 1)
    end = len(dates) if asof is None else next(
        (i for i, day in enumerate(dates) if day > asof), len(dates)
    )
    visible_dates = dates[:end]
    visible_closes = closes[:end]
    records: list[dict[str, object]] = []
    result: dict[str, object] = {
        "status": "ready",
        "provider": provider.name,
        "model_id": provider.model_id,
        "model_revision": provider.model_revision,
        "instrument_id": instrument_id,
        "asof": visible_dates[-1].isoformat() if visible_dates else None,
        "min_context": config.min_context,
        "step": config.step,
        "horizons": list(config.horizons),
        "records": records,
        "summary": {},
        "research_only": True,
    }
    for horizon in config.horizons:
        for origin in range(config.min_context - 1, len(visible_dates) - horizon, config.step):
            history = tuple(visible_closes[:origin + 1])
            try:
                output = _check_output(provider.forecast(history, horizon), horizon)
            except ForecastUnavailable as exc:
                result.update(status=exc.code, reason=str(exc), records=[], summary={})
                return result
            origin_price = float(history[-1])
            target_price = float(visible_closes[origin + horizon])
            forecast_price = output.values[horizon - 1]
            predicted_return = forecast_price / origin_price - 1.0
            actual_return = target_price / origin_price - 1.0
            direction = _sign(predicted_return)
            covered = None
            if output.p10 is not None and output.p90 is not None:
                covered = output.p10[horizon - 1] <= target_price <= output.p90[horizon - 1]
            records.append({
                "origin_date": visible_dates[origin].isoformat(),
                "target_date": visible_dates[origin + horizon].isoformat(),
                "horizon_sessions": horizon,
                "context_count": len(history),
                "origin_close": origin_price,
                "forecast_close": forecast_price,
                "actual_close": target_price,
                "random_walk_close": origin_price,
                "absolute_error": abs(forecast_price - target_price),
                "random_walk_absolute_error": abs(origin_price - target_price),
                "predicted_return": predicted_return,
                "actual_return": actual_return,
                "absolute_return_error": abs(predicted_return - actual_return),
                "random_walk_absolute_return_error": abs(actual_return),
                "direction_correct": None if direction == 0 else direction == _sign(actual_return),
                "interval_10_90_covered": covered,
            })
    for horizon in config.horizons:
        items = [item for item in records if item["horizon_sessions"] == horizon]
        n = len(items)
        if not n:
            result["summary"][str(horizon)] = {"status": "insufficient_history", "count": 0}
            continue
        mae = sum(item["absolute_error"] for item in items) / n
        naive_mae = sum(item["random_walk_absolute_error"] for item in items) / n
        return_mae = sum(item["absolute_return_error"] for item in items) / n
        naive_return_mae = sum(item["random_walk_absolute_return_error"] for item in items) / n
        directed = [item["direction_correct"] for item in items if item["direction_correct"] is not None]
        intervals = [item["interval_10_90_covered"] for item in items if item["interval_10_90_covered"] is not None]
        result["summary"][str(horizon)] = {
            "status": "ready", "count": n,
            "mae_close": mae,
            "random_walk_mae_close": naive_mae,
            "mae_skill_vs_random_walk": 1.0 - mae / naive_mae if naive_mae else None,
            "mae_return": return_mae,
            "random_walk_mae_return": naive_return_mae,
            "mae_return_skill_vs_random_walk": (
                1.0 - return_mae / naive_return_mae if naive_return_mae else None
            ),
            "directional_hit_rate": sum(directed) / len(directed) if directed else None,
            "directional_coverage": len(directed) / n,
            "interval_10_90_coverage": sum(intervals) / len(intervals) if intervals else None,
        }
    if not records:
        result["status"] = "insufficient_history"
    return result
