from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from csi300_official_delta_audit import SOURCE_FILES, extract_csi300


class OfficialDeltaSourceTest(unittest.TestCase):
    def test_official_attachment_versions_have_same_csi300_rows(self) -> None:
        source = ROOT / "studies/random-entry-baseline-v1/official-sources"
        rows = {day: extract_csi300(source / spec["name"], spec["sha256"])
                for day, spec in SOURCE_FILES.items()}
        self.assertEqual(rows["2021-06-01"], rows["2021-06-10"])
        adjustment = rows["2021-06-10"]
        self.assertEqual([len(adjustment[key]) for key in ("调入", "调出", "备选名单")],
                         [25, 25, 15])
        self.assertIn("SH688111", {item["symbol"] for item in adjustment["调入"]})
        self.assertIn("SH603156", {item["symbol"] for item in adjustment["调出"]})

    def test_wrong_source_hash_is_rejected(self) -> None:
        spec = SOURCE_FILES["2021-06-01"]
        path = ROOT / "studies/random-entry-baseline-v1/official-sources" / spec["name"]
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            extract_csi300(path, "0" * 64)


if __name__ == "__main__":
    unittest.main()
