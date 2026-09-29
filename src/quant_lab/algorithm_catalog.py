"""Versioned forecast components available to the strategy research pipeline.

An algorithm component is an input to a possible signal rule.  It is not an
executable trading strategy: none of these forecast providers currently has a
registered signal, position, execution, and risk policy bound to it.

Implementation is declared here; research state is derived only from verified
local archives.  Merely listing a model never creates an experiment result.
"""
from __future__ import annotations

import re
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from quant_lab.chronos_adapter import CHRONOS_BOLT_TINY_MODEL_ID
from quant_lab.forecast import TIMESFM_MODEL_ID
from quant_lab.forecast_benchmark import read_benchmark_record
from quant_lab.forecast_qlib import read_qlib_benchmark_record
from quant_lab.forecast_stability import read_stability_record


REGISTRY_VERSION = "1"
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_RECORD_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA40 = re.compile(r"[0-9a-fA-F]{40}\Z")


# A local identifier names a model family or a pinned adapter target.  Null
# model_id means no specific external weight has been approved for this slot.
_DEFINITIONS: tuple[dict[str, Any], ...] = (
    dict(algorithm_id="random-walk", display_name="随机游走基线", provider="Built-in",
         model_id="last-observed-close", adapter="quant_lab.forecast.RandomWalkProvider",
         implementation_status="implemented", source_url=None),
    dict(algorithm_id="momentum-20-extrapolation", display_name="20 日动量外推基线",
         provider="Built-in", model_id="last-20-session-geometric-drift",
         adapter="quant_lab.forecast.Momentum20Provider",
         implementation_status="implemented", source_url=None),
    dict(algorithm_id="tft", display_name="Temporal Fusion Transformer (TFT)",
         provider="Google Research", model_id=None, adapter=None,
         implementation_status="candidate",
         source_url="https://github.com/google-research/google-research/tree/master/tft"),
    dict(algorithm_id="timesfm-2.5", display_name="TimesFM 2.5",
         provider="Google Research", model_id=TIMESFM_MODEL_ID,
         adapter="quant_lab.forecast.TimesFM25Provider",
         implementation_status="adapter_available",
         source_url="https://huggingface.co/google/timesfm-2.5-200m-pytorch"),
    dict(algorithm_id="chronos", display_name="Chronos",
         provider="Amazon Science", model_id=None, adapter=None,
         implementation_status="candidate",
         source_url="https://github.com/amazon-science/chronos-forecasting"),
    dict(algorithm_id="chronos-bolt-tiny", display_name="Chronos-Bolt tiny",
         provider="Amazon Science", model_id=CHRONOS_BOLT_TINY_MODEL_ID,
         adapter="quant_lab.chronos_adapter.ChronosBoltTinyProvider",
         implementation_status="adapter_available",
         source_url="https://huggingface.co/amazon/chronos-bolt-tiny"),
    dict(algorithm_id="chronos-2", display_name="Chronos-2",
         provider="Amazon Science", model_id="amazon/chronos-2", adapter=None,
         implementation_status="candidate",
         source_url="https://huggingface.co/amazon/chronos-2"),
    dict(algorithm_id="moirai", display_name="Moirai",
         provider="Salesforce Research", model_id=None, adapter=None,
         implementation_status="candidate",
         source_url="https://github.com/SalesforceAIResearch/uni2ts"),
    dict(algorithm_id="moirai-2", display_name="Moirai 2.0",
         provider="Salesforce Research", model_id="Salesforce/moirai-2.0-R-small",
         adapter=None, implementation_status="candidate",
         source_url="https://huggingface.co/Salesforce/moirai-2.0-R-small"),
    dict(algorithm_id="tiny-time-mixers", display_name="Tiny Time Mixers",
         provider="IBM Granite", model_id="ibm-granite/granite-timeseries-ttm-r2",
         adapter=None, implementation_status="candidate",
         source_url="https://huggingface.co/ibm-granite/granite-timeseries-ttm-r2"),
    dict(algorithm_id="granite-ts", display_name="Granite Time Series",
         provider="IBM Granite", model_id=None, adapter=None,
         implementation_status="candidate",
         source_url="https://github.com/ibm-granite/granite-tsfm"),
    dict(algorithm_id="time-moe", display_name="Time-MoE",
         provider="Time-MoE Research", model_id=None, adapter=None,
         implementation_status="candidate",
         source_url="https://github.com/Time-MoE/Time-MoE"),
    dict(algorithm_id="timer", display_name="Timer",
         provider="THUML", model_id="thuml/timer-base-84m", adapter=None,
         implementation_status="candidate",
         source_url="https://huggingface.co/thuml/timer-base-84m"),
)
_BY_ID = {item["algorithm_id"]: item for item in _DEFINITIONS}


def _archived_files(root: Path, suffix: str) -> list[Path]:
    if not root.exists() or root.is_symlink() or not root.is_dir():
        return []
    selected: list[Path] = []
    for day in sorted(root.iterdir(), key=lambda path: path.name, reverse=True):
        if not _DAY.fullmatch(day.name) or day.is_symlink() or not day.is_dir():
            continue
        for path in sorted(day.iterdir(), key=lambda item: item.name, reverse=True):
            if path.name.endswith(suffix) and path.is_file() and not path.is_symlink():
                selected.append(path)
    return selected


def _valid_provider(provider: object, definition: dict[str, Any]) -> bool:
    if not isinstance(provider, dict):
        return False
    if (provider.get("name") != definition["algorithm_id"]
            or provider.get("model_id") != definition["model_id"]
            or provider.get("status") not in ("ready", "partial_failure")):
        return False
    revision = provider.get("model_revision")
    if definition["implementation_status"] == "adapter_available":
        if not isinstance(revision, str) or not _SHA40.fullmatch(revision):
            return False
    pooled = provider.get("pooled")
    return isinstance(pooled, dict) and any(
        isinstance(item, dict) and item.get("status") == "ready"
        and type(item.get("count")) is int and item["count"] > 0
        for item in pooled.values()
    )


def _verified_experiments(report_root: Path) -> tuple[
    dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], str | None],
]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    revisions: dict[tuple[str, str], str | None] = {}
    for dataset, folder, suffix, reader in (
        ("focus", "forecast-experiments", ".json", read_benchmark_record),
        ("qlib", "forecast-experiments-qlib", ".json.gz", read_qlib_benchmark_record),
    ):
        try:
            paths = _archived_files(report_root / folder, suffix)
        except OSError:
            continue
        for path in paths:
            try:
                record = reader(path)
                payload = record["payload"]
                record_id = record["record_id"]
                if (not _RECORD_ID.fullmatch(record_id)
                        or payload.get("research_only") is not True
                        or not isinstance(payload.get("providers"), list)):
                    continue
                created_at = record["created_at"]
                if not isinstance(created_at, str):
                    continue
                for provider in payload["providers"]:
                    definition = _BY_ID.get(provider.get("name")) if isinstance(provider, dict) else None
                    if definition is None or not _valid_provider(provider, definition):
                        continue
                    revisions[(definition["algorithm_id"], record_id)] = provider.get("model_revision")
                    key = (definition["algorithm_id"], dataset)
                    if key not in latest or created_at > latest[key]["created_at"]:
                        latest[key] = {
                            "kind": "forecast_experiment", "record_id": record_id,
                            "dataset": dataset, "created_at": created_at,
                            "archive_ref": path.relative_to(report_root).as_posix(),
                            "provider_status": provider["status"],
                            "horizons": {
                                horizon: {"status": detail["status"],
                                          "sample_count": detail["count"]}
                                for horizon, detail in provider["pooled"].items()
                                if horizon in ("5", "20") and isinstance(detail, dict)
                                and detail.get("status") == "ready"
                                and type(detail.get("count")) is int
                            },
                        }
            except (OSError, UnicodeError, ValueError, TypeError, KeyError,
                    EOFError, OverflowError, zlib.error):
                # Corrupt or unverified records cannot establish execution.
                continue
    return latest, revisions


def _verified_stability(report_root: Path,
                        revisions: dict[tuple[str, str], str | None]
                        ) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    try:
        paths = _archived_files(report_root / "forecast-stability", ".json")
    except OSError:
        return latest
    for path in paths:
        try:
            record = read_stability_record(path)
            payload = record["payload"]
            algorithm_id = payload["model_name"]
            source_id = payload["source_benchmark_record_id"]
            created_at = payload["created_at"]
            if (algorithm_id not in _BY_ID
                    or (algorithm_id, source_id) not in revisions
                    or payload.get("model_revision") != revisions[(algorithm_id, source_id)]
                    or not isinstance(created_at, str)
                    or not _RECORD_ID.fullmatch(record["record_id"])):
                continue
            evidence = {
                "kind": "stability_audit", "record_id": record["record_id"],
                "source_record_id": source_id, "created_at": created_at,
                "archive_ref": path.relative_to(report_root).as_posix(),
                "report_ref": path.with_suffix(".md").relative_to(report_root).as_posix(),
            }
            if (algorithm_id not in latest
                    or created_at > latest[algorithm_id]["created_at"]):
                latest[algorithm_id] = evidence
        except (OSError, UnicodeError, ValueError, TypeError, KeyError,
                EOFError, OverflowError):
            continue
    return latest


def _archive_fingerprint(root: Path) -> tuple[tuple[str, int, int, int, int, int], ...]:
    """Invalidate verified evidence when any archive or audit artifact changes.

    Include ctime as well as mtime so a same-size rewrite with restored mtime
    cannot keep a previously verified record in the process cache.
    """
    items: list[tuple[str, int, int, int, int, int]] = []
    for folder, suffixes in (
        ("forecast-experiments", (".json",)),
        ("forecast-experiments-qlib", (".json.gz",)),
        ("forecast-stability", (".json", ".md")),
    ):
        directory = root / folder
        try:
            status = directory.lstat()
            items.append((folder, status.st_mode, status.st_size,
                          status.st_mtime_ns, status.st_ctime_ns, status.st_ino))
            if directory.is_symlink() or not directory.is_dir():
                continue
            for day in directory.iterdir():
                if not _DAY.fullmatch(day.name):
                    continue
                status = day.lstat()
                items.append((f"{folder}/{day.name}", status.st_mode, status.st_size,
                              status.st_mtime_ns, status.st_ctime_ns, status.st_ino))
                if day.is_symlink() or not day.is_dir():
                    continue
                for path in day.iterdir():
                    if not path.name.endswith(suffixes):
                        continue
                    status = path.lstat()
                    items.append((f"{folder}/{day.name}/{path.name}", status.st_mode,
                                  status.st_size, status.st_mtime_ns, status.st_ctime_ns,
                                  status.st_ino))
        except OSError:
            items.append((folder + "/unavailable", 0, 0, 0, 0, 0))
    return tuple(sorted(items))


@lru_cache(maxsize=8)
def _verified_evidence(root_path: str, fingerprint: tuple[
    tuple[str, int, int, int, int, int], ...
]) -> tuple[dict[tuple[str, str], dict[str, Any]],
           dict[tuple[str, str], str | None], dict[str, dict[str, Any]]]:
    del fingerprint  # The hashable key controls invalidation, not verification.
    root = Path(root_path)
    experiments, revisions = _verified_experiments(root)
    return experiments, revisions, _verified_stability(root, revisions)


def algorithm_catalog(report_root: str | Path) -> list[dict[str, Any]]:
    """Return immutable registrations enriched by hash-verified local evidence."""
    root = Path(report_root).absolute()
    experiments, revisions, stability = _verified_evidence(
        str(root), _archive_fingerprint(root))
    result: list[dict[str, Any]] = []
    for definition in _DEFINITIONS:
        algorithm_id = definition["algorithm_id"]
        evidence = [dict(experiments[key]) for key in ((algorithm_id, "qlib"),
                                                        (algorithm_id, "focus"))
                    if key in experiments]
        if algorithm_id in stability:
            evidence.append(dict(stability[algorithm_id]))
        has_experiment = any(item["kind"] == "forecast_experiment" for item in evidence)
        implementation = definition["implementation_status"]
        status = ("experimented" if has_experiment else
                  "adapter_only" if implementation != "candidate" else "candidate")
        newest = max((item for item in evidence if item["kind"] == "forecast_experiment"),
                     key=lambda item: item["created_at"], default=None)
        revision = revisions.get((algorithm_id, newest["record_id"])) if newest else None
        next_gate = (
            "实现适配器，固定权重与依赖，在同一数据截面完成预测实验。"
            if implementation == "candidate" and not has_experiment else
            "固定模型版本，在同一数据截面完成预测实验。" if not has_experiment else
            "制定信号、仓位、成交和风控规则，再做含成本的策略回测与样本外验证。"
        )
        result.append({
            **definition,
            "version": REGISTRY_VERSION,
            "role": "forecast_provider",
            "strategy_roles": ["signal_candidate"],
            "strategy_ids": [],
            "model_revision": revision,
            "research_status": "archived_experiment" if has_experiment else "not_evaluated",
            "status": status,
            "evidence": evidence,
            "next_gate": next_gate,
        })
    return result
