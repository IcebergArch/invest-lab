from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.event_study import (
    EventDailyBar, MacroEvent, append_event_study_record,
    evaluate_event_study, load_event_registry, read_event_study_record,
    render_event_study_markdown, tag_event_regime_days,
)


START = date(2024, 1, 1)
DATES = tuple(START + timedelta(days=i) for i in range(50))
CHINA = timezone(timedelta(hours=8))


def event(position: int = 24, *, hour: int = 16) -> MacroEvent:
    return MacroEvent(
        event_id="policy.example.2024", title="合成政策公告",
        category="monetary_policy",
        announcement_at=datetime.combine(DATES[position], datetime.min.time(), CHINA)
        + timedelta(hours=hour),
        time_precision="minute", effective_date=DATES[position + 2],
        source_url="https://example.org/official-notice",
        publication_certainty="official_verified",
        availability_confidence="clock_verified", magnitude=-0.5,
        magnitude_unit="percentage_point",
    )


def bars(*, missing: tuple[str, int] | None = None,
         ineligible: tuple[str, int] | None = None) -> list[EventDailyBar]:
    items = []
    for key, asset_type, basis, unit in (
        ("index:000300.SH", "index", "index_points", "index_point"),
        ("stock:TEST.SH", "stock", "forward_adjusted", "CNY/share"),
        ("stock:OTHER.SZ", "stock", "forward_adjusted", "CNY/share"),
    ):
        for i, day in enumerate(DATES):
            if missing == (key, i):
                continue
            close = (200 + 0.5 * i if asset_type == "index" else
                     100 + i if key == "stock:TEST.SH" else 50 + 0.5 * i)
            items.append(EventDailyBar(
                instrument_id=key, asset_type=asset_type, trade_date=day,
                close=close, price_basis=basis, price_unit=unit,
                source_id="test_source", dataset_id="fixed_fixture",
                payload_hash="a" * 64,
                volume_shares=(200.0 if i >= 25 else 100.0)
                if asset_type == "stock" else None,
                amount_cny=(2000.0 if i >= 25 else 1000.0)
                if asset_type == "stock" else None,
                tradability=("ineligible" if ineligible == (key, i) else
                             "tradable" if asset_type == "stock" else "unknown"),
                observed_at=datetime(2024, 3, 1, tzinfo=timezone.utc),
            ))
    return items


def evaluate(*, cases: list[MacroEvent] | None = None,
             data: list[EventDailyBar] | None = None,
             asof: date = DATES[-1]):
    return evaluate_event_study(
        cases if cases is not None else [event()],
        data if data is not None else bars(),
        benchmark_id="index:000300.SH", asof=asof,
        cohorts={"synthetic_group": ["stock:TEST.SH", "stock:OTHER.SZ"]},
    )


class EventStudyTests(unittest.TestCase):
    def test_after_close_uses_next_full_session_and_fixed_volume_baseline(self) -> None:
        result = evaluate()
        item = result["events"][0]
        self.assertEqual(DATES[25].isoformat(), item["reaction_date"])
        observed = item["announcement_day_response"]
        self.assertAlmostEqual(124 / 123 - 1,
                               observed["stocks"]["stock:TEST.SH"]["return"])
        post0 = item["stocks"]["stock:TEST.SH"]["windows"]["post_0"]
        self.assertAlmostEqual(125 / 124 - 1, post0["return"])
        self.assertAlmostEqual(125 / 124 - 212.5 / 212,
                               post0["excess_return"])
        self.assertEqual(2.0, post0["volume_ratio_to_control"])
        self.assertEqual(2.0, post0["amount_ratio_to_control"])
        self.assertEqual("tradable", post0["tradability"])
        self.assertEqual(2, item["cohorts"]["synthetic_group"]["windows"]["post_5"]["complete_count"])
        self.assertFalse(result["causal_effect_estimated"])
        self.assertFalse(result["intrinsic_value_assessed"])
        self.assertEqual(26, result["regime_slices"]["stress_session_count"])
        self.assertEqual(24, result["regime_slices"]["core_session_count"])
        self.assertEqual(5, result["regime_slices"]["events"][0]["pre_reaction_hindsight_count"])
        self.assertTrue(result["regime_slices"]["retrospective_diagnostic_only"])

    def test_date_only_and_intraday_start_next_session_but_preopen_starts_today(self) -> None:
        date_only = replace(event(), announcement_at=DATES[24], time_precision="date",
                            availability_confidence="date_verified")
        intraday = replace(event(), event_id="intraday", announcement_at=(
            datetime(2024, 1, 25, 10, 30, tzinfo=CHINA)))
        preopen = replace(event(), event_id="preopen", announcement_at=(
            datetime(2024, 1, 25, 9, 0, tzinfo=CHINA)))
        uncertain_clock = replace(preopen, event_id="clock_uncertain",
                                  availability_confidence="date_verified")
        result = evaluate(cases=[date_only, intraday, preopen, uncertain_clock])
        self.assertEqual([DATES[25].isoformat(), DATES[25].isoformat(),
                          DATES[24].isoformat(), DATES[25].isoformat()],
                         [item["reaction_date"] for item in result["events"]])

    def test_missing_session_and_ineligible_state_are_explicit(self) -> None:
        missing = evaluate(data=bars(missing=("stock:TEST.SH", 27)))
        item = missing["events"][0]["stocks"]["stock:TEST.SH"]["windows"]["post_5"]
        self.assertEqual("missing_bar", item["status"])
        self.assertEqual([DATES[27].isoformat()], item["missing_dates"])
        self.assertIsNone(item["return"])
        self.assertIsNone(item["excess_return"])
        ineligible = evaluate(data=bars(ineligible=("stock:TEST.SH", 25)))
        item = ineligible["events"][0]["stocks"]["stock:TEST.SH"]["windows"]["post_0"]
        self.assertEqual("ready", item["status"])
        self.assertEqual("ineligible", item["tradability"])

    def test_future_and_unverified_events_have_no_backtest_windows(self) -> None:
        future = replace(event(), event_id="future", announcement_at=(
            datetime.combine(DATES[30], datetime.min.time(), CHINA)
            + timedelta(hours=8)))
        unverified = replace(event(), event_id="unverified",
                             publication_certainty="unverified")
        result = evaluate(cases=[future, unverified], asof=DATES[29])
        self.assertEqual("not_available_asof", result["events"][0]["status"])
        self.assertEqual("unverified_publication_time", result["events"][1]["status"])
        self.assertEqual({}, result["events"][0]["stocks"])
        self.assertIsNone(result["events"][0]["reaction_date"])
        self.assertEqual(DATES[29].isoformat(), result["data_asof"])
        self.assertEqual(0, result["regime_slices"]["excluded_core_session_count"])

    def test_after_close_with_no_future_session_still_reports_observed_announcement_day(self) -> None:
        result = evaluate(asof=DATES[24])
        item = result["events"][0]
        self.assertEqual("no_reaction_session_by_asof", item["status"])
        self.assertAlmostEqual(124 / 123 - 1,
                               item["announcement_day_response"]["stocks"]["stock:TEST.SH"]["return"])
        self.assertEqual(0, result["regime_slices"]["stress_session_count"])
        self.assertIn("公告日已观察到的指数收益", render_event_study_markdown(result))

    def test_stress_tagging_is_union_of_fixed_windows(self) -> None:
        overlapping = replace(event(), event_id="overlap", announcement_at=(
            datetime.combine(DATES[26], datetime.min.time(), CHINA)
            + timedelta(hours=16)))
        tagged = tag_event_regime_days([event(), overlapping], DATES, asof=DATES[-1])
        self.assertEqual(28, tagged["stress_session_count"])
        self.assertGreater(tagged["overlap_session_count"], 0)
        self.assertEqual(22, tagged["core_session_count"])
        self.assertFalse(tagged["future_unscheduled_events_avoidable"])

    def test_rejects_mixed_stock_basis_and_duplicate_dates(self) -> None:
        data = bars()
        position = next(i for i, item in enumerate(data)
                        if item.instrument_id == "stock:OTHER.SZ")
        data[position] = replace(data[position], price_basis="qlib_adjusted",
                                 price_unit="source_normalized")
        with self.assertRaisesRegex(ValueError, "inconsistent price basis"):
            evaluate(data=data)
        data = bars()
        data.append(data[-1])
        with self.assertRaisesRegex(ValueError, "duplicate instrument/date"):
            evaluate(data=data)

    def test_report_and_append_only_archive_verify_both_files(self) -> None:
        result = evaluate()
        report = render_event_study_markdown(result)
        self.assertIn("A 股政策事件研究", report)
        self.assertIn("公告日已观察到的指数收益", report)
        self.assertIn("fundamentals", json.dumps(result, ensure_ascii=False))
        with tempfile.TemporaryDirectory() as folder:
            first = append_event_study_record(folder, result)
            second = append_event_study_record(folder, result)
            self.assertNotEqual(first["json_path"], second["json_path"])
            saved = read_event_study_record(first["json_path"])
            self.assertEqual(result, saved["payload"])
            self.assertEqual(0o600, Path(first["json_path"]).stat().st_mode & 0o777)
            markdown_path = Path(first["report_path"])
            markdown_path.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "integrity"):
                read_event_study_record(first["json_path"])

    def test_registry_keeps_date_precision_without_inventing_time(self) -> None:
        payload = [replace(event(), announcement_at=DATES[24],
                           time_precision="date",
                           availability_confidence="date_verified").to_record()]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            cases = load_event_registry(path)
        self.assertEqual(DATES[24], cases[0].announcement_at)
        self.assertEqual("date", cases[0].time_precision)
        with self.assertRaisesRegex(ValueError, "Asia/Shanghai"):
            replace(event(), announcement_at=datetime(2024, 1, 25, 8,
                                                       tzinfo=timezone.utc)).validate()


if __name__ == "__main__":
    unittest.main()
