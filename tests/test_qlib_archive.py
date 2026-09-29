from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.qlib_archive import (
    MANIFEST_KEYS, QlibArchiveError, inspect_release, parse_calendar,
    parse_instruments, read_feature,
)


DAY = "2000-01-04\n2000-01-05\n2000-01-06\n"
FUTURE = DAY + "2000-01-07\n2000-01-10\n"
ALL = "SH600000\t2000-01-04\t2000-01-06\nSZ000003\t2000-01-04\t2000-01-05\n"
CSI = "SH600000\t2000-01-04\t2000-01-06\n"


def feature(start: int, *values: float) -> bytes:
    return struct.pack("<" + "f" * (len(values) + 1), float(start), *values)


def fixture_files() -> dict[str, bytes]:
    root = "qlib_bin/"
    files = {
        root + "calendars/day.txt": DAY.encode(),
        root + "calendars/day_future.txt": FUTURE.encode(),
        root + "instruments/all.txt": ALL.encode(),
        root + "features/sh600000/close.day.bin": feature(0, 10, math.nan, 11),
        root + "features/sh600000/factor.day.bin": feature(0, 1, math.nan, 2),
        root + "features/sh600000/amount.day.bin": feature(0, 1000, math.nan, 1100),
        root + "features/sz000003/close.day.bin": feature(0, 5, 6),
        root + "features/sz000003/factor.day.bin": feature(0, 1, 1),
    }
    for name in ("csi300", "csi500", "csi800", "csi1000", "csiall"):
        files[root + f"instruments/{name}.txt"] = CSI.encode()
    return files


def write_release(directory: Path, files: dict[str, bytes], *, bad_link: bool = False):
    archive = directory / "qlib_bin.tar.gz"
    manifest_path = directory / "qlib_bin.manifest.json"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, payload in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))
        if bad_link:
            member = tarfile.TarInfo("qlib_bin/features/link")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../secret"
            bundle.addfile(member)
    data = {
        "release_tag": "2000-01-06", "target_trade_date": "2000-01-06",
        "future_start_date": "2000-01-07", "future_end_date": "2000-01-10",
        "dolt_commit": "a" * 32, "investment_data_commit": "b" * 40,
        "qlib_commit": "c" * 40, "image_digest": "sha256:" + "d" * 64,
        "archive_size_bytes": archive.stat().st_size,
        "archive_sha256": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    assert tuple(data) == MANIFEST_KEYS
    manifest_path.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
    return archive, manifest_path


class QlibArchiveTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.archive, self.manifest = write_release(self.directory, fixture_files())

    def test_verified_release_coverage_and_sample(self):
        result = inspect_release(self.archive, self.manifest, "2000-01-06",
                                 sample="600000.SH", sample_rows=2)
        self.assertEqual("inspected", result["status"])
        self.assertEqual(3, result["calendar"]["sessions"])
        self.assertEqual(2, result["instruments"]["count"])
        self.assertEqual({"SH": 1, "SZ": 1}, result["instruments"]["exchange_counts"])
        self.assertEqual(1, result["instruments"]["ended_before_target_count"])
        self.assertEqual(4, result["daily_close"]["valid_bar_count"])
        self.assertEqual(2, result["daily_close"]["stocks_with_valid_close"])
        self.assertEqual(2, result["daily_close"]["per_year"][0]["stocks"])
        self.assertEqual(1, result["daily_close"]["nan_count"])
        self.assertEqual("SH600000", result["sample"]["symbol"])
        self.assertEqual(["2000-01-04", "2000-01-06"],
                         [row["date"] for row in result["sample"]["rows"]])
        self.assertEqual(2.0, result["sample"]["rows"][-1]["factor"])

    def test_hash_and_tag_fail_before_archive_read(self):
        with self.assertRaisesRegex(QlibArchiveError, "tag"):
            inspect_release(self.archive, self.manifest, "2000-01-07")
        with self.archive.open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaisesRegex(QlibArchiveError, "SHA-256"):
            inspect_release(self.archive, self.manifest, "2000-01-06")

    def test_unsafe_tar_member_rejected_after_identity_check(self):
        archive, manifest = write_release(self.directory, fixture_files(), bad_link=True)
        with self.assertRaisesRegex(QlibArchiveError, "unsafe archive member"):
            inspect_release(archive, manifest, "2000-01-06")

    def test_malformed_binary_rejected(self):
        files = fixture_files()
        files["qlib_bin/features/sh600000/close.day.bin"] = feature(2, 10, 11)
        archive, manifest = write_release(self.directory, files)
        with self.assertRaisesRegex(QlibArchiveError, "exceeds day calendar"):
            inspect_release(archive, manifest, "2000-01-06")
        with self.assertRaisesRegex(QlibArchiveError, "float32 length"):
            read_feature(b"bad", 3)

    def test_calendar_and_instrument_parsers_reject_ambiguous_rows(self):
        with self.assertRaisesRegex(QlibArchiveError, "sorted and unique"):
            parse_calendar(b"2000-01-05\n2000-01-04\n")
        with self.assertRaisesRegex(QlibArchiveError, "overlapping"):
            parse_instruments(
                b"SH600000\t2000-01-04\t2000-01-05\n"
                b"SH600000\t2000-01-05\t2000-01-06\n"
            )


if __name__ == "__main__":
    unittest.main()
