"""Retrospective group price/volume behaviour pilot on one verified Qlib release.

The target is a fixed-cohort, equal-weight daily-return proxy, not investor flow
or an executable portfolio.  The 2026 release and cohort membership are known
in hindsight.  Every model fit uses only labels completed by its origin close.
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import json
import math
import os
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence
from uuid import uuid4

from quant_lab.event_study import MacroEvent, load_event_registry
from quant_lab.forecast_qlib import COHORT_VERSION, select_stratified_cohort
from quant_lab.qlib_archive import QlibArchiveError, parse_calendar, parse_instruments, read_feature
from quant_lab.qlib_local import _load_index, _verified_file


PILOT_VERSION = "qlib-group-behavior-ridge-v2"
FEATURES = ("group_return_1", "group_return_5", "breadth_1",
            "signed_log_volume_change_1", "log_volume_change_1", "participation")
RECORD_SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 12_000_000
MAX_GZIP_BYTES = 4_000_000


@dataclass(frozen=True)
class PilotConfig:
    start: date = date(2021, 1, 4)
    horizons: tuple[int, ...] = (5, 20)
    step: int = 20
    min_stocks: int = 20
    min_train: int = 120
    train_window: int = 504
    ridge_alpha: float = 30.0
    volume_ratio_clip: float = 4.0
    prediction_clip: float = 0.30

    def __post_init__(self) -> None:
        if (not self.horizons or len(set(self.horizons)) != len(self.horizons)
                or any(type(h) is not int or h < 1 for h in self.horizons)):
            raise ValueError("horizons must be unique positive integers")
        if (type(self.step) is not int or self.step < 1
                or type(self.min_stocks) is not int or self.min_stocks < 2
                or type(self.min_train) is not int or self.min_train < 10
                or type(self.train_window) is not int or self.train_window < self.min_train):
            raise ValueError("invalid pilot sample configuration")
        if (not math.isfinite(self.ridge_alpha) or self.ridge_alpha <= 0
                or not math.isfinite(self.volume_ratio_clip) or self.volume_ratio_clip <= 1
                or not math.isfinite(self.prediction_clip) or not 0 < self.prediction_clip < 1):
            raise ValueError("invalid pilot numeric configuration")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _compound(returns: Sequence[float]) -> float:
    value = 1.0
    for item in returns:
        value *= 1.0 + item
    return value - 1.0


def _read_verified_field(root: Path, index: Mapping[str, Any], symbol: str,
                         field: str, calendar_size: int) -> tuple[int, tuple[float, ...]]:
    name = f"qlib_bin/features/{symbol.lower()}/{field}.day.bin"
    with _verified_file(root, index, name) as stream:
        return read_feature(stream.read(), calendar_size, name)


def load_group_bars(root: str | Path, manifest: str | Path, expected_tag: str,
                    *, per_stratum: int = 3
                    ) -> tuple[tuple[date, ...], list[dict[str, tuple[float, float]]], dict[str, Any]]:
    """Read and hash-check adjusted close and volume for the fixed cohort."""
    root_path = Path(root).resolve()
    index = _load_index(root_path, Path(manifest), expected_tag)
    with _verified_file(root_path, index, "qlib_bin/calendars/day.txt") as stream:
        calendar = parse_calendar(stream.read(), "day calendar")
    with _verified_file(root_path, index, "qlib_bin/instruments/all.txt") as stream:
        instruments = parse_instruments(stream.read(), "all instruments")
    target = date.fromisoformat(index["release"]["target_trade_date"])
    if calendar[-1] != target:
        raise QlibArchiveError("release target and calendar disagree")
    symbols, population = select_stratified_cohort(instruments, target,
                                                    per_stratum=per_stratum)
    if not symbols:
        raise QlibArchiveError("fixed cohort is empty")
    daily: list[dict[str, tuple[float, float]]] = [{} for _ in calendar]
    missing: Counter[str] = Counter()
    feature_files: list[dict[str, str]] = []
    for symbol in symbols:
        paths = {field: f"qlib_bin/features/{symbol.lower()}/{field}.day.bin"
                 for field in ("close", "volume")}
        for field, name in paths.items():
            if name not in index["files"]:
                raise QlibArchiveError(f"missing {field} feature for fixed cohort: {symbol}")
            feature_files.append({"symbol": symbol, "field": field,
                                  "sha256": index["files"][name]["sha256"]})
        close_start, closes = _read_verified_field(root_path, index, symbol, "close", len(calendar))
        volume_start, volumes = _read_verified_field(root_path, index, symbol, "volume", len(calendar))
        spans = instruments[symbol]
        for position, day in enumerate(calendar):
            if not any(begin <= day <= finish for begin, finish in spans):
                continue
            close_pos, volume_pos = position - close_start, position - volume_start
            close = closes[close_pos] if 0 <= close_pos < len(closes) else math.nan
            volume = volumes[volume_pos] if 0 <= volume_pos < len(volumes) else math.nan
            if math.isinf(close) or math.isinf(volume) or close < 0 or volume < 0:
                raise QlibArchiveError(f"invalid adjusted close/volume for {symbol}/{day}")
            if not math.isfinite(close) or close == 0:
                missing["close"] += 1
            elif not math.isfinite(volume) or volume == 0:
                missing["volume"] += 1
            else:
                daily[position][symbol] = (float(close), float(volume))
    provenance = {
        "source_id": index["source_id"], "release": index["release"],
        "cohort_version": COHORT_VERSION, "per_stratum": per_stratum,
        "strata_population": population, "symbols": symbols,
        "calendar_sha256": index["files"]["qlib_bin/calendars/day.txt"]["sha256"],
        "instruments_sha256": index["files"]["qlib_bin/instruments/all.txt"]["sha256"],
        "feature_files": feature_files, "missing_source_rows": dict(missing),
    }
    return calendar, daily, provenance


def build_group_daily(bars: Sequence[Mapping[str, tuple[float, float]]],
                      cohort_size: int, min_stocks: int, volume_ratio_clip: float
                      ) -> list[dict[str, float | int] | None]:
    """Compute observed daily group return/breadth and relative-volume proxies."""
    limit = math.log(volume_ratio_clip)
    result: list[dict[str, float | int] | None] = [None]
    for previous, current in zip(bars, bars[1:]):
        matched = sorted(previous.keys() & current.keys())
        if len(matched) < min_stocks:
            result.append(None)
            continue
        returns: list[float] = []
        signed_volume: list[float] = []
        volume_change: list[float] = []
        clipped = 0
        for symbol in matched:
            prior_close, prior_volume = previous[symbol]
            close, volume = current[symbol]
            stock_return = close / prior_close - 1.0
            log_ratio = math.log(volume / prior_volume)
            if not math.isfinite(stock_return) or not math.isfinite(log_ratio):
                raise ValueError("group feature is not finite")
            bounded = max(-limit, min(limit, log_ratio))
            clipped += int(bounded != log_ratio)
            sign = 1 if stock_return > 0 else -1 if stock_return < 0 else 0
            returns.append(stock_return)
            signed_volume.append(sign * bounded)
            volume_change.append(bounded)
        result.append({
            "group_return": mean(returns),
            "breadth": (sum(item > 0 for item in returns)
                        - sum(item < 0 for item in returns)) / len(returns),
            "signed_log_volume_change": mean(signed_volume),
            "log_volume_change": mean(volume_change),
            "participation": len(matched) / cohort_size,
            "matched_stocks": len(matched), "clipped_volume_pairs": clipped,
        })
    return result


def _features_at(daily: Sequence[dict[str, float | int] | None], position: int
                 ) -> tuple[float, ...] | None:
    if position < 4 or any(item is None for item in daily[position - 4:position + 1]):
        return None
    current = daily[position]
    assert current is not None
    returns = [float(item["group_return"]) for item in daily[position - 4:position + 1]
               if item is not None]
    return (
        float(current["group_return"]), _compound(returns), float(current["breadth"]),
        float(current["signed_log_volume_change"]),
        float(current["log_volume_change"]), float(current["participation"]),
    )


def _target_at(daily: Sequence[dict[str, float | int] | None], position: int,
               horizon: int) -> float | None:
    if position + horizon >= len(daily):
        return None
    future = daily[position + 1:position + horizon + 1]
    if any(item is None for item in future):
        return None
    return _compound([float(item["group_return"]) for item in future if item is not None])


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Small ridge normal equations, with pivoting and a closed failure mode."""
    n = len(vector)
    a = [row[:] + [value] for row, value in zip(matrix, vector)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(a[row][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ArithmeticError("ridge matrix is singular")
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= scale
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            for j in range(col, n + 1):
                a[row][j] -= factor * a[col][j]
    answer = [a[i][n] for i in range(n)]
    if any(not math.isfinite(item) for item in answer):
        raise ArithmeticError("ridge coefficients are not finite")
    return answer


def ridge_predict(training: Sequence[tuple[tuple[float, ...], float]],
                  features: tuple[float, ...], alpha: float,
                  prediction_clip: float) -> float:
    width = len(features)
    feature_means = [mean(row[0][i] for row in training) for i in range(width)]
    scales = [math.sqrt(mean((row[0][i] - feature_means[i]) ** 2 for row in training))
              for i in range(width)]
    scales = [item if item > 1e-12 else 1.0 for item in scales]
    target_mean = mean(row[1] for row in training)
    matrix = [[0.0] * width for _ in range(width)]
    vector = [0.0] * width
    for raw, target in training:
        normalized = [(raw[i] - feature_means[i]) / scales[i] for i in range(width)]
        for i in range(width):
            vector[i] += normalized[i] * (target - target_mean)
            for j in range(width):
                matrix[i][j] += normalized[i] * normalized[j]
    for i in range(width):
        matrix[i][i] += alpha
    coefficients = _solve(matrix, vector)
    prediction = target_mean + sum(
        coefficients[i] * (features[i] - feature_means[i]) / scales[i]
        for i in range(width)
    )
    if not math.isfinite(prediction):
        raise ArithmeticError("ridge prediction is not finite")
    return max(-prediction_clip, min(prediction_clip, prediction))


def _summary(records: Sequence[Mapping[str, Any]], candidates: int,
             failures: Counter[str]) -> dict[str, Any]:
    if not records:
        return {"status": "no_paired_samples", "candidate_origins": candidates,
                "paired_count": 0, "paired_coverage": 0.0 if candidates else None,
                "failure_counts": dict(sorted(failures.items()))}
    model_mae = mean(float(row["model_absolute_return_error"]) for row in records)
    baseline_mae = mean(float(row["zero_return_absolute_return_error"]) for row in records)
    directional = [row for row in records if abs(float(row["actual_return"])) > 1e-12]
    def sign(value: float) -> int:
        return 1 if value > 1e-12 else -1 if value < -1e-12 else 0
    return {
        "status": "ready", "candidate_origins": candidates,
        "paired_count": len(records),
        "paired_coverage": len(records) / candidates if candidates else None,
        "failure_counts": dict(sorted(failures.items())),
        "model_mae_return": model_mae,
        "zero_return_mae_return_on_same_cases": baseline_mae,
        "mae_skill_vs_zero_return": 1 - model_mae / baseline_mae if baseline_mae else None,
        "model_directional_hit_rate": (
            sum(sign(float(row["model_prediction_return"]))
                == sign(float(row["actual_return"])) for row in directional) /
            len(directional) if directional else None
        ),
        "model_and_baseline_sample_keys_identical": True,
    }


def _verified_event(event: MacroEvent) -> dict[str, Any]:
    """Validate a reviewed event and return its canonical provenance record."""
    if not isinstance(event, MacroEvent):
        raise ValueError("event stress requires reviewed MacroEvent records")
    record = event.to_record()  # Calls MacroEvent.validate().
    if (event.publication_certainty != "official_verified"
            or event.availability_confidence == "unverified"):
        raise ValueError(f"event stress requires verified publication and availability: {event.event_id}")
    return record


def _event_origin(calendar: Sequence[date], event: MacroEvent) -> int:
    """Choose the last close certainly preceding a verified announcement."""
    day = event.announcement_day
    # Date-only and date-verified timestamps do not establish clock availability.
    # Use the prior session even if page metadata appears to say after 15:00.
    if (event.time_precision == "minute"
            and event.availability_confidence == "clock_verified"):
        moment = event.announcement_at
        assert isinstance(moment, datetime)
        if moment.timetz().replace(tzinfo=None) >= time(15, 0):
            return bisect.bisect_right(calendar, day) - 1
    return bisect.bisect_left(calendar, day) - 1


def evaluate_group_behavior(
    calendar: Sequence[date],
    bars: Sequence[Mapping[str, tuple[float, float]]],
    cohort_size: int,
    events: Sequence[MacroEvent],
    *,
    config: PilotConfig = PilotConfig(),
) -> dict[str, Any]:
    """Run paired walk-forward forecasts and separate preannouncement stress."""
    if len(calendar) != len(bars) or not calendar or any(
        left >= right for left, right in zip(calendar, calendar[1:])
    ):
        raise ValueError("calendar/bars must align and dates must increase")
    if cohort_size < config.min_stocks:
        raise ValueError("minimum group size exceeds fixed cohort")
    reviewed_events = tuple((_verified_event(event), event) for event in events)
    daily = build_group_daily(bars, cohort_size, config.min_stocks,
                              config.volume_ratio_clip)
    features = [_features_at(daily, i) for i in range(len(calendar))]
    start_index = bisect.bisect_left(calendar, config.start)
    horizons: dict[str, Any] = {}

    for horizon in config.horizons:
        targets = [_target_at(daily, i, horizon) for i in range(len(calendar))]

        def predict_at(position: int) -> tuple[dict[str, Any] | None, str | None]:
            if position < start_index or position + horizon >= len(calendar):
                return None, "outside_evaluation_window"
            x, actual = features[position], targets[position]
            if x is None:
                return None, "missing_origin_features"
            if actual is None:
                return None, "missing_future_group_observations"
            # k+h <= position: every fitted target matured by this origin close.
            eligible = [k for k in range(start_index, position - horizon + 1)
                        if features[k] is not None and targets[k] is not None]
            selected = eligible[-config.train_window:]
            if len(selected) < config.min_train:
                return None, "insufficient_matured_training_labels"
            training = [(features[k], targets[k]) for k in selected]
            assert all(row[0] is not None and row[1] is not None for row in training)
            try:
                prediction = ridge_predict(training, x, config.ridge_alpha,
                                           config.prediction_clip)
            except ArithmeticError:
                return None, "model_fit_error"
            assert daily[position] is not None
            return {
                "origin_date": calendar[position].isoformat(),
                "target_date": calendar[position + horizon].isoformat(),
                "horizon_sessions": horizon,
                "feature_values": dict(zip(FEATURES, x)),
                "origin_matched_stocks": daily[position]["matched_stocks"],
                "origin_clipped_volume_pairs": daily[position]["clipped_volume_pairs"],
                "training_count": len(selected),
                "last_training_origin_date": calendar[selected[-1]].isoformat(),
                "last_matured_training_target_date": calendar[selected[-1] + horizon].isoformat(),
                "actual_return": actual,
                "model_prediction_return": prediction,
                "zero_return_prediction_return": 0.0,
                "model_absolute_return_error": abs(prediction - actual),
                "zero_return_absolute_return_error": abs(actual),
            }, None

        regular: list[dict[str, Any]] = []
        failures: Counter[str] = Counter()
        scheduled = list(range(start_index, len(calendar) - horizon, config.step))
        for position in scheduled:
            record, reason = predict_at(position)
            if reason:
                failures[reason] += 1
            else:
                assert record is not None
                regular.append(record)
        stressed: list[dict[str, Any]] = []
        stress_failures: Counter[str] = Counter()
        for event_record, event in reviewed_events:
            position = _event_origin(calendar, event)
            record, reason = predict_at(position)
            if reason:
                stress_failures[reason] += 1
                stressed.append({"event_id": event.event_id, "status": reason,
                                 "preannouncement_origin_date": (
                                     calendar[position].isoformat() if position >= 0 else None)})
            else:
                assert record is not None
                stressed.append({"event_id": event.event_id, "status": "ready",
                                 "announcement_at": event_record["announcement_at"], **record})
        ready_stress = [row for row in stressed if row["status"] == "ready"]
        horizons[str(horizon)] = {
            "regular": {"summary": _summary(regular, len(scheduled), failures),
                        "records": regular},
            "event_stress": {"summary": _summary(ready_stress, len(reviewed_events), stress_failures),
                             "records": stressed},
        }
    return {
        "status": "ready" if any(value["regular"]["records"] for value in horizons.values())
                  else "no_paired_samples",
        "horizons": horizons,
        "calendar_start": calendar[0].isoformat(),
        "calendar_end": calendar[-1].isoformat(),
        "cohort_size": cohort_size,
        "daily_observations_since_start": sum(
            item is not None for item, day in zip(daily, calendar) if day >= config.start),
        "volume_ratio_clipped_pairs_since_start": sum(
            int(item["clipped_volume_pairs"]) for item, day in zip(daily, calendar)
            if day >= config.start and item is not None),
    }


def run_qlib_group_behavior_pilot(
    root: str | Path, manifest: str | Path, expected_tag: str,
    event_registry: str | Path,
    *,
    config: PilotConfig = PilotConfig(),
    per_stratum: int = 3,
) -> dict[str, Any]:
    calendar, bars, provenance = load_group_bars(
        root, manifest, expected_tag, per_stratum=per_stratum)
    event_bytes = Path(event_registry).read_bytes()
    events = load_event_registry(event_registry)
    if Path(event_registry).read_bytes() != event_bytes:
        raise ValueError("event registry changed while loading")
    reviewed_events = [_verified_event(event) for event in events]
    evaluated = evaluate_group_behavior(calendar, bars, len(provenance["symbols"]),
                                        events, config=config)
    configuration = {
        "start": config.start.isoformat(), "horizons": list(config.horizons),
        "step": config.step, "min_stocks": config.min_stocks,
        "min_train": config.min_train, "train_window": config.train_window,
        "ridge_alpha": config.ridge_alpha,
        "volume_ratio_clip": config.volume_ratio_clip,
        "prediction_clip": config.prediction_clip,
        "event_eligibility": "official_verified_and_availability_verified",
        "event_origin_policy": "last_close_before_verified_announcement_clock",
    }
    event_hash = "sha256:" + hashlib.sha256(event_bytes).hexdigest()
    input_hash = "sha256:" + hashlib.sha256(_canonical({
        "provenance": provenance, "event_registry_sha256": event_hash,
        "configuration": configuration,
    })).hexdigest()
    implementation_hash = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {
        "pilot_version": PILOT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_fingerprint_sha256": input_hash,
        "implementation_sha256": implementation_hash,
        "event_registry_sha256": event_hash,
        "reviewed_events": reviewed_events,
        "event_origin_policy": "last_close_before_verified_announcement_clock",
        "source": provenance,
        "configuration": configuration,
        "model": {"name": "origin-refit-standardized-ridge", "feature_names": list(FEATURES),
                  "target": "fixed-cohort-dynamic-availability-equal-weight-future-return",
                  "volume_handling": "within-stock previous-day log ratio clipped to +/- log(4)"},
        **evaluated,
        "research_only": True,
        "institution_identity_available": False,
        "actual_net_buying_observed": False,
        "point_in_time_validated": False,
        "prospective_out_of_sample": False,
        "executable_trading_return": False,
        "limitations": [
            "40 只股票由 2026 年固定 Release 的区间事后分层选出，不代表全 A 股或当时可投资股票池。",
            "Qlib close 和 volume 均为复权特征；仅比较同股相邻日相对量能，无法还原原始成交股数。",
            "涨跌与相对量能只是群体交易行为代理，不能识别机构/散户身份、真实净买卖额或因果资金流。",
            "未来收益为日度等权动态可观测股票代理，不含开盘成交、费用、冲击成本与交易约束。",
            "政策事件压力窗口为已知历史事件的回顾性诊断，事件标签不进入模型特征。",
        ],
    }


def archive_group_behavior_record(directory: str | Path, result: Mapping[str, Any]) -> dict[str, str]:
    if result.get("pilot_version") != PILOT_VERSION:
        raise ValueError("unknown group pilot version")
    payload = dict(result)
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    record_id = uuid4().hex
    record = {"schema_version": RECORD_SCHEMA_VERSION, "record_id": record_id,
              "payload_sha256": digest, "payload": payload}
    plain = _canonical(record)
    if len(plain) > MAX_RECORD_BYTES:
        raise ValueError("group pilot record exceeds uncompressed size limit")
    compressed = gzip.compress(plain, compresslevel=6, mtime=0)
    if len(compressed) > MAX_GZIP_BYTES:
        raise ValueError("group pilot record exceeds compressed size limit")
    folder = Path(directory) / now[:10]
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    stamp = now.replace("-", "").replace(":", "").replace(".", "").replace("+00:00", "Z")
    path = folder / f"{stamp}-{record_id}.json.gz"
    temporary = folder / f".{record_id}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(compressed)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"record_id": record_id, "payload_sha256": digest,
            "path": str(path.resolve())}


def read_group_behavior_record(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    if file.stat().st_size > MAX_GZIP_BYTES:
        raise ValueError("group pilot record exceeds compressed size limit")
    with gzip.open(file, "rb") as stream:
        plain = stream.read(MAX_RECORD_BYTES + 1)
    if len(plain) > MAX_RECORD_BYTES:
        raise ValueError("group pilot record exceeds uncompressed size limit")
    record = json.loads(plain)
    if (not isinstance(record, dict) or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(record.get("payload"), dict)
            or record["payload"].get("pilot_version") != PILOT_VERSION
            or hashlib.sha256(_canonical(record["payload"])).hexdigest()
            != record.get("payload_sha256")):
        raise ValueError("group pilot record failed integrity check")
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qlib group price/relative-volume research pilot")
    parser.add_argument("--root", default="data/quant/qlib-releases/2026-09-28/published")
    parser.add_argument("--manifest", default="data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json")
    parser.add_argument("--tag", default="2026-09-28")
    parser.add_argument("--events", default="data/quant/reviewed-policy-events.json")
    parser.add_argument("--out", default="reports/quant/group-behavior-experiments")
    parser.add_argument("--step", type=int, default=20)
    args = parser.parse_args(argv)
    result = run_qlib_group_behavior_pilot(
        args.root, args.manifest, args.tag, args.events,
        config=PilotConfig(step=args.step),
    )
    saved = archive_group_behavior_record(args.out, result)
    print(json.dumps({
        "status": result["status"], "input_fingerprint_sha256": result["input_fingerprint_sha256"],
        "record": saved, "summary": {
            horizon: {group: section[group]["summary"] for group in ("regular", "event_stress")}
            for horizon, section in result["horizons"].items()
        },
    }, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
