"""Research-only daily rules built from versioned, time-checked factor inputs.

This module is intentionally outside the frozen paper-account runtime manifest.
Rule registration is not evidence of net return or permission to trade.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Sequence

from quant_lab.daily_factors_v2 import FactorBar, calculate_factor
from quant_lab.decision_ensemble import DecisionProposal, PortfolioBudget, combine_decisions
from quant_lab.factor_library import get_factor
from quant_lab.daily_factors_v2 import get_factor_definition_v2
from quant_lab.factor_store import FactorStore

RULE_VERSION = "daily-research-v2"


@dataclass(frozen=True)
class DailyRuleDefinition:
    rule_id: str
    factor_ids: tuple[str, ...]
    description: str
    deployment_status: str = "research_only_unvalidated"


_RULES = (
    DailyRuleDefinition(
        "gap_followthrough", ("overnight_gap", "intraday_return"),
        "隔夜上跳幅度在 0–3% 且当日开收收益为正，次日才可考虑目标；阈值仅供试验。"),
    DailyRuleDefinition(
        "low_range_risk", ("rolling_range_volatility",),
        "在当前可用股票中选 20 日高低价波动度较低的一半，作为风险候选。"),
    DailyRuleDefinition(
        "volume_confirmed_momentum", ("momentum", "relative_volume"),
        "60 日正动量且当日成交股数不低于过去 20 日中位数；次日目标等权。"),
    DailyRuleDefinition(
        "liquid_momentum", ("momentum", "amihud_illiquidity", "relative_amount"),
        "60 日正动量、当日成交额不低于近期均值；在合格股票中取日线不流动性较低的一半。"),
)
_INDEX = {rule.rule_id: rule for rule in _RULES}


def daily_rule_catalog_v2() -> list[dict[str, object]]:
    return [{"rule_id": rule.rule_id, "version": RULE_VERSION,
             "factor_ids": list(rule.factor_ids), "description": rule.description,
             "deployment_status": rule.deployment_status,
             "data_frequency": "daily", "evaluation_status": "not_evaluated"}
            for rule in _RULES]


def _validated_panel(bars_by_instrument: Mapping[str, Sequence[FactorBar]],
                     decision_at: datetime) -> tuple[list[str], str, str]:
    if decision_at.tzinfo is None or decision_at.utcoffset() is None:
        raise ValueError("decision_at must be timezone aware")
    if not bars_by_instrument:
        raise ValueError("empty daily rule panel")
    keys = sorted(bars_by_instrument)
    final_dates = set()
    bases = set()
    snapshot = []
    for key in keys:
        bars = bars_by_instrument[key]
        if not bars or any(bar.instrument_id != key for bar in bars):
            raise ValueError("instrument history identity mismatch")
        # Every rule receives the same input snapshot; calculate_factor validates
        # ascending dates, OHLC, source availability, and price basis per stock.
        calculate_factor("intraday_return", bars, decision_at=decision_at)
        final_dates.add(bars[-1].trade_date)
        bases.add(bars[-1].price_basis)
        snapshot.append([key, [[bar.trade_date.isoformat(), bar.open, bar.high,
                                bar.low, bar.close, bar.volume_shares,
                                bar.amount_cny, bar.price_basis, bar.amount_unit,
                                bar.available_at.isoformat(), bar.source_id]
                               for bar in bars]])
    if len(final_dates) != 1 or len(bases) != 1:
        raise ValueError("daily rule panel has mixed dates or price bases")
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return keys, next(iter(final_dates)).isoformat(), hashlib.sha256(payload.encode()).hexdigest()


def build_daily_rule_proposal(
        rule_id: str, bars_by_instrument: Mapping[str, Sequence[FactorBar]],
        *, decision_at: datetime) -> DecisionProposal:
    """Return a target proposal only; no fills or predictive claims are made."""
    try:
        rule = _INDEX[rule_id]
    except KeyError as exc:
        raise ValueError(f"unknown daily rule: {rule_id}") from exc
    keys, asof, snapshot_hash = _validated_panel(bars_by_instrument, decision_at)
    values: dict[str, dict[str, float | None]] = {}
    for key in keys:
        bars = bars_by_instrument[key]
        current: dict[str, float | None] = {}
        for factor_id in rule.factor_ids:
            if factor_id == "momentum":
                current[factor_id] = get_factor("momentum").calculate(
                    [bar.close for bar in bars], 60)
            else:
                current[factor_id] = calculate_factor(
                    factor_id, bars, decision_at=decision_at, window=20)
        values[key] = current
    return _proposal_from_values(rule_id, rule, keys, asof, snapshot_hash, values)


def _proposal_from_values(rule_id: str, rule: DailyRuleDefinition,
                          keys: list[str], asof: str, snapshot_hash: str,
                          values: dict[str, dict[str, float | None]]) -> DecisionProposal:
    selected: list[str] = []
    if rule_id == "gap_followthrough":
        selected = [key for key in keys
                    if values[key]["overnight_gap"] is not None
                    and 0 < values[key]["overnight_gap"] <= .03
                    and values[key]["intraday_return"] is not None
                    and values[key]["intraday_return"] > 0]
    elif rule_id == "low_range_risk":
        available = sorted((values[key]["rolling_range_volatility"], key)
                           for key in keys
                           if values[key]["rolling_range_volatility"] is not None)
        selected = [key for _, key in available[:math.ceil(len(available) / 2)]]
    elif rule_id == "volume_confirmed_momentum":
        selected = [key for key in keys
                    if values[key]["momentum"] is not None
                    and values[key]["momentum"] > 0
                    and values[key]["relative_volume"] is not None
                    and values[key]["relative_volume"] >= 1]
    elif rule_id == "liquid_momentum":
        eligible = sorted((values[key]["amihud_illiquidity"], key)
                          for key in keys
                          if values[key]["momentum"] is not None
                          and values[key]["momentum"] > 0
                          and values[key]["relative_amount"] is not None
                          and values[key]["relative_amount"] >= 1
                          and values[key]["amihud_illiquidity"] is not None)
        selected = [key for _, key in eligible[:math.ceil(len(eligible) / 2)]]
    targets = {key: (1.0 / len(selected) if key in selected else 0.0)
               for key in keys}
    return DecisionProposal(rule_id, RULE_VERSION, asof, snapshot_hash,
                            rule.factor_ids, targets, values)


def build_daily_rule_proposal_from_store(
        store: FactorStore, rule_id: str, instrument_ids: Sequence[str], *,
        asof: date, decision_at: datetime, source_snapshot_id: str,
        price_basis: str) -> DecisionProposal:
    """Apply an existing decision function to the shared PIT factor store."""
    try:
        rule = _INDEX[rule_id]
    except KeyError as exc:
        raise ValueError(f"unknown daily rule: {rule_id}") from exc
    specs = _factor_specifications(rule.factor_ids)
    snapshot = store.decision_snapshot(instrument_ids, specs, asof=asof,
                                       decision_at=decision_at,
                                       source_snapshot_id=source_snapshot_id,
                                       price_basis=price_basis)
    return _proposal_from_store_snapshot(rule_id, rule, instrument_ids, snapshot)


def _factor_specifications(factor_ids: Sequence[str]) -> list[tuple[str, str, dict]]:
    specs = []
    for factor_id in factor_ids:
        parameter = 60 if factor_id == "momentum" else (
            1 if factor_id in ("overnight_gap", "intraday_return") else 20)
        parameters = {"lookback" if factor_id == "momentum" else "window": parameter}
        version = (get_factor("momentum").version if factor_id == "momentum"
                   else get_factor_definition_v2(factor_id).version)
        specs.append((factor_id, version, parameters))
    return specs


def _proposal_from_store_snapshot(rule_id: str, rule: DailyRuleDefinition,
                                  instrument_ids: Sequence[str], snapshot: dict) -> DecisionProposal:
    values = {}
    for instrument_id, row in snapshot["values"].items():
        values[instrument_id] = {
            factor_id: next(value for key, value in row.items()
                            if key.startswith(factor_id + ":"))
            for factor_id in rule.factor_ids}
    return _proposal_from_values(rule_id, rule, sorted(instrument_ids),
                                 snapshot["asof"], snapshot["input_sha256"], values)


def combine_daily_rule_proposals_from_store(
        rule_ids: Sequence[str], store: FactorStore,
        instrument_ids: Sequence[str], function_weights: Mapping[str, float],
        budget: PortfolioBudget, *, asof: date, decision_at: datetime,
        source_snapshot_id: str, price_basis: str) -> dict:
    """Combine several factor-store decisions under one explicit budget."""
    if len(set(rule_ids)) != len(rule_ids):
        raise ValueError("duplicate daily decision rule")
    try:
        rules = [_INDEX[rule_id] for rule_id in rule_ids]
    except KeyError as exc:
        raise ValueError(f"unknown daily rule: {exc.args[0]}") from exc
    factor_ids = list(dict.fromkeys(factor_id for rule in rules for factor_id in rule.factor_ids))
    snapshot = store.decision_snapshot(
        instrument_ids, _factor_specifications(factor_ids), asof=asof,
        decision_at=decision_at, source_snapshot_id=source_snapshot_id,
        price_basis=price_basis)
    proposals = [_proposal_from_store_snapshot(rule_id, rule, instrument_ids, snapshot)
                 for rule_id, rule in zip(rule_ids, rules)]
    record = combine_decisions(proposals, function_weights, budget)
    record["deployment_status"] = "research_only_unvalidated"
    return record


def combine_daily_rule_proposals(
        rule_ids: Sequence[str], bars_by_instrument: Mapping[str, Sequence[FactorBar]],
        function_weights: Mapping[str, float], budget: PortfolioBudget,
        *, decision_at: datetime) -> dict:
    """Combine several research decisions under one explicit portfolio budget."""
    proposals = [build_daily_rule_proposal(rule_id, bars_by_instrument,
                                           decision_at=decision_at)
                 for rule_id in rule_ids]
    record = combine_decisions(proposals, function_weights, budget)
    record["deployment_status"] = "research_only_unvalidated"
    return record
