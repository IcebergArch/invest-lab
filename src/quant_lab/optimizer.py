"""Small, auditable parameter search using the versioned north-star objective.

Selection uses training dates only.  The selected parameter set is then shown
on a later historical holdout, which remains retrospective evidence because
the stock pool and provider histories were not frozen at that boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from quant_lab.backtest import period_metrics, run_backtest, run_buy_and_hold
from quant_lab.canonical_data import (CONTRACT_VERSION, CanonicalClosePanel,
                                      MarketStoreAdapter, build_close_panel)
from quant_lab.north_star import POLICY_VERSION, score_backtest
from quant_lab.storage import MarketStore
from quant_lab.strategy_catalog import STRATEGY_VERSION
from quant_lab.strategies import (CrossSectionalMomentumStrategy,
                                  MeanReversionZScoreStrategy, SmaTrendStrategy,
                                  Strategy)


OPTIMIZER_VERSION = "chronological-grid-v1"
MAX_RECORD_BYTES = 1_000_000


def _grid(strategy_id: str) -> list[Strategy]:
    if strategy_id == "sma-trend":
        return [SmaTrendStrategy(fast_window=fast, slow_window=slow)
                for fast, slow in ((10, 40), (20, 60), (30, 90))]
    if strategy_id == "cross-sectional-momentum":
        return [CrossSectionalMomentumStrategy(lookback=lookback, top_n=1)
                for lookback in (20, 60, 120)]
    if strategy_id == "mean-reversion-zscore":
        return [MeanReversionZScoreStrategy(window=window, entry_z=entry)
                for window, entry in ((20, 1.0), (20, 1.5), (40, 1.5))]
    raise ValueError(f"unknown strategy: {strategy_id}")


def optimize_panel(
    panel: CanonicalClosePanel,
    strategy_id: str,
    *,
    cost_rate: float = 0.001,
    train_fraction: float = 0.7,
) -> dict[str, Any]:
    """Select on chronological training data; never rank by holdout metrics."""
    if panel.price_basis != "forward_adjusted" or panel.price_unit != "CNY/share":
        raise ValueError("optimizer needs same-basis forward-adjusted stock closes")
    dates = list(panel.dates)
    if len(dates) < 252:
        raise ValueError("optimizer requires at least 252 common sessions")
    if not 0.5 <= train_fraction <= 0.8:
        raise ValueError("train_fraction must be between 0.5 and 0.8")
    if not panel.closes or any(len(values) != len(dates) for values in panel.closes.values()):
        raise ValueError("invalid close panel")
    boundary = int((len(dates) - 1) * train_fraction)
    train_dates = dates[:boundary + 1]
    train_closes = {key: values[:boundary + 1] for key, values in panel.closes.items()}
    train_hold = run_buy_and_hold(train_dates, train_closes, cost_rate)
    candidates: list[dict[str, Any]] = []
    candidate_strategies = _grid(strategy_id)
    for index, strategy in enumerate(candidate_strategies):
        train_result = run_backtest(strategy, train_dates, train_closes, cost_rate)
        objective = score_backtest(train_result.metrics, train_hold.metrics)
        candidates.append({
            "candidate_index": index,
            "parameters": {key: value for key, value in vars(strategy).items() if key != "name"},
            "train_metrics": dict(train_result.metrics),
            "train_north_star": objective,
        })
    # Deterministic tie-break: better training drawdown, then the declared grid
    # order.  Later holdout data is not consulted here.
    selected = max(candidates, key=lambda item: (
        item["train_north_star"]["score"],
        item["train_metrics"]["max_drawdown"],
        -item["candidate_index"],
    ))
    winner = candidate_strategies[selected["candidate_index"]]
    full_result = run_backtest(winner, dates, panel.closes, cost_rate)
    full_hold = run_buy_and_hold(dates, panel.closes, cost_rate)
    holdout_metrics = period_metrics(full_result, boundary, len(dates) - 1)
    holdout_benchmark = period_metrics(full_hold, boundary, len(dates) - 1)
    return {
        "optimizer_version": OPTIMIZER_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "strategy_id": strategy_id,
        "strategy_version": STRATEGY_VERSION,
        "objective_policy_version": POLICY_VERSION,
        "selection_rule": "maximum training north-star score; tie: smaller drawdown, then grid order",
        "canonical_contract_version": CONTRACT_VERSION,
        "input_fingerprint_sha256": panel.input_fingerprint_sha256,
        "price_basis": panel.price_basis,
        "universe": sorted(panel.closes),
        "cost_rate": cost_rate,
        "execution_assumption": "EOD signal t; next trading close t+1 proxy fill",
        "train": {"start": dates[0].isoformat(), "end": dates[boundary].isoformat(),
                  "sessions": len(train_dates), "benchmark_metrics": dict(train_hold.metrics)},
        "candidate_grid": candidates,
        "selected_candidate_index": selected["candidate_index"],
        "selected_parameters": selected["parameters"],
        "historical_holdout": {
            "status": "retrospective_holdout_only",
            "start": dates[boundary].isoformat(), "end": dates[-1].isoformat(),
            "sessions": len(dates) - boundary,
            "metrics": holdout_metrics,
            "benchmark_metrics": holdout_benchmark,
            "north_star": score_backtest(holdout_metrics, holdout_benchmark),
            "reason": "参数只按前段选择；股票池事后选定且历史行情可修订，后段不是独立前瞻样本。",
        },
        "promotion_status": "not_eligible",
        "promotion_reason": "缺少独立前瞻样本、历史时点股票池、可交易性和完整费用验证。",
    }


def optimize_focus_stocks(store: MarketStore, strategy_id: str,
                          asof: date | None = None, cost_rate: float = 0.001
                          ) -> dict[str, Any]:
    instrument_ids = store.list_instrument_ids("focus_stock")
    if not instrument_ids:
        raise ValueError("no registered focus stocks")
    panel = build_close_panel(
        MarketStoreAdapter(store.path).iter_bars(instrument_ids=instrument_ids, end=asof),
        expected_price_basis="forward_adjusted",
    )
    if len(panel.closes) != len(instrument_ids):
        raise ValueError("normalized panel is missing a focus stock")
    return optimize_panel(panel, strategy_id, cost_rate=cost_rate)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def append_optimization_record(directory: str | Path, result: Mapping[str, Any]
                               ) -> dict[str, str]:
    if result.get("optimizer_version") != OPTIMIZER_VERSION:
        raise ValueError("unknown optimizer result version")
    payload = dict(result)
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    optimization_id = uuid4().hex
    record = {
        "schema_version": 1, "optimization_id": optimization_id,
        "created_at": now, "payload_sha256": digest, "payload": payload,
    }
    body = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False).encode("utf-8") + b"\n"
    if len(body) > MAX_RECORD_BYTES:
        raise ValueError("optimization record exceeds size limit")
    folder = Path(directory) / now[:10]
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    stamp = now.replace("-", "").replace(":", "").replace(".", "")
    path = folder / f"{stamp}-{optimization_id}.json"
    temporary = folder / f".{optimization_id}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"optimization_id": optimization_id, "created_at": now,
            "payload_sha256": digest, "path": str(path.resolve())}


def list_optimization_records(directory: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    root = Path(directory)
    if not root.exists():
        return []
    paths: list[Path] = []
    for folder in sorted((path for path in root.iterdir() if path.is_dir()), reverse=True):
        for path in sorted(folder.glob("*.json"), reverse=True):
            paths.append(path)
            if len(paths) == limit:
                break
        if len(paths) == limit:
            break
    records = []
    for path in paths:
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("optimization record exceeds size limit")
        record = json.loads(raw)
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or not isinstance(record.get("payload"), dict)
                or record["payload"].get("optimizer_version") != OPTIMIZER_VERSION
                or hashlib.sha256(_canonical(record["payload"])).hexdigest()
                != record.get("payload_sha256")):
            raise ValueError("optimization record failed integrity check")
        records.append(record)
    return records
