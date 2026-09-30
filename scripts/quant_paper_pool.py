"""Strategy-agnostic forward paper account driven by dated decision records.

A policy adapter generates targets. This runner only persists the decision,
executes prior targets at the next raw daily close, and scores the ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_lab.data_sources import EastMoneyDailySource
from quant_lab.decision_pipeline import decide, sha256_json, validate_policy
from quant_lab.models import Instrument
from quant_lab.paper_execution import execute_target
from paper_portfolio import digest, load_panel, read_json, write_once

ZONE = ZoneInfo('Asia/Shanghai')
DEFAULT_POLICY = Path('policies/ensemble-focus-three-v2-five-bps.json')
DEFAULT_OUT = Path('reports/quant/paper/ensemble-focus-three-v2-five-bps-2026-09-29')


def runtime_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = [root / 'scripts' / 'quant_paper_pool.py', root / 'scripts' / 'paper_portfolio.py']
    paths += [root / 'src' / 'quant_lab' / name for name in
              ('data_sources.py', 'decision_pipeline.py', 'decision_ensemble.py',
               'factor_library.py', 'features.py', 'strategies.py',
               'strategy_catalog.py', 'paper_execution.py')]
    h = hashlib.sha256()
    for path in paths:
        h.update(str(path.relative_to(root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def raw_prices(db: Path, keys: list[str], day: str) -> dict:
    with sqlite3.connect(f'file:{db.resolve()}?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = {r['instrument_id']: dict(r) for r in con.execute(
            f"SELECT instrument_id,symbol,name,provider_code,exchange,source_id FROM instruments "
            f"WHERE instrument_id IN ({','.join('?' for _ in keys)})", keys)}
    if set(rows) != set(keys):
        raise ValueError('market metadata missing for policy universe')
    source = EastMoneyDailySource()
    result = {}
    for key in keys:
        item = rows[key]
        if item['source_id'] != 'eastmoney_kline' or not key.startswith('stock:'):
            raise ValueError(f'{key}: raw-price adapter unavailable')
        instrument = Instrument(key, item['symbol'], item['name'], 'stock', 'paper_pool',
                                'eastmoney_kline', item['provider_code'], item['exchange'], 'none')
        bars = source.fetch(instrument, date.fromisoformat(day), date.fromisoformat(day))
        if len(bars) != 1 or bars[0].trade_date.isoformat() != day or bars[0].volume <= 0:
            raise ValueError(f'{key}: raw close missing or no trading volume on {day}')
        bar = bars[0]
        result[key] = {'close': bar.close, 'volume_lots': bar.volume,
                       'adjustment': bar.adjustment, 'source_id': bar.source_id,
                       'payload_hash': bar.payload_hash,
                       'fetched_at': datetime.now(timezone.utc).isoformat()}
    return result


def sessions(root: Path) -> list[dict]:
    paths = sorted((root / 'sessions').glob('????-??-??.json'))
    if not paths:
        raise ValueError('no opening session')
    result = []
    prior = None
    for path in paths:
        row = read_json(path)
        if row['date'] != path.stem or row['prior_sha256'] != (digest(prior) if prior else None):
            raise ValueError(f'broken paper chain at {path.name}')
        result.append(row)
        prior = row
    return result


def init(args) -> dict:
    root = Path(args.out)
    if root.exists():
        raise ValueError('paper account already exists')
    policy = read_json(Path(args.policy))
    validate_policy(policy)
    asof = policy['start_asof']
    if date.fromisoformat(asof) > datetime.now(ZONE).date():
        raise ValueError('opening date is in the future')
    keys = sorted(policy['universe'])
    dates, panel, qfq = load_panel(Path(args.db), keys, asof)
    if dates[-1] != asof:
        raise ValueError('opening signal market date incomplete')
    decision = decide(policy, asof, panel)
    raw = raw_prices(Path(args.db), keys, asof)
    cash = float(policy['initial_cash'])
    empty = {key: 0 for key in keys}
    config = {'schema_version': 1, 'kind': 'forward_strategy_agnostic_paper_pool',
              'created_at': datetime.now(timezone.utc).isoformat(),
              'policy': policy, 'policy_sha256': sha256_json(policy),
              'runtime_sha256': runtime_hash(), 'start_asof': asof,
              'universe': keys, 'signal_price_basis': 'qfq',
              'fill_price_basis': 'EastMoney unadjusted daily close',
              'fill_assumption': 'decision at close t, proxy fill at close t+1',
              'known_limits': ['No intraday fill or price-limit queue model.',
                               'No full corporate-action and dividend cash reconciliation.',
                               'Fixed aggregate transaction fee, no separate taxes or slippage.',
                               'Focus-stock universe selected with historical knowledge.']}
    record = {'date': asof, 'prior_sha256': None, 'pending_decision': decision,
              'qfq_lineage': qfq, 'raw_bars': raw,
              'strategy': {'cash': cash, 'shares': empty, 'equity': cash,
                           'cost_basis': {key: 0.0 for key in keys}},
              'baseline': {'cash': cash, 'shares': empty, 'equity': cash},
              'stress_strategy': {'cash': cash, 'shares': empty, 'equity': cash,
                                  'cost_basis': {key: 0.0 for key in keys}},
              'stress_baseline': {'cash': cash, 'shares': empty, 'equity': cash},
              'orders': [], 'baseline_orders': [], 'status': 'pending_first_future_session'}
    write_once(root / 'config.json', config)
    write_once(root / 'sessions' / f'{asof}.json', record)
    return {'account': str(root.resolve()), 'policy_id': policy['policy_id'],
            'asof': asof, 'pending_decision_sha256': decision['decision_sha256'],
            'observed_sessions': 0}


def advance(args) -> dict:
    root = Path(args.out)
    config = read_json(root / 'config.json')
    policy = config['policy']
    if config['runtime_sha256'] != runtime_hash() or config['policy_sha256'] != sha256_json(policy):
        raise ValueError('frozen policy or runtime changed; use a new paper account')
    rows = sessions(root)
    previous = rows[-1]
    now = datetime.now(ZONE)
    end = args.date or now.date().isoformat()
    if date.fromisoformat(end) > now.date() or (end == now.date().isoformat() and now.time() < time(16, 0)):
        raise ValueError('future or not-yet-final current market date')
    keys = config['universe']
    dates, panel, _ = load_panel(Path(args.db), keys, end)
    if previous['date'] not in dates:
        raise ValueError('prior market date absent from latest signal panel')
    prior_index = dates.index(previous['date'])
    earlier = {key: values[:prior_index + 1] for key, values in panel.items()}
    if sha256_json(earlier) != previous['pending_decision']['signal_panel_sha256']:
        raise ValueError('historical signal panel was revised; pause for source review')
    added = 0
    for index in range(prior_index + 1, len(dates)):
        day = dates[index]
        today_panel = {key: values[:index + 1] for key, values in panel.items()}
        with sqlite3.connect(f'file:{Path(args.db).resolve()}?mode=ro', uri=True) as con:
            con.row_factory = sqlite3.Row
            lineage = {r['instrument_id']: dict(r) for r in con.execute(
                f"SELECT instrument_id,trade_date,close,adjustment,source_id,payload_hash,run_id,fetched_at "
                f"FROM daily_bars WHERE trade_date=? AND adjustment='qfq' "
                f"AND instrument_id IN ({','.join('?' for _ in keys)})", [day, *keys])}
        if set(lineage) != set(keys):
            raise ValueError(f'{day}: incomplete signal lineage')
        if day == now.date().isoformat():
            cutoff = datetime.combine(now.date(), time(15, 5), ZONE)
            for key, row in lineage.items():
                fetched = datetime.fromisoformat(row['fetched_at'].replace('Z', '+00:00'))
                if fetched.tzinfo is None or fetched.astimezone(ZONE) < cutoff:
                    raise ValueError(f'{key}: current-day qfq bar fetched before final close')
        raw = raw_prices(Path(args.db), keys, day)
        target = previous['pending_decision']['target_weights']
        account, orders = execute_target(previous['strategy'], target, raw,
                                         policy['cost_rate'], buy_lot=policy['buy_lot'])
        stress_account, stress_orders = execute_target(previous['stress_strategy'], target, raw,
            policy['stress_cost_rate'], buy_lot=policy['buy_lot'])
        base = previous['baseline']
        stress_base = previous['stress_baseline']
        if previous['date'] == config['start_asof']:
            baseline_target = {key: 1 / len(keys) for key in keys}
            baseline, baseline_orders = execute_target(base, baseline_target, raw,
                policy['cost_rate'], buy_lot=policy['buy_lot'], track_cost_basis=False)
            stress_baseline, stress_baseline_orders = execute_target(stress_base, baseline_target, raw,
                policy['stress_cost_rate'], buy_lot=policy['buy_lot'], track_cost_basis=False)
        else:
            baseline = {**base, 'equity': base['cash'] + sum(base['shares'][key] * raw[key]['close'] for key in keys)}
            baseline_orders = []
            stress_baseline = {**stress_base, 'equity': stress_base['cash'] + sum(stress_base['shares'][key] * raw[key]['close'] for key in keys)}
            stress_baseline_orders = []
        next_decision = decide(policy, day, today_panel)
        record = {'date': day, 'prior_sha256': digest(previous),
                  'executed_decision_sha256': previous['pending_decision']['decision_sha256'],
                  'executed_decision_asof': previous['date'],
                  'executed_targets': target, 'orders': orders,
                  'baseline_orders': baseline_orders,
                  'stress_orders': stress_orders, 'stress_baseline_orders': stress_baseline_orders,
                  'strategy': account, 'baseline': baseline,
                  'stress_strategy': stress_account, 'stress_baseline': stress_baseline,
                  'pending_decision': next_decision,
                  'qfq_lineage': lineage, 'raw_bars': raw, 'status': 'observed'}
        write_once(root / 'sessions' / f'{day}.json', record)
        previous = record
        added += 1
    return {'account': str(root.resolve()), 'new_sessions': added, 'status': status(args)}


def drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def status(args) -> dict:
    root = Path(args.out)
    config = read_json(root / 'config.json')
    rows = sessions(root)
    policy = config['policy']
    latest = rows[-1]
    initial = float(policy['initial_cash'])
    observed = len(rows) - 1
    strategy_equity = [r['strategy']['equity'] for r in rows]
    baseline_equity = [r['baseline']['equity'] for r in rows]
    stress_equity = [r['stress_strategy']['equity'] for r in rows]
    stress_baseline_equity = [r['stress_baseline']['equity'] for r in rows]
    strategy_return = strategy_equity[-1] / initial - 1
    baseline_return = baseline_equity[-1] / initial - 1
    stress_return = stress_equity[-1] / initial - 1
    stress_baseline_return = stress_baseline_equity[-1] / initial - 1
    strategy_dd = drawdown(strategy_equity)
    baseline_dd = drawdown(baseline_equity)
    sells = [order for row in rows[1:] for order in row['orders'] if order['side'] == 'sell']
    wins = sum(order['realized_pnl'] > 0 for order in sells)
    preliminary = (observed >= policy['validation']['min_forward_sessions']
                   and strategy_return > baseline_return and strategy_dd >= baseline_dd)
    stress_gate = stress_return > stress_baseline_return
    review_candidate = preliminary and stress_gate
    return {'account': str(root.resolve()), 'policy_id': policy['policy_id'],
            'asof': latest['date'], 'observed_sessions': observed,
            'strategy_equity': strategy_equity[-1], 'strategy_return': strategy_return,
            'baseline_return': baseline_return,
            'stress_cost_rate': policy['stress_cost_rate'],
            'stress_strategy_return': stress_return,
            'stress_baseline_return': stress_baseline_return,
            'strategy_max_drawdown': strategy_dd, 'baseline_max_drawdown': baseline_dd,
            'cash': latest['strategy']['cash'], 'shares': latest['strategy']['shares'],
            'simulated_order_count': sum(len(r['orders']) for r in rows),
            'completed_sell_count': len(sells),
            'completed_sell_win_rate': wins / len(sells) if sells else None,
            'preliminary_gate_passed': preliminary,
            'stress_gate_passed': stress_gate if observed else False,
            'decision_sha256': latest['pending_decision']['decision_sha256'],
            'next_targets': latest['pending_decision']['target_weights'],
            'assistance_status': 'human_review_candidate' if review_candidate else 'research_only',
            'reason': ('no independent forward evidence' if observed == 0 else
                       'forward and stress gates have passed; corporate actions and tradability still need review'
                       if review_candidate else 'forward or transaction-cost stress gate not passed')}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'advance', 'status'))
    parser.add_argument('--policy', default=str(DEFAULT_POLICY))
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    parser.add_argument('--db', default='data/quant/market.sqlite3')
    parser.add_argument('--date', help='advance through completed China market date')
    args = parser.parse_args()
    result = {'init': init, 'advance': advance, 'status': status}[args.command](args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
