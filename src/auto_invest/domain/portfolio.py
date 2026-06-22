from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Mapping

from auto_invest.domain.models import Side, Trade


@dataclass
class Position:
    instrument_id: str
    quantity: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")

    def market_value(self, price: Decimal) -> Decimal:
        return self.quantity * price


@dataclass
class Portfolio:
    cash: Decimal
    positions: Dict[str, Position] = field(default_factory=dict)

    def quantity(self, instrument_id: str) -> Decimal:
        position = self.positions.get(instrument_id)
        if position is None:
            return Decimal("0")
        return position.quantity

    def equity(self, prices: Mapping[str, Decimal]) -> Decimal:
        total = self.cash
        for instrument_id, position in self.positions.items():
            price = prices.get(instrument_id)
            if price is not None:
                total += position.market_value(price)
        return total

    def apply_trade(self, trade: Trade) -> None:
        if trade.quantity <= 0:
            raise ValueError("trade quantity must be positive")

        position = self.positions.get(
            trade.instrument_id,
            Position(instrument_id=trade.instrument_id),
        )

        if trade.side == Side.BUY:
            new_quantity = position.quantity + trade.quantity
            if new_quantity <= 0:
                raise ValueError("buy trade produced invalid position")
            weighted_cost = (
                position.quantity * position.avg_price + trade.quantity * trade.price
            )
            position.quantity = new_quantity
            position.avg_price = weighted_cost / new_quantity
        else:
            if trade.quantity > position.quantity:
                raise ValueError("sell quantity cannot exceed current position")
            position.quantity -= trade.quantity
            if position.quantity == 0:
                position.avg_price = Decimal("0")

        self.cash += trade.cash_delta

        if position.quantity == 0:
            self.positions.pop(trade.instrument_id, None)
        else:
            self.positions[trade.instrument_id] = position
