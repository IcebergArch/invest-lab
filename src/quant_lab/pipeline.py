"""One-command, auditable research run. Sync is optional for offline replay."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from quant_lab.market import build_market_pulse, write_market_pulse
from quant_lab.baostock_source import read_baostock_snapshot
from quant_lab.industry_validation import validate_industry_screen, write_industry_validation
from quant_lab.flow_view import build_flow_view
from quant_lab.forecast import Momentum20Provider, RandomWalkProvider
from quant_lab.forecast_report import evaluate_focus_forecasts, write_forecast_report
from quant_lab.research import research_report, write_research_report
from quant_lab.recommendations import build_recommendations
from quant_lab.storage import MarketStore
from quant_lab.stock_screen import StockScreenConfig, build_stock_shortlist
from quant_lab.strategy_registry import append_strategy_snapshot
from quant_lab.sync import sync_database
from quant_lab.workflow import save_candidates, save_stock_candidates, screen_groups


def run_pipeline(store: MarketStore, out_dir: str | Path, strategy: str = "sma-trend",
                 asof: Optional[date] = None, refresh: bool = False,
                 sync_start: date = date(2021, 1, 1),
                 stock_snapshot_path: str | Path | None = None) -> dict[str, object]:
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    store.initialize()
    run_id = uuid4().hex
    sync_id = None
    if refresh:
        end = asof or date.today() - timedelta(days=1)
        result = sync_database(store, sync_start, end, ["all"],
                               progress=lambda message: print(f"[data] {message}", flush=True))
        sync_id = result.run_id
    market = build_market_pulse(store, asof)
    industry_validation = validate_industry_screen(store, asof)
    flow = build_flow_view(market)
    stock_ids = store.list_stock_ids()
    snapshot = read_baostock_snapshot(stock_snapshot_path) if stock_snapshot_path else None
    if snapshot:
        snapshot_ids = {item.instrument_id for item in snapshot.instruments}
        stock_ids = [item for item in stock_ids if item in snapshot_ids]
    screen_config = StockScreenConfig(
        expected_universe_count=len(snapshot.instruments) if snapshot else None
    )
    stock_screen = build_stock_shortlist(
        store, stock_ids, date.fromisoformat(market["asof"]), screen_config
    ) if stock_ids else {
        "status": "blocked_empty_universe", "reason": "数据库没有股票；本次不生成股票候选。",
        "universe_count": 0, "candidate_count": 0, "candidates": [], "forecast_probability": None,
    }
    stock_screen["universe_snapshot_id"] = snapshot.snapshot_id if snapshot else None
    stock_screen["expected_universe_count"] = (
        len(snapshot.instruments) if snapshot else stock_screen.get("expected_universe_count")
    )
    stock_screen["universe_snapshot_collected_at"] = snapshot.collected_at if snapshot else None
    stock_screen["universe_snapshot_scope"] = (
        "BaoStock 当前在市沪深 A 股，不含北交所" if snapshot else None
    )
    if snapshot:
        stock_screen.setdefault("limitations", []).append(
            "当前股票清单来自 BaoStock，主库历史日线可能来自其他供应商；尚未完成全历史跨源复权价对账。"
        )
    if snapshot and (date.today() - date.fromisoformat(snapshot.collected_at[:10])).days > 4:
        stock_screen.update(status="blocked_stale_universe_snapshot",
                            reason="当前股票清单采集时间超过4天；本次不发布股票候选。",
                            candidate_count=0, candidates=[])
    if snapshot and asof and (date.fromisoformat(snapshot.collected_at[:10]) - asof).days > 4:
        stock_screen.update(status="blocked_snapshot_after_asof",
                            reason="当前股票清单晚于历史回看日期，不能当作当时股票池；本次不发布股票候选。",
                            candidate_count=0, candidates=[])
    research = research_report(store, strategy, asof)
    forecast_baseline = evaluate_focus_forecasts(store, RandomWalkProvider(), asof)
    forecast_momentum = evaluate_focus_forecasts(store, Momentum20Provider(), asof)
    candidates = screen_groups(market, run_id, industry_validation)
    save_candidates(store, candidates)
    stock_screen["candidates"] = save_stock_candidates(store, stock_screen, run_id)
    stock_screen["candidate_count"] = len(stock_screen["candidates"])
    recommendations = build_recommendations(
        store, stock_screen, research, forecast_momentum, snapshot,
        date.fromisoformat(market["asof"]),
    )
    recommendations["run_id"] = run_id
    market_md, market_json, market_html = write_market_pulse(
        market, output / "market-pulse.md", candidates, stock_screen, industry_validation,
        research, forecast_momentum
    )
    validation_md, validation_json = write_industry_validation(
        industry_validation, output / "industry-validation.md"
    )
    flow_path = output / "flow.json"
    flow_path.write_text(json.dumps(flow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stock_screen_path = output / "stock-shortlist.json"
    stock_screen_path.write_text(json.dumps(stock_screen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    recommendations_path = output / "recommendations.json"
    recommendations_path.write_text(
        json.dumps(recommendations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    candidate_path = output / "candidates.json"
    candidate_path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    research_path = output / "research.json"
    write_research_report(research, research_path)
    forecast_baseline_json, forecast_baseline_md = write_forecast_report(
        forecast_baseline, output / "forecast-baseline.json")
    forecast_momentum_json, forecast_momentum_md = write_forecast_report(
        forecast_momentum, output / "forecast-momentum.json")
    strategy_snapshot = append_strategy_snapshot(
        output.parent / "strategy-runs", research,
        source_run_id=run_id, origin="pipeline_run",
    )
    manifest = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sync_run_id": sync_id,
        "market_broad_asof": market["asof"],
        "market_industry_asof": market["industry_asof"],
        "stock_signal_asof": research["asof"],
        "strategy_id": research["strategy_id"],
        "strategy_version": research["strategy_version"],
        "feature_version": research["feature_version"],
        "cost_rate": research["cost_rate"],
        "execution_assumption": research["execution_assumption"],
        "stock_signal_count": len(research["signals"]),
        "stock_universe_count": len(research["universe"]),
        "stock_universe_scope": "focus_stock_pilot_only",
        "focus_stock_backtest_count": len(research["universe"]),
        "candidate_count": len(candidates),
        "stock_screen_status": stock_screen["status"],
        "stock_screen_candidate_count": stock_screen["candidate_count"],
        "stock_screen_universe_snapshot_id": stock_screen["universe_snapshot_id"],
        "stock_screen_universe_scope": stock_screen["universe_snapshot_scope"],
        "recommendation_status": recommendations["status"],
        "recommendation_count": recommendations["recommendation_count"],
        "industry_validation_selected_origins": industry_validation["selected_origin_count"],
        "forecast_baseline_status": forecast_baseline["status"],
        "forecast_momentum_status": forecast_momentum["status"],
        "backtest_metrics": research["backtest"]["metrics"],
        "strategy_snapshot_record_id": strategy_snapshot["record_id"],
        "artifacts": {
            "market_visual": str(market_html.resolve()),
            "market_markdown": str(market_md.resolve()),
            "market_data": str(market_json.resolve()),
            "flow_data": str(flow_path.resolve()),
            "research_data": str(research_path.resolve()),
            "strategy_snapshot": strategy_snapshot["path"],
            "backtest_markdown": str((output / "backtest.md").resolve()),
            "industry_validation_markdown": str(validation_md.resolve()),
            "industry_validation_data": str(validation_json.resolve()),
            "forecast_baseline_data": str(forecast_baseline_json.resolve()),
            "forecast_baseline_markdown": str(forecast_baseline_md.resolve()),
            "forecast_momentum_data": str(forecast_momentum_json.resolve()),
            "forecast_momentum_markdown": str(forecast_momentum_md.resolve()),
            "candidates": str(candidate_path.resolve()),
            "stock_shortlist": str(stock_screen_path.resolve()),
            "recommendations": str(recommendations_path.resolve()),
            "stock_universe_snapshot": (str(Path(stock_snapshot_path).resolve())
                                        if stock_snapshot_path else None),
        },
    }
    (output / "run.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
    return manifest
