"""Predeclared daily-price candidate screen on the three-stock research pool.

This is a retrospective sensitivity study, not a deployable trading policy.
Every candidate, its exposure-matched reference, and the passive references
use the same entry dates, next-close execution, and cost on both sides.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

from quant_lab.cost_model import FEE_RATE_PER_SIDE
from quant_lab.decision_ensemble import PortfolioBudget, apply_budget
from quant_lab.strategy_catalog import get_strategy_definition
from paper_portfolio import load_panel
from random_entry_baseline import (UNIVERSE, cash_matched_hold_episode,
                                   episode, exposure_matched_targets,
                                   hold_episode, quantile)

VERSION = "robust-candidate-screen-v3"
TRAIN_END = "2023-12-29"
LATER_START = "2024-01-02"
HORIZONS = (126, 252)
SEEDS = (202, 404, 606)
SAMPLE_PER_YEAR = 24
BUDGET = PortfolioBudget(.8, .35)
CANDIDATES = (
    "sma20_60", "trend_exposure_equal_all", "timeseries_momentum_120", "cross_momentum_60",
    "inverse_volatility_20", "trend_momentum_blend", "trend_lowvol_blend",
)
_SMA = get_strategy_definition("sma-trend").build()
_CROSS_MOM = get_strategy_definition("cross-sectional-momentum").build()


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _equal(keys: Sequence[str]) -> dict[str, float]:
    return {key: 1.0 / len(keys) for key in keys} if keys else {}


def _inverse_volatility(history: Mapping[str, Sequence[float]]) -> dict[str, float]:
    if any(len(closes) < 21 for closes in history.values()):
        return {}
    inverse = {}
    for key, closes in sorted(history.items()):
        returns = [closes[-i] / closes[-i - 1] - 1 for i in range(1, 21)]
        avg = sum(returns) / 20
        volatility = math.sqrt(sum((value - avg) ** 2 for value in returns) / 20)
        inverse[key] = 1 / max(volatility, 1e-6)
    total = sum(inverse.values())
    return {key: value / total for key, value in inverse.items()}


def _blend(left: Mapping[str, float], right: Mapping[str, float],
           keys: Sequence[str]) -> dict[str, float]:
    return {key: (left.get(key, 0.0) + right.get(key, 0.0)) / 2
            for key in keys}


def candidate_targets(panel: Mapping[str, Sequence[float]]) -> dict[str, list[dict[str, float]]]:
    """Use data through the decision close only; return targets for t < last."""
    if not panel or len({len(values) for values in panel.values()}) != 1:
        raise ValueError("candidate panel must have complete common dates")
    count = len(next(iter(panel.values())))
    if count < max(HORIZONS) + 121:
        raise ValueError("panel is too short for lookback and holding horizon")
    if any(not math.isfinite(float(value)) or value <= 0
           for values in panel.values() for value in values):
        raise ValueError("candidate panel has invalid closes")
    keys = sorted(panel)
    result: dict[str, list[dict[str, float]]] = {name: [] for name in CANDIDATES}
    for index in range(count - 1):
        history = {key: panel[key][:index + 1] for key in keys}
        sma = _SMA.weights(history)
        cross = _CROSS_MOM.weights(history)
        trend = _equal([key for key in keys
                        if len(history[key]) > 120 and
                        history[key][-1] / history[key][-121] - 1 > 0])
        lowvol = _inverse_volatility(history)
        raw = {
            "sma20_60": sma,
            "timeseries_momentum_120": trend,
            "cross_momentum_60": cross,
            "inverse_volatility_20": lowvol,
            "trend_momentum_blend": _blend(trend, cross, keys),
            "trend_lowvol_blend": _blend(trend, lowvol, keys),
        }
        for name in CANDIDATES:
            if name == "trend_exposure_equal_all":
                continue
            result[name].append(apply_budget(
                {key: float(raw[name].get(key, 0.0)) for key in keys}, BUDGET))
        sma_gross = sum(result["sma20_60"][-1].values())
        result["trend_exposure_equal_all"].append(
            {key: sma_gross / len(keys) for key in keys})
    return result


def eligible_entries(dates: Sequence[str], *, year: int, horizon: int) -> list[int]:
    """A cohort may cross December, but must exit inside its train/later split."""
    if horizon < 2:
        raise ValueError("horizon must permit entry and liquidation")
    if dates != sorted(set(dates)):
        raise ValueError("dates must be strictly increasing")
    split_end = TRAIN_END if year <= 2023 else dates[-1]
    return [index for index, day in enumerate(dates)
            if day.startswith(f"{year}-") and index + horizon < len(dates)
            and dates[index + horizon] <= split_end
            and (day < LATER_START if year <= 2023 else day >= LATER_START)]


def sampled_entries(eligible: Sequence[int], *, seed: int,
                    count: int, horizon: int) -> tuple[list[int], list[int]]:
    """Return common paired dates and an interval-disjoint audit subset."""
    if count < 1 or horizon < 2:
        raise ValueError("invalid sample count or horizon")
    sampled = sorted(random.Random(seed).sample(list(eligible), min(count, len(eligible))))
    return sampled, nonoverlapping_entries(sampled, horizon)


def nonoverlapping_entries(indices: Sequence[int], horizon: int) -> list[int]:
    """One reproducible, chronologically greedy interval-disjoint subset."""
    disjoint = []
    last_exit = -1
    for index in sorted(set(indices)):
        if index > last_exit:
            disjoint.append(index)
            last_exit = index + horizon
    return disjoint


def _summary(rows: Sequence[dict]) -> dict | None:
    if not rows:
        return None
    strategies = [row["strategy"] for row in rows]
    references = [row["exposure_equal"] for row in rows]
    excess = [a["return"] - b["return"] for a, b in zip(strategies, references)]
    drawdown_delta = [a["max_drawdown"] - b["max_drawdown"]
                      for a, b in zip(strategies, references)]
    passive_excess = [a["return"] - row["passive_80"]["return"]
                      for a, row in zip(strategies, rows)]
    return {
        "episodes": len(rows),
        "median_net_return": median([x["return"] for x in strategies]),
        "p10_net_return": quantile([x["return"] for x in strategies], .1),
        "median_net_return_excess_vs_exposure_equal": median(excess),
        "p10_net_return_excess_vs_exposure_equal": quantile(excess, .1),
        "beat_exposure_equal_fraction": sum(x > 0 for x in excess) / len(rows),
        "median_paired_drawdown_change_vs_exposure_equal": median(drawdown_delta),
        "p10_paired_drawdown_change_vs_exposure_equal": quantile(drawdown_delta, .1),
        "median_excess_vs_passive_80": median(passive_excess),
        "median_turnover_with_exit": median([x["turnover"] for x in strategies]),
        "median_exposure_equal_return": median([x["return"] for x in references]),
        "median_passive_80_return": median([row["passive_80"]["return"] for row in rows]),
        "median_full_equal_hold_return": median([row["full_hold"]["return"] for row in rows]),
    }


def first_buy_after_observation(dates: Sequence[str],
                                targets: Sequence[Mapping[str, float]],
                                index: int, horizon: int) -> dict:
    """First positive decision after a random observation; None means no buy."""
    if index < 0 or index + horizon >= len(dates):
        raise ValueError("observation has no full holding window")
    # The penultimate decision is reserved for the mandatory final-close exit.
    for decision_index in range(index, min(index + horizon - 1, len(targets))):
        if sum(targets[decision_index].values()) > 0:
            return {
                "observation_date": dates[index],
                "first_positive_decision_date": dates[decision_index],
                "proxy_buy_fill_date": dates[decision_index + 1],
                "wait_sessions": decision_index - index,
            }
    return {"observation_date": dates[index],
            "first_positive_decision_date": None,
            "proxy_buy_fill_date": None, "wait_sessions": None}


def evaluate_panel(dates: Sequence[str], panel: Mapping[str, Sequence[float]], *,
                   horizons: Sequence[int] = HORIZONS,
                   seeds: Sequence[int] = SEEDS,
                   sample_per_year: int = SAMPLE_PER_YEAR,
                   fee: float = FEE_RATE_PER_SIDE) -> dict:
    if len(dates) != len(next(iter(panel.values()))) or dates != sorted(set(dates)):
        raise ValueError("invalid common-date panel")
    if not 0 <= fee < .5 or not math.isfinite(fee):
        raise ValueError("invalid fee")
    targets = candidate_targets(panel)
    matched = {name: exposure_matched_targets(series, sorted(panel))
               for name, series in targets.items()}
    years = sorted({int(day[:4]) for day in dates if day >= "2021-01-01"})
    results: dict[str, dict] = {}
    segments: dict[str, dict] = {}
    for horizon in horizons:
        horizon_rows: dict[str, dict] = {}
        horizon_segments: dict[str, dict] = {}
        for seed in seeds:
            cohorts = {}
            split_bins = {
                split: {name: {"observation": {}, "actual_buy_signal": {},
                               "wait": {}, "signal_dates": [], "eligible_count": 0}
                        for name in CANDIDATES}
                for split in ("selection", "later_check")}
            for year in years:
                eligible = eligible_entries(dates, year=year, horizon=horizon)
                observed, observed_disjoint = sampled_entries(
                    eligible, seed=seed + year * 1009 + horizon,
                    count=sample_per_year, horizon=horizon)
                if not eligible:
                    cohorts[str(year)] = {"eligible_observation_count": 0,
                                          "observation_sample_count": 0,
                                          "observation_disjoint_count": 0,
                                          "observation_dates": [],
                                          "observation_disjoint_dates": [],
                                          "candidates": {}}
                    continue
                reference_cache: dict[int, tuple[dict, dict]] = {}
                candidate_rows = {}
                split = "selection" if year <= 2023 else "later_check"
                for candidate_number, name in enumerate(CANDIDATES):
                    signal_eligible = [index for index in eligible
                                       if sum(targets[name][index].values()) > 0]
                    buy_sample, buy_disjoint = sampled_entries(
                        signal_eligible,
                        seed=seed + year * 1009 + horizon + (
                            1 if name == "trend_exposure_equal_all"
                            else candidate_number + 1) * 97,
                        count=sample_per_year, horizon=horizon)
                    all_indices = sorted(set(observed) | set(buy_sample))
                    rows: dict[int, dict] = {}
                    for index in all_indices:
                        if index not in reference_cache:
                            reference_cache[index] = (
                                cash_matched_hold_episode(dates, panel, index,
                                                          horizon, fee,
                                                          BUDGET.max_gross_weight),
                                hold_episode(dates, panel, index, horizon, fee))
                        passive, full = reference_cache[index]
                        rows[index] = {
                            "strategy": episode(dates, panel, targets[name], index, horizon, fee),
                            "exposure_equal": episode(dates, panel, matched[name], index, horizon, fee),
                            "passive_80": passive, "full_hold": full,
                        }
                    waiting = [first_buy_after_observation(
                        dates, targets[name], index, horizon) for index in observed]
                    observed_waits = [item["wait_sessions"] for item in waiting
                                      if item["wait_sessions"] is not None]
                    split_bin = split_bins[split][name]
                    split_bin["observation"].update({i: rows[i] for i in observed})
                    split_bin["actual_buy_signal"].update({i: rows[i] for i in buy_sample})
                    split_bin["wait"].update({i: item for i, item in zip(observed, waiting)})
                    split_bin["eligible_count"] += len(signal_eligible)
                    if signal_eligible:
                        split_bin["signal_dates"].append(dates[signal_eligible[0]])
                    candidate_rows[name] = {
                        "first_positive_signal_in_year":
                            dates[signal_eligible[0]] if signal_eligible else None,
                        "buy_signal_eligible_count": len(signal_eligible),
                        "observation_to_first_buy": waiting,
                        "immediate_buy_signal_fraction_of_observations":
                            sum(item["wait_sessions"] == 0 for item in waiting) / len(waiting),
                        "no_buy_signal_within_horizon_count": len(waiting) - len(observed_waits),
                        "median_wait_sessions_when_signal_found":
                            median(observed_waits) if observed_waits else None,
                        "random_observation": {
                            "all": _summary([rows[i] for i in observed]),
                            "disjoint": _summary([rows[i] for i in observed_disjoint]),
                        },
                        "actual_buy_signal": {
                            "sample_count": len(buy_sample),
                            "disjoint_count": len(buy_disjoint),
                            "sampled_decision_dates": [dates[i] for i in buy_sample],
                            "disjoint_decision_dates": [dates[i] for i in buy_disjoint],
                            "all": _summary([rows[i] for i in buy_sample]),
                            "disjoint": _summary([rows[i] for i in buy_disjoint]),
                        },
                    }
                cohorts[str(year)] = {
                    "eligible_observation_count": len(eligible),
                    "observation_sample_count": len(observed),
                    "observation_disjoint_count": len(observed_disjoint),
                    "observation_dates": [dates[i] for i in observed],
                    "observation_disjoint_dates": [dates[i] for i in observed_disjoint],
                    "candidates": candidate_rows,
                }
            horizon_rows[str(seed)] = cohorts
            horizon_segments[str(seed)] = {}
            for split, candidate_bins in split_bins.items():
                horizon_segments[str(seed)][split] = {}
                for name, bin_ in candidate_bins.items():
                    obs = bin_["observation"]
                    buy = bin_["actual_buy_signal"]
                    waits = [item["wait_sessions"] for item in bin_["wait"].values()
                             if item["wait_sessions"] is not None]
                    horizon_segments[str(seed)][split][name] = {
                        "buy_signal_eligible_count": bin_["eligible_count"],
                        "first_positive_signal_in_segment":
                            min(bin_["signal_dates"]) if bin_["signal_dates"] else None,
                        "observation_count": len(obs),
                        "immediate_buy_signal_fraction_of_observations":
                            sum(item["wait_sessions"] == 0 for item in bin_["wait"].values())
                            / len(obs) if obs else None,
                        "median_wait_sessions_when_signal_found": median(waits) if waits else None,
                        "no_buy_signal_within_horizon_count": len(obs) - len(waits),
                        "random_observation": {
                            "all": _summary([obs[i] for i in sorted(obs)]),
                            "segment_disjoint": _summary([
                                obs[i] for i in nonoverlapping_entries(list(obs), horizon)]),
                        },
                        "actual_buy_signal": {
                            "sample_count": len(buy),
                            "all": _summary([buy[i] for i in sorted(buy)]),
                            "segment_disjoint": _summary([
                                buy[i] for i in nonoverlapping_entries(list(buy), horizon)]),
                        },
                    }
        results[str(horizon)] = horizon_rows
        segments[str(horizon)] = horizon_segments
    return {
        "schema_version": 1, "experiment_version": VERSION,
        "status": "retrospective_candidate_screen_unvalidated",
        "first_date": dates[0], "last_date": dates[-1],
        "universe": sorted(panel), "price_basis": "current-vintage qfq close",
        "fee_rate_per_side": fee, "horizons_sessions": list(horizons),
        "seeds": list(seeds), "samples_per_entry_year": sample_per_year,
        "candidate_ids": list(CANDIDATES),
        "candidate_definitions": {
            "sma20_60": "Existing 20/60-day SMA trend comparator; equal weight eligible names.",
            "trend_exposure_equal_all": "Independent executable timing candidate: use SMA20/60 gross exposure, then allocate it equally to all three names. Timing comes from SMA; no SMA stock selection is retained.",
            "timeseries_momentum_120": "Positive 120-session close return; equal weight eligible names.",
            "cross_momentum_60": "Highest positive 60-session close return; one name.",
            "inverse_volatility_20": "Inverse 20-session close-return volatility across all three names.",
            "trend_momentum_blend": "Equal proposal weight: time-series momentum and cross-sectional momentum.",
            "trend_lowvol_blend": "Equal proposal weight: time-series momentum and inverse volatility.",
        },
        "budget": {"max_gross_weight": BUDGET.max_gross_weight,
                   "max_stock_weight": BUDGET.max_stock_weight},
        "execution_rule": "Signal at decision close t; fill next close t+1; liquidate at horizon close without same-close rebalance.",
        "year_split": {"selection_end": TRAIN_END, "later_start": LATER_START},
        "comparison_rule": "Within each candidate and sample type, the strategy and its equal-weight reference share entry dates, daily gross target, execution, and fees. Passive references share dates and fees. Candidate-specific buy-signal dates differ across candidates.",
        "sample_types": {
            "random_observation": "Uniform random decision dates, including cash-only targets; waiting to first positive target is separately recorded.",
            "actual_buy_signal": "Uniform random dates among that candidate's positive decision targets, known at the decision close; conditional diagnostic, not the unconditional strategy return.",
        },
        "disjoint_rule": "Within each year, greedily retain sampled entries whose [decision, liquidation] intervals do not overlap.",
        "interpretation": "Repeated seeds and overlapping episodes are dependent; disjoint subsets are small. No stability or predictive claim.",
        "limitations": [
            "Three stocks were selected with hindsight; survivorship and universe selection are unresolved.",
            "Historical qfq closes were fetched in 2026, not archived point-in-time snapshots.",
            "Next-close fills and fixed 5bp each side omit price limits, suspension, lot size, slippage, tax detail, and corporate-action cash flows.",
            "These predeclared candidates were specified after later history existed; 2024+ is only a chronological retrospective slice.",
        ],
        "results_by_horizon_seed_entry_year": results,
        "results_by_horizon_seed_segment": segments,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/quant/market.sqlite3")
    parser.add_argument("--end", default="2026-09-29")
    parser.add_argument("--out", default="studies/random-entry-baseline-v1/robust-candidate-screen-v3.json")
    parser.add_argument("--samples-per-year", type=int, default=SAMPLE_PER_YEAR)
    args = parser.parse_args()
    dates, panel, lineage = load_panel(Path(args.db), list(UNIVERSE), args.end)
    report = evaluate_panel(dates, panel, sample_per_year=args.samples_per_year)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["panel_sha256"] = _digest({"dates": dates, "closes": panel})
    report["last_bar_lineage"] = lineage
    root = Path(__file__).resolve().parents[1]
    paths = [Path(__file__), root / "scripts" / "random_entry_baseline.py",
             root / "scripts" / "paper_portfolio.py",
             *[root / "src" / "quant_lab" / name for name in
               ("backtest.py", "cost_model.py", "decision_ensemble.py",
                "strategy_catalog.py", "strategies.py", "features.py", "factor_library.py")]]
    report["code_manifest_sha256"] = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths}
    report["code_sha256"] = _digest(report["code_manifest_sha256"])
    output = Path(args.out)
    if output.exists():
        raise ValueError("report already exists; choose a versioned output")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                 allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()),
                      "horizons": report["horizons_sessions"],
                      "seeds": report["seeds"],
                      "candidates": report["candidate_ids"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
