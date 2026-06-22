from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class Market(Enum):
    CHINA_A = "CN_A"
    HONG_KONG = "HK"
    US = "US"
    CRYPTO = "CRYPTO"


class AssetType(Enum):
    STOCK = "STOCK"
    ETF = "ETF"
    CRYPTO = "CRYPTO"


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class MarketRule:
    market: Market
    currency: str
    timezone: str
    quantity_step: Decimal
    min_quantity: Decimal
    price_tick: Decimal
    always_open: bool = False


MARKET_RULES = {
    Market.CHINA_A: MarketRule(
        market=Market.CHINA_A,
        currency="CNY",
        timezone="Asia/Shanghai",
        quantity_step=Decimal("100"),
        min_quantity=Decimal("100"),
        price_tick=Decimal("0.01"),
    ),
    Market.HONG_KONG: MarketRule(
        market=Market.HONG_KONG,
        currency="HKD",
        timezone="Asia/Hong_Kong",
        quantity_step=Decimal("100"),
        min_quantity=Decimal("100"),
        price_tick=Decimal("0.01"),
    ),
    Market.US: MarketRule(
        market=Market.US,
        currency="USD",
        timezone="America/New_York",
        quantity_step=Decimal("1"),
        min_quantity=Decimal("1"),
        price_tick=Decimal("0.01"),
    ),
    Market.CRYPTO: MarketRule(
        market=Market.CRYPTO,
        currency="USD",
        timezone="UTC",
        quantity_step=Decimal("0.0001"),
        min_quantity=Decimal("0.0001"),
        price_tick=Decimal("0.01"),
        always_open=True,
    ),
}


def default_market_rule(market: Market) -> MarketRule:
    return MARKET_RULES[market]


def as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    symbol: str
    market: Market
    asset_type: AssetType
    currency: str
    name: str
    quantity_step: Decimal
    min_quantity: Decimal
    price_tick: Decimal
    timezone: str

    @classmethod
    def create(
        cls,
        symbol: str,
        market: Market,
        asset_type: AssetType,
        name: str = "",
        currency: str = "",
        quantity_step: Decimal = Decimal("0"),
        min_quantity: Decimal = Decimal("0"),
        price_tick: Decimal = Decimal("0"),
        timezone: str = "",
    ) -> "Instrument":
        rule = default_market_rule(market)
        return cls(
            instrument_id=f"{market.value}:{symbol}",
            symbol=symbol,
            market=market,
            asset_type=asset_type,
            currency=currency or rule.currency,
            name=name or symbol,
            quantity_step=quantity_step or rule.quantity_step,
            min_quantity=min_quantity or rule.min_quantity,
            price_tick=price_tick or rule.price_tick,
            timezone=timezone or rule.timezone,
        )


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    instrument_id: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ValueError("bar low cannot be greater than high")
        if not (self.low <= self.open <= self.high):
            raise ValueError("bar open must be between low and high")
        if not (self.low <= self.close <= self.high):
            raise ValueError("bar close must be between low and high")


@dataclass(frozen=True)
class TargetWeight:
    instrument_id: str
    weight: Decimal

    def __post_init__(self) -> None:
        if self.weight < Decimal("0"):
            raise ValueError("target weight cannot be negative in long-only mode")


@dataclass(frozen=True)
class Trade:
    timestamp: datetime
    instrument_id: str
    side: Side
    quantity: Decimal
    price: Decimal
    commission: Decimal

    @property
    def gross_value(self) -> Decimal:
        return self.quantity * self.price

    @property
    def cash_delta(self) -> Decimal:
        if self.side == Side.BUY:
            return -(self.gross_value + self.commission)
        return self.gross_value - self.commission

    @property
    def signed_quantity(self) -> Decimal:
        if self.side == Side.BUY:
            return self.quantity
        return -self.quantity
