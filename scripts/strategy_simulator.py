"""Forward strategy regression pool: frozen decisions and raw-close paper fills.

A share-lot daily-close proxy, never a real or intraday order. Each day's target
comes from the previous completed session and is fixed before that day's close.
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
from quant_lab.models import Instrument
from quant_lab.paper_execution import execute_target
from quant_lab.research import get_strategy
from quant_lab.strategy_catalog import STRATEGY_VERSION
from paper_portfolio import checked_weights, code_hash, digest, load_panel, read_json, write_once

ROOT = Path('reports/quant/paper/sma-trend-raw-lot-v3-five-bps-2026-09-29')
META = {
    'stock:000338.SZ': ('000338.SZ', '潍柴动力', '0.000338', 'SZSE'),
    'stock:002475.SZ': ('002475.SZ', '立讯精密', '0.002475', 'SZSE'),
    'stock:002714.SZ': ('002714.SZ', '牧原股份', '0.002714', 'SZSE'),
}
ZONE = ZoneInfo('Asia/Shanghai')


def execution_hash() -> str:
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for name in ('paper_portfolio.py', 'strategy_simulator.py'):
        h.update(name.encode())
        h.update((root / name).read_bytes())
    h.update(b'paper_execution.py')
    h.update((root.parent / 'src' / 'quant_lab' / 'paper_execution.py').read_bytes())
    return h.hexdigest()


def raw_prices(keys: list[str], day: str) -> dict:
    source = EastMoneyDailySource()
    result = {}
    for key in keys:
        symbol, name, provider, exchange = META[key]
        item = Instrument(key, symbol, name, 'stock', 'focus_stock',
                          'eastmoney_kline', provider, exchange, 'none')
        bars = source.fetch(item, date.fromisoformat(day), date.fromisoformat(day))
        if len(bars) != 1 or bars[0].trade_date.isoformat() != day or bars[0].volume <= 0:
            raise ValueError(f'{key}: missing or untradable raw bar on {day}')
        bar = bars[0]
        result[key] = {'close': bar.close, 'volume_lots': bar.volume,
                       'source_id': bar.source_id, 'adjustment': bar.adjustment,
                       'payload_hash': bar.payload_hash,
                       'fetched_at': datetime.now(timezone.utc).isoformat()}
    return result


def records(root: Path) -> list[dict]:
    paths = sorted((root / 'sessions').glob('????-??-??.json'))
    if not paths:
        raise ValueError('no starting session')
    result = []
    previous = None
    for path in paths:
        row = read_json(path)
        if row['date'] != path.stem or row['prior_sha256'] != (digest(previous) if previous else None):
            raise ValueError(f'broken session chain at {path.name}')
        result.append(row)
        previous = row
    return result


def init(args) -> dict:
    root = Path(args.out)
    if root.exists():
        raise ValueError('pool already exists')
    research = read_json(Path(args.research))
    universe = sorted(research['universe'])
    if set(universe) != set(META):
        raise ValueError('this execution adapter only covers frozen three-stock universe')
    if research['strategy_version'] != STRATEGY_VERSION:
        raise ValueError('strategy version mismatch')
    strategy = get_strategy(research['strategy_id'])
    params = {k: v for k, v in vars(strategy).items() if k != 'name'}
    if params != research['strategy_parameters']:
        raise ValueError('strategy parameters mismatch')
    asof = research['asof']
    dates, panel, qfq = load_panel(Path(args.db), universe, asof)
    if dates[-1] != asof:
        raise ValueError('research market date is incomplete')
    target = checked_weights(strategy, panel, universe)
    if {s['instrument_id']: float(s['target_weight']) for s in research['signals']} != target:
        raise ValueError('saved research signals do not match price panel')
    if not math.isfinite(args.cash) or args.cash <= 0:
        raise ValueError('cash must be positive')
    raw = raw_prices(universe, asof)
    config = {'schema_version': 2, 'kind': 'forward_strategy_regression_pool',
              'created_at': datetime.now(timezone.utc).isoformat(),
              'start_asof': asof, 'strategy_id': research['strategy_id'],
              'strategy_version': STRATEGY_VERSION, 'strategy_parameters': params,
              'strategy_code_sha256': code_hash(), 'research_sha256': digest(research),
              'execution_code_sha256': execution_hash(),
              'universe': universe, 'initial_cash': float(args.cash),
              'cost_rate': float(research['cost_rate']), 'buy_lot': 100,
              'signal_price_basis': 'qfq adjusted daily close',
              'fill_price_basis': 'EastMoney unadjusted daily close',
              'execution': 'decision at close t, simulated fill at close t+1',
              'constraints': ['100-share buy lots', 'sell then buy',
                              'cash balance cannot go negative', 'no trade on zero-volume day'],
              'limitations': ['Close proxy is not an attainable intraday fill.',
                              'No bid/ask, slippage, limit-order queue or partial fills.',
                              'No corporate-action cash/dividend reconciliation; pause and review before interpreting split/ex-dividend periods.',
                              'Buy and sell each cost 0.05% of notional; no separate taxes or minimum commission.',
                              'Frozen focus universe was selected with historical knowledge.']}
    empty = {key: 0 for key in universe}
    first = {'date': asof, 'prior_sha256': None, 'signal_asof': asof,
             'pending_targets': target, 'qfq_lineage': qfq, 'raw_bars': raw,
             'signal_panel_sha256': digest(panel),
             'strategy': {'cash': float(args.cash), 'shares': empty,
                          'equity': float(args.cash), 'cost_basis': {k: 0.0 for k in universe}},
             'baseline': {'cash': float(args.cash), 'shares': empty,
                          'equity': float(args.cash)}, 'orders': [], 'baseline_orders': [],
             'status': 'pending_first_future_session'}
    write_once(root / 'config.json', config)
    write_once(root / 'sessions' / f'{asof}.json', first)
    return {'pool': str(root.resolve()), 'start_asof': asof,
            'pending_targets': target, 'observed_sessions': 0}


def advance(args) -> dict:
    root = Path(args.out)
    config = read_json(root / 'config.json')
    if (config['strategy_code_sha256'] != code_hash()
            or config['strategy_version'] != STRATEGY_VERSION
            or config['execution_code_sha256'] != execution_hash()):
        raise ValueError('frozen strategy or execution model changed; start a new pool')
    rows = records(root)
    previous = rows[-1]
    now = datetime.now(ZONE)
    end = args.date or now.date().isoformat()
    if date.fromisoformat(end) > now.date() or (end == now.date().isoformat() and now.time() < time(16, 0)):
        raise ValueError('future or not-yet-final current market date')
    universe = config['universe']
    dates, panel, _ = load_panel(Path(args.db), universe, end)
    if previous['date'] not in dates:
        raise ValueError('prior market date is absent from current panel')
    prior_index = dates.index(previous['date'])
    if digest({key: values[:prior_index + 1] for key, values in panel.items()}) != previous['signal_panel_sha256']:
        raise ValueError('historical signal-price panel changed; review adjustment before extending pool')
    strategy = get_strategy(config['strategy_id'])
    added = 0
    for index in range(dates.index(previous['date']) + 1, len(dates)):
        day = dates[index]
        if day == now.date().isoformat() and now.time() < time(16, 0):
            raise ValueError('current market day not final')
        today_panel = {key: values[:index + 1] for key, values in panel.items()}
        with sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            qfq = {row['instrument_id']: dict(row) for row in con.execute(
                f"SELECT instrument_id,trade_date,close,adjustment,source_id,payload_hash,run_id,fetched_at "
                f"FROM daily_bars WHERE trade_date=? AND adjustment='qfq' "
                f"AND instrument_id IN ({','.join('?' for _ in universe)})", [day, *universe])}
        if set(qfq) != set(universe):
            raise ValueError(f'{day}: incomplete signal-price lineage')
        if day == now.date().isoformat():
            close_cutoff = datetime.combine(now.date(), time(15, 5), ZONE)
            for key, row in qfq.items():
                fetched = datetime.fromisoformat(row['fetched_at'].replace('Z', '+00:00'))
                if fetched.tzinfo is None or fetched.astimezone(ZONE) < close_cutoff:
                    raise ValueError(f'{key}: signal bar fetched before final close')
        raw = raw_prices(universe, day)
        old = previous['strategy']
        pre_equity = old['cash'] + sum(old['shares'][key] * raw[key]['close'] for key in universe)
        strategy_account, orders = execute_target(old, previous['pending_targets'], raw,
                                             config['cost_rate'], buy_lot=config['buy_lot'])
        base = previous['baseline']
        base_target = ({key: 1 / len(universe) for key in universe}
                       if previous['date'] == config['start_asof']
                       else {key: base['shares'][key] * raw[key]['close'] /
                             (base['cash'] + sum(base['shares'][k] * raw[k]['close'] for k in universe))
                             for key in universe})
        baseline_account, baseline_orders = (execute_target(base, base_target, raw, config['cost_rate'], buy_lot=config['buy_lot'], track_cost_basis=False)
                                             if previous['date'] == config['start_asof']
                                             else ({**base, 'equity': base['cash'] + sum(base['shares'][k] * raw[k]['close'] for k in universe)}, []))
        current_target = checked_weights(strategy, today_panel, universe)
        record = {'date': day, 'prior_sha256': digest(previous),
                  'executed_decision_date': previous['date'],
                  'executed_targets': previous['pending_targets'],
                  'pretrade_equity': pre_equity,
                  'strategy': strategy_account, 'baseline': baseline_account,
                  'orders': orders, 'baseline_orders': baseline_orders,
                  'pending_targets': current_target, 'signal_asof': day,
                  'signal_panel_sha256': digest(today_panel),
                  'qfq_lineage': qfq, 'raw_bars': raw, 'status': 'observed'}
        write_once(root / 'sessions' / f'{day}.json', record)
        previous = record
        added += 1
    return {'pool': str(root.resolve()), 'new_sessions': added, 'status': status(args)}


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def status(args) -> dict:
    root = Path(args.out)
    config = read_json(root / 'config.json')
    rows = records(root)
    latest = rows[-1]
    strategy_curve = [r['strategy']['equity'] for r in rows]
    baseline_curve = [r['baseline']['equity'] for r in rows]
    closed = [o for r in rows[1:] for o in r['orders'] if o['side'] == 'sell']
    wins = sum(o['realized_pnl'] > 0 for o in closed)
    session_samples = []
    for before, after in zip(rows[1:-1], rows[2:]):
        for key, shares in before['strategy']['shares'].items():
            if shares > 0:
                session_samples.append(after['raw_bars'][key]['close'] > before['raw_bars'][key]['close'])
    observed = len(rows) - 1
    strategy_return = latest['strategy']['equity'] / config['initial_cash'] - 1
    baseline_return = latest['baseline']['equity'] / config['initial_cash'] - 1
    strategy_dd = max_drawdown(strategy_curve)
    baseline_dd = max_drawdown(baseline_curve)
    return {'pool': str(root.resolve()), 'asof': latest['date'], 'observed_sessions': observed,
            'strategy_equity': latest['strategy']['equity'], 'cash': latest['strategy']['cash'],
            'shares': latest['strategy']['shares'], 'strategy_return': strategy_return,
            'baseline_return': baseline_return, 'strategy_max_drawdown': strategy_dd,
            'baseline_max_drawdown': baseline_dd,
            'simulated_order_count': sum(len(r['orders']) for r in rows),
            'completed_sell_count': len(closed),
            'completed_sell_win_rate': wins / len(closed) if closed else None,
            'held_stock_session_count': len(session_samples),
            'held_stock_positive_session_rate': sum(session_samples) / len(session_samples) if session_samples else None,
            'preliminary_gate_passed': observed >= 126 and strategy_return > baseline_return and strategy_dd >= baseline_dd,
            'pending_targets': latest['pending_targets'],
            'next_step': 'await next completed session, sync qfq bars, then advance' if not observed else 'continue forward observations',
            'scope': 'prospective evidence in frozen three-stock universe; no market-wide validation'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'advance', 'status'))
    parser.add_argument('--db', default='data/quant/market.sqlite3')
    parser.add_argument('--research', default='reports/quant/latest/research.json')
    parser.add_argument('--out', default=str(ROOT))
    parser.add_argument('--cash', type=float, default=100000.0)
    parser.add_argument('--date')
    args = parser.parse_args()
    print(json.dumps({'init': init, 'advance': advance, 'status': status}[args.command](args), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
