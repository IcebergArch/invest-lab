"""Append-only forward paper portfolio for a frozen quant_lab strategy.

This is a weight-based, daily-close proxy. It records future observations only;
it does not place orders or claim an intraday fill.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_lab.research import get_strategy
from quant_lab.strategy_catalog import STRATEGY_VERSION


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def write_once(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def code_hash() -> str:
    root = Path(__file__).resolve().parents[1] / "src" / "quant_lab"
    names = ("strategies.py", "strategy_catalog.py", "factor_library.py")
    h = hashlib.sha256()
    for name in names:
        h.update(name.encode())
        h.update((root / name).read_bytes())
    return h.hexdigest()


def checked_weights(strategy, panel: dict[str, list[float]], universe: list[str]) -> dict[str, float]:
    weights = dict(strategy.weights(panel))
    if set(weights) - set(universe):
        raise ValueError("strategy returned a stock outside the frozen universe")
    if any(not math.isfinite(float(v)) or float(v) < 0 for v in weights.values()):
        raise ValueError("invalid target weight")
    if sum(weights.values()) > 1 + 1e-12:
        raise ValueError("target weights exceed 100%")
    return {key: float(weights.get(key, 0)) for key in universe}


def load_panel(db: Path, universe: list[str], end: str) -> tuple[list[str], dict[str, list[float]], dict[str, dict]]:
    uri = f"file:{db.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"SELECT instrument_id,trade_date,close,adjustment,source_id,payload_hash,run_id,fetched_at "
            f"FROM daily_bars WHERE instrument_id IN ({','.join('?' for _ in universe)}) "
            "AND trade_date<=? AND adjustment='qfq' ORDER BY trade_date,instrument_id",
            [*universe, end],
        ).fetchall()
        calendar = [r[0] for r in con.execute(
            "SELECT trade_date FROM daily_bars WHERE instrument_id='index:000300.SH' "
            "AND trade_date<=? ORDER BY trade_date", (end,)
        )]
    by_date: dict[str, dict[str, sqlite3.Row]] = {}
    for row in rows:
        by_date.setdefault(row["trade_date"], {})[row["instrument_id"]] = row
    if not by_date:
        raise ValueError("no stock bars")
    dates = [day for day in calendar if day >= min(by_date)]
    if not dates:
        raise ValueError("no common market dates")
    panel = {key: [] for key in universe}
    for day in dates:
        if set(by_date[day]) != set(universe):
            raise ValueError(f"incomplete stock bars on {day}; paper run paused")
        for key in universe:
            value = float(by_date[day][key]["close"])
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid close for {key} on {day}")
            panel[key].append(value)
    latest = {key: dict(by_date[dates[-1]][key]) for key in universe}
    return dates, panel, latest


def initialize(args) -> dict:
    root = Path(args.out)
    if root.exists():
        raise ValueError("paper account already exists; use advance or status")
    report = read_json(Path(args.research))
    universe = sorted(report["universe"])
    asof = report["asof"]
    if date.fromisoformat(asof) > date.today():
        raise ValueError("research date is in the future")
    if report["strategy_version"] != STRATEGY_VERSION:
        raise ValueError("research strategy version differs from installed code")
    strategy = get_strategy(report["strategy_id"])
    params = {k: v for k, v in vars(strategy).items() if k != "name"}
    if params != report["strategy_parameters"]:
        raise ValueError("research parameters differ from installed strategy")
    dates, panel, latest = load_panel(Path(args.db), universe, asof)
    if dates[-1] != asof:
        raise ValueError("research date is not the latest complete market date")
    targets = checked_weights(strategy, panel, universe)
    if {s["instrument_id"]: float(s["target_weight"]) for s in report["signals"]} != targets:
        raise ValueError("frozen research signals disagree with current market data")
    config = {
        "schema_version": 1,
        "kind": "forward_paper_portfolio",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "start_asof": asof,
        "strategy_id": report["strategy_id"],
        "strategy_version": report["strategy_version"],
        "strategy_parameters": params,
        "strategy_code_sha256": code_hash(),
        "research_sha256": digest(report),
        "universe": universe,
        "initial_cash": float(args.cash),
        "cost_rate": float(report["cost_rate"]),
        "price_basis": "qfq adjusted daily close",
        "execution": "signal at close t, proxy rebalance at close t+1",
        "limitations": [
            "No real orders or actual account positions.",
            "No intraday fill, price-limit, suspension, split tax, or component fee model.",
            "Same hindsight-selected focus-stock pool as the frozen research report.",
        ],
    }
    if not math.isfinite(config["initial_cash"]) or config["initial_cash"] <= 0:
        raise ValueError("initial cash must be positive")
    first = {
        "date": asof, "prior_record_sha256": None,
        "strategy_equity": config["initial_cash"], "strategy_weights": {k: 0.0 for k in universe},
        "baseline_equity": config["initial_cash"], "baseline_weights": {k: 0.0 for k in universe},
        "pending_targets": targets, "strategy_turnover": 0.0,
        "close": {k: panel[k][-1] for k in universe}, "bar_lineage": latest,
        "status": "pending_first_future_session",
    }
    write_once(root / "config.json", config)
    write_once(root / "sessions" / f"{asof}.json", first)
    return {"account": str(root.resolve()), "start_asof": asof, "pending_targets": targets}


def drift(weights: dict[str, float], growth: dict[str, float], equity: float) -> tuple[dict[str, float], float]:
    factor = 1 - sum(weights.values()) + sum(weights[k] * growth[k] for k in weights)
    if factor <= 0:
        raise ValueError("nonpositive portfolio value")
    return {k: weights[k] * growth[k] / factor for k in weights}, equity * factor


def advance(args) -> dict:
    root = Path(args.out)
    config = read_json(root / "config.json")
    if code_hash() != config["strategy_code_sha256"] or STRATEGY_VERSION != config["strategy_version"]:
        raise ValueError("frozen strategy code changed; existing paper account cannot be silently reinterpreted")
    strategy = get_strategy(config["strategy_id"])
    paths = sorted((root / "sessions").glob("????-??-??.json"))
    if not paths:
        raise ValueError("paper account has no starting session")
    status(args)  # verify the complete append-only hash chain before extending it
    previous = read_json(paths[-1])
    universe = config["universe"]
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    end = args.date or now.date().isoformat()
    if date.fromisoformat(end) > now.date():
        raise ValueError("cannot observe future data")
    if date.fromisoformat(end) == now.date() and now.time() < time(16, 0):
        raise ValueError("current trading day is not final before 16:00 Asia/Shanghai")
    dates, panel, _ = load_panel(Path(args.db), universe, end)
    if previous["date"] not in dates:
        raise ValueError("previous paper date missing from current market calendar")
    start = dates.index(previous["date"])
    written = 0
    for index in range(start + 1, len(dates)):
        day = dates[index]
        today_panel = {k: values[:index + 1] for k, values in panel.items()}
        growth = {k: panel[k][index] / panel[k][index - 1] for k in universe}
        strat_pre, strat_gross = drift(previous["strategy_weights"], growth, previous["strategy_equity"])
        target = previous["pending_targets"]
        turnover = sum(abs(target[k] - strat_pre[k]) for k in universe)
        strat_equity = strat_gross * (1 - turnover * config["cost_rate"])
        base_pre, base_equity = drift(previous["baseline_weights"], growth, previous["baseline_equity"])
        if previous["date"] == config["start_asof"]:
            base_weights = {k: 1 / len(universe) for k in universe}
            base_equity *= 1 - config["cost_rate"]
        else:
            base_weights = base_pre
        with sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            lineage = {r["instrument_id"]: dict(r) for r in con.execute(
                f"SELECT instrument_id,trade_date,close,adjustment,source_id,payload_hash,run_id,fetched_at "
                f"FROM daily_bars WHERE trade_date=? AND instrument_id IN ({','.join('?' for _ in universe)}) "
                "AND adjustment='qfq'", [day, *universe]
            )}
        if day == now.date().isoformat():
            market_close = datetime.combine(now.date(), time(15, 5), ZoneInfo("Asia/Shanghai"))
            for key, row in lineage.items():
                fetched = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
                if fetched.tzinfo is None or fetched.astimezone(ZoneInfo("Asia/Shanghai")) < market_close:
                    raise ValueError(f"{key}: current-day bar was captured before final close")
        record = {
            "date": day, "prior_record_sha256": digest(previous),
            "strategy_equity": strat_equity, "strategy_weights": target,
            "baseline_equity": base_equity, "baseline_weights": base_weights,
            "pending_targets": checked_weights(strategy, today_panel, universe),
            "strategy_turnover": turnover,
            "executed_decision_date": previous["date"],
            "executed_target_weights": target,
            "execution_price_basis": "next trading day qfq adjusted close proxy",
            "execution_cost": strat_gross * turnover * config["cost_rate"],
            "decision_outcomes": {k: {"selected": target[k] > 0,
                                      "next_session_return": growth[k] - 1,
                                      "correct_direction": (growth[k] > 1) if target[k] > 0 else None}
                                  for k in universe},
            "close": {k: panel[k][index] for k in universe},
            "prior_close_in_current_panel": {k: panel[k][index - 1] for k in universe},
            "bar_lineage": lineage, "status": "observed",
        }
        write_once(root / "sessions" / f"{day}.json", record)
        previous = record
        written += 1
    return {"account": str(root.resolve()), "new_sessions": written, "asof": previous["date"],
            "strategy_return": previous["strategy_equity"] / config["initial_cash"] - 1,
            "baseline_return": previous["baseline_equity"] / config["initial_cash"] - 1,
            "status": "pending" if written == 0 and len(paths) == 1 else "observing"}


def status(args) -> dict:
    root = Path(args.out)
    config = read_json(root / "config.json")
    paths = sorted((root / "sessions").glob("????-??-??.json"))
    previous = None
    for path in paths:
        record = read_json(path)
        if previous is not None and record["prior_record_sha256"] != digest(previous):
            raise ValueError(f"broken paper record hash chain at {path.name}")
        previous = record
    if previous is None:
        raise ValueError("paper account is empty")
    observed = len(paths) - 1
    records = [read_json(path) for path in paths]
    selected = [outcome for record in records[1:]
                for outcome in record.get("decision_outcomes", {}).values()
                if outcome["selected"]]
    hits = sum(outcome["correct_direction"] for outcome in selected)
    strategy_curve = [read_json(path)["strategy_equity"] for path in paths]
    baseline_curve = [read_json(path)["baseline_equity"] for path in paths]
    def max_drawdown(curve: list[float]) -> float:
        peak = curve[0]
        worst = 0.0
        for value in curve:
            peak = max(peak, value)
            worst = min(worst, value / peak - 1)
        return worst
    gate = (observed >= 126 and previous["strategy_equity"] > previous["baseline_equity"]
            and max_drawdown(strategy_curve) >= max_drawdown(baseline_curve))
    return {"account": str(root.resolve()), "asof": previous["date"], "observed_sessions": observed,
            "strategy_return": previous["strategy_equity"] / config["initial_cash"] - 1,
            "baseline_return": previous["baseline_equity"] / config["initial_cash"] - 1,
            "strategy_max_drawdown": max_drawdown(strategy_curve),
            "baseline_max_drawdown": max_drawdown(baseline_curve),
            "selected_stock_session_count": len(selected),
            "selected_stock_next_session_hit_rate": hits / len(selected) if selected else None,
            "hit_rate_definition": "positive next-session qfq return among stocks allocated positive target weight at prior close",
            "preliminary_gate_passed": gate,
            "validation_rule": "at least 126 future sessions, higher net return and no worse drawdown than same-pool buy-and-hold",
            "scope": "prospective evidence for this frozen three-stock pool; not a market-wide strategy validation",
            "pending_targets": previous["pending_targets"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "advance", "status"))
    parser.add_argument("--db", default="data/quant/market.sqlite3")
    parser.add_argument("--research", default="reports/quant/latest/research.json")
    parser.add_argument("--out", default="reports/quant/paper/sma-trend-v1-2026-09-29")
    parser.add_argument("--cash", type=float, default=100000)
    parser.add_argument("--date", help="advance through this date; defaults to today")
    args = parser.parse_args()
    result = {"init": initialize, "advance": advance, "status": status}[args.command](args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
