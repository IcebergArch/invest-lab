"""Conservative, read-only stock card from an immutable published Qlib release.

Qlib closes are adjusted.  A caller may expose an original-price comparison
only after separately auditing the release's close/factor relationship.  The
amount unit has its own audit gate and an explicit CNY multiplier.  Neither
gate is inferred from a successful file integrity check.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

from quant_lab.qlib_archive import QlibArchiveError
from quant_lab.qlib_local import read_stock
from quant_lab.stock_analysis import _metrics, _parse_cost, _risk, _trend
from quant_lab.stock_chart import build_stock_chart


ANALYSIS_CALENDAR_LIMIT = 1600


def _usable_close(row: dict[str, Any]) -> bool:
    value = row.get("close")
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _positive_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _base_report(query: str, cost: float | None) -> dict[str, Any]:
    return {
        "status": "unknown_stock", "reason": "这只股票不在已发布的 Qlib 归档中。",
        "query": query, "instrument_id": None, "name": None, "asof": None,
        "latest_close": None, "adjusted_close": None, "cost_price": cost,
        "cost_return": None, "summary": "暂时无法生成分析报告。",
        "trend": None, "risk": None, "forecast": None, "backtest": None,
        "sources": [], "limitations": [],
    }


def analyze_qlib_stock(
    root: str | Path,
    manifest_path: str | Path,
    expected_tag: str,
    symbol: str,
    cost_price: float | str | None = None,
    *,
    asof: date | None = None,
    factor_verified: bool = False,
    amount_verified: bool = False,
    amount_multiplier_cny: float | None = None,
) -> dict[str, Any]:
    """Analyze only a bounded recent slice; never modify or promote Qlib data.

    ``factor_verified`` means an independent, release-specific cross-source
    audit has established that original CNY close equals adjusted close/factor.
    ``amount_verified`` additionally requires that same factor audit and an
    independently audited multiplier from Qlib amount units to CNY.  A missing
    or bad latest factor still blocks original-price comparison.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("请输入股票代码")
    if asof is not None and (not isinstance(asof, date) or isinstance(asof, datetime)):
        raise TypeError("asof must be a date")
    if type(factor_verified) is not bool or type(amount_verified) is not bool:
        raise TypeError("verification flags must be bool")
    if amount_verified and not factor_verified:
        raise ValueError("成交额换算须先完成复权因子核验")
    if amount_verified and not _positive_number(amount_multiplier_cny):
        raise ValueError("已核验成交额必须提供正数人民币换算系数")
    if not amount_verified and amount_multiplier_cny is not None:
        raise ValueError("未核验成交额不得提供人民币换算系数")
    cost = _parse_cost(cost_price)
    query = symbol.strip()
    report = _base_report(query, cost)
    try:
        extracted = read_stock(
            root, manifest_path, expected_tag, query,
            end=asof.isoformat() if asof else None,
            fields=("close", "factor", "amount"), limit=ANALYSIS_CALENDAR_LIMIT,
            include_close_coverage=True,
        )
    except QlibArchiveError as exc:
        message = str(exc)
        if message.startswith("stock absent from all.txt:"):
            return report
        if message.startswith("six-digit code is ambiguous:"):
            report.update(status="ambiguous_stock", reason="六位代码对应多个交易所，请输入带交易所的完整代码。")
            return report
        raise

    stock = extracted["stock"]
    rows = extracted["rows"]
    release = extracted["release"]
    coverage = extracted["close_coverage"]
    report["instrument_id"] = stock["instrument_id"]
    source = {
        "latest_source_id": extracted["source_id"],
        "latest_run_id": None,
        "release_tag": release["tag"],
        "release_target_trade_date": release["target_trade_date"],
        "manifest_sha256": release["manifest_sha256"],
        "archive_sha256": release["archive_sha256"],
        "investment_data_commit": release["investment_data_commit"],
        "qlib_commit": release["qlib_commit"],
        "dolt_commit": release["dolt_commit"],
        "adjustment": extracted["adjustment"],
        "first_trade_date_in_window": rows[0]["date"] if rows else None,
        "last_trade_date": None,
        "bar_count": coverage["valid_close_count"] if coverage else None,
        "first_valid_close_date": coverage["first_valid"] if coverage else None,
        "last_valid_close_date": coverage["last_valid"] if coverage else None,
        "full_history_missing_close_count": coverage["missing_or_invalid_close_count"] if coverage else None,
        "analyzed_contiguous_close_rows": 0,
        "read_window_calendar_rows": len(rows),
        "read_window_missing_or_invalid_close_rows": None,
        "calendar_rows_in_range": extracted["calendar_rows_in_range"],
        "truncated_to_latest": extracted["truncated_to_latest"],
        "instrument_intervals": stock["intervals"],
        "factor_verified": factor_verified,
        "amount_verified": amount_verified,
        "amount_multiplier_cny": amount_multiplier_cny if amount_verified else None,
    }
    report["sources"] = [source]
    valid_positions = [index for index, row in enumerate(rows) if _usable_close(row)]
    if not valid_positions:
        source["read_window_missing_or_invalid_close_rows"] = len(rows)
        report.update(
            status="no_bars",
            reason=f"所读归档窗口内没有可用的复权收盘价；该窗口最多包含最近 {ANALYSIS_CALENDAR_LIMIT} 个交易日历行。",
            summary="暂时无法生成价格走势报告。",
        )
        return report

    latest_index = valid_positions[-1]
    latest = rows[latest_index]
    # A gap invalidates fixed 5/20/60-session comparisons across it.  Start at
    # the most recent valid close and stop at the first missing/invalid close.
    contiguous: list[dict[str, Any]] = []
    for row in reversed(rows[:latest_index + 1]):
        if not _usable_close(row):
            break
        raw_amount = row.get("amount")
        converted_amount = (
            float(raw_amount) * float(amount_multiplier_cny)
            if amount_verified and isinstance(raw_amount, (int, float))
            and not isinstance(raw_amount, bool) and math.isfinite(raw_amount)
            and raw_amount >= 0 else None
        )
        if converted_amount is not None and not math.isfinite(converted_amount):
            converted_amount = None
        contiguous.append({
            "date": row["date"], "close": float(row["close"]), "amount": converted_amount,
            "amount_unit": "CNY" if converted_amount is not None else "unverified",
        })
    contiguous.reverse()

    metrics = _metrics(contiguous)
    trend = _trend(metrics)
    risk = _risk(metrics, None)
    if not amount_verified:
        risk["flags"] = [flag for flag in risk["flags"]
                         if flag != "近 20 日人民币成交额不完整，无法可靠评估流动性。"]
    trailing_missing = len(rows) - latest_index - 1
    if trailing_missing:
        risk["flags"].append(f"归档证券区间末尾有 {trailing_missing} 个日历行没有可用收盘价。")
    interval_ended_before_release = stock["intervals"][-1]["end"] < release["target_trade_date"]
    if interval_ended_before_release:
        risk["flags"].append("此证券在归档中的记录区间早于 Release 截止日结束；不能当作当前行情。")
    if not factor_verified:
        risk["flags"].append("该股原价换算尚未通过独立核验，不能给出原价和成本价差异。")
    if not amount_verified:
        risk["flags"].append("该股成交额口径尚未通过核验，不计算人民币流动性指标。")
    risk["summary"] = " ".join(risk["flags"]) if risk["flags"] else risk["summary"]

    adjusted_close = float(latest["close"])
    factor = latest.get("factor")
    original_close = adjusted_close / float(factor) if factor_verified and _positive_number(factor) else None
    if original_close is not None and not math.isfinite(original_close):
        original_close = None
    cost_return = original_close / cost - 1 if cost is not None and original_close is not None else None
    if factor_verified and original_close is None:
        risk["flags"].append("最新记录缺少有效复权因子，原价和成本价差异仍不可用。")
        risk["summary"] = " ".join(risk["flags"])

    source.update(
        last_trade_date=latest["date"],
        analyzed_contiguous_close_rows=len(contiguous),
        read_window_missing_or_invalid_close_rows=len(rows) - len(valid_positions),
    )
    limitations = [
        "走势与价格风险只使用最新连续有效的 Qlib 复权收盘价；它不是实时行情或原始成交价。",
        f"全史只统计有效收盘数；走势计算仅用最近最多 {ANALYSIS_CALENDAR_LIMIT} 个日历行，缺值会截断连续样本，较早历史质量未在此报告中核验。",
        "该 Release 是事后发布的数据，并非历史时点可得的股票池；本报告不提供荐股、预测或策略回测。",
        "Qlib 归档与主行情库保持隔离，不用于现有选股和回测。",
    ]
    if not factor_verified:
        limitations.append("该股原价换算未通过报告核验；复权收盘价不可与输入成本价比较。")
    elif cost is not None:
        limitations.append("成本价差异只比较换算后的最新收盘价；未核对买入日期、分红送转、持仓数量和交易费用，不是账户盈亏。")
    if not amount_verified:
        limitations.append("该股成交额口径未通过报告核验，不展示人民币成交额。")

    status = "ready" if (
        original_close is not None and amount_verified
        and metrics["return_60d"] is not None
        and metrics["avg_amount_20d_cny"] is not None
        and trailing_missing == 0
        and not interval_ended_before_release
    ) else "partial_data"
    summary = f"{stock['instrument_id']} 截至 {latest['date']} 的 Qlib 复权收盘价为 {adjusted_close:.4f}；{trend['label']}。"
    if original_close is not None:
        summary += f"经核验因子换算的原价约 {original_close:.2f} 元。"
    if cost_return is not None:
        summary += f"相对输入成本价的价格差异 {cost_return:+.1%}，不是账户盈亏。"
    if not factor_verified and cost is not None:
        summary += "成本价已记录，但复权价不能直接用于成本比较。"
    report.update(
        status=status,
        reason=("此报告仅有已核验的部分字段；查看报告边界。" if status == "partial_data" else None),
        asof=latest["date"], latest_close=original_close,
        adjusted_close=adjusted_close, cost_return=cost_return,
        summary=summary, trend=trend, risk=risk,
        chart=build_stock_chart(
            [date.fromisoformat(item["date"]) for item in contiguous],
            [item["close"] for item in contiguous],
            instrument_id=stock["instrument_id"], price_basis="qlib_adjusted",
            include_strategy_signals=False, include_research_forecast=False,
        ),
        sources=[source], limitations=limitations,
    )
    return report
