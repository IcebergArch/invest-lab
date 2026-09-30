from __future__ import annotations

import io
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.api_server import QuantAPIService, ReadOnlyMarketStore, make_handler
from quant_lab.archive_publication import publish_checkpoint
from quant_lab.baostock_backfill import BACKFILL_VERSION
from quant_lab.forecast_benchmark import (BENCHMARK_VERSION as FOCUS_FORECAST_VERSION,
                                          append_benchmark_record)
from quant_lab.forecast_qlib import (BENCHMARK_VERSION as QLIB_FORECAST_VERSION,
                                     append_qlib_benchmark_record)
from quant_lab.forecast_stability import (StabilityCriteria, append_stability_record,
                                          evaluate_forecast_stability)
from quant_lab.group_behavior_pilot import (PILOT_VERSION, archive_group_behavior_record)
from quant_lab.models import DailyBar, Instrument
from quant_lab.north_star import score_backtest
from quant_lab.optimizer import OPTIMIZER_VERSION, append_optimization_record
from quant_lab.qlib_local import publish_release_tree
from quant_lab.raw_data_inventory import INVENTORY_VERSION
from quant_lab.storage import MarketStore
from quant_lab.strategy_registry import append_strategy_snapshot
from test_qlib_archive import fixture_files, write_release
from test_forecast_stability import benchmark as stability_benchmark


def invoke(service: QuantAPIService, method: str, path: str,
           body: bytes = b"", content_type: str = "application/json",
           origin: str | None = None) -> tuple[int, dict[str, str], dict[str, object]]:
    """Call the real HTTP handler with in-memory streams, without a socket."""
    handler = object.__new__(make_handler(service))
    handler.path = path
    handler.wfile = io.BytesIO()
    handler.rfile = io.BytesIO(body)
    handler.headers = {"Content-Length": str(len(body)), "Content-Type": content_type}
    if origin is not None:
        handler.headers["Origin"] = origin
    received: dict[str, object] = {"headers": {}}
    handler.send_response = lambda status: received.update(status=int(status))
    handler.send_header = lambda key, value: received["headers"].update({key: value})
    handler.end_headers = lambda: None
    if method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    return (int(received["status"]), received["headers"],
            json.loads(handler.wfile.getvalue()))


class QuantAPIContractTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "market.sqlite3"
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.writer = MarketStore(self.db)
        self.writer.initialize()
        self.service = QuantAPIService(self.writer, self.reports)

    def write_report(self, name: str, data: dict[str, object]) -> None:
        (self.reports / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_console_exposes_last_backfill_batch_without_claiming_worker_liveness(self) -> None:
        missing = invoke(self.service, "GET", "/api/console-status")[2]
        self.assertEqual("missing_batch", missing["historical_backfill_batch"]["status"])
        folder = self.root / "backfill"
        folder.mkdir()
        path = folder / "last-batch.json"
        batch = {
            "backfill_version": BACKFILL_VERSION, "run_id": "a" * 32,
            "source_id": "baostock_daily", "snapshot_id": "b" * 64,
            "requested_window_count": 10, "completed_window_count": 9,
            "empty_window_count": 1, "failed_window_count": 0,
            "remaining_window_count": 26289, "deferred_failure_count": 0,
            "bar_count": 6640, "status_count": 6640,
            "elapsed_seconds": 103.1, "error": None,
            "private_local_path": "/private/data/archive.sqlite3",
        }
        path.write_text(json.dumps(batch), encoding="utf-8")
        ready = invoke(self.service, "GET", "/api/console-status")[2]
        projected = ready["historical_backfill_batch"]
        self.assertEqual("ready", projected["status"])
        self.assertEqual("a" * 32, projected["last_batch"]["run_id"])
        self.assertEqual(26289, projected["last_batch"]["remaining_window_count"])
        self.assertTrue(projected["file_modified_at"].endswith("Z"))
        self.assertNotIn("worker_status", projected)
        self.assertNotIn("private_local_path", json.dumps(projected))
        batch["completed_window_count"] = 11
        path.write_text(json.dumps(batch), encoding="utf-8")
        invalid = invoke(self.service, "GET", "/api/console-status")[2]
        self.assertEqual("unavailable", invalid["historical_backfill_batch"]["status"])

    def test_console_projects_verified_stability_tiers_without_forecast_rows(self) -> None:
        data = stability_benchmark(bad_event=True)
        data["providers"][1]["per_stock"][0]["records"][0]["private_marker"] = "secret-forecast"
        report = evaluate_forecast_stability(
            data, "test-model", event_stress_days=["2021-02-03", "2022-02-03"],
            source_benchmark_record_id="benchmark-1", event_study_record_id="event-1",
            criteria=StabilityCriteria(min_core_samples=4, min_stocks=1, min_years=2,
                                       min_blocks=2, min_stress_samples=2,
                                       min_stress_blocks=2, min_stress_regimes=2,
                                       bootstrap_draws=100))
        saved = append_stability_record(self.root / "forecast-stability", report)
        code, _, body = invoke(self.service, "GET", "/api/console-status")
        self.assertEqual(200, code)
        stability = body["forecast_stability"]
        self.assertEqual("ready", stability["status"])
        self.assertEqual(saved["record_id"], stability["latest"][0]["record_id"])
        self.assertEqual("coarse_or_unstable", stability["latest"][0]["horizons"]["5"]["tier"])
        self.assertEqual(12, stability["latest"][0]["horizons"]["5"]["sample_count"])
        self.assertEqual(4, stability["latest"][0]["horizons"]["5"]["event_stress_count"])
        self.assertNotIn("secret-forecast", json.dumps(body))
        self.assertNotIn("per_stock", json.dumps(stability))
        altered = json.loads(Path(saved["json_path"]).read_text())
        altered["payload"]["horizons"]["5"]["tier"] = "broadly_stable"
        Path(saved["json_path"]).write_text(json.dumps(altered))
        bad = invoke(self.service, "GET", "/api/console-status")[2]["forecast_stability"]
        self.assertEqual("unavailable", bad["status"])
        self.assertEqual([], bad["latest"])

    def test_console_projects_group_behavior_summary_without_feature_rows(self) -> None:
        payload = {
            "pilot_version": PILOT_VERSION, "research_only": True,
            "institution_identity_available": False, "actual_net_buying_observed": False,
            "event_origin_policy": "last_close_before_verified_announcement_clock",
            "created_at": "2026-09-29T09:19:27+00:00", "calendar_end": "2026-09-28",
            "cohort_size": 40, "model": {"name": "origin-refit-standardized-ridge"},
            "reviewed_events": [{"event_id": f"policy-{i}"} for i in range(4)],
            "point_in_time_validated": False, "prospective_out_of_sample": False,
            "horizons": {"5": {
                "regular": {"summary": {"paired_count": 63, "candidate_origins": 70,
                                        "paired_coverage": 0.9,
                                        "mae_skill_vs_zero_return": -0.04387,
                                        "model_and_baseline_sample_keys_identical": True},
                            "records": [{"private_feature": "secret-feature"}]},
                "event_stress": {"summary": {"paired_count": 4,
                                             "model_and_baseline_sample_keys_identical": True}}},
            },
        }
        saved = archive_group_behavior_record(self.root / "group-behavior-experiments", payload)
        status, _, body = invoke(self.service, "GET", "/api/console-status")
        self.assertEqual(200, status)
        projected = body["group_behavior_pilot"]
        self.assertEqual("ready", projected["status"])
        self.assertEqual(saved["record_id"], projected["latest"]["record_id"])
        self.assertEqual(63, projected["latest"]["horizons"]["5"]["regular_paired_count"])
        self.assertEqual(4, projected["latest"]["horizons"]["5"]["policy_stress_paired_count"])
        self.assertNotIn("secret-feature", json.dumps(body))
        self.assertNotIn("records", json.dumps(projected))

    def test_console_projects_verified_forecast_archives_without_prediction_rows(self) -> None:
        provider = {
            "name": "random-walk", "model_id": "naive-last-close", "model_revision": "1",
            "status": "ready", "per_stock": [{"records": [{"private_marker": "row-secret"}]}],
            "pooled": {
                "5": {"status": "ready", "count": 21,
                      "mae_return_skill_vs_random_walk": 0.0,
                      "mae_return_skill_vs_random_walk_on_same_cases": 0.0,
                      "comparison_coverage": 1.0},
                "20": {"status": "ready", "count": 18,
                       "mae_return_skill_vs_random_walk": 0.0,
                       "mae_return_skill_vs_random_walk_on_same_cases": 0.0,
                       "comparison_coverage": 1.0},
            },
            "comparison_coverage": {
                "5": {"fraction": 1.0}, "20": {"fraction": 1.0},
            },
        }
        focus = {
            "benchmark_version": FOCUS_FORECAST_VERSION,
            "experiment_key_sha256": "e" * 64,
            "input_fingerprint_sha256": "a" * 64,
            "price_basis": "forward_adjusted",
            "data_start": "2021-01-04", "data_asof": "2026-09-29",
            "common_sessions": 1392, "universe": ["stock:603993.SH"],
            "configuration": {"min_context": 120, "step": 20},
            "research_only": True, "point_in_time_validated": False,
            "prospective_out_of_sample": False,
            "providers": [provider],
        }
        qlib = {
            **focus, "benchmark_version": QLIB_FORECAST_VERSION,
            "input_fingerprint_sha256": "b" * 64,
            "price_basis": "qlib_adjusted",
            "calendar_start": "2008-01-02", "calendar_end": "2026-09-28",
            "cohort_selection": {"selected_count": 40},
            "providers": [{**provider, "stocks_with_samples": 39}],
        }
        saved_focus = append_benchmark_record(self.root / "forecast-experiments", focus)
        saved_qlib = append_qlib_benchmark_record(self.root / "forecast-experiments-qlib", qlib)
        status, _, body = invoke(self.service, "GET", "/api/console-status")
        self.assertEqual(200, status)
        sources = {item["dataset"]: item for item in body["forecast_experiments"]["datasets"]}
        self.assertEqual("ready", sources["focus"]["status"])
        self.assertEqual(saved_focus["record_id"], sources["focus"]["latest"]["record_id"])
        self.assertEqual(21, sources["focus"]["latest"]["providers"][0]["horizons"]["5"]["sample_count"])
        self.assertEqual(1.0, sources["focus"]["latest"]["providers"][0]["horizons"]["5"]["coverage"])
        self.assertEqual(saved_qlib["record_id"], sources["qlib"]["latest"]["record_id"])
        self.assertEqual(40, sources["qlib"]["latest"]["stock_count"])
        self.assertNotIn("row-secret", json.dumps(body))
        self.assertNotIn("per_stock", json.dumps(body))

        newer = append_benchmark_record(self.root / "forecast-experiments", focus)
        file = Path(newer["path"])
        altered = json.loads(file.read_text(encoding="utf-8"))
        altered["payload"]["universe"] = ["stock:000001.SZ"]
        file.write_text(json.dumps(altered), encoding="utf-8")
        after = invoke(self.service, "GET", "/api/console-status")[2]
        self.assertEqual("partial", after["forecast_experiments"]["datasets"][0]["status"])
        self.assertEqual(saved_focus["record_id"],
                         after["forecast_experiments"]["datasets"][0]["latest"]["record_id"])

    def test_console_projects_saved_raw_inventory_and_rejects_changed_summary(self) -> None:
        def sqlite_source(role: str, bars: int, stock_count: int,
                          groups: list[dict[str, object]],
                          year_exchange: list[dict[str, object]],
                          zero_years: list[str]) -> dict[str, object]:
            historical = role == "baostock_archive"
            latest_run = {"run_id": "backfill-1", "status": "running"}
            status_fields = ({"status_rows": bars, "raw_amount_cny_observed_rows": 2,
                              "canonical_amount_null_but_raw_amount_observed_rows": 1}
                             if historical else None)
            catalogue = {"listing_count": 5} if historical else None
            summary = {"schema_version": "3", "groups": groups,
                       "year_exchange": year_exchange, "status_fields": status_fields,
                       "catalogue": catalogue, "window_counts": None,
                       "latest_run": latest_run}
            digest = hashlib.sha256(json.dumps(
                summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")).hexdigest()
            return {"status": "ready", "role": role, "schema_version": "3",
                    "snapshot": {"method": "single_sqlite_read_transaction",
                                 "quick_check": "ok", "sql_writes": False,
                                 "summary_sha256": digest},
                    "bar_groups": groups, "year_exchange_coverage": year_exchange,
                    "status_fields": status_fields, "catalogue": catalogue,
                    "backfill_window_counts": None, "latest_run": latest_run,
                    "stock_bar_rows": bars, "stock_instrument_count": stock_count,
                    "zero_stock_years": zero_years, "archive_complete": False,
                    "point_in_time_eligible": False}

        main_group = {"asset_type": "stock", "adjustment": "qfq",
                      "volume_unit": "lot", "amount_unit": "CNY", "bar_rows": 4,
                      "amount_observed_rows": 4, "amount_null_rows": 0}
        historical_group = {"asset_type": "stock", "adjustment": "none",
                            "volume_unit": "share", "amount_unit": "CNY", "bar_rows": 3,
                            "amount_observed_rows": 3, "amount_null_rows": 0}
        years = [{"year": str(year), "exchange": exchange,
                  "bar_rows": (1 if year == 2000 and exchange == "SH" else
                               2 if year == 2024 and exchange == "SZ" else 0)}
                 for year in range(2000, 2025) for exchange in ("SH", "SZ")]
        zero_years = [str(year) for year in range(2001, 2024)]
        fields = ("open", "high", "low", "close", "volume", "amount", "adjclose",
                  "change", "factor", "vwap")
        qlib = {"status": "ready", "price_basis": "qlib_adjusted",
                "field_coverage_is_file_presence_only": True,
                "valid_value_counts_require_binary_scan": True,
                "snapshot": {"method": "fixed_manifest_and_published_index",
                             "full_feature_content_reverified_by_inventory": False,
                             "archive_sha256": "sha256:" + "a" * 64},
                "instrument_intervals": {"instrument_count": 2},
                "calendar": {"first": "2000-01-04", "last": "2024-12-31",
                             "sessions": 6000},
                "features": {field: {"feature_file_count": 2,
                                     "valid_value_count": None,
                                     "unit": "source_native_unverified"}
                             for field in fields}}
        generated = datetime.now(timezone.utc)
        report = {"inventory_version": INVENTORY_VERSION,
                  "generated_at_utc": generated.isoformat(),
                  "sources": {
                      "main_market_store": sqlite_source("main_store", 4, 1,
                                                         [main_group], [], []),
                      "historical_baostock_archive": sqlite_source(
                          "baostock_archive", 3, 2, [historical_group], years,
                          zero_years),
                      "qlib_release": qlib,
                  }}
        folder = self.root / "data"
        folder.mkdir()
        path = folder / f"raw-data-inventory-{generated.date().isoformat()}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        body = invoke(self.service, "GET", "/api/console-status")[2]
        projected = body["raw_data_inventory"]
        self.assertEqual("ready", projected["status"])
        self.assertEqual(4, projected["main"]["bar_count"])
        self.assertEqual(3, projected["historical"]["bar_count"])
        self.assertEqual(23, len(projected["historical"]["zero_stock_years"]))
        self.assertEqual(2, projected["qlib"]["files_per_feature"])
        self.assertNotIn("bar_groups", json.dumps(projected))
        self.assertNotIn("instrument_intervals", json.dumps(projected))

        report["sources"]["historical_baostock_archive"]["bar_groups"][0]["bar_rows"] = 4
        path.write_text(json.dumps(report), encoding="utf-8")
        changed = invoke(self.service, "GET", "/api/console-status")[2]["raw_data_inventory"]
        self.assertEqual("partial", changed["status"])
        self.assertEqual("unavailable", changed["historical"]["status"])
        self.assertEqual("ready", changed["main"]["status"])

    def test_dashboard_returns_bounded_contract_without_full_stock_pool(self) -> None:
        self.write_report("market-pulse.json", {"asof": "2026-09-28", "groups": []})
        self.write_report("flow.json", {"reader_lines": ["大盘概况", "行业概况", "不能推断净流入"]})
        self.write_report("run.json", {"run_id": "report-run", "stock_signal_asof": "2026-09-28"})
        self.write_report("stock-shortlist.json", {
            "status": "blocked_universe_coverage", "reason": "覆盖不足", "asof": "2026-09-28",
            "universe_count": 3, "expected_universe_count": 5222, "data_ready_count": 1,
            "universe": [f"stock:{index:06d}.SZ" for index in range(25_000)],
            "excluded": [{"instrument_id": f"stock:{index:06d}.SZ", "reason": "行情缺失"}
                         for index in range(25_000)],
        })
        self.write_report("research.json", {"asof": "2026-09-28", "universe": ["stock:000001.SZ"],
                                            "backtest": {"metrics": {"max_drawdown": -0.1},
                                                         "equity_curve": list(range(100))}})
        self.write_report("forecast-momentum.json", {"status": "ready", "pooled": {},
                                                      "per_stock": list(range(100))})
        self.write_report("industry-validation.json", {"industry_data_asof": "2026-09-24",
                                                       "points": list(range(100))})
        self.write_report("recommendations.json", {"status": "blocked_insufficient_evidence",
                                                   "recommendations": []})
        status, headers, body = invoke(self.service, "GET", "/api/dashboard")
        self.assertEqual(200, status)
        self.assertEqual("application/json; charset=utf-8", headers["Content-Type"])
        self.assertEqual("2026-09-28", body["market"]["asof"])
        self.assertEqual("report-run", body["run"]["run_id"])
        self.assertEqual("blocked_insufficient_evidence", body["recommendations"]["status"])
        self.assertEqual(5222, body["stock_shortlist"]["expected_universe_count"])
        self.assertNotIn("universe", body["stock_shortlist"])
        self.assertNotIn("excluded", body["stock_shortlist"])
        self.assertNotIn("equity_curve", body["research"]["backtest"])
        self.assertNotIn("per_stock", body["forecast"])
        self.assertLess(len(json.dumps(body, ensure_ascii=False).encode("utf-8")), 2_000_000)

    def test_console_status_lists_actual_coverage_runs_and_private_journal_metadata(self) -> None:
        instrument = Instrument(
            "stock:000338.SZ", "000338.SZ", "潍柴动力", "stock", "test", "test_source",
            "0.000338", exchange="SZSE", adjustment="qfq",
        )
        self.writer.upsert_instruments([instrument])
        self.writer.upsert_bars([DailyBar(
            instrument.instrument_id, date(2026, 9, 29), 10, 11, 9, 10, 1000, 10000,
            "share", "CNY", "qfq", "test_source", "hash",
        )])
        self.writer.start_run("sync-1", date(2026, 9, 29), date(2026, 9, 29), ["stock"])
        self.writer.finish_run("sync-1", "success", 1, 1)
        self.write_report("run.json", {
            "run_id": "research-1", "generated_at": "2026-09-29T08:00:00+00:00",
            "market_broad_asof": "2026-09-29", "stock_signal_asof": "2026-09-29",
            "strategy_id": "sma-trend", "strategy_version": "1", "feature_version": "price-v1",
            "stock_universe_scope": "focus_stock_pilot_only", "stock_universe_count": 1,
            "stock_screen_status": "blocked_universe_coverage",
            "recommendation_status": "blocked_insufficient_evidence", "recommendation_count": 0,
            "cost_rate": 0.001, "execution_assumption": "next close",
            "backtest_metrics": {
                "total_return": 0.1, "cagr": 0.02, "annual_volatility": 0.2,
                "sharpe_rf0": 0.1, "max_drawdown": -0.3, "turnover": 2,
            },
        })
        journal_day = self.root / "research-journal" / "2026-09-29"
        journal_day.mkdir(parents=True)
        journal_payload = {"request": {"cost_price": 1234.56}, "report": {
            "instrument_id": instrument.instrument_id, "name": instrument.name,
            "asof": "2026-09-29", "status": "ready", "cost_price": 1234.56,
        }}
        journal_hash = hashlib.sha256(json.dumps(
            journal_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        (journal_day / "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa.json").write_text(json.dumps({
            "schema_version": 1,
            "record_id": "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
            "generated_at": "2026-09-29T08:00:00Z", "payload_sha256": journal_hash,
            "payload": journal_payload,
        }), encoding="utf-8")
        status, _headers, body = invoke(self.service, "GET", "/api/console-status")
        self.assertEqual(200, status)
        self.assertEqual("ready", body["status"])
        self.assertEqual("research-1", body["published_run"]["run_id"])
        self.assertEqual(1, body["market_store"]["groups"]["stock"]["instrument_count"])
        self.assertEqual("2026-09-29", body["market_store"]["groups"]["stock"]["last_date"])
        self.assertEqual("sync-1", body["market_store"]["latest_sync_runs"][0]["run_id"])
        self.assertEqual("aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
                         body["research_journal"]["recent"][0]["record_id"])
        self.assertNotIn("1234.56", json.dumps(body))
        self.assertNotIn("artifacts", body["published_run"])
        file = journal_day / "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa.json"
        tampered_record = json.loads(file.read_text(encoding="utf-8"))
        tampered_record["payload"]["report"]["name"] = "伪造名称"
        file.write_text(json.dumps(tampered_record, ensure_ascii=False), encoding="utf-8")
        self.assertEqual([], invoke(self.service, "GET", "/api/console-status")[2]
                         ["research_journal"]["recent"])

    def test_console_only_attaches_backtest_to_matching_published_manifest(self) -> None:
        metrics = {
            "total_return": 0.1, "cagr": 0.02, "annual_volatility": 0.2,
            "sharpe_rf0": 0.1, "max_drawdown": -0.3, "turnover": 2,
        }
        self.write_report("run.json", {
            "run_id": "research-1", "generated_at": "2026-09-29T08:00:00+00:00",
            "stock_signal_asof": "2026-09-29", "strategy_id": "sma-trend",
            "strategy_version": "1", "feature_version": "price-v1",
            "stock_universe_scope": "focus_stock_pilot_only", "stock_universe_count": 1,
            "cost_rate": 0.001, "execution_assumption": "next close",
            "backtest_metrics": metrics,
        })
        self.write_report("research.json", {
            "generated_at": "2026-09-29T07:59:00+00:00", "asof": "2026-09-29",
            "strategy_id": "sma-trend", "strategy_version": "1", "feature_version": "price-v1",
            "strategy_parameters": {"fast_window": 20, "slow_window": 60},
            "universe": ["stock:000338.SZ"], "cost_rate": 0.001,
            "execution_assumption": "next close", "limitations": ["仅三股试验"],
            "backtest": {"start": "2021-01-04", "end": "2026-09-29",
                         "observations": 1392, "metrics": metrics,
                         "prospective_out_of_sample": {"status": "pending", "matured_sessions": 0,
                                                       "reason": "尚无后验样本"}},
        })
        body = invoke(self.service, "GET", "/api/console-status")[2]
        strategy = body["strategies"][0]
        self.assertEqual("research-1", strategy["source_run_id"])
        self.assertEqual("available", strategy["backtest"]["status"])
        self.assertEqual("2021-01-04", strategy["backtest"]["start"])
        self.assertEqual(1392, strategy["backtest"]["observations"])
        self.assertEqual("pending", strategy["backtest"]["prospective_out_of_sample"]["status"])
        self.assertEqual("not_evaluated", body["rules"][0]["backtest_status"])
        research = json.loads((self.reports / "research.json").read_text())
        research["backtest"]["metrics"]["total_return"] = 999
        self.write_report("research.json", research)
        mismatch = invoke(self.service, "GET", "/api/console-status")[2]
        self.assertEqual("unavailable", mismatch["strategies"][0]["backtest"]["status"])

    def test_console_lists_hash_checked_strategy_snapshots_and_reports_tampering(self) -> None:
        directory = self.reports.parent / "strategy-runs"
        metrics = {"total_return": 0.1, "cagr": 0.02, "annual_volatility": 0.2,
                   "sharpe_rf0": 0.1, "max_drawdown": -0.3, "turnover": 2}
        for strategy_id in ("sma-trend", "equal-weight-hold"):
            append_strategy_snapshot(directory, {
                "generated_at": "2026-09-29T08:00:00+00:00", "asof": "2026-09-29",
                "strategy_id": strategy_id, "strategy_version": "1",
                "feature_version": "price-v1", "strategy_parameters": {},
                "universe": ["stock:000338.SZ", "stock:002475.SZ", "stock:002714.SZ"],
                "cost_rate": 0.001, "execution_assumption": "next close",
                "limitations": ["仅三股回看试验"],
                "backtest": {"start": "2021-01-04", "end": "2026-09-29",
                             "observations": 1392, "metrics": metrics,
                             "benchmarks": {"focus_equal_weight_hold": {
                                 "status": "available", "metrics": metrics}},
                             "input_fingerprint_sha256": "b" * 64,
                             "equity_curve": [{"date": "2026-09-29", "equity": 1.1}],
                             "prospective_out_of_sample": {"status": "pending",
                                                           "matured_sessions": 0}},
            }, origin="baseline_capture")
        body = invoke(self.service, "GET", "/api/console-status")[2]
        self.assertEqual("ready", body["strategy_archive"]["status"])
        self.assertEqual(2, body["strategy_archive"]["record_count"])
        self.assertEqual("ready", body["experiments"]["status"])
        self.assertEqual(2, len(body["experiments"]["recent"]))
        self.assertEqual("retrospective_research", body["experiments"]["recent"][0]["state"])
        observed = {item["strategy_id"]: item for item in body["strategies"]}
        self.assertTrue({"sma-trend", "equal-weight-hold"} <= set(observed))
        self.assertEqual("available", observed["sma-trend"]["backtest"]["status"])
        self.assertEqual(5.0, observed["sma-trend"]["north_star"]["score"])
        self.assertEqual("available", observed["equal-weight-hold"]["backtest"]["status"])
        self.assertEqual("implemented_unvalidated",
                         observed["cross-sectional-momentum"]["status"])
        factor_rows = {item["factor_id"]: item for item in body["factors"]}
        self.assertTrue({"sma", "momentum", "zscore", "overnight_gap",
                         "amihud_illiquidity", "ofi", "queue_imbalance"} <= set(factor_rows))
        self.assertEqual("data_required", factor_rows["ofi"]["availability_status"])
        self.assertFalse(factor_rows["amihud_illiquidity"]["active_for_decision"])
        rule_rows = {item["rule_id"]: item for item in body["rules"]}
        self.assertEqual("research_only_unvalidated",
                         rule_rows["gap_followthrough"]["deployment_status"])
        self.assertTrue(all("equity_curve" not in item["backtest"]
                            for item in body["strategies"]))
        file = next(directory.rglob("*.json"))
        record = json.loads(file.read_text(encoding="utf-8"))
        record["research"]["backtest"]["metrics"]["total_return"] = 99
        file.write_text(json.dumps(record), encoding="utf-8")
        tampered = invoke(self.service, "GET", "/api/console-status")[2]
        self.assertEqual("unavailable", tampered["strategy_archive"]["status"])
        self.assertEqual("unavailable", tampered["experiments"]["status"])

    def test_console_keeps_older_strategy_visible_after_over_100_newer_snapshots(self) -> None:
        directory = self.reports.parent / "strategy-runs"
        metrics = {"total_return": 0.1, "cagr": 0.02, "annual_volatility": 0.2,
                   "sharpe_rf0": 0.1, "max_drawdown": -0.3, "turnover": 2}

        def research(strategy_id: str) -> dict[str, object]:
            return {
                "generated_at": "2026-09-29T08:00:00+00:00", "asof": "2026-09-29",
                "strategy_id": strategy_id, "strategy_version": "1",
                "feature_version": "price-v1", "strategy_parameters": {},
                "universe": ["stock:000338.SZ", "stock:002475.SZ", "stock:002714.SZ"],
                "cost_rate": 0.001, "execution_assumption": "next close",
                "limitations": ["仅三股试验"],
                "backtest": {"start": "2021-01-04", "end": "2026-09-29",
                             "observations": 1392, "metrics": metrics,
                             "input_fingerprint_sha256": "b" * 64,
                             "equity_curve": [{"date": "2026-09-29", "equity": 1.1}]},
            }

        append_strategy_snapshot(directory, research("cross-sectional-momentum"),
                                 origin="baseline_capture")
        for _ in range(105):
            append_strategy_snapshot(directory, research("sma-trend"),
                                     origin="pipeline_run")
        body = invoke(self.service, "GET", "/api/console-status")[2]
        strategies = {item["strategy_id"]: item for item in body["strategies"]}
        self.assertEqual("available", strategies["cross-sectional-momentum"]["backtest"]["status"])
        self.assertEqual("baseline_capture", strategies["cross-sectional-momentum"]["origin"])
        self.assertTrue(all(item["strategy_id"] == "sma-trend"
                            for item in body["experiments"]["recent"]))
        self.assertEqual(106, body["strategy_archive"]["record_count"])
        self.assertEqual(30, body["strategy_archive"]["recent_record_count"])

    def test_feedback_post_links_saved_report_and_exposes_only_public_metadata(self) -> None:
        key = "stock:000338.SZ"
        self.writer.upsert_instruments([Instrument(
            key, "000338.SZ", "潍柴动力", "stock", "test", "test_source",
            "0.000338", exchange="SZSE", adjustment="qfq",
        )])
        self.writer.upsert_bars([DailyBar(
            key, date.today(), 10, 11, 9, 10, 1_000_000, 10_000_000,
            "share", "CNY", "qfq", "test_source", "hash",
        )])
        query = invoke(self.service, "POST", "/api/stock-report",
                       b'{"symbol":"000338","cost_price":12}')[2]
        record = query["research_record"]
        feedback = {
            "research_record_id": record["record_id"],
            "research_record_date": record["generated_at"][:10],
            "decision": "watch", "observed_date": date.today().isoformat(),
            "note": "个人备注：暂时不交易",
        }
        status, _headers, saved = invoke(
            self.service, "POST", "/api/feedback-case",
            json.dumps(feedback, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(200, status)
        self.assertEqual("saved", saved["status"])
        self.assertNotIn("path", saved)
        case_file = next((self.reports.parent / "feedback-cases").rglob("*.json"))
        archived = json.loads(case_file.read_text(encoding="utf-8"))
        self.assertEqual(record["record_id"], archived["payload"]["research_record_id"])
        self.assertEqual("个人备注：暂时不交易", archived["payload"]["note"])
        body = invoke(self.service, "GET", "/api/console-status")[2]
        self.assertEqual("ready", body["feedback_cases"]["status"])
        self.assertEqual(saved["case_id"], body["feedback_cases"]["recent"][0]["case_id"])
        self.assertEqual("pending", body["reviews"]["status"])
        self.assertNotIn("个人备注", json.dumps(body, ensure_ascii=False))
        self.assertNotIn("executed_price", body["feedback_cases"]["recent"][0])
        self.assertNotIn("quantity", body["feedback_cases"]["recent"][0])
        self.assertNotIn("note", body["feedback_cases"]["recent"][0])
        review_payload = {"case_id": saved["case_id"], "status": "partial",
                          "market_asof": date.today().isoformat(),
                          "horizon_sessions": 5, "outcome": {"5": {"price_return": 0.03}}}
        review_hash = hashlib.sha256(json.dumps(
            review_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        review_dir = self.reports.parent / "case-reviews" / date.today().isoformat()
        review_dir.mkdir(parents=True)
        (review_dir / "review.json").write_text(json.dumps({
            "schema_version": 1, "review_id": "review-1",
            "created_at": "2026-09-29T08:00:00Z", "payload_sha256": review_hash,
            "payload": review_payload,
        }), encoding="utf-8")
        reviewed = invoke(self.service, "GET", "/api/console-status")[2]
        self.assertEqual("ready", reviewed["reviews"]["status"])
        self.assertEqual("partial", reviewed["feedback_cases"]["recent"][0]["review_status"])
        self.assertEqual("review-1", reviewed["feedback_cases"]["recent"][0]["latest_review_id"])
        feedback["observed_date"] = "2000-01-01"
        rejected = invoke(self.service, "POST", "/api/feedback-case",
                          json.dumps(feedback).encode())
        self.assertEqual(400, rejected[0])
        media_type = invoke(self.service, "POST", "/api/feedback-case",
                            json.dumps(feedback).encode(), content_type="text/plain")
        self.assertEqual(415, media_type[0])

    def test_console_optimization_summary_validates_saved_scores_and_hash(self) -> None:
        metrics = {"total_return": 0.1, "cagr": 0.02, "annual_volatility": 0.2,
                   "sharpe_rf0": 0.1, "max_drawdown": -0.3, "turnover": 2}
        score = score_backtest(metrics, metrics)
        directory = self.reports.parent / "optimizations"
        append_optimization_record(directory, {
            "optimizer_version": OPTIMIZER_VERSION,
            "strategy_id": "sma-trend", "strategy_version": "1",
            "objective_policy_version": score["policy_version"],
            "selection_rule": "train only", "canonical_contract_version": "contract-v1",
            "input_fingerprint_sha256": "c" * 64,
            "universe": ["stock:000338.SZ"],
            "train": {"start": "2021-01-04", "end": "2025-01-08", "sessions": 974,
                      "benchmark_metrics": metrics},
            "candidate_grid": [{"candidate_index": 0,
                                "parameters": {"fast_window": 20, "slow_window": 60},
                                "train_metrics": metrics, "train_north_star": score}],
            "selected_candidate_index": 0,
            "selected_parameters": {"fast_window": 20, "slow_window": 60},
            "historical_holdout": {"status": "retrospective_holdout_only",
                                   "start": "2025-01-08", "end": "2026-09-29", "sessions": 419,
                                   "metrics": metrics, "benchmark_metrics": metrics,
                                   "north_star": score, "reason": "回看留出段"},
            "promotion_status": "not_eligible", "promotion_reason": "尚无前瞻验证",
        })
        body = invoke(self.service, "GET", "/api/console-status")[2]
        self.assertEqual("ready", body["optimizations"]["status"])
        entry = body["optimizations"]["recent"][0]
        self.assertEqual("sma-trend", entry["strategy_id"])
        self.assertEqual(1, entry["candidate_count"])
        self.assertEqual(5.0, entry["train"]["selected_north_star"]["score"])
        self.assertEqual("retrospective_holdout_only", entry["historical_holdout"]["status"])
        self.assertEqual("not_eligible", entry["promotion_status"])
        file = next(directory.rglob("*.json"))
        record = json.loads(file.read_text(encoding="utf-8"))
        record["payload"]["selected_candidate_index"] = 99
        file.write_text(json.dumps(record), encoding="utf-8")
        invalid = invoke(self.service, "GET", "/api/console-status")[2]
        self.assertEqual("unavailable", invalid["optimizations"]["status"])

    def test_offline_task_endpoints_run_on_read_only_fixture_and_persist_events(self) -> None:
        stock = Instrument(
            "stock:000338.SZ", "000338.SZ", "潍柴动力", "stock", "focus_stock",
            "fixture", "0.000338", exchange="SZSE", adjustment="qfq")
        index = Instrument(
            "index:000300.SH", "000300.SH", "沪深300", "index", "broad_index",
            "fixture", "1.000300", exchange="SSE", adjustment="none")
        self.writer.upsert_instruments([stock, index])
        start = date(2024, 1, 1)
        bars = []
        for offset in range(300):
            day = start + timedelta(days=offset)
            close = 10 + offset * 0.015 + (offset % 7) * 0.04
            bars.append(DailyBar(
                stock.instrument_id, day, close, close * 1.01, close * 0.99, close,
                1000, close * 1000, "share", "CNY", "qfq", "fixture", "f" * 64))
        bars.append(DailyBar(index.instrument_id, start + timedelta(days=299),
                             3000, 3030, 2970, 3000, 0, None,
                             "source_native", "unavailable", "none", "fixture", "e" * 64))
        self.writer.upsert_bars(bars)
        request = b'{"strategy":"sma-trend"}'
        accepted = invoke(self.service, "POST", "/api/optimize-run", request)
        self.assertEqual(202, accepted[0])
        task_id = accepted[2]["task_id"]
        self.assertEqual("accepted", accepted[2]["status"])
        terminal = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            tasks = invoke(self.service, "GET", "/api/console-status")[2]["tasks"]
            terminal = next((item for item in tasks["recent"]
                             if item["task_id"] == task_id), None)
            if terminal:
                break
            time.sleep(0.01)
        self.assertIsNotNone(terminal)
        self.assertEqual("completed", terminal["status"])
        self.assertEqual("saved", terminal["result"]["runs"][0]["status"])
        optimization_id = terminal["result"]["runs"][0]["optimization_id"]
        self.assertEqual(1, len(list((self.reports.parent / "optimizations").rglob("*.json"))))
        accepted_again = invoke(self.service, "POST", "/api/optimize-run", request)
        self.assertEqual(202, accepted_again[0])
        second_id = accepted_again[2]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            tasks = invoke(self.service, "GET", "/api/console-status")[2]["tasks"]
            second = next((item for item in tasks["recent"]
                           if item["task_id"] == second_id), None)
            if second:
                break
            time.sleep(0.01)
        self.assertEqual("existing", second["result"]["runs"][0]["status"])
        self.assertEqual(optimization_id, second["result"]["runs"][0]["optimization_id"])
        self.assertEqual(1, len(list((self.reports.parent / "optimizations").rglob("*.json"))))
        review = invoke(self.service, "POST", "/api/review-cases", b"{}")
        self.assertEqual(202, review[0])
        review_id = review[2]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            tasks = invoke(self.service, "GET", "/api/console-status")[2]["tasks"]
            reviewed = next((item for item in tasks["recent"]
                             if item["task_id"] == review_id), None)
            if reviewed:
                break
            time.sleep(0.01)
        self.assertEqual("completed", reviewed["status"])
        self.assertEqual(0, reviewed["result"]["saved"])
        restarted = QuantAPIService(self.writer, self.reports)
        recovered = invoke(restarted, "GET", "/api/console-status")[2]["tasks"]
        self.assertIsNone(recovered["active"])
        self.assertIn(task_id, {item["task_id"] for item in recovered["recent"]})
        self.assertEqual(6, len(list((self.reports.parent / "task-runs").rglob("*.json"))))

    def test_offline_task_rejects_cross_origin_and_serializes_active_runs(self) -> None:
        request = b'{"strategy":"all"}'
        self.assertEqual(403, invoke(self.service, "POST", "/api/optimize-run", request,
                                     origin="https://example.com")[0])
        self.assertEqual(415, invoke(self.service, "POST", "/api/optimize-run", request,
                                     content_type="text/plain")[0])
        self.assertEqual(400, invoke(self.service, "POST", "/api/review-cases",
                                     b'{"sync":true}')[0])
        started, release = threading.Event(), threading.Event()

        def blocked(_strategy: str) -> tuple[str, dict[str, object]]:
            started.set()
            release.wait(2)
            return "completed", {"runs": []}

        with patch.object(self.service, "_optimize_task", side_effect=blocked):
            accepted = invoke(self.service, "POST", "/api/optimize-run", request)
            self.assertEqual(202, accepted[0])
            self.assertTrue(started.wait(1))
            busy = invoke(self.service, "POST", "/api/review-cases", b"{}")
            self.assertEqual(409, busy[0])
            self.assertEqual(accepted[2]["task_id"], busy[2]["task_id"])
            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self.service.console_status()["tasks"]["active"]:
                time.sleep(0.01)
        self.assertIsNone(self.service.console_status()["tasks"]["active"])

    def test_stock_query_uses_read_only_store_and_preserves_partial_status(self) -> None:
        key = "stock:000338.SZ"
        day = date.today()
        self.writer.upsert_instruments([Instrument(
            key, "000338.SZ", "潍柴动力", "stock", "test", "test_source",
            "0.000338", exchange="SZSE", adjustment="qfq",
        )])
        self.writer.upsert_bars([DailyBar(
            key, day, 10, 11, 9, 10, 1_000_000, None, "share", "unavailable",
            "qfq", "test_source", "payload-hash",
        )])
        payload = json.dumps({"symbol": "000338", "cost_price": 12}).encode()
        status, _headers, body = invoke(self.service, "POST", "/api/stock-report", payload)
        self.assertEqual(200, status)
        self.assertEqual("partial_data", body["status"])
        self.assertEqual(key, body["instrument_id"])
        self.assertEqual(day.isoformat(), body["asof"])
        self.assertAlmostEqual(10 / 12 - 1, body["cost_return"])
        self.assertIsNone(body["forecast"]["direction_probability"])
        self.assertEqual("test_source", body["sources"][0]["latest_source_id"])
        self.assertEqual("insufficient_evidence", body["decision"]["status"])
        self.assertEqual("qfq_cny", body["chart"]["price_basis"])
        self.assertEqual("saved", body["research_record"]["status"])
        saved = next((self.root / "research-journal").rglob("*.json"))
        archived = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(body["research_record"]["record_id"], archived["record_id"])
        self.assertEqual(body["chart"], archived["payload"]["report"]["chart"])
        self.assertEqual(body["decision"], archived["payload"]["report"]["decision"])
        with self.service.store.connect() as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM daily_bars")
        with sqlite3.connect(self.db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0])

    def test_http_stock_query_reads_separate_published_qlib_release_as_fallback(self) -> None:
        release_dir = self.root / "qlib-releases" / "2000-01-06"
        release_dir.mkdir(parents=True)
        archive, manifest = write_release(release_dir, fixture_files())
        publish_release_tree(archive, manifest, "2000-01-06", release_dir / "published")
        summary_path = self.reports.parent / "qlib" / "latest-summary.json"
        summary_path.parent.mkdir()
        summary_path.write_text(json.dumps({
            "status": "inspected", "source_id": "investment_data_qlib_release",
            "release_tag": "2000-01-06", "calendar_first": "2000-01-04",
            "calendar_last": "2000-01-06", "stock_count_with_valid_close": 2,
            "bar_count": 5, "inactive_reference_covered": 0,
            "adjustment": "qlib_adjusted", "eligible_for_screening": False,
            "validated_at": "2026-09-29T07:00:00+00:00",
        }), encoding="utf-8")
        status, _, body = invoke(self.service, "POST", "/api/stock-report",
                                 b'{"symbol":"600000","cost_price":10}')
        self.assertEqual(200, status)
        self.assertEqual("partial_data", body["status"])
        self.assertEqual("stock:600000.SH", body["instrument_id"])
        self.assertEqual(11.0, body["adjusted_close"])
        self.assertIsNone(body["latest_close"])
        self.assertIsNone(body["cost_return"])
        self.assertEqual("2000-01-06", body["sources"][0]["release_tag"])
        self.assertEqual(2, body["sources"][0]["bar_count"])
        self.assertIsNone(body["forecast"])
        self.assertIsNone(body["backtest"])
        self.assertEqual("qlib_adjusted", body["chart"]["price_basis"])
        self.assertEqual("unavailable", body["chart"]["forecast"]["status"])
        self.assertEqual("saved", body["research_record"]["status"])
        with self.writer.connect() as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM daily_bars WHERE instrument_id='stock:600000.SH'"
            ).fetchone()[0])

        with patch("quant_lab.api_server.append_stock_report", side_effect=PermissionError("denied")):
            status, _, unsaved = invoke(self.service, "POST", "/api/stock-report",
                                        b'{"symbol":"600000","cost_price":10}')
        self.assertEqual(200, status)
        self.assertEqual("save_failed", unsaved["research_record"]["status"])

        status, _, exchange_prefix = invoke(self.service, "POST", "/api/stock-report",
                                            b'{"symbol":"SH.600000"}')
        self.assertEqual(200, status)
        self.assertEqual("stock:600000.SH", exchange_prefix["instrument_id"])

        status, _, missing = invoke(self.service, "POST", "/api/stock-report",
                                    b'{"symbol":"999999"}')
        self.assertEqual(200, status)
        self.assertEqual("unknown_stock", missing["status"])

    def test_missing_database_and_malformed_request_return_json_errors(self) -> None:
        missing = self.root / "missing" / "market.sqlite3"
        readonly = ReadOnlyMarketStore(missing)
        self.assertFalse(missing.parent.exists())
        service = QuantAPIService(self.writer, self.reports)
        service.store = readonly
        status, _headers, body = invoke(service, "POST", "/api/stock-report",
                                        b'{"symbol":"000338"}')
        self.assertEqual(503, status)
        self.assertEqual("stock_data_unavailable", body["status"])
        status, _headers, body = invoke(self.service, "POST", "/api/stock-report", b"\xff\xfe")
        self.assertEqual(400, status)
        self.assertEqual("invalid_input", body["status"])
        oversized_number = b'{"symbol":' + b"9" * 4301 + b"}"
        status, _headers, body = invoke(self.service, "POST", "/api/stock-report",
                                        oversized_number)
        self.assertEqual(400, status)
        self.assertEqual("invalid_input", body["status"])
        status, _headers, body = invoke(self.service, "POST", "/api/stock-report",
                                        b'{"symbol":"000338","cost_price":"NaN"}')
        self.assertEqual(400, status)
        self.assertEqual("invalid_input", body["status"])

    def test_nonfinite_published_json_is_marked_invalid(self) -> None:
        (self.reports / "flow.json").write_text('{"bad":NaN}', encoding="utf-8")
        status, _headers, body = invoke(self.service, "GET", "/api/dashboard")
        self.assertEqual(200, status)
        self.assertEqual("invalid_report", body["flow"]["status"])

    def test_archive_progress_is_published_snapshot_not_live_wal_database(self) -> None:
        archive = self.root / "historical-baostock-raw.sqlite3"
        connection = sqlite3.connect(archive)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE backfill_snapshots (
                    snapshot_id TEXT, scope TEXT, collected_at TEXT,
                    listing_count INTEGER, recorded_at TEXT)
            """)
            connection.execute("""
                CREATE TABLE backfill_windows (
                    source_id TEXT, adjustment TEXT, instrument_id TEXT,
                    status TEXT, bar_count INTEGER)
            """)
            connection.execute("INSERT INTO backfill_snapshots VALUES (?,?,?,?,?)",
                               ("snapshot-1", "provider-active-and-inactive-sh-sz-a",
                                "2026-09-29T06:00:00+00:00", 5559,
                                "2026-09-29T06:00:00+00:00"))
            connection.executemany("INSERT INTO backfill_windows VALUES (?,?,?,?,?)", [
                ("baostock_daily", "none", "stock:600000.SH", "success", 480),
                ("baostock_daily", "none", "stock:600000.SH", "success", 500),
                ("baostock_daily", "none", "stock:000001.SZ", "failed", 0),
            ])
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        Path(str(archive) + "-wal").unlink(missing_ok=True)
        Path(str(archive) + "-shm").unlink(missing_ok=True)
        self.assertFalse(Path(str(archive) + "-shm").exists())
        output = self.reports.parent / "backfill" / "historical-archive-status.json"
        published = publish_checkpoint(archive, output)
        self.assertEqual(1, published["stock_count"])
        self.assertEqual(980, published["bar_count"])
        self.assertEqual(1, published["failed_window_count"])
        first = self.service.dashboard()["historical_archive"]
        self.assertEqual(5559, first["catalogue_stock_count"])
        self.assertEqual(1, first["stock_count"])
        connection = sqlite3.connect(archive)
        try:
            connection.execute("INSERT INTO backfill_windows VALUES (?,?,?,?,?)",
                               ("baostock_daily", "none", "stock:000002.SZ", "success", 300))
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(1, self.service.dashboard()["historical_archive"]["stock_count"])
        publish_checkpoint(archive, output)
        self.assertEqual(2, self.service.dashboard()["historical_archive"]["stock_count"])

    def test_qlib_archive_summary_is_optional_bounded_and_separate(self) -> None:
        self.assertNotIn("qlib_archive", self.service.dashboard())
        summary_path = self.reports.parent / "qlib" / "latest-summary.json"
        summary_path.parent.mkdir()
        summary = {
            "status": "inspected", "source_id": "investment_data_qlib_release",
            "release_tag": "2026-09-28", "calendar_first": "2000-01-04",
            "calendar_last": "2026-09-28", "stock_count_with_valid_close": 5400,
            "bar_count": 10_000_000, "inactive_reference_covered": 320,
            "adjustment": "qlib_adjusted", "eligible_for_screening": False,
            "validated_at": "2026-09-29T07:00:00+00:00", "sample_rows": list(range(20)),
        }
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        dashboard = self.service.dashboard()
        self.assertEqual(5400, dashboard["qlib_archive"]["stock_count_with_valid_close"])
        self.assertEqual(320, dashboard["qlib_archive"]["inactive_reference_covered"])
        self.assertNotIn("sample_rows", dashboard["qlib_archive"])
        self.assertEqual("missing_report", dashboard["stock_shortlist"]["status"])

        summary["eligible_for_screening"] = True
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        self.assertNotIn("qlib_archive", self.service.dashboard())
        summary["eligible_for_screening"] = False
        summary_path.write_text(json.dumps({**summary, "padding": "x" * 5000}), encoding="utf-8")
        self.assertNotIn("qlib_archive", self.service.dashboard())

    def test_three_names_are_hidden_until_manifest_confirms_same_run(self) -> None:
        self.write_report("market-pulse.json", {"asof": "2026-09-28"})
        self.write_report("stock-shortlist.json", {"asof": "2026-09-28", "status": "ready"})
        self.write_report("run.json", {"run_id": "old-run", "recommendation_status": "blocked_insufficient_evidence",
                                       "recommendation_count": 0})
        recommendation = {
            "status": "ready_for_human_review", "run_id": "new-run", "asof": "2026-09-28",
            "recommendation_count": 3,
            "recommendations": [{"instrument_id": f"stock:00000{index}.SZ"}
                                for index in (1, 2, 3)],
        }
        self.write_report("recommendations.json", recommendation)
        mixed = self.service.dashboard()["recommendations"]
        self.assertEqual("invalid_report", mixed["status"])
        self.assertEqual([], mixed["recommendations"])
        self.write_report("run.json", {"run_id": "new-run", "recommendation_status": "ready_for_human_review",
                                       "recommendation_count": 3, "stock_screen_status": "ready"})
        same_run = self.service.dashboard()["recommendations"]
        self.assertEqual("ready_for_human_review", same_run["status"])
        self.assertEqual(3, len(same_run["recommendations"]))
        self.write_report("stock-shortlist.json", {"asof": "2026-09-28",
                                                  "status": "blocked_universe_coverage"})
        mixed = self.service.dashboard()["recommendations"]
        self.assertEqual("invalid_report", mixed["status"])
        self.assertEqual([], mixed["recommendations"])


if __name__ == "__main__":
    unittest.main()
