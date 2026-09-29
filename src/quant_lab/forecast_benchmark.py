"""Comparable, archived walk-forward forecast experiments on canonical closes.

This module evaluates forecasts, not trading strategies.  All providers see
the same common-date panel and each forecast call receives only observations
through its origin.  The current canonical sources do not establish a true
historical point-in-time snapshot, so archived results remain retrospective.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import platform
import re
import secrets
import sqlite3
import sys
from datetime import date, datetime, timezone
from importlib import metadata
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence
from uuid import uuid4

from quant_lab.canonical_data import (
    CONTRACT_VERSION, CanonicalClosePanel, MarketStoreAdapter, build_close_panel,
)
from quant_lab.forecast import (
    EvaluationConfig, ForecastProvider, Momentum20Provider, RandomWalkProvider,
    evaluate_forecasts,
)
from quant_lab.storage import MarketStore


BENCHMARK_VERSION = "canonical-walk-forward-v1"
RECORD_SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 8_000_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def runtime_metadata(providers: Sequence[ForecastProvider]) -> dict[str, Any]:
    """Capture lightweight runtime identity without loading model packages."""
    packages: dict[str, str | None] = {}
    for name in ("timesfm", "chronos-forecasting", "torch", "uni2ts",
                 "granite-tsfm", "transformers"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    implementations: list[dict[str, str | None]] = []
    for provider in providers:
        kind = type(provider)
        try:
            source = inspect.getsourcefile(kind)
        except (TypeError, OSError):
            source = None
        digest = None
        if source and Path(source).is_file():
            digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        implementations.append({
            "provider": provider.name,
            "class": kind.__module__ + "." + kind.__qualname__,
            "module_source_sha256": digest,
        })
    return {
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "platform": platform.system() + "/" + platform.machine(),
        "optional_distributions": packages,
        "provider_implementations": implementations,
    }


def provider_identity(provider: ForecastProvider) -> dict[str, object]:
    name = getattr(provider, "name", None)
    model_id = getattr(provider, "model_id", None)
    revision = getattr(provider, "model_revision", None)
    if not isinstance(name, str) or not name or not isinstance(model_id, str) or not model_id:
        raise ValueError("provider requires nonempty name and model_id")
    if revision is not None and (not isinstance(revision, str) or not revision):
        raise ValueError("provider revision must be a nonempty string or None")
    configuration = {}
    for key in ("max_context", "max_horizon"):
        value = getattr(provider, key, None)
        if type(value) is int:
            configuration[key] = value
    return {
        "name": name, "model_id": model_id, "model_revision": revision,
        "provider_configuration": configuration,
        "execution_mode": ("injected_runner" if getattr(provider, "_runner", None) is not None
                           else "standard"),
    }


def _validate_panel(panel: CanonicalClosePanel) -> None:
    if panel.price_basis != "forward_adjusted" or panel.price_unit != "CNY/share":
        raise ValueError("benchmark requires same-basis adjusted stock closes")
    if not _SHA256.fullmatch(panel.input_fingerprint_sha256):
        raise ValueError("canonical panel requires a SHA-256 input fingerprint")
    if not panel.dates or any(a >= b for a, b in zip(panel.dates, panel.dates[1:])):
        raise ValueError("panel dates must be nonempty and strictly increasing")
    if not panel.closes:
        raise ValueError("panel needs at least one stock")
    for instrument_id, closes in panel.closes.items():
        if not instrument_id.startswith("stock:") or len(closes) != len(panel.dates):
            raise ValueError("panel requires aligned stock close series")
        if any(isinstance(v, bool) or not math.isfinite(float(v)) or float(v) <= 0
               for v in closes):
            raise ValueError("panel contains invalid stock closes")


def _pooled(items: Sequence[Mapping[str, Any]], horizons: Sequence[int]
            ) -> dict[str, dict[str, Any]]:
    pooled: dict[str, dict[str, Any]] = {}
    for horizon in horizons:
        records = [r for item in items for r in item["records"]
                   if r["horizon_sessions"] == horizon]
        if not records:
            pooled[str(horizon)] = {"status": "insufficient_history", "count": 0}
            continue
        absolute_return_error = mean(r["absolute_return_error"] for r in records)
        random_walk_error = mean(r["random_walk_absolute_return_error"] for r in records)
        directed = [r["direction_correct"] for r in records
                    if r["direction_correct"] is not None]
        intervals = [r["interval_10_90_covered"] for r in records
                     if r["interval_10_90_covered"] is not None]
        pooled[str(horizon)] = {
            "status": "ready",
            "count": len(records),
            "mae_close": mean(r["absolute_error"] for r in records),
            "mae_return": absolute_return_error,
            "random_walk_mae_return": random_walk_error,
            "mae_return_skill_vs_random_walk": (
                1 - absolute_return_error / random_walk_error if random_walk_error else None
            ),
            "directional_hit_rate": sum(directed) / len(directed) if directed else None,
            "directional_coverage": len(directed) / len(records),
            "interval_10_90_coverage": sum(intervals) / len(intervals) if intervals else None,
        }
    return pooled


def evaluate_panel_benchmark(
    panel: CanonicalClosePanel,
    providers: Sequence[ForecastProvider] | None = None,
    *,
    config: EvaluationConfig = EvaluationConfig(),
) -> dict[str, Any]:
    """Evaluate comparable forecast origins, retaining every observable error.

    Only providers explicitly passed are executed.  The default consists of
    dependency-free random-walk and momentum baselines.  A random walk is
    required because all skill metrics compare to that reference.
    """
    _validate_panel(panel)
    selected = tuple(providers if providers is not None else
                     (RandomWalkProvider(), Momentum20Provider()))
    if not selected:
        raise ValueError("at least one provider is required")
    identities = [provider_identity(provider) for provider in selected]
    names = [identity["name"] for identity in identities]
    if len(set(names)) != len(names):
        raise ValueError("provider names must be unique within an experiment")
    if "random-walk" not in names:
        raise ValueError("random-walk is required as a comparable baseline")
    # A name alone is not enough to trust the baseline semantics.
    baseline = selected[names.index("random-walk")]
    if (baseline.model_id != RandomWalkProvider.model_id
            or baseline.model_revision != RandomWalkProvider.model_revision
            or type(baseline) is not RandomWalkProvider):
        raise ValueError("random-walk must be the built-in baseline")
    selected = (baseline,) + tuple(provider for provider in selected if provider is not baseline)
    identities = [provider_identity(provider) for provider in selected]
    instrument_ids = sorted(panel.closes)
    config_meta = {"horizons": list(config.horizons), "min_context": config.min_context,
                   "step": config.step}
    runtime = runtime_metadata(selected)
    harness_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    experiment_key = hashlib.sha256(_canonical({
        "benchmark_version": BENCHMARK_VERSION,
        "canonical_contract_version": CONTRACT_VERSION,
        "input_fingerprint_sha256": panel.input_fingerprint_sha256,
        "universe": instrument_ids,
        "dates": [panel.dates[0].isoformat(), panel.dates[-1].isoformat()],
        "configuration": config_meta,
        "providers": identities,
        "runtime": runtime,
        "harness_source_sha256": harness_digest,
    })).hexdigest()
    results: list[dict[str, Any]] = []
    baseline_samples: dict[int, set[tuple[str, str, str]]] = {}
    for provider, identity in zip(selected, identities):
        per_stock: list[dict[str, Any]] = []
        runtime_error: str | None = None
        for instrument_id in instrument_ids:
            try:
                item = evaluate_forecasts(
                    panel.dates, panel.closes[instrument_id], provider, instrument_id,
                    config=config,
                )
            except Exception as exc:
                # Isolate an optional model's runtime failure from the other
                # providers.  The exception type is enough for diagnostics;
                # avoid saving arbitrary model messages in the record pool.
                runtime_error = type(exc).__name__
                break
            per_stock.append(item)
            if item["status"] not in ("ready", "insufficient_history"):
                break
        unavailable = next((item for item in per_stock if item["status"] not in
                            ("ready", "insufficient_history")), None)
        if runtime_error:
            status, reason = "provider_runtime_error", runtime_error
        elif unavailable:
            status = unavailable["status"]
            reason = unavailable.get("reason")
        elif any(item["status"] == "ready" for item in per_stock):
            status, reason = "ready", None
        else:
            status, reason = "insufficient_history", None
        if status not in ("ready", "insufficient_history"):
            # Partial output is not comparable and must not contribute metrics.
            per_stock = []
        samples = {
            horizon: {(item["instrument_id"], record["origin_date"],
                       record["target_date"])
                      for item in per_stock for record in item["records"]
                      if record["horizon_sessions"] == horizon}
            for horizon in config.horizons
        }
        if provider is baseline:
            baseline_samples = samples
        results.append({
            **identity,
            "status": status,
            "reason": reason,
            "pooled": _pooled(per_stock, config.horizons),
            "per_stock": per_stock,
            "sample_alignment": "same_as_random_walk" if status == "ready" else "not_evaluated",
            "pretraining_overlap_status": (
                "not_applicable" if isinstance(provider, (RandomWalkProvider, Momentum20Provider))
                else "unknown"
            ),
        })
        if provider is baseline and status not in ("ready", "insufficient_history"):
            raise ValueError("built-in random-walk baseline failed")
    # The baseline may be listed after another provider, so compare again to
    # avoid an order-dependent alignment assertion.
    if baseline_samples:
        for result in results:
            if result["status"] != "ready":
                continue
            for horizon in config.horizons:
                sample = {(item["instrument_id"], record["origin_date"],
                           record["target_date"])
                          for item in result["per_stock"] for record in item["records"]
                          if record["horizon_sessions"] == horizon}
                if sample != baseline_samples[horizon]:
                    raise ValueError("providers produced different forecast sample origins")
    for result in results:
        result["comparison_coverage"] = {
            str(horizon): {
                "eligible_count": len(baseline_samples[horizon]),
                "evaluated_count": result["pooled"][str(horizon)]["count"],
                "fraction": (result["pooled"][str(horizon)]["count"] /
                             len(baseline_samples[horizon]) if baseline_samples[horizon] else None),
            }
            for horizon in config.horizons
        }
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "experiment_key_sha256": experiment_key,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ready" if any(r["status"] == "ready" for r in results) else
                  "insufficient_history",
        "canonical_contract_version": CONTRACT_VERSION,
        "input_fingerprint_sha256": panel.input_fingerprint_sha256,
        "price_basis": panel.price_basis,
        "price_unit": panel.price_unit,
        "universe": instrument_ids,
        "data_start": panel.dates[0].isoformat(),
        "data_asof": panel.dates[-1].isoformat(),
        "common_sessions": len(panel.dates),
        "configuration": config_meta,
        "runtime": runtime,
        "harness_source_sha256": harness_digest,
        "providers": results,
        "research_only": True,
        "point_in_time_validated": False,
        "prospective_out_of_sample": False,
        "foundation_model_pretraining_overlap": "unknown_for_external_models",
        "limitations": [
            "当前历史复权价与股票池未冻结为当时可见的版本；滚动起点仅限制模型输入，不能证明完整时点正确性。",
            "预测误差和方向命中率不是扣成本的交易收益或风险控制结果。",
        ],
    }


def evaluate_focus_benchmark(
    database: MarketStore | str | Path,
    providers: Sequence[ForecastProvider] | None = None,
    *,
    asof: date | None = None,
    config: EvaluationConfig = EvaluationConfig(),
) -> dict[str, Any]:
    """Read the registered focus stock universe and normalized qfq bars."""
    path = database.path if isinstance(database, MarketStore) else Path(database)
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        instrument_ids = [row[0] for row in connection.execute(
            "SELECT instrument_id FROM instruments WHERE family='focus_stock' "
            "AND asset_type='stock' ORDER BY instrument_id"
        )]
    finally:
        connection.close()
    if not instrument_ids:
        raise ValueError("no registered focus stocks")
    panel = build_close_panel(
        (bar for bar in MarketStoreAdapter(path).iter_bars(
            instrument_ids=instrument_ids, end=asof,
        ) if bar.price_basis == "forward_adjusted"),
        expected_price_basis="forward_adjusted",
    )
    if len(panel.closes) != len(instrument_ids):
        raise ValueError("normalized panel is missing a focus stock")
    result = evaluate_panel_benchmark(panel, providers, config=config)
    result["universe_scope"] = "registered focus stocks only; retrospective selected sample"
    return result


def append_benchmark_record(directory: str | Path, result: Mapping[str, Any]
                           ) -> dict[str, str]:
    """Save a private, immutable, SHA-checked full experiment record."""
    if result.get("benchmark_version") != BENCHMARK_VERSION:
        raise ValueError("unknown benchmark version")
    payload = dict(result)
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    record_id = uuid4().hex
    record = {"schema_version": RECORD_SCHEMA_VERSION, "record_id": record_id,
              "created_at": created_at, "payload_sha256": digest, "payload": payload}
    body = json.dumps(record, sort_keys=True, ensure_ascii=False, indent=2,
                      allow_nan=False).encode("utf-8") + b"\n"
    if len(body) > MAX_RECORD_BYTES:
        raise ValueError("benchmark record exceeds size limit")
    folder = Path(directory) / created_at[:10]
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    stamp = created_at.replace("-", "").replace(":", "").replace(".", "").replace("+00:00", "Z")
    path = folder / f"{stamp}-{record_id}.json"
    temporary = folder / f".{record_id}.{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"record_id": record_id, "created_at": created_at,
            "payload_sha256": digest, "path": str(path.resolve())}


def read_benchmark_record(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("benchmark record exceeds size limit")
    record = json.loads(raw)
    if (not isinstance(record, dict) or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(record.get("payload"), dict)
            or record["payload"].get("benchmark_version") != BENCHMARK_VERSION
            or hashlib.sha256(_canonical(record["payload"])).hexdigest()
            != record.get("payload_sha256")):
        raise ValueError("benchmark record failed integrity check")
    return record
