from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.models import DailyBar
from quant_lab.stock_universe import (
    EastMoneyStockUniverseSource,
    parse_stock_listing,
    read_stock_snapshot,
    sync_stock_pilot,
    write_stock_snapshot,
)
from quant_lab.storage import MarketStore


def response(total, *rows):
    return {"rc": 0, "data": {"total": total, "diff": list(rows)}}


SH = {"f12": "600000", "f13": 1, "f14": "浦发银行", "f6": 123456, "f20": 123456789}
SZ = {"f12": "002475", "f13": 0, "f14": "立讯精密", "f6": 567890, "f21": 7654321}
BJ = {"f12": "920779", "f13": 0, "f14": "武汉蓝电"}


class FakeHttp:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        if not self.pages:
            raise AssertionError("unexpected request")
        return self.pages.pop(0), f"page-hash-{len(self.calls)}"


class FakeDailySource:
    def __init__(self):
        self.calls = []

    def fetch(self, instrument, start, end):
        self.calls.append(instrument)
        return [DailyBar(instrument.instrument_id, date(2026, 9, 28),
                         10.0, 11.0, 9.0, 10.5, 100.0, 1000.0,
                         "lot", "CNY", "qfq", "eastmoney_kline", "test-hash")]


class StockUniverseTest(unittest.TestCase):
    def test_all_three_venues_and_optional_quote_fields(self):
        self.assertEqual("600000.SH", parse_stock_listing(SH).instrument.symbol)
        self.assertEqual("002475.SZ", parse_stock_listing(SZ).instrument.symbol)
        self.assertEqual("920779.BJ", parse_stock_listing(BJ).instrument.symbol)
        self.assertEqual("focus_stock", parse_stock_listing(SZ).instrument.family)
        self.assertEqual(123456, parse_stock_listing(SH).provider_f6)
        with self.assertRaisesRegex(ValueError, "unexpected stock market"):
            parse_stock_listing({"f12": "900901", "f13": 1, "f14": "B share"})

    def test_complete_pages_and_bookend_produce_snapshot(self):
        http = FakeHttp([response(3, SH, SZ), response(3, BJ), response(3, SH, SZ)])
        snapshot = EastMoneyStockUniverseSource(http).list_current(
            page_size=2, minimum_total=0
        )
        self.assertEqual(3, snapshot.expected_total)
        self.assertEqual(3, len(snapshot.listings))
        self.assertEqual([1, 2, 1], [int(call[1]["pn"]) for call in http.calls])
        self.assertEqual("f12,f13,f14,f6,f20,f21", http.calls[0][1]["fields"])
        with tempfile.TemporaryDirectory() as directory:
            path = write_stock_snapshot(snapshot, Path(directory) / "stocks.json")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot.snapshot_id, saved["snapshot_id"])
            self.assertEqual(3, saved["retrieved_total"])
            self.assertFalse(saved["historical_point_in_time"])
            self.assertEqual(snapshot, read_stock_snapshot(path))
            saved["listings"][0]["name"] = "被改写"
            path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ID mismatch"):
                read_stock_snapshot(path)

    def test_missing_duplicate_or_changed_page_fails_closed(self):
        cases = [
            [response(3, SH), response(3, BJ)],  # page 1 short
            [response(3, SH, SZ), response(3, SH)],  # duplicate page 2
            [response(3, SH, SZ), response(3, BJ), response(3, BJ, SZ)],
        ]
        for pages in cases:
            with self.subTest(pages=pages):
                with self.assertRaises(ValueError):
                    EastMoneyStockUniverseSource(FakeHttp(pages)).list_current(
                        page_size=2, minimum_total=0
                    )

    def test_pilot_is_explicitly_bounded_and_provenanced(self):
        snapshot = EastMoneyStockUniverseSource(
            FakeHttp([response(3, SH, SZ, BJ)])
        ).list_current(page_size=3, minimum_total=0)
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            with self.assertRaisesRegex(ValueError, "select 1..1"):
                sync_stock_pilot(store, snapshot, ["stock:600000.SH", "stock:002475.SZ"],
                                 date(2026, 9, 1), date(2026, 9, 29), max_instruments=1)
            source = FakeDailySource()
            bj_source = FakeDailySource()
            result = sync_stock_pilot(
                store, snapshot, ["stock:002475.SZ", "stock:920779.BJ"],
                date(2026, 9, 1), date(2026, 9, 29),
                market_source=source, bj_source=bj_source,
            )
            self.assertEqual(2, result.row_count)
            self.assertEqual(1, len(source.calls))
            self.assertEqual(1, len(bj_source.calls))
            self.assertEqual("focus_stock", store.instrument_summary()[1]["family"])
            with store.connect() as connection:
                run = connection.execute(
                    "SELECT status, groups_json, row_count FROM sync_runs WHERE run_id=?",
                    (result.run_id,),
                ).fetchone()
            self.assertEqual("success", run["status"])
            self.assertIn(result.snapshot_id, run["groups_json"])
            self.assertEqual(2, run["row_count"])

    def test_pilot_does_not_accept_price_only_fallback_as_liquidity_data(self):
        snapshot = EastMoneyStockUniverseSource(
            FakeHttp([response(1, SH)])
        ).list_current(page_size=1, minimum_total=0)

        class PriceOnlySource:
            def fetch(self, instrument, start, end):
                return [DailyBar(instrument.instrument_id, date(2026, 9, 28),
                                 10.0, 11.0, 9.0, 10.5, 100.0, None,
                                 "lot", "unavailable", "qfq", "tencent_kline", "hash")]

        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            with self.assertRaisesRegex(ValueError, "missing CNY amount"):
                sync_stock_pilot(store, snapshot, ["stock:600000.SH"],
                                 date(2026, 9, 1), date(2026, 9, 29),
                                 market_source=PriceOnlySource())
            with store.connect() as connection:
                run = connection.execute("SELECT status, row_count FROM sync_runs").fetchone()
            self.assertEqual("failed", run["status"])
            self.assertEqual(0, run["row_count"])


if __name__ == "__main__":
    unittest.main()
