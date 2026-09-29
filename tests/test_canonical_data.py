from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from quant_lab.canonical_data import (
    BaoStockArchiveAdapter, MarketStoreAdapter, build_close_panel,
    normalize_market_row,
)


HASH_A = "a" * 64
HASH_B = "b" * 64


class CanonicalDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "market.sqlite3"
        with sqlite3.connect(self.db) as connection:
            connection.executescript("""
                CREATE TABLE instruments(instrument_id TEXT PRIMARY KEY, asset_type TEXT);
                CREATE TABLE daily_bars(
                    instrument_id TEXT, trade_date TEXT, open REAL, high REAL,
                    low REAL, close REAL, volume REAL, amount REAL,
                    volume_unit TEXT, amount_unit TEXT, adjustment TEXT,
                    source_id TEXT, payload_hash TEXT, run_id TEXT, fetched_at TEXT
                );
                CREATE TABLE baostock_daily_status(
                    instrument_id TEXT, trade_date TEXT, tradestatus INTEGER,
                    is_st INTEGER, raw_amount_cny REAL, has_valid_bar INTEGER,
                    payload_hash TEXT, run_id TEXT, fetched_at TEXT
                );
            """)
            connection.executemany(
                "INSERT INTO instruments VALUES (?,?)",
                [("stock:600000.SH", "stock"), ("stock:000001.SZ", "stock")],
            )
            connection.execute(
                "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("stock:600000.SH", "2026-09-28", 10.0, 11.0, 9.5, 10.5,
                 12.0, 1234.0, "lot", "CNY", "qfq", "eastmoney_kline",
                 HASH_A, "run-main", "2026-09-29T01:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("stock:000001.SZ", "2026-09-28", 8.0, 8.3, 7.9, 8.1,
                 100.0, 800.0, "share", "CNY", "none", "baostock_daily",
                 HASH_B, "run-archive", "2026-09-29T01:01:00+00:00"),
            )
            connection.execute(
                "INSERT INTO baostock_daily_status VALUES (?,?,?,?,?,?,?,?,?)",
                ("stock:000001.SZ", "2026-09-28", 1, 0, 800.0, 1,
                 HASH_B, "run-archive", "2026-09-29T01:01:00+00:00"),
            )

    def test_market_adapter_converts_lots_but_keeps_price_basis(self) -> None:
        bars = list(MarketStoreAdapter(self.db).iter_bars(
            instrument_ids=["stock:600000.SH"], batch_size=1,
        ))
        self.assertEqual(1, len(bars))
        bar = bars[0]
        self.assertEqual("forward_adjusted", bar.price_basis)
        self.assertEqual("CNY/share", bar.price_unit)
        self.assertEqual(12, bar.source_volume)
        self.assertEqual("lot", bar.source_volume_unit)
        self.assertEqual(1200, bar.volume_shares)
        self.assertEqual(1234, bar.amount_cny)
        self.assertEqual("run-main", bar.run_id)
        self.assertIsNone(bar.source_published_at)
        self.assertFalse(bar.point_in_time_eligible)
        self.assertEqual("unknown", bar.tradability)

    def test_archive_adapter_requires_matching_status_and_raw_basis(self) -> None:
        bar = next(BaoStockArchiveAdapter(self.db).iter_bars(
            instrument_ids=["stock:000001.SZ"],
            start=date(2026, 9, 28), end=date(2026, 9, 28),
        ))
        self.assertEqual("raw_unadjusted", bar.price_basis)
        self.assertEqual("tradable", bar.tradability)
        self.assertEqual(100, bar.volume_shares)
        self.assertEqual(800, bar.amount_cny)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE baostock_daily_status SET run_id='different'"
            )
        with self.assertRaisesRegex(ValueError, "run IDs differ"):
            list(BaoStockArchiveAdapter(self.db).iter_bars())

    def test_ineligible_archive_amount_is_never_exposed_as_liquidity(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE baostock_daily_status SET is_st=1")
            connection.execute(
                "UPDATE daily_bars SET amount=NULL,amount_unit='ineligible' "
                "WHERE source_id='baostock_daily'"
            )
        bar = next(BaoStockArchiveAdapter(self.db).iter_bars())
        self.assertEqual("ineligible", bar.tradability)
        self.assertIsNone(bar.amount_cny)
        self.assertEqual(800, bar.raw_amount_cny)

    def test_panel_rejects_mixed_basis_and_fingerprints_lineage(self) -> None:
        main = next(MarketStoreAdapter(self.db).iter_bars(
            instrument_ids=["stock:600000.SH"],
        ))
        archive = next(BaoStockArchiveAdapter(self.db).iter_bars())
        with self.assertRaisesRegex(ValueError, "cannot mix"):
            build_close_panel([main, archive], expected_price_basis="forward_adjusted")
        first = build_close_panel([main], expected_price_basis="forward_adjusted")
        self.assertEqual((date(2026, 9, 28),), first.dates)
        self.assertEqual((10.5,), first.closes["stock:600000.SH"])
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE daily_bars SET payload_hash=? WHERE source_id='eastmoney_kline'",
                               (HASH_B,))
        changed = next(MarketStoreAdapter(self.db).iter_bars(
            instrument_ids=["stock:600000.SH"],
        ))
        second = build_close_panel([changed], expected_price_basis="forward_adjusted")
        self.assertNotEqual(first.input_fingerprint_sha256, second.input_fingerprint_sha256)

    def test_unknown_unit_and_nonfinite_price_are_rejected(self) -> None:
        row = {
            "instrument_id": "stock:600000.SH", "asset_type": "stock",
            "trade_date": "2026-09-28", "open": 10., "high": 11., "low": 9.,
            "close": 10.5, "volume": 1., "amount": 100.,
            "volume_unit": "mystery", "amount_unit": "CNY",
            "adjustment": "qfq", "source_id": "eastmoney_kline",
            "payload_hash": HASH_A, "run_id": "run-main",
            "fetched_at": "2026-09-29T01:00:00+00:00",
        }
        with self.assertRaisesRegex(ValueError, "unknown"):
            normalize_market_row(row)
        row["volume_unit"] = "lot"
        row["close"] = float("nan")
        with self.assertRaisesRegex(ValueError, "OHLC"):
            normalize_market_row(row)


if __name__ == "__main__":
    unittest.main()
