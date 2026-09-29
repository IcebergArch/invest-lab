from __future__ import annotations

import math
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.qlib_archive import QlibArchiveError
from quant_lab.qlib_local import publish_release_tree
from quant_lab.qlib_stock_report import analyze_qlib_stock
from test_qlib_archive import feature, fixture_files, write_release


def _calendar(count: int) -> list[date]:
    day = date(2000, 1, 6)
    result = []
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day)
        day -= timedelta(days=1)
    return sorted(result)


class QlibStockReportTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "published"

    def publish(self, files: dict[str, bytes]) -> Path:
        archive, manifest = write_release(self.directory, files)
        publish_release_tree(archive, manifest, "2000-01-06", self.root)
        return manifest

    def long_fixture(self, *, missing_close_index: int | None = None) -> dict[str, bytes]:
        dates = _calendar(70)
        day_text = "".join(f"{day.isoformat()}\n" for day in dates)
        files = fixture_files()
        files["qlib_bin/calendars/day.txt"] = day_text.encode()
        files["qlib_bin/calendars/day_future.txt"] = (day_text + "2000-01-07\n2000-01-10\n").encode()
        files["qlib_bin/instruments/all.txt"] = f"SH600000\t{dates[0]}\t{dates[-1]}\n".encode()
        for pool in ("csi300", "csi500", "csi800", "csi1000", "csiall"):
            files[f"qlib_bin/instruments/{pool}.txt"] = files["qlib_bin/instruments/all.txt"]
        for name in list(files):
            if name.startswith("qlib_bin/features/sz000003/"):
                del files[name]
        closes = [10 + index * 0.1 for index in range(len(dates))]
        if missing_close_index is not None:
            closes[missing_close_index] = math.nan
        files["qlib_bin/features/sh600000/close.day.bin"] = feature(0, *closes)
        files["qlib_bin/features/sh600000/factor.day.bin"] = feature(0, *([2.0] * len(dates)))
        files["qlib_bin/features/sh600000/amount.day.bin"] = feature(0, *([1_000.0] * len(dates)))
        return files

    def test_default_never_compares_adjusted_close_with_cost_or_assumes_amount_unit(self) -> None:
        manifest = self.publish(fixture_files())
        report = analyze_qlib_stock(self.root, manifest, "2000-01-06", "600000", cost_price=5)
        self.assertEqual("partial_data", report["status"])
        self.assertEqual("stock:600000.SH", report["instrument_id"])
        self.assertEqual("2000-01-06", report["asof"])
        self.assertEqual(11, report["adjusted_close"])
        self.assertIsNone(report["latest_close"])
        self.assertIsNone(report["cost_return"])
        self.assertIsNone(report["risk"]["metrics"]["avg_amount_20d_cny"])
        self.assertIsNone(report["forecast"])
        self.assertIsNone(report["backtest"])
        self.assertIn("不能直接用于成本比较", report["summary"])
        source = report["sources"][0]
        self.assertEqual("2000-01-06", source["release_tag"])
        self.assertTrue(source["manifest_sha256"].startswith("sha256:"))
        self.assertFalse(source["factor_verified"])
        self.assertFalse(source["amount_verified"])
        self.assertEqual(1, source["analyzed_contiguous_close_rows"])
        self.assertEqual(2, source["bar_count"])
        self.assertEqual("2000-01-04", source["first_valid_close_date"])
        self.assertEqual(1, source["full_history_missing_close_count"])

    def test_explicit_audits_enable_original_price_and_cny_amount(self) -> None:
        manifest = self.publish(self.long_fixture())
        report = analyze_qlib_stock(
            self.root, manifest, "2000-01-06", "SH600000", cost_price=8,
            factor_verified=True, amount_verified=True, amount_multiplier_cny=1_000,
        )
        self.assertEqual("ready", report["status"])
        self.assertAlmostEqual(16.9, report["adjusted_close"], places=5)
        self.assertAlmostEqual(8.45, report["latest_close"], places=5)
        self.assertAlmostEqual(8.45 / 8 - 1, report["cost_return"], places=5)
        metrics = report["risk"]["metrics"]
        self.assertAlmostEqual(1_000_000, metrics["avg_amount_20d_cny"])
        self.assertIsNotNone(metrics["return_60d"])
        self.assertIsNotNone(metrics["max_drawdown_60d"])
        self.assertTrue(report["sources"][0]["factor_verified"])
        self.assertEqual(1_000, report["sources"][0]["amount_multiplier_cny"])

    def test_missing_close_breaks_fixed_horizon_and_asof_excludes_future(self) -> None:
        manifest = self.publish(self.long_fixture(missing_close_index=55))
        report = analyze_qlib_stock(
            self.root, manifest, "2000-01-06", "600000",
            factor_verified=True, amount_verified=True, amount_multiplier_cny=1_000,
        )
        self.assertEqual("partial_data", report["status"])
        self.assertEqual(14, report["sources"][0]["analyzed_contiguous_close_rows"])
        self.assertIsNone(report["trend"]["metrics"]["return_20d"])
        self.assertIsNone(report["risk"]["metrics"]["annualized_volatility_20d"])
        self.assertIsNone(report["risk"]["metrics"]["avg_amount_20d_cny"])
        earlier = analyze_qlib_stock(self.root, manifest, "2000-01-06", "600000", asof=date(1999, 12, 31))
        self.assertLessEqual(earlier["asof"], "1999-12-31")
        self.assertLess(earlier["adjusted_close"], report["adjusted_close"])

    def test_invalid_verification_or_tampered_file_never_produces_report(self) -> None:
        manifest = self.publish(fixture_files())
        with self.assertRaisesRegex(ValueError, "成交额换算"):
            analyze_qlib_stock(self.root, manifest, "2000-01-06", "600000", amount_verified=True)
        with self.assertRaisesRegex(ValueError, "换算系数"):
            analyze_qlib_stock(self.root, manifest, "2000-01-06", "600000",
                               factor_verified=True, amount_verified=True)
        with self.assertRaises(TypeError):
            analyze_qlib_stock(self.root, manifest, "2000-01-06", "600000", factor_verified=1)
        self.assertEqual("unknown_stock", analyze_qlib_stock(
            self.root, manifest, "2000-01-06", "600001.SH")["status"])
        with (self.root / "qlib_bin/features/sh600000/close.day.bin").open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaises(QlibArchiveError):
            analyze_qlib_stock(self.root, manifest, "2000-01-06", "600000")

    def test_no_close_and_ended_instrument_interval_have_explicit_limits(self) -> None:
        files = fixture_files()
        files["qlib_bin/features/sh600000/close.day.bin"] = feature(0, math.nan, math.nan, math.nan)
        manifest = self.publish(files)
        missing = analyze_qlib_stock(self.root, manifest, "2000-01-06", "600000")
        self.assertEqual("no_bars", missing["status"])
        self.assertEqual("2000-01-06", missing["sources"][0]["release_tag"])
        self.assertEqual(3, missing["sources"][0]["read_window_missing_or_invalid_close_rows"])
        ended = analyze_qlib_stock(self.root, manifest, "2000-01-06", "000003.SZ")
        self.assertEqual("2000-01-05", ended["asof"])
        self.assertIn("早于 Release 截止日结束", ended["risk"]["summary"])


if __name__ == "__main__":
    unittest.main()
