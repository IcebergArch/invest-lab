from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.cli import parser
from quant_lab.models import DailyBar, Instrument
from quant_lab.storage import MarketStore
from quant_lab.sync import sync_database


class TargetedSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MarketStore(Path(self.temp.name) / "market.sqlite3")
        self.store.initialize()
        self.stock = Instrument(
            "stock:603993.SH", "603993.SH", "洛阳钼业", "stock",
            "current_stock", "eastmoney_kline", "1.603993", "SSE", "qfq",
        )
        self.store.upsert_instruments([self.stock])

    @staticmethod
    def _bar(item: Instrument) -> DailyBar:
        return DailyBar(
            instrument_id=item.instrument_id,
            trade_date=date(2026, 9, 29),
            open=16.8, high=17.0, low=16.7, close=16.75,
            volume=1000, amount=1675000,
            volume_unit="lot", amount_unit="CNY",
            adjustment=item.adjustment, source_id="eastmoney_kline",
            payload_hash="source-response-sha256",
        )

    def test_stock_only_sync_records_target_and_row_lineage(self) -> None:
        progress: list[str] = []
        with patch("quant_lab.sync.PublicMarketDailySource") as source:
            source.return_value.fetch.side_effect = (
                lambda item, _start, _end: [self._bar(item)]
            )
            result = sync_database(
                self.store, date(2026, 9, 29), date(2026, 9, 29),
                stock_ids=[self.stock.instrument_id], workers=1,
                progress=progress.append,
            )
        self.assertEqual((1, 1), (result.instrument_count, result.row_count))
        self.assertEqual(1, source.return_value.fetch.call_count)
        self.assertIn("2026-09-29..2026-09-29", progress[0])
        with self.store.connect() as connection:
            run = connection.execute(
                "SELECT status, groups_json FROM sync_runs WHERE run_id=?",
                (result.run_id,),
            ).fetchone()
            bar = connection.execute(
                "SELECT run_id, source_id, payload_hash, fetched_at FROM daily_bars "
                "WHERE instrument_id=? AND trade_date='2026-09-29'",
                (self.stock.instrument_id,),
            ).fetchone()
        self.assertEqual("success", run["status"])
        self.assertEqual([self.stock.instrument_id], json.loads(run["groups_json"]))
        self.assertEqual(result.run_id, bar["run_id"])
        self.assertEqual("eastmoney_kline", bar["source_id"])
        self.assertEqual("source-response-sha256", bar["payload_hash"])
        self.assertTrue(bar["fetched_at"])

    def test_core_and_explicit_stock_are_deduplicated(self) -> None:
        core = Instrument(
            "stock:002475.SZ", "002475.SZ", "立讯精密", "stock",
            "focus_stock", "eastmoney_kline", "0.002475", "SZSE", "qfq",
        )
        self.store.upsert_instruments([core])
        with patch("quant_lab.sync.PublicMarketDailySource") as source:
            source.return_value.fetch.side_effect = (
                lambda item, _start, _end: [self._bar(item)]
            )
            result = sync_database(
                self.store, date(2026, 9, 29), date(2026, 9, 29),
                groups=["stocks"], stock_ids=[core.instrument_id, core.instrument_id],
                workers=1,
            )
        self.assertEqual(3, result.instrument_count)
        self.assertEqual(3, source.return_value.fetch.call_count)
        with self.store.connect() as connection:
            groups = json.loads(connection.execute(
                "SELECT groups_json FROM sync_runs WHERE run_id=?",
                (result.run_id,),
            ).fetchone()[0])
        self.assertEqual(["stocks", core.instrument_id], groups)

    def test_rejects_unknown_registered_stock_before_fetch(self) -> None:
        with patch("quant_lab.sync.PublicMarketDailySource") as source:
            with self.assertRaisesRegex(ValueError, "not registered"):
                sync_database(
                    self.store, date(2026, 9, 29), date(2026, 9, 29),
                    stock_ids=["stock:999999.SH"], workers=1,
                )
        source.assert_not_called()
        with self.store.connect() as connection:
            run = connection.execute(
                "SELECT status, error FROM sync_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual("failed", run["status"])
        self.assertIn("not registered", run["error"])

    def test_cli_stock_can_be_used_without_group(self) -> None:
        args = parser().parse_args([
            "sync", "--start", "2026-09-29", "--end", "2026-09-29",
            "--stock", "stock:603993.SH",
        ])
        self.assertIsNone(args.group)
        self.assertEqual([self.stock.instrument_id], args.stock)


if __name__ == "__main__":
    unittest.main()
