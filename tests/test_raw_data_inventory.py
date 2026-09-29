from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from quant_lab.models import DailyBar, Instrument
from quant_lab.raw_data_inventory import (
    _fill_year_exchange, _qlib_feature_counts, _read_sqlite, write_inventory,
)
from quant_lab.storage import MarketStore


class RawDataInventoryTests(unittest.TestCase):
    def test_mixed_units_and_unavailable_amount_are_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "market.sqlite3"
            store = MarketStore(path)
            store.initialize()
            instruments = [
                Instrument("stock:600000.SH", "600000", "A", "stock", "focus",
                           "eastmoney_kline", "1.600000", "SH", "qfq"),
                Instrument("stock:000001.SZ", "000001", "B", "stock", "focus",
                           "tencent_kline", "sz000001", "SZ", "qfq"),
            ]
            store.upsert_instruments(instruments)
            store.upsert_bars([
                DailyBar(instruments[0].instrument_id, date(2024, 1, 2),
                         10, 11, 9, 10.5, 500, 525000, "lot", "CNY", "qfq",
                         "eastmoney_kline", "a" * 64),
                DailyBar(instruments[1].instrument_id, date(2024, 1, 2),
                         20, 21, 19, 20.5, 700, None, "lot", "unavailable", "qfq",
                         "tencent_kline", "b" * 64),
            ])
            result = _read_sqlite(path, role="main_store")
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["snapshot"]["quick_check"], "ok")
            self.assertEqual(result["stock_instrument_count"], 2)
            groups = {row["source_id"]: row for row in result["bar_groups"]}
            self.assertEqual(groups["eastmoney_kline"]["volume_unit"], "lot")
            self.assertEqual(groups["eastmoney_kline"]["amount_observed_rows"], 1)
            self.assertEqual(groups["tencent_kline"]["amount_unit"], "unavailable")
            self.assertEqual(groups["tencent_kline"]["amount_observed_rows"], 0)
            self.assertEqual(groups["tencent_kline"]["amount_null_rows"], 1)

    def test_raw_amount_survives_ineligible_canonical_bar(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.sqlite3"
            store = MarketStore(path)
            store.initialize()
            instrument = Instrument("stock:000002.SZ", "000002", "C", "stock",
                                    "historical_stock", "baostock_daily", "sz.000002",
                                    "SZ", "none")
            store.upsert_instruments([instrument])
            store.upsert_bars([
                DailyBar(instrument.instrument_id, date(2024, 1, 2),
                         10, 10.5, 9.5, 10, 0, None, "share", "ineligible", "none",
                         "baostock_daily", "c" * 64),
            ])
            with sqlite3.connect(path) as connection:
                connection.execute("""
                    CREATE TABLE baostock_daily_status (
                        instrument_id TEXT, trade_date TEXT, tradestatus INTEGER,
                        is_st INTEGER, raw_amount_cny REAL, has_valid_bar INTEGER,
                        payload_hash TEXT, run_id TEXT)
                """)
                connection.execute("""
                    INSERT INTO baostock_daily_status VALUES (?,?,?,?,?,?,?,?)
                """, (instrument.instrument_id, "2024-01-02", 0, 0, 123.0, 1,
                      "c" * 64, "run-1"))
            result = _read_sqlite(path, role="baostock_archive", start_year=2023)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["bar_groups"][0]["amount_observed_rows"], 0)
            self.assertEqual(result["bar_groups"][0]["zero_stored_volume_rows"], 1)
            self.assertTrue(result["bar_groups"][0]["stored_zero_may_represent_source_null"])
            self.assertEqual(result["status_fields"]["raw_amount_cny_observed_rows"], 1)
            self.assertEqual(result["status_fields"][
                "canonical_amount_null_but_raw_amount_observed_rows"], 1)
            self.assertEqual(result["zero_stock_years"], ["2023"])
            self.assertIn({"year": "2023", "exchange": "SH", "bar_rows": 0,
                           "instrument_count": 0, "first_trade_date": None,
                           "last_trade_date": None}, result["year_exchange_coverage"])

    def test_qlib_files_are_not_claimed_as_valid_values(self):
        index = {"files": {
            "qlib_bin/features/sh600000/open.day.bin": {},
            "qlib_bin/features/sh600000/volume.day.bin": {},
            "qlib_bin/features/sh600001/open.day.bin": {},
            "qlib_bin/features/sh600000/change.day.bin": {},
        }}
        result = _qlib_feature_counts(index)
        self.assertEqual(result["open"]["feature_file_count"], 2)
        self.assertIsNone(result["open"]["valid_value_count"])
        self.assertEqual(result["volume"]["unit"], "adjusted_or_unknown_source_unit")
        self.assertEqual(result["change"]["field_kind"], "source_supplied_transform")

    def test_atomic_report_is_parseable_and_missing_source_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            report = {"status": _read_sqlite(Path(temporary) / "absent.sqlite3",
                                               role="main_store")["status"]}
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(write_inventory(path, report), path)
            self.assertEqual(json.loads(path.read_text()), report)


if __name__ == "__main__":
    unittest.main()
