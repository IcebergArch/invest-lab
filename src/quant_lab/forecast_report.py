"""Run the same rolling forecast evaluation on the focus-stock pilot panel."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Optional

from quant_lab.canonical_data import MarketStoreAdapter, build_close_panel
from quant_lab.forecast import EvaluationConfig, ForecastProvider, evaluate_forecasts
from quant_lab.storage import MarketStore


def evaluate_focus_forecasts(store: MarketStore, provider: ForecastProvider,
                             asof: Optional[date] = None,
                             config: EvaluationConfig = EvaluationConfig()) -> dict[str, object]:
    ids = store.list_instrument_ids("focus_stock")
    if not ids:
        raise ValueError("forecast evaluation needs focus-stock daily data")
    bars = MarketStoreAdapter(store.path).iter_bars(instrument_ids=ids, end=asof)
    canonical = build_close_panel(
        (bar for bar in bars if bar.price_basis == "forward_adjusted"),
        expected_price_basis="forward_adjusted",
    )
    if set(canonical.closes) != set(ids):
        raise ValueError("forecast evaluation needs same-basis closes for every focus stock")
    dates, panel = canonical.dates, canonical.closes
    items = []
    for instrument_id in ids:
        result = evaluate_forecasts(dates, panel[instrument_id], provider,
                                    instrument_id, asof=asof, config=config)
        items.append(result)
        if result["status"] not in ("ready", "insufficient_history"):
            break
    unavailable = next((item for item in items if item["status"] not in
                        ("ready", "insufficient_history")), None)
    pooled = {}
    for horizon in config.horizons:
        records = [record for item in items for record in item["records"]
                   if record["horizon_sessions"] == horizon]
        if not records:
            pooled[str(horizon)] = {"status": "insufficient_history", "count": 0}
            continue
        error = mean(record["absolute_return_error"] for record in records)
        naive = mean(record["random_walk_absolute_return_error"] for record in records)
        directed = [record["direction_correct"] for record in records
                    if record["direction_correct"] is not None]
        pooled[str(horizon)] = {
            "status": "ready", "count": len(records),
            "mae_return": error,
            "random_walk_mae_return": naive,
            "mae_return_skill_vs_random_walk": 1 - error / naive if naive else None,
            "directional_hit_rate": sum(directed) / len(directed) if directed else None,
            "directional_coverage": len(directed) / len(records),
        }
    return {
        "status": unavailable["status"] if unavailable else
                  "ready" if any(item["status"] == "ready" for item in items)
                  else "insufficient_history",
        "reason": unavailable.get("reason") if unavailable else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_asof": dates[-1].isoformat() if dates else None,
        "data_start": dates[0].isoformat() if dates else None,
        "common_sessions": len(dates),
        "price_basis": canonical.price_basis,
        "input_fingerprint_sha256": canonical.input_fingerprint_sha256,
        "point_in_time_eligible": False,
        "universe_scope": "仅现有三只重点股，不能验证全市场选股能力",
        "provider": provider.name,
        "model_id": provider.model_id,
        "model_revision": provider.model_revision,
        "configuration": {"horizons": list(config.horizons), "min_context": config.min_context,
                          "step": config.step},
        "pooled": pooled,
        "per_stock": items,
        "limitations": [
            "每个起点仅使用当时及以前的已存收盘价；历史价格可能被复权修订，并非不可变时点快照。",
            "预测误差不等于扣成本交易收益；模型输出目前不参与股票筛选或下单。",
            "三只事后选择的股票不是市场代表样本；上涨概率与可交易收益尚未校准。",
        ],
    }


def write_forecast_report(report: dict[str, object], output: str | Path) -> tuple[Path, Path]:
    data = Path(output)
    data.parent.mkdir(parents=True, exist_ok=True)
    markdown = data.with_suffix(".md")
    data.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 预测模型滚动检验", "",
             f"模型：{report['provider']}（{report['model_id']}）；权重版本：{report['model_revision'] or '未固定/不适用'}。",
             f"数据：{report['data_start']} 至 {report['data_asof']}，{report['common_sessions']} 个共同交易日；"
             f"{report['universe_scope']}。", ""]
    if report["status"] != "ready":
        lines += [f"状态：{report['status']}。{report['reason'] or '没有足够历史数据。'}", ""]
    else:
        lines += ["| 预测窗口 | 观察数 | 平均收益预测误差 | 相对最后收盘价基线 | 方向命中率 |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for horizon, values in report["pooled"].items():
            if values["status"] != "ready":
                lines.append(f"| {horizon} 个交易日 | 0 | — | — | — |")
                continue
            skill = values["mae_return_skill_vs_random_walk"]
            hit = values["directional_hit_rate"]
            skill_text = f"{skill:+.2%}" if skill is not None else "—"
            hit_text = f"{hit:.1%}" if hit is not None else "无方向预测"
            lines.append(f"| {horizon} 个交易日 | {values['count']} | "
                         f"{values['mae_return']:.2%} | {skill_text} | {hit_text} |")
        lines += ["", "正的误差改善表示比最后收盘价基线的绝对收益误差更小；这不证明交易收益。", ""]
    lines += ["## 限制", "", *(f"- {item}" for item in report["limitations"]), ""]
    markdown.write_text("\n".join(lines), encoding="utf-8")
    return data, markdown
