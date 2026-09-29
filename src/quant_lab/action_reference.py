"""Deterministic, dated operating reference for a single-stock research card.

This is a small risk rule, not a return forecast or an order generator.  It
keeps a historical signal separate from the price and trading-status checks
needed before a human acts.  In particular, an adjusted Qlib close cannot be
used as an original-price or cost basis without release-specific verification.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping


RULE_VERSION = "single-stock-action-reference-v1"
_SHANGHAI = timezone(timedelta(hours=8))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _choice(stance: str, label: str, reason: str) -> dict[str, str]:
    return {"stance": stance, "label": label, "reason": reason}


def _base(report: Mapping[str, Any], source: Mapping[str, Any], asof: date | None) -> dict[str, Any]:
    qlib = source.get("latest_source_id") == "investment_data_qlib_release"
    cost = _number(report.get("cost_price"))
    if cost is None:
        cost_context = None
    elif qlib and source.get("factor_verified") is not True:
        cost_context = "输入成本价已记录；Qlib 复权价尚未通过原价换算核验，不能计算持仓盈亏或止损价。"
    elif source.get("adjustment") == "qfq":
        cost_context = "成本价只作价格参照；主库前复权日线未核对买入日期、公司行动及费用，不是账户盈亏。"
    else:
        cost_context = "成本价只作价格参照；未核对持仓数量、买入日期和费用，不是账户盈亏。"
    return {
        "rule_version": RULE_VERSION,
        "status": "insufficient_evidence",
        "asof": asof.isoformat() if asof else None,
        "buy": _choice("unavailable", "暂不能判断买入", "缺少可核验的当前价格或完整风险数据。"),
        "sell": _choice("unavailable", "暂不能判断卖出", "缺少可核验的当前价格或完整风险数据。"),
        "reasons": [],
        "risks": [],
        "cost_context": cost_context,
        "next_check": "先核对最新交易日价格、交易状态和持仓风险预算。",
        "input_provenance": {
            "instrument_id": report.get("instrument_id"),
            "report_status": report.get("status"),
            "source_id": source.get("latest_source_id"),
            "source_kind": "qlib_archive" if qlib else "main_store",
            "source_run_id": source.get("latest_run_id"),
            "source_payload_hash": source.get("latest_payload_hash"),
            "release_tag": source.get("release_tag") if qlib else None,
            "manifest_sha256": source.get("manifest_sha256") if qlib else None,
            "archive_sha256": source.get("archive_sha256") if qlib else None,
            "market_reference_date": source.get("market_reference_date") if not qlib else None,
            "bar_count": source.get("bar_count"),
            "factor_verified": source.get("factor_verified") is True if qlib else None,
            "amount_verified": source.get("amount_verified") is True if qlib else None,
            "price_basis": "verified_original_price" if qlib and source.get("factor_verified") is True
                           else "unverified_adjusted_price" if qlib else source.get("adjustment"),
        },
        "signals": [],
    }


def build_action_reference(report: Mapping[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """Return separate buy/sell stances from one dated report, failing closed.

    ``today`` is the China-local calendar date; injection makes historical
    reproducibility possible.  All stances are conditional research cues.
    They never imply a live quote, an expected return, or an executable order.
    """
    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")
    if today is None:
        today = datetime.now(_SHANGHAI).date()
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date")
    source_list = report.get("sources")
    source = source_list[0] if isinstance(source_list, list) and source_list and isinstance(source_list[0], Mapping) else {}
    asof = _day(report.get("asof"))
    result = _base(report, source, asof)
    reasons: list[str] = result["reasons"]
    risks: list[str] = result["risks"]
    qlib = result["input_provenance"]["source_kind"] == "qlib_archive"

    if report.get("status") not in ("ready", "partial_data") or asof is None or not source:
        risks.append("报告未形成可核验的单股日线结论。")
        return result
    if asof > today:
        risks.append("报告日期晚于当前中国日历日期，时间口径异常。")
        return result
    if qlib:
        release_end = _day(source.get("release_target_trade_date"))
        if release_end is None:
            risks.append("Qlib Release 截止交易日缺失，不能确认当前数据覆盖。")
            return result
        if asof < release_end:
            result["buy"] = _choice("avoid", "回避新增买入", "该股归档价格早于 Release 截止日，不能确认当前仍可交易。")
            risks.append("归档证券记录已结束；先核对是否仍在交易。")
            return result
        if asof > release_end:
            risks.append("归档报告日期晚于 Release 截止交易日，时间口径异常。")
            return result
        if source.get("factor_verified") is not True or _number(report.get("latest_close")) is None:
            result["buy"] = _choice("wait", "暂缓买入", "Qlib 原价换算未核验；复权价不能直接当作现价。")
            risks.append("Qlib 原价换算未核验，不能给出基于现价或成本的买卖判断。")
            return result
        if source.get("amount_verified") is not True:
            result["buy"] = _choice("wait", "暂缓买入", "人民币成交额口径未核验，流动性无法判断。")
            risks.append("Qlib 成交额单位未核验。")
            return result
    elif _number(report.get("latest_close")) is None:
        risks.append("主库缺少有效的最新收盘价。")
        return result

    missing = source.get("missing_recent_market_sessions")
    if not qlib and (type(missing) is not int or missing != 0):
        result["buy"] = _choice("wait", "暂缓买入", "主库股票日线未与市场交易日历对齐。")
        risks.append("最近市场交易日覆盖未得到核实。")
        return result

    risk = report.get("risk")
    trend = report.get("trend")
    metrics = risk.get("metrics") if isinstance(risk, Mapping) else None
    if not isinstance(metrics, Mapping) or not isinstance(trend, Mapping):
        risks.append("缺少完整的趋势或风险指标。")
        return result
    r20 = _number(metrics.get("return_20d"))
    r60 = _number(metrics.get("return_60d"))
    vol = _number(metrics.get("annualized_volatility_20d"))
    drawdown = _number(metrics.get("max_drawdown_60d"))
    amount = _number(metrics.get("avg_amount_20d_cny"))
    if (r20 is None or r60 is None or vol is None or drawdown is None or amount is None
            or r20 <= -1 or r60 <= -1 or vol < 0 or not 0 <= drawdown <= 1 or amount < 0):
        result["buy"] = _choice("wait", "暂缓买入", "近 60 日价格或近 20 日波动、人民币成交额不足以支持规则判断。")
        risks.append("风险或流动性指标不完整。")
        return result

    reasons.extend((
        f"近 20 日价格变化 {r20:+.1%}，近 60 日 {r60:+.1%}。",
        f"近 20 日年化波动 {vol:.1%}，近 60 日最大回撤 {drawdown:.1%}。",
        f"近 20 日平均成交额 {amount / 100_000_000:.2f} 亿元。",
    ))
    result["signals"] = [
        {"key": "return_20d", "value": r20, "asof": asof.isoformat()},
        {"key": "return_60d", "value": r60, "asof": asof.isoformat()},
        {"key": "annualized_volatility_20d", "value": vol, "asof": asof.isoformat()},
        {"key": "max_drawdown_60d", "value": drawdown, "asof": asof.isoformat()},
        {"key": "avg_amount_20d_cny", "value": amount, "asof": asof.isoformat()},
    ]
    risks.append("价格趋势仅是已发生的变化；本规则阈值未独立样本外验证，不预测收益。")
    if report.get("forecast") is None or (isinstance(report.get("forecast"), Mapping)
                                           and report["forecast"].get("status") in ("baseline_only", "insufficient_history")):
        risks.append("尚无经独立验证的个股收益预测。")

    severe = sum((r20 <= -0.15, r60 <= -0.20, drawdown >= 0.25, vol >= 0.60))
    weakening = (r20 <= -0.08 and r60 < 0) or (drawdown >= 0.15 and r20 < 0)
    if severe >= 2:
        result["sell"] = _choice("review_exit", "核价后评估退出", "多项历史下行或高波动条件同时触发；先核对最新价格与交易状态。")
    elif severe or weakening:
        result["sell"] = _choice("consider_reduce", "核价后考虑减仓", "近 20/60 日走势或回撤触发风险收缩条件；按个人仓位上限决定。")
    else:
        result["sell"] = _choice("hold", "可继续持有并观察", "当前未触发本规则的减仓条件；继续跟踪风险，不代表未来不会下跌。")

    if amount < 20_000_000 or severe >= 2:
        result["buy"] = _choice("avoid", "回避新增买入", "流动性偏弱或多项高风险条件触发；不适合用历史上涨机会覆盖这些风险。")
    elif r20 < 0 or r60 < 0 or severe or weakening:
        result["buy"] = _choice("wait", "暂缓买入", "短中期价格或风险条件尚未恢复；等待新数据重新评估。")
    elif (r20 >= 0.03 and r60 >= 0.05 and vol <= 0.35
          and drawdown <= 0.12 and amount >= 100_000_000):
        result["buy"] = _choice("consider", "可研究性考虑分批买入", "趋势、波动、回撤和流动性同时达到规则阈值；这是条件筛选，不是收益预测。")
    else:
        result["buy"] = _choice("wait", "继续观察", "虽然未触发明显高风险，买入条件尚未同时满足。")

    if amount < 20_000_000:
        risks.append("近 20 日平均成交额低于 2000 万元，实际减仓可能受流动性限制。")
    if vol >= 0.60 or drawdown >= 0.25:
        risks.append("波动或历史回撤较高，需先核对仓位承受能力。")

    lag = (today - asof).days
    if lag >= 2:
        result["status"] = "insufficient_evidence"
        result["buy"] = _choice("wait", "等待更新行情", "报告早于当前中国日历日期至少 2 天，无法确认最新交易状态。")
        result["sell"] = _choice("unavailable", "先核价再判断卖出", "历史风险信号可能已变化；需要最新交易日收盘价。")
        risks.append("报告日期已落后；不作为当前交易指令。")
    elif lag == 1:
        result["status"] = "insufficient_evidence"
        if result["buy"]["stance"] == "consider":
            result["buy"] = _choice("wait", "核对今日行情后再考虑买入", "前一日条件满足，但今天价格和交易状态未知。")
        if result["sell"]["stance"] in ("consider_reduce", "review_exit"):
            result["sell"]["reason"] += " 这是上一交易日信号，不能直接作为今天的下单指令。"
        risks.append("报告基于前一日收盘，尚未核对今日价格与交易状态。")
    else:
        result["status"] = "ready"
    result["next_check"] = "执行前核对最新价格、涨跌停和停牌状态；下一交易日收盘后按同一规则复核。"
    return result
