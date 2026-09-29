from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_lab.baostock_backfill import (
    BackfillConfig, BackfillWindow, _record_window, archive_status, parser,
    main, plan_backfill, run_backfill,
)
from quant_lab.baostock_source import (
    BaoStockSource, read_baostock_historical_snapshot,
    write_baostock_historical_snapshot,
)
from quant_lab.models import DailyBar, Instrument
from quant_lab.storage import MarketStore


FIELDS = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
DAILY = ["date", "code", "open", "high", "low", "close", "volume", "amount",
         "adjustflag", "tradestatus", "isST"]


class Result:
    error_code = "0"
    error_msg = "success"

    def __init__(self, fields, rows, *, error_code="0", error_msg="success"):
        self.fields = fields
        self.rows = rows
        self.error_code = error_code
        self.error_msg = error_msg
        self.index = -1

    def next(self):
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return [self.rows[self.index].get(key, "") for key in self.fields]


def listing(code, *, status="1", ipo="2000-01-01", out=""):
    return dict(code=code, code_name=code, ipoDate=ipo, outDate=out,
                type="1", status=status)


def day(code, day, *, is_st="0"):
    return dict(date=day, code=code, open="10", high="11", low="9", close="10",
                volume="1000000", amount="10000000", adjustflag="3",
                tradestatus="1", isST=is_st)


class Client:
    def __init__(self, basics, daily_rows, failing_start=None):
        self.basics = basics
        self.daily_rows = daily_rows
        self.failing_start = failing_start
        self.logins = 0
        self.logouts = 0
        self.queries = []

    def login(self):
        self.logins += 1
        return Result([], [])

    def logout(self):
        self.logouts += 1
        return Result([], [])

    def query_stock_basic(self):
        return Result(FIELDS, self.basics)

    def query_history_k_data_plus(self, code, fields, **kwargs):
        self.queries.append((code, kwargs))
        if kwargs["start_date"] == self.failing_start:
            return Result(DAILY, [], error_code="100", error_msg="transient network failure")
        rows = [row for row in self.daily_rows
                if row["code"] == code
                and kwargs["start_date"] <= row["date"] <= kwargs["end_date"]]
        return Result(DAILY, rows)


class FakeClock:
    def __init__(self):
        self.value = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class EastMoneyFake:
    def __init__(self):
        self.calls = []

    def fetch(self, instrument, start, end):
        self.calls.append((instrument.provider_code, start, end))
        return [DailyBar(instrument.instrument_id, start, 10, 11, 9, 10,
                         1_000_000, 10_000_000, "lot", "CNY", "none",
                         "eastmoney_kline", "payload")]


class HistoricalBackfillTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.store = MarketStore(self.directory / "raw-history.sqlite3")
        self.client = Client([
            listing("sh.600000"),
            listing("sz.000001", status="0", ipo="2001-06-01", out="2003-12-31"),
            listing("bj.920001"),
            dict(listing("sh.000001"), type="2"),
        ], [day("sh.600000", "2023-12-29"), day("sh.600000", "2024-01-02")])
        self.source = BaoStockSource(self.client, provider_version="test-0.9.4")
        self.snapshot = self.source.list_historical_coverage(minimum_count=2)

    def test_historical_catalogue_keeps_inactive_and_listing_dates(self):
        self.assertEqual(2, len(self.snapshot.listings))
        self.assertEqual(1, self.snapshot.bse_excluded_count)
        inactive = next(item for item in self.snapshot.listings if item.provider_status == "0")
        self.assertEqual("2001-06-01", inactive.ipo_date)
        self.assertEqual("2003-12-31", inactive.out_date)
        path = write_baostock_historical_snapshot(self.snapshot, self.directory / "catalogue.json")
        self.assertEqual(self.snapshot, read_baostock_historical_snapshot(path))
        data = json.loads(path.read_text())
        data["listings"][0]["provider_status"] = "1"
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            read_baostock_historical_snapshot(path)

    def test_one_login_for_two_calendar_buckets_throttle_resume_and_incremental_tail(self):
        config = BackfillConfig(date(2023, 12, 28), date(2024, 1, 2),
                                max_windows=2, min_interval_seconds=1.5)
        clock = FakeClock()
        logins_before = self.client.logins
        first = run_backfill(
            self.store, self.snapshot, config, ["stock:600000.SH"],
            bao_source=self.source, sleep=clock.sleep, monotonic=clock.monotonic,
        )
        self.assertIsNone(first.error)
        self.assertEqual(2, first.completed_window_count)
        self.assertEqual(2, first.bar_count)
        self.assertEqual(2, first.status_count)
        self.assertEqual(logins_before + 1, self.client.logins)
        self.assertEqual(1, len(clock.sleeps))
        self.assertGreaterEqual(clock.sleeps[0], 1.5)
        self.assertEqual(["3", "3"], [call[1]["adjustflag"] for call in self.client.queries])
        with self.store.connect() as connection:
            states = connection.execute("SELECT status FROM backfill_windows").fetchall()
            bars = connection.execute("SELECT source_id,adjustment,amount_unit FROM daily_bars").fetchall()
        self.assertEqual(["success", "success"], [row["status"] for row in states])
        self.assertTrue(all(row["adjustment"] == "none" and row["amount_unit"] == "CNY"
                            for row in bars))
        second = run_backfill(self.store, self.snapshot, config, ["stock:600000.SH"],
                              bao_source=self.source)
        self.assertIsNone(second.run_id)
        self.assertEqual(2, len(self.client.queries))
        extended = BackfillConfig(date(2023, 12, 28), date(2024, 1, 5), max_windows=2)
        plan = plan_backfill(self.store, self.snapshot, extended, ["stock:600000.SH"])
        self.assertEqual(1, len(plan.windows))
        self.assertEqual(date(2024, 1, 3), plan.windows[0].start)
        self.assertEqual(date(2024, 1, 5), plan.windows[0].end)

    def test_failed_window_checkpoints_and_cools_down_without_losing_success(self):
        self.client.failing_start = "2024-01-01"
        config = BackfillConfig(date(2023, 12, 28), date(2024, 1, 2), max_windows=2)
        result = run_backfill(self.store, self.snapshot, config, ["stock:600000.SH"],
                              bao_source=self.source, sleep=FakeClock().sleep)
        # Recent-first ordering hits the 2024 failure before 2023; no bars are
        # claimed, and the next invocation defers the failed window.
        self.assertEqual(1, result.failed_window_count)
        self.assertEqual(0, result.bar_count)
        with self.store.connect() as connection:
            row = connection.execute("SELECT status,attempts,last_error FROM backfill_windows").fetchone()
            self.assertEqual("failed", row["status"])
            self.assertEqual(1, row["attempts"])
            self.assertIn("transient network failure", row["last_error"])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0])
        plan = plan_backfill(self.store, self.snapshot, config, ["stock:600000.SH"])
        self.assertEqual(1, plan.deferred_failure_count)
        self.assertEqual(1, len(plan.windows))
        self.assertEqual(date(2023, 12, 28), plan.windows[0].start)
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        retry = plan_backfill(self.store, self.snapshot, config,
                              ["stock:600000.SH"], now=later)
        self.assertEqual(0, retry.deferred_failure_count)
        self.assertEqual(2, len(retry.windows))

    def test_ipo_out_bounds_and_eastmoney_source_are_explicit(self):
        config = BackfillConfig(date(2000, 1, 1), date(2005, 12, 31), max_windows=10)
        plan = plan_backfill(self.store, self.snapshot, config, ["stock:000001.SZ"])
        self.assertEqual(2, len(plan.windows))
        self.assertEqual({2001, 2003}, {item.start.year for item in plan.windows})
        east_store = MarketStore(self.directory / "eastmoney.sqlite3")
        east = EastMoneyFake()
        east_config = BackfillConfig(date(2026, 9, 28), date(2026, 9, 28),
                                     source="eastmoney", max_windows=1)
        result = run_backfill(east_store, self.snapshot, east_config,
                              ["stock:600000.SH"], eastmoney_source=east)
        self.assertEqual(1, result.completed_window_count)
        self.assertEqual(0, result.status_count)
        self.assertEqual("1.600000", east.calls[0][0])
        with east_store.connect() as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM baostock_daily_status").fetchone()[0])

    def test_refuses_to_mix_archive_with_existing_main_source(self):
        self.store.initialize()
        instrument = Instrument("stock:600000.SH", "600000.SH", "x", "stock",
                                "focus_stock", "tencent_kline", "sh600000", adjustment="none")
        self.store.upsert_instruments([instrument])
        self.store.upsert_bars([DailyBar(instrument.instrument_id, date(2026, 9, 28),
                                         10, 11, 9, 10, 1, None, "lot", "unavailable",
                                         "none", "tencent_kline", "hash")])
        config = BackfillConfig(date(2026, 9, 28), date(2026, 9, 28))
        with self.assertRaisesRegex(ValueError, "dedicated DB"):
            plan_backfill(self.store, self.snapshot, config, ["stock:600000.SH"])

    def test_cli_can_explicitly_override_failure_cooldown(self):
        args = parser().parse_args([
            "run", "--snapshot", "history.json", "--db", "history.sqlite3",
            "--end", "2026-09-28", "--retry-failed-after-seconds", "0",
        ])
        self.assertEqual(0.0, args.retry_failed_after_seconds)

    def test_batch_result_file_stays_machine_readable_with_provider_stdout(self):
        snapshot_path = write_baostock_historical_snapshot(
            self.snapshot, self.directory / "snapshot.json")
        result_path = self.directory / "progress" / "last-batch.json"
        fake_result = SimpleNamespace(
            error=None, to_dict=lambda: {"remaining_window_count": 0, "completed_window_count": 1})

        def noisy_run(*_args, **_kwargs):
            print("login success")
            return fake_result

        with patch("quant_lab.baostock_backfill.run_backfill", side_effect=noisy_run), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["run", "--snapshot", str(snapshot_path),
                         "--db", str(self.store.path), "--end", "2026-09-28",
                         "--result-json", str(result_path)])
        self.assertEqual(0, code)
        self.assertIn("login success", output.getvalue())
        self.assertEqual(0, json.loads(result_path.read_text())["remaining_window_count"])

    def test_existing_annual_checkpoints_are_subtracted_from_three_year_bucket(self):
        config = BackfillConfig(date(2024, 1, 1), date(2026, 9, 28), max_windows=10)
        plan_backfill(self.store, self.snapshot, config, ["stock:600000.SH"])
        self.store.start_run("old-annual-run", date(2024, 1, 1), date(2026, 9, 28), ["test"])
        for year, right in ((2024, date(2024, 12, 31)),
                            (2026, date(2026, 9, 28))):
            _record_window(self.store, self.snapshot, "baostock",
                           BackfillWindow("stock:600000.SH", date(year, 1, 1), right),
                           "old-annual-run", (), (), "success")
        self.store.finish_run("old-annual-run", "success", 1, 0)
        plan = plan_backfill(self.store, self.snapshot, config, ["stock:600000.SH"])
        self.assertEqual(1, len(plan.windows))
        self.assertEqual(date(2025, 1, 1), plan.windows[0].start)
        self.assertEqual(date(2025, 12, 31), plan.windows[0].end)

    def test_read_only_status_reports_catalogue_rows_windows_and_latest_run(self):
        config = BackfillConfig(date(2024, 1, 1), date(2024, 1, 2), max_windows=1)
        result = run_backfill(self.store, self.snapshot, config,
                              ["stock:600000.SH"], bao_source=self.source)
        before = self.store.path.stat().st_mtime_ns
        status = archive_status(self.store.path)
        self.assertEqual(self.snapshot.snapshot_id, status["catalogue"]["snapshot_id"])
        self.assertEqual(2, status["catalogue"]["listing_count"])
        self.assertEqual(result.run_id, status["latest_run"]["run_id"])
        self.assertEqual(1, status["bars_by_source"][0]["stock_count"])
        self.assertEqual(1, status["bars_by_source"][0]["bar_count"])
        self.assertEqual(1, status["windows"]["success"]["count"])
        self.assertEqual(0, status["windows"]["failed"]["count"])
        self.assertEqual(before, self.store.path.stat().st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
