from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.analysis_timeline import INDEX_EVENTS, build_analysis_timeline


def report(symbol: str, days: list[str], factors: list[dict] | None = None) -> dict:
    return {"instrument_id": symbol,
            "chart": {"history": [{"date": day, "close": 1.0} for day in days]},
            "factor_insights": factors or []}


class AnalysisTimelineTest(unittest.TestCase):
    def test_official_2021_adjustment_keeps_announcement_effective_and_price_dates_distinct(self) -> None:
        result = build_analysis_timeline(report("stock:600079.SH", [
            "2021-05-28", "2021-06-11", "2021-06-15", "2021-06-16"]))
        events = [item for item in result["events"] if item["category"] == "index"]
        self.assertEqual(1, len(events))
        item = events[0]
        self.assertEqual("2021-05-28", item["event_at"])
        self.assertEqual("2021-06-11 收盘后", item["effective_at"])
        self.assertEqual("2021-06-15", item["anchor_date"])
        self.assertTrue(item["source_url"].startswith("https://www.csindex.com.cn/"))
        self.assertEqual(64, len(item["source_sha256"]))
        self.assertEqual("audited_selected_adjustments", next(layer["status"] for layer in result["layers"] if layer["category"] == "index"))

    def test_official_2026_csi_a50_inclusion_for_cmoc(self) -> None:
        result = build_analysis_timeline(report("stock:603993.SH", [
            "2026-05-29", "2026-06-12", "2026-06-15", "2026-06-16"]))
        item = next(event for event in result["events"] if event["event_id"] == "csi-a50:2026-06:603993.SH:in")
        self.assertEqual("2026-05-29", item["event_at"])
        self.assertEqual("2026-06-12 收盘后", item["effective_at"])
        self.assertEqual("2026-06-15", item["anchor_date"])
        self.assertTrue(item["source_url"].startswith("https://oss-ch.csindex.com.cn/"))
        self.assertEqual(64, len(item["input_sha256"]))

    def test_index_layer_does_not_claim_missing_or_changed_sources(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing.json"
            report_data = report("stock:603993.SH", ["2026-06-12", "2026-06-15"])
            only_2026 = build_analysis_timeline(report_data, index_audit=missing)
            layer = next(item for item in only_2026["layers"] if item["category"] == "index")
            self.assertIn("中证 A50", layer["coverage"])
            self.assertNotIn("沪深 300", layer["coverage"])
            changed = json.loads(INDEX_EVENTS.read_text(encoding="utf-8"))
            changed["events"][0]["document_sha256"] = "0" * 64
            bad_registry = Path(folder) / "changed.json"
            bad_registry.write_text(json.dumps(changed), encoding="utf-8")
            only_2021 = build_analysis_timeline(report_data, index_events=bad_registry)
            layer = next(item for item in only_2021["layers"] if item["category"] == "index")
            self.assertEqual("audited_single_adjustment", layer["status"])
            self.assertFalse(any(item["event_id"] == "csi-a50:2026-06:603993.SH:in"
                                 for item in only_2021["events"]))

    def test_policy_after_close_marks_next_trading_day_and_factor_marks_observation(self) -> None:
        factor = {"factor_id": "momentum", "asof": "2022-04-25", "status": "ok", "value": 0.1,
                  "change": 0.02, "source_id": "market", "input_sha256": "a" * 64,
                  "availability_evidence": "retrospective"}
        result = build_analysis_timeline(report("stock:600079.SH", [
            "2022-04-15", "2022-04-18", "2022-04-25"], [factor]))
        policy = next(item for item in result["events"] if item["event_id"] == "pboc.rrr.2022-04-15")
        self.assertEqual("2022-04-15T18:14:39+08:00", policy["event_at"])
        self.assertEqual("2022-04-18", policy["anchor_date"])
        self.assertEqual("2022-04-25", policy["effective_at"])
        observed = next(item for item in result["events"] if item["category"] == "factor")
        self.assertEqual("2022-04-25", observed["anchor_date"])
        self.assertEqual("retrospective", observed["availability"])

    def test_missing_sources_fail_to_empty_layers(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing.json"
            result = build_analysis_timeline(report("stock:600079.SH", ["2026-09-29"]),
                                             policy_registry=missing, index_audit=missing,
                                             index_events=missing,
                                             company_disclosures=missing)
        self.assertFalse(result["events"])
        self.assertEqual("source_unavailable", next(layer["status"] for layer in result["layers"] if layer["category"] == "index"))
        self.assertEqual("source_unavailable", next(layer["status"] for layer in result["layers"] if layer["category"] == "disclosure"))

    def test_reviewed_documents_use_next_trading_day_when_release_time_is_unknown(self) -> None:
        days = ["2026-04-24", "2026-04-27", "2026-04-29", "2026-04-30", "2026-05-06",
                "2026-08-20", "2026-08-21", "2026-08-28", "2026-08-31", "2026-09-01"]
        for stock, q1_published, q1_plotted, h1_published, h1_plotted in (
            ("stock:603993.SH", "2026-04-25", "2026-04-27", "2026-08-20", "2026-08-21"),
            ("stock:000977.SZ", "2026-04-30", "2026-05-06", "2026-08-29", "2026-08-31"),
            ("stock:002594.SZ", "2026-04-29", "2026-04-30", "2026-08-29", "2026-08-31"),
        ):
            with self.subTest(stock=stock):
                result = build_analysis_timeline(report(stock, days))
                events = [item for item in result["events"] if item["category"] == "disclosure"]
                self.assertEqual(2, len(events))
                self.assertEqual([(q1_published, q1_plotted), (h1_published, h1_plotted)],
                                 [(item["event_at"], item["anchor_date"]) for item in events])
                for item in events:
                    self.assertEqual("document_calendar_date", item["event_time_kind"])
                    self.assertEqual("official_document_date_only", item["availability"])
                    self.assertEqual(64, len(item["source_sha256"]))
                if stock == "stock:603993.SH":
                    self.assertIn("更正版", events[0]["title"])
                    self.assertTrue(events[0]["source_url"].startswith("https://static.sse.com.cn/"))
                self.assertEqual("curated_date_only_subset", next(
                    layer["status"] for layer in result["layers"] if layer["category"] == "disclosure"))


if __name__ == "__main__":
    unittest.main()
