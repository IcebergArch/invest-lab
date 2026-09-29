from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping, Sequence

from quant_lab.backtest import BacktestResult, run_backtest
from quant_lab.storage import MarketStore
from quant_lab.strategies import (
    CrossSectionalMomentumStrategy,
    MeanReversionZScoreStrategy,
    SmaTrendStrategy,
    Strategy,
    default_strategies,
)
from quant_lab.universe import CORE_INSTRUMENTS


FOCUS_STOCK_IDS = [
    "stock:002475.SZ",
    "stock:000338.SZ",
    "stock:002714.SZ",
]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def verify_formulas() -> list[Check]:
    checks: list[Check] = []

    sma = SmaTrendStrategy(fast_window=1, slow_window=3)
    sma_weights = sma.weights({"rise": [1.0, 2.0, 3.0], "fall": [3.0, 2.0, 1.0]})
    checks.append(
        Check(
            "SMA formula",
            sma_weights == {"rise": 1.0},
            f"expected rise=1.0, got {sma_weights}",
        )
    )

    momentum = CrossSectionalMomentumStrategy(lookback=2, top_n=1)
    scores = momentum.scores({"winner": [100.0, 110.0, 120.0], "loser": [100.0, 95.0, 90.0]})
    momentum_weights = momentum.weights(
        {"winner": [100.0, 110.0, 120.0], "loser": [100.0, 95.0, 90.0]}
    )
    checks.append(
        Check(
            "momentum formula",
            math.isclose(scores["winner"], 0.2) and momentum_weights == {"winner": 1.0},
            f"expected score=0.2 and winner=1.0, got scores={scores}, weights={momentum_weights}",
        )
    )

    reversion = MeanReversionZScoreStrategy(window=3, entry_z=1.0)
    zscores = reversion.zscores({"cheap": [10.0, 10.0, 7.0], "flat": [9.0, 10.0, 9.0]})
    reversion_weights = reversion.weights(
        {"cheap": [10.0, 10.0, 7.0], "flat": [9.0, 10.0, 9.0]}
    )
    checks.append(
        Check(
            "z-score formula",
            math.isclose(zscores["cheap"], -math.sqrt(2.0))
            and reversion_weights == {"cheap": 1.0},
            f"expected cheap z=-sqrt(2) and weight=1.0, got z={zscores}, weights={reversion_weights}",
        )
    )

    class AlwaysLong:
        name = "always-long-oracle"

        def weights(self, history: Mapping[str, Sequence[float]]) -> Mapping[str, float]:
            return {"asset": 1.0}

    dates = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)]
    accounting = run_backtest(
        AlwaysLong(), dates, {"asset": [100.0, 110.0, 121.0, 133.1]}, cost_rate=0.0
    )
    checks.append(
        Check(
            "backtest accounting oracle",
            math.isclose(accounting.points[-1].equity, 1.21),
            f"expected next-close equity=1.21, got {accounting.points[-1].equity}",
        )
    )

    trend = SmaTrendStrategy(fast_window=2, slow_window=3)
    base = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    changed_future = [10.0, 11.0, 12.0, 13.0, 14.0, 1500.0]
    original = run_backtest(trend, dates_for(len(base)), {"asset": base}, cost_rate=0.0)
    perturbed = run_backtest(
        trend, dates_for(len(changed_future)), {"asset": changed_future}, cost_rate=0.0
    )
    unchanged_prefix = all(
        left.weights == right.weights and math.isclose(left.equity, right.equity)
        for left, right in zip(original.points[:-1], perturbed.points[:-1])
    )
    checks.append(
        Check(
            "no future-data leakage",
            unchanged_prefix,
            "changing the final close must not alter any earlier weight or equity",
        )
    )
    return checks


def dates_for(length: int) -> list[date]:
    first = date(2026, 1, 1)
    return [first + timedelta(days=index) for index in range(length)]


def verify_store(store: MarketStore) -> list[Check]:
    expected_core = {item.instrument_id for item in CORE_INSTRUMENTS}
    summaries = store.instrument_summary()
    by_id = {str(row["instrument_id"]): row for row in summaries}
    checks = [
        Check(
            "core universe present",
            expected_core <= set(by_id),
            f"missing={sorted(expected_core - set(by_id))}",
        ),
        Check(
            "all instruments have bars",
            bool(summaries) and all(int(row["rows"] or 0) > 0 for row in summaries),
            f"empty={[row['instrument_id'] for row in summaries if int(row['rows'] or 0) == 0]}",
        ),
    ]
    industry_count = sum(row["family"] == "sw_level_1" for row in summaries)
    checks.append(
        Check(
            "SW level-one coverage",
            industry_count == 31,
            f"expected 31 industries, got {industry_count}",
        )
    )
    with store.connect() as connection:
        invalid_ohlc = connection.execute(
            """
            SELECT COUNT(*) FROM daily_bars
            WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
               OR low > open OR low > close OR high < open OR high < close
               OR volume < 0 OR amount < 0
            """
        ).fetchone()[0]
        source_counts = dict(
            connection.execute(
                "SELECT source_id, COUNT(*) FROM daily_bars GROUP BY source_id"
            ).fetchall()
        )
    checks.append(
        Check("OHLC invariants", invalid_ohlc == 0, f"invalid rows={invalid_ohlc}")
    )
    checks.append(
        Check(
            "source lineage",
            (
                source_counts.get("eastmoney_kline", 0) > 0
                or source_counts.get("tencent_kline", 0) > 0
            )
            and source_counts.get("sw_research", 0) > 0,
            f"rows by source={source_counts}",
        )
    )
    return checks


def store_warnings(store: MarketStore) -> list[str]:
    with store.connect() as connection:
        reference_dates = {
            date.fromisoformat(row[0])
            for row in connection.execute(
                "SELECT trade_date FROM daily_bars WHERE instrument_id='index:000001.SH'"
            )
        }
        industry_ids = [
            row[0]
            for row in connection.execute(
                "SELECT instrument_id FROM instruments WHERE family='sw_level_1'"
            )
        ]
        gaps: dict[str, list[date]] = {}
        for instrument_id in industry_ids:
            available = {
                date.fromisoformat(row[0])
                for row in connection.execute(
                    "SELECT trade_date FROM daily_bars WHERE instrument_id=?",
                    (instrument_id,),
                )
            }
            if not available:
                continue
            first, last = min(available), max(available)
            missing = sorted(
                day for day in reference_dates if first <= day <= last and day not in available
            )
            if missing:
                gaps[instrument_id] = missing
    if not gaps:
        return []
    unique_dates = sorted({day for values in gaps.values() for day in values})
    return [
        "申万官方历史接口存在交易日缺口："
        f"affected_instruments={len(gaps)}, unique_dates={len(unique_dates)}, "
        f"dates={[day.isoformat() for day in unique_dates]}；未跨源伪造 OHLC 填补。"
    ]


def run_actual_backtests(store: MarketStore) -> list[BacktestResult]:
    dates, panel = store.load_close_panel(FOCUS_STOCK_IDS)
    return [run_backtest(strategy, dates, panel) for strategy in default_strategies()]


def build_verification_report(store: MarketStore) -> dict[str, object]:
    formula_checks = verify_formulas()
    data_checks = verify_store(store)
    backtests = run_actual_backtests(store)
    all_checks = formula_checks + data_checks
    return {
        "passed": all(check.passed for check in all_checks),
        "checks": [asdict(check) for check in all_checks],
        "warnings": store_warnings(store),
        "backtests": [
            {
                "strategy": result.strategy_name,
                "start": result.points[0].trade_date.isoformat(),
                "end": result.points[-1].trade_date.isoformat(),
                "observations": len(result.points),
                "metrics": dict(result.metrics),
            }
            for result in backtests
        ],
        "notes": [
            "信号仅使用当日及以前的收盘价，在下一交易日收盘代理成交，再从随后收益区间生效。",
            "结果验证计算逻辑与数据完整性，不代表策略具有未来收益。",
        ],
    }


def write_report(report: Mapping[str, object], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
