"""Execution-coverage stress on Qlib's 2026 CSI300 monthly-snapshot history.

The release's reconstructed membership is not verified official daily membership
or point-in-time input. Returns, when priceable, are adjusted-close proxies only.
Every sampled case remains in the report even if a fill or mark cannot be made.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Callable, Mapping, Sequence

from qlib_csi300_entry_eligibility import (FloatSeries, RELEASE_TAG,
                                           load_verified_source, selected_members,
                                           sha_json)
from random_entry_baseline import quantile

VERSION = "qlib-csi300-monthly-snapshot-stress-v4"
FIRST_ENTRY = "2021-01-04"
SEGMENTS = {"selection_history": (FIRST_ENTRY, "2023-12-29"),
            "later_history": ("2024-01-02", RELEASE_TAG)}
HORIZONS = (126, 252)
SEEDS = (202, 404, 606)
SAMPLES_PER_ENTRY_YEAR = 8
WARMUP = 60
FEE = .0005
MAX_GROSS = .8
MAX_NAME = .35
PICK_COUNT = 20
POLICIES = ("sma_active_equal", "sma_exposure_equal_20",
            "fixed_80_buy_hold_20", "cash")


def _valid(series: FloatSeries | None, index: int) -> float | None:
    value = series.at(index) if series is not None else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _feature(features: Mapping[str, Mapping[str, FloatSeries]],
             symbol: str, field: str, index: int) -> float | None:
    return _valid(features.get(symbol, {}).get(field), index)


def signal_day(calendar: Sequence[date], masks: Mapping[str, Sequence[int]],
               features: Mapping[str, Mapping[str, FloatSeries]],
               index: int, *, seed: int, pick_count: int = PICK_COUNT,
               warmup: int = WARMUP,
               fixed_symbols: Sequence[str] | None = None) -> dict:
    """Use only the release's membership and feature indices <= decision t."""
    if index < warmup - 1 or index >= len(calendar) - 1 or warmup < 60:
        raise ValueError("signal date lacks 60-session warmup or next session")
    members = sorted(symbol for symbol, mask in masks.items() if mask[index])
    if fixed_symbols is None:
        selected, member_count = selected_members(masks, calendar, index, seed,
                                                  pick_count)
    else:
        selected = tuple(fixed_symbols)
        if not selected or len(set(selected)) != len(selected):
            raise ValueError("fixed episode pool must have unique symbols")
        member_count = len(members)
    eligible: list[str] = []
    active: list[str] = []
    excluded = Counter()
    for symbol in selected:
        if _feature(features, symbol, "volume", index) is None:
            excluded["nonpositive_or_missing_t_volume"] += 1
            continue
        closes = [_feature(features, symbol, "close", j)
                  for j in range(index - 59, index + 1)]
        if any(value is None for value in closes):
            excluded["incomplete_60_session_t_close"] += 1
            continue
        eligible.append(symbol)
        if sum(closes[-20:]) / 20 > sum(closes) / 60:
            active.append(symbol)
    if active:
        raw_weight = min(1 / len(active), MAX_NAME)
        scale = min(1.0, MAX_GROSS / (raw_weight * len(active)))
        sma_active = {symbol: raw_weight * scale for symbol in active}
    else:
        sma_active = {}
    gross = sum(sma_active.values())
    if eligible:
        exposure_equal = {symbol: gross / len(eligible) for symbol in eligible
                          if gross > 0}
        fixed_per_name = min(MAX_GROSS / len(eligible), MAX_NAME)
        fixed = {symbol: fixed_per_name for symbol in eligible}
    else:
        exposure_equal, fixed = {}, {}
    for target in (sma_active, exposure_equal, fixed):
        if sum(target.values()) > MAX_GROSS + 1e-12 or any(
                weight > MAX_NAME + 1e-12 for weight in target.values()):
            raise ValueError("target exceeds budget; no silent renormalization")
    return {
        "decision_date": calendar[index].isoformat(),
        "monthly_snapshot_members": members,
        "member_count": member_count,
        "episode_fixed_pool": list(selected),
        "eligible_members": eligible,
        "active_sma_members": active,
        "ineligible_t_counts": dict(sorted(excluded.items())),
        "targets": {"sma_active_equal": sma_active,
                    "sma_exposure_equal_20": exposure_equal,
                    "fixed_80_buy_hold_20": fixed, "cash": {}},
        "sma_target_gross": gross,
    }


def eligible_entry_indices(calendar: Sequence[date], segment: str,
                           horizon: int, year: int) -> list[int]:
    if segment not in SEGMENTS or horizon < 2:
        raise ValueError("unknown segment or invalid horizon")
    first, last = SEGMENTS[segment]
    return [i for i, day in enumerate(calendar)
            if day.year == year and first <= day.isoformat() <= last
            and i + horizon < len(calendar)
            and calendar[i + horizon].isoformat() <= last]


def sample_entry_indices(eligible: Sequence[int], *, seed: int,
                         year: int, horizon: int,
                         count: int = SAMPLES_PER_ENTRY_YEAR) -> list[int]:
    if count < 1:
        raise ValueError("sample count must be positive")
    return sorted(random.Random(seed + year * 1009 + horizon).sample(
        list(eligible), min(len(eligible), count)))


def disjoint_entries(indices: Sequence[int], horizon: int) -> list[int]:
    result = []
    last_exit = -1
    for index in sorted(set(indices)):
        if index > last_exit:
            result.append(index)
            last_exit = index + horizon
    return result


def t_member_exit_events(calendar: Sequence[date], masks: Mapping[str, Sequence[int]],
                         members: Sequence[str], start: int, end: int) -> list[dict]:
    """Membership transitions only; an exit does not imply a stock cannot trade."""
    return [{"symbol": symbol, "last_member_date": calendar[j - 1].isoformat(),
             "first_outside_date": calendar[j].isoformat()}
            for symbol in members for j in range(start + 1, end + 1)
            if masks[symbol][j - 1] and not masks[symbol][j]]


def simulate_episode(calendar: Sequence[date], masks: Mapping[str, Sequence[int]],
                     features: Mapping[str, Mapping[str, FloatSeries]],
                     target_at: Callable[[int, str], Mapping[str, float]],
                     *, start: int, horizon: int, policy: str,
                     case_id: str, fee: float = FEE) -> dict:
    """Stop at the first impossible fill/mark; retain the failed case and order ID."""
    if policy not in POLICIES or horizon < 2 or start < 0 or start + horizon >= len(calendar):
        raise ValueError("invalid episode contract")
    if not math.isfinite(fee) or not 0 <= fee < .5:
        raise ValueError("invalid fee")
    end = start + horizon
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    turnover_total = 0.0
    fee_paid_equity_units = 0.0
    proxy_order_count = 0
    proxy_trade_sessions = 0
    weights: dict[str, float] = {}
    initial_orders: list[dict] = []
    held_exit_events: list[dict] = []
    zero_volume_held_marks = 0
    initial_decision_id = f"{case_id}:{policy}:{calendar[start].isoformat()}"

    def failed(reason: str, index: int, symbol: str, *,
               decision_id: str, order_id: str | None = None) -> dict:
        if index == start + 1:
            for order in initial_orders:
                if order["status"] == "proxy_fill_candidate":
                    order["status"] = "not_executed_due_to_batch_failure"
        return {
            "status": "not_priceable_or_unfilled", "proxy_return": None,
            "max_drawdown": None, "turnover_with_exit": None,
            "equity_before_failure": equity,
            "fee_paid_before_failure_equity_units": fee_paid_equity_units,
            "proxy_order_count_before_failure": proxy_order_count,
            "proxy_trade_sessions_before_failure": proxy_trade_sessions,
            "first_failure": {"reason": reason, "date": calendar[index].isoformat(),
                              "symbol": symbol, "decision_id": decision_id,
                              "order_id": order_id},
            "initial_decision_id": initial_decision_id,
            "initial_orders": initial_orders,
            "held_constituent_exit_events_before_failure": held_exit_events,
            "held_zero_volume_mark_count_before_failure": zero_volume_held_marks,
        }

    for index in range(start + 1, end + 1):
        decision_index = index - 1
        decision_id = (f"{case_id}:{policy}:final_exit" if index == end else
                       f"{case_id}:{policy}:{calendar[decision_index].isoformat()}")
        for symbol in weights:
            if masks[symbol][index - 1] and not masks[symbol][index]:
                held_exit_events.append({"symbol": symbol,
                                         "first_outside_date": calendar[index].isoformat()})
        growth = {}
        for symbol in weights:
            previous = _feature(features, symbol, "close", index - 1)
            current = _feature(features, symbol, "close", index)
            if previous is None or current is None:
                return failed("unpriceable_held_close", index, symbol,
                              decision_id=decision_id)
            growth[symbol] = current / previous
            if _feature(features, symbol, "volume", index) is None:
                zero_volume_held_marks += 1
        gross_factor = 1 - sum(weights.values()) + sum(
            weight * growth[symbol] for symbol, weight in weights.items())
        if gross_factor <= 0 or not math.isfinite(gross_factor):
            return failed("invalid_adjusted_mark", index, "*", decision_id=decision_id)
        equity *= gross_factor
        before = {symbol: weight * growth[symbol] / gross_factor
                  for symbol, weight in weights.items()}
        desired = ({} if index == end else
                   dict(before) if policy == "fixed_80_buy_hold_20" and index > start + 1
                   else dict(target_at(decision_index, policy)))
        no_new_target = (policy == "fixed_80_buy_hold_20"
                         and start + 1 < index < end)
        if not no_new_target and (any(
                not math.isfinite(weight) or weight < 0 or weight > MAX_NAME + 1e-12
                for weight in desired.values())
                or sum(desired.values()) > MAX_GROSS + 1e-12):
            raise ValueError("target provider violated budget")
        if index == start + 1:
            for symbol, target in sorted(desired.items()):
                order_id = f"{decision_id}:{symbol}"
                close = _feature(features, symbol, "close", index)
                volume = _feature(features, symbol, "volume", index)
                reason = ("missing_next_close" if close is None else
                          "nonpositive_next_volume" if volume is None else None)
                initial_orders.append({"order_id": order_id, "symbol": symbol,
                                       "target_weight": target,
                                       "proxy_fill_date": calendar[index].isoformat(),
                                       "status": "proxy_fill_candidate" if reason is None else "proxy_rejected",
                                       "reason": reason,
                                       "adjusted_close_proxy": close})
        trade_symbols = sorted(set(before) | set(desired))
        for symbol in trade_symbols:
            delta = desired.get(symbol, 0.0) - before.get(symbol, 0.0)
            if abs(delta) <= 1e-12:
                continue
            order_id = f"{decision_id}:{symbol}"
            if _feature(features, symbol, "close", index) is None:
                return failed("unfillable_missing_close", index, symbol,
                              decision_id=decision_id, order_id=order_id)
            if _feature(features, symbol, "volume", index) is None:
                return failed("unfillable_nonpositive_volume", index, symbol,
                              decision_id=decision_id, order_id=order_id)
        turnover = sum(abs(desired.get(symbol, 0.0) - before.get(symbol, 0.0))
                       for symbol in trade_symbols)
        if index == start + 1:
            for order in initial_orders:
                if order["status"] == "proxy_fill_candidate":
                    order["status"] = "proxy_filled"
        fee_paid_equity_units += equity * fee * turnover
        equity *= 1 - fee * turnover
        turnover_total += turnover
        proxy_order_count += sum(
            abs(desired.get(symbol, 0.0) - before.get(symbol, 0.0)) > 1e-12
            for symbol in trade_symbols)
        proxy_trade_sessions += int(turnover > 1e-12)
        weights = {symbol: weight for symbol, weight in desired.items() if weight > 0}
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    return {
        "status": "priced_adjusted_close_proxy", "proxy_return": equity - 1,
        "max_drawdown": drawdown, "turnover_with_exit": turnover_total,
        "fee_paid_equity_units": fee_paid_equity_units,
        "proxy_order_count": proxy_order_count,
        "proxy_trade_sessions": proxy_trade_sessions,
        "first_failure": None, "initial_decision_id": initial_decision_id,
        "initial_orders": initial_orders,
        "held_constituent_exit_events_before_failure": held_exit_events,
        "held_zero_volume_mark_count_before_failure": zero_volume_held_marks,
    }


def _summary(cases: Sequence[dict], horizon: int) -> dict:
    if not cases:
        return {"sample_count": 0, "jointly_priceable_count": 0,
                "paired_selection_excess_vs_same_exposure": None,
                "paired_excess_vs_fixed_80": None}
    priceable = [row for row in cases if all(
        row["policies"][key]["status"] == "priced_adjusted_close_proxy"
        for key in ("sma_active_equal", "sma_exposure_equal_20",
                    "fixed_80_buy_hold_20"))]
    paired_selection = [row["policies"]["sma_active_equal"]["proxy_return"]
              - row["policies"]["sma_exposure_equal_20"]["proxy_return"]
              for row in priceable]
    paired_fixed = [row["policies"]["sma_active_equal"]["proxy_return"]
              - row["policies"]["fixed_80_buy_hold_20"]["proxy_return"]
              for row in priceable]
    drawdown_selection = [
        row["policies"]["sma_active_equal"]["max_drawdown"]
        - row["policies"]["sma_exposure_equal_20"]["max_drawdown"]
        for row in priceable]
    drawdown_fixed = [
        row["policies"]["sma_active_equal"]["max_drawdown"]
        - row["policies"]["fixed_80_buy_hold_20"]["max_drawdown"]
        for row in priceable]
    value = (lambda xs: {"median": median(xs), "p10": quantile(xs, .1),
                         "p90": quantile(xs, .9)} if xs else None)
    status = {policy: dict(sorted(Counter(
        row["policies"][policy]["status"] for row in cases).items()))
              for policy in POLICIES}
    failures = {policy: dict(sorted(Counter(
        row["policies"][policy]["first_failure"]["reason"]
        for row in cases if row["policies"][policy]["first_failure"] is not None).items()))
        for policy in POLICIES}
    disjoint = disjoint_entries([row["start_index"] for row in cases], horizon)
    by_index = {row["start_index"]: row for row in cases}
    independent_priceable = [by_index[i] for i in disjoint if by_index[i] in priceable]
    buy_signal = [row for row in cases if row["signal_at_t"]["sma_target_gross"] > 0]
    buy_priceable = [row for row in priceable
                     if row["signal_at_t"]["sma_target_gross"] > 0]
    policy_metrics = {
        policy: {
            "net_return": value([row["policies"][policy]["proxy_return"]
                                  for row in priceable]),
            "max_drawdown": value([row["policies"][policy]["max_drawdown"]
                                    for row in priceable]),
            "turnover_with_exit": value([row["policies"][policy]["turnover_with_exit"]
                                          for row in priceable]),
            "fee_paid_equity_units": value([row["policies"][policy]["fee_paid_equity_units"]
                                            for row in priceable]),
            "proxy_order_count": value([row["policies"][policy]["proxy_order_count"]
                                        for row in priceable]),
        } for policy in POLICIES}
    return {
        "sample_count": len(cases),
        "sampled_decision_dates": [row["decision_date"] for row in cases],
        "disjoint_sample_count": len(disjoint),
        "disjoint_jointly_priceable_count": len(independent_priceable),
        "immediate_sma_buy_signal_count": len(buy_signal),
        "sma_entry_target_at_80pct_count": sum(
            abs(row["signal_at_t"]["sma_target_gross"] - MAX_GROSS) <= 1e-12
            for row in cases),
        "sma_entry_target_at_80pct_fraction": sum(
            abs(row["signal_at_t"]["sma_target_gross"] - MAX_GROSS) <= 1e-12
            for row in cases) / len(cases),
        "median_active_sma_names_at_entry": median([
            len(row["signal_at_t"]["active_sma_members"]) for row in cases]),
        "median_eligible_fixed_pool_names_at_entry": median([
            len(row["signal_at_t"]["eligible_members"]) for row in cases]),
        "buy_signal_conditional_jointly_priceable_count": len(buy_priceable),
        "buy_signal_conditional_paired_selection_excess": value([
            row["policies"]["sma_active_equal"]["proxy_return"]
            - row["policies"]["sma_exposure_equal_20"]["proxy_return"]
            for row in buy_priceable]),
        "buy_signal_conditional_paired_excess_vs_fixed_80": value([
            row["policies"]["sma_active_equal"]["proxy_return"]
            - row["policies"]["fixed_80_buy_hold_20"]["proxy_return"]
            for row in buy_priceable]),
        "jointly_priceable_count": len(priceable),
        "jointly_priceable_case_ids": [row["case_id"] for row in priceable],
        "status_counts": status, "failure_reason_counts": failures,
        "sma_proxy_return_on_jointly_priceable": value([
            row["policies"]["sma_active_equal"]["proxy_return"] for row in priceable]),
        "exposure_equal_proxy_return_on_jointly_priceable": value([
            row["policies"]["sma_exposure_equal_20"]["proxy_return"]
            for row in priceable]),
        "fixed_80_proxy_return_on_jointly_priceable": value([
            row["policies"]["fixed_80_buy_hold_20"]["proxy_return"]
            for row in priceable]),
        "paired_selection_excess_vs_same_exposure": value(paired_selection),
        "paired_excess_vs_fixed_80": value(paired_fixed),
        "paired_max_drawdown_change_vs_same_exposure": value(drawdown_selection),
        "paired_max_drawdown_change_vs_fixed_80": value(drawdown_fixed),
        "policy_metrics_on_jointly_priceable": policy_metrics,
        "disjoint_paired_selection_excess": value([
            row["policies"]["sma_active_equal"]["proxy_return"]
            - row["policies"]["sma_exposure_equal_20"]["proxy_return"]
            for row in independent_priceable]),
        "cash_proxy_return": 0.0,
    }


def evaluate(calendar: Sequence[date], masks: Mapping[str, Sequence[int]],
             features: Mapping[str, Mapping[str, FloatSeries]], *,
             seeds: Sequence[int] = SEEDS,
             horizons: Sequence[int] = HORIZONS,
             pick_count: int = PICK_COUNT,
             samples_per_entry_year: int = SAMPLES_PER_ENTRY_YEAR) -> dict:
    if not calendar or list(calendar) != sorted(set(calendar)):
        raise ValueError("calendar must be increasing and unique")
    if any(len(mask) != len(calendar) for mask in masks.values()):
        raise ValueError("membership mask length differs from calendar")
    cases = {}
    summaries = {}
    for segment, (first, last) in SEGMENTS.items():
        years = list(range(date.fromisoformat(first).year,
                           date.fromisoformat(last).year + 1))
        cases[segment], summaries[segment] = {}, {}
        for horizon in horizons:
            cases[segment][str(horizon)] = {}
            summaries[segment][str(horizon)] = {}
            for seed in seeds:
                all_rows = []
                for year in years:
                    eligible = eligible_entry_indices(calendar, segment, horizon, year)
                    indices = sample_entry_indices(eligible, seed=seed, year=year,
                                                   horizon=horizon,
                                                   count=samples_per_entry_year)
                    for index in indices:
                        end = index + horizon
                        signal = signal_day(calendar, masks, features, index,
                                            seed=seed, pick_count=pick_count)
                        fixed_pool = tuple(signal["episode_fixed_pool"])
                        case_signal_cache = {index: signal}

                        def target_at(decision_index: int, policy: str) -> Mapping[str, float]:
                            if decision_index not in case_signal_cache:
                                case_signal_cache[decision_index] = signal_day(
                                    calendar, masks, features, decision_index,
                                    seed=seed, pick_count=pick_count,
                                    fixed_symbols=fixed_pool)
                            return case_signal_cache[decision_index]["targets"][policy]

                        case_id = hashlib.sha256(
                            f"{VERSION}|{segment}|{horizon}|{seed}|{calendar[index]}".encode()
                        ).hexdigest()[:20]
                        members = signal["monthly_snapshot_members"]
                        policies = {policy: simulate_episode(
                            calendar, masks, features, target_at, start=index,
                            horizon=horizon, policy=policy, case_id=case_id)
                            for policy in POLICIES}
                        all_rows.append({
                            "case_id": case_id, "start_index": index,
                            "entry_year": year,
                            "decision_date": calendar[index].isoformat(),
                            "first_proxy_fill_date": calendar[index + 1].isoformat(),
                            "exit_date": calendar[end].isoformat(),
                            "signal_at_t": {
                                "member_count": signal["member_count"],
                                "monthly_snapshot_members": members,
                                "episode_fixed_pool": signal["episode_fixed_pool"],
                                "eligible_members": signal["eligible_members"],
                                "active_sma_members": signal["active_sma_members"],
                                "ineligible_t_counts": signal["ineligible_t_counts"],
                                "sma_target_gross": signal["sma_target_gross"],
                                "target_weights_sha256": {key: sha_json(value)
                                                           for key, value in signal["targets"].items()},
                            },
                            "t_member_exits_during_window": t_member_exit_events(
                                calendar, masks, fixed_pool, index, end),
                            "policies": policies,
                        })
                all_rows.sort(key=lambda row: row["start_index"])
                cases[segment][str(horizon)][str(seed)] = all_rows
                summaries[segment][str(horizon)][str(seed)] = {
                    "all": _summary(all_rows, horizon),
                    "by_entry_year": {str(year): _summary(
                        [row for row in all_rows if row["entry_year"] == year], horizon)
                        for year in years},
                }
    return {
        "schema_version": 1, "study_version": VERSION,
        "status": "retrospective_monthly_snapshot_execution_stress",
        "historical_daily_index_membership_confirmed": False,
        "point_in_time_source_vintage_confirmed": False,
        "actual_execution_confirmed": False,
        "release_tag": RELEASE_TAG,
        "first_entry_date": FIRST_ENTRY,
        "segments": SEGMENTS, "horizons_sessions": list(horizons),
        "seeds": list(seeds), "samples_per_entry_year": samples_per_entry_year,
        "warmup_sessions_from_2020": WARMUP,
        "cost_rate_per_side": FEE,
        "budget": {"max_gross_weight": MAX_GROSS,
                   "max_stock_weight": MAX_NAME},
        "hash_selected_names_per_entry": pick_count,
        "hash_selection_rule": "Once per episode at entry t: SHA256(seed|entry_date|symbol), ascending among t monthly-snapshot members; first N before checking any prices. The episode keeps those names until exit even if membership changes.",
        "policy_rules": {
            "sma_active_equal": "Original 20/60-day SMA: equal weight active names in the fixed episode pool, then cap at 80% gross and 35% per name; daily signal refresh.",
            "sma_exposure_equal_20": "Use exactly the SMA daily target gross, but spread across all eligible names in the same fixed episode pool. The net paired difference tests allocation plus resulting turnover/cost; it cannot be attributed solely to name selection.",
            "fixed_80_buy_hold_20": "At t+1 buy 80% equally across entry-eligible fixed-pool names subject to the name cap, then hold without daily rebalancing and sell at the final close.",
            "cash": "No stock target or fee.",
        },
        "t_eligibility": "At entry t hash-select 20 from release membership before inspecting prices. The episode keeps that pool, including exited members. Daily SMA trading eligibility requires 60 valid adjusted closes through its decision t and positive source-native volume at t. No future completeness filter.",
        "execution": "Entry t close decision and t+1 adjusted-close proxy fill. Both SMA policies refresh daily and proxy rebalance at the next close; the fixed-80 comparator buys once and holds. All liquidate at the final close. Missing held close or required trade price/volume makes the episode unpriceable or unfilled.",
        "valuation": "Only continuously positive adjusted closes can mark held names. Zero-volume held marks are counted; a required trade on zero volume is rejected. No last-price carry or silent deletion.",
        "ledger_scope": "Each case stores its t member list, fixed episode pool, first proxy-fill orders and statuses, trade/fee totals, membership exits, and first failed order ID. Full daily orders are reproducible by replaying this code and verified release; this report does not store every successful daily order.",
        "conditional_buy_interpretation": "The immediate positive-SMA-target subset is a signal-conditional diagnostic, not the unconditional random-observation result; its entry dates must not be compared unpaired with other policy samples.",
        "monthly_snapshot_mismatch_example": {
            "official_announcement": "https://www.sse.com.cn/market/sseindex/diclosure/c/c_20210528_5476672.shtml",
            "official_effective_after_close": "2021-06-11",
            "named_addition": "SH688111",
            "named_removal": "SH603156",
            "release_snapshot_switch": "2021-06-30",
            "known_lagged_sessions": "2021-06-15..2021-06-29 (11 trading sessions)",
        },
        "limitations": [
            "The 2026 published Qlib monthly index_weight snapshot is neither verified official daily index membership nor point-in-time data.",
            "Adjusted close and source-native volume do not establish raw-price order execution, ST status, limit queues, suspension handling, or corporate-action cash flow.",
            "Unpriceable and unfilled windows remain in the denominator; priced-window returns are conditional adjusted-close proxies only.",
            "Entry windows and repeated seeds overlap; name-level observations are not independent success trials.",
        ],
        "cases": cases, "summaries": summaries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path("data/quant/qlib-releases/2026-09-28/published"))
    parser.add_argument("--manifest", type=Path,
                        default=Path("data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json"))
    parser.add_argument("--out", type=Path,
                        default=Path("studies/random-entry-baseline-v1/qlib-csi300-monthly-snapshot-stress-v4.json"))
    parser.add_argument("--samples-per-entry-year", type=int,
                        default=SAMPLES_PER_ENTRY_YEAR)
    args = parser.parse_args()
    base = Path(__file__).resolve().parents[1]
    dependencies = [Path(__file__), base / "scripts/qlib_csi300_entry_eligibility.py",
                    base / "scripts/random_entry_baseline.py",
                    base / "src/quant_lab/qlib_local.py",
                    base / "src/quant_lab/qlib_archive.py"]

    def code_manifest() -> dict[str, str]:
        return {str(path.relative_to(base)):
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                for path in dependencies}

    source_before = code_manifest()
    calendar, masks, features, lineage = load_verified_source(args.root, args.manifest)
    report = evaluate(calendar, masks, features,
                      samples_per_entry_year=args.samples_per_entry_year)
    if code_manifest() != source_before:
        raise ValueError("implementation changed during Qlib stress run")
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["source_lineage"] = lineage
    report["code_file_sha256"] = source_before
    report["code_manifest_sha256"] = sha_json(report["code_file_sha256"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2,
                                allow_nan=False) + "\n")
    print(json.dumps({"output": str(args.out.resolve()),
                      "cases": sum(len(rows) for horizons in report["cases"].values()
                                   for seeds in horizons.values() for rows in seeds.values()),
                      "status": report["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
