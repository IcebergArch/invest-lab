"""Historical label check for the report's simple industry-direction screen.

This checks whether a past relative-strength observation was followed by a
positive index move.  SW indices and reader groups are not tradable portfolios.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Optional

from quant_lab.market import GROUPS
from quant_lab.storage import MarketStore


GROUP_NAMES = tuple(name for name in GROUPS if name != "其他（综合）")
HORIZON = 5
WARMUP = 20


def _group_return(closes: dict[str, dict[str, float]], name: str,
                  start_day: str, end_day: str) -> float:
    return mean(closes[code][end_day] / closes[code][start_day] - 1
                for code in GROUPS[name])


def validate_industry_screen(store: MarketStore, asof: Optional[date] = None) -> dict[str, object]:
    """Replay nonoverlapping 5-session checks using only past data at origin.

    Future labels are read solely for evaluation, never for choosing groups.
    The mapping is today's fixed reader taxonomy and source revisions are not
    point-in-time archived, so this is a pilot association check.
    """
    codes = [code for name in GROUP_NAMES for code in GROUPS[name]]
    ids = [f"industry:{code}.SW" for code in codes]
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT instrument_id,trade_date,close,source_id,run_id,payload_hash "
            "FROM daily_bars WHERE instrument_id IN (" + ",".join("?" for _ in ids) + ") "
            + ("AND trade_date<=? " if asof else "") + "ORDER BY trade_date,instrument_id",
            [*ids, asof.isoformat()] if asof else ids,
        ).fetchall()
    closes: dict[str, dict[str, float]] = {code: {} for code in codes}
    lineage: dict[str, dict[str, object]] = {}
    for row in rows:
        code = row["instrument_id"].split(":", 1)[1].split(".", 1)[0]
        closes[code][row["trade_date"]] = float(row["close"])
        lineage[code] = {key: row[key] for key in ("source_id", "run_id", "payload_hash")}
    if any(not days for days in closes.values()):
        raise ValueError("industry validation needs all 30 included SW series")
    common_dates = sorted(set.intersection(*(set(days) for days in closes.values())))
    if len(common_dates) < WARMUP + HORIZON + 1:
        raise ValueError("industry validation needs at least 26 common sessions")

    points = []
    no_selection_count = 0
    # Moving origins by the holding horizon prevents overlapping future labels.
    for index in range(WARMUP, len(common_dates) - HORIZON, HORIZON):
        origin, previous, target = (common_dates[index], common_dates[index - HORIZON],
                                    common_dates[index + HORIZON])
        trailing = {name: _group_return(closes, name, previous, origin) for name in GROUP_NAMES}
        selected = sorted((name for name in GROUP_NAMES if trailing[name] > 0),
                          key=lambda name: (-trailing[name], name))[:3]
        if not selected:
            no_selection_count += 1
            continue
        future = {name: _group_return(closes, name, origin, target) for name in GROUP_NAMES}
        selected_return = mean(future[name] for name in selected)
        baseline_return = mean(future.values())
        points.append({
            "origin": origin,
            "target": target,
            "selected_groups": selected,
            "trailing_5d": {name: trailing[name] for name in selected},
            "future_5d": {name: future[name] for name in selected},
            "selected_mean_future_5d": selected_return,
            "five_group_equal_weight_future_5d": baseline_return,
            "excess_vs_equal_weight": selected_return - baseline_return,
        })
    outcomes = [point["selected_mean_future_5d"] for point in points]
    excesses = [point["excess_vs_equal_weight"] for point in points]
    result = {
        "validation_version": "sw-reader-screen-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": common_dates[WARMUP],
        "last_origin": points[-1]["origin"] if points else None,
        "last_target": points[-1]["target"] if points else None,
        "industry_data_asof": common_dates[-1],
        "common_sessions": len(common_dates),
        "fixed_group_names": list(GROUP_NAMES),
        "horizon_sessions": HORIZON,
        "origin_stride_sessions": HORIZON,
        "selection_rule": "origin 当日收盘前 5 日涨幅大于 0 的五大类，按过去涨幅取前 3；仅使用起点及以前价格",
        "comparison": "同一批起点、同一未来五个交易日，五大类指数等权平均",
        "origin_count": len(points) + no_selection_count,
        "selected_origin_count": len(points),
        "no_selection_count": no_selection_count,
        "positive_future_rate": sum(value > 0 for value in outcomes) / len(outcomes) if outcomes else None,
        "mean_future_5d": mean(outcomes) if outcomes else None,
        "mean_excess_vs_equal_weight": mean(excesses) if excesses else None,
        "outperform_equal_weight_rate": sum(value > 0 for value in excesses) / len(excesses) if excesses else None,
        "worst_future_5d": min(outcomes) if outcomes else None,
        "latest_source_lineage": lineage,
        "points": points,
        "limitations": [
            "申万行业指数及自定义大类不可直接交易；这里是后续走势检验，不是可实现的投资收益。",
            "采用当前固定行业映射，缺历史成分与当时公布时间；历史分类/行情修订可能造成时点偏差。",
            "没有模拟 10:30 成交、涨跌停、冲击成本或交易费用，不可用来决定自动交易。",
            "每五个交易日取一次非重叠起点，仍可能受同一市场环境与样本期间影响。",
        ],
    }
    return result


def write_industry_validation(report: dict[str, object], output: str | Path) -> tuple[Path, Path]:
    markdown = Path(output)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    data_path = markdown.with_suffix(".json")
    data_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    def percent(value: object, signed: bool = False) -> str:
        if value is None:
            return "—"
        return f"{value:+.2%}" if signed else f"{value:.2%}"
    def percentage_points(value: object) -> str:
        return "—" if value is None else f"{value*100:+.2f} 个百分点"
    lines = [
        "# 五条行业主线历史检验", "",
        f"数据至 {report['industry_data_asof']}；非重叠起点 {report['origin_count']} 次，其中有候选 {report['selected_origin_count']} 次。", "",
        "| 指标 | 结果 |", "| --- | ---: |",
        f"| 随后 5 日平均表现为正的比例 | {percent(report['positive_future_rate'])} |",
        f"| 随后 5 日指数平均涨跌 | {percent(report['mean_future_5d'])} |",
        f"| 相对五条主线等权基线的平均差 | "
        f"{percentage_points(report['mean_excess_vs_equal_weight'])} |",
        f"| 跑赢等权基线的比例 | {percent(report['outperform_equal_weight_rate'])} |",
        f"| 单次最差随后 5 日表现 | {percent(report['worst_future_5d'], signed=True)} |", "",
        "筛选只看起点之前五日；结果看随后五个交易日。指标是申万指数走势，不是可交易收益。", "",
        "## 限制", "", *(f"- {item}" for item in report["limitations"]), "",
    ]
    markdown.write_text("\n".join(lines), encoding="utf-8")
    return markdown, data_path
