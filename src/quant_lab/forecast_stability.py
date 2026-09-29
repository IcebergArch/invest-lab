"""Paired forecast stability audit with retrospective event stress slices.

This evaluates archived close-only forecasts.  A historical event mask cannot
be used to pretend that an unscheduled announcement was known in advance.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence
from uuid import uuid4


STABILITY_VERSION = "paired-event-stability-v4"


@dataclass(frozen=True)
class StabilityCriteria:
    min_core_samples: int = 100
    min_stocks: int = 10
    min_years: int = 5
    min_blocks: int = 5
    min_coverage: float = 0.9
    min_stress_samples: int = 20
    min_stress_blocks: int = 3
    min_stress_regimes: int = 3
    min_samples_per_stress_regime: int = 5
    bootstrap_draws: int = 500
    seed: int = 20260929

    def __post_init__(self) -> None:
        if (min(self.min_core_samples, self.min_stocks, self.min_years,
                self.min_blocks, self.min_stress_samples,
                self.min_stress_blocks, self.min_stress_regimes,
                self.min_samples_per_stress_regime,
                self.bootstrap_draws) < 1
                or not 0 < self.min_coverage <= 1):
            raise ValueError("invalid stability criteria")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _records(provider: Mapping[str, Any]) -> dict[tuple[str, str, str, int], Mapping[str, Any]]:
    result: dict[tuple[str, str, str, int], Mapping[str, Any]] = {}
    for stock in provider.get("per_stock", []):
        instrument_id = stock["instrument_id"]
        for row in stock.get("records", []):
            key = (instrument_id, row["origin_date"], row["target_date"],
                   int(row["horizon_sessions"]))
            if (not instrument_id or date.fromisoformat(key[1]) >= date.fromisoformat(key[2])
                    or key[3] < 1):
                raise ValueError("invalid forecast sample key")
            for field in ("origin_close", "actual_close", "absolute_return_error"):
                value = float(row[field])
                if not math.isfinite(value) or value < 0 or (field != "absolute_return_error" and value == 0):
                    raise ValueError("invalid forecast sample value")
            if key in result:
                raise ValueError("duplicate forecast sample key")
            result[key] = row
    return result


def _skill(rows: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> float | None:
    if not rows:
        return None
    model = sum(float(candidate["absolute_return_error"]) for _, candidate in rows)
    baseline = sum(float(reference["absolute_return_error"]) for reference, _ in rows)
    return 1.0 - model / baseline if baseline > 0 else None


def _bootstrap_interval(
    rows: Sequence[tuple[tuple[str, str, str, int], Mapping[str, Any], Mapping[str, Any]]],
    config: StabilityCriteria,
    *, min_blocks: int | None = None,
) -> tuple[float | None, float | None, int]:
    # Resample a calendar year across all stocks together. A-share outcomes in
    # the same macro/policy regime are correlated; stock-year resampling would
    # overstate independent evidence for the promotion confidence interval.
    blocks: dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = defaultdict(list)
    for key, baseline, candidate in rows:
        blocks[key[1][:4]].append((baseline, candidate))
    if len(blocks) < (config.min_blocks if min_blocks is None else min_blocks):
        return None, None, len(blocks)
    groups = list(blocks.values())
    rng = random.Random(config.seed)
    estimates = []
    for _ in range(config.bootstrap_draws):
        sample = [pair for _ in groups for pair in groups[rng.randrange(len(groups))]]
        value = _skill(sample)
        if value is not None and math.isfinite(value):
            estimates.append(value)
    if len(estimates) < config.bootstrap_draws // 2:
        return None, None, len(blocks)
    estimates.sort()
    lower = estimates[int(0.025 * (len(estimates) - 1))]
    upper = estimates[int(0.975 * (len(estimates) - 1))]
    return lower, upper, len(blocks)


def _slice_summary(
    rows: Sequence[tuple[tuple[str, str, str, int], Mapping[str, Any], Mapping[str, Any]]],
    config: StabilityCriteria,
    *, min_blocks: int | None = None,
) -> dict[str, Any]:
    paired = [(reference, candidate) for _, reference, candidate in rows]
    lower, upper, blocks = _bootstrap_interval(rows, config, min_blocks=min_blocks)
    years = sorted({key[1][:4] for key, _, _ in rows})
    stocks = sorted({key[0] for key, _, _ in rows})
    intervals = [candidate["interval_10_90_covered"] for _, _, candidate in rows
                 if candidate["interval_10_90_covered"] is not None]
    directions = [candidate["direction_correct"] for _, _, candidate in rows
                  if candidate["direction_correct"] is not None]
    return {
        "count": len(rows), "stock_count": len(stocks), "year_count": len(years),
        "block_count": blocks,
        "stock_year_block_count": len({(key[0], key[1][:4]) for key, _, _ in rows}),
        "paired_mae_return_skill_vs_random_walk": _skill(paired),
        "block_bootstrap_95pct": [lower, upper],
        "mean_model_absolute_return_error": (
            mean(float(candidate["absolute_return_error"]) for _, _, candidate in rows)
            if rows else None),
        "directional_hit_rate": sum(directions) / len(directions) if directions else None,
        "directional_coverage": len(directions) / len(rows) if rows else None,
        "interval_10_90_coverage": sum(intervals) / len(intervals) if intervals else None,
        "interval_sample_count": len(intervals),
    }


def _date(value: object) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("dates must be ISO YYYY-MM-DD")
    return date.fromisoformat(value)


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("evidence timestamps must include a UTC offset")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("evidence timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _scope_filter(scope: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    if scope is None:
        return None, None
    record_id = scope.get("declaration_record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("feasible scope needs a prior declaration record ID")
    declared_at = _utc_datetime(scope.get("declared_at"))
    ids = scope.get("instrument_ids")
    if ids is not None and (not isinstance(ids, (list, tuple)) or not ids
                            or any(not isinstance(item, str) or not item for item in ids)
                            or len(ids) != len(set(ids))):
        raise ValueError("scope instrument_ids must be a nonempty unique list")
    start = _date(scope["origin_date_start"]) if scope.get("origin_date_start") else None
    end = _date(scope["origin_date_end"]) if scope.get("origin_date_end") else None
    if start and end and start > end:
        raise ValueError("scope origin dates are reversed")
    if ids is None and start is None and end is None:
        raise ValueError("scope needs an instrument or origin-date filter")
    normalized = {
        "declaration_record_id": record_id,
        "declared_at": declared_at.isoformat(),
        "instrument_ids": sorted(ids) if ids is not None else None,
        "origin_date_start": start.isoformat() if start else None,
        "origin_date_end": end.isoformat() if end else None,
    }
    return normalized, hashlib.sha256(_canonical(normalized)).hexdigest()


def _in_scope(key: tuple[str, str, str, int], scope: Mapping[str, Any]) -> bool:
    ids = scope["instrument_ids"]
    return (ids is None or key[0] in ids) and (
        scope["origin_date_start"] is None or key[1] >= scope["origin_date_start"]
    ) and (scope["origin_date_end"] is None or key[1] <= scope["origin_date_end"])


def _statistical_pass(summary: Mapping[str, Any], coverage: float | None,
                      config: StabilityCriteria) -> bool:
    lower = summary["block_bootstrap_95pct"][0]
    return (summary["count"] >= config.min_core_samples
            and summary["stock_count"] >= config.min_stocks
            and summary["year_count"] >= config.min_years
            and summary["block_count"] >= config.min_blocks
            and lower is not None and lower > 0
            and coverage is not None and coverage >= config.min_coverage)


def _validation_reasons(
    benchmark: Mapping[str, Any], provider: Mapping[str, Any], model_name: str,
    horizon: int, rows: Sequence[tuple[tuple[str, str, str, int], Mapping[str, Any], Mapping[str, Any]]],
    evidence: Mapping[str, Any] | None, scope: Mapping[str, Any] | None,
    scope_hash: str | None, *, validation_evidence_verified: bool,
    scope_declaration_verified: bool,
) -> list[str]:
    """Check a caller-supplied validation record against the evaluated cases.

    Callers must obtain the declaration and validation from independently
    verified, append-only records. This function checks their content and
    linkage; a record ID or self-reported benchmark booleans alone never pass.
    """
    reasons: list[str] = []
    if benchmark.get("point_in_time_validated") is not True:
        reasons.append("benchmark_point_in_time_unvalidated")
    if benchmark.get("prospective_out_of_sample") is not True:
        reasons.append("benchmark_not_prospective")
    if not rows:
        reasons.append("no_evaluated_cases")
    if not isinstance(evidence, Mapping):
        return reasons + ["missing_validation_record"]
    if not validation_evidence_verified:
        reasons.append("validation_record_provenance_unverified")
    if scope is not None and not scope_declaration_verified:
        reasons.append("scope_declaration_provenance_unverified")
    required = ("record_id", "declaration_record_id", "source_experiment_key_sha256",
                "source_input_fingerprint_sha256", "model_name", "model_revision",
                "horizon_sessions", "declared_at", "model_frozen_at", "validated_at",
                "first_origin_date", "last_target_date")
    if any(not evidence.get(field) for field in required):
        return reasons + ["incomplete_validation_record"]
    if (evidence.get("source_experiment_key_sha256") != benchmark.get("experiment_key_sha256")
            or evidence.get("source_input_fingerprint_sha256") != benchmark.get("input_fingerprint_sha256")
            or evidence.get("model_name") != model_name
            or evidence.get("model_revision") != provider.get("model_revision")
            or evidence.get("horizon_sessions") != horizon):
        reasons.append("validation_source_mismatch")
    if (evidence.get("point_in_time_validated") is not True
            or evidence.get("prospective_out_of_sample") is not True):
        reasons.append("validation_not_pit_prospective")
    if scope is None:
        if evidence.get("scope_sha256") is not None:
            reasons.append("validation_scope_mismatch")
    elif (evidence.get("declaration_record_id") != scope["declaration_record_id"]
          or evidence.get("scope_sha256") != scope_hash):
        reasons.append("validation_scope_mismatch")
    try:
        declared = _utc_datetime(evidence["declared_at"])
        frozen = _utc_datetime(evidence["model_frozen_at"])
        validated = _utc_datetime(evidence["validated_at"])
        first = min(_date(key[1]) for key, _, _ in rows)
        last = max(_date(key[2]) for key, _, _ in rows)
        if (declared.date() >= first or frozen.date() >= first
                or validated.date() < last):
            reasons.append("validation_not_prior_and_matured")
        if (_date(evidence["first_origin_date"]) != first
                or _date(evidence["last_target_date"]) != last):
            reasons.append("validation_period_mismatch")
        if scope is not None and _utc_datetime(scope["declared_at"]) != declared:
            reasons.append("scope_declaration_time_mismatch")
    except (ValueError, TypeError, KeyError):
        reasons.append("invalid_validation_dates")
    return reasons


def evaluate_forecast_stability(
    benchmark: Mapping[str, Any], model_name: str, *,
    event_stress_days: Sequence[str] = (),
    criteria: StabilityCriteria = StabilityCriteria(),
    predeclared_feasible_scope: Mapping[str, Any] | None = None,
    validation_evidence: Mapping[str, Any] | None = None,
    validation_evidence_verified: bool = False,
    scope_declaration_verified: bool = False,
    event_taxonomy_verified: bool = False,
    event_stress_regimes: Mapping[str, str] | None = None,
    source_benchmark_record_id: str | None = None,
    event_study_record_id: str | None = None,
) -> dict[str, Any]:
    """Audit equal forecast cases and abstention eligibility.

    A forecast is in the stress slice if its origin-to-target date interval
    touches any pre-supplied policy-event day.  This is retrospective slicing.
    Event-free "core" results are diagnostics only. Grades use every paired
    forecast within the applicable universe, including event-stress cases.
    A caller must supply a separately verified prior/prospective validation
    record to make either positive grade eligible.
    """
    if benchmark.get("benchmark_version") not in (
        "canonical-walk-forward-v1", "qlib-valid-close-walk-forward-v1"
    ):
        raise ValueError("unsupported benchmark archive")
    if any(value is not None and (not isinstance(value, str) or not value)
           for value in (source_benchmark_record_id, event_study_record_id)):
        raise ValueError("source record IDs must be nonempty strings")
    providers = {item["name"]: item for item in benchmark["providers"]}
    if "random-walk" not in providers or model_name not in providers or model_name == "random-walk":
        raise ValueError("model and built-in random-walk baseline are required")
    scope, scope_hash = _scope_filter(predeclared_feasible_scope)
    baseline = _records(providers["random-walk"])
    candidate = _records(providers[model_name])
    if not set(candidate).issubset(baseline):
        raise ValueError("candidate has forecast cases absent from baseline")
    for key in candidate:
        a, b = baseline[key], candidate[key]
        if (a["actual_close"] != b["actual_close"]
                or a["origin_close"] != b["origin_close"]):
            raise ValueError("paired forecasts disagree on observed prices")
    stress_days = sorted({_date(day).isoformat() for day in event_stress_days})
    regimes = event_stress_regimes or {}
    if (any(_date(day).isoformat() not in stress_days
            or not isinstance(regime, str) or not regime.strip()
            for day, regime in regimes.items())):
        raise ValueError("event regimes must name supplied stress days")
    taxonomy = {day: regimes[day] for day in stress_days if day in regimes}
    taxonomy_hash = hashlib.sha256(_canonical(taxonomy)).hexdigest() if taxonomy else None
    horizons = tuple(int(value) for value in benchmark["configuration"]["horizons"])
    results: dict[str, Any] = {}
    for horizon in horizons:
        eligible = [key for key in baseline if key[3] == horizon]
        rows = [(key, baseline[key], candidate[key]) for key in candidate if key[3] == horizon]
        core, stress = [], []
        for item in rows:
            key = item[0]
            (stress if any(key[1] <= day <= key[2] for day in stress_days) else core).append(item)
        coverage = len(rows) / len(eligible) if eligible else None
        all_summary = _slice_summary(rows, criteria)
        core_summary = _slice_summary(core, criteria)
        stress_summary = _slice_summary(stress, criteria, min_blocks=criteria.min_stress_blocks)
        stress_eligible = [key for key in eligible
                           if any(key[1] <= day <= key[2] for day in stress_days)]
        stress_coverage = len(stress) / len(stress_eligible) if stress_eligible else None
        stress_lower = stress_summary["block_bootstrap_95pct"][0]
        stress_check = (len(stress) >= criteria.min_stress_samples
                        and stress_summary["block_count"] >= criteria.min_stress_blocks
                        and stress_lower is not None and stress_lower > 0
                        and stress_coverage is not None
                        and stress_coverage >= criteria.min_coverage)
        regime_counts: dict[str, int] = defaultdict(int)
        for key, _, _ in stress:
            for regime in {taxonomy[day] for day in stress_days
                           if day in taxonomy and key[1] <= day <= key[2]}:
                regime_counts[regime] += 1
        tested_regimes = sum(value >= criteria.min_samples_per_stress_regime
                             for value in regime_counts.values())
        taxonomy_check = (event_taxonomy_verified
                          and len(taxonomy) == len(stress_days)
                          and tested_regimes >= criteria.min_stress_regimes
                          and isinstance(validation_evidence, Mapping)
                          and validation_evidence.get("stress_taxonomy_sha256") == taxonomy_hash)
        if taxonomy_check and rows:
            try:
                taxonomy_check = (_utc_datetime(validation_evidence["stress_taxonomy_declared_at"]).date()
                                  < min(_date(key[1]) for key, _, _ in rows))
            except (ValueError, TypeError, KeyError):
                taxonomy_check = False
        broad_statistical = _statistical_pass(all_summary, coverage, criteria)
        broad_validation_reasons = (
            _validation_reasons(benchmark, providers[model_name], model_name,
                                horizon, rows, validation_evidence, None, None,
                                validation_evidence_verified=validation_evidence_verified,
                                scope_declaration_verified=scope_declaration_verified)
            if scope is None else ["scope_specific_validation_not_broad"])
        scope_eligible = [key for key in eligible if scope is not None and _in_scope(key, scope)]
        scope_rows = [item for item in rows if scope is not None and _in_scope(item[0], scope)]
        scope_coverage = (len(scope_rows) / len(scope_eligible) if scope_eligible else None)
        scope_summary = _slice_summary(scope_rows, criteria) if scope is not None else None
        scope_is_strict_subset = bool(scope_eligible and len(scope_eligible) < len(eligible))
        scope_statistical = (scope_summary is not None and scope_is_strict_subset
                             and _statistical_pass(scope_summary, scope_coverage, criteria))
        scope_validation_reasons = (
            _validation_reasons(benchmark, providers[model_name], model_name,
                                horizon, scope_rows, validation_evidence, scope, scope_hash,
                                validation_evidence_verified=validation_evidence_verified,
                                scope_declaration_verified=scope_declaration_verified)
            if scope is not None else ["no_predeclared_scope"])
        if (broad_statistical and stress_check and taxonomy_check
                and not broad_validation_reasons
                and providers[model_name].get("status") == "ready"):
            tier = "broadly_stable"
        elif scope_statistical and not scope_validation_reasons:
            tier = "conditional_scope_only"
        else:
            tier = "coarse_or_unstable"
        results[str(horizon)] = {
            "tier": tier, "recommendation": "abstain" if tier == "coarse_or_unstable" else
                              "research_only_until_strategy_backtest",
            "candidate_status": providers[model_name]["status"],
            "eligible_baseline_samples": len(eligible), "candidate_samples": len(rows),
            "candidate_coverage": coverage,
            "all_cases": all_summary,
            "core": core_summary, "event_stress": stress_summary,
            "event_stress_eligible_baseline_samples": len(stress_eligible),
            "event_stress_candidate_coverage": stress_coverage,
            "retrospectively_excluded_from_core": len(stress),
            "broad_statistical_check_passed": broad_statistical,
            "event_stress_check_passed": stress_check,
            "event_taxonomy_check_passed": taxonomy_check,
            "event_stress_tested_regime_count": tested_regimes,
            "broad_validation_reasons": broad_validation_reasons,
            "scope_eligible_baseline_samples": len(scope_eligible),
            "scope_candidate_coverage": scope_coverage,
            "scope_is_strict_subset": scope_is_strict_subset,
            "scope_all_cases": scope_summary,
            "scope_statistical_check_passed": scope_statistical,
            "scope_validation_reasons": scope_validation_reasons,
        }
    return {
        "stability_version": STABILITY_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_benchmark_version": benchmark["benchmark_version"],
        "source_benchmark_record_id": source_benchmark_record_id,
        "source_experiment_key_sha256": benchmark["experiment_key_sha256"],
        "source_input_fingerprint_sha256": benchmark["input_fingerprint_sha256"],
        "event_study_record_id": event_study_record_id,
        "model_name": model_name,
        "model_revision": providers[model_name].get("model_revision"),
        "price_basis": benchmark["price_basis"],
        "event_stress_days": stress_days,
        "event_stress_taxonomy_sha256": taxonomy_hash,
        "event_taxonomy_verified": event_taxonomy_verified,
        "event_mask_use": "retrospective_diagnostic_only",
        "criteria": criteria.__dict__,
        "scope_declaration_record_id": scope["declaration_record_id"] if scope else None,
        "scope_sha256": scope_hash,
        "scope_filter": scope,
        "validation_record_id": (validation_evidence or {}).get("record_id"),
        "validation_evidence_verified": validation_evidence_verified,
        "scope_declaration_verified": scope_declaration_verified,
        "group_trading_behavior_validated": False,
        "point_in_time_validated": benchmark.get("point_in_time_validated") is True,
        "prospective_out_of_sample": benchmark.get("prospective_out_of_sample") is True,
        "horizons": results,
        "limitations": [
            "此批模型只使用收盘价，不能证明对群体交易行为或机构交易身份有预测力。",
            "事件前后标签来自事后完整日历；未预告政策不能在实盘中提前规避。",
            "等级只描述预测误差证据；可交易性、费用、黑天鹅风控仍需独立策略回测。",
            "声明和验证记录须由调用方独立核验来源与不可变性；此审计仅核对内容关联。",
        ],
    }


def append_stability_record(directory: str | Path, report: Mapping[str, Any]) -> dict[str, str]:
    """Append paired JSON/Markdown records; both are checked on readback."""
    if report.get("stability_version") != STABILITY_VERSION:
        raise ValueError("unknown stability record")
    payload = dict(report)
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    markdown = render_stability_markdown(payload).encode("utf-8")
    report_digest = hashlib.sha256(markdown).hexdigest()
    record_id = uuid4().hex
    body = _canonical({"record_id": record_id, "payload_sha256": digest,
                       "report_sha256": report_digest, "payload": payload}) + b"\n"
    folder = Path(directory) / datetime.now(timezone.utc).date().isoformat()
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = folder / f"{record_id}.json"
    markdown_path = folder / f"{record_id}.md"
    temporary_paths: list[Path] = []
    created_paths: list[Path] = []
    try:
        for destination, data in ((markdown_path, markdown), (path, body)):
            temporary = folder / f".{record_id}.{secrets.token_hex(8)}.tmp"
            temporary_paths.append(temporary)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
            created_paths.append(destination)
    except Exception:
        for created in created_paths:
            created.unlink(missing_ok=True)
        raise
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)
    return {"record_id": record_id, "payload_sha256": digest,
            "report_sha256": report_digest, "json_path": str(path.resolve()),
            "report_path": str(markdown_path.resolve())}


def read_stability_record(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.stat().st_size > 16_000_000:
        raise ValueError("stability record exceeds size limit")
    record = json.loads(source.read_text(encoding="utf-8"))
    markdown_path = source.with_suffix(".md")
    if markdown_path.is_file() and markdown_path.stat().st_size > 1_000_000:
        raise ValueError("stability Markdown exceeds size limit")
    if (not isinstance(record, dict)
            or not isinstance(record.get("record_id"), str)
            or len(record["record_id"]) != 32
            or any(char not in "0123456789abcdef" for char in record["record_id"])
            or source.stem != record["record_id"]
            or not isinstance(record.get("payload"), dict)
            or record["payload"].get("stability_version") != STABILITY_VERSION
            or hashlib.sha256(_canonical(record["payload"])).hexdigest()
            != record.get("payload_sha256")
            or not markdown_path.is_file()
            or hashlib.sha256(markdown_path.read_bytes()).hexdigest()
            != record.get("report_sha256")):
        raise ValueError("stability record failed integrity check")
    return record


def render_stability_markdown(report: Mapping[str, Any]) -> str:
    lines = ["# 预测稳定性审计", "",
             f"模型：{report['model_name']}；价格口径：{report['price_basis']}。",
             "事件窗口仅用于回顾性切片；等级依据包含事件样本的全部配对预测。", "",
             "| 预测窗口 | 全部配对样本 | 相对随机游走误差改善 | 95% 区块重抽区间 | 普通/事件样本 | 证据等级 |",
             "| --- | ---: | ---: | ---: | ---: | --- |"]
    verification_notes: list[str] = []
    for horizon, item in sorted(report["horizons"].items(), key=lambda pair: int(pair[0])):
        all_cases = item["all_cases"]
        skill = all_cases["paired_mae_return_skill_vs_random_walk"]
        low, high = all_cases["block_bootstrap_95pct"]
        interval = f"[{low:+.1%}, {high:+.1%}]" if low is not None and high is not None else "样本块不足"
        grade = {"broadly_stable": "广域稳定（仍需策略回测）",
                 "conditional_scope_only": "仅预声明范围可用",
                 "coarse_or_unstable": "粗预测/证据不足，弃答"}[item["tier"]]
        slice_counts = f"{item['core']['count']}/{item['event_stress']['count']}"
        lines.append(f"| {horizon} 步 | {all_cases['count']} | "
                     f"{skill:+.2%} | {interval} | {slice_counts} | {grade} |"
                     if skill is not None else
                     f"| {horizon} 步 | {all_cases['count']} | — | {interval} | "
                     f"{slice_counts} | {grade} |")
        reasons = item["broad_validation_reasons"] + item["scope_validation_reasons"]
        if item["tier"] == "coarse_or_unstable" and reasons:
            verification_notes.append(f"- {horizon} 步：`{'`, `'.join(sorted(set(reasons)))}`")
    lines += ["", "## 验证缺口", "", *verification_notes,
              "", "## 限制", "", *(f"- {note}" for note in report["limitations"]), ""]
    return "\n".join(lines)
