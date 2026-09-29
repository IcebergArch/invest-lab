"""Retrospective macro-event study on explicit daily observations.

The event list and windows are inputs fixed before inspection.  This module
describes market response; it neither estimates causal policy effects nor
derives a trading signal or an issuer's intrinsic value.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from quant_lab.canonical_data import CONTRACT_VERSION, CanonicalDailyBar


STUDY_VERSION = "fixed-macro-event-study-v1"
RECORD_SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 16_000_000
WINDOWS: Mapping[str, tuple[int, int]] = {
    "pre_20": (-20, -1),
    "pre_5": (-5, -1),
    "post_0": (0, 0),
    "post_1": (0, 1),
    "post_5": (0, 5),
    "post_20": (0, 20),
}
CONTROL_WINDOW = (-20, -6)
STRESS_WINDOW = (-5, 20)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _valid_number(value: object, *, positive: bool = False) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


@dataclass(frozen=True)
class MacroEvent:
    event_id: str
    title: str
    category: str
    announcement_at: date | datetime
    time_precision: str
    effective_date: date | None
    source_url: str
    publication_certainty: str
    availability_confidence: str
    magnitude: float | None = None
    magnitude_unit: str | None = None
    co_announcements: tuple[str, ...] = ()

    def validate(self) -> None:
        if not _ID.fullmatch(self.event_id):
            raise ValueError("event_id needs a short stable identifier")
        if not self.title.strip() or not self.category.strip():
            raise ValueError("event title and policy category are required")
        if self.time_precision not in {"minute", "date"}:
            raise ValueError("time_precision must be minute or date")
        if self.time_precision == "minute":
            if not isinstance(self.announcement_at, datetime):
                raise ValueError("minute precision needs an announcement datetime")
            if (self.announcement_at.tzinfo is None
                    or self.announcement_at.utcoffset() is None
                    or self.announcement_at.utcoffset().total_seconds() != 8 * 3600):
                raise ValueError("announcement timestamp must be Asia/Shanghai (+08:00)")
        elif isinstance(self.announcement_at, datetime) or not isinstance(
            self.announcement_at, date
        ):
            raise ValueError("date precision needs a date without an invented clock time")
        if self.effective_date is not None and (
            isinstance(self.effective_date, datetime)
            or not isinstance(self.effective_date, date)
        ):
            raise ValueError("effective_date must be a date or null")
        parsed = urlsplit(self.source_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
                or parsed.password or any(char.isspace() for char in self.source_url)):
            raise ValueError("event requires a direct HTTP(S) source URL")
        if self.publication_certainty not in {"official_verified", "unverified"}:
            raise ValueError("publication_certainty must be official_verified or unverified")
        if self.availability_confidence not in {
            "clock_verified", "date_verified", "unverified",
        }:
            raise ValueError("invalid availability_confidence")
        if self.time_precision == "date" and self.availability_confidence == "clock_verified":
            raise ValueError("a date-only source cannot verify a clock time")
        if (self.magnitude is None) != (self.magnitude_unit is None):
            raise ValueError("documented magnitude and unit must be supplied together")
        if self.magnitude is not None and (
            isinstance(self.magnitude, bool) or not isinstance(self.magnitude, (int, float))
            or not math.isfinite(self.magnitude) or not self.magnitude_unit.strip()
        ):
            raise ValueError("magnitude must be finite and have a unit")
        if (not isinstance(self.co_announcements, tuple)
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.co_announcements)):
            raise ValueError("co_announcements must be a tuple of nonempty descriptions")

    @property
    def announcement_day(self) -> date:
        return self.announcement_at.date() if isinstance(self.announcement_at, datetime) else self.announcement_at

    def to_record(self) -> dict[str, Any]:
        self.validate()
        return {
            "event_id": self.event_id, "title": self.title,
            "category": self.category,
            "announcement_at": self.announcement_at.isoformat(),
            "time_precision": self.time_precision,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "source_url": self.source_url,
            "publication_certainty": self.publication_certainty,
            "availability_confidence": self.availability_confidence,
            "magnitude": self.magnitude,
            "magnitude_unit": self.magnitude_unit,
            "co_announcements": list(self.co_announcements),
        }

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]) -> "MacroEvent":
        precision = item.get("time_precision")
        raw_at = item.get("announcement_at")
        raw_co_announcements = item.get("co_announcements", ())
        if not isinstance(raw_co_announcements, (list, tuple)):
            raise ValueError("co_announcements must be an array")
        if not isinstance(raw_at, str):
            raise ValueError("announcement_at must be an ISO date or datetime string")
        try:
            announced = (datetime.fromisoformat(raw_at) if precision == "minute"
                         else date.fromisoformat(raw_at) if precision == "date" else None)
            effective = (date.fromisoformat(item["effective_date"])
                         if item.get("effective_date") is not None else None)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid event date or timestamp") from exc
        if announced is None:
            raise ValueError("time_precision must be minute or date")
        event = cls(
            event_id=item["event_id"], title=item["title"],
            category=item["category"], announcement_at=announced,
            time_precision=precision, effective_date=effective,
            source_url=item["source_url"],
            publication_certainty=item["publication_certainty"],
            availability_confidence=item["availability_confidence"],
            magnitude=item.get("magnitude"), magnitude_unit=item.get("magnitude_unit"),
            co_announcements=tuple(raw_co_announcements),
        )
        event.validate()
        return event


def load_event_registry(path: str | Path) -> tuple[MacroEvent, ...]:
    """Load a reviewed JSON event list; no events are inferred from prices."""
    source = Path(path)
    if source.stat().st_size > 1_000_000:
        raise ValueError("event registry exceeds size limit")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("event registry must be a JSON array")
    if len(payload) > 1000 or any(not isinstance(item, dict) for item in payload):
        raise ValueError("event registry must contain at most 1000 event objects")
    events = tuple(MacroEvent.from_mapping(item) for item in payload)
    if len({event.event_id for event in events}) != len(events):
        raise ValueError("duplicate event_id in registry")
    return events


@dataclass(frozen=True)
class EventDailyBar:
    """Small event-study input compatible with canonical or verified Qlib rows.

    Qlib adjusted values may be supplied with ``price_basis=qlib_adjusted``;
    their price level/unit is never compared with a MarketStore price level.
    Unknown volume/amount units must be represented by ``None``.
    """

    instrument_id: str
    asset_type: str
    trade_date: date
    close: float
    price_basis: str
    price_unit: str
    source_id: str
    dataset_id: str
    payload_hash: str
    volume_shares: float | None = None
    amount_cny: float | None = None
    tradability: str = "unknown"
    observed_at: datetime | None = None

    @classmethod
    def from_canonical(cls, bar: CanonicalDailyBar) -> "EventDailyBar":
        bar.validate()
        return cls(
            instrument_id=bar.instrument_id, asset_type=bar.asset_type,
            trade_date=bar.trade_date, close=bar.close,
            price_basis=bar.price_basis, price_unit=bar.price_unit,
            source_id=bar.source_id, dataset_id=bar.dataset_id,
            payload_hash=bar.payload_hash, volume_shares=bar.volume_shares,
            amount_cny=bar.amount_cny, tradability=bar.tradability,
            observed_at=bar.observed_at,
        )

    def validate(self) -> None:
        if not self.instrument_id or not self.source_id or not self.dataset_id:
            raise ValueError("event bar requires instrument and source identity")
        if isinstance(self.trade_date, datetime) or not isinstance(self.trade_date, date):
            raise ValueError("event bar trade_date must be a date")
        if self.asset_type == "stock":
            if self.price_basis not in {"forward_adjusted", "qlib_adjusted"}:
                raise ValueError("event study requires adjusted stock returns")
        elif self.asset_type == "index":
            if self.price_basis != "index_points":
                raise ValueError("benchmark must use index_points")
        else:
            raise ValueError("event bar asset_type must be stock or index")
        if not self.price_unit:
            raise ValueError("price unit is required")
        if not _valid_number(self.close, positive=True):
            raise ValueError("event close must be finite and positive")
        if self.volume_shares is not None and (
            self.asset_type != "stock" or not _valid_number(self.volume_shares)
        ):
            raise ValueError("volume_shares requires valid stock share units")
        if self.amount_cny is not None and not _valid_number(self.amount_cny):
            raise ValueError("amount_cny must be finite nonnegative CNY")
        if self.tradability not in {"tradable", "ineligible", "unknown"}:
            raise ValueError("invalid tradability status")
        if not _SHA256.fullmatch(self.payload_hash):
            raise ValueError("event bar requires source payload SHA-256")
        if self.observed_at is not None and (
            self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None
        ):
            raise ValueError("observed_at needs a timezone")


def _first_full_session(event: MacroEvent, calendar: Sequence[date]) -> int:
    announced = event.announcement_day
    if (event.time_precision == "minute"
            and event.availability_confidence == "clock_verified"
            and isinstance(event.announcement_at, datetime)
            and event.announcement_at.timetz().replace(tzinfo=None) < time(9, 30)):
        return bisect.bisect_left(calendar, announced)
    return bisect.bisect_right(calendar, announced)


def _window_measure(
    rows: Mapping[date, EventDailyBar], calendar: Sequence[date],
    reaction_position: int, offsets: tuple[int, int],
) -> dict[str, Any]:
    begin = reaction_position + offsets[0]
    end = reaction_position + offsets[1]
    if begin - 1 < 0:
        status = "insufficient_prior_history"
    elif end >= len(calendar):
        status = "insufficient_future_history"
    else:
        status = "ready"
    result: dict[str, Any] = {
        "status": status,
        "start_date": calendar[begin].isoformat() if 0 <= begin < len(calendar) else None,
        "end_date": calendar[end].isoformat() if 0 <= end < len(calendar) else None,
        "return": None, "volume_ratio_to_control": None,
        "amount_ratio_to_control": None,
        "volume_status": "not_evaluated", "amount_status": "not_evaluated",
        "tradability": "unknown", "missing_dates": [],
    }
    if status != "ready":
        return result
    dates = calendar[begin - 1:end + 1]
    missing = [day.isoformat() for day in dates if day not in rows]
    if missing:
        result["status"] = "missing_bar"
        result["missing_dates"] = missing
        return result
    first = rows[calendar[begin - 1]].close
    last = rows[calendar[end]].close
    result["return"] = last / first - 1
    trade_states = {rows[day].tradability for day in dates[1:]}
    result["tradability"] = ("ineligible" if "ineligible" in trade_states else
                            "unknown" if "unknown" in trade_states else "tradable")
    control_start = reaction_position + CONTROL_WINDOW[0]
    control_end = reaction_position + CONTROL_WINDOW[1]
    if control_start < 0:
        result["volume_status"] = "insufficient_control_history"
        result["amount_status"] = "insufficient_control_history"
        return result
    control_dates = calendar[control_start:control_end + 1]
    if any(day not in rows for day in control_dates):
        result["volume_status"] = "missing_control_bar"
        result["amount_status"] = "missing_control_bar"
        return result
    observed_dates = calendar[begin:end + 1]
    if any(rows[day].tradability == "ineligible"
           for day in tuple(control_dates) + tuple(observed_dates)):
        result["volume_status"] = "ineligible_observation"
        result["amount_status"] = "ineligible_observation"
        return result
    for field, result_field, status_field in (
        ("volume_shares", "volume_ratio_to_control", "volume_status"),
        ("amount_cny", "amount_ratio_to_control", "amount_status"),
    ):
        control = [getattr(rows[day], field) for day in control_dates]
        observed = [getattr(rows[day], field) for day in observed_dates]
        if any(value is None for value in control + observed):
            result[status_field] = "unit_or_value_unavailable"
            continue
        baseline = mean(control)
        if baseline <= 0:
            result[status_field] = "zero_control_mean"
            continue
        result[result_field] = mean(observed) / baseline
        result[status_field] = "ready"
    return result


def _announcement_day_measure(
    rows: Mapping[date, EventDailyBar], calendar: Sequence[date],
    day: date,
) -> dict[str, Any]:
    position = bisect.bisect_left(calendar, day)
    if position >= len(calendar) or calendar[position] != day:
        return {"status": "non_trading_day", "return": None}
    if position == 0:
        return {"status": "insufficient_prior_history", "return": None}
    previous = calendar[position - 1]
    if previous not in rows or day not in rows:
        return {"status": "missing_bar", "return": None}
    return {"status": "ready", "return": rows[day].close / rows[previous].close - 1}


def _cohort_summary(stock_results: Mapping[str, Mapping[str, Any]],
                    members: Sequence[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {"members": list(members), "member_count": len(members),
                               "windows": {}}
    for name in WINDOWS:
        eligible = [(stock_id, stock_results[stock_id]["windows"][name])
                    for stock_id in members if stock_id in stock_results]
        ready = [(stock_id, item) for stock_id, item in eligible
                 if item["status"] == "ready" and item["excess_return"] is not None]
        summary["windows"][name] = {
            "status": "ready" if ready else "no_complete_members",
            "complete_count": len(ready),
            "missing_count": len(members) - len(ready),
            "mean_stock_return": mean(item["return"] for _, item in ready) if ready else None,
            "mean_excess_return": mean(item["excess_return"] for _, item in ready) if ready else None,
            "contributing_ids": [stock_id for stock_id, _ in ready],
            "confidence_interval": None,
        }
    return summary


def tag_event_regime_days(
    events: Sequence[MacroEvent], trading_days: Sequence[date], *, asof: date,
) -> dict[str, Any]:
    """Tag a fixed retrospective stress slice for a separate model evaluation.

    This uses the eventual event registry to label *past* data.  Pre-event
    labels and unscheduled future events cannot be known by a live strategy.
    A caller must compare core and stress metrics separately, with coverage.
    """
    if not isinstance(asof, date) or isinstance(asof, datetime):
        raise ValueError("asof must be a date")
    calendar = tuple(day for day in trading_days if day <= asof)
    if not calendar or any(not isinstance(day, date) or isinstance(day, datetime)
                           for day in calendar):
        raise ValueError("trading_days must contain dates")
    if any(left >= right for left, right in zip(calendar, calendar[1:])):
        raise ValueError("trading_days must be strictly increasing")
    tags: dict[date, list[str]] = {}
    detail: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in events:
        event.validate()
        if event.event_id in seen_ids:
            raise ValueError("duplicate event_id")
        seen_ids.add(event.event_id)
        entry: dict[str, Any] = {
            "event_id": event.event_id, "status": None,
            "reaction_date": None, "tagged_session_count": 0,
            "pre_reaction_hindsight_count": 0, "window_complete": False,
        }
        if (event.publication_certainty != "official_verified"
                or event.availability_confidence == "unverified"):
            entry["status"] = "unverified_publication_time"
        elif event.announcement_day > asof:
            entry["status"] = "not_available_asof"
        else:
            position = _first_full_session(event, calendar)
            if position >= len(calendar):
                entry["status"] = "no_reaction_session_by_asof"
            else:
                entry["status"] = "tagged"
                entry["reaction_date"] = calendar[position].isoformat()
                entry["window_complete"] = position + STRESS_WINDOW[1] < len(calendar)
                first = max(0, position + STRESS_WINDOW[0])
                last = min(len(calendar) - 1, position + STRESS_WINDOW[1])
                for index in range(first, last + 1):
                    day = calendar[index]
                    tags.setdefault(day, []).append(event.event_id)
                    entry["tagged_session_count"] += 1
                    if index < position:
                        entry["pre_reaction_hindsight_count"] += 1
        detail.append(entry)
    tagged = {day.isoformat(): sorted(ids) for day, ids in sorted(tags.items())}
    count = len(tagged)
    return {
        "slice_version": "fixed-policy-stress-slice-v1",
        "stress_window_sessions": list(STRESS_WINDOW),
        "count_scope": "benchmark_calendar_sessions_not_forecast_samples",
        "calendar_session_count": len(calendar),
        "core_session_count": len(calendar) - count,
        "stress_session_count": count,
        "excluded_core_session_count": count,
        "overlap_session_count": sum(len(ids) > 1 for ids in tags.values()),
        "event_tags_by_date": tagged,
        "events": detail,
        "retrospective_diagnostic_only": True,
        "future_unscheduled_events_avoidable": False,
        "pre_event_tags_are_hindsight": True,
    }


def evaluate_event_study(
    events: Sequence[MacroEvent],
    bars: Iterable[CanonicalDailyBar | EventDailyBar],
    *,
    benchmark_id: str,
    asof: date,
    cohorts: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Measure fixed windows against one index, without estimating causality.

    ``bars`` may combine stocks and an index, but every stock in an experiment
    must use the same adjusted basis.  The index is compared only through
    dimensionless returns; index points never mix with share price levels.
    """
    if not isinstance(asof, date) or isinstance(asof, datetime):
        raise ValueError("asof must be a date")
    if not events or len(events) > 1000:
        raise ValueError("study requires 1-1000 preregistered events")
    if len({event.event_id for event in events}) != len(events):
        raise ValueError("duplicate event_id")
    for event in events:
        event.validate()
    grouped: dict[str, dict[date, EventDailyBar]] = {}
    identity: dict[str, tuple[str, str, str]] = {}
    digest = hashlib.sha256()
    digest.update(STUDY_VERSION.encode("ascii") + b"\n")
    count = 0
    prepared: list[EventDailyBar] = []
    for supplied in bars:
        bar = (EventDailyBar.from_canonical(supplied)
               if isinstance(supplied, CanonicalDailyBar) else supplied)
        if not isinstance(bar, EventDailyBar):
            raise TypeError("bars must be canonical or EventDailyBar records")
        bar.validate()
        if bar.trade_date > asof:
            continue
        prepared.append(bar)
    prepared.sort(key=lambda bar: (bar.instrument_id, bar.trade_date))
    for bar in prepared:
        key = (bar.asset_type, bar.price_basis, bar.price_unit)
        if bar.instrument_id in identity and identity[bar.instrument_id] != key:
            raise ValueError(f"inconsistent price basis or unit for {bar.instrument_id}")
        identity[bar.instrument_id] = key
        rows = grouped.setdefault(bar.instrument_id, {})
        if bar.trade_date in rows:
            raise ValueError(f"duplicate instrument/date: {bar.instrument_id}/{bar.trade_date}")
        rows[bar.trade_date] = bar
        digest.update(_json_bytes({
            "instrument_id": bar.instrument_id, "trade_date": bar.trade_date.isoformat(),
            "close": bar.close, "volume_shares": bar.volume_shares,
            "amount_cny": bar.amount_cny, "tradability": bar.tradability,
            "price_basis": bar.price_basis, "price_unit": bar.price_unit,
            "source_id": bar.source_id, "dataset_id": bar.dataset_id,
            "payload_hash": bar.payload_hash,
            "observed_at": bar.observed_at.isoformat() if bar.observed_at else None,
        }) + b"\n")
        count += 1
    if benchmark_id not in grouped or identity[benchmark_id][0:2] != ("index", "index_points"):
        raise ValueError("benchmark_id must identify an index_points series")
    stock_ids = sorted(key for key, value in identity.items() if value[0] == "stock")
    if not stock_ids:
        raise ValueError("study needs at least one adjusted stock")
    stock_bases = {identity[key][1] for key in stock_ids}
    if len(stock_bases) != 1:
        raise ValueError("cannot mix stock adjustment bases in one event study")
    cohort_members: dict[str, list[str]] = {}
    assigned: set[str] = set()
    if cohorts:
        for label, members in sorted(cohorts.items()):
            if not label.strip() or not members:
                raise ValueError("cohort label and members are required")
            if label == "all_selected_stocks":
                raise ValueError("all_selected_stocks is a reserved cohort label")
            member_ids = sorted(set(members))
            if len(member_ids) != len(members) or any(key not in stock_ids for key in member_ids):
                raise ValueError("cohorts must list unique included stocks")
            if any(key in assigned for key in member_ids):
                raise ValueError("cohort membership must be exclusive")
            assigned.update(member_ids)
            cohort_members[label] = member_ids
    cohort_members["all_selected_stocks"] = stock_ids
    calendar = tuple(sorted(grouped[benchmark_id]))
    if not calendar:
        raise ValueError("benchmark has no calendar sessions")
    regime_slices = tag_event_regime_days(events, calendar, asof=asof)
    event_records = [event.to_record() for event in events]
    design = {
        "windows_sessions": {key: list(offsets) for key, offsets in WINDOWS.items()},
        "return_denominator": "close of session immediately before window start",
        "post_window_origin": "first full tradable session after verified availability",
        "liquidity_control_sessions": list(CONTROL_WINDOW),
        "stress_slice_sessions": list(STRESS_WINDOW),
        "liquidity_statistic": "mean daily observed value divided by fixed control mean",
        "market_control": benchmark_id,
        "excess_return": "stock_window_return_minus_benchmark_window_return",
        "cohorts": cohort_members,
    }
    experiment_key = hashlib.sha256(_json_bytes({
        "study_version": STUDY_VERSION, "events": event_records,
        "design": design, "asof": asof.isoformat(),
        "input_fingerprint_sha256": digest.hexdigest(),
    })).hexdigest()
    evaluations: list[dict[str, Any]] = []
    for event, metadata in zip(events, event_records):
        row: dict[str, Any] = {
            **metadata, "status": None, "reaction_date": None,
            "announcement_day_response": None, "benchmark": None,
            "stocks": {}, "cohorts": {},
            "fundamentals_status": "not_collected",
            "attribution_status": ("bundled_announcements_recorded"
                                   if event.co_announcements else "other_news_not_ruled_out"),
        }
        if (event.publication_certainty != "official_verified"
                or event.availability_confidence == "unverified"):
            row["status"] = "unverified_publication_time"
            evaluations.append(row)
            continue
        if event.announcement_day > asof:
            row["status"] = "not_available_asof"
            evaluations.append(row)
            continue
        announce_index = _announcement_day_measure(
            grouped[benchmark_id], calendar, event.announcement_day,
        )
        announcement_stocks = {
            key: _announcement_day_measure(grouped[key], calendar, event.announcement_day)
            for key in stock_ids
        }
        for key, item in announcement_stocks.items():
            item["excess_return"] = (
                item["return"] - announce_index["return"]
                if item["status"] == announce_index["status"] == "ready" else None
            )
        row["announcement_day_response"] = {
            "description": "observed close-to-close only; may precede the announcement and is not a tradable signal",
            "benchmark": announce_index, "stocks": announcement_stocks,
        }
        reaction_position = _first_full_session(event, calendar)
        if reaction_position >= len(calendar):
            row["status"] = "no_reaction_session_by_asof"
            evaluations.append(row)
            continue
        reaction_day = calendar[reaction_position]
        row["status"] = "ready"
        row["reaction_date"] = reaction_day.isoformat()
        benchmark_windows = {
            label: _window_measure(grouped[benchmark_id], calendar,
                                   reaction_position, offsets)
            for label, offsets in WINDOWS.items()
        }
        row["benchmark"] = {
            "instrument_id": benchmark_id, "price_basis": "index_points",
            "source_ids": sorted({bar.source_id for bar in grouped[benchmark_id].values()}),
            "windows": benchmark_windows,
        }
        for key in stock_ids:
            windows = {
                label: _window_measure(grouped[key], calendar, reaction_position, offsets)
                for label, offsets in WINDOWS.items()
            }
            for label, item in windows.items():
                benchmark_return = benchmark_windows[label]["return"]
                item["benchmark_return"] = benchmark_return
                item["excess_return"] = (
                    item["return"] - benchmark_return
                    if item["status"] == benchmark_windows[label]["status"] == "ready"
                    else None
                )
            row["stocks"][key] = {
                "instrument_id": key, "price_basis": identity[key][1],
                "price_unit": identity[key][2],
                "source_ids": sorted({bar.source_id for bar in grouped[key].values()}),
                "windows": windows,
            }
        row["cohorts"] = {
            label: _cohort_summary(row["stocks"], members)
            for label, members in cohort_members.items()
        }
        evaluations.append(row)
    return {
        "study_version": STUDY_VERSION,
        "canonical_contract_version": CONTRACT_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "experiment_key_sha256": experiment_key,
        "input_fingerprint_sha256": digest.hexdigest(),
        "input_bar_count": count,
        "data_asof": asof.isoformat(),
        "data_start": min(bar.trade_date for bar in prepared).isoformat(),
        "benchmark_calendar_start": calendar[0].isoformat(),
        "benchmark_calendar_end": calendar[-1].isoformat(),
        "benchmark_session_count": len(calendar),
        "stock_price_basis": next(iter(stock_bases)),
        "benchmark_price_basis": "index_points",
        "design": design,
        "regime_slices": regime_slices,
        "events": evaluations,
        "research_only": True,
        "point_in_time_market_data_validated": False,
        "causal_effect_estimated": False,
        "intrinsic_value_assessed": False,
        "confidence_intervals_status": "not_estimated_cross_sectionally_dependent_one_event",
        "limitations": [
            "事件日期与窗口必须在检查收益前预先登记；结果是相关的市场反应，不能单凭事件研究推断因果。",
            "现有行情数据未冻结历史当时可见版本；复权和股票池可能含事后信息，不能直接宣称可交易收益。",
            "公告日收益只描述已观察到的当日收盘变化；盘中或盘后公告时可能包含公告前价格波动。",
            "成交量和成交额只在单位核实且固定控制窗口完整时计算；不同来源或口径的量额不做跨标的水平比较。",
            "未收集带披露时间的财报事实，不能由价格反应推断企业内在价值是否变化。",
            "只有单一事件或同日相关股票时不提供朴素独立样本置信区间。",
            "政策应激切片是事后评估标签；公告前时段与未预告的未来事件不能预先识别或规避。",
        ],
    }


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _percent(value: object) -> str:
    return "—" if value is None else f"{float(value) * 100:+.2f}%"


def _ratio(value: object) -> str:
    return "—" if value is None else f"{float(value):.2f}×"


def render_event_study_markdown(result: Mapping[str, Any]) -> str:
    if result.get("study_version") != STUDY_VERSION:
        raise ValueError("unknown event study result version")
    lines = [
        "# A 股政策事件研究",
        "",
        f"数据截至：{result['data_asof']}；股票价格口径：{result['stock_price_basis']}；",
        f"对照指数：{result['design']['market_control']}（仅比较收益率）。",
        "",
        "固定窗口：公告前 20/5 个交易日、首个完整可交易日 0、累计至 +1/+5/+20 个交易日。",
        "成交量和成交额相对 -20 至 -6 日的日均值；当日收益只作描述，不代表可执行交易。",
        f"固定事件应激切片 -5 至 +20 日：标记 {result['regime_slices']['stress_session_count']} 日；"
        f"核心评估候选 {result['regime_slices']['core_session_count']} 日（只是事后切分）。",
        "",
    ]
    if "qlib_selection" in result:
        selected = result["qlib_selection"]
        release = result["qlib_release"]["release"]
        lines.extend([
            f"固定 Qlib 版本 {release['tag']}：分层选中 {selected['selected_count']} 只，"
            f"有有效收盘观测 {selected['observed_stock_count']} 只；"
            "只用 Qlib 复权 CLOSE，量额单位未核实，量比和额比均不计算。",
            "本报告样本是事后分层抽样，不代表全部 A 股，也不是当年已知股票池。",
            "",
            "| 政策事件 | 完整反应日 | 首日相对指数均值 | +5 日相对指数均值 | +20 日相对指数均值 |",
            "| --- | --- | ---: | ---: | ---: |",
        ])
        for event in result["events"]:
            if event["status"] != "ready":
                lines.append("| " + _markdown_cell(event["title"]) + " | — | — | — | — |")
                continue
            group = event["cohorts"]["all_selected_stocks"]
            cells = []
            for label in ("post_0", "post_5", "post_20"):
                item = group["windows"][label]
                cells.append(_percent(item["mean_excess_return"]) +
                             f"（{item['complete_count']}/{group['member_count']}）")
            lines.append("| " + _markdown_cell(event["title"]) + " | "
                         + str(event["reaction_date"]) + " | " + " | ".join(cells) + " |")
        lines.append("")
    for event in result["events"]:
        lines.extend([
            f"## {_markdown_cell(event['title'])}（{_markdown_cell(event['event_id'])}）",
            "",
            f"- 类别：{_markdown_cell(event['category'])}；公告：{_markdown_cell(event['announcement_at'])}（{event['time_precision']}）；实施：{event['effective_date'] or '未标明'}。",
            f"- 公告时点证据：{event['publication_certainty']} / {event['availability_confidence']}；幅度：{event['magnitude'] if event['magnitude'] is not None else '未量化'} {event['magnitude_unit'] or ''}。",
            f"- 来源：[原始公告]({event['source_url']})；首个完整可交易日：{event['reaction_date'] or '尚无'}；状态：{event['status']}。",
            "",
        ])
        if event["co_announcements"]:
            lines.append("同期一并发布：" + "；".join(
                _markdown_cell(value) for value in event["co_announcements"]
            ) + "。不能把价格变化单独归给本条措施。")
            lines.append("")
        observed = event["announcement_day_response"]
        if observed is not None:
            lines.append("公告日已观察到的指数收益：" + _percent(observed["benchmark"]["return"])
                         + "（可能早于公告，不是可交易收益）。")
            lines.append("")
        if event["status"] != "ready":
            continue
        lines.extend([
            "",
            "| 范围 | 窗口 | 收益 | 相对指数 | 量比 | 额比 | 数据/可交易状态 |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ])
        for label in WINDOWS:
            item = event["benchmark"]["windows"][label]
            lines.append("| 指数 | " + label + " | " + _percent(item["return"])
                         + " | — | " + _ratio(item["volume_ratio_to_control"])
                         + " | " + _ratio(item["amount_ratio_to_control"])
                         + " | " + item["status"] + " |")
        for stock_id, stock in sorted(event["stocks"].items()):
            for label in WINDOWS:
                item = stock["windows"][label]
                lines.append("| " + _markdown_cell(stock_id) + " | " + label
                             + " | " + _percent(item["return"])
                             + " | " + _percent(item["excess_return"])
                             + " | " + _ratio(item["volume_ratio_to_control"])
                             + " | " + _ratio(item["amount_ratio_to_control"])
                             + " | " + item["status"] + "/" + item["tradability"] + " |")
        lines.extend(["", "分组汇总（等权，完整样本数）：", ""])
        for label, cohort in event["cohorts"].items():
            item = cohort["windows"]["post_5"]
            lines.append(f"- {_markdown_cell(label)}：+5 日平均相对指数 "
                         f"{_percent(item['mean_excess_return'])}；"
                         f"{item['complete_count']}/{cohort['member_count']} 只完整。")
        lines.append("")
    lines.extend(["## 解读边界", ""])
    lines.extend("- " + item for item in result["limitations"])
    lines.append("")
    lines.append("输入指纹：`" + result["input_fingerprint_sha256"] + "`；实验键：`"
                 + result["experiment_key_sha256"] + "`。")
    lines.append("")
    return "\n".join(lines)


def append_event_study_record(directory: str | Path,
                              result: Mapping[str, Any]) -> dict[str, str]:
    """Create immutable, hash-checked JSON and human-readable Markdown files."""
    if result.get("study_version") != STUDY_VERSION:
        raise ValueError("unknown event study result version")
    payload = dict(result)
    report = render_event_study_markdown(payload).encode("utf-8")
    payload_sha = hashlib.sha256(_json_bytes(payload)).hexdigest()
    report_sha = hashlib.sha256(report).hexdigest()
    created = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    record_id = uuid4().hex
    record = {"schema_version": RECORD_SCHEMA_VERSION, "record_id": record_id,
              "created_at": created, "payload_sha256": payload_sha,
              "report_sha256": report_sha, "payload": payload}
    body = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")
    if len(body) > MAX_RECORD_BYTES:
        raise ValueError("event study record exceeds size limit")
    folder = Path(directory) / created[:10]
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    stem = created.replace("-", "").replace(":", "").replace(".", "").replace("+00:00", "Z") + "-" + record_id
    json_path, report_path = folder / (stem + ".json"), folder / (stem + ".md")
    created_paths: list[Path] = []
    temporary_paths: list[Path] = []
    try:
        for destination, data in ((report_path, report), (json_path, body)):
            temporary = folder / ("." + record_id + "." + secrets.token_hex(8) + ".tmp")
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
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
    return {"record_id": record_id, "payload_sha256": payload_sha,
            "report_sha256": report_sha, "json_path": str(json_path.resolve()),
            "report_path": str(report_path.resolve())}


def read_event_study_record(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    with source.open("rb") as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("event study record exceeds size limit")
    record = json.loads(raw)
    report_path = source.with_suffix(".md")
    if (not isinstance(record, dict)
            or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(record.get("payload"), dict)
            or record["payload"].get("study_version") != STUDY_VERSION
            or hashlib.sha256(_json_bytes(record["payload"])).hexdigest()
            != record.get("payload_sha256")
            or not report_path.is_file()
            or hashlib.sha256(report_path.read_bytes()).hexdigest()
            != record.get("report_sha256")):
        raise ValueError("event study record failed integrity check")
    return record
