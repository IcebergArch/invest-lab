"""Separate, research-only daily OHLCV factor candidates.

These functions consume *final* China A-share daily bars. ``available_at`` must
be an evidenced first-availability time, not a historical download timestamp.
No factor in this module is approved for a decision policy by registration alone.
Order-flow, queue and intraday realized-volatility factors need intraday data
and deliberately have no daily-bar calculator.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from statistics import mean, median
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

FACTOR_SET_VERSION = "daily-ohlcv-v2"
_CHINA = ZoneInfo("Asia/Shanghai")
_PRICE_BASES = frozenset(("raw_unadjusted", "forward_adjusted"))
_AMOUNT_UNITS = frozenset(("CNY", "unavailable", "source_native", "ineligible"))


@dataclass(frozen=True)
class FactorBar:
    instrument_id: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume_shares: float | None
    amount_cny: float | None
    price_basis: str
    amount_unit: str
    available_at: datetime | None
    source_id: str


@dataclass(frozen=True)
class FactorDefinitionV2:
    factor_id: str
    version: str
    frequency: str
    required_fields: tuple[str, ...]
    availability_status: str
    research_status: str
    source: str
    output_unit: str
    description: str
    calculator: Callable[[Sequence[FactorBar], int], float | None] | None


def _positive(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return number


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value


def _validate_bars(bars: Sequence[FactorBar], decision_at: datetime) -> None:
    decision = _aware(decision_at, "decision_at")
    if not bars:
        raise ValueError("factor bars cannot be empty")
    instrument_id = bars[0].instrument_id
    price_basis = bars[0].price_basis
    if not instrument_id or price_basis not in _PRICE_BASES:
        raise ValueError("invalid instrument or daily price basis")
    previous_date: date | None = None
    for bar in bars:
        if (bar.instrument_id != instrument_id or bar.price_basis != price_basis
                or not bar.source_id or type(bar.trade_date) is not date):
            raise ValueError("mixed instrument, price basis or missing bar lineage")
        if previous_date is not None and bar.trade_date <= previous_date:
            raise ValueError("factor bars must have strictly increasing trade dates")
        previous_date = bar.trade_date
        opening = _positive(bar.open, "open")
        high = _positive(bar.high, "high")
        low = _positive(bar.low, "low")
        closing = _positive(bar.close, "close")
        if low > min(opening, closing) or high < max(opening, closing) or low > high:
            raise ValueError("invalid daily OHLC range")
        _positive(bar.volume_shares, "volume_shares")
        if bar.amount_unit not in _AMOUNT_UNITS:
            raise ValueError("unknown amount unit")
        if bar.amount_unit == "CNY":
            _positive(bar.amount_cny, "amount_cny")
        elif bar.amount_cny is not None:
            raise ValueError("amount_cny requires verified CNY units")
        available = _aware(bar.available_at, "available_at")
        session_end = datetime.combine(bar.trade_date, time(15, 0), _CHINA)
        if available < session_end:
            raise ValueError("final daily bar cannot be available before session close")
        if available > decision:
            raise ValueError("factor bar was not available at decision time")


def _require_amount(bars: Sequence[FactorBar]) -> list[float]:
    if any(bar.amount_unit != "CNY" or bar.amount_cny is None for bar in bars):
        raise ValueError("factor requires positive CNY amount for every required bar")
    return [_positive(bar.amount_cny, "amount_cny") for bar in bars]


def _overnight_gap(bars: Sequence[FactorBar], _window: int) -> float | None:
    if len(bars) < 2:
        return None
    return bars[-1].open / bars[-2].close - 1.0


def _intraday_return(bars: Sequence[FactorBar], _window: int) -> float:
    return bars[-1].close / bars[-1].open - 1.0


def _rolling_range_volatility(bars: Sequence[FactorBar], window: int) -> float | None:
    if window < 2:
        raise ValueError("rolling_range_volatility requires window >= 2")
    if len(bars) < window:
        return None
    ranges = [math.log(bar.high / bar.low) ** 2 for bar in bars[-window:]]
    return math.sqrt(mean(ranges) / (4 * math.log(2)))


def _amihud_illiquidity(bars: Sequence[FactorBar], window: int) -> float | None:
    if len(bars) < window + 1:
        return None
    tail = bars[-window - 1:]
    amounts = _require_amount(tail[1:])
    return mean(abs(tail[index].close / tail[index - 1].close - 1.0) / amounts[index - 1]
                for index in range(1, len(tail)))


def _relative_volume(bars: Sequence[FactorBar], window: int) -> float | None:
    if len(bars) < window + 1:
        return None
    volumes = [_positive(bar.volume_shares, "volume_shares")
               for bar in bars[-window - 1:]]
    return volumes[-1] / median(volumes[:-1])


def _relative_amount(bars: Sequence[FactorBar], window: int) -> float | None:
    if len(bars) < window + 1:
        return None
    amounts = _require_amount(bars[-window - 1:])
    return amounts[-1] / mean(amounts[:-1])


_DEFINITIONS = (
    FactorDefinitionV2("overnight_gap", FACTOR_SET_VERSION, "daily",
                       ("open", "previous_close"), "implemented", "research_unvalidated",
                       "daily_ohlcv", "return_fraction",
                       "Final daily open divided by the prior close, minus one.", _overnight_gap),
    FactorDefinitionV2("intraday_return", FACTOR_SET_VERSION, "daily",
                       ("open", "close"), "implemented", "research_unvalidated",
                       "daily_ohlcv", "return_fraction",
                       "Final daily close divided by the same-day open, minus one.", _intraday_return),
    FactorDefinitionV2("rolling_range_volatility", FACTOR_SET_VERSION, "daily",
                       ("high", "low"), "implemented", "research_unvalidated",
                       "daily_ohlcv", "per_session_volatility_fraction",
                       "Parkinson range estimator from daily highs and lows; not intraday RV.",
                       _rolling_range_volatility),
    FactorDefinitionV2("amihud_illiquidity", FACTOR_SET_VERSION, "daily",
                       ("close", "amount_cny"), "implemented", "research_unvalidated",
                       "daily_ohlcv_with_verified_cny_amount", "inverse_CNY",
                       "Mean absolute close-to-close return divided by same-day CNY amount.",
                       _amihud_illiquidity),
    FactorDefinitionV2("relative_volume", FACTOR_SET_VERSION, "daily",
                       ("volume_shares",), "implemented", "research_unvalidated",
                       "daily_ohlcv_with_verified_share_units", "ratio",
                       "Current share volume divided by prior window median share volume.",
                       _relative_volume),
    FactorDefinitionV2("relative_amount", FACTOR_SET_VERSION, "daily",
                       ("amount_cny",), "implemented", "research_unvalidated",
                       "daily_ohlcv_with_verified_cny_amount", "ratio",
                       "Current CNY amount divided by the prior window's mean amount.",
                       _relative_amount),
    FactorDefinitionV2("ofi", FACTOR_SET_VERSION, "order_event",
                       ("best_bid_size", "best_ask_size", "quote_event_time"),
                       "data_required", "not_evaluated", "trade_quote_feed", "shares",
                       "Order-flow imbalance requires timestamped quote changes.", None),
    FactorDefinitionV2("queue_imbalance", FACTOR_SET_VERSION, "order_book",
                       ("bid_depth", "ask_depth", "book_event_time"),
                       "data_required", "not_evaluated", "level2_order_book", "ratio",
                       "Queue imbalance requires a timestamped order-book snapshot.", None),
    FactorDefinitionV2("intraday_realized_volatility", FACTOR_SET_VERSION, "intraday",
                       ("intraday_prices", "bar_end_times"),
                       "data_required", "not_evaluated", "intraday_trade_or_bar_feed",
                       "volatility_fraction",
                       "Intraday realized volatility requires intraday returns.", None),
)
_INDEX = {item.factor_id: item for item in _DEFINITIONS}
_RESEARCH_REFERENCES = {
    "rolling_range_volatility": "https://doi.org/10.1086/296071",
    "amihud_illiquidity": "https://doi.org/10.1016/S1386-4181(01)00024-6",
    "ofi": "https://arxiv.org/abs/1011.6402",
    "queue_imbalance": "https://arxiv.org/abs/1512.03492",
    "intraday_realized_volatility": "https://doi.org/10.1111/1468-0262.00418",
}
_MINIMUM_HISTORY = {
    "overnight_gap": "2 daily bars",
    "intraday_return": "1 daily bar",
    "rolling_range_volatility": "window daily bars",
    "relative_volume": "window + 1 daily bars",
    "amihud_illiquidity": "window + 1 daily bars",
    "relative_amount": "window + 1 daily bars",
    "ofi": "2 ordered quote events",
    "queue_imbalance": "1 valid order-book snapshot",
    "intraday_realized_volatility": "2 ordered intraday prices",
}


def get_factor_definition_v2(factor_id: str) -> FactorDefinitionV2:
    try:
        return _INDEX[factor_id]
    except KeyError as exc:
        raise ValueError(f"unknown v2 factor: {factor_id}") from exc


def factor_catalog_v2() -> list[dict[str, object]]:
    return [{
        "factor_id": item.factor_id,
        "version": item.version,
        "frequency": item.frequency,
        "required_fields": list(item.required_fields),
        "availability_status": item.availability_status,
        "research_status": item.research_status,
        "source": item.source,
        "output_unit": item.output_unit,
        "description": item.description,
        "active_for_decision": False,
        "minimum_history": _MINIMUM_HISTORY[item.factor_id],
        "research_reference": _RESEARCH_REFERENCES.get(item.factor_id),
    } for item in _DEFINITIONS]


def calculate_factor(factor_id: str, bars: Sequence[FactorBar], *,
                     decision_at: datetime, window: int = 20) -> float | None:
    """Calculate one candidate only from final daily bars known by decision time.

    ``None`` means insufficient lookback. Missing/invalid data raise instead of
    silently becoming zero, and registration never permits policy activation.
    """
    definition = get_factor_definition_v2(factor_id)
    if definition.calculator is None:
        raise ValueError(f"{factor_id}: intraday data required; no daily calculator")
    if type(window) is not int or window < 1:
        raise ValueError("window must be a positive integer")
    _validate_bars(bars, decision_at)
    return definition.calculator(bars, window)
