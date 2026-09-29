from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.baostock_source import (
    BaoStockSource,
    read_baostock_snapshot,
    sync_baostock_pilot,
    write_baostock_snapshot,
)
from quant_lab.storage import MarketStore


class FakeResult:
    def __init__(self, fields, rows, *, error_code="0", error_msg="success"):
        self.fields = list(fields)
        self.rows = list(rows)
        self.error_code = error_code
        self.error_msg = error_msg
        self.position = -1

    def next(self):
        self.position += 1
        return self.position < len(self.rows)

    def get_row_data(self):
        return [self.rows[self.position].get(field, "") for field in self.fields]


BASIC_FIELDS = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
DAILY_FIELDS = [
    "date", "code", "open", "high", "low", "close", "volume", "amount",
    "adjustflag", "tradestatus", "isST",
]


def basic(code, name, type_="1", status="1"):
    return dict(code=code, code_name=name, ipoDate="2020-01-01", outDate="",
                type=type_, status=status)


def daily(code, day="2026-09-28", **updates):
    row = dict(date=day, code=code, open="10.0", high="11.0", low="9.0",
               close="10.5", volume="1000000", amount="10500000",
               adjustflag="2", tradestatus="1", isST="0")
    row.update(updates)
    return row


class FakeClient:
    def __init__(self, basic_rows, daily_rows=None, *, daily_error=None):
        self.basic_rows = basic_rows
        self.daily_rows = daily_rows or {}
        self.daily_error = daily_error
        self.calls = []
        self.login_count = 0
        self.logout_count = 0

    def login(self):
        self.login_count += 1
        return FakeResult([], [])

    def logout(self):
        self.logout_count += 1
        return FakeResult([], [])

    def query_stock_basic(self):
        self.calls.append(("basic",))
        return FakeResult(BASIC_FIELDS, self.basic_rows)

    def query_history_k_data_plus(self, code, fields, **kwargs):
        self.calls.append(("daily", code, fields, kwargs))
        if self.daily_error:
            return FakeResult(DAILY_FIELDS, [], error_code="100", error_msg=self.daily_error)
        return FakeResult(DAILY_FIELDS, self.daily_rows.get(code, []))


class BaoStockSourceTest(unittest.TestCase):
    def source(self, client):
        return BaoStockSource(client, provider_version="test-0.9.4")

    def test_current_list_filters_status_type_and_non_a_codes(self):
        rows = [
            basic("sh.600000", "浦发银行"),
            basic("sz.002475", "立讯精密"),
            basic("sh.600001", "退市", status="0"),
            basic("sh.000001", "指数", type_="2"),
            basic("sh.900901", "B股"),
            basic("bj.920779", "北交所"),
        ]
        client = FakeClient(rows)
        snapshot = self.source(client).list_current(minimum_count=2)
        self.assertEqual(6, snapshot.source_row_count)
        self.assertEqual(2, len(snapshot.instruments))
        self.assertEqual(2, snapshot.excluded_non_a_count)
        self.assertEqual(
            {"stock:600000.SH", "stock:002475.SZ"},
            {item.instrument_id for item in snapshot.instruments},
        )
        focus = next(item for item in snapshot.instruments if item.symbol == "002475.SZ")
        self.assertEqual("focus_stock", focus.family)
        self.assertEqual("sz.002475", focus.provider_code)
        self.assertEqual("qfq", focus.adjustment)
        self.assertEqual(1, client.login_count)
        self.assertEqual(1, client.logout_count)

    def test_snapshot_file_is_content_checked(self):
        snapshot = self.source(FakeClient([basic("sh.600000", "浦发银行")])).list_current(
            minimum_count=1
        )
        with tempfile.TemporaryDirectory() as directory:
            path = write_baostock_snapshot(snapshot, Path(directory) / "list.json")
            self.assertEqual(snapshot, read_baostock_snapshot(path))
            data = json.loads(path.read_text(encoding="utf-8"))
            data["instruments"][0]["name"] = "被篡改"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ID mismatch"):
                read_baostock_snapshot(path)

    def test_short_or_duplicate_list_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "returned only"):
            self.source(FakeClient([basic("sh.600000", "浦发银行")])).list_current(
                minimum_count=2
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.source(FakeClient([basic("sh.600000", "浦发银行")] * 2)).list_current(
                minimum_count=1
            )

    def test_fetch_uses_qfq_and_preserves_cny_amount_and_status(self):
        client = FakeClient(
            [basic("sz.002475", "立讯精密")],
            {"sz.002475": [daily("sz.002475")]},
        )
        source = self.source(client)
        item = source.list_current(minimum_count=1).instruments[0]
        batch = source.fetch(item, date(2026, 9, 24), date(2026, 9, 28))
        self.assertEqual(1, len(batch.bars))
        self.assertEqual("share", batch.bars[0].volume_unit)
        self.assertEqual("CNY", batch.bars[0].amount_unit)
        self.assertEqual(10_500_000, batch.bars[0].amount)
        self.assertEqual("qfq", batch.bars[0].adjustment)
        self.assertTrue(batch.statuses[0].recommendable)
        self.assertEqual("2", client.calls[-1][3]["adjustflag"])
        self.assertEqual("d", client.calls[-1][3]["frequency"])
        self.assertIn("tradestatus,isST", client.calls[-1][2])
        self.assertEqual(2, client.login_count)
        self.assertEqual(2, client.logout_count)

    def test_st_and_halt_days_cannot_pass_liquidity_gate(self):
        client = FakeClient(
            [basic("sh.600000", "浦发银行")],
            {"sh.600000": [
                daily("sh.600000", "2026-09-24"),
                daily("sh.600000", "2026-09-25", isST="1"),
                daily("sh.600000", "2026-09-28", tradestatus="0", amount="", volume="", open="", high="", low="", close=""),
            ]},
        )
        source = self.source(client)
        item = source.list_current(minimum_count=1).instruments[0]
        batch = source.fetch(item, date(2026, 9, 24), date(2026, 9, 28))
        self.assertEqual(2, len(batch.bars))
        self.assertEqual(3, len(batch.statuses))
        self.assertEqual("ineligible", batch.bars[-1].amount_unit)
        self.assertIsNone(batch.bars[-1].amount)
        self.assertFalse(batch.statuses[-1].has_valid_bar)
        self.assertFalse(any(item.recommendable for item in batch.statuses[1:]))

    def test_unknown_status_or_adjustment_fails_closed(self):
        for changed in ({"isST": ""}, {"tradestatus": "9"}, {"adjustflag": "3"}):
            with self.subTest(changed=changed):
                client = FakeClient(
                    [basic("sh.600000", "浦发银行")],
                    {"sh.600000": [daily("sh.600000", **changed)]},
                )
                source = self.source(client)
                item = source.list_current(minimum_count=1).instruments[0]
                with self.assertRaises(ValueError):
                    source.fetch(item, date(2026, 9, 24), date(2026, 9, 28))

    def test_pilot_is_bounded_and_records_lineage_and_status(self):
        client = FakeClient(
            [basic("sh.600000", "浦发银行"), basic("sz.002475", "立讯精密")],
            {"sh.600000": [daily("sh.600000", isST="1")],
             "sz.002475": [daily("sz.002475")]},
        )
        source = self.source(client)
        snapshot = source.list_current(minimum_count=2)
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "pilot.sqlite3"
            store = MarketStore(db)
            with self.assertRaisesRegex(ValueError, "1..30"):
                sync_baostock_pilot(store, snapshot, [], date(2026, 9, 1), date(2026, 9, 28), source=source)
            self.assertFalse(db.exists())
            with self.assertRaisesRegex(ValueError, "550"):
                sync_baostock_pilot(store, snapshot, ["stock:600000.SH"],
                                    date(2025, 1, 1), date(2026, 9, 28), source=source)
            result = sync_baostock_pilot(
                store, snapshot, ["stock:600000.SH", "stock:002475.SZ"],
                date(2026, 9, 1), date(2026, 9, 28), source=source,
            )
            self.assertEqual(2, result.row_count)
            self.assertEqual(2, result.status_count)
            self.assertEqual(("stock:002475.SZ",), result.recommendable_ids)
            with store.connect() as connection:
                run = connection.execute(
                    "SELECT status,groups_json,row_count FROM sync_runs WHERE run_id=?",
                    (result.run_id,),
                ).fetchone()
                statuses = connection.execute(
                    "SELECT instrument_id,is_st,tradestatus,raw_amount_cny,run_id "
                    "FROM baostock_daily_status ORDER BY instrument_id"
                ).fetchall()
                blocked_bar = connection.execute(
                    "SELECT amount,amount_unit,source_id,run_id FROM daily_bars "
                    "WHERE instrument_id='stock:600000.SH'"
                ).fetchone()
            self.assertEqual("success", run["status"])
            self.assertIn(snapshot.snapshot_id, run["groups_json"])
            self.assertEqual(2, run["row_count"])
            self.assertEqual(1, statuses[1]["is_st"])
            self.assertEqual(result.run_id, statuses[0]["run_id"])
            self.assertIsNone(blocked_bar["amount"])
            self.assertEqual("ineligible", blocked_bar["amount_unit"])
            self.assertEqual(result.run_id, blocked_bar["run_id"])

    def test_failed_fetch_leaves_no_bars_and_records_failed_run(self):
        client = FakeClient([basic("sh.600000", "浦发银行")], daily_error="network failure")
        source = self.source(client)
        snapshot = source.list_current(minimum_count=1)
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "pilot.sqlite3")
            with self.assertRaisesRegex(RuntimeError, "network failure"):
                sync_baostock_pilot(
                    store, snapshot, ["stock:600000.SH"],
                    date(2026, 9, 24), date(2026, 9, 28), source=source,
                )
            with store.connect() as connection:
                run = connection.execute("SELECT status,row_count FROM sync_runs").fetchone()
                count = connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
            self.assertEqual("failed", run["status"])
            self.assertEqual(0, run["row_count"])
            self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
