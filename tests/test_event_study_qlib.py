from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from quant_lab.event_study import (
    append_event_study_record, read_event_study_record,
    render_event_study_markdown,
)
from quant_lab.event_study_qlib import evaluate_qlib_event_cohort
from quant_lab.qlib_archive import QlibArchiveError
from quant_lab.qlib_local import publish_release_tree
from test_qlib_archive import fixture_files, write_release


class QlibEventStudyTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        archive, self.manifest = write_release(self.directory, fixture_files())
        self.qlib_root = self.directory / "published"
        publish_release_tree(archive, self.manifest, "2000-01-06", self.qlib_root)
        self.db = self.directory / "market.sqlite3"
        with sqlite3.connect(self.db) as connection:
            connection.executescript("""
                CREATE TABLE instruments(instrument_id TEXT, asset_type TEXT);
                CREATE TABLE daily_bars(
                    instrument_id TEXT, trade_date TEXT, open REAL, high REAL,
                    low REAL, close REAL, volume REAL, amount REAL,
                    volume_unit TEXT, amount_unit TEXT, adjustment TEXT,
                    source_id TEXT, payload_hash TEXT, run_id TEXT, fetched_at TEXT
                );
            """)
            connection.execute("INSERT INTO instruments VALUES ('index:000300.SH','index')")
            for day, close in (("2000-01-04", 100.0), ("2000-01-05", 101.0),
                               ("2000-01-06", 102.0)):
                connection.execute(
                    "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("index:000300.SH", day, close, close, close, close,
                     0.0, None, "source_native", "unavailable", "none",
                     "test_index", "a" * 64, "run-index",
                     "2000-01-07T00:00:00+00:00"),
                )
        self.events = self.directory / "events.json"
        self.events.write_text(json.dumps([{
            "event_id": "synthetic.policy.2000", "title": "合成公告",
            "category": "monetary_policy.rrr",
            "announcement_at": "2000-01-04", "time_precision": "date",
            "effective_date": "2000-01-06",
            "source_url": "https://example.org/source",
            "publication_certainty": "official_verified",
            "availability_confidence": "date_verified",
            "magnitude": -0.25, "magnitude_unit": "percentage_point",
            "co_announcements": [],
        }]), encoding="utf-8")

    def evaluate(self):
        return evaluate_qlib_event_cohort(
            self.qlib_root, self.manifest, "2000-01-06", self.db, self.events,
        )

    def test_fixed_cohort_preserves_q_lib_close_hash_and_only_computes_returns(self) -> None:
        result = self.evaluate()
        self.assertEqual("qlib_adjusted", result["stock_price_basis"])
        self.assertEqual("index_points", result["benchmark_price_basis"])
        self.assertEqual(2, result["qlib_selection"]["selected_count"])
        self.assertEqual(2, result["qlib_selection"]["observed_stock_count"])
        self.assertEqual(7, result["input_bar_count"])
        self.assertEqual(0, result["benchmark_lineage"]["qlib_calendar_sessions_missing_in_index"])
        self.assertTrue(all(item["file_sha256"].startswith("sha256:")
                            for item in result["qlib_selection"]["selected"]))
        self.assertEqual(64, len(result["event_registry_file_sha256"]))
        self.assertEqual(64, len(result["implementation_sha256"]["event_study_qlib"]))
        item = result["events"][0]
        self.assertEqual("2000-01-05", item["reaction_date"])
        stock = item["stocks"]["stock:000003.SZ"]["windows"]["post_0"]
        self.assertAlmostEqual(6 / 5 - 1, stock["return"])
        self.assertAlmostEqual((6 / 5 - 1) - (101 / 100 - 1),
                               stock["excess_return"])
        self.assertIsNone(stock["volume_ratio_to_control"])
        self.assertIsNone(stock["amount_ratio_to_control"])
        self.assertIn("只用 Qlib 复权 CLOSE", render_event_study_markdown(result))
        with tempfile.TemporaryDirectory() as folder:
            saved = append_event_study_record(folder, result)
            self.assertEqual(result, read_event_study_record(saved["json_path"])["payload"])

    def test_source_feature_tampering_is_rejected(self) -> None:
        feature_path = self.qlib_root / "qlib_bin/features/sh600000/close.day.bin"
        with feature_path.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(QlibArchiveError):
            self.evaluate()


if __name__ == "__main__":
    unittest.main()
