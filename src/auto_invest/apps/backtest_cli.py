from __future__ import annotations

import argparse
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from auto_invest.adapters.csv_market_data import CsvMarketData, load_instruments
from auto_invest.backtest.engine import BacktestConfig, BacktestEngine
from auto_invest.domain.models import Instrument
from auto_invest.strategies.buy_and_hold import BuyAndHoldStrategy
from auto_invest.strategies.moving_average import MovingAverageCrossStrategy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local multi-market backtest.")
    parser.add_argument("--bars", required=True, help="CSV bars file.")
    parser.add_argument("--instruments", required=True, help="JSON instrument universe.")
    parser.add_argument(
        "--instrument-id",
        action="append",
        dest="instrument_ids",
        help="Instrument id to include. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--strategy",
        choices=["ma-cross", "buy-and-hold"],
        default="ma-cross",
    )
    parser.add_argument("--fast-window", type=int, default=3)
    parser.add_argument("--slow-window", type=int, default=5)
    parser.add_argument("--initial-cash", default="100000")
    parser.add_argument("--commission-rate", default="0.0003")
    parser.add_argument("--slippage-bps", default="1")
    return parser


def build_strategy(
    name: str,
    instrument_ids: Sequence[str],
    fast_window: int,
    slow_window: int,
):
    if name == "buy-and-hold":
        weight = Decimal("1") / Decimal(len(instrument_ids))
        return BuyAndHoldStrategy({instrument_id: weight for instrument_id in instrument_ids})
    return MovingAverageCrossStrategy(
        instrument_ids=instrument_ids,
        fast_window=fast_window,
        slow_window=slow_window,
    )


def select_instruments(
    instruments: Mapping[str, Instrument],
    requested: Sequence[str],
) -> Sequence[str]:
    if requested:
        missing = sorted(set(requested) - set(instruments))
        if missing:
            raise SystemExit(f"Unknown instrument ids: {missing}")
        return list(requested)
    return sorted(instruments)


def format_decimal(value: Decimal) -> str:
    return f"{value:.6f}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    instruments = load_instruments(args.instruments)
    instrument_ids = select_instruments(instruments, args.instrument_ids or [])
    strategy = build_strategy(
        args.strategy,
        instrument_ids,
        args.fast_window,
        args.slow_window,
    )

    engine = BacktestEngine(
        market_data=CsvMarketData(args.bars),
        instruments=instruments,
        config=BacktestConfig(
            initial_cash=Decimal(args.initial_cash),
            commission_rate=Decimal(args.commission_rate),
            slippage_bps=Decimal(args.slippage_bps),
        ),
    )
    result = engine.run(strategy, instrument_ids)

    print(f"strategy: {strategy.name}")
    print(f"instruments: {', '.join(instrument_ids)}")
    for key, value in result.metrics.items():
        print(f"{key}: {format_decimal(value)}")
    print(f"cash: {format_decimal(result.portfolio.cash)}")
    print("positions:")
    for instrument_id, position in sorted(result.portfolio.positions.items()):
        print(
            f"  {instrument_id} quantity={format_decimal(position.quantity)} "
            f"avg_price={format_decimal(position.avg_price)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
