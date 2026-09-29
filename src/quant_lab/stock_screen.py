"""Explainable, price-only A-share shortlist for human review.

The caller supplies the universe and the last date the screen may see. This is
an end-of-day research screen, not a calibrated forecast or an order signal.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Sequence

from quant_lab.storage import MarketStore


SCREEN_VERSION = "stock-risk-return-v1"
_HISTORY_SESSIONS = 61  # 60 trading-day return/drawdown plus the starting close
_BATCH_SIZE = 400  # leave room for date and limit parameters on older SQLite


@dataclass(frozen=True)
class StockScreenConfig:
    top_n: int = 5
    # A small pilot pool must never be displayed as a broad A-share shortlist.
    # This floor does not itself verify a complete market universe.
    min_universe_size: int = 500
    expected_universe_count: int | None = None
    min_expected_universe_coverage: float = 0.99
    min_universe_coverage: float = 0.95
    max_calendar_lag_days: int = 4
    min_avg_amount_cny: float = 20_000_000.0
    max_annualized_volatility: float = 0.6
    max_drawdown_60d: float = 0.25
    min_momentum_20d: float = 0.0
    min_momentum_60d: float = 0.0

    def __post_init__(self) -> None:
        if self.top_n < 1:
            raise ValueError("top_n must be positive")
        if self.min_universe_size < 1:
            raise ValueError("min_universe_size must be positive")
        if self.expected_universe_count is not None and self.expected_universe_count < 1:
            raise ValueError("expected_universe_count must be positive")
        if not 0 < self.min_expected_universe_coverage <= 1:
            raise ValueError("min_expected_universe_coverage must be in (0, 1]")
        if not 0 < self.min_universe_coverage <= 1:
            raise ValueError("min_universe_coverage must be in (0, 1]")
        if self.max_calendar_lag_days < 0:
            raise ValueError("max_calendar_lag_days cannot be negative")
        for name in ("min_avg_amount_cny", "max_annualized_volatility",
                     "max_drawdown_60d", "min_momentum_20d", "min_momentum_60d"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.min_avg_amount_cny < 0 or self.max_annualized_volatility <= 0:
            raise ValueError("liquidity must be nonnegative and volatility cap positive")
        if not 0 < self.max_drawdown_60d <= 1:
            raise ValueError("max_drawdown_60d must be in (0, 1]")


def _chunks(values: Sequence[str]) -> list[Sequence[str]]:
    return [values[start:start + _BATCH_SIZE]
            for start in range(0, len(values), _BATCH_SIZE)]


def _load_inputs(store: MarketStore, instrument_ids: Sequence[str], asof: date
                 ) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, dict[str, object]]], list[str]]:
    metadata: dict[str, dict[str, str]] = {}
    bars: dict[str, dict[str, dict[str, object]]] = {key: {} for key in instrument_ids}
    benchmark_dates: list[str] = []
    with store.connect() as connection:
        # The benchmark is a calendar only. Its price never enters a stock score.
        benchmark_dates = [row[0] for row in connection.execute(
            "SELECT DISTINCT trade_date FROM daily_bars WHERE instrument_id='index:000300.SH' "
            "AND trade_date<=? ORDER BY trade_date DESC LIMIT ?",
            (asof.isoformat(), _HISTORY_SESSIONS),
        )]
        for chunk in _chunks(instrument_ids):
            marks = ",".join("?" for _ in chunk)
            for row in connection.execute(
                f"SELECT instrument_id,name,asset_type,adjustment FROM instruments "
                f"WHERE instrument_id IN ({marks})", chunk
            ):
                metadata[row["instrument_id"]] = dict(row)
            # Query only rows available by `asof` and only the instrument's
            # configured adjustment. Fetching a bounded tail scales to a larger
            # supplied universe without loading each stock's entire history.
            sql = f"""
                WITH recent AS (
                    SELECT b.instrument_id,b.trade_date,b.close,b.amount,
                           b.amount_unit,b.adjustment,b.source_id,b.payload_hash,
                           b.run_id,b.fetched_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY b.instrument_id ORDER BY b.trade_date DESC
                           ) AS row_number
                    FROM daily_bars b
                    JOIN instruments i ON i.instrument_id=b.instrument_id
                    WHERE b.instrument_id IN ({marks})
                      AND i.asset_type='stock' AND b.adjustment=i.adjustment
                      AND b.trade_date<=?
                )
                SELECT * FROM recent WHERE row_number<=?
                ORDER BY instrument_id,trade_date
            """
            for row in connection.execute(sql, [*chunk, asof.isoformat(), _HISTORY_SESSIONS]):
                bars[row["instrument_id"]][row["trade_date"]] = dict(row)
    return metadata, bars, sorted(benchmark_dates)


def _risk_metrics(rows: list[dict[str, object]]) -> dict[str, float]:
    closes = [float(row["close"]) for row in rows]
    amounts = [float(row["amount"]) for row in rows[-20:]]
    daily_returns = [closes[index] / closes[index - 1] - 1
                     for index in range(len(closes) - 20, len(closes))]
    mean_return = sum(daily_returns) / len(daily_returns)
    variance = sum((value - mean_return) ** 2 for value in daily_returns) / len(daily_returns)
    peak = closes[0]
    drawdown = 0.0
    for close in closes[1:]:
        peak = max(peak, close)
        drawdown = max(drawdown, 1 - close / peak)
    return {
        "momentum_20d": closes[-1] / closes[-21] - 1,
        "momentum_60d": closes[-1] / closes[0] - 1,
        "annualized_volatility_20d": math.sqrt(variance * 252),
        "max_drawdown_60d": drawdown,
        "avg_amount_20d_cny": sum(amounts) / len(amounts),
        "latest_close": closes[-1],
    }


def _risk_reasons(metrics: dict[str, float], config: StockScreenConfig) -> list[str]:
    reasons = []
    if metrics["avg_amount_20d_cny"] < config.min_avg_amount_cny:
        reasons.append("近20日平均成交额不足")
    if metrics["annualized_volatility_20d"] > config.max_annualized_volatility:
        reasons.append("近20日价格波动过大")
    if metrics["max_drawdown_60d"] > config.max_drawdown_60d:
        reasons.append("近60日最大回撤过深")
    if metrics["momentum_20d"] <= config.min_momentum_20d:
        reasons.append("近20日走势未达到门槛")
    if metrics["momentum_60d"] <= config.min_momentum_60d:
        reasons.append("近60日走势未达到门槛")
    return reasons


def build_stock_shortlist(
    store: MarketStore,
    instrument_ids: Sequence[str],
    asof: date,
    config: StockScreenConfig | None = None,
) -> dict[str, object]:
    """Screen supplied stock IDs using only bars with trade_date <= asof.

    A narrow supplied list must be described as a narrow list by its caller.
    Historical data can have been revised since `asof`; the store currently has
    no immutable data-publication snapshots for a true historical replay.
    """
    if not isinstance(asof, date) or isinstance(asof, datetime):
        raise TypeError("asof must be a date")
    ids = list(dict.fromkeys(instrument_ids))
    if not ids or any(not isinstance(key, str) or not key for key in ids):
        raise ValueError("instrument_ids must contain nonempty stock IDs")
    config = config or StockScreenConfig()
    metadata, bars, benchmark_dates = _load_inputs(store, ids, asof)
    union_dates = sorted({day for item in bars.values() for day in item})
    if (len(benchmark_dates) >= _HISTORY_SESSIONS and
            (not union_dates or benchmark_dates[-1] >= union_dates[-1])):
        reference_dates = benchmark_dates[-_HISTORY_SESSIONS:]
        calendar_source = "index:000300.SH"
    else:
        reference_dates = union_dates[-_HISTORY_SESSIONS:]
        calendar_source = "requested_stock_union"
    market_date = date.fromisoformat(reference_dates[-1]) if reference_dates else None

    result: dict[str, object] = {
        "screen_version": SCREEN_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_asof": asof.isoformat(),
        "asof": market_date.isoformat() if market_date else None,
        "calendar_source": calendar_source,
        "universe_count": len(ids),
        "universe_scope": "仅调用方提供的股票ID；未经成分清单逐一验证，不称为全A股",
        "expected_universe_count": config.expected_universe_count,
        "expected_universe_coverage_rate": (
            len(ids) / config.expected_universe_count
            if config.expected_universe_count is not None else None
        ),
        "universe": ids,
        "history_sessions_required": _HISTORY_SESSIONS,
        "data_ready_count": 0,
        "coverage_rate": 0.0,
        "eligible_count": 0,
        "candidate_count": 0,
        "candidates": [],
        "excluded": [],
        "forecast_probability": None,
        "parameters": asdict(config),
        "score_definition": "100 × (0.6 × 20日涨幅 + 0.4 × 60日涨幅 − 0.15 × 20日年化波动率 − 0.25 × 60日最大回撤)；规则排序分，非收益预测或概率",
        "limitations": [
            "只筛选调用方提供的股票池；覆盖率也是相对该股票池，不代表全A股覆盖率。",
            "股票数门槛和预期数量比值不能验证成分身份；全A股覆盖仍须与有日期的权威股票清单逐一核对。",
            "历史复权价和证券元数据可能被改写；现有数据库不是不可变的历史时点快照。",
            "日线成交额和收盘价不能证明盘中可成交、真实资金净流入或未来收益。",
            "价格过滤无法保证规避黑天鹅、停牌或极端流动性事件；不生成自动订单。",
        ],
    }

    if market_date is None or (asof - market_date).days > config.max_calendar_lag_days:
        result.update(status="blocked_stale_data", reason="股票池最近行情过旧或没有行情；本次不筛选。")
        return result
    if len(reference_dates) < _HISTORY_SESSIONS:
        result.update(status="blocked_insufficient_history", reason="不足61个参考交易日；本次不筛选。")
        return result

    excluded: list[dict[str, object]] = []
    ready: list[dict[str, object]] = []
    for key in ids:
        meta = metadata.get(key)
        name = meta["name"] if meta else key
        reasons: list[str] = []
        if meta is None:
            reasons.append("股票ID未登记")
        elif meta["asset_type"] != "stock":
            reasons.append("不是股票")
        by_date = bars[key]
        if not by_date:
            reasons.append("截至日期无日线行情")
        elif any(day not in by_date for day in reference_dates):
            reasons.append("最近61个参考交易日行情不完整或未更新")
        rows = [by_date[day] for day in reference_dates if day in by_date]
        if len(rows) == _HISTORY_SESSIONS:
            if any(row["amount_unit"] != "CNY" or row["amount"] is None
                   or not math.isfinite(float(row["amount"])) or float(row["amount"]) < 0
                   for row in rows[-20:]):
                reasons.append("近20日人民币成交额缺失或口径不一致")
            if any(not math.isfinite(float(row["close"])) or float(row["close"]) <= 0
                   for row in rows):
                reasons.append("收盘价无效")
        if reasons:
            excluded.append({"instrument_id": key, "name": name, "reasons": reasons})
            continue
        metrics = _risk_metrics(rows)
        ready.append({"instrument_id": key, "name": name, "metrics": metrics,
                      "latest_bar_lineage": {field: rows[-1][field] for field in
                                             ("source_id", "payload_hash", "run_id", "fetched_at", "adjustment")}})

    result["data_ready_count"] = len(ready)
    result["coverage_rate"] = len(ready) / len(ids)
    result["excluded"] = excluded
    if result["coverage_rate"] < config.min_universe_coverage:
        result.update(status="blocked_universe_coverage",
                      reason=f"仅 {len(ready)}/{len(ids)} 只股票有完整且同日的可用行情；本次不筛选。")
        return result
    if len(ids) < config.min_universe_size:
        result.update(status="blocked_narrow_universe",
                      reason=f"股票池仅 {len(ids)} 只，低于宽样本筛选门槛 {config.min_universe_size} 只；本次不生成股票推荐。")
        return result
    if config.expected_universe_count is not None and len(ids) > config.expected_universe_count:
        result.update(status="blocked_invalid_universe_count",
                      reason="输入股票数多于已核对清单的预期数量；请先核对股票池，暂不筛选。")
        return result
    if (config.expected_universe_count is not None and
            result["expected_universe_coverage_rate"] < config.min_expected_universe_coverage):
        result.update(status="blocked_expected_universe_coverage",
                      reason="相对已核对股票清单的覆盖率不足；本次不生成股票推荐。")
        return result

    eligible = []
    for item in ready:
        metrics = item["metrics"]
        reasons = _risk_reasons(metrics, config)
        if reasons:
            excluded.append({"instrument_id": item["instrument_id"],
                             "name": item["name"], "reasons": reasons,
                             "metrics": metrics})
            continue
        score = 100 * (0.6 * metrics["momentum_20d"] +
                       0.4 * metrics["momentum_60d"] -
                       0.15 * metrics["annualized_volatility_20d"] -
                       0.25 * metrics["max_drawdown_60d"])
        item["score"] = round(score, 4)
        item["reason"] = (
            f"历史已实现涨跌：近20日 {metrics['momentum_20d']:+.1%}，近60日 {metrics['momentum_60d']:+.1%}；"
            f"近20日年化波动 {metrics['annualized_volatility_20d']:.1%}，"
            f"近60日最大回撤 {metrics['max_drawdown_60d']:.1%}，"
            f"近20日平均成交额 {metrics['avg_amount_20d_cny'] / 1e8:.2f} 亿元。"
            "这些是已发生的价格与成交记录，不是未来预期收益。"
        )
        item["forecast_probability"] = None
        item["status"] = "待人工复核"
        eligible.append(item)
    eligible.sort(key=lambda item: (-item["score"], item["instrument_id"]))
    candidates = []
    for rank, item in enumerate(eligible[:config.top_n], 1):
        candidates.append({"rank": rank, "asof": result["asof"], **item})
    result["eligible_count"] = len(eligible)
    result["candidate_count"] = len(candidates)
    result["candidates"] = candidates
    if candidates:
        result.update(status="ready", reason="已按涨势、波动、回撤和成交额筛出待人工复核股票。")
    else:
        result.update(status="no_qualified_stocks", reason="本期没有股票同时通过收益趋势和风险门槛。")
    return result
