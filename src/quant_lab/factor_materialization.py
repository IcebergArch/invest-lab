"""Explicit, unscheduled materialization of retrospective daily factors."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

from quant_lab.daily_factors_v2 import FactorBar, calculate_factor, get_factor_definition_v2
from quant_lab.factor_library import get_factor
from quant_lab.factor_store import FactorObservation, FactorStore
from quant_lab.storage import MarketStore

DAILY_SPECIFICATIONS = (
    ("overnight_gap", 1), ("intraday_return", 1),
    ("rolling_range_volatility", 20), ("relative_volume", 20),
    ("relative_amount", 20), ("amihud_illiquidity", 20),
    ("momentum", 20), ("momentum", 60), ("sma", 20), ("sma", 60),
    ("zscore", 20),
)
MATERIALIZER_VERSION = "market-daily-unit-normalized-v1"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("daily bar capture timestamp has no timezone")
    return parsed


def materialize_stock_factors(market: MarketStore, factors: FactorStore,
                              instrument_id: str, *, asof: date,
                              sessions: int = 21) -> dict[str, object]:
    """Save latest sessions from the existing market store as retrospective evidence.

    Historical daily_bars.fetched_at is a capture time, not the provider's first
    publication time. These records are useful for analysis but cannot pass a
    decision_snapshot point-in-time read.
    """
    if sessions < 1:
        raise ValueError("sessions must be positive")
    with market.connect() as connection:
        instrument = connection.execute(
            "SELECT adjustment FROM instruments WHERE instrument_id=? AND asset_type='stock'",
            (instrument_id,)).fetchone()
        if instrument is None:
            raise ValueError("stock not registered in market store")
        rows = [dict(row) for row in connection.execute("""
            SELECT trade_date,open,high,low,close,volume,amount,volume_unit,
                   amount_unit,adjustment,source_id,payload_hash,run_id,fetched_at
            FROM daily_bars WHERE instrument_id=? AND adjustment=? AND trade_date<=?
            ORDER BY trade_date
            """, (instrument_id, instrument["adjustment"], asof.isoformat()))]
    if not rows:
        return {"instrument_id": instrument_id, "status": "no_bars", "observation_count": 0}
    if instrument["adjustment"] not in ("none", "qfq"):
        raise ValueError("daily factor price basis not mapped")
    basis = "raw_unadjusted" if instrument["adjustment"] == "none" else "forward_adjusted"
    snapshot_id = _digest([MATERIALIZER_VERSION,
                           [[row["trade_date"], row["payload_hash"], row["fetched_at"]]
                            for row in rows]])
    observations = []
    start = max(0, len(rows) - sessions)
    for index in range(start, len(rows)):
        for factor_id, parameter in DAILY_SPECIFICATIONS:
            lookback = (parameter + 1 if factor_id in (
                "momentum", "overnight_gap", "relative_volume", "relative_amount",
                "amihud_illiquidity") else parameter)
            used = rows[max(0, index - lookback + 1): index + 1]
            prices = [float(row["close"]) for row in used]
            captured = max(_aware(row["fetched_at"]) for row in used)
            status = "ok"
            try:
                if factor_id in ("momentum", "sma", "zscore"):
                    definition = get_factor(factor_id)
                    value = definition.calculate(prices, parameter)
                    version = definition.version
                    unit = definition.output_unit
                else:
                    definition = get_factor_definition_v2(factor_id)
                    bars = []
                    for row in used:
                        if row["volume_unit"] not in ("share", "lot"):
                            raise ValueError("daily volume unit cannot be converted to shares")
                        volume_shares = float(row["volume"]) * (
                            100 if row["volume_unit"] == "lot" else 1)
                        amount_valid = row["amount_unit"] == "CNY" and row["amount"] is not None and row["amount"] > 0
                        bars.append(FactorBar(
                            instrument_id, date.fromisoformat(row["trade_date"]),
                            float(row["open"]), float(row["high"]), float(row["low"]),
                            float(row["close"]), volume_shares,
                            float(row["amount"]) if amount_valid else None,
                            basis, "CNY" if amount_valid else "unavailable",
                            _aware(row["fetched_at"]), row["source_id"]))
                    value = calculate_factor(factor_id, bars,
                                             decision_at=datetime.now(timezone.utc),
                                             window=parameter)
                    version = definition.version
                    unit = definition.output_unit
                if value is None:
                    status = "insufficient_history"
            except (ValueError, OverflowError):
                value = None
                status = "missing_input"
                definition = (get_factor(factor_id) if factor_id in ("momentum", "sma", "zscore")
                              else get_factor_definition_v2(factor_id))
                version = definition.version
                unit = definition.output_unit
            observations.append(FactorObservation(
                factor_id=factor_id, factor_version=version,
                parameters={"window" if factor_id != "momentum" else "lookback": parameter},
                instrument_id=instrument_id, asof_date=date.fromisoformat(rows[index]["trade_date"]),
                value=value, status=status, output_unit=unit, price_basis=basis,
                source_id=rows[index]["source_id"], source_snapshot_id=snapshot_id,
                input_sha256=_digest([MATERIALIZER_VERSION,
                                     [[row["trade_date"], row["payload_hash"]] for row in used]]),
                source_captured_at=captured, first_available_at=None,
                availability_evidence="retrospective"))
    factors.initialize()
    ids = factors.append(observations)
    return {"instrument_id": instrument_id, "status": "materialized",
            "source_snapshot_id": snapshot_id, "observation_count": len(ids),
            "latest_asof": rows[-1]["trade_date"], "point_in_time_eligible": False}
