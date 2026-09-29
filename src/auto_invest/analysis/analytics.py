"""Analysis-service layer.

Pure computation over the data exposed by :mod:`datasources`: per-instrument
statistics, the correlation matrix, sector rotation, and backtests. No I/O of
its own beyond delegating bar/instrument loading to the project adapters.
"""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from auto_invest.adapters.csv_market_data import CsvMarketData, load_instruments
from auto_invest.backtest.engine import BacktestConfig, BacktestEngine
from auto_invest.strategies.buy_and_hold import BuyAndHoldStrategy
from auto_invest.strategies.moving_average import MovingAverageCrossStrategy

from auto_invest.analysis import datasources as ds

TRADING_DAYS = 252  # annualisation factor for volatility


def _sma(values: List[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(round(sum(values[i + 1 - window : i + 1]) / window, 4))
    return out


def max_drawdown(closes: List[float]) -> float:
    """High-watermark max drawdown, matching reports.calculate_metrics."""
    if not closes:
        return 0.0
    peak = closes[0]
    mdd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        if peak > 0:
            dd = c / peak - 1.0
            if dd < mdd:
                mdd = dd
    return mdd


def stock_research(
    rows_by_id: Dict[str, List[dict]],
    instrument_id: str,
    fast: int,
    slow: int,
) -> dict:
    series = rows_by_id.get(instrument_id)
    if not series:
        raise KeyError(instrument_id)

    dates = [r["date"] for r in series]
    closes = [r["close"] for r in series]
    volumes = [r["volume"] for r in series]

    returns: List[Optional[float]] = [None]
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        returns.append(round((closes[i] / prev - 1.0) * 100, 4) if prev else None)

    realised = [r for r in returns if r is not None]
    n = len(realised)
    cum_return = (closes[-1] / closes[0] - 1.0) * 100 if closes[0] else 0.0

    if n >= 2:
        mean_r = sum(realised) / n
        var = sum((r - mean_r) ** 2 for r in realised) / (n - 1)
        ann_vol = math.sqrt(var) * math.sqrt(TRADING_DAYS)
        ann_return = mean_r * TRADING_DAYS
        sharpe = (mean_r * TRADING_DAYS) / ann_vol if ann_vol else 0.0
        downside = [r for r in realised if r < 0]
        if downside:
            downside_dev = math.sqrt(sum(r * r for r in downside) / n) * math.sqrt(TRADING_DAYS)
            sortino = (mean_r * TRADING_DAYS) / downside_dev if downside_dev else 0.0
        else:
            sortino = 0.0
        win_rate = len([r for r in realised if r > 0]) / n * 100
        best_day, worst_day = max(realised), min(realised)
    else:
        ann_vol = ann_return = sharpe = sortino = win_rate = best_day = worst_day = 0.0

    streak = max_streak = 0
    for r in realised:
        if r < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    mdd = max_drawdown(closes) * 100

    return {
        "id": instrument_id,
        "name": ds.instrument_name(instrument_id),
        "industry": ds.instrument_industry(instrument_id),
        "fast": fast,
        "slow": slow,
        "dates": dates,
        "close": closes,
        "fast_ma": _sma(closes, fast),
        "slow_ma": _sma(closes, slow),
        "volume": volumes,
        "returns": returns,
        "stats": {
            "points": len(closes),
            "last_close": round(closes[-1], 4),
            "cum_return": round(cum_return, 2),
            "ann_return": round(ann_return, 2),
            "ann_vol": round(ann_vol, 2),
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
            "win_rate": round(win_rate, 1),
            "best_day": round(best_day, 2),
            "worst_day": round(worst_day, 2),
            "max_losing_streak": max_streak,
            "max_drawdown": round(mdd, 2),
        },
    }


def correlation_matrix(rows_by_id: Dict[str, List[dict]]) -> dict:
    """Pearson correlation of daily returns across instruments, on shared dates."""
    by_id_dates: Dict[str, Dict[str, float]] = {
        iid: {r["date"]: r["close"] for r in series}
        for iid, series in rows_by_id.items()
    }
    common = (
        sorted(set.intersection(*[set(d) for d in by_id_dates.values()]))
        if by_id_dates
        else []
    )
    ids = sorted(rows_by_id)

    returns: Dict[str, List[float]] = {}
    for iid in ids:
        closes = [by_id_dates[iid][d] for d in common]
        returns[iid] = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]

    def pearson(a: List[float], b: List[float]) -> Optional[float]:
        m = len(a)
        if m < 2:
            return None
        ma, mb = sum(a) / m, sum(b) / m
        cov = sum((a[i] - ma) * (b[i] - mb) for i in range(m))
        va = math.sqrt(sum((x - ma) ** 2 for x in a))
        vb = math.sqrt(sum((x - mb) ** 2 for x in b))
        if va == 0 or vb == 0:
            return None
        return cov / (va * vb)

    matrix = []
    for i in ids:
        matrix.append([
            (lambda c: round(c, 3) if c is not None else None)(pearson(returns[i], returns[j]))
            for j in ids
        ])

    return {
        "ids": ids,
        "names": [ds.instrument_name(i) for i in ids],
        "matrix": matrix,
        "points": max(len(common) - 1, 0),
    }


def industry_breakdown(rows_by_id: Dict[str, List[dict]]) -> dict:
    """Group sample instruments by sector and report each one's cumulative return."""
    from collections import defaultdict

    groups: Dict[str, List[dict]] = defaultdict(list)
    for iid, series in sorted(rows_by_id.items()):
        closes = [r["close"] for r in series]
        cum = (closes[-1] / closes[0] - 1.0) * 100 if closes and closes[0] else 0.0
        groups[ds.instrument_industry(iid)].append(
            {"id": iid, "name": ds.instrument_name(iid), "cum_return": round(cum, 2)}
        )
    out = []
    for sector, members in sorted(groups.items()):
        avg = round(sum(m["cum_return"] for m in members) / len(members), 2)
        out.append({"sector": sector, "avg_return": avg, "members": members})
    out.sort(key=lambda g: g["avg_return"], reverse=True)
    return {"sectors": out}


def sector_rotation() -> dict:
    """GICS 11-sector annual total returns — the 'where is the economy going' view."""
    years = ds.sector_years()
    secs = ds.sectors()
    rankings = []
    for yi in range(len(years) - 1, -1, -1):
        row = sorted(
            ({"sector": s, "ret": vals[yi]} for s, vals in secs.items()),
            key=lambda x: x["ret"],
            reverse=True,
        )
        rankings.append({"year": years[yi], "ranked": row})
    latest_sorted = sorted(
        ({"sector": s, "ret": vals[-1]} for s, vals in secs.items()),
        key=lambda x: x["ret"],
        reverse=True,
    )
    return {
        "years": years,
        "sectors": secs,
        "rankings": rankings,
        "latest_year": years[-1],
        "latest_best": latest_sorted[0],
        "latest_worst": latest_sorted[-1],
    }


def normalised_market(bars_path: Path) -> dict:
    """All instruments' close prices normalised to 100 on shared dates."""
    rows_by_id = ds.read_bars(bars_path)
    all_dates = sorted({r["date"] for series in rows_by_id.values() for r in series})
    series_out = []
    for iid, series in sorted(rows_by_id.items()):
        by_date = {r["date"]: r["close"] for r in series}
        base = series[0]["close"]
        values, last = [], None
        for d in all_dates:
            if d in by_date:
                last = round(by_date[d] / base * 100, 2)
            values.append(last)
        series_out.append({"id": iid, "name": ds.instrument_name(iid), "values": values})
    return {"dates": all_dates, "series": series_out}


def run_backtest(
    bars_path: Path,
    instruments_path: Path,
    strategy_name: str,
    fast: int,
    slow: int,
    initial_cash: str,
) -> dict:
    instruments = load_instruments(str(instruments_path))
    instrument_ids = sorted(instruments)

    if strategy_name == "buy-and-hold":
        weight = Decimal("1") / Decimal(len(instrument_ids))
        strategy = BuyAndHoldStrategy({iid: weight for iid in instrument_ids})
    else:
        strategy = MovingAverageCrossStrategy(
            instrument_ids=instrument_ids, fast_window=fast, slow_window=slow
        )

    engine = BacktestEngine(
        market_data=CsvMarketData(str(bars_path)),
        instruments=instruments,
        config=BacktestConfig(initial_cash=Decimal(str(initial_cash))),
    )
    result = engine.run(strategy, instrument_ids)

    return {
        "strategy": strategy.name,
        "instruments": instrument_ids,
        "initial_cash": float(initial_cash),
        "curve": [
            {"date": p["timestamp"].date().isoformat(), "equity": float(p["equity"])}
            for p in result.equity_curve
        ],
        "metrics": {k: float(v) for k, v in result.metrics.items()},
        "positions": {
            iid: {"quantity": float(pos.quantity), "avg_price": float(pos.avg_price)}
            for iid, pos in sorted(result.portfolio.positions.items())
        },
    }
