from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Sequence

from quant_lab.data_sources import SOURCE_CATALOG
from quant_lab.baostock_source import (BaoStockSource, read_baostock_snapshot,
                                        sync_baostock_pilot, write_baostock_snapshot)
from quant_lab.forecast import Momentum20Provider, RandomWalkProvider, TimesFM25Provider
from quant_lab.chronos_adapter import ChronosBoltTinyProvider
from quant_lab.forecast import EvaluationConfig
from quant_lab.forecast_benchmark import append_benchmark_record, evaluate_focus_benchmark
from quant_lab.forecast_qlib import append_qlib_benchmark_record, evaluate_qlib_cohort
from quant_lab.forecast_report import evaluate_focus_forecasts, write_forecast_report
from quant_lab.feedback_loop import review_feedback_cases
from quant_lab.market import build_market_pulse, write_market_pulse
from quant_lab.optimizer import append_optimization_record, optimize_focus_stocks
from quant_lab.pipeline import run_pipeline
from quant_lab.research import get_strategy, research_report, write_research_report
from quant_lab.strategy_registry import append_strategy_snapshot
from quant_lab.storage import MarketStore
from quant_lab.sync import sync_database
from quant_lab.verification import build_verification_report, write_report
from quant_lab.workflow import record_decision, review_decisions


DEFAULT_DB = "data/quant/market.sqlite3"
DEFAULT_REPORT = "reports/quant/verification.json"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="quant-lab", description="可审计的 A 股日线数据与策略验证工具"
    )
    root.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="初始化数据库")

    sync = commands.add_parser("sync", help="同步日线数据")
    sync.add_argument("--start", default="2021-01-01")
    sync.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    sync.add_argument(
        "--group",
        action="append",
        choices=["all", "core", "stocks", "broad", "industry"],
        help="可重复；不传 --stock 时默认 all",
    )
    sync.add_argument(
        "--stock", action="append",
        help="可重复；仅同步主库已登记股票，如 stock:603993.SH（最多 30 只）",
    )
    sync.add_argument("--workers", type=int, default=4)

    commands.add_parser("status", help="查看数据覆盖")
    commands.add_parser("sources", help="查看数据源目录")
    bao_pool = commands.add_parser("baostock-universe", help="取得当前沪深在市股票清单快照；不含北交所")
    bao_pool.add_argument("--out", help="快照文件路径；默认保存带日期和哈希的文件")
    bao_pilot = commands.add_parser("baostock-pilot", help="将显式选择的沪深股票同步到独立试点库")
    bao_pilot.add_argument("--snapshot", required=True, help="baostock-universe 生成的快照")
    bao_pilot.add_argument("--pilot-db", required=True, help="独立 SQLite 路径；不能是主库")
    bao_pilot.add_argument("--stock", action="append", required=True,
                           help="如 stock:002475.SZ；可重复，最多30只")
    bao_pilot.add_argument("--start", required=True, type=date.fromisoformat)
    bao_pilot.add_argument("--end", required=True, type=date.fromisoformat)

    verify = commands.add_parser("verify", help="验证数据和三套策略")
    verify.add_argument("--out", default=DEFAULT_REPORT)
    research = commands.add_parser("research", help="生成回测、当日信号及来源报告")
    research.add_argument("strategy", choices=["sma-trend", "cross-sectional-momentum", "mean-reversion-zscore"])
    research.add_argument("--date", type=date.fromisoformat, help="信号截至日期；默认库内最新共同交易日")
    research.add_argument("--cost-rate", type=float, default=0.001)
    research.add_argument("--out", default="reports/quant/research.json")
    optimize = commands.add_parser("optimize", help="按北极星目标做历史训练段参数搜索并保存留出段结果")
    optimize.add_argument("strategy", choices=["all", "sma-trend", "cross-sectional-momentum",
                                               "mean-reversion-zscore"])
    optimize.add_argument("--date", type=date.fromisoformat, help="只读取截至该日的数据")
    optimize.add_argument("--cost-rate", type=float, default=0.001)
    optimize.add_argument("--out-dir", default="reports/quant/optimizations")
    forecast = commands.add_parser("forecast", help="滚动评估预测器；模型输出不产生交易信号")
    forecast.add_argument("--provider", choices=["random-walk", "momentum-20", "timesfm-2.5"],
                          default="random-walk")
    forecast.add_argument("--date", type=date.fromisoformat, help="只使用截至该日的历史数据")
    forecast.add_argument("--model-revision", help="TimesFM 权重的 40 位 commit SHA")
    forecast.add_argument("--out", default="reports/quant/forecast-evaluation.json")
    benchmark = commands.add_parser(
        "forecast-benchmark", help="固定数据源与起点比较预测模型并保存只追加实验记录"
    )
    benchmark.add_argument("--provider", action="append",
                           choices=["momentum-20", "timesfm-2.5", "chronos-bolt-tiny"],
                           help="可重复；始终包括随机游走基线，默认还包括20日动量")
    benchmark.add_argument("--date", type=date.fromisoformat, help="只使用截至该日的数据")
    benchmark.add_argument("--dataset", choices=["focus", "qlib"], default="focus",
                           help="focus=主库三只关注股；qlib=已校验历史 Release 的分层样本")
    benchmark.add_argument("--qlib-root", default="data/quant/qlib-releases/2026-09-28/published")
    benchmark.add_argument("--qlib-manifest", default="data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json")
    benchmark.add_argument("--qlib-tag", default="2026-09-28")
    benchmark.add_argument("--per-stratum", type=int, default=3,
                           help="Qlib 每层最多抽取股票数；默认3")
    benchmark.add_argument("--timesfm-revision", help="TimesFM 权重的40位 commit SHA")
    benchmark.add_argument("--chronos-revision", help="Chronos-Bolt 权重的40位 commit SHA")
    benchmark.add_argument("--horizon", type=int, action="append", help="预测交易观测数；默认5、20")
    benchmark.add_argument("--min-context", type=int, default=120)
    benchmark.add_argument("--step", type=int, default=20)
    benchmark.add_argument("--out-dir", help="实验归档目录；按数据源分别设置默认值")
    market = commands.add_parser("market", help="宽基、简化行业与成交关注度观察")
    market.add_argument("--date", type=date.fromisoformat, help="截至日期；默认最新共同交易日")
    market.add_argument("--out", default="reports/quant/market-pulse.md")
    run = commands.add_parser("run", help="一键运行数据、特征、策略、回测、信号、报告闭环")
    run.add_argument("--strategy", choices=["sma-trend", "cross-sectional-momentum", "mean-reversion-zscore"], default="sma-trend")
    run.add_argument("--date", type=date.fromisoformat, help="截至日期；默认使用各数据源最新日")
    run.add_argument("--out-dir", default="reports/quant/latest")
    run.add_argument("--sync", action="store_true", help="先联网同步数据")
    run.add_argument("--sync-start", type=date.fromisoformat, default=date(2021, 1, 1))
    run.add_argument("--stock-snapshot", help="已校验的 BaoStock 沪深当前股票池快照")
    decide = commands.add_parser("decide", help="记录你对行业或股票候选的决定，不执行交易")
    decide.add_argument("candidate_id")
    decide.add_argument("choice", choices=["watch", "act", "skip"])
    decide.add_argument("--note", default="")
    review = commands.add_parser("review", help="复盘人工决定与已到期结果")
    review.add_argument("--out", default="reports/quant/latest/review.json")
    review_cases = commands.add_parser("review-cases", help="逐例复盘已保存回执的到期价格结果")
    review_cases.add_argument("--date", type=date.fromisoformat, help="最多复盘到该交易日")
    review_cases.add_argument("--cases-dir", default="reports/quant/feedback-cases")
    review_cases.add_argument("--reviews-dir", default="reports/quant/case-reviews")
    api = commands.add_parser("api", help="启动只读量化 JSON API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8766)
    api.add_argument("--reports", default="reports/quant/latest")
    return root


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    store = MarketStore(args.db)

    if args.command == "init":
        store.initialize()
        print(f"initialized: {Path(args.db).resolve()}")
        return 0

    if args.command == "sources":
        print(
            json.dumps(
                [source.__dict__ for source in SOURCE_CATALOG],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "baostock-universe":
        try:
            snapshot = BaoStockSource().list_current()
        except (RuntimeError, ValueError) as exc:
            print(f"BaoStock 股票清单获取失败：{exc}", file=sys.stderr)
            return 2
        output = args.out or (
            f"reports/quant/snapshots/{date.today().isoformat()}-baostock-shsz-"
            f"{snapshot.snapshot_id[:12]}.json"
        )
        path = write_baostock_snapshot(snapshot, output)
        print(f"snapshot_id={snapshot.snapshot_id} listed_sh_sz={len(snapshot.instruments)} "
              f"source_rows={snapshot.source_row_count} bse_excluded=true")
        print(f"snapshot: {path.resolve()}")
        return 0

    if args.command == "baostock-pilot":
        pilot_path = Path(args.pilot_db).resolve()
        if pilot_path == Path(args.db).resolve():
            raise ValueError("pilot database must differ from the main --db path")
        try:
            snapshot = read_baostock_snapshot(args.snapshot)
            pilot_store = MarketStore(pilot_path)
            result = sync_baostock_pilot(pilot_store, snapshot, args.stock,
                                         args.start, args.end)
        except (RuntimeError, ValueError) as exc:
            print(f"BaoStock 试点同步失败：{exc}", file=sys.stderr)
            return 2
        print(f"pilot_run_id={result.run_id} snapshot_id={result.snapshot_id} "
              f"stocks={result.instrument_count} bars={result.row_count} "
              f"daily_statuses={result.status_count}")
        print(f"pilot_db: {pilot_path}")
        return 0

    if args.command == "sync":
        result = sync_database(
            store=store,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
            groups=args.group or ([] if args.stock else ["all"]),
            workers=args.workers,
            progress=lambda message: print(message, flush=True),
            stock_ids=args.stock or [],
        )
        print(
            f"sync success: run_id={result.run_id} "
            f"instruments={result.instrument_count} rows={result.row_count}"
        )
        return 0

    if args.command == "status":
        summaries = store.instrument_summary()
        if not summaries:
            print("database is empty; run `quant-lab sync`")
            return 0
        for row in summaries:
            print(
                f"{row['instrument_id']:<22} {row['name']:<12} "
                f"rows={row['rows']:<5} {row['first_date']}..{row['last_date']} "
                f"source={row['source_id']}"
            )
        return 0

    if args.command == "verify":
        report = build_verification_report(store)
        write_report(report, args.out)
        for check in report["checks"]:
            marker = "PASS" if check["passed"] else "FAIL"
            print(f"[{marker}] {check['name']}: {check['detail']}")
        for warning in report["warnings"]:
            print(f"[WARN] {warning}")
        for backtest in report["backtests"]:
            metrics = backtest["metrics"]
            print(
                f"[BACKTEST] {backtest['strategy']}: "
                f"return={metrics['total_return']:.2%} "
                f"max_drawdown={metrics['max_drawdown']:.2%} "
                f"sharpe={metrics['sharpe_rf0']:.3f}"
            )
        print(f"report: {Path(args.out).resolve()}")
        return 0 if report["passed"] else 1

    if args.command == "research":
        report = research_report(store, args.strategy, args.date, args.cost_rate)
        write_research_report(report, args.out)
        snapshot = append_strategy_snapshot(
            Path(args.out).parent / "strategy-runs", report, origin="baseline_capture",
        )
        print(f"asof={report['asof']} strategy={report['strategy_id']}")
        for signal in report["signals"]:
            print(f"{signal['instrument_id']} {signal['action']} score={signal['score']} {signal['reason']}")
        print(f"report: {Path(args.out).resolve()}")
        print(f"strategy_record_id={snapshot['record_id']} archive={snapshot['path']}")
        return 0

    if args.command == "optimize":
        names = (["sma-trend", "cross-sectional-momentum", "mean-reversion-zscore"]
                 if args.strategy == "all" else [args.strategy])
        for name in names:
            result = optimize_focus_stocks(store, name, args.date, args.cost_rate)
            saved = append_optimization_record(args.out_dir, result)
            chosen = result["candidate_grid"][result["selected_candidate_index"]]
            print(f"strategy={name} selected={result['selected_parameters']} "
                  f"train_score={chosen['train_north_star']['score']:.1f} "
                  f"historical_holdout_score={result['historical_holdout']['north_star']['score']:.1f} "
                  f"optimization_id={saved['optimization_id']}")
        return 0

    if args.command == "forecast":
        if args.provider == "timesfm-2.5" and not re.fullmatch(
            r"[0-9a-fA-F]{40}", args.model_revision or ""
        ):
            raise ValueError("timesfm-2.5 requires --model-revision with a 40-character commit SHA")
        provider = (RandomWalkProvider() if args.provider == "random-walk" else
                    Momentum20Provider() if args.provider == "momentum-20" else
                    TimesFM25Provider(model_revision=args.model_revision))
        report = evaluate_focus_forecasts(store, provider, args.date)
        data, markdown = write_forecast_report(report, args.out)
        print(f"provider={report['provider']} status={report['status']} "
              f"asof={report['data_asof']} scope={report['universe_scope']}")
        print(f"report: {markdown.resolve()}\ndata: {data.resolve()}")
        return 0 if report["status"] == "ready" else 2

    if args.command == "forecast-benchmark":
        names = args.provider or ["momentum-20"]
        if len(names) != len(set(names)):
            raise ValueError("forecast-benchmark provider names must be unique")
        if "timesfm-2.5" in names and not re.fullmatch(
            r"[0-9a-fA-F]{40}", args.timesfm_revision or ""
        ):
            raise ValueError("timesfm-2.5 requires --timesfm-revision with a 40-character commit SHA")
        if "chronos-bolt-tiny" in names and not re.fullmatch(
            r"[0-9a-fA-F]{40}", args.chronos_revision or ""
        ):
            raise ValueError("chronos-bolt-tiny requires --chronos-revision with a 40-character commit SHA")
        providers = [RandomWalkProvider()]
        for name in names:
            if name == "momentum-20":
                providers.append(Momentum20Provider())
            elif name == "timesfm-2.5":
                providers.append(TimesFM25Provider(model_revision=args.timesfm_revision))
            else:
                providers.append(ChronosBoltTinyProvider(model_revision=args.chronos_revision))
        config = EvaluationConfig(
            horizons=tuple(args.horizon or (5, 20)),
            min_context=args.min_context,
            step=args.step,
        )
        if args.dataset == "qlib":
            if args.date is not None:
                raise ValueError("qlib benchmark uses the fixed Release date; omit --date")
            output_dir = args.out_dir or "reports/quant/forecast-experiments-qlib"
            report = evaluate_qlib_cohort(
                args.qlib_root, args.qlib_manifest, args.qlib_tag,
                providers, config=config, per_stratum=args.per_stratum,
            )
            saved = append_qlib_benchmark_record(output_dir, report)
            asof = report["calendar_end"]
            stock_count = report["cohort_selection"]["selected_count"]
        else:
            output_dir = args.out_dir or "reports/quant/forecast-experiments"
            report = evaluate_focus_benchmark(store.path, providers, asof=args.date, config=config)
            saved = append_benchmark_record(output_dir, report)
            asof = report["data_asof"]
            stock_count = len(report["universe"])
        print(f"record_id={saved['record_id']} dataset={args.dataset} asof={asof} "
              f"stocks={stock_count} input_sha256={report['input_fingerprint_sha256']}")
        for result in report["providers"]:
            metrics = result["pooled"].get("20", {})
            skill = (metrics.get("mae_return_skill_vs_random_walk") if args.dataset == "focus"
                     else metrics.get("mae_return_skill_vs_random_walk_on_same_cases"))
            skill_text = f"{skill:+.2%}" if skill is not None else "n/a"
            print(f"provider={result['name']} status={result['status']} "
                  f"20d_samples={metrics.get('count', 0)} 20d_mae_skill={skill_text}")
        print(f"archive: {saved['path']}")
        return 0 if all(item["status"] == "ready" for item in report["providers"]) else 2

    if args.command == "market":
        report = build_market_pulse(store, args.date)
        markdown, data, html = write_market_pulse(report, args.out)
        print(f"broad_asof={report['asof']} industry_asof={report['industry_asof']} SW_up={report['industry_breadth']['up']}/31")
        for group in report["groups"]:
            change = f" share_delta={group['share_change_pp']:+.2f}pp" if report["activity_available"] else ""
            print(f"{group['name']} 5d={group['returns']['5d']:+.2%}{change}")
        print(f"visual: {html.resolve()}\nreport: {markdown.resolve()}\ndata: {data.resolve()}")
        return 0

    if args.command == "run":
        manifest = run_pipeline(store, args.out_dir, args.strategy, args.date,
                                args.sync, args.sync_start, args.stock_snapshot)
        print(f"[1/6] data: broad={manifest['market_broad_asof']} "
              f"industry={manifest['market_industry_asof']} stock={manifest['stock_signal_asof']}")
        print(f"[2/6] feature: {manifest['feature_version']}")
        print(f"[3/6] strategy: {manifest['strategy_id']} v{manifest['strategy_version']}")
        print("[4/6] backtest: completed; metrics saved in research.json")
        print(f"[5/6] shortlist: {manifest['candidate_count']} directions; "
              f"stock_screen={manifest['stock_screen_status']} "
              f"({manifest['stock_screen_candidate_count']} candidates)")
        print(f"[6/6] report: {manifest['artifacts']['market_visual']}")
        print(f"run_id={manifest['run_id']} manifest={Path(args.out_dir).resolve() / 'run.json'}")
        return 0

    if args.command == "decide":
        store.initialize()
        decision_id = record_decision(store, args.candidate_id, args.choice, args.note)
        print(f"decision_id={decision_id} candidate_id={args.candidate_id} choice={args.choice}")
        return 0

    if args.command == "review":
        store.initialize()
        report = review_decisions(store)
        write_report(report, args.out)
        print(f"decisions={report['decision_count']} matured_20d={report['matured_20d_count']}")
        print(report["optimization"])
        print(f"report: {Path(args.out).resolve()}")
        return 0

    if args.command == "review-cases":
        result = review_feedback_cases(
            store, args.cases_dir, args.reviews_dir, asof=args.date,
        )
        print(f"status={result['status']} market_asof={result['market_asof']} "
              f"saved={result['saved']} pending={result['pending']} "
              f"unavailable={result['unavailable']}")
        return 0 if result["status"] == "reviewed" else 2

    if args.command == "api":
        from quant_lab.api_server import serve_quant_api

        serve_quant_api(store, args.reports, host=args.host, port=args.port)
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
