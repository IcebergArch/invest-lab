from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.industry_validation import GROUP_NAMES, validate_industry_screen
from quant_lab.market import GROUPS
from quant_lab.models import DailyBar, Instrument
from quant_lab.storage import MarketStore


class IndustryValidationTest(unittest.TestCase):
    def test_future_winner_does_not_change_origin_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            codes = [code for name in GROUP_NAMES for code in GROUPS[name]]
            store.upsert_instruments([
                Instrument(f"industry:{code}.SW", f"{code}.SW", code,
                           "industry_index", "sw_level_1", "sw_research", code)
                for code in codes
            ])
            start = date(2026, 1, 1)
            bars = []
            for index in range(31):
                day = start + timedelta(days=index)
                for name in GROUP_NAMES:
                    for code in GROUPS[name]:
                        close = (100 + index if index <= 20 else 120 - 2 * (index - 20)) \
                            if name == "资源能源" else (100 + 3 * max(0, index - 20)) \
                            if name == "消费健康" else 100
                        bars.append(DailyBar(f"industry:{code}.SW", day, close, close,
                                             close, close, 1000, 100_000,
                                             "source_native", "source_native", "none",
                                             "sw_research", f"day-{index}"))
            store.upsert_bars(bars)
            result = validate_industry_screen(store, start + timedelta(days=25))
            self.assertEqual(1, result["origin_count"])
            point = result["points"][0]
            self.assertEqual(["资源能源"], point["selected_groups"])
            self.assertLess(point["selected_mean_future_5d"], 0)
            self.assertGreater(point["five_group_equal_weight_future_5d"], 0)
            self.assertEqual((start + timedelta(days=25)).isoformat(), point["target"])


if __name__ == "__main__":
    unittest.main()
