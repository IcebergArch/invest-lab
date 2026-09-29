"""One-minute reading of the market pulse, with explicit evidence boundaries.

This module only rearranges already verified market-pulse observations.  SW
industry amounts measure turnover, not a directional investor flow.  An
exchange-published financing-balance change would describe *financed buying*,
not all investors' net purchases.  The current report has neither series.

The Shenzhen exchange's 2024 disclosure change also means that the published
post-close *northbound* summary is aggregate turnover, not a public daily
buy-minus-sell series: https://www.szse.cn/aboutus/trends/news/t20240412_606836.html
The SW published page labels its industry amount as traded amount:
https://www.swsresearch.com/institute_sw/allIndex/releasedIndex
"""

from __future__ import annotations

from datetime import date
from typing import Mapping


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("market report is missing an ISO as-of date")
    return date.fromisoformat(value)


def _change_word(value: float) -> str:
    return "上涨" if value > 0 else "下跌" if value < 0 else "持平"


def _source_trace(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source_ids": sorted({str(row["source_id"]) for row in rows if row.get("source_id")}),
        "run_ids": sorted({str(row["run_id"]) for row in rows if row.get("run_id")}),
    }


def build_flow_view(market_report: Mapping[str, object]) -> dict[str, object]:
    """Build a short, dated market reading from ``build_market_pulse`` output.

    ``focus_points`` describe the largest *changes in share of traded amount*
    among the five reader-facing groups.  They never rank net buying.  The
    result is deliberately read-only and contains no trade instruction.
    """
    broad_date = _day(market_report["asof"])
    industry_date = _day(market_report["industry_asof"])
    broad = list(market_report["broad"])
    groups = [row for row in market_report["groups"] if row["name"] != "其他（综合）"]
    industries = list(market_report["industries"])
    if not broad or not groups:
        raise ValueError("market report needs broad indices and industry groups")

    falling = sum(float(row["returns"]["1d"]) < 0 for row in broad)
    rising = sum(float(row["returns"]["1d"]) > 0 for row in broad)
    industry_breadth = market_report["industry_breadth"]
    industry_down = int(industry_breadth["down"])
    industry_total = int(industry_breadth["total"])
    activity_available = bool(market_report.get("activity_available")) and all(
        isinstance(row.get("share_change_pp"), (int, float)) for row in groups
    )

    if falling == len(broad):
        market_word = "全部下跌"
    elif rising == len(broad):
        market_word = "全部上涨"
    else:
        market_word = f"{falling} 个下跌、{rising} 个上涨"
    broad_line = f"{broad_date.isoformat()}：观察的 {len(broad)} 个大盘指数{market_word}。"

    focus: list[dict[str, object]] = []
    if activity_available:
        ranked = sorted(groups, key=lambda row: abs(float(row["share_change_pp"])), reverse=True)
        for row in ranked[:3]:
            delta = float(row["share_change_pp"])
            movement = float(row["returns"]["1d"])
            attention_word = "增加" if delta > 0 else "减少" if delta < 0 else "持平"
            price_word = _change_word(movement)
            focus.append({
                "group": str(row["name"]),
                "asof": industry_date.isoformat(),
                "attention": attention_word,
                "share_change_pp": delta,
                "group_return_1d": movement,
                "group_return_word": price_word,
                "line": (f"{row['name']}：成交占比较此前 20 个共同交易日均值"
                         f"{attention_word} {abs(delta):.2f} 个百分点；"
                         f"组内行业指数当天平均{price_word} {abs(movement):.2%}。"),
            })

    if activity_available:
        leaders = sorted(
            (row for row in groups if float(row["share_change_pp"]) > 0),
            key=lambda row: float(row["share_change_pp"]), reverse=True,
        )[:2]
        if leaders:
            labels = "、".join(str(row["name"]) for row in leaders)
            leader_down = sum(float(row["returns"]["1d"]) < 0 for row in leaders)
            if leader_down == len(leaders):
                price_note = "，这些方向当天也下跌"
            elif leader_down:
                price_note = f"，其中 {leader_down} 类当天也下跌"
            else:
                price_note = ""
            activity_line = (
                f"{industry_date.isoformat()}：{industry_total} 个申万一级行业指数中"
                f" {industry_down} 个下跌；{labels}的成交占比上升{price_note}。"
            )
        else:
            activity_line = (
                f"{industry_date.isoformat()}：{industry_total} 个申万一级行业指数中"
                f" {industry_down} 个下跌；没有成交占比上升的主要方向。"
            )
    else:
        activity_line = (
            f"{industry_date.isoformat()}：{industry_total} 个申万一级行业指数中"
            f" {industry_down} 个下跌；成交额数据不足，暂不判断交易关注度。"
        )

    if broad_date == industry_date:
        date_line = "大盘和行业日期一致。"
        date_status = "aligned"
    elif industry_date < broad_date:
        date_line = (f"行业只更新到 {industry_date.isoformat()}，早于大盘的"
                     f" {broad_date.isoformat()}；不能用行业变化解释较新一天。")
        date_status = "industry_behind"
    else:
        date_line = (f"大盘只更新到 {broad_date.isoformat()}，早于行业的"
                     f" {industry_date.isoformat()}；不能用大盘变化解释较新一天。")
        date_status = "broad_behind"

    # There is no directional-investor-flow table or source in this input.  A
    # rising traded-amount share is therefore never promoted to "net inflow".
    net_flow_line = "谁在净买入：当前报告没有可核验的数据，暂不判断。"
    return {
        "version": "flow-view-v1",
        "broad_asof": broad_date.isoformat(),
        "industry_asof": industry_date.isoformat(),
        "date_status": date_status,
        "date_line": date_line,
        "reader_lines": [broad_line, activity_line, f"{date_line} {net_flow_line}"],
        "focus_points": focus,
        "activity_available": activity_available,
        "net_flow_available": False,
        "net_flow_line": net_flow_line,
        "metric_definition": (
            "成交占比＝行业组申万指数成交额之和 / 31 个申万一级行业指数成交额之和；"
            "变化＝当日占比－此前 20 个共同交易日平均占比。"
        ),
        "source_trace": {
            "broad": _source_trace(broad),
            "industry": _source_trace(industries),
            "industry_published_field": "成交额（仅衡量成交，不标识买卖方向）",
            "industry_published_url": "https://www.swsresearch.com/institute_sw/allIndex/releasedIndex",
            "northbound_disclosure_url": "https://www.szse.cn/aboutus/trends/news/t20240412_606836.html",
        },
    }
