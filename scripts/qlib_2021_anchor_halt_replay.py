"""Replay pure suspension windows with no fills on halted stock-days.

This is a close-price proxy. Halted stocks keep their supplier-carried prior
close for marking, while target orders are skipped until a tradable day.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from qlib_csi300_entry_eligibility import load_verified_source
from qlib_csi300_monthly_snapshot_stress import signal_day
from qlib_2021_anchor_conditional_returns import load_actions
from qlib_2021_anchor_raw_status_audit import audit as status_audit
from raw_action_aware_hold_stress import (Action, CAPITAL, FEE, LOT, TARGET_GROSS,
                                          _distribution, simulate_hold)
from raw_action_aware_sma_stress import simulate_rebalanced

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-pure-halt-replay-v1'
COMPLETE = 'fully_liquidated_raw_action_proxy'


def simulate_rebalanced_with_halts(
    dates: Sequence[str], panel: Mapping[str, Sequence[float]],
    actions: Mapping[str, Sequence[Action]],
    targets: Mapping[str, Mapping[str, float]],
    trading: Mapping[str, Sequence[bool]], *, entry: int, exit_index: int,
    lot_size: int = LOT, capital: float = CAPITAL, fee: float = FEE,
) -> dict:
    if (entry < 1 or exit_index <= entry or exit_index >= len(dates)
            or len(panel) != 3 or set(panel) != set(actions) or set(panel) != set(trading)
            or any(len(panel[s]) != len(dates) or len(trading[s]) != len(dates)
                   for s in panel)
            or not all(trading[s][entry] and trading[s][exit_index] for s in panel)
            or lot_size < 1 or capital <= 0 or not 0 <= fee < .5):
        raise ValueError('invalid suspension-aware episode contract')
    symbols = sorted(panel)
    on_record: dict[str, list[Action]] = defaultdict(list)
    on_operate: dict[str, list[Action]] = defaultdict(list)
    for symbol in symbols:
        for action in actions[symbol]:
            on_record[action.record_date].append(action)
            on_operate[action.operate_date].append(action)
    shares = {symbol: 0.0 for symbol in symbols}
    pending_shares: list[tuple[str, float, str]] = []
    pending_cash: list[tuple[float, str]] = []
    entitled: dict[tuple[str, str], float] = {}
    cash = peak = capital
    drawdown = fee_total = buy_gross = sell_gross = 0.0
    halted_stock_days = blocked_order_days = order_count = 0
    for index in range(entry, exit_index + 1):
        day = dates[index]
        for item in tuple(pending_shares):
            symbol, quantity, release = item
            if release == day:
                shares[symbol] += quantity
                pending_shares.remove(item)
        for item in tuple(pending_cash):
            amount, release = item
            if release == day:
                cash += amount
                pending_cash.remove(item)
        for action in on_operate.get(day, []):
            base = entitled.pop(action.key, 0.0)
            if base <= 0:
                continue
            if action.cash_per_share:
                amount = base * action.cash_per_share
                if action.pay_date == day:
                    cash += amount
                else:
                    pending_cash.append((amount, action.pay_date))
            if action.bonus_per_share:
                quantity = base * action.bonus_per_share
                if abs(quantity - round(quantity)) > 1e-6:
                    raise ValueError(f'{action.instrument_id} {day}: unknown fractional bonus settlement')
                if action.stock_list_date == day:
                    shares[action.instrument_id] += quantity
                else:
                    pending_shares.append((action.instrument_id, quantity,
                                           action.stock_list_date))
        prices = {symbol: panel[symbol][index] for symbol in symbols}
        if any(not math.isfinite(price) or price <= 0 for price in prices.values()):
            raise ValueError(f'{day}: invalid raw close')

        def pending_quantity(symbol: str) -> float:
            return sum(quantity for key, quantity, _ in pending_shares if key == symbol)

        pre_equity = (cash + sum((shares[s] + pending_quantity(s)) * prices[s]
                                 for s in symbols) + sum(amount for amount, _ in pending_cash))
        if pre_equity <= 0:
            raise ValueError('nonpositive pretrade equity')
        decision_day = dates[index - 1]
        desired = {} if index == exit_index else dict(targets[decision_day])
        if (set(desired) - set(symbols) or any(
                not math.isfinite(weight) or weight < 0 or weight > .35 + 1e-12
                for weight in desired.values())
                or sum(desired.values()) > TARGET_GROSS + 1e-12):
            raise ValueError(f'{decision_day}: target violates frozen budget')
        target_quantity = {
            symbol: math.floor(desired.get(symbol, 0.0) * pre_equity
                               / prices[symbol] / lot_size + 1e-12) * lot_size
            for symbol in symbols}
        for symbol in symbols:
            if not trading[symbol][index]:
                halted_stock_days += 1
                if abs(shares[symbol] + pending_quantity(symbol)
                       - target_quantity[symbol]) > 1e-7:
                    blocked_order_days += 1
                continue
            excess = max(0.0, shares[symbol] + pending_quantity(symbol)
                         - target_quantity[symbol])
            sale = min(shares[symbol], excess)
            if target_quantity[symbol] > 0:
                sale = math.floor(sale / lot_size + 1e-12) * lot_size
            if sale > 1e-10:
                gross = sale * prices[symbol]
                cost = gross * fee
                cash += gross - cost
                shares[symbol] -= sale
                sell_gross += gross
                fee_total += cost
                order_count += 1
        for symbol in symbols:
            if not trading[symbol][index]:
                continue
            shortfall = max(0.0, target_quantity[symbol] - shares[symbol]
                            - pending_quantity(symbol))
            buy = math.floor(shortfall / lot_size + 1e-12) * lot_size
            if buy <= 1e-10:
                continue
            affordable = cash / (prices[symbol] * (1 + fee))
            if buy > affordable + 1e-9:
                buy = math.floor(max(0.0, affordable) / lot_size + 1e-12) * lot_size
            if buy <= 1e-10:
                continue
            gross = buy * prices[symbol]
            cost = gross * fee
            cash -= gross + cost
            if cash < -1e-7:
                raise ValueError('negative cash after buy')
            shares[symbol] += buy
            buy_gross += gross
            fee_total += cost
            order_count += 1
        for action in on_record.get(day, []):
            base = shares[action.instrument_id] + pending_quantity(action.instrument_id)
            if base > 0:
                entitled[action.key] = base
        marked = (cash + sum((shares[s] + pending_quantity(s)) * prices[s]
                             for s in symbols) + sum(amount for amount, _ in pending_cash))
        if cash < -1e-7 or marked <= 0:
            raise ValueError('invalid posttrade cash or equity')
        peak = max(peak, marked)
        drawdown = min(drawdown, marked / peak - 1)
    pending = len(pending_shares) + len(pending_cash) + len(entitled)
    if any(shares.values()) and not pending:
        raise ValueError('planned exit failed to liquidate despite normal trading status')
    result = {'status': ('unsettled_corporate_action_at_horizon' if pending
                         else COMPLETE),
              'net_return': None if pending else cash / capital - 1,
              'max_drawdown': None if pending else drawdown,
              'fee_cny': fee_total, 'buy_gross': buy_gross,
              'sell_gross': sell_gross, 'order_count': order_count,
              'halted_stock_days': halted_stock_days,
              'blocked_order_days': blocked_order_days,
              'pending_claim_count': pending}
    return result


def audit_halted_carry(dates: list[str], raw: dict, trading: dict) -> int:
    checked = 0
    for symbol in raw:
        previous = None
        for day in dates:
            row = raw[symbol].get(day)
            status = trading[symbol].get(day)
            if row is None or status is None or row <= 0:
                raise ValueError(f'{symbol} {day}: missing raw bar/status')
            if status:
                previous = row
            else:
                if previous is None or abs(row - previous) > 1e-8:
                    raise ValueError(f'{symbol} {day}: halted close is not carried prior close')
                checked += 1
    return checked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--db', type=Path, default=Path('data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--snapshot', type=Path, default=Path(
        'reports/quant/snapshots/2026-09-29-baostock-historical-shsz-590b41a3bd0a.json'))
    parser.add_argument('--qlib-root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-pure-halt-replay-v1.json'))
    args = parser.parse_args()
    names = {
        'anchor': 'qlib-2021-anchor-pool-coverage-audit-v1.json',
        'window': 'qlib-2021-anchor-random-window-eligibility-v1.json',
        'raw': 'qlib-2021-anchor-raw-status-audit-v1.json',
        'alignment': 'qlib-2021-anchor-action-alignment-v1.json',
        'conditional': 'qlib-2021-anchor-conditional-raw-returns-v1.json',
    }
    payloads = {key: (args.source_dir / name).read_bytes() for key, name in names.items()}
    reports = {key: json.loads(data) for key, data in payloads.items()}
    if (reports['window']['case_count'] != 6144
            or reports['conditional']['case_count'] != 6144
            or reports['conditional']['source_lineage']['window_report_sha256']
            != hashlib.sha256(payloads['window']).hexdigest()
            or reports['conditional']['script_sha256'] != hashlib.sha256((
                BASE / 'scripts/qlib_2021_anchor_conditional_returns.py').read_bytes()).hexdigest()
            or reports['raw']['raw_archive']['quick_check'] != 'ok'):
        raise ValueError('frozen anchor source contract differs')
    fresh_raw = status_audit(
        args.source_dir / names['anchor'], args.snapshot, args.db,
        args.qlib_root, args.qlib_manifest)
    if (fresh_raw['raw_archive'] != reports['raw']['raw_archive']
            or fresh_raw['classification_totals'] != reports['raw']['classification_totals']
            or fresh_raw['per_symbol'] != reports['raw']['per_symbol']):
        raise ValueError('raw archive differs from frozen stock-day status audit')
    calendar, masks, features, lineage = load_verified_source(
        args.qlib_root, args.qlib_manifest)
    if lineage != reports['anchor']['source_lineage']:
        raise ValueError('Qlib release differs from anchor report')
    symbols = reports['anchor']['selected_symbols_in_hash_order']
    actions = load_actions(args.source_dir,
                           hashlib.sha256(payloads['anchor']).hexdigest(),
                           reports['alignment'], symbols)
    ids = ['stock:' + s[2:] + '.' + s[:2] for s in symbols]
    connection = sqlite3.connect(args.db.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('raw archive integrity check failed')
        placeholders = ','.join('?' for _ in ids)
        rows = connection.execute(f'''
            SELECT b.instrument_id,b.trade_date,b.close,s.tradestatus,s.is_st
            FROM daily_bars b JOIN baostock_daily_status s
              ON s.instrument_id=b.instrument_id AND s.trade_date=b.trade_date
            WHERE b.instrument_id IN ({placeholders})
              AND b.trade_date BETWEEN ? AND ?
            ORDER BY b.instrument_id,b.trade_date
        ''', (*ids, '2021-01-04', '2026-09-28')).fetchall()
    finally:
        connection.close()
    if len(rows) != reports['raw']['raw_archive']['selected_joined_row_count']:
        raise ValueError('raw archive row count differs')
    raw = {identifier: {} for identifier in ids}
    trading = {identifier: {} for identifier in ids}
    st = {identifier: {} for identifier in ids}
    for identifier, day, close, trade, is_st in rows:
        if identifier not in raw or day in raw[identifier] or trade not in (0, 1):
            raise ValueError('unexpected raw stock, duplicate date or trading status')
        raw[identifier][day] = close
        trading[identifier][day] = bool(trade)
        st[identifier][day] = bool(is_st)
    dates = [day.isoformat() for day in calendar]
    date_index = {day: i for i, day in enumerate(dates)}
    targets = {}
    for pool_name, members in reports['window']['pool_symbols'].items():
        targets[pool_name] = {'sma': {}, 'same_exposure_equal': {}}
        for index, day in enumerate(dates):
            if day < '2021-01-04' or day >= '2026-09-28':
                continue
            signal = signal_day(calendar, masks, features, index, seed=202,
                                fixed_symbols=members)
            for policy, source_key in (('sma', 'sma_active_equal'),
                                       ('same_exposure_equal', 'sma_exposure_equal_20')):
                targets[pool_name][policy][day] = {
                    'stock:' + symbol[2:] + '.' + symbol[:2]: weight
                    for symbol, weight in signal['targets'][source_key].items()}
    case_by_id = {case['case_id']: case for case in reports['conditional']['cases']}
    cases = []
    eligible_count = parity_count = checked_halted_days = 0
    for source in reports['window']['cases']:
        pure_halt = (source['has_halt'] and not source['has_supplier_outdate_gap']
                     and not source['has_st'])
        if not pure_halt or not source['entry_all_three_normal_non_st'] \
                or not source['exit_all_three_normal_non_st']:
            continue
        eligible_count += 1
        if case_by_id[source['case_id']]['status'] != 'daily_raw_close_proxy_unavailable':
            raise ValueError('pure-halt case not previously unresolved')
        first = date_index[source['decision_date']]
        last = date_index[source['exit_date']]
        episode = dates[first:last + 1]
        pool_ids = ['stock:' + symbol[2:] + '.' + symbol[:2]
                    for symbol in source['members']]
        panel = {identifier: [raw[identifier].get(day) for day in episode]
                 for identifier in pool_ids}
        mask = {identifier: [trading[identifier].get(day) for day in episode]
                for identifier in pool_ids}
        if any(any(st[identifier].get(day) for day in episode[1:])
               for identifier in pool_ids):
            raise ValueError('pure-halt case contains unflagged ST day')
        checked_halted_days += audit_halted_carry(
            episode[1:], {identifier: raw[identifier] for identifier in pool_ids},
            {identifier: trading[identifier] for identifier in pool_ids})
        for identifier in pool_ids:
            if any(not mask[identifier][i] and any(
                    action_date == episode[i]
                    for action in actions[identifier]
                    for action_date in (action.record_date, action.operate_date,
                                        action.pay_date, action.stock_list_date)
                    if action_date) for i in range(1, len(episode))):
                raise ValueError('corporate action date coincides with halted stock-day')
            if panel[identifier][0] is None:
                panel[identifier][0] = panel[identifier][1]
            if mask[identifier][0] is None:
                mask[identifier][0] = True
        pool_actions = {identifier: actions[identifier] for identifier in pool_ids}
        try:
            output = {
                'passive_fixed_80': simulate_hold(
                    episode, panel, pool_actions, entry=1,
                    exit_index=last - first, lot_size=LOT),
                'sma': simulate_rebalanced_with_halts(
                    episode, panel, pool_actions, targets[source['pool']]['sma'],
                    mask, entry=1, exit_index=last - first),
                'same_exposure_equal': simulate_rebalanced_with_halts(
                    episode, panel, pool_actions,
                    targets[source['pool']]['same_exposure_equal'], mask,
                    entry=1, exit_index=last - first),
            }
        except ValueError as exc:
            if 'fractional bonus settlement' not in str(exc):
                raise
            cases.append({'case_id': source['case_id'], 'segment': source['segment'],
                          'horizon_sessions': source['horizon_sessions'],
                          'seed': source['seed'], 'pool': source['pool'],
                          'status': 'unsupported_fractional_bonus_settlement',
                          'reason': str(exc), 'returns': None})
            continue
        status = ('fully_liquidated_halt_mark_proxy' if all(
            value['status'] == COMPLETE for value in output.values())
                  else 'unsettled_corporate_action_at_horizon')
        cases.append({'case_id': source['case_id'], 'segment': source['segment'],
                      'horizon_sessions': source['horizon_sessions'],
                      'seed': source['seed'], 'pool': source['pool'],
                      'status': status, 'returns': output,
                      'halted_stock_days': source['stock_day_status_counts']['halted'],
                      'sma_blocked_order_days': output['sma']['blocked_order_days'],
                      'equal_blocked_order_days': output['same_exposure_equal']['blocked_order_days']})
    # On fully tradable episodes, the independently implemented order ledger
    # must reproduce the original ledger before halt-window results are used.
    parity_groups = set()
    expected_parity_groups = {
        (case['pool'], case['segment'], case['horizon_sessions'])
        for case in reports['conditional']['cases']
        if case['status'] == 'fully_liquidated_conditional_proxy'}
    for source in reports['window']['cases']:
        previous = case_by_id[source['case_id']]
        parity_key = (source['pool'], source['segment'], source['horizon_sessions'])
        if (previous['status'] != 'fully_liquidated_conditional_proxy'
                or parity_key in parity_groups):
            continue
        first = date_index[source['decision_date']]
        last = date_index[source['exit_date']]
        episode = dates[first:last + 1]
        pool_ids = ['stock:' + symbol[2:] + '.' + symbol[:2]
                    for symbol in source['members']]
        panel = {identifier: [raw[identifier].get(day) for day in episode]
                 for identifier in pool_ids}
        mask = {identifier: [True] * len(episode) for identifier in pool_ids}
        for identifier in pool_ids:
            if panel[identifier][0] is None:
                panel[identifier][0] = panel[identifier][1]
        for policy in ('sma', 'same_exposure_equal'):
            new = simulate_rebalanced_with_halts(
                episode, panel, {identifier: actions[identifier] for identifier in pool_ids},
                targets[source['pool']][policy], mask, entry=1, exit_index=last - first)
            old = simulate_rebalanced(
                episode, panel, {identifier: actions[identifier] for identifier in pool_ids},
                targets[source['pool']][policy], entry=1, exit_index=last - first,
                lot_size=LOT)
            if any(abs(new[field] - old[field]) > 1e-9
                   for field in ('net_return', 'max_drawdown', 'fee_cny')):
                raise ValueError('all-tradable new/old ledger parity failed')
        parity_count += 1
        parity_groups.add(parity_key)
    if parity_groups != expected_parity_groups:
        raise ValueError('missing a priceable pool/period/horizon parity audit')
    counts = Counter(case['status'] for case in cases)
    report = {
        'study_version': VERSION,
        'status': 'retrospective_halt_mark_proxy_not_stable_baseline',
        'source_lineage': {key + '_report_sha256': hashlib.sha256(data).hexdigest()
                           for key, data in payloads.items()},
        'eligible_pure_halt_cases': eligible_count,
        'all_tradable_ledger_parity_cases': parity_count,
        'halted_stock_days_checked_with_carried_close': checked_halted_days,
        'result_status_counts': dict(counts),
        'cases': cases,
        'limitations': [
            'A supplier-carried prior close is only a stale mark during suspension, not an executable price.',
            'Orders for halted names are not filled; the next eligible day uses the most recent available target.',
            'ST cases, entry/exit suspensions, post-outDate gaps and fractional bonus settlement remain unresolved.',
            'Price-limit queues, volume constraints, slip, taxes, PIT constituents and prospective validation remain open.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'eligible': eligible_count, 'statuses': dict(counts)},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
