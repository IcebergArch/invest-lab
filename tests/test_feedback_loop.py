from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.feedback_loop import (append_feedback_case, list_case_reviews,
                                     list_feedback_cases, review_feedback_cases)
from quant_lab.models import DailyBar, Instrument
from quant_lab.research_journal import append_stock_report
from quant_lab.storage import MarketStore


class FeedbackLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.journal = root / "journal"
        self.cases = root / "cases"
        self.reviews = root / "reviews"
        self.store = MarketStore(root / "market.sqlite3")
        self.store.initialize()
        self.stock_id = "stock:000001.SZ"
        self.start = date(2026, 1, 1)
        self.store.upsert_instruments([
            Instrument(self.stock_id, "000001", "示例银行", "stock", "focus_stock",
                       "eastmoney_kline", "0.000001", "SZ", "qfq"),
            Instrument("index:000300.SH", "000300", "沪深300", "index", "broad",
                       "eastmoney_kline", "1.000300", "SH", "none"),
        ])
        bars = []
        for index in range(65):
            day = self.start + timedelta(days=index)
            for instrument_id, close, adjustment in (
                (self.stock_id, 10.0 + index * 0.1, "qfq"),
                ("index:000300.SH", 100.0 + index, "none"),
            ):
                bars.append(DailyBar(
                    instrument_id, day, close, close, close, close,
                    1000.0, 10000.0, "share", "CNY", adjustment,
                    "eastmoney_kline", "a" * 64,
                ))
        self.store.upsert_bars(bars)
        self.record = append_stock_report(
            self.journal, query="000001", cost_price=None,
            report={
                "status": "ready", "instrument_id": self.stock_id,
                "name": "示例银行", "asof": self.start.isoformat(),
                "latest_close": 10.0, "sources": [{"latest_payload_hash": "a" * 64}],
                "decision": {"rule_version": "scenario-v1", "input_provenance": {
                    "source_kind": "main_store", "price_basis": "qfq",
                    "source_payload_hash": "a" * 64,
                }},
            },
        )

    def save_case(self) -> dict:
        return append_feedback_case(
            self.cases, self.journal,
            research_record_id=self.record["record_id"],
            research_record_date=self.record["generated_at"][:10],
            decision="watch", observed_date=self.start.isoformat(),
            note="等待确认",
        )

    def test_case_links_report_and_due_review_is_idempotent(self) -> None:
        saved = self.save_case()
        cases = list_feedback_cases(self.cases)
        self.assertEqual(saved["case_id"], cases[0]["case_id"])
        self.assertNotIn("note", cases[0])
        first = review_feedback_cases(
            self.store, self.cases, self.reviews,
            asof=self.start + timedelta(days=7),
        )
        self.assertEqual(1, first["saved"])
        review = list_case_reviews(self.reviews)[0]
        self.assertEqual("partial", review["status"])
        self.assertEqual(5, review["horizon_sessions"])
        self.assertAlmostEqual(0.05, review["outcome"]["5"]["price_return"])
        repeated = review_feedback_cases(
            self.store, self.cases, self.reviews,
            asof=self.start + timedelta(days=7),
        )
        self.assertEqual(0, repeated["saved"])
        next_day = review_feedback_cases(
            self.store, self.cases, self.reviews,
            asof=self.start + timedelta(days=8),
        )
        self.assertEqual(0, next_day["saved"])
        second_horizon = review_feedback_cases(
            self.store, self.cases, self.reviews,
            asof=self.start + timedelta(days=20),
        )
        self.assertEqual(1, second_horizon["saved"])
        self.assertEqual(20, list_case_reviews(self.reviews)[0]["horizon_sessions"])
        complete = review_feedback_cases(
            self.store, self.cases, self.reviews,
            asof=self.start + timedelta(days=60),
        )
        self.assertEqual(1, complete["saved"])
        self.assertEqual("complete", list_case_reviews(self.reviews)[0]["status"])
        after_complete = review_feedback_cases(
            self.store, self.cases, self.reviews,
            asof=self.start + timedelta(days=61),
        )
        self.assertEqual(0, after_complete["saved"])

    def test_revised_signal_price_blocks_outcome(self) -> None:
        self.save_case()
        self.store.upsert_bars([DailyBar(
            self.stock_id, self.start, 11.0, 11.0, 11.0, 11.0,
            1000.0, 10000.0, "share", "CNY", "qfq", "eastmoney_kline", "b" * 64,
        )])
        result = review_feedback_cases(
            self.store, self.cases, self.reviews,
            asof=self.start + timedelta(days=7),
        )
        self.assertEqual(1, result["unavailable"])
        self.assertEqual("unavailable", list_case_reviews(self.reviews)[0]["status"])

    def test_all_cases_and_reviews_are_scanned_after_display_limit(self) -> None:
        case_ids = {self.save_case()["case_id"] for _ in range(101)}
        self.assertEqual(101, len(case_ids))
        first = review_feedback_cases(
            self.store, self.cases, self.reviews,
            asof=self.start + timedelta(days=7),
        )
        self.assertEqual(101, first["saved"])
        self.assertEqual(101, len(list(self.reviews.rglob("*.json"))))
        # The public list stays bounded, while the engine must remember all
        # 101 case/review identities when the market date changes.
        self.assertEqual(100, len(list_case_reviews(self.reviews, limit=100)))
        repeated = review_feedback_cases(
            self.store, self.cases, self.reviews,
            asof=self.start + timedelta(days=8),
        )
        self.assertEqual(0, repeated["saved"])
        self.assertEqual(101, len(list(self.reviews.rglob("*.json"))))

    def test_signal_source_revision_is_not_marked_complete(self) -> None:
        self.save_case()
        review_feedback_cases(self.store, self.cases, self.reviews,
                              asof=self.start + timedelta(days=60))
        self.assertEqual("complete", list_case_reviews(self.reviews)[0]["status"])
        self.store.upsert_bars([DailyBar(
            self.stock_id, self.start, 10.0, 10.0, 10.0, 10.0,
            1000.0, 10000.0, "share", "CNY", "qfq", "eastmoney_kline", "b" * 64,
        )])
        revised = review_feedback_cases(self.store, self.cases, self.reviews,
                                        asof=self.start + timedelta(days=61))
        self.assertEqual(1, revised["saved"])
        current = list_case_reviews(self.reviews)[0]
        self.assertEqual("needs_source_review", current["status"])
        self.assertIn("来源哈希", current["reason"])
        repeated = review_feedback_cases(self.store, self.cases, self.reviews,
                                         asof=self.start + timedelta(days=62))
        self.assertEqual(0, repeated["saved"])

    def test_target_source_revision_is_not_marked_complete(self) -> None:
        self.save_case()
        review_feedback_cases(self.store, self.cases, self.reviews,
                              asof=self.start + timedelta(days=60))
        target_day = self.start + timedelta(days=5)
        self.store.upsert_bars([DailyBar(
            self.stock_id, target_day, 10.7, 10.7, 10.7, 10.7,
            1000.0, 10000.0, "share", "CNY", "qfq", "eastmoney_kline", "b" * 64,
        )])
        revised = review_feedback_cases(self.store, self.cases, self.reviews,
                                        asof=self.start + timedelta(days=61))
        self.assertEqual(1, revised["saved"])
        current = list_case_reviews(self.reviews)[0]
        self.assertEqual("needs_source_review", current["status"])
        self.assertIn("首次复盘修订", current["reason"])
        repeated = review_feedback_cases(self.store, self.cases, self.reviews,
                                         asof=self.start + timedelta(days=62))
        self.assertEqual(0, repeated["saved"])


if __name__ == "__main__":
    unittest.main()
