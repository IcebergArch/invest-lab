from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.research_journal import append_stock_report


def report() -> dict[str, object]:
    return {
        "status": "partial_data",
        "query": "600000",
        "instrument_id": "stock:600000.SH",
        "asof": "2026-09-28",
        "summary": "历史数据观察",
        "sources": [{"latest_source_id": "investment_data_qlib_release",
                     "archive_sha256": "a" * 64}],
        "decision": {"action": "observe", "reason": "截至日期旧于今天"},
        "chart": {"dates": ["2026-09-25", "2026-09-28"],
                  "closes": [11.1, 11.2]},
    }


class ResearchJournalTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal = self.root / "journal"

    def save(self, item: dict[str, object] | None = None) -> dict[str, str]:
        return append_stock_report(
            self.journal, query="600000", cost_price=10.5,
            report=item or report(),
            versions={"decision_policy": "scenario-v1", "chart": "daily-close-v1"},
        )

    def test_private_records_preserve_full_report_and_stable_payload_hash(self) -> None:
        first = self.save()
        second = self.save()
        self.assertNotEqual(first["record_id"], second["record_id"])
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(first["payload_sha256"], second["payload_sha256"])
        self.assertEqual(str(UUID(first["record_id"])), first["record_id"])
        self.assertTrue(first["generated_at"].endswith("Z"))

        path = Path(first["path"])
        self.assertEqual((self.journal / first["generated_at"][:10]).resolve(), path.parent)
        self.assertEqual(0o700, stat.S_IMODE(self.journal.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(1, saved["schema_version"])
        self.assertEqual(first["record_id"], saved["record_id"])
        self.assertEqual(first["generated_at"], saved["generated_at"])
        self.assertEqual(report(), saved["payload"]["report"])
        self.assertEqual({"query": "600000", "cost_price": 10.5}, saved["payload"]["request"])
        self.assertEqual("scenario-v1", saved["payload"]["versions"]["decision_policy"])
        canonical = json.dumps(saved["payload"], ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), saved["payload_sha256"])
        self.assertEqual([], list(path.parent.glob("*.tmp")))

    def test_existing_uuid_record_is_never_replaced(self) -> None:
        with patch("quant_lab.research_journal.uuid4", return_value=UUID(int=7)):
            first = self.save()
            original = Path(first["path"]).read_bytes()
            with self.assertRaises(FileExistsError):
                self.save()
        self.assertEqual(original, Path(first["path"]).read_bytes())
        self.assertEqual(1, len(list(Path(first["path"]).parent.iterdir())))

    def test_write_failure_does_not_leave_or_claim_a_record(self) -> None:
        with patch("quant_lab.research_journal.os.link", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.save()
        self.assertEqual([], list(self.journal.rglob("*.json")))
        self.assertEqual([], list(self.journal.rglob("*.tmp")))

    def test_invalid_or_incomplete_reports_do_not_create_a_journal(self) -> None:
        bad_status = report()
        bad_status["status"] = "unknown_stock"
        with self.assertRaises(ValueError):
            self.save(bad_status)
        nonfinite = report()
        nonfinite["chart"] = {"closes": [float("nan")]}
        with self.assertRaises(ValueError):
            self.save(nonfinite)
        no_lineage = report()
        no_lineage["sources"] = []
        with self.assertRaises(ValueError):
            self.save(no_lineage)
        coerced_keys = report()
        coerced_keys["chart"] = {1: 11.2}
        with self.assertRaises(TypeError):
            self.save(coerced_keys)
        self.assertFalse(self.journal.exists())


if __name__ == "__main__":
    unittest.main()
