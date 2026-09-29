from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.qlib_archive import QlibArchiveError
from quant_lab.qlib_local import publish_release_tree, read_stock
from test_qlib_archive import fixture_files, write_release


class QlibLocalTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.archive, self.manifest = write_release(self.directory, fixture_files())
        self.root = self.directory / "published"

    def test_atomic_publication_and_single_stock_random_access(self):
        published = publish_release_tree(self.archive, self.manifest, "2000-01-06", self.root)
        self.assertEqual("published", published["status"])
        self.assertTrue((self.root / "release-index.json").is_file())
        index = json.loads((self.root / "release-index.json").read_text())
        self.assertIn("qlib_bin/features/sh600000/close.day.bin", index["files"])
        report = read_stock(self.root, self.manifest, "2000-01-06", "600000",
                            start="2000-01-04", end="2000-01-06", limit=1)
        self.assertEqual("qlib_adjusted", report["adjustment"])
        self.assertEqual("stock:600000.SH", report["stock"]["instrument_id"])
        self.assertTrue(report["truncated_to_latest"])
        self.assertEqual(3, report["calendar_rows_in_range"])
        self.assertEqual([{"date": "2000-01-06", "close": 11.0,
                           "factor": 2.0, "amount": 1100.0}], report["rows"])
        self.assertEqual("pending_cross_source_check", report["raw_price_factor_validation"])
        missing = read_stock(self.root, self.manifest, "2000-01-06", "000003.SZ",
                             fields=("close", "amount"), limit=10)
        self.assertEqual(["amount"], missing["missing_fields"])
        self.assertEqual(2, missing["returned_rows"])

    def test_per_file_hash_rejects_modified_feature(self):
        publish_release_tree(self.archive, self.manifest, "2000-01-06", self.root)
        feature = self.root / "qlib_bin/features/sh600000/close.day.bin"
        with feature.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(QlibArchiveError, "size changed"):
            read_stock(self.root, self.manifest, "2000-01-06", "SH600000")

    def test_manifest_binding_and_existing_target_protection(self):
        publish_release_tree(self.archive, self.manifest, "2000-01-06", self.root)
        with self.assertRaisesRegex(QlibArchiveError, "already exists"):
            publish_release_tree(self.archive, self.manifest, "2000-01-06", self.root)
        payload = json.loads(self.manifest.read_text())
        payload["archive_sha256"] = "sha256:" + "0" * 64
        self.manifest.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
        with self.assertRaisesRegex(QlibArchiveError, "does not match manifest"):
            read_stock(self.root, self.manifest, "2000-01-06", "SH600000")

    def test_unsafe_archive_never_publishes_partial_tree(self):
        archive, manifest = write_release(self.directory, fixture_files(), bad_link=True)
        with self.assertRaisesRegex(QlibArchiveError, "unsafe archive member"):
            publish_release_tree(archive, manifest, "2000-01-06", self.root)
        self.assertFalse(self.root.exists())
        self.assertEqual([], list(self.directory.glob(".published.stage-*")))


if __name__ == "__main__":
    unittest.main()
