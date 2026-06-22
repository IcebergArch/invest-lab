from __future__ import annotations

import unittest
from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[1]
path.insert(0, str(ROOT / "src"))

from auto_invest.adapters.csv_market_data import CsvMarketData, load_instruments
from auto_invest.domain.models import Market


class MarketDataTest(unittest.TestCase):
    def test_loads_multi_market_instruments(self) -> None:
        instruments = load_instruments(str(ROOT / "configs" / "universe.example.json"))

        self.assertEqual(instruments["CN_A:000001.SZ"].market, Market.CHINA_A)
        self.assertEqual(instruments["HK:00700.HK"].currency, "HKD")
        self.assertEqual(instruments["US:AAPL"].quantity_step.as_tuple().exponent, 0)
        self.assertEqual(str(instruments["CRYPTO:BTC-USD"].quantity_step), "0.0001")

    def test_csv_bars_are_sorted_and_filterable(self) -> None:
        source = CsvMarketData(str(ROOT / "data" / "sample" / "bars.csv"))

        bars = list(source.list_bars(["US:AAPL"]))

        self.assertEqual(len(bars), 6)
        self.assertEqual({bar.instrument_id for bar in bars}, {"US:AAPL"})
        self.assertEqual(bars, sorted(bars, key=lambda bar: (bar.timestamp, bar.instrument_id)))


if __name__ == "__main__":
    unittest.main()
