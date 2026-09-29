"""Broad, stratified forecast pilot on one verified published Qlib release.

Qlib adjusted closes are deliberately kept outside CanonicalClosePanel: their
adjustment convention has not been reconciled with MarketStore qfq prices.
Each stock is evaluated on its own valid-close observations, so short or
ceased intervals do not disappear in an all-stock date intersection.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import secrets
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence
from uuid import uuid4

from quant_lab.forecast import (
    EvaluationConfig, ForecastProvider, Momentum20Provider, RandomWalkProvider,
    evaluate_forecasts,
)
from quant_lab.forecast_benchmark import provider_identity, runtime_metadata
from quant_lab.qlib_archive import (
    QlibArchiveError, _stock_id, parse_calendar, parse_instruments, read_feature,
)
from quant_lab.qlib_local import _load_index, _verified_file


COHORT_VERSION = "qlib-stratified-sha256-v1"
BENCHMARK_VERSION = "qlib-valid-close-walk-forward-v1"
RECORD_SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 64_000_000
MAX_GZIP_BYTES = 16_000_000


class _OriginTracingProvider:
    """Keep the failed request coordinates without retaining model inputs."""

    def __init__(self, provider: ForecastProvider) -> None:
        self.provider = provider
        self.name = provider.name
        self.model_id = provider.model_id
        self.model_revision = provider.model_revision
        self.history_count: int | None = None
        self.horizon: int | None = None

    def forecast(self, history: Sequence[float], horizon: int):
        self.history_count = len(history)
        self.horizon = horizon
        return self.provider.forecast(history, horizon)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _stratum(symbol: str, spans: Sequence[tuple[date, date]],
             target: date) -> str:
    first_year = spans[0][0].year
    era = "pre2010" if first_year < 2010 else "2010s" if first_year < 2020 else "2020s"
    lifecycle = "ended" if spans[-1][1] < target else "active"
    return f"{symbol[:2]}/{era}/{lifecycle}"


def select_stratified_cohort(
    instruments: Mapping[str, Sequence[tuple[date, date]]],
    target: date,
    *,
    per_stratum: int = 3,
) -> tuple[list[str], dict[str, int]]:
    """Choose at most three stable SHA-ranked codes in each nonempty stratum."""
    if isinstance(per_stratum, bool) or not 1 <= per_stratum <= 10:
        raise ValueError("per_stratum must be between 1 and 10")
    grouped: dict[str, list[str]] = defaultdict(list)
    for symbol, spans in instruments.items():
        if symbol[:2] not in ("SH", "SZ", "BJ") or not spans:
            continue
        grouped[_stratum(symbol, spans, target)].append(symbol)
    selected: list[str] = []
    population: dict[str, int] = {}
    for stratum, symbols in sorted(grouped.items()):
        population[stratum] = len(symbols)
        ranked = sorted(symbols, key=lambda symbol: (
            hashlib.sha256((COHORT_VERSION + ":" + symbol).encode("ascii")).hexdigest(),
            symbol,
        ))
        selected.extend(ranked[:per_stratum])
    return sorted(selected), population


def _read_series(
    root: Path, index: dict[str, Any], symbol: str,
    spans: Sequence[tuple[date, date]], calendar: Sequence[date],
) -> tuple[tuple[date, ...], tuple[float, ...], dict[str, Any]]:
    name = f"qlib_bin/features/{symbol.lower()}/close.day.bin"
    if name not in index["files"]:
        return (), (), {"status": "missing_close_feature", "file": name,
                        "file_sha256": None, "calendar_rows": 0,
                        "missing_close_rows": 0, "valid_close_rows": 0}
    with _verified_file(root, index, name) as stream:
        start, values = read_feature(stream.read(), len(calendar), name)
    dates: list[date] = []
    closes: list[float] = []
    eligible = 0
    missing = 0
    for position, day in enumerate(calendar):
        if not any(begin <= day <= finish for begin, finish in spans):
            continue
        eligible += 1
        offset = position - start
        value = values[offset] if 0 <= offset < len(values) else math.nan
        if math.isnan(value):
            missing += 1
            continue
        if not math.isfinite(value) or value <= 0:
            raise QlibArchiveError(f"invalid adjusted close in {symbol}/{day}")
        dates.append(day)
        closes.append(float(value))
    return tuple(dates), tuple(closes), {
        "status": "ready" if closes else "no_valid_close",
        "file": name,
        "file_sha256": index["files"][name]["sha256"],
        "calendar_rows": eligible,
        "missing_close_rows": missing,
        "valid_close_rows": len(closes),
        "first_valid": dates[0].isoformat() if dates else None,
        "last_valid": dates[-1].isoformat() if dates else None,
    }


def _sample_keys(result: Mapping[str, Any]) -> dict[int, set[tuple[str, str]]]:
    samples: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for record in result.get("records", []):
        samples[int(record["horizon_sessions"])].add((
            record["origin_date"], record["target_date"],
        ))
    return samples


def _provider_summary(per_stock: Sequence[Mapping[str, Any]],
                      horizons: Sequence[int], baseline_counts: Mapping[int, int]
                      ) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for horizon in horizons:
        records = [record for item in per_stock for record in item.get("records", [])
                   if record["horizon_sessions"] == horizon]
        eligible = baseline_counts[horizon]
        if not records:
            summary[str(horizon)] = {
                "status": "insufficient_or_unavailable", "count": 0,
                "baseline_eligible_count": eligible,
                "comparison_coverage": 0.0 if eligible else None,
            }
            continue
        model_mae = mean(record["absolute_return_error"] for record in records)
        naive_mae = mean(record["random_walk_absolute_return_error"] for record in records)
        stock_summaries = [item["summary"][str(horizon)] for item in per_stock
                           if item.get("summary", {}).get(str(horizon), {}).get("status") == "ready"]
        equal_stock_mae = mean(item["mae_return"] for item in stock_summaries)
        equal_stock_naive_mae = mean(item["random_walk_mae_return"]
                                     for item in stock_summaries)
        directed = [record["direction_correct"] for record in records
                    if record["direction_correct"] is not None]
        intervals = [record["interval_10_90_covered"] for record in records
                     if record["interval_10_90_covered"] is not None]
        summary[str(horizon)] = {
            "status": "ready",
            "count": len(records),
            "baseline_eligible_count": eligible,
            "comparison_coverage": len(records) / eligible if eligible else None,
            "mae_return": model_mae,
            "random_walk_mae_return_on_same_cases": naive_mae,
            "mae_return_skill_vs_random_walk_on_same_cases": (
                1 - model_mae / naive_mae if naive_mae else None
            ),
            "equal_stock_mae_return": equal_stock_mae,
            "equal_stock_random_walk_mae_return_on_same_cases": equal_stock_naive_mae,
            "equal_stock_skill_vs_random_walk_on_same_cases": (
                1 - equal_stock_mae / equal_stock_naive_mae
                if equal_stock_naive_mae else None
            ),
            "directional_hit_rate": sum(directed) / len(directed) if directed else None,
            "directional_coverage": len(directed) / len(records),
            "interval_10_90_coverage": sum(intervals) / len(intervals) if intervals else None,
            "contributing_stocks": len({item["instrument_id"] for item in per_stock
                                         if any(r["horizon_sessions"] == horizon
                                                for r in item.get("records", []))}),
        }
    return summary


def evaluate_qlib_cohort(
    root: str | Path,
    manifest: str | Path,
    expected_tag: str,
    providers: Sequence[ForecastProvider] | None = None,
    *,
    config: EvaluationConfig = EvaluationConfig(),
    per_stratum: int = 3,
) -> dict[str, Any]:
    """Verify source files and evaluate a deterministic broad stock cohort."""
    root_path = Path(root).resolve()
    index = _load_index(root_path, Path(manifest), expected_tag)
    with _verified_file(root_path, index, "qlib_bin/calendars/day.txt") as stream:
        calendar = parse_calendar(stream.read(), "day calendar")
    with _verified_file(root_path, index, "qlib_bin/instruments/all.txt") as stream:
        instruments = parse_instruments(stream.read(), "all instruments")
    target = date.fromisoformat(index["release"]["target_trade_date"])
    if calendar[-1] != target:
        raise QlibArchiveError("published calendar target differs from release")
    symbols, strata_population = select_stratified_cohort(
        instruments, target, per_stratum=per_stratum,
    )
    if not symbols:
        raise QlibArchiveError("no cohort stocks in published release")
    selected = tuple(providers if providers is not None else
                     (RandomWalkProvider(), Momentum20Provider()))
    baseline = next((p for p in selected if type(p) is RandomWalkProvider), None)
    if baseline is None:
        raise ValueError("built-in random-walk baseline is required")
    if len({p.name for p in selected}) != len(selected):
        raise ValueError("provider names must be unique")
    selected = (baseline,) + tuple(p for p in selected if p is not baseline)
    identities = [provider_identity(p) for p in selected]
    series: dict[str, tuple[tuple[date, ...], tuple[float, ...]]] = {}
    cohort: list[dict[str, Any]] = []
    for symbol in symbols:
        spans = instruments[symbol]
        dates, closes, provenance = _read_series(
            root_path, index, symbol, spans, calendar,
        )
        instrument_id = _stock_id(symbol)
        series[instrument_id] = (dates, closes)
        cohort.append({
            "symbol": symbol, "instrument_id": instrument_id,
            "stratum": _stratum(symbol, spans, target),
            "intervals": [{"start": begin.isoformat(), "end": end.isoformat()}
                          for begin, end in spans],
            **provenance,
        })
    input_fingerprint = hashlib.sha256(_canonical({
        "benchmark_version": BENCHMARK_VERSION,
        "cohort_version": COHORT_VERSION,
        "release": index["release"],
        "calendar_sha256": index["files"]["qlib_bin/calendars/day.txt"]["sha256"],
        "instruments_sha256": index["files"]["qlib_bin/instruments/all.txt"]["sha256"],
        "cohort": cohort,
    })).hexdigest()
    per_provider: list[dict[str, Any]] = []
    baseline_samples: dict[str, dict[int, set[tuple[str, str]]]] = {}
    baseline_counts: dict[int, int] = {h: 0 for h in config.horizons}
    for provider, identity in zip(selected, identities):
        per_stock: list[dict[str, Any]] = []
        failure_counts: dict[str, int] = defaultdict(int)
        for item in cohort:
            instrument_id = item["instrument_id"]
            dates, closes = series[instrument_id]
            if not closes:
                result: dict[str, Any] = {
                    "instrument_id": instrument_id, "status": "insufficient_history",
                    "records": [], "summary": {}, "reason": "no valid close",
                }
            else:
                traced = _OriginTracingProvider(provider)
                try:
                    result = evaluate_forecasts(
                        dates, closes, traced, instrument_id, config=config,
                    )
                except Exception as exc:
                    result = {
                        "instrument_id": instrument_id,
                        "status": "provider_runtime_error", "records": [],
                        "summary": {}, "reason": type(exc).__name__,
                    }
                if result["status"] not in ("ready", "insufficient_history") and traced.history_count:
                    result["first_failure"] = {
                        "origin_date": dates[traced.history_count - 1].isoformat(),
                        "horizon_sessions": traced.horizon,
                        "history_count": traced.history_count,
                    }
            if result["status"] not in ("ready", "insufficient_history"):
                failure_counts[result["status"]] += 1
                result["records"] = []
                result["summary"] = {}
            if provider is baseline:
                if result["status"] not in ("ready", "insufficient_history"):
                    raise ValueError("built-in random-walk baseline failed")
                samples = _sample_keys(result)
                baseline_samples[instrument_id] = samples
                for horizon in config.horizons:
                    baseline_counts[horizon] += len(samples.get(horizon, set()))
            elif result["status"] == "ready":
                if _sample_keys(result) != baseline_samples[instrument_id]:
                    raise ValueError(f"model sample origins differ from baseline: {instrument_id}")
            per_stock.append(result)
        successful = sum(result["status"] == "ready" for result in per_stock)
        per_provider.append({
            **identity,
            "status": ("ready" if successful and not failure_counts else
                       "partial_failure" if successful else
                       "unavailable" if failure_counts else "insufficient_history"),
            "stocks_with_samples": successful,
            "failure_counts": dict(sorted(failure_counts.items())),
            "pretraining_overlap_status": (
                "not_applicable" if isinstance(provider, (RandomWalkProvider, Momentum20Provider))
                else "unknown"
            ),
            "per_stock": per_stock,
            "pooled": _provider_summary(per_stock, config.horizons, baseline_counts),
        })
    config_meta = {"horizons": list(config.horizons), "min_context": config.min_context,
                   "step": config.step}
    runtime = runtime_metadata(selected)
    harness_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    experiment_key = hashlib.sha256(_canonical({
        "input_fingerprint_sha256": input_fingerprint,
        "configuration": config_meta,
        "providers": identities,
        "runtime": runtime,
        "harness_source_sha256": harness_digest,
    })).hexdigest()
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "experiment_key_sha256": experiment_key,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ready" if any(p["status"] == "ready" for p in per_provider) else
                  "partial_failure" if any(p["status"] == "partial_failure" for p in per_provider)
                  else "insufficient_history",
        "source_id": index["source_id"],
        "release": index["release"],
        "input_fingerprint_sha256": input_fingerprint,
        "price_basis": "qlib_adjusted",
        "price_unit": "Qlib adjusted close feature",
        "horizon_unit": "valid_stock_close_observations",
        "calendar_start": calendar[0].isoformat(),
        "calendar_end": calendar[-1].isoformat(),
        "cohort_selection": {"version": COHORT_VERSION,
                             "per_stratum": per_stratum,
                             "strata_population": strata_population,
                             "selected_count": len(cohort)},
        "cohort": cohort,
        "configuration": config_meta,
        "runtime": runtime,
        "harness_source_sha256": harness_digest,
        "providers": per_provider,
        "research_only": True,
        "point_in_time_validated": False,
        "prospective_out_of_sample": False,
        "foundation_model_pretraining_overlap": "unknown_for_external_models",
        "limitations": [
            "Qlib 调整价未与主库前复权口径合并；该 Release 只能用于独立回顾性预测误差研究。",
            "股票池与历史价格来自 2026-09-28 固定 Release，并非各历史起点已知版本。",
            "每股独立取有效收盘价，停牌空值不计作预测 horizon；不同股票起点日期不完全相同。",
            "结束区间不等于已核实退市；按发布版本的首次区间与结束状态分层可能包含事后信息。",
            "收益率预测误差不等于计费交易收益；模型不得直接进入推荐或自动交易。",
        ],
    }


def append_qlib_benchmark_record(directory: str | Path, result: Mapping[str, Any]
                                 ) -> dict[str, str]:
    """Append a compressed full record, bounded and SHA-checked on readback."""
    if result.get("benchmark_version") != BENCHMARK_VERSION:
        raise ValueError("unknown Qlib benchmark version")
    payload = dict(result)
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    record_id = uuid4().hex
    record = {"schema_version": RECORD_SCHEMA_VERSION, "record_id": record_id,
              "created_at": created_at, "payload_sha256": digest, "payload": payload}
    plain = _canonical(record)
    if len(plain) > MAX_RECORD_BYTES:
        raise ValueError("Qlib benchmark record exceeds uncompressed size limit")
    compressed = gzip.compress(plain, compresslevel=6, mtime=0)
    if len(compressed) > MAX_GZIP_BYTES:
        raise ValueError("Qlib benchmark record exceeds compressed size limit")
    folder = Path(directory) / created_at[:10]
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    stamp = created_at.replace("-", "").replace(":", "").replace(".", "").replace("+00:00", "Z")
    path = folder / f"{stamp}-{record_id}.json.gz"
    temporary = folder / f".{record_id}.{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(compressed)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"record_id": record_id, "created_at": created_at,
            "payload_sha256": digest, "path": str(path.resolve())}


def read_qlib_benchmark_record(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    if file.stat().st_size > MAX_GZIP_BYTES:
        raise ValueError("Qlib benchmark record exceeds compressed size limit")
    with gzip.open(file, "rb") as stream:
        plain = stream.read(MAX_RECORD_BYTES + 1)
    if len(plain) > MAX_RECORD_BYTES:
        raise ValueError("Qlib benchmark record exceeds uncompressed size limit")
    record = json.loads(plain)
    if (not isinstance(record, dict) or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(record.get("payload"), dict)
            or record["payload"].get("benchmark_version") != BENCHMARK_VERSION
            or hashlib.sha256(_canonical(record["payload"])).hexdigest()
            != record.get("payload_sha256")):
        raise ValueError("Qlib benchmark record failed integrity check")
    return record
