"""Small reproducible EOD research flow over the stored close panel."""
from __future__ import annotations

from quant_lab.cost_model import FEE_RATE_PER_SIDE

import json
import hashlib
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from quant_lab.backtest import period_metrics, run_backtest, run_buy_and_hold
from quant_lab.features import FEATURE_VERSION
from quant_lab.factor_library import get_factor
from quant_lab.strategy_catalog import STRATEGY_VERSION, get_strategy_definition
from quant_lab.storage import MarketStore
from quant_lab.strategies import Strategy


@dataclass(frozen=True)
class Signal:
    instrument_id: str
    trade_date: str
    strategy_id: str
    strategy_version: str
    feature_version: str
    action: str
    score: int
    reason: str
    price_at_signal: float
    target_weight: float


def get_strategy(name: str) -> Strategy:
    return get_strategy_definition(name).build()


def _reason(strategy: Strategy, values: list[float]) -> tuple[int, str]:
    if strategy.name == "sma-trend":
        factor = get_factor("sma")
        fast, slow = factor.calculate(values, strategy.fast_window), factor.calculate(values, strategy.slow_window)
        if fast is None or slow is None:
            return 0, "insufficient history"
        distance = fast / slow - 1
        return max(0, min(100, round(50 + distance * 500))), f"SMA{strategy.fast_window}/SMA{strategy.slow_window}-1={distance:.2%}"
    if strategy.name == "cross-sectional-momentum":
        value = get_factor("momentum").calculate(values, strategy.lookback)
        if value is None:
            return 0, "insufficient history"
        return max(0, min(100, round(50 + value * 100))), f"{strategy.lookback}D momentum={value:.2%}"
    value = get_factor("zscore").calculate(values, strategy.window)
    if value is None:
        return 0, "insufficient history or zero volatility"
    return max(0, min(100, round(50 - value * 20))), f"{strategy.window}D z-score={value:.2f}"


def research_report(store: MarketStore, strategy_name: str, asof: Optional[date] = None,
                    cost_rate: float = FEE_RATE_PER_SIDE) -> dict[str, object]:
    strategy = get_strategy(strategy_name)
    ids = store.list_instrument_ids("focus_stock")
    if not ids:
        raise ValueError("no focus stocks; run sync first")
    dates, panel = store.load_close_panel(ids, end=asof)
    if len(dates) < 2:
        raise ValueError("at least two common trading dates are required")
    result = run_backtest(strategy, dates, panel, cost_rate)
    equal_hold = run_buy_and_hold(dates, panel, cost_rate)
    benchmark: dict[str, object] = {
        "status": "available", "name": "focus-stock equal-weight buy-and-hold",
        "universe": ids, "cost_rate": cost_rate,
        "execution_assumption": "Equal-weight entry at the next close; no later rebalancing",
        "metrics": dict(equal_hold.metrics),
    }
    try:
        index_dates, index_panel = store.load_close_panel(["index:000300.SH"],
                                                          start=dates[0], end=dates[-1])
    except ValueError:
        index_dates, index_panel = [], {}
    if index_dates == dates:
        index_result = run_buy_and_hold(dates, index_panel, cost_rate=0.0,
                                        name="csi300-price-reference")
        index_reference: dict[str, object] = {
            "status": "available", "instrument_id": "index:000300.SH",
            "name": "沪深300价格指数", "reference_only": True, "cost_rate": 0.0,
            "metrics": dict(index_result.metrics),
        }
    else:
        index_result = None
        index_reference = {
            "status": "unavailable", "instrument_id": "index:000300.SH",
            "reason": "沪深300与股票回测的交易日期未能完整对齐",
        }
    split: dict[str, object]
    if len(dates) >= 252:
        boundary = int((len(dates) - 1) * 0.7)
        split = {
            "status": "retrospective_diagnostic_only",
            "boundary_date": dates[boundary].isoformat(),
            "early_period": {
                "start": dates[0].isoformat(), "end": dates[boundary].isoformat(),
                "sessions": boundary + 1,
                "strategy_metrics": period_metrics(result, 0, boundary),
                "equal_hold_metrics": period_metrics(equal_hold, 0, boundary),
            },
            "recent_period": {
                "start": dates[boundary].isoformat(), "end": dates[-1].isoformat(),
                "sessions": len(dates) - boundary,
                "strategy_metrics": period_metrics(result, boundary, len(dates) - 1),
                "equal_hold_metrics": period_metrics(equal_hold, boundary, len(dates) - 1),
            },
            "independent_out_of_sample": False,
            "reason": "策略参数未在分界日之前预先冻结；后段只能作回看诊断。",
        }
    else:
        split = {"status": "insufficient_history", "sessions": len(dates),
                 "minimum_sessions": 252, "independent_out_of_sample": False}
    fingerprint = hashlib.sha256(json.dumps(
        {"dates": [day.isoformat() for day in dates], "panel": panel},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    targets = strategy.weights(panel)
    previous = strategy.weights({key: values[:-1] for key, values in panel.items()})
    signals = []
    for key in ids:
        target = targets.get(key, 0.0)
        prior = previous.get(key, 0.0)
        action = "BUY" if target > prior else "SELL" if target < prior else "HOLD"
        score, reason = _reason(strategy, panel[key])
        signals.append(asdict(Signal(key, dates[-1].isoformat(), strategy.name,
                                     STRATEGY_VERSION, FEATURE_VERSION, action, score,
                                     reason, panel[key][-1], target)))
    signals.sort(key=lambda item: (-item["score"], item["instrument_id"]))
    with store.connect() as connection:
        lineage = [dict(row) for row in connection.execute(
            "SELECT b.instrument_id,b.source_id,b.payload_hash,b.run_id,b.fetched_at "
            "FROM daily_bars b WHERE b.trade_date=? AND b.instrument_id IN (" +
            ",".join("?" for _ in ids) + ") ORDER BY b.instrument_id",
            [dates[-1].isoformat(), *ids],
        )]
        source_rows = [dict(row) for row in connection.execute(
            "SELECT b.instrument_id,b.source_id,COUNT(*) AS rows "
            "FROM daily_bars b WHERE b.trade_date>=? AND b.trade_date<=? "
            "AND b.instrument_id IN (" + ",".join("?" for _ in ids) + ") "
            "GROUP BY b.instrument_id,b.source_id ORDER BY b.instrument_id,b.source_id",
            [dates[0].isoformat(), dates[-1].isoformat(), *ids],
        )]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asof": dates[-1].isoformat(),
        "strategy_id": strategy.name,
        "strategy_version": STRATEGY_VERSION,
        "strategy_parameters": {k: v for k, v in vars(strategy).items() if k != "name"},
        "feature_version": FEATURE_VERSION,
        "universe": ids,
        "adjustment": "qfq",
        "cost_rate": cost_rate,
        "execution_assumption": "EOD signal at t; execute at next trading close t+1; earn returns afterward",
        "backtest": {"start": dates[0].isoformat(), "end": dates[-1].isoformat(),
                     "observations": len(dates), "metrics": dict(result.metrics),
                     "benchmarks": {"focus_equal_weight_hold": benchmark,
                                    "csi300_price_reference": index_reference},
                     "retrospective_split": split,
                     "prospective_out_of_sample": {
                         "status": "pending", "matured_sessions": 0,
                         "reason": "需先冻结规则与数据版本，再由后续新交易日独立检验。",
                     },
                     "input_fingerprint_sha256": fingerprint,
                     "historical_bar_source_counts": source_rows,
                     "equity_curve": [{"date": point.trade_date.isoformat(), "equity": point.equity}
                                      for point in result.points]},
        "signals": signals,
        "latest_bar_lineage": lineage,
        "limitations": ["Next-close fills are a daily-data proxy; 10:30 execution requires intraday data.",
                        "Three focus stocks are not a market-wide scan.",
                        "Historical qfq prices can be revised; this database is not an immutable as-of snapshot.",
                        "The current focus-stock pool was selected with hindsight and can have survivorship bias.",
                        "Daily bars do not model suspensions, price limits, dividends, taxes, or exact execution liquidity.",
                        "Scores are rule-based ranks, not calibrated probabilities."],
    }


def write_research_report(report: dict[str, object], output: str | Path) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.with_name("backtest.md").write_text(render_backtest_markdown(report), encoding="utf-8")


def render_backtest_markdown(report: dict[str, object]) -> str:
    backtest = report["backtest"]
    metrics = backtest["metrics"]
    benchmarks = backtest["benchmarks"]
    equal = benchmarks["focus_equal_weight_hold"]
    index = benchmarks["csi300_price_reference"]

    def pct(value: float) -> str:
        return f"{value:.1%}"

    def metric_row(label: str, item: dict[str, float], cost: str) -> str:
        return (f"| {label} | {pct(item['total_return'])} | {pct(item['cagr'])} | "
                f"{pct(item['max_drawdown'])} | {pct(item['annual_volatility'])} | {cost} |")

    lines = [
        "# 回测核对卡：重点股票试运行", "",
        f"- 截至：{report['asof']}；回测：{backtest['start']} 至 {backtest['end']}，"
        f"{backtest['observations']} 个共同交易日，{len(report['universe'])} 只股票。",
        f"- 策略：`{report['strategy_id']}`；收盘产生信号，下一交易日收盘代理成交。"
        "日线不验证 10:30 成交。",
        f"- 交易成本：每单位成交额 {pct(report['cost_rate'])}，建仓和调仓均计；"
        "未单独建模佣金、印花税、滑点。", "",
        "| 对照 | 累计收益 | 年化收益 | 最大回撤 | 年化波动 | 成本口径 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
        metric_row("策略", metrics, pct(report["cost_rate"])),
        metric_row("三股等权买入持有", equal["metrics"], pct(equal["cost_rate"])),
    ]
    if index["status"] == "available":
        lines.append(metric_row("沪深300价格指数（仅作参照）", index["metrics"], "0，指数不可直接成交"))
    else:
        lines.append(f"| 沪深300价格指数 | 暂无 | 暂无 | 暂无 | 暂无 | {index['reason']} |")
    lines.extend([
        "",
        f"策略累计换手：{metrics['turnover']:.2f}（逐期成交额除以当期权益后相加）。"
        "这不是交易次数或初始资金的倍数。", "",
    ])
    split = backtest["retrospective_split"]
    if split["status"] == "retrospective_diagnostic_only":
        early = split["early_period"]
        recent = split["recent_period"]
        lines.extend([
            "## 后段历史诊断", "",
            f"前段 {early['start']} 至 {early['end']}："
            f"策略 {pct(early['strategy_metrics']['total_return'])}，"
            f"同池等权持有 {pct(early['equal_hold_metrics']['total_return'])}。",
            f"{recent['start']} 至 {recent['end']}（{recent['sessions']} 个交易日）："
            f"策略 {pct(recent['strategy_metrics']['total_return'])}，"
            f"同池等权持有 {pct(recent['equal_hold_metrics']['total_return'])}，"
            f"策略最大回撤 {pct(recent['strategy_metrics']['max_drawdown'])}。",
            "这段数据已经存在于规则开发时；因此**不是独立时间外验证**。", "",
        ])
    else:
        lines.extend(["## 后段历史诊断", "", "样本少于 252 个交易日，暂不计算。", ""])
    lines.extend([
        "## 决策边界", "",
        "真正时间外样本：**尚无**。需要冻结策略、参数、股票池和数据版本后，"
        "用未来新到的交易日检验。",
        "当前只有三只事后确定的重点股；前复权历史价可被重算，且当前库会覆盖旧值。"
        "沪深300是价格指数参照，不含交易成本，也不是可直接成交的产品。",
        "历史股票数据混合来源，回测未完成跨源价格对账。",
        "日线无法判断停牌、涨跌停和具体成交价；现有费用是统一费率。",
        f"回测输入 SHA-256：`{backtest['input_fingerprint_sha256']}`。", "",
    ])
    return "\n".join(lines)
