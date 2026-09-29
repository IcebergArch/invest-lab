"""Evidence gate for three human-review stock ideas.

The current project has a 5,222-name *current* BaoStock list, but only three
focus stocks in its main price store.  A historical test of those three names
does not validate a market-wide selection rule.  This module makes that gap an
explicit output instead of silently relabeling the focus list as picks.

Future producers may supply ``recommendation_validation`` in their backtest
and forecast reports.  Those fields are deliberately separate from the
current focus-stock research outputs.  A ready report still means "for human
review", never an automated order or a probability of profit.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timezone
from typing import Mapping

from quant_lab.baostock_source import BaoStockUniverseSnapshot, SOURCE_ID
from quant_lab.storage import MarketStore


RECOMMENDATION_VERSION = "three-stock-evidence-gate-v1"
RECOMMENDATION_COUNT = 3
MIN_UNIVERSE_COUNT = 500
MIN_UNIVERSE_COVERAGE = 0.99
MIN_DATA_COVERAGE = 0.95
MIN_OOS_SESSIONS = 126
MIN_SELECTION_EVENTS = 12
MIN_FORECAST_ORIGINS = 30
MAX_BACKTEST_DRAWDOWN = 0.30
MAX_P10_20D_LOSS = 0.15


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _check(checks: list[dict[str, object]], key: str, passed: bool, detail: str) -> None:
    checks.append({"key": key, "passed": passed, "detail": detail})


def _sort_score(item: Mapping[str, object]) -> tuple[float, str]:
    score = _finite(item.get("score"))
    return -(score if score is not None else float("-inf")), str(item["instrument_id"])


def _backtest_validated(
    research: Mapping[str, object], snapshot_id: str, screen_version: object, asof: date
) -> bool:
    evidence = research.get("recommendation_validation")
    if not isinstance(evidence, Mapping):
        return False
    end = _date(evidence.get("data_end"))
    drawdown = _finite(evidence.get("max_drawdown"))
    excess = _finite(evidence.get("excess_return_after_costs"))
    return all((
        evidence.get("status") == "passed",
        evidence.get("validation_type") == "stock-selection-walk-forward",
        evidence.get("universe_snapshot_id") == snapshot_id,
        evidence.get("screen_version") == screen_version,
        evidence.get("point_in_time_universe") is True,
        evidence.get("independent_out_of_sample") is True,
        evidence.get("costs_included") is True,
        evidence.get("tradability_modeled") is True,
        isinstance(evidence.get("run_id"), str) and bool(evidence["run_id"]),
        isinstance(evidence.get("oos_sessions"), int)
        and evidence["oos_sessions"] >= MIN_OOS_SESSIONS,
        isinstance(evidence.get("selection_events"), int)
        and evidence["selection_events"] >= MIN_SELECTION_EVENTS,
        excess is not None and excess > 0,
        drawdown is not None and -MAX_BACKTEST_DRAWDOWN <= drawdown <= 0,
        end is not None and 0 <= (asof - end).days <= 90,
    ))


def _forecast_validated(
    forecast: Mapping[str, object], snapshot_id: str, asof: date
) -> tuple[bool, dict[str, Mapping[str, object]]]:
    evidence = forecast.get("recommendation_validation")
    if not isinstance(evidence, Mapping):
        return False, {}
    skill = _finite(evidence.get("mae_return_skill_vs_random_walk"))
    predictions = evidence.get("predictions")
    if not isinstance(predictions, list):
        return False, {}
    by_id: dict[str, Mapping[str, object]] = {}
    for item in predictions:
        if not isinstance(item, Mapping) or not isinstance(item.get("instrument_id"), str):
            return False, {}
        key = item["instrument_id"]
        if key in by_id or item.get("origin_date") != asof.isoformat():
            return False, {}
        expected = _finite(item.get("expected_return_20d"))
        p10 = _finite(item.get("p10_return_20d"))
        if expected is None or p10 is None or p10 > expected:
            return False, {}
        by_id[key] = item
    end = _date(evidence.get("data_end"))
    valid = all((
        evidence.get("status") == "passed",
        evidence.get("validation_type") == "stock-selection-forecast-oos",
        evidence.get("universe_snapshot_id") == snapshot_id,
        evidence.get("horizon_sessions") == 20,
        evidence.get("model_id") == forecast.get("model_id"),
        isinstance(evidence.get("model_revision"), str)
        and bool(evidence["model_revision"]),
        isinstance(evidence.get("oos_origin_count"), int)
        and evidence["oos_origin_count"] >= MIN_FORECAST_ORIGINS,
        skill is not None and skill > 0,
        end is not None and 0 <= (asof - end).days <= 90,
        bool(by_id),
    ))
    return valid, by_id if valid else {}


def _current_status_ok(store: MarketStore, candidates: list[Mapping[str, object]], asof: date) -> bool:
    """Require a same-day BaoStock active/non-ST row tied to the price payload."""
    if not candidates:
        return False
    ids = [item["instrument_id"] for item in candidates]
    marks = ",".join("?" for _ in ids)
    try:
        with store.connect() as connection:
            rows = connection.execute(f"""
                SELECT b.instrument_id,b.trade_date,b.source_id,b.payload_hash,b.run_id,
                       b.amount,b.amount_unit,b.adjustment,
                       s.tradestatus,s.is_st,s.raw_amount_cny,
                       s.payload_hash AS status_hash,s.run_id AS status_run_id
                FROM daily_bars b
                LEFT JOIN baostock_daily_status s
                    ON s.instrument_id=b.instrument_id AND s.trade_date=b.trade_date
                WHERE b.instrument_id IN ({marks}) AND b.trade_date=? AND b.adjustment='qfq'
            """, [*ids, asof.isoformat()]).fetchall()
    except (sqlite3.OperationalError, KeyError, TypeError):
        return False
    by_id = {row["instrument_id"]: row for row in rows}
    if len(by_id) != len(ids):
        return False
    for item in candidates:
        row = by_id[item["instrument_id"]]
        lineage = item.get("latest_bar_lineage")
        if not isinstance(lineage, Mapping):
            return False
        amount = _finite(row["amount"])
        raw_amount = _finite(row["raw_amount_cny"])
        if not all((
            row["source_id"] == SOURCE_ID,
            row["amount_unit"] == "CNY",
            amount is not None and amount > 0,
            row["tradestatus"] == 1,
            row["is_st"] == 0,
            raw_amount is not None and raw_amount > 0,
            row["payload_hash"] == row["status_hash"] == lineage.get("payload_hash"),
            row["run_id"] == row["status_run_id"] == lineage.get("run_id"),
            isinstance(row["run_id"], str) and bool(row["run_id"]),
            isinstance(row["payload_hash"], str) and bool(row["payload_hash"]),
            lineage.get("source_id") == SOURCE_ID,
            lineage.get("adjustment") == "qfq",
        )):
            return False
    return True


def build_recommendations(
    store: MarketStore,
    stock_screen: Mapping[str, object],
    research: Mapping[str, object],
    forecast: Mapping[str, object],
    snapshot: BaoStockUniverseSnapshot | None,
    asof: date,
) -> dict[str, object]:
    """Emit exactly three diversified names only after every evidence gate.

    ``stock_screen`` must be built by ``build_stock_shortlist`` over the
    supplied validated snapshot, and the two validation records must come
    from future market-wide producers.  This function is intentionally
    fail-closed for today's three-stock pilot reports.
    """
    if not isinstance(asof, date) or isinstance(asof, datetime):
        raise TypeError("asof must be a date")
    checks: list[dict[str, object]] = []
    snapshot_id = snapshot.snapshot_id if snapshot else None
    snapshot_ids = {item.instrument_id for item in snapshot.instruments} if snapshot else set()
    collected = _date(snapshot.collected_at[:10]) if snapshot else None
    snapshot_ok = bool(
        snapshot and len(snapshot_ids) == len(snapshot.instruments)
        and len(snapshot_ids) >= MIN_UNIVERSE_COUNT
        and collected is not None and 0 <= (collected - asof).days <= 4
    )
    _check(checks, "current_universe", snapshot_ok,
           "需要近期采集且可核验的沪深在市股票清单；北交所另计。")

    requested = stock_screen.get("universe")
    supplied = set(requested) if isinstance(requested, list) and all(
        isinstance(key, str) for key in requested) else set()
    universe_ok = bool(
        snapshot_ok and isinstance(requested, list) and len(supplied) == len(requested)
        and supplied <= snapshot_ids
        and len(supplied) / len(snapshot_ids) >= MIN_UNIVERSE_COVERAGE
        and stock_screen.get("universe_snapshot_id") == snapshot_id
        and stock_screen.get("expected_universe_count") == len(snapshot_ids)
    )
    _check(checks, "universe_coverage", universe_ok,
           f"主库需覆盖核对清单至少 {MIN_UNIVERSE_COVERAGE:.0%} 的股票身份。")

    coverage = _finite(stock_screen.get("coverage_rate"))
    data_ok = bool(
        stock_screen.get("status") == "ready"
        and stock_screen.get("asof") == asof.isoformat()
        and coverage is not None and coverage >= MIN_DATA_COVERAGE
        and stock_screen.get("data_ready_count") == int(round(coverage * len(supplied)))
    )
    _check(checks, "recent_tradable_data", data_ok,
           "需同日、近61个交易日价格及近20个交易日人民币成交额，含停牌/ST门禁。")

    backtest_ok = bool(snapshot_id and _backtest_validated(
        research, snapshot_id, stock_screen.get("screen_version"), asof
    ))
    _check(checks, "marketwide_backtest", backtest_ok,
           "需按历史当时股票池滚动回测，独立样本外至少126日、12次筛选，并计成本、可成交性及回撤。")

    forecast_ok, predictions = _forecast_validated(forecast, snapshot_id or "", asof)
    _check(checks, "forecast_validation", forecast_ok,
           "需市场级样本外预测检验优于随机游走，并提供同日20交易日预测与风险分位。")

    raw = stock_screen.get("candidates")
    candidates = [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []
    candidate_ids = [item.get("instrument_id") for item in candidates]
    candidate_ok = bool(
        len(candidates) >= RECOMMENDATION_COUNT
        and all(isinstance(key, str) for key in candidate_ids)
        and len(set(candidate_ids)) == len(candidate_ids)
        and all(key in supplied for key in candidate_ids)
    )
    _check(checks, "candidate_pool", candidate_ok,
           "筛选器须提供至少3只不同股票，且每只属于已核对股票池。")

    status_ok = candidate_ok and _current_status_ok(store, candidates, asof)
    _check(checks, "source_status", status_ok,
           "候选当日日线与BaoStock交易/ST状态须同源、同日、同运行及原始行哈希。")

    selected: list[dict[str, object]] = []
    used_groups: set[str] = set()
    if candidate_ok and forecast_ok and status_ok:
        for item in sorted(candidates, key=_sort_score):
            key = item["instrument_id"]
            prediction = predictions.get(key)
            group = item.get("industry_group")
            metrics = item.get("metrics")
            if not prediction or not isinstance(group, str) or not group or group in used_groups:
                continue
            if not isinstance(metrics, Mapping):
                continue
            expected = _finite(prediction.get("expected_return_20d"))
            p10 = _finite(prediction.get("p10_return_20d"))
            volatility = _finite(metrics.get("annualized_volatility_20d"))
            drawdown = _finite(metrics.get("max_drawdown_60d"))
            if (expected is None or expected <= 0 or p10 is None or p10 < -MAX_P10_20D_LOSS
                    or volatility is None or volatility > 0.6
                    or drawdown is None or drawdown > 0.25):
                continue
            selected.append({
                "rank": len(selected) + 1,
                "instrument_id": key,
                "name": item.get("name") or key,
                "industry_group": group,
                "asof": asof.isoformat(),
                "screen_score": item.get("score"),
                "risk_metrics": dict(metrics),
                "forecast_expected_return_20d": expected,
                "forecast_p10_return_20d": p10,
                "latest_bar_lineage": dict(item["latest_bar_lineage"]),
                "basis": "近期涨势、波动、回撤与成交额通过规则筛选；市场级样本外回测及预测验证通过。",
            })
            used_groups.add(group)
            if len(selected) == RECOMMENDATION_COUNT:
                break
    diversity_ok = len(selected) == RECOMMENDATION_COUNT
    _check(checks, "three_distinct_industries", diversity_ok,
           "需3只分属不同大类，且20日预测收益为正、10%分位亏损未超过15%。")

    ready = all(item["passed"] for item in checks)
    failures = [item["detail"] for item in checks if not item["passed"]]
    return {
        "recommendation_version": RECOMMENDATION_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asof": asof.isoformat(),
        "status": "ready_for_human_review" if ready else "blocked_insufficient_evidence",
        "reason": "已通过全部证据门禁；仅供人工决策。" if ready else "；".join(failures),
        "requested_count": RECOMMENDATION_COUNT,
        "recommendation_count": RECOMMENDATION_COUNT if ready else 0,
        "recommendations": selected if ready else [],
        "checks": checks,
        "universe_snapshot_id": snapshot_id,
        "stock_screen_version": stock_screen.get("screen_version"),
        "selection_policy": "按已验证风险收益规则分排序；每个行业大类最多一只；预测需经样本外验证。",
        "limitations": [
            "BaoStock快照是当前在市沪深股票池，不含北交所；历史回测须另存历史当时的成分表。",
            "样本外回测及预测不能保证未来收益，分位区间不能覆盖所有黑天鹅事件。",
            "三只股票仍有集中风险；结果供人工审核，不生成订单。",
        ],
    }
