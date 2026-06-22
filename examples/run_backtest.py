from __future__ import annotations

from pathlib import Path

from auto_invest.adapters.csv_market_data import CsvMarketData, load_instruments
from auto_invest.backtest.engine import BacktestConfig, BacktestEngine
from auto_invest.strategies.moving_average import MovingAverageCrossStrategy


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    instruments = load_instruments(str(ROOT / "configs" / "universe.example.json"))
    instrument_ids = sorted(instruments)
    strategy = MovingAverageCrossStrategy(
        instrument_ids=instrument_ids,
        fast_window=2,
        slow_window=3,
    )
    engine = BacktestEngine(
        market_data=CsvMarketData(str(ROOT / "data" / "sample" / "bars.csv")),
        instruments=instruments,
        config=BacktestConfig(),
    )
    result = engine.run(strategy, instrument_ids)
    print(result.metrics)


if __name__ == "__main__":
    main()
