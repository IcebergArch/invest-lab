"""Read-only, dated analysis of one stock already present in MarketStore.

This is a historical research card.  Its simple forecast and backtest are
baselines, not a calibrated return estimate or a trading recommendation.
"""
from __future__ import annotations

from quant_lab.cost_model import FEE_RATE_PER_SIDE

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from quant_lab.backtest import run_backtest, run_buy_and_hold
from quant_lab.forecast import EvaluationConfig, RandomWalkProvider, evaluate_forecasts
from quant_lab.storage import MarketStore
from quant_lab.stock_chart import build_stock_chart
from quant_lab.strategies import SmaTrendStrategy


def _empty_report(query: str, cost_price: float | None) -> dict[str, Any]:
    return {
        "status": "unknown_stock",
        "reason": "库中没有这只股票。请输入已入库的六位代码、交易所代码或准确名称。",
        "query": query,
        "instrument_id": None,
        "name": None,
        "asof": None,
        "latest_close": None,
        "cost_price": cost_price,
        "cost_return": None,
        "summary": "暂时无法生成分析报告。",
        "trend": None,
        "risk": None,
        "forecast": None,
        "backtest": None,
        "sources": [],
        "limitations": [],
    }


def _parse_cost(value: float | str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("成本价必须是正数") from exc
    if not math.isfinite(cost) or cost <= 0:
        raise ValueError("成本价必须是有限的正数")
    return cost


def _find_stock(store: MarketStore, query: str) -> tuple[dict[str, Any] | None, list[str]]:
    normalized = query.strip().upper()
    if normalized.startswith("STOCK:"):
        normalized = normalized[6:]
    if len(normalized) == 9 and normalized[:3] in ("SH.", "SZ.", "BJ."):
        normalized = normalized[3:] + "." + normalized[:2]
    with store.connect() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT instrument_id,symbol,name,asset_type,exchange,adjustment,source_id "
            "FROM instruments WHERE asset_type='stock' ORDER BY instrument_id"
        )]
    exact = [row for row in rows if normalized in (
        row["instrument_id"].upper(), row["symbol"].upper(), row["name"].upper(),
        row["instrument_id"].split(":", 1)[-1].upper(),
    )]
    if exact:
        return (exact[0], []) if len(exact) == 1 else (None, [row["instrument_id"] for row in exact])
    if len(normalized) == 6 and normalized.isdigit():
        matches = [row for row in rows if row["symbol"].split(".", 1)[0] == normalized]
        return (matches[0], []) if len(matches) == 1 else (
            None, [row["instrument_id"] for row in matches]
        )
    return None, []


def _max_drawdown(closes: list[float]) -> float:
    peak = closes[0]
    largest = 0.0
    for close in closes[1:]:
        peak = max(peak, close)
        largest = max(largest, 1.0 - close / peak)
    return largest


def _metrics(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    closes = [float(row["close"]) for row in rows]
    result: dict[str, float | None] = {
        "return_5d": closes[-1] / closes[-6] - 1 if len(closes) >= 6 else None,
        "return_20d": closes[-1] / closes[-21] - 1 if len(closes) >= 21 else None,
        "return_60d": closes[-1] / closes[-61] - 1 if len(closes) >= 61 else None,
        "annualized_volatility_20d": None,
        "max_drawdown_60d": _max_drawdown(closes[-61:]) if len(closes) >= 61 else None,
        "avg_amount_20d_cny": None,
    }
    if len(closes) >= 21:
        daily = [closes[index] / closes[index - 1] - 1 for index in range(len(closes) - 20, len(closes))]
        average = sum(daily) / len(daily)
        variance = sum((value - average) ** 2 for value in daily) / len(daily)
        result["annualized_volatility_20d"] = math.sqrt(variance * 252)
    recent = rows[-20:]
    if len(recent) == 20 and all(
        row["amount"] is not None and row["amount_unit"] == "CNY"
        and math.isfinite(float(row["amount"])) and float(row["amount"]) >= 0
        for row in recent
    ):
        result["avg_amount_20d_cny"] = sum(float(row["amount"]) for row in recent) / 20
    return result


def _trend(metrics: dict[str, float | None]) -> dict[str, Any]:
    short = metrics["return_20d"]
    long = metrics["return_60d"]
    if short is None or long is None:
        label = "历史不足"
        detail = "需要至少 61 个交易日收盘价，才能同时看近 20 日和 60 日走势。"
    elif short > 0 and long > 0:
        label = "近 20 日与 60 日均上涨"
        detail = f"近 20 日 {short:+.1%}，近 60 日 {long:+.1%}；这是已发生的价格变化。"
    elif short < 0 and long < 0:
        label = "近 20 日与 60 日均下跌"
        detail = f"近 20 日 {short:+.1%}，近 60 日 {long:+.1%}；这是已发生的价格变化。"
    else:
        label = "短中期方向不一致"
        detail = f"近 20 日 {short:+.1%}，近 60 日 {long:+.1%}；暂不把它简化为单一趋势。"
    return {"label": label, "detail": detail, "metrics": metrics}


def _risk(metrics: dict[str, float | None], missing_market_sessions: int | None) -> dict[str, Any]:
    flags: list[str] = []
    if missing_market_sessions:
        flags.append(f"相对沪深 300 交易日历，最近缺少 {missing_market_sessions} 个交易日的股票收盘价。")
    if metrics["return_20d"] is None or metrics["return_60d"] is None:
        flags.append("历史收盘价不足，部分趋势与风险指标无法计算。")
    if metrics["avg_amount_20d_cny"] is None:
        flags.append("近 20 日人民币成交额不完整，无法可靠评估流动性。")
    elif metrics["avg_amount_20d_cny"] < 20_000_000:
        flags.append("近 20 日平均成交额低于 2000 万元，流动性可能偏弱。")
    if metrics["annualized_volatility_20d"] is not None and metrics["annualized_volatility_20d"] > 0.6:
        flags.append("近 20 日价格年化波动率高于 60%，价格波动较大。")
    if metrics["max_drawdown_60d"] is not None and metrics["max_drawdown_60d"] > 0.25:
        flags.append("近 60 日收盘价最大回撤超过 25%。")
    if metrics["return_20d"] is not None and metrics["return_20d"] < 0:
        flags.append("近 20 日价格下跌。")
    return {
        "flags": flags,
        "summary": "；".join(flags) if flags else "未触发本报告的价格与流动性提示；不代表风险低。",
        "metrics": metrics,
    }


def _forecast(dates: list[date], closes: list[float], instrument_id: str) -> dict[str, Any]:
    latest = closes[-1]
    evaluated = evaluate_forecasts(
        dates, closes, RandomWalkProvider(), instrument_id,
        config=EvaluationConfig(horizons=(5, 20), min_context=120, step=20),
    )
    validation = {
        horizon: {
            "status": item["status"],
            "count": item["count"],
            "mean_absolute_return_error": item.get("mae_return"),
        }
        for horizon, item in evaluated["summary"].items()
    }
    return {
        "status": "baseline_only" if evaluated["status"] == "ready" else "insufficient_history",
        "method": "最后收盘价不变（随机游走基线）",
        "baseline_close_5d": latest,
        "baseline_close_20d": latest,
        "rolling_validation": validation,
        "direction_probability": None,
        "model_research_status": "Google TimesFM 尚未对该股票完成独立验证，结果不进入此报告。",
        "explanation": "5/20 个交易日基线只用于与后续模型比较，不是目标价或上涨概率。",
    }


def _backtest(dates: list[date], closes: list[float], instrument_id: str) -> dict[str, Any]:
    if len(dates) < 120:
        return {
            "status": "insufficient_history",
            "reason": "少于 120 个收盘价，暂不运行单股策略示例回测。",
        }
    panel = {instrument_id: closes}
    strategy = run_backtest(SmaTrendStrategy(), dates, panel, cost_rate=FEE_RATE_PER_SIDE)
    hold = run_buy_and_hold(dates, panel, cost_rate=FEE_RATE_PER_SIDE)
    return {
        "status": "historical_diagnostic_only",
        "strategy": "20/60 日均线趋势，空仓或持有该股",
        "start": dates[0].isoformat(),
        "end": dates[-1].isoformat(),
        "observations": len(dates),
        "strategy_metrics": dict(strategy.metrics),
        "buy_hold_metrics": dict(hold.metrics),
        "cost_rate_per_turnover": FEE_RATE_PER_SIDE,
        "execution_assumption": "收盘产生信号，下一交易日收盘价代理成交。",
        "independent_out_of_sample": False,
        "reason": "这是历史示例；规则未预先冻结并在未来新数据上独立验证。",
    }


def analyze_stock(
    store: MarketStore,
    symbol: str,
    cost_price: float | str | None = None,
    asof: date | None = None,
) -> dict[str, Any]:
    """Build a dated stock card using only stored bars on or before ``asof``.

    A missing symbol or missing history is represented by an explicit status.
    Invalid cost input raises ValueError for the caller to present as a form error.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("请输入股票代码或名称")
    if asof is not None and (not isinstance(asof, date) or isinstance(asof, datetime)):
        raise TypeError("asof must be a date")
    cost = _parse_cost(cost_price)
    cutoff = asof or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    report = _empty_report(symbol.strip(), cost)
    instrument, ambiguous = _find_stock(store, symbol)
    if ambiguous:
        report.update(status="ambiguous_stock", reason="股票代码或名称对应多只股票，请输入带交易所的完整代码。",
                      matches=ambiguous)
        return report
    if instrument is None:
        return report
    report.update(instrument_id=instrument["instrument_id"], name=instrument["name"])

    with store.connect() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT trade_date,open,high,low,close,volume,amount,amount_unit,adjustment,"
            "source_id,payload_hash,run_id,fetched_at FROM daily_bars "
            "WHERE instrument_id=? AND adjustment=? AND trade_date<=? ORDER BY trade_date",
            (instrument["instrument_id"], instrument["adjustment"], cutoff.isoformat()),
        )]
    if not rows:
        report.update(status="no_bars", reason="库中有股票登记，但截至该日期没有可用日线。",
                      summary="暂时无法生成分析报告。")
        return report
    if any(not math.isfinite(float(row["close"])) or float(row["close"]) <= 0 for row in rows):
        report.update(status="invalid_price_data", reason="库内存在无效收盘价，本次停止分析。")
        return report

    dates = [date.fromisoformat(row["trade_date"]) for row in rows]
    closes = [float(row["close"]) for row in rows]
    latest = rows[-1]
    with store.connect() as connection:
        market = connection.execute(
            "SELECT MAX(trade_date) AS latest_date, "
            "COUNT(DISTINCT CASE WHEN trade_date>? THEN trade_date END) AS missing_sessions "
            "FROM daily_bars WHERE instrument_id='index:000300.SH' AND trade_date<=?",
            (latest["trade_date"], cutoff.isoformat()),
        ).fetchone()
    metrics = _metrics(rows)
    market_latest = market["latest_date"]
    missing_market_sessions = market["missing_sessions"] if market_latest else None
    trend = _trend(metrics)
    risk = _risk(metrics, missing_market_sessions)
    cost_return = closes[-1] / cost - 1 if cost is not None else None
    sources = [
        {"source_id": source_id, "bar_count": count}
        for source_id, count in sorted(
            ((source_id, sum(row["source_id"] == source_id for row in rows))
             for source_id in {row["source_id"] for row in rows}),
            key=lambda item: item[0],
        )
    ]
    source_lineage = {
        "latest_source_id": latest["source_id"],
        "latest_run_id": latest["run_id"],
        "latest_payload_hash": latest["payload_hash"],
        "latest_fetched_at": latest["fetched_at"],
        "adjustment": latest["adjustment"],
        "first_trade_date": rows[0]["trade_date"],
        "last_trade_date": latest["trade_date"],
        "bar_count": len(rows),
        "close_history_sha256": hashlib.sha256(json.dumps(
            [[row["trade_date"], row["close"], row["payload_hash"]] for row in rows],
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest(),
        "market_reference_date": market_latest,
        "missing_recent_market_sessions": missing_market_sessions,
        "historical_sources": sources,
    }
    limitations = [
        "价格、成交额和回测均来自已入库日线；不能代表实时行情或真实资金净流入。",
        "历史前复权价可能在公司行动后改写；当前数据库并非不可变的历史时点快照。",
        "均线策略回测没有独立时间外验证，也未模拟停牌、涨跌停及准确盘中成交。",
        "基线预测没有方向概率，不能用来保证收益或规避黑天鹅。",
    ]
    if cost is not None:
        limitations.append("成本价对比只计算最新库内收盘价与输入价的差异；未核对买入日期、分红送转、持仓数量和交易费用，不是账户盈亏。")
    summary = f"{instrument['name']}截至 {latest['trade_date']} 收盘 {closes[-1]:.2f} 元；{trend['label']}。"
    if cost_return is not None:
        summary += f"相对输入成本价的价格差异 {cost_return:+.1%}，不是账户盈亏。"
    if missing_market_sessions:
        summary += f"行情相对沪深 300 日历落后 {missing_market_sessions} 个交易日。"
    status = "partial_data" if (
        metrics["return_60d"] is None or metrics["avg_amount_20d_cny"] is None
        or bool(missing_market_sessions)
    ) else "ready"
    report.update(
        status=status,
        reason="部分历史或成交额字段不足，以下可用指标仍按真实数据展示。" if status == "partial_data" else None,
        asof=latest["trade_date"],
        latest_close=closes[-1],
        cost_return=cost_return,
        summary=summary,
        trend=trend,
        risk=risk,
        forecast=_forecast(dates, closes, instrument["instrument_id"]),
        backtest=_backtest(dates, closes, instrument["instrument_id"]),
        chart=build_stock_chart(
            dates, closes, instrument_id=instrument["instrument_id"],
            price_basis="qfq_cny" if latest["adjustment"] == "qfq" else "stored_close",
            include_strategy_signals=True, include_research_forecast=True,
        ),
        sources=[source_lineage],
        limitations=limitations,
    )
    return report
