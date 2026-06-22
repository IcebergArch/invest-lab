from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from itertools import groupby
from typing import Dict, List, Mapping, Optional, Sequence

from auto_invest.backtest.reports import calculate_metrics
from auto_invest.domain.models import Instrument, Side, TargetWeight, Trade, as_decimal
from auto_invest.domain.portfolio import Portfolio
from auto_invest.ports.market_data import MarketDataPort
from auto_invest.strategies.base import BarHistory, MarketSnapshot, Strategy


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: Decimal = Decimal("100000")
    commission_rate: Decimal = Decimal("0.0003")
    slippage_bps: Decimal = Decimal("1")
    max_total_weight: Decimal = Decimal("1")


@dataclass
class BacktestResult:
    portfolio: Portfolio
    equity_curve: List[Mapping[str, Decimal]]
    trades: List[Trade]
    metrics: Mapping[str, Decimal] = field(default_factory=dict)


class BacktestEngine:
    def __init__(
        self,
        market_data: MarketDataPort,
        instruments: Mapping[str, Instrument],
        config: Optional[BacktestConfig] = None,
    ):
        self.market_data = market_data
        self.instruments = dict(instruments)
        self.config = config or BacktestConfig()

    def run(
        self,
        strategy: Strategy,
        instrument_ids: Sequence[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> BacktestResult:
        unknown = sorted(set(instrument_ids) - set(self.instruments))
        if unknown:
            raise ValueError(f"unknown instruments: {unknown}")

        bars = list(self.market_data.list_bars(instrument_ids, start=start, end=end))
        history = BarHistory()
        portfolio = Portfolio(cash=as_decimal(self.config.initial_cash))
        trades: List[Trade] = []
        equity_curve: List[Mapping[str, Decimal]] = []

        for timestamp, grouped in groupby(bars, key=lambda bar: bar.timestamp):
            current_bars = {bar.instrument_id: bar for bar in grouped}
            for bar in current_bars.values():
                history.append(bar)

            prices = history.latest_prices()
            snapshot = MarketSnapshot(
                timestamp=timestamp,
                bars=current_bars,
                history=history,
                prices=prices,
            )
            targets = list(strategy.on_bar(snapshot, portfolio))
            trades.extend(self._rebalance(portfolio, timestamp, targets, prices))
            equity_curve.append(
                {
                    "timestamp": timestamp,
                    "cash": portfolio.cash,
                    "equity": portfolio.equity(prices),
                }
            )

        metrics = dict(calculate_metrics(equity_curve, self.config.initial_cash))
        metrics["trade_count"] = Decimal(len(trades))
        return BacktestResult(
            portfolio=portfolio,
            equity_curve=equity_curve,
            trades=trades,
            metrics=metrics,
        )

    def _rebalance(
        self,
        portfolio: Portfolio,
        timestamp: datetime,
        targets: Sequence[TargetWeight],
        prices: Mapping[str, Decimal],
    ) -> List[Trade]:
        target_map = {target.instrument_id: as_decimal(target.weight) for target in targets}
        if sum(target_map.values(), Decimal("0")) > self.config.max_total_weight:
            raise ValueError("target weights exceed max_total_weight")

        instrument_ids = sorted(set(portfolio.positions) | set(target_map))
        equity = portfolio.equity(prices)
        planned = []
        for instrument_id in instrument_ids:
            price = prices.get(instrument_id)
            if price is None or price <= 0:
                continue
            current_value = portfolio.quantity(instrument_id) * price
            target_value = equity * target_map.get(instrument_id, Decimal("0"))
            delta_value = target_value - current_value
            if delta_value == 0:
                continue
            planned.append((instrument_id, price, delta_value))

        sell_first = sorted(planned, key=lambda item: item[2])
        trades: List[Trade] = []
        for instrument_id, price, delta_value in sell_first:
            trade = self._build_trade(portfolio, timestamp, instrument_id, price, delta_value)
            if trade is None:
                continue
            portfolio.apply_trade(trade)
            trades.append(trade)

        return trades

    def _build_trade(
        self,
        portfolio: Portfolio,
        timestamp: datetime,
        instrument_id: str,
        mark_price: Decimal,
        delta_value: Decimal,
    ) -> Optional[Trade]:
        instrument = self.instruments[instrument_id]
        side = Side.BUY if delta_value > 0 else Side.SELL
        execution_price = self._execution_price(mark_price, side)
        raw_quantity = abs(delta_value) / execution_price

        if side == Side.SELL:
            raw_quantity = min(raw_quantity, portfolio.quantity(instrument_id))

        quantity = self._round_down(raw_quantity, instrument.quantity_step)
        if quantity < instrument.min_quantity:
            return None

        if side == Side.BUY:
            quantity = self._cap_by_cash(portfolio.cash, quantity, execution_price, instrument.quantity_step)
            if quantity < instrument.min_quantity:
                return None

        commission = quantity * execution_price * self.config.commission_rate
        return Trade(
            timestamp=timestamp,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            price=execution_price,
            commission=commission,
        )

    def _cap_by_cash(
        self,
        cash: Decimal,
        quantity: Decimal,
        execution_price: Decimal,
        quantity_step: Decimal,
    ) -> Decimal:
        total_cost = quantity * execution_price * (Decimal("1") + self.config.commission_rate)
        if total_cost <= cash:
            return quantity
        affordable = cash / (execution_price * (Decimal("1") + self.config.commission_rate))
        return self._round_down(affordable, quantity_step)

    def _execution_price(self, mark_price: Decimal, side: Side) -> Decimal:
        slippage = self.config.slippage_bps / Decimal("10000")
        if side == Side.BUY:
            return mark_price * (Decimal("1") + slippage)
        return mark_price * (Decimal("1") - slippage)

    def _round_down(self, quantity: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return quantity
        units = (quantity / step).to_integral_value(rounding=ROUND_FLOOR)
        return units * step
