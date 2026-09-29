"""HTTP bridge for stock research with a read-only market store.

The visualization service is a separate process and has no database access.
Successful queries are saved to a separate local research journal.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from quant_lab.baostock_source import read_baostock_historical_snapshot
from quant_lab.algorithm_catalog import REGISTRY_VERSION as ALGORITHM_REGISTRY_VERSION, algorithm_catalog
from quant_lab.action_reference import RULE_VERSION, build_action_reference
from quant_lab.baostock_backfill import BACKFILL_VERSION
from quant_lab.factor_library import factor_catalog
from quant_lab.feedback_loop import (append_feedback_case, list_case_reviews,
                                     list_feedback_cases, review_feedback_cases)
from quant_lab.forecast_benchmark import read_benchmark_record
from quant_lab.forecast_qlib import read_qlib_benchmark_record
from quant_lab.forecast_stability import read_stability_record
from quant_lab.group_behavior_pilot import read_group_behavior_record
from quant_lab.north_star import POLICY_VERSION as NORTH_STAR_VERSION, score_backtest
from quant_lab.optimizer import (append_optimization_record, list_optimization_records,
                                 optimize_focus_stocks)
from quant_lab.qlib_archive import QlibArchiveError
from quant_lab.qlib_stock_report import analyze_qlib_stock
from quant_lab.raw_data_inventory import INVENTORY_VERSION
from quant_lab.research_journal import append_stock_report
from quant_lab.stock_analysis import analyze_stock
from quant_lab.stock_chart import CHART_VERSION, SIGNAL_RULE_ID, SIGNAL_RULE_VERSION
from quant_lab.stock_screen import SCREEN_VERSION
from quant_lab.storage import MarketStore
from quant_lab.strategy_catalog import strategy_catalog
from quant_lab.strategy_registry import (list_latest_strategy_snapshots,
                                         list_strategy_snapshots)


_LOGGER = logging.getLogger(__name__)


class ReadOnlyMarketStore(MarketStore):
    def __init__(self, path: str | Path):
        # MarketStore normally creates the parent directory for writers.
        # The HTTP process must not create or initialize market storage.
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            yield connection
        finally:
            connection.close()


class QuantAPIService:
    def __init__(self, store: MarketStore, report_dir: str | Path,
                 journal_dir: str | Path | None = None):
        self.store = ReadOnlyMarketStore(store.path)
        self.report_dir = Path(report_dir)
        self.journal_dir = Path(journal_dir) if journal_dir is not None else self.report_dir.parent / "research-journal"
        self._archived_names: tuple[dict[str, str], str | None] | None = None
        self._task_lock = threading.Lock()
        self._active_task: dict[str, Any] | None = None

    def _finalize_stock_report(self, report: dict[str, Any], *, query: str,
                               cost_price: float | None) -> dict[str, Any]:
        if report.get("status") not in ("ready", "partial_data"):
            return report
        report["decision"] = build_action_reference(report)
        chart = report.get("chart") if isinstance(report.get("chart"), dict) else {}
        forecast = chart.get("forecast") if isinstance(chart.get("forecast"), dict) else {}
        versions = {
            "decision_policy": RULE_VERSION,
            "chart": CHART_VERSION,
            "chart_signal_rule": chart.get("signal_rule", {}).get("rule_version")
                                 if isinstance(chart.get("signal_rule"), dict) else None,
            "forecast_model_id": forecast.get("model_id"),
            "forecast_model_version": forecast.get("model_version"),
        }
        try:
            saved = append_stock_report(
                self.journal_dir, query=query, cost_price=cost_price,
                report=report, versions=versions,
            )
        except (OSError, TypeError, ValueError):
            report["research_record"] = {
                "status": "save_failed",
                "reason": "本次研究记录未能写入本地归档；请检查量化服务的报告目录权限。",
            }
        else:
            report["research_record"] = {
                "status": "saved",
                "record_id": saved["record_id"],
                "generated_at": saved["generated_at"],
                "payload_sha256": saved["payload_sha256"],
            }
        return report

    def _archived_stock_names(self) -> tuple[dict[str, str], str | None]:
        """Use only a hash-checked local BaoStock snapshot for display names."""
        if self._archived_names is not None:
            return self._archived_names
        archive = self._historical_archive()
        snapshot_id = archive.get("snapshot_id")
        if not isinstance(snapshot_id, str) or re.fullmatch(r"[0-9a-f]{64}", snapshot_id) is None:
            self._archived_names = ({}, None)
            return self._archived_names
        directory = self.report_dir.parent / "snapshots"
        paths = list(directory.glob(f"*-baostock-historical-shsz-{snapshot_id[:12]}.json"))
        if len(paths) != 1:
            self._archived_names = ({}, None)
            return self._archived_names
        try:
            snapshot = read_baostock_historical_snapshot(paths[0])
            if snapshot.snapshot_id != snapshot_id:
                raise ValueError("snapshot ID differs from published checkpoint")
            names = {item.instrument.instrument_id: item.instrument.name
                     for item in snapshot.listings if item.instrument.name}
            self._archived_names = (names, snapshot_id)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            self._archived_names = ({}, None)
        return self._archived_names

    def _read_json(self, filename: str) -> dict[str, Any]:
        path = self.report_dir / filename
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"status": "missing_report", "reason": f"缺少 {filename}；先运行 quant_lab run。"}
        except (OSError, UnicodeError, ValueError) as exc:
            return {"status": "invalid_report", "reason": f"读取 {filename} 失败：{exc}"}
        if not isinstance(value, dict):
            return {"status": "invalid_report", "reason": f"{filename} 不是 JSON 对象。"}
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            return {"status": "invalid_report", "reason": f"{filename} 含无效数值。"}
        return value

    def _historical_archive(self) -> dict[str, Any]:
        """Read the last published archive checkpoint, never the live WAL DB."""
        path = self.report_dir.parent / "backfill" / "historical-archive-status.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"status": "missing_archive"}
        except (OSError, UnicodeError, ValueError):
            return {"status": "unavailable"}
        if (not isinstance(value, dict) or value.get("status") != "ready"
                or value.get("source_id") != "baostock_daily"
                or value.get("adjustment") != "none"
                or value.get("eligible_for_screening") is not False
                or not all(isinstance(value.get(field), int) and not isinstance(value.get(field), bool)
                           and value[field] >= 0 for field in (
                               "catalogue_stock_count", "stock_count", "completed_window_count",
                               "bar_count", "failed_window_count"))
                or value["stock_count"] > value["catalogue_stock_count"]):
            return {"status": "unavailable"}
        return {key: value.get(key) for key in (
            "status", "source_id", "snapshot_id", "catalogue_scope",
            "catalogue_collected_at", "catalogue_stock_count", "stock_count",
            "completed_window_count", "bar_count", "failed_window_count",
            "adjustment", "eligible_for_screening", "published_at",
        )}

    def _historical_backfill_batch(self) -> dict[str, Any]:
        """Show the last finished batch, without inferring worker liveness."""
        path = self.report_dir.parent / "backfill" / "last-batch.json"
        if not path.exists():
            return {"status": "missing_batch"}
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                file_modified_at = datetime.fromtimestamp(
                    os.fstat(stream.fileno()).st_mtime, timezone.utc
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError("oversize batch result")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict) or value.get("backfill_version") != BACKFILL_VERSION:
                raise ValueError("invalid batch result")
            if (not isinstance(value.get("run_id"), str)
                    or re.fullmatch(r"[0-9a-f]{32}", value["run_id"]) is None
                    or value.get("source_id") != "baostock_daily"
                    or not isinstance(value.get("snapshot_id"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", value["snapshot_id"]) is None):
                raise ValueError("invalid batch identity")
            counts = ("requested_window_count", "completed_window_count",
                      "empty_window_count", "failed_window_count",
                      "remaining_window_count", "deferred_failure_count",
                      "bar_count", "status_count")
            if any(isinstance(value.get(key), bool) or not isinstance(value.get(key), int)
                   or value[key] < 0 for key in counts):
                raise ValueError("invalid batch counts")
            if (value["requested_window_count"] > 100
                    or sum(value[key] for key in (
                        "completed_window_count", "empty_window_count", "failed_window_count"
                    )) > value["requested_window_count"]):
                raise ValueError("inconsistent batch counts")
            elapsed = value.get("elapsed_seconds")
            if (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
                    or not math.isfinite(elapsed) or elapsed < 0):
                raise ValueError("invalid batch duration")
            error = value.get("error")
            if error is not None and not isinstance(error, str):
                raise ValueError("invalid batch error")
            return {"status": "ready", "file_modified_at": file_modified_at,
                    "last_batch": {key: value[key] for key in (
                        "run_id", "source_id", "snapshot_id", *counts, "elapsed_seconds"
                    )} | {"error": error[:240] if error is not None else None}}
        except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
            return {"status": "unavailable", "reason": "最近回填批次快照不可读取或校验失败。"}

    def _qlib_archive(self) -> dict[str, Any] | None:
        """Expose only a small summary after an independent release inspection."""
        path = self.report_dir.parent / "qlib" / "latest-summary.json"
        try:
            with path.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                return None
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        if (value.get("status") != "inspected"
                or value.get("source_id") != "investment_data_qlib_release"
                or value.get("adjustment") != "qlib_adjusted"
                or value.get("eligible_for_screening") is not False):
            return None
        for field in ("stock_count_with_valid_close", "bar_count", "inactive_reference_covered"):
            number = value.get(field)
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                return None
        if (value["stock_count_with_valid_close"] == 0
                or value["bar_count"] < value["stock_count_with_valid_close"]
                or value["inactive_reference_covered"] > value["stock_count_with_valid_close"]):
            return None
        try:
            dates = []
            for field in ("release_tag", "calendar_first", "calendar_last"):
                item = value[field]
                if not isinstance(item, str) or len(item) != 10:
                    return None
                parsed = date.fromisoformat(item)
                if parsed.isoformat() != item:
                    return None
                dates.append(parsed)
            validated_at = value["validated_at"]
            if not isinstance(validated_at, str) or len(validated_at) > 64:
                return None
            parsed_time = datetime.fromisoformat(validated_at.replace("Z", "+00:00"))
            if parsed_time.tzinfo is None or not dates[1] <= dates[2] <= dates[0]:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return {key: value[key] for key in (
            "status", "source_id", "release_tag", "calendar_first", "calendar_last",
            "stock_count_with_valid_close", "bar_count", "inactive_reference_covered",
            "adjustment", "eligible_for_screening", "validated_at",
        )}

    def dashboard(self) -> dict[str, Any]:
        market = self._read_json("market-pulse.json")
        flow = self._read_json("flow.json")
        run = self._read_json("run.json")
        stock_shortlist = self._read_json("stock-shortlist.json")
        research = self._read_json("research.json")
        forecast = self._read_json("forecast-momentum.json")
        industry_validation = self._read_json("industry-validation.json")
        recommendations = self._read_json("recommendations.json")
        # The visual page needs aggregate coverage and the block reason.
        # Market-wide lists and per-stock exclusions can exceed the proxy's
        # response limit; full details stay in the research artifact.
        stock_shortlist = {key: stock_shortlist.get(key) for key in (
            "status", "reason", "asof", "universe_count",
            "expected_universe_count", "data_ready_count", "coverage_rate",
            "candidate_count", "universe_snapshot_id", "universe_snapshot_scope",
            "limitations",
        )}
        if recommendations.get("status") == "ready_for_human_review":
            # The writer publishes run.json last. During a new research run,
            # files in the latest directory can briefly belong to two runs.
            # Never display three names unless the published manifest confirms
            # this recommendation artifact and its as-of date.
            same_run = bool(
                run.get("run_id")
                and recommendations.get("run_id") == run.get("run_id")
                and run.get("recommendation_status") == "ready_for_human_review"
                and run.get("recommendation_count") == 3
                and run.get("stock_screen_status") == "ready"
                and stock_shortlist.get("status") == "ready"
                and recommendations.get("recommendation_count") == 3
                and isinstance(recommendations.get("recommendations"), list)
                and len(recommendations["recommendations"]) == 3
                and recommendations.get("asof") == stock_shortlist.get("asof")
                and recommendations.get("asof") == market.get("asof")
            )
            if not same_run:
                recommendations = {
                    "status": "invalid_report",
                    "reason": "推荐报告与本次运行清单或数据日期不一致，请稍后刷新。",
                    "recommendation_count": 0,
                    "recommendations": [],
                }
        if "backtest" in research:
            backtest = research["backtest"]
            research = {
                "asof": research.get("asof"),
                "strategy_id": research.get("strategy_id"),
                "universe": research.get("universe"),
                "backtest": {key: backtest.get(key) for key in (
                    "start", "end", "observations", "metrics", "benchmarks",
                    "prospective_out_of_sample",
                )},
                "limitations": research.get("limitations"),
            }
        if "per_stock" in forecast:
            forecast = {key: forecast.get(key) for key in (
                "status", "provider", "data_asof", "universe_scope", "pooled", "limitations",
            )}
        if "points" in industry_validation:
            industry_validation = {key: industry_validation.get(key) for key in (
                "industry_data_asof", "selected_origin_count", "positive_future_rate",
                "mean_future_5d", "mean_excess_vs_equal_weight",
                "outperform_equal_weight_rate", "worst_future_5d", "limitations",
            )}
        dashboard = {
            "market": market,
            "flow": flow,
            "run": run,
            "stock_shortlist": stock_shortlist,
            "research": research,
            "forecast": forecast,
            "industry_validation": industry_validation,
            "recommendations": recommendations,
            "historical_archive": self._historical_archive(),
        }
        qlib_archive = self._qlib_archive()
        if qlib_archive is not None:
            dashboard["qlib_archive"] = qlib_archive
        return dashboard

    @staticmethod
    def _metric_summary(value: Any) -> dict[str, float] | None:
        """Only expose the six named, finite metrics from a published backtest."""
        keys = ("total_return", "cagr", "annual_volatility", "sharpe_rf0",
                "max_drawdown", "turnover")
        if not isinstance(value, dict):
            return None
        result: dict[str, float] = {}
        for key in keys:
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                return None
            try:
                finite = float(number)
            except OverflowError:
                return None
            if not math.isfinite(finite):
                return None
            result[key] = finite
        return result

    @staticmethod
    def _parameter_summary(value: Any) -> dict[str, int | float]:
        if not isinstance(value, dict):
            return {}
        allowed = ("fast_window", "slow_window", "lookback", "top_n", "window", "entry_z")
        selected: dict[str, int | float] = {}
        for key in allowed:
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                continue
            try:
                finite = float(number)
            except OverflowError:
                continue
            if math.isfinite(finite) and 0 < finite <= 1000:
                selected[key] = number
        return selected

    def _north_star_summary(self, backtest: Any) -> dict[str, Any]:
        unavailable = {"policy_version": NORTH_STAR_VERSION, "status": "unavailable",
                       "reason": "缺少同股票池等权持有基准或有效的 CAGR/最大回撤指标。"}
        if not isinstance(backtest, dict):
            return unavailable
        metrics = self._metric_summary(backtest.get("metrics"))
        benchmarks = backtest.get("benchmarks")
        hold = benchmarks.get("focus_equal_weight_hold") if isinstance(benchmarks, dict) else None
        hold_metrics = (self._metric_summary(hold.get("metrics"))
                        if isinstance(hold, dict) and hold.get("status") == "available" else None)
        if metrics is None or hold_metrics is None:
            return unavailable
        try:
            return score_backtest(metrics, hold_metrics)
        except ValueError:
            return unavailable

    def _market_store_status(self) -> dict[str, Any]:
        groups = {key: {"instrument_count": 0, "bar_count": 0,
                        "first_date": None, "last_date": None}
                  for key in ("stock", "index", "industry")}
        try:
            with self.store.connect() as connection:
                instrument_rows = connection.execute(
                    "SELECT asset_type, COUNT(*) AS count FROM instruments GROUP BY asset_type"
                ).fetchall()
                bar_rows = connection.execute(
                    """SELECT i.asset_type, COUNT(*) AS count,
                              MIN(b.trade_date) AS first_date, MAX(b.trade_date) AS last_date
                       FROM daily_bars b JOIN instruments i ON i.instrument_id=b.instrument_id
                       GROUP BY i.asset_type"""
                ).fetchall()
                run_rows = connection.execute(
                    """SELECT run_id, status, started_at, finished_at,
                              requested_start, requested_end, instrument_count,
                              row_count, error
                       FROM sync_runs ORDER BY started_at DESC LIMIT 5"""
                ).fetchall()
        except (OSError, sqlite3.Error):
            return {"status": "unavailable", "groups": groups, "latest_sync_runs": []}
        aliases = {"stock": "stock", "index": "index", "industry_index": "industry"}
        for row in instrument_rows:
            key = aliases.get(row["asset_type"])
            if key:
                groups[key]["instrument_count"] = row["count"]
        for row in bar_rows:
            key = aliases.get(row["asset_type"])
            if key:
                groups[key].update(bar_count=row["count"], first_date=row["first_date"],
                                   last_date=row["last_date"])
        latest_runs = [{key: row[key] for key in (
            "run_id", "status", "started_at", "finished_at", "requested_start",
            "requested_end", "instrument_count", "row_count"
        )} | {"error": row["error"][:240] if isinstance(row["error"], str) else None}
            for row in run_rows]
        return {"status": "ready", "groups": groups, "latest_sync_runs": latest_runs}

    def _research_journal_status(self) -> dict[str, Any]:
        """List recent research metadata without disclosing private cost inputs."""
        if not self.journal_dir.is_dir():
            return {"status": "empty", "recent": []}
        try:
            days = sorted((path for path in self.journal_dir.iterdir()
                           if path.is_dir() and not path.is_symlink()
                           and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name)),
                          reverse=True)[:10]
            candidates: list[Path] = []
            for day in days:
                candidates.extend(path for path in day.iterdir()
                                  if path.is_file() and not path.is_symlink()
                                  and re.fullmatch(r"[0-9a-f-]{36}\.json", path.name))
            candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
            recent: list[dict[str, Any]] = []
            for path in candidates[:32]:
                if path.stat().st_size > 512_000:
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    payload = record.get("payload", {})
                    report = payload.get("report", {})
                    if (not isinstance(record, dict) or not isinstance(payload, dict)
                            or not isinstance(report, dict)
                            or not isinstance(record.get("record_id"), str)
                            or not isinstance(record.get("generated_at"), str)
                            or record.get("schema_version") != 1
                            or path.name != f"{record['record_id']}.json"
                            or path.parent.name != record["generated_at"][:10]):
                        continue
                    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"), allow_nan=False).encode("utf-8")
                    if hashlib.sha256(canonical).hexdigest() != record.get("payload_sha256"):
                        continue
                    recent.append({
                        "record_id": record["record_id"],
                        "generated_at": record["generated_at"],
                        "instrument_id": report.get("instrument_id"),
                        "name": report.get("name"),
                        "asof": report.get("asof"),
                        "status": report.get("status"),
                        "payload_sha256": record.get("payload_sha256"),
                    })
                except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
                    continue
                if len(recent) == 5:
                    break
        except OSError:
            return {"status": "unavailable", "recent": []}
        return {"status": "ready", "recent": recent}

    def _strategy_status(self, run: dict[str, Any], *,
                         research_override: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        strategy_id = run.get("strategy_id")
        strategy_version = run.get("strategy_version")
        run_id = run.get("run_id")
        if not all(isinstance(item, str) and 0 < len(item) <= 80
                   for item in (strategy_id, strategy_version)):
            return []
        if research_override is None and not isinstance(run_id, str):
            return []
        entry: dict[str, Any] = {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "feature_version": run.get("feature_version"),
            "status": "retrospective_pilot",
            "source_run_id": run_id,
            "asof": run.get("stock_signal_asof"),
            "parameters": {},
            "universe_scope": run.get("stock_universe_scope"),
            "universe_count": run.get("stock_universe_count"),
            "backtest": {"status": "unavailable", "reason": "缺少与已发布运行清单一致的回测产物。"},
            "north_star": {"policy_version": NORTH_STAR_VERSION, "status": "unavailable",
                           "reason": "缺少可核验的同池基准回测。"},
            "limitations": [],
        }
        research = research_override if research_override is not None else self._read_json("research.json")
        backtest = research.get("backtest")
        if (research.get("strategy_id") != strategy_id
                or research.get("strategy_version") != strategy_version
                or research.get("feature_version") != run.get("feature_version")
                or research.get("asof") != run.get("stock_signal_asof")
                or research.get("cost_rate") != run.get("cost_rate")
                or research.get("execution_assumption") != run.get("execution_assumption")
                or not isinstance(research.get("universe"), list)
                or len(research["universe"]) != run.get("stock_universe_count")
                or not isinstance(backtest, dict)
                or backtest.get("metrics") != run.get("backtest_metrics")):
            return [entry]
        metrics = self._metric_summary(backtest.get("metrics"))
        start, end, observations = (backtest.get("start"), backtest.get("end"),
                                    backtest.get("observations"))
        try:
            valid_period = (isinstance(start, str) and isinstance(end, str)
                            and date.fromisoformat(start) <= date.fromisoformat(end)
                            and type(observations) is int and observations > 0)
        except ValueError:
            valid_period = False
        if metrics is None or not valid_period:
            entry["backtest"]["reason"] = "回测指标或样本区间无效。"
            return [entry]
        published_at = research.get("generated_at")
        run_published_at = run.get("generated_at")
        if (not isinstance(published_at, str) or not isinstance(run_published_at, str)
                or published_at > run_published_at):
            entry["backtest"]["reason"] = "研究产物晚于运行清单，无法确认同次发布。"
            return [entry]
        entry["parameters"] = self._parameter_summary(research.get("strategy_parameters"))
        limitations = research.get("limitations")
        if isinstance(limitations, list):
            entry["limitations"] = [item[:240] for item in limitations[:8]
                                    if isinstance(item, str)]
        benchmarks: dict[str, Any] = {}
        source_benchmarks = backtest.get("benchmarks")
        if isinstance(source_benchmarks, dict):
            for key in ("focus_equal_weight_hold", "csi300_price_reference"):
                item = source_benchmarks.get(key)
                if isinstance(item, dict):
                    benchmark_metrics = self._metric_summary(item.get("metrics"))
                    benchmarks[key] = {
                        "status": "available" if item.get("status") == "available"
                                  and benchmark_metrics is not None else "unavailable",
                        "reference_only": item.get("reference_only") is True,
                        "metrics": benchmark_metrics,
                    }
        retrospective = backtest.get("retrospective_split")
        retrospective_summary: dict[str, Any] | None = None
        if isinstance(retrospective, dict):
            retrospective_summary = {key: retrospective.get(key) for key in (
                "status", "boundary_date", "independent_out_of_sample", "reason")}
            for key in ("early_period", "recent_period"):
                period = retrospective.get(key)
                if isinstance(period, dict):
                    retrospective_summary[key] = {field: period.get(field) for field in (
                        "start", "end", "sessions")}
                    retrospective_summary[key]["strategy_metrics"] = self._metric_summary(
                        period.get("strategy_metrics"))
                    retrospective_summary[key]["equal_hold_metrics"] = self._metric_summary(
                        period.get("equal_hold_metrics"))
        prospective = backtest.get("prospective_out_of_sample")
        if not isinstance(prospective, dict):
            prospective = {"status": "unavailable", "reason": "缺少独立样本外跟踪记录。"}
        entry["backtest"] = {
            "status": "available",
            "start": start, "end": end, "observations": observations,
            "metrics": metrics,
            "benchmarks": benchmarks,
            "retrospective_split": retrospective_summary,
            "prospective_out_of_sample": {key: prospective.get(key) for key in (
                "status", "matured_sessions", "reason")},
            "execution_assumption": research.get("execution_assumption"),
            "cost_rate": research.get("cost_rate"),
            "universe": research["universe"][:10],
            "input_fingerprint_sha256": backtest.get("input_fingerprint_sha256"),
        }
        entry["north_star"] = self._north_star_summary(backtest)
        return [entry]

    def _archived_strategy_status(self) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        """Project hash-checked research snapshots into bounded console cards."""
        directory = self.report_dir.parent / "strategy-runs"
        try:
            records = list_strategy_snapshots(directory, limit=30)
            latest_records = list_latest_strategy_snapshots(
                directory, {item["strategy_id"] for item in strategy_catalog()})
            total_records = sum(1 for day in directory.iterdir()
                                if day.is_dir() and not day.is_symlink()
                                for path in day.glob("*.json")
                                if path.is_file() and not path.is_symlink()) if directory.exists() else 0
        except (OSError, UnicodeError, ValueError, TypeError):
            reason = "策略归档无法通过完整性校验；本次只显示已发布运行结果。"
            return [], {"status": "unavailable", "record_count": 0, "reason": reason}, {
                "status": "unavailable", "recent": [], "reason": reason}
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        experiments: list[dict[str, Any]] = []
        for record in records:
            research = record["research"]
            backtest = research["backtest"]
            prospective = backtest.get("prospective_out_of_sample")
            prospective = prospective if isinstance(prospective, dict) else {}
            if len(experiments) < 20:
                experiments.append({
                    "experiment_id": record["record_id"],
                    "snapshot_record_id": record["record_id"],
                    "recorded_at": record["generated_at"],
                    "origin": record["origin"],
                    "source_run_id": record.get("source_run_id"),
                    "strategy_id": research["strategy_id"],
                    "strategy_version": research["strategy_version"],
                    "asof": research["asof"],
                    "state": "retrospective_research",
                    "metrics": self._metric_summary(backtest.get("metrics")),
                    "north_star": self._north_star_summary(backtest),
                    "validation": {
                        "prospective_status": prospective.get("status", "unavailable"),
                        "matured_sessions": prospective.get("matured_sessions"),
                    },
                    "input_fingerprint_sha256": backtest.get("input_fingerprint_sha256"),
                    "parameters": self._parameter_summary(research.get("strategy_parameters")),
                    "cost_rate": research.get("cost_rate"),
                    "execution_assumption": research.get("execution_assumption"),
                    "universe_count": len(research.get("universe", [])),
                    "adjustment": research.get("adjustment"),
                })
        selected_records = {record["research"]["strategy_id"]: record for record in latest_records}
        for record in records:
            selected_records.setdefault(record["research"]["strategy_id"], record)
        for record in selected_records.values():
            research = record["research"]
            backtest = research["backtest"]
            source_run_id = record.get("source_run_id")
            run = {
                "run_id": source_run_id,
                "generated_at": record["generated_at"],
                "strategy_id": research.get("strategy_id"),
                "strategy_version": research.get("strategy_version"),
                "feature_version": research.get("feature_version"),
                "stock_signal_asof": research.get("asof"),
                "stock_universe_scope": "focus_stock_pilot_only"
                if len(research.get("universe", [])) == 3 else "archived_research",
                "stock_universe_count": len(research.get("universe", [])),
                "cost_rate": research.get("cost_rate"),
                "execution_assumption": research.get("execution_assumption"),
                "backtest_metrics": backtest.get("metrics"),
            }
            projected = self._strategy_status(run, research_override=research)
            if not projected:
                continue
            entry = projected[0]
            entry.update(origin=record["origin"], record_id=record["record_id"],
                         research_sha256=record["research_sha256"],
                         recorded_at=record["generated_at"])
            key = (entry["strategy_id"], entry["strategy_version"])
            if key not in latest:
                latest[key] = entry
        status = "ready" if records else "empty"
        return (list(latest.values()), {"status": status, "record_count": total_records,
                                        "recent_record_count": len(records),
                                        "verified_scope": "recent_and_latest_per_registered_strategy"},
                {"status": status, "recent": experiments})

    @staticmethod
    def _forecast_experiment_projection(record: dict[str, Any], dataset: str
                                        ) -> dict[str, Any]:
        """Keep large, private per-stock predictions out of the console API."""
        payload = record["payload"]
        providers = payload["providers"]
        if (not isinstance(providers, list) or not providers
                or not isinstance(payload.get("input_fingerprint_sha256"), str)
                or not isinstance(payload.get("price_basis"), str)):
            raise ValueError("invalid forecast experiment metadata")

        def metric(value: object) -> float | None:
            return float(value) if (type(value) in (int, float)
                                    and math.isfinite(value)) else None

        summaries: list[dict[str, Any]] = []
        for provider in providers[:12]:
            if (not isinstance(provider, dict)
                    or not isinstance(provider.get("name"), str)
                    or not isinstance(provider.get("status"), str)
                    or not isinstance(provider.get("pooled"), dict)):
                raise ValueError("invalid forecast provider summary")
            horizons: dict[str, dict[str, Any]] = {}
            for horizon in ("5", "20"):
                pooled = provider["pooled"].get(horizon)
                if not isinstance(pooled, dict):
                    continue
                focus_coverage = provider.get("comparison_coverage")
                focus_horizon = (focus_coverage.get(horizon)
                                 if isinstance(focus_coverage, dict) else None)
                coverage = (focus_horizon.get("fraction")
                            if dataset == "focus" and isinstance(focus_horizon, dict)
                            else pooled.get("comparison_coverage") if dataset == "qlib" else None)
                skill_key = ("mae_return_skill_vs_random_walk" if dataset == "focus"
                             else "mae_return_skill_vs_random_walk_on_same_cases")
                horizons[horizon] = {
                    "status": str(pooled.get("status", "unavailable"))[:40],
                    "sample_count": pooled.get("count") if type(pooled.get("count")) is int else None,
                    "coverage": metric(coverage),
                    "mae_return_skill_vs_random_walk": metric(pooled.get(skill_key)),
                }
            summaries.append({
                "name": provider["name"][:80],
                "model_id": str(provider.get("model_id") or "")[:120],
                "model_revision": (str(provider["model_revision"])[:80]
                                   if provider.get("model_revision") is not None else None),
                "status": provider["status"][:50],
                "reason": str(provider.get("reason") or "")[:80] or None,
                "pretraining_overlap_status": str(provider.get("pretraining_overlap_status") or "")[:40],
                "stocks_with_samples": (provider.get("stocks_with_samples")
                                        if type(provider.get("stocks_with_samples")) is int else None),
                "horizons": horizons,
            })
        if not summaries:
            raise ValueError("forecast experiment has no providers")
        focus = dataset == "focus"
        cohort_selection = payload.get("cohort_selection")
        universe = payload.get("universe")
        configuration = payload.get("configuration")
        return {
            "record_id": str(record["record_id"])[:80],
            "created_at": str(record["created_at"])[:80],
            "dataset": dataset,
            "benchmark_version": str(payload.get("benchmark_version") or "")[:80],
            "experiment_key_sha256": str(payload.get("experiment_key_sha256") or "")[:64],
            "input_fingerprint_sha256": payload["input_fingerprint_sha256"][:64],
            "price_basis": payload["price_basis"][:60],
            "data_start": payload.get("data_start") if focus else payload.get("calendar_start"),
            "data_asof": payload.get("data_asof") if focus else payload.get("calendar_end"),
            "stock_count": (len(universe) if focus and isinstance(universe, list) else
                            cohort_selection.get("selected_count") if isinstance(cohort_selection, dict) else None),
            "common_sessions": payload.get("common_sessions") if focus else None,
            "horizon_unit": ("common_trading_sessions" if focus else
                             str(payload.get("horizon_unit") or "")[:60]),
            "configuration": {key: configuration.get(key) for key in ("min_context", "step")}
                             if isinstance(configuration, dict) else {},
            "research_only": payload.get("research_only") is True,
            "point_in_time_validated": payload.get("point_in_time_validated") is True,
            "prospective_out_of_sample": payload.get("prospective_out_of_sample") is True,
            "providers": summaries,
            "provider_count": len(providers),
        }

    def _forecast_experiment_status(self) -> dict[str, Any]:
        """Read at most 20 candidate records per dataset, newest verified first."""
        datasets: list[dict[str, Any]] = []
        for dataset, folder, suffix, reader in (
            ("focus", "forecast-experiments", ".json", read_benchmark_record),
            ("qlib", "forecast-experiments-qlib", ".json.gz", read_qlib_benchmark_record),
        ):
            root = self.report_dir.parent / folder
            source: dict[str, Any] = {"dataset": dataset, "status": "empty", "latest": None}
            if not root.exists():
                datasets.append(source)
                continue
            if root.is_symlink() or not root.is_dir():
                source.update(status="unavailable", reason="预测实验归档路径不可校验。")
                datasets.append(source)
                continue
            try:
                checked = 0
                invalid = 0
                days = (day for day in sorted(root.iterdir(), key=lambda item: item.name,
                                               reverse=True)
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name)
                        and day.is_dir() and not day.is_symlink())
                for day in days:
                    for path in sorted(day.iterdir(), key=lambda item: item.name, reverse=True):
                        if not path.name.endswith(suffix) or path.is_symlink() or not path.is_file():
                            continue
                        checked += 1
                        try:
                            source["latest"] = self._forecast_experiment_projection(reader(path), dataset)
                        except (OSError, UnicodeError, ValueError, TypeError, KeyError, EOFError):
                            invalid += 1
                        if source["latest"] is not None or checked >= 20:
                            break
                    if source["latest"] is not None or checked >= 20:
                        break
                source["status"] = ("partial" if invalid else "ready") if source["latest"] else (
                    "unavailable" if invalid else "empty")
                if invalid:
                    source["reason"] = "较新的预测实验记录未通过完整性或结构校验，已跳过。"
            except OSError:
                source.update(status="unavailable", reason="预测实验归档暂不可读取。")
            datasets.append(source)
        return {"status": "ready" if any(item["latest"] for item in datasets) else
                "unavailable" if any(item["status"] == "unavailable" for item in datasets)
                else "empty", "datasets": datasets}

    @staticmethod
    def _forecast_stability_projection(record: dict[str, Any]) -> dict[str, Any]:
        """Expose only bounded headline metrics from a verified stability audit."""
        payload = record["payload"]
        if (payload.get("event_mask_use") != "retrospective_diagnostic_only"
                or not isinstance(payload.get("model_name"), str)
                or not isinstance(payload.get("source_benchmark_record_id"), str)
                or not isinstance(payload.get("event_study_record_id"), str)
                or not isinstance(payload.get("horizons"), dict)):
            raise ValueError("invalid stability audit linkage")

        def metric(value: object) -> float | None:
            return float(value) if type(value) in (int, float) and math.isfinite(value) else None

        def count(value: object) -> int:
            if type(value) is not int or value < 0:
                raise ValueError("invalid stability count")
            return value

        horizons: dict[str, Any] = {}
        allowed = {"broadly_stable", "conditional_scope_only", "coarse_or_unstable"}
        for horizon in ("5", "20"):
            item = payload["horizons"].get(horizon)
            if item is None:
                continue
            if not isinstance(item, dict) or item.get("tier") not in allowed:
                raise ValueError("invalid stability tier")
            all_cases, core, stress = (item.get(key) for key in
                                       ("all_cases", "core", "event_stress"))
            if any(not isinstance(part, dict) for part in (all_cases, core, stress)):
                raise ValueError("invalid stability slices")
            total, ordinary, pressured = (count(part.get("count")) for part in
                                          (all_cases, core, stress))
            if total != ordinary + pressured or total != count(item.get("candidate_samples")):
                raise ValueError("inconsistent stability slices")
            interval = all_cases.get("block_bootstrap_95pct")
            if not isinstance(interval, list) or len(interval) != 2:
                raise ValueError("invalid stability interval")
            horizons[horizon] = {
                "tier": item["tier"],
                "recommendation": str(item.get("recommendation") or "")[:60],
                "sample_count": total,
                "core_count": ordinary,
                "event_stress_count": pressured,
                "candidate_coverage": metric(item.get("candidate_coverage")),
                "mae_return_skill_vs_random_walk": metric(
                    all_cases.get("paired_mae_return_skill_vs_random_walk")),
                "block_bootstrap_95pct": [metric(value) for value in interval],
            }
        if not horizons:
            raise ValueError("stability audit has no supported horizon")
        return {
            "record_id": str(record["record_id"])[:80],
            "created_at": str(payload["created_at"])[:80],
            "model_name": payload["model_name"][:80],
            "model_revision": str(payload.get("model_revision") or "")[:80],
            "source_benchmark_record_id": payload["source_benchmark_record_id"][:80],
            "event_study_record_id": payload["event_study_record_id"][:80],
            "event_mask_use": "retrospective_diagnostic_only",
            "point_in_time_validated": payload.get("point_in_time_validated") is True,
            "prospective_out_of_sample": payload.get("prospective_out_of_sample") is True,
            "horizons": horizons,
        }

    def _forecast_stability_status(self) -> dict[str, Any]:
        root = self.report_dir.parent / "forecast-stability"
        empty = {"status": "empty", "latest": []}
        if not root.exists():
            return empty
        if root.is_symlink() or not root.is_dir():
            return {"status": "unavailable", "latest": [],
                    "reason": "预测稳定性归档路径不可校验。"}
        try:
            candidates: list[dict[str, Any]] = []
            checked = invalid = 0
            days = (day for day in sorted(root.iterdir(), key=lambda item: item.name,
                                           reverse=True)
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name)
                    and day.is_dir() and not day.is_symlink())
            for day in days:
                for path in sorted(day.iterdir(), key=lambda item: item.stat().st_mtime,
                                   reverse=True):
                    if path.suffix != ".json" or path.is_symlink() or not path.is_file():
                        continue
                    checked += 1
                    try:
                        candidates.append(self._forecast_stability_projection(
                            read_stability_record(path)))
                    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
                        invalid += 1
                    if checked >= 40:
                        break
                if checked >= 40:
                    break
            newest: dict[str, dict[str, Any]] = {}
            for candidate in candidates:
                model = candidate["model_name"]
                if model not in newest or candidate["created_at"] > newest[model]["created_at"]:
                    newest[model] = candidate
            latest = sorted(newest.values(), key=lambda item: item["created_at"], reverse=True)[:6]
            return {"status": ("partial" if invalid else "ready") if latest else
                    "unavailable" if invalid else "empty", "latest": latest,
                    **({"reason": "部分预测稳定性记录未通过完整性或结构校验。"} if invalid else {})}
        except OSError:
            return {"status": "unavailable", "latest": [],
                    "reason": "预测稳定性归档暂不可读取。"}

    @staticmethod
    def _group_behavior_projection(record: dict[str, Any]) -> dict[str, Any]:
        payload = record["payload"]
        if (payload.get("research_only") is not True
                or payload.get("institution_identity_available") is not False
                or payload.get("actual_net_buying_observed") is not False
                or payload.get("event_origin_policy") !=
                "last_close_before_verified_announcement_clock"
                or not isinstance(payload.get("model"), dict)
                or not isinstance(payload.get("horizons"), dict)):
            raise ValueError("invalid group behaviour pilot contract")

        def metric(value: object) -> float | None:
            return float(value) if type(value) in (int, float) and math.isfinite(value) else None

        def count(value: object) -> int:
            if type(value) is not int or value < 0:
                raise ValueError("invalid group behaviour sample count")
            return value

        horizons: dict[str, Any] = {}
        for horizon in ("5", "20"):
            detail = payload["horizons"].get(horizon)
            if not isinstance(detail, dict):
                continue
            regular = detail.get("regular")
            stress = detail.get("event_stress")
            regular_summary = regular.get("summary") if isinstance(regular, dict) else None
            stress_summary = stress.get("summary") if isinstance(stress, dict) else None
            if not isinstance(regular_summary, dict) or not isinstance(stress_summary, dict):
                raise ValueError("invalid group behaviour summary")
            if (regular_summary.get("model_and_baseline_sample_keys_identical") is not True
                    or stress_summary.get("model_and_baseline_sample_keys_identical") is not True):
                raise ValueError("group behaviour cases are not paired")
            regular_count = count(regular_summary.get("paired_count"))
            candidate_count = count(regular_summary.get("candidate_origins"))
            coverage = metric(regular_summary.get("paired_coverage"))
            if (regular_count > candidate_count or coverage is None
                    or abs(coverage - (regular_count / candidate_count if candidate_count else 0)) > 1e-9):
                raise ValueError("invalid group behaviour coverage")
            horizons[horizon] = {
                "regular_paired_count": regular_count,
                "regular_candidate_origins": candidate_count,
                "regular_coverage": coverage,
                "regular_mae_skill_vs_zero_return": metric(
                    regular_summary.get("mae_skill_vs_zero_return")),
                "policy_stress_paired_count": count(stress_summary.get("paired_count")),
                "policy_stress_mae_skill_vs_zero_return": metric(
                    stress_summary.get("mae_skill_vs_zero_return")),
            }
        if not horizons:
            raise ValueError("group behaviour pilot has no supported horizons")
        return {
            "record_id": str(record["record_id"])[:80],
            "created_at": str(payload["created_at"])[:80],
            "model_name": str(payload.get("model", {}).get("name") or "")[:80],
            "cohort_size": count(payload.get("cohort_size")),
            "calendar_end": str(payload.get("calendar_end") or "")[:10],
            "reviewed_event_count": count(len(payload.get("reviewed_events", []))),
            "point_in_time_validated": payload.get("point_in_time_validated") is True,
            "prospective_out_of_sample": payload.get("prospective_out_of_sample") is True,
            "event_origin_policy": payload["event_origin_policy"],
            "horizons": horizons,
        }

    def _group_behavior_status(self) -> dict[str, Any]:
        root = self.report_dir.parent / "group-behavior-experiments"
        if not root.exists():
            return {"status": "empty", "latest": None}
        if root.is_symlink() or not root.is_dir():
            return {"status": "unavailable", "latest": None,
                    "reason": "群体量价实验归档路径不可校验。"}
        try:
            invalid = checked = 0
            days = (day for day in sorted(root.iterdir(), key=lambda item: item.name,
                                           reverse=True)
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name)
                    and day.is_dir() and not day.is_symlink())
            for day in days:
                for path in sorted(day.iterdir(), key=lambda item: item.name, reverse=True):
                    if not path.name.endswith(".json.gz") or path.is_symlink() or not path.is_file():
                        continue
                    checked += 1
                    try:
                        latest = self._group_behavior_projection(read_group_behavior_record(path))
                        return {"status": "partial" if invalid else "ready", "latest": latest,
                                **({"reason": "较新群体量价记录未通过完整性或结构校验。"}
                                   if invalid else {})}
                    except (OSError, UnicodeError, ValueError, TypeError, KeyError, EOFError):
                        invalid += 1
                    if checked >= 20:
                        break
                if checked >= 20:
                    break
            return {"status": "unavailable" if invalid else "empty", "latest": None,
                    **({"reason": "群体量价实验记录未通过完整性或结构校验。"}
                       if invalid else {})}
        except OSError:
            return {"status": "unavailable", "latest": None,
                    "reason": "群体量价实验归档暂不可读取。"}

    def _raw_data_inventory_status(self) -> dict[str, Any]:
        """Project one saved inventory snapshot; never inspect a live source here."""
        directory = self.report_dir.parent / "data"
        unavailable = {"status": "unavailable", "reason": "原始数据盘点快照缺失或未通过校验。"}
        try:
            candidates = sorted((path for path in directory.iterdir()
                                 if re.fullmatch(r"raw-data-inventory-\d{4}-\d{2}-\d{2}\.json",
                                                 path.name)
                                 and path.is_file() and not path.is_symlink()),
                                key=lambda path: path.name, reverse=True)
            if not candidates:
                return unavailable
            path = candidates[0]
            with path.open("rb") as stream:
                raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                return unavailable
            report = json.loads(raw)
            if (not isinstance(report, dict)
                    or report.get("inventory_version") != INVENTORY_VERSION
                    or not isinstance(report.get("sources"), dict)):
                return unavailable
            generated = datetime.fromisoformat(report["generated_at_utc"])
            if (generated.tzinfo is None or generated.date().isoformat() != path.name[-15:-5]
                    or generated > datetime.now(timezone.utc) + timedelta(minutes=5)):
                return unavailable
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
            return unavailable

        def natural(value: object) -> int:
            if type(value) is not int or value < 0:
                raise ValueError("invalid inventory count")
            return value

        def sqlite_source(key: str, *, historical: bool) -> dict[str, Any]:
            source = report["sources"].get(key)
            if not isinstance(source, dict) or source.get("status") != "ready":
                return {"status": "unavailable", "reason": "该来源未完成盘点。"}
            try:
                snapshot = source["snapshot"]
                if (not isinstance(snapshot, dict)
                        or snapshot.get("method") != "single_sqlite_read_transaction"
                        or snapshot.get("quick_check") != "ok"
                        or snapshot.get("sql_writes") is not False
                        or source.get("role") != ("baostock_archive" if historical else "main_store")):
                    raise ValueError("invalid source snapshot")
                summary = {
                    "schema_version": source["schema_version"],
                    "groups": source["bar_groups"],
                    "year_exchange": source["year_exchange_coverage"],
                    "status_fields": source["status_fields"],
                    "catalogue": source["catalogue"],
                    "window_counts": source["backfill_window_counts"],
                    "latest_run": source["latest_run"],
                }
                digest = hashlib.sha256(json.dumps(
                    summary, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                ).encode("utf-8")).hexdigest()
                if digest != snapshot.get("summary_sha256"):
                    raise ValueError("summary hash mismatch")
                groups = source["bar_groups"]
                if not isinstance(groups, list):
                    raise ValueError("invalid grouped bars")
                stock_groups = [item for item in groups if isinstance(item, dict)
                                and item.get("asset_type") == "stock"
                                and item.get("adjustment") == ("none" if historical else "qfq")]
                stock_bars = natural(source["stock_bar_rows"])
                stock_count = natural(source["stock_instrument_count"])
                if not stock_groups or sum(natural(item["bar_rows"]) for item in stock_groups) != stock_bars:
                    raise ValueError("stock total differs from groups")
                volume_units = sorted({str(item.get("volume_unit")) for item in stock_groups})
                if volume_units != (["share"] if historical else ["lot"]):
                    raise ValueError("unexpected source volume unit")
                amount_cny_rows = sum(natural(item["amount_observed_rows"])
                                      for item in stock_groups if item.get("amount_unit") == "CNY")
                amount_null_rows = sum(natural(item["amount_null_rows"]) for item in stock_groups)
                latest_run = source.get("latest_run")
                latest_run = latest_run if isinstance(latest_run, dict) else {}
                result: dict[str, Any] = {
                    "status": "ready", "stock_count": stock_count, "bar_count": stock_bars,
                    "volume_unit": volume_units[0], "amount_unit": "CNY where observed",
                    "amount_cny_rows": amount_cny_rows, "amount_null_rows": amount_null_rows,
                    "latest_run": {
                        "run_id": str(latest_run.get("run_id") or "")[:80],
                        "status_at_snapshot": str(latest_run.get("status") or "unknown")[:40],
                    },
                    "summary_sha256": digest,
                    "point_in_time_eligible": source.get("point_in_time_eligible") is True,
                }
                if historical:
                    rows = source["year_exchange_coverage"]
                    if not isinstance(rows, list) or len(rows) > 200:
                        raise ValueError("invalid year matrix")
                    matrix: dict[int, dict[str, int]] = {}
                    for row in rows:
                        if (not isinstance(row, dict) or row.get("exchange") not in ("SH", "SZ")
                                or not isinstance(row.get("year"), str)
                                or re.fullmatch(r"20\d{2}", row["year"]) is None):
                            raise ValueError("invalid year/exchange row")
                        year = int(row["year"])
                        exchange = row["exchange"]
                        cells = matrix.setdefault(year, {})
                        if exchange in cells:
                            raise ValueError("duplicate year/exchange row")
                        cells[exchange] = natural(row["bar_rows"])
                    years = sorted(matrix)
                    if (not years or years != list(range(2000, years[-1] + 1))
                            or any(set(matrix[year]) != {"SH", "SZ"} for year in years)):
                        raise ValueError("incomplete year/exchange matrix")
                    zero_years = [str(year) for year in years
                                  if matrix[year]["SH"] + matrix[year]["SZ"] == 0]
                    if zero_years != source.get("zero_stock_years"):
                        raise ValueError("zero-year list differs from matrix")
                    status_fields = source["status_fields"]
                    catalogue = source["catalogue"]
                    if not isinstance(status_fields, dict) or not isinstance(catalogue, dict):
                        raise ValueError("missing raw-status or catalogue summary")
                    result.update({
                        "year_exchange": [{"year": year, "sh_bars": matrix[year]["SH"],
                                           "sz_bars": matrix[year]["SZ"]} for year in years],
                        "zero_stock_years": zero_years,
                        "status_rows": natural(status_fields["status_rows"]),
                        "raw_amount_cny_rows": natural(status_fields["raw_amount_cny_observed_rows"]),
                        "raw_amount_only_rows": natural(status_fields[
                            "canonical_amount_null_but_raw_amount_observed_rows"]),
                        "catalogue_stock_count": natural(catalogue["listing_count"]),
                        "archive_complete": source.get("archive_complete") is True,
                    })
                    if result["status_rows"] != stock_bars:
                        raise ValueError("raw-status count differs from bars")
                return result
            except (KeyError, TypeError, ValueError, OverflowError):
                return {"status": "unavailable", "reason": "该来源摘要未通过结构或哈希校验。"}

        def qlib_source() -> dict[str, Any]:
            source = report["sources"].get("qlib_release")
            if not isinstance(source, dict) or source.get("status") != "ready":
                return {"status": "unavailable", "reason": "Qlib 发布包盘点不可用。"}
            try:
                snapshot = source["snapshot"]
                features = source["features"]
                instruments = source["instrument_intervals"]
                calendar = source["calendar"]
                fields = {"open", "high", "low", "close", "volume", "amount",
                          "adjclose", "change", "factor", "vwap"}
                stock_count = natural(instruments["instrument_count"])
                if (not isinstance(snapshot, dict) or not isinstance(features, dict)
                        or set(features) != fields
                        or snapshot.get("method") != "fixed_manifest_and_published_index"
                        or snapshot.get("full_feature_content_reverified_by_inventory") is not False
                        or source.get("field_coverage_is_file_presence_only") is not True
                        or source.get("valid_value_counts_require_binary_scan") is not True
                        or source.get("price_basis") != "qlib_adjusted"):
                    raise ValueError("invalid Qlib file-presence summary")
                for item in features.values():
                    if (not isinstance(item, dict)
                            or natural(item.get("feature_file_count")) != stock_count
                            or item.get("valid_value_count") is not None):
                        raise ValueError("invalid Qlib feature count")
                return {
                    "status": "ready", "stock_count": stock_count,
                    "calendar_start": str(calendar["first"])[:10],
                    "calendar_end": str(calendar["last"])[:10],
                    "calendar_sessions": natural(calendar["sessions"]),
                    "feature_fields": sorted(fields), "feature_count": len(fields),
                    "files_per_feature": stock_count,
                    "field_coverage_is_file_presence_only": True,
                    "volume_unit": str(features["volume"].get("unit"))[:60],
                    "amount_unit": str(features["amount"].get("unit"))[:60],
                    "archive_sha256": str(snapshot.get("archive_sha256") or "")[:72],
                }
            except (KeyError, TypeError, ValueError, OverflowError):
                return {"status": "unavailable", "reason": "Qlib 文件盘点摘要结构不完整。"}

        main = sqlite_source("main_market_store", historical=False)
        historical = sqlite_source("historical_baostock_archive", historical=True)
        qlib = qlib_source()
        statuses = [main["status"], historical["status"], qlib["status"]]
        age_minutes = max(0, int((datetime.now(timezone.utc) - generated).total_seconds() // 60))
        return {
            "status": "ready" if all(item == "ready" for item in statuses) else
                      "partial" if any(item == "ready" for item in statuses) else "unavailable",
            "inventory_version": INVENTORY_VERSION,
            "snapshot_at": generated.isoformat(),
            "snapshot_age_minutes": age_minutes,
            "report_filename": path.name,
            "main": main, "historical": historical, "qlib": qlib,
        }

    def _optimization_status(self) -> dict[str, Any]:
        """Read saved parameter searches; this endpoint never runs a search."""
        try:
            records = list_optimization_records(self.report_dir.parent / "optimizations", limit=20)
            recent: list[dict[str, Any]] = []
            for record in records:
                payload = record["payload"]
                train = payload["train"]
                holdout = payload["historical_holdout"]
                grid = payload["candidate_grid"]
                index = payload["selected_candidate_index"]
                if (not isinstance(train, dict) or not isinstance(holdout, dict)
                        or not isinstance(grid, list) or type(index) is not int
                        or index < 0 or index >= len(grid)
                        or not isinstance(grid[index], dict)
                        or grid[index].get("candidate_index") != index
                        or grid[index].get("parameters") != payload.get("selected_parameters")):
                    raise ValueError("invalid optimization selection")
                selected = grid[index]
                train_metrics = self._metric_summary(selected.get("train_metrics"))
                train_benchmark = self._metric_summary(train.get("benchmark_metrics"))
                holdout_metrics = self._metric_summary(holdout.get("metrics"))
                holdout_benchmark = self._metric_summary(holdout.get("benchmark_metrics"))
                if None in (train_metrics, train_benchmark, holdout_metrics, holdout_benchmark):
                    raise ValueError("invalid optimization metrics")
                expected_train_score = score_backtest(train_metrics, train_benchmark)
                expected_holdout_score = score_backtest(holdout_metrics, holdout_benchmark)
                score_fields = ("policy_version", "status", "score", "scale",
                                "benchmark", "components", "normalization")
                def same_scored_fields(saved: Any, expected: dict[str, Any]) -> bool:
                    return isinstance(saved, dict) and all(
                        saved.get(field) == expected.get(field) for field in score_fields
                    )
                if (not same_scored_fields(selected.get("train_north_star"), expected_train_score)
                        or not same_scored_fields(holdout.get("north_star"), expected_holdout_score)):
                    raise ValueError("optimization score differs from versioned policy")
                recent.append({
                    "optimization_id": record["optimization_id"],
                    "created_at": record["created_at"],
                    "payload_sha256": record["payload_sha256"],
                    "optimizer_version": payload.get("optimizer_version"),
                    "strategy_id": payload.get("strategy_id"),
                    "strategy_version": payload.get("strategy_version"),
                    "objective_policy_version": payload.get("objective_policy_version"),
                    "canonical_contract_version": payload.get("canonical_contract_version"),
                    "input_fingerprint_sha256": payload.get("input_fingerprint_sha256"),
                    "universe_count": len(payload.get("universe", [])),
                    "candidate_count": len(grid),
                    "selected_candidate_index": index,
                    "selected_parameters": self._parameter_summary(payload.get("selected_parameters")),
                    "selection_rule": payload.get("selection_rule"),
                    "train": {"start": train.get("start"), "end": train.get("end"),
                              "sessions": train.get("sessions"),
                              "selected_metrics": train_metrics,
                              "selected_north_star": expected_train_score,
                              "benchmark_metrics": train_benchmark},
                    "historical_holdout": {
                        "status": holdout.get("status"), "start": holdout.get("start"),
                        "end": holdout.get("end"), "sessions": holdout.get("sessions"),
                        "metrics": holdout_metrics, "benchmark_metrics": holdout_benchmark,
                        "north_star": expected_holdout_score,
                        "reason": holdout.get("reason")},
                    "promotion_status": payload.get("promotion_status"),
                    "promotion_reason": payload.get("promotion_reason"),
                })
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
            return {"status": "unavailable", "recent": [],
                    "reason": "参数优化记录无法通过完整性或评分规则校验。"}
        return {"status": "ready" if recent else "pending", "recent": recent}

    def _append_task_event(self, task: dict[str, Any], event: str) -> None:
        """Append one private, hash-checked task event before/after execution."""
        if event not in ("accepted", "terminal"):
            raise ValueError("unknown task event")
        recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        payload = {key: task.get(key) for key in (
            "task_id", "kind", "strategy", "status", "created_at", "finished_at", "result")}
        payload["event"] = event
        payload["recorded_at"] = recorded_at
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        record = {"schema_version": 1, "payload_sha256": hashlib.sha256(canonical).hexdigest(),
                  "payload": payload}
        body = json.dumps(record, ensure_ascii=False, sort_keys=True,
                          allow_nan=False).encode("utf-8") + b"\n"
        if len(body) > 64_000:
            raise ValueError("task event too large")
        folder = self.report_dir.parent / "task-runs" / recorded_at[:10]
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        stamp = recorded_at.replace("-", "").replace(":", "").replace(".", "")
        final_path = folder / f"{stamp}-{task['task_id']}-{event}.json"
        temp_path = folder / f".{task['task_id']}.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp_path, final_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _task_status(self) -> dict[str, Any]:
        with self._task_lock:
            active = dict(self._active_task) if self._active_task else None
        root = self.report_dir.parent / "task-runs"
        if not root.exists():
            return {"status": "ready", "active": active, "recent": []}
        try:
            paths: list[Path] = []
            for folder in sorted((path for path in root.iterdir()
                                  if path.is_dir() and not path.is_symlink()), reverse=True)[:30]:
                for path in sorted(folder.glob("*.json"), reverse=True):
                    if path.is_symlink():
                        continue
                    paths.append(path)
                    if len(paths) >= 200:
                        break
                if len(paths) >= 200:
                    break
            recent: list[dict[str, Any]] = []
            seen: set[str] = set()
            for path in paths:
                if path.stat().st_size > 64_000:
                    raise ValueError("oversize task event")
                record = json.loads(path.read_text(encoding="utf-8"))
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict) or record.get("schema_version") != 1:
                    raise ValueError("invalid task event")
                canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"), allow_nan=False).encode("utf-8")
                if hashlib.sha256(canonical).hexdigest() != record.get("payload_sha256"):
                    raise ValueError("task event hash mismatch")
                task_id = payload.get("task_id")
                if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
                    raise ValueError("invalid task ID")
                if task_id in seen or (active and task_id == active["task_id"]):
                    continue
                seen.add(task_id)
                item = {key: payload.get(key) for key in (
                    "task_id", "kind", "strategy", "status", "created_at", "finished_at", "result")}
                if payload.get("event") == "accepted":
                    item["status"] = "interrupted"
                    item["finished_at"] = None
                    item["result"] = {"reason": "任务开始后服务重启或终态未能归档；请核对成果归档。"}
                elif payload.get("event") != "terminal":
                    raise ValueError("invalid task event kind")
                recent.append(item)
                if len(recent) == 10:
                    break
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            return {"status": "unavailable", "active": active, "recent": [],
                    "reason": "任务事件归档无法通过完整性校验。"}
        return {"status": "ready", "active": active, "recent": recent}

    def _optimize_task(self, strategy: str) -> tuple[str, dict[str, Any]]:
        strategy_ids = (["sma-trend", "cross-sectional-momentum", "mean-reversion-zscore"]
                        if strategy == "all" else [strategy])
        directory = self.report_dir.parent / "optimizations"
        existing = list_optimization_records(directory, limit=100)
        prior: dict[tuple[Any, ...], str] = {}
        for item in existing:
            payload = item["payload"]
            holdout = payload.get("historical_holdout") or {}
            key = (payload.get("strategy_id"), payload.get("strategy_version"),
                   payload.get("optimizer_version"), payload.get("objective_policy_version"),
                   payload.get("input_fingerprint_sha256"), payload.get("cost_rate"),
                   holdout.get("end"))
            prior.setdefault(key, item["optimization_id"])
        runs: list[dict[str, Any]] = []
        for strategy_id in strategy_ids:
            try:
                result = optimize_focus_stocks(self.store, strategy_id)
                key = (result["strategy_id"], result["strategy_version"],
                       result["optimizer_version"], result["objective_policy_version"],
                       result["input_fingerprint_sha256"], result["cost_rate"],
                       result["historical_holdout"]["end"])
                existing_id = prior.get(key)
                if existing_id:
                    run_status, optimization_id = "existing", existing_id
                else:
                    saved = append_optimization_record(directory, result)
                    run_status, optimization_id = "saved", saved["optimization_id"]
                    prior[key] = optimization_id
                selected = result["candidate_grid"][result["selected_candidate_index"]]
                runs.append({
                    "strategy_id": strategy_id, "status": run_status,
                    "optimization_id": optimization_id,
                    "data_asof": result["historical_holdout"]["end"],
                    "input_fingerprint_sha256": result["input_fingerprint_sha256"],
                    "train_score": selected["train_north_star"]["score"],
                    "holdout_score": result["historical_holdout"]["north_star"]["score"],
                    "promotion_status": result["promotion_status"],
                })
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                _LOGGER.exception("optimization task failed for %s", strategy_id)
                runs.append({"strategy_id": strategy_id, "status": "failed",
                             "reason": "数据面板不可用或优化归档失败；请检查量化数据与日志。"})
        failed = sum(item["status"] == "failed" for item in runs)
        status = "failed" if failed == len(runs) else "partial" if failed else "completed"
        return status, {"runs": runs}

    def _run_task(self, task: dict[str, Any]) -> None:
        try:
            if task["kind"] == "optimize":
                status, result = self._optimize_task(task["strategy"])
            else:
                result = review_feedback_cases(
                    self.store, self.report_dir.parent / "feedback-cases",
                    self.report_dir.parent / "case-reviews")
                status = "completed"
        except Exception:
            _LOGGER.exception("offline task execution failed: %s", task["task_id"])
            status, result = "failed", {"reason": "离线任务执行失败；请检查数据与本地日志。"}
        terminal = {**task, "status": status, "finished_at":
                    datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "result": result}
        try:
            self._append_task_event(terminal, "terminal")
        except Exception:
            _LOGGER.exception("offline task terminal event could not be saved: %s", task["task_id"])
            # The accepted event remains, and a restarted process will mark it
            # interrupted instead of claiming an unrecorded success.
            pass
        finally:
            with self._task_lock:
                if self._active_task and self._active_task["task_id"] == task["task_id"]:
                    self._active_task = None

    def _start_task(self, kind: str, *, strategy: str | None = None) -> dict[str, Any]:
        with self._task_lock:
            if self._active_task is not None:
                return {"status": "busy", "task_id": self._active_task["task_id"],
                        "kind": self._active_task["kind"],
                        "reason": "已有离线任务正在执行；请等待其完成。"}
            task = {"task_id": uuid4().hex, "kind": kind, "strategy": strategy,
                    "status": "running", "created_at":
                    datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "finished_at": None, "result": None}
            try:
                self._append_task_event(task, "accepted")
                self._active_task = task
                threading.Thread(target=self._run_task, args=(task,), daemon=True).start()
            except (OSError, RuntimeError, ValueError, TypeError):
                _LOGGER.exception("offline task could not start or persist accepted event")
                self._active_task = None
                return {"status": "storage_unavailable",
                        "reason": "任务无法写入本地事件归档或启动后台执行。"}
            return {"status": "accepted", "task_id": task["task_id"],
                    "kind": kind, "created_at": task["created_at"]}

    def submit_optimize_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        strategy = payload.get("strategy")
        if set(payload) != {"strategy"} or strategy not in (
                "all", "sma-trend", "cross-sectional-momentum", "mean-reversion-zscore"):
            return {"status": "invalid_input", "reason": "请选择已登记的策略或全部策略。"}
        return self._start_task("optimize", strategy=strategy)

    def submit_review_cases(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload:
            return {"status": "invalid_input", "reason": "复盘任务不接受附加参数。"}
        return self._start_task("review_cases")

    def console_status(self) -> dict[str, Any]:
        """A bounded, read-only view of published research and local operations."""
        run = self._read_json("run.json")
        run_fields = ("run_id", "generated_at", "market_broad_asof", "market_industry_asof",
                      "stock_signal_asof", "stock_screen_status", "recommendation_status",
                      "recommendation_count", "strategy_id", "strategy_version", "feature_version",
                      "stock_universe_scope", "stock_universe_count")
        published_run = {key: run.get(key) for key in run_fields}
        market_store = self._market_store_status()
        published_strategies = self._strategy_status(run)
        archived_strategies, strategy_archive, experiments = self._archived_strategy_status()
        strategies = {(item["strategy_id"], item["strategy_version"]): item
                      for item in archived_strategies}
        for item in published_strategies:
            key = (item["strategy_id"], item["strategy_version"])
            archived = strategies.get(key)
            if (archived is None or (isinstance(item.get("asof"), str)
                                    and isinstance(archived.get("asof"), str)
                                    and item["asof"] > archived["asof"])):
                strategies[key] = item
        for definition in strategy_catalog():
            key = (definition["strategy_id"], definition["strategy_version"])
            if key in strategies:
                strategies[key]["factor_ids"] = definition["factor_ids"]
                strategies[key]["description"] = definition["description"]
                strategies[key]["implementation_status"] = "implemented"
                continue
            strategies[key] = {
                "strategy_id": definition["strategy_id"],
                "strategy_version": definition["strategy_version"],
                "factor_ids": definition["factor_ids"],
                "description": definition["description"],
                "implementation_status": "implemented",
                "status": "implemented_unvalidated",
                "source_run_id": None,
                "asof": None,
                "parameters": definition["parameters"],
                "universe_scope": None,
                "universe_count": 0,
                "backtest": {"status": "unavailable",
                             "reason": "策略已实现，但尚无通过归档完整性校验的实跑回测。"},
                "north_star": {"policy_version": NORTH_STAR_VERSION,
                               "status": "unavailable", "reason": "尚无回测与同池基准。"},
                "limitations": ["需要在明确数据快照、成本与执行假设下运行并归档。"],
            }
        cases_dir = self.report_dir.parent / "feedback-cases"
        reviews_dir = self.report_dir.parent / "case-reviews"
        try:
            cases = list_feedback_cases(cases_dir, limit=20)
            feedback_cases = {"status": "ready" if cases else "pending", "recent": cases}
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            feedback_cases = {"status": "unavailable", "recent": [],
                              "reason": "回执记录无法通过完整性校验。"}
        try:
            review_records = list_case_reviews(reviews_dir, limit=20)
            reviews = {"status": "ready" if review_records else "pending",
                       "recent": review_records}
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            reviews = {"status": "unavailable", "recent": [],
                       "reason": "复盘记录无法通过完整性校验。"}
        if feedback_cases["status"] == "ready" and reviews["status"] == "ready":
            latest_review_by_case = {item["case_id"]: item for item in reversed(reviews["recent"])
                                     if isinstance(item.get("case_id"), str)}
            for case in feedback_cases["recent"]:
                review = latest_review_by_case.get(case["case_id"])
                if review:
                    case["review_status"] = review["status"]
                    case["latest_review_id"] = review["review_id"]
                    case["latest_review_horizon_sessions"] = review.get("horizon_sessions")
        return {
            "status": "ready" if market_store["status"] == "ready" and run.get("run_id") else "partial",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "china_today": datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
            "published_run": published_run,
            "market_store": market_store,
            "historical_archive": self._historical_archive(),
            "historical_backfill_batch": self._historical_backfill_batch(),
            "qlib_archive": self._qlib_archive(),
            "research_journal": self._research_journal_status(),
            "strategy_archive": strategy_archive,
            "strategies": list(strategies.values()),
            "algorithm_components": algorithm_catalog(self.report_dir.parent),
            "factors": factor_catalog(),
            "experiments": experiments,
            "forecast_experiments": self._forecast_experiment_status(),
            "forecast_stability": self._forecast_stability_status(),
            "group_behavior_pilot": self._group_behavior_status(),
            "raw_data_inventory": self._raw_data_inventory_status(),
            "optimizations": self._optimization_status(),
            "feedback_cases": feedback_cases,
            "reviews": reviews,
            "tasks": self._task_status(),
            "rules": [
                {"rule_id": "single-stock-action-reference", "version": RULE_VERSION,
                 "backtest_status": "not_evaluated",
                 "reason": "个股操作参考规则尚无独立策略回测，不展示策略收益。"},
                {"rule_id": SIGNAL_RULE_ID, "version": SIGNAL_RULE_VERSION,
                 "backtest_status": "not_evaluated",
                 "reason": "图表买卖标记只是历史信号，未单独模拟成交与收益。"},
            ],
            "versions": {"decision_policy": RULE_VERSION, "chart": CHART_VERSION,
                         "stock_screen": SCREEN_VERSION, "backfill": BACKFILL_VERSION,
                         "north_star": NORTH_STAR_VERSION,
                         "algorithm_registry": ALGORITHM_REGISTRY_VERSION},
        }

    def submit_feedback_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save a human receipt linked to a hash-checked research report."""
        allowed = {"research_record_id", "research_record_date", "decision",
                   "observed_date", "executed_price", "quantity", "note"}
        if set(payload) - allowed:
            return {"status": "invalid_input", "reason": "回执包含不支持的字段。"}
        try:
            saved = append_feedback_case(
                self.report_dir.parent / "feedback-cases", self.journal_dir,
                research_record_id=payload.get("research_record_id"),
                research_record_date=payload.get("research_record_date"),
                decision=payload.get("decision"),
                observed_date=payload.get("observed_date"),
                executed_price=payload.get("executed_price"),
                quantity=payload.get("quantity"),
                note=payload.get("note", ""),
            )
        except (ValueError, TypeError, OverflowError):
            return {"status": "invalid_input",
                    "reason": "回执数据或关联研究记录无效；请核对记录、日期及成交信息。"}
        except OSError:
            return {"status": "storage_unavailable",
                    "reason": "本地回执库暂不可写入，请检查报告目录权限。"}
        return {"status": "saved", "case_id": saved["case_id"],
                "created_at": saved["created_at"],
                "instrument_id": saved["instrument_id"],
                "decision": saved["decision"],
                "review_status": saved["review_status"]}

    def stock_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = payload.get("symbol")
        cost_price = payload.get("cost_price")
        if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 40:
            return {"status": "invalid_input", "reason": "请输入股票代码或已登记的名称。"}
        if cost_price in ("", None):
            cost_price = None
        elif isinstance(cost_price, bool):
            return {"status": "invalid_input", "reason": "成本价须为正数。"}
        else:
            try:
                cost_price = float(cost_price)
            except (TypeError, ValueError):
                return {"status": "invalid_input", "reason": "成本价须为正数。"}
        try:
            query = symbol.strip()
            primary = analyze_stock(self.store, query, cost_price=cost_price)
            if primary["status"] != "unknown_stock":
                return self._finalize_stock_report(primary, query=query, cost_price=cost_price)
            # The local Qlib release is a separate, read-only research source.
            # Do not promote its bars into the current screening/backtest store.
            normalized = query.upper().removeprefix("STOCK:")
            if re.fullmatch(r"(SH|SZ|BJ)\.[0-9]{6}", normalized):
                normalized = normalized[3:] + "." + normalized[:2]
            if not (re.fullmatch(r"[0-9]{6}", normalized)
                    or re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", normalized)
                    or re.fullmatch(r"(SH|SZ|BJ)[0-9]{6}", normalized)):
                return primary
            summary = self._qlib_archive()
            if summary is None:
                return primary
            release_dir = self.store.path.parent / "qlib-releases" / summary["release_tag"]
            published = release_dir / "published"
            manifest = release_dir / "qlib_bin.manifest.json"
            if not published.is_dir() or not manifest.is_file():
                return primary
            report = analyze_qlib_stock(
                published, manifest, summary["release_tag"], normalized,
                cost_price=cost_price,
            )
            report["query"] = query
            if report.get("instrument_id"):
                names, name_snapshot_id = self._archived_stock_names()
                name = names.get(report["instrument_id"])
                if name and name_snapshot_id:
                    report["name"] = name
                    report["summary"] = report["summary"].replace(report["instrument_id"], name, 1)
                    if report.get("sources"):
                        report["sources"][0]["name_source_snapshot_id"] = name_snapshot_id
                    report["limitations"].append(
                        "股票名称来自另存的 BaoStock 清单快照，可能不是该历史日期的名称。"
                    )
            return self._finalize_stock_report(report, query=query, cost_price=cost_price)
        except (TypeError, ValueError) as exc:
            return {"status": "invalid_input", "reason": str(exc)}
        except (QlibArchiveError, OSError):
            return {"status": "stock_data_unavailable",
                    "reason": "独立历史归档当前无法校验或读取，请稍后重试。"}
        except sqlite3.Error:
            return {"status": "stock_data_unavailable",
                    "reason": "股票数据库暂不可读取；请先初始化数据或检查数据库路径。"}


def make_handler(service: QuantAPIService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - standard library naming
            if self.path == "/api/dashboard":
                self._json(HTTPStatus.OK, service.dashboard())
            elif self.path == "/api/console-status":
                self._json(HTTPStatus.OK, service.console_status())
            elif self.path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            else:
                self._json(HTTPStatus.NOT_FOUND, {"status": "not_found", "reason": "页面不存在。"})

        def do_POST(self) -> None:  # noqa: N802 - standard library naming
            managed_paths = ("/api/feedback-case", "/api/optimize-run", "/api/review-cases")
            if self.path not in ("/api/stock-report", *managed_paths):
                self._json(HTTPStatus.NOT_FOUND, {"status": "not_found", "reason": "接口不存在。"})
                return
            if (self.path in managed_paths
                    and self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    != "application/json"):
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                           {"status": "invalid_input", "reason": "请求必须使用 application/json。"})
                return
            if self.path in managed_paths:
                origin = self.headers.get("Origin")
                if origin and origin not in ("http://127.0.0.1:8765", "http://localhost:8765"):
                    self._json(HTTPStatus.FORBIDDEN,
                               {"status": "forbidden", "reason": "仅接受本地同源操作。"})
                    return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            max_length = 512 if self.path in ("/api/optimize-run", "/api/review-cases") else 8192
            if length < 1 or length > max_length:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "invalid_input", "reason": "请求内容长度无效。"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeError, ValueError):
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "invalid_input", "reason": "请求必须是 JSON。"})
                return
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "invalid_input", "reason": "请求必须是 JSON 对象。"})
                return
            if self.path == "/api/stock-report":
                result = service.stock_report(payload)
            elif self.path == "/api/feedback-case":
                result = service.submit_feedback_case(payload)
            elif self.path == "/api/optimize-run":
                result = service.submit_optimize_run(payload)
            else:
                result = service.submit_review_cases(payload)
            code = (HTTPStatus.BAD_REQUEST if result.get("status") == "invalid_input"
                    else HTTPStatus.SERVICE_UNAVAILABLE if result.get("status") in (
                        "stock_data_unavailable", "storage_unavailable")
                    else HTTPStatus.CONFLICT if result.get("status") == "busy"
                    else HTTPStatus.ACCEPTED if result.get("status") == "accepted"
                    else HTTPStatus.OK)
            self._json(code, result)

    return Handler


def serve_quant_api(store: MarketStore, report_dir: str | Path,
                    host: str = "127.0.0.1", port: int = 8766) -> None:
    container_bind = host == "0.0.0.0" and os.environ.get("QUANT_API_CONTAINER") == "1"
    if host not in ("127.0.0.1", "localhost", "::1") and not container_bind:
        raise ValueError("quant API only binds locally unless run inside the local Docker service")
    server = ThreadingHTTPServer((host, port), make_handler(QuantAPIService(store, report_dir)))
    server.daemon_threads = True
    print(f"量化 API：http://{host}:{server.server_port}/api/dashboard", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
