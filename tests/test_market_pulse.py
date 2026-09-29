from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.market import BROAD, GROUPS, build_market_pulse
from quant_lab.models import DailyBar, Instrument
from quant_lab.storage import MarketStore
from quant_lab.workflow import (record_decision, review_decisions, save_candidates,
                                save_stock_candidates, screen_groups)


class MarketPulseTest(unittest.TestCase):
    def test_stock_candidate_decision_reaches_review_with_future_prices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            instrument_id = "stock:600000.SH"
            store.upsert_instruments([
                Instrument(instrument_id, "600000.SH", "试验股票", "stock", "focus_stock",
                           "test_source", "1.600000", "SSE", "qfq")
            ])
            start = date(2026, 1, 1)
            store.upsert_bars([
                DailyBar(instrument_id, start + timedelta(days=offset), 100 + offset,
                         101 + offset, 99 + offset, 100 + offset, 1000, 100_000,
                         "share", "CNY", "qfq", "test_source", f"bar-{offset}")
                for offset in range(21)
            ])
            screen = {
                "status": "ready", "screen_version": "test-stock-v1",
                "candidates": [{
                    "instrument_id": instrument_id, "name": "试验股票", "rank": 1,
                    "asof": start.isoformat(), "status": "待人工复核",
                }],
            }
            candidates = save_stock_candidates(store, screen, "stock-run")
            self.assertEqual(1, len(candidates))
            record_decision(store, candidates[0]["candidate_id"], "watch", "继续观察")
            report = review_decisions(store)
            self.assertEqual(1, report["matured_20d_count"])
            self.assertEqual("stock", report["items"][0]["subject_type"])
            self.assertAlmostEqual(0.05, report["items"][0]["return_5d"])
            self.assertAlmostEqual(0.20, report["items"][0]["return_20d"])
            self.assertEqual(1, report["by_type"]["stock"]["watch"]["samples"])

    def test_amount_share_is_attention_not_net_flow_and_asof_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            codes = [code for members in GROUPS.values() for code in members]
            self.assertEqual(31, len(codes))
            self.assertEqual(31, len(set(codes)))
            instruments = [Instrument(f"industry:{code}.SW", f"{code}.SW", code,
                                      "industry_index", "sw_level_1", "sw_research", code)
                           for code in codes]
            instruments += [Instrument(key, key.split(":")[1], name, "index", "csi",
                                       "tencent_kline", key)
                            for key, name in BROAD.items()]
            store.upsert_instruments(instruments)
            bars = []
            for offset in range(22):
                day = date(2026, 1, 1) + timedelta(days=offset)
                for item in instruments:
                    industry = item.asset_type == "industry_index"
                    amount = 200.0 if item.provider_code == "801010" and offset == 20 else 100.0
                    bars.append(DailyBar(item.instrument_id, day, 100.0, 123.0, 99.0,
                                         100.0 + offset, 100.0,
                                         amount if industry else None,
                                         "source_native" if industry else "lot",
                                         "source_native" if industry else "unavailable",
                                         "none", item.source_id, str(offset)))
            store.upsert_bars(bars)
            early = build_market_pulse(store, date(2026, 1, 21))
            self.assertEqual("2026-01-21", early["asof"])
            self.assertEqual(False, early["net_flow_available"])
            agriculture = next(row for row in early["industries"] if row["code"] == "801010")
            self.assertGreater(agriculture["share_change_pp"], 0)
            self.assertAlmostEqual(2.0, agriculture["activity_ratio"])
            self.assertAlmostEqual(1.0, sum(row["amount_share"] for row in early["groups"]))
            rerun = build_market_pulse(store, date(2026, 1, 21))
            self.assertEqual(early["groups"], rerun["groups"])
            candidates = screen_groups(early, "test-run")
            self.assertTrue(candidates)
            save_candidates(store, candidates)
            decision_id = record_decision(store, candidates[0]["candidate_id"], "watch", "先观察")
            self.assertEqual(decision_id, record_decision(
                store, candidates[0]["candidate_id"], "skip", "不跟踪"))
            review = review_decisions(store)
            self.assertEqual(1, review["decision_count"])
            self.assertEqual("skip", review["items"][0]["choice"])
            self.assertEqual(0, review["matured_20d_count"])


if __name__ == "__main__":
    unittest.main()
