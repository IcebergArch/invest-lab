from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("package_quant_data", ROOT / "scripts/package-quant-data.py")
assert SPEC is not None and SPEC.loader is not None
archive_tools = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = archive_tools
SPEC.loader.exec_module(archive_tools)


class DataArchiveTest(unittest.TestCase):
    def test_full_backup_restores_wal_database_and_research_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for category in archive_tools.SOURCES:
                (root / category).mkdir(parents=True)
            database = root / "data/quant/market.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE evidence (value TEXT)")
                connection.execute("INSERT INTO evidence VALUES ('committed-in-wal')")
                connection.commit()
                (root / "reports/quant/review.json").write_text('{"status":"pending"}')
                (root / "studies/replay.json").write_text('{"trades":3}')
                (root / "policies/frozen.json").write_text('{"fee_bps":5}')
                (root / "studies/incomplete.partial").write_text("unfinished")
                output = root / "backups"
                output.mkdir()
                summary = archive_tools.backup_one(archive_tools.source_files(root), output, "full")
                self.assertTrue(summary["verified"])
                self.assertEqual(4, summary["file_count"])
                with zipfile.ZipFile(summary["path"]) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(sorted(str(item) for item in archive_tools.SOURCES), manifest["sources"])
                    self.assertEqual("sqlite_backup", next(item["capture"] for item in manifest["files"]
                                                           if item["path"] == "data/quant/market.sqlite3"))
                    restored = root / "restore.sqlite3"
                    restored.write_bytes(archive.read("data/quant/market.sqlite3"))
                    with sqlite3.connect(restored) as reader:
                        self.assertEqual("ok", reader.execute("PRAGMA integrity_check").fetchone()[0])
                        self.assertEqual("committed-in-wal", reader.execute("SELECT value FROM evidence").fetchone()[0])
            finally:
                connection.close()

    def test_valid_zip_crc_cannot_hide_a_manifest_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "payload.json"
            source.write_text("original")
            item = archive_tools.SourceFile(source, "studies/payload.json", "studies", "_root", False)
            summary = archive_tools.backup_one([item], root, "fixture")
            corrupted = root / "changed.zip"
            with zipfile.ZipFile(summary["path"]) as original, zipfile.ZipFile(corrupted, "w") as changed:
                changed.writestr("manifest.json", original.read("manifest.json"))
                changed.writestr("studies/payload.json", "modified")
            with self.assertRaisesRegex(ValueError, "清单校验失败"):
                archive_tools.verify_archive(corrupted)


if __name__ == "__main__":
    unittest.main()
