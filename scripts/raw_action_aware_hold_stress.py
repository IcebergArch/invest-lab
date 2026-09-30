"""Replay the frozen random-entry fixed-80% hold comparator on raw closes.

This research ledger tracks dividends and bonus share delivery. It retains all
sampled windows, including any that cannot be fully liquidated at the horizon.
Historical point-in-time universe, limit queues and taxes are not established.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

BASE = Path(__file__).resolve().parents[1]
VERSION = 'raw-action-aware-36-pool-hold-stress-v1'
RUN_ID = 'a80b4ab1cd8145b8bb59acb565c239d2'
FEE = .0005
CAPITAL = 100_000.0
TARGET_GROSS = .8
LOT = 100
YEARS = (2021, 2022, 2023)


@dataclass(frozen=True)
class Action:
    instrument_id: str
    record_date: str
    operate_date: str
    pay_date: str | None
    stock_list_date: str | None
    cash_per_share: float
    bonus_per_share: float

    @property
    def key(self) -> tuple[str, str]:
        return (self.instrument_id, self.operate_date)


def parse_action(instrument_id: str, row: Mapping[str, str]) -> Action:
    cash = float(row['dividCashPsBeforeTax'] or 0)
    bonus = float(row['dividStocksPs'] or 0) + float(row['dividReserveToStockPs'] or 0)
    record, operate = row['dividRegistDate'], row['dividOperateDate']
    pay = row['dividPayDate'] or None
    listing = row['dividStockMarketDate'] or None
    if (not record or not operate or record >= operate
            or any(not math.isfinite(x) or x < 0 for x in (cash, bonus))
            or (cash > 0 and (not pay or pay < operate))
            or (bonus > 0 and (not listing or listing < operate))):
        raise ValueError(f'{instrument_id}: malformed corporate action')
    return Action(instrument_id, record, operate, pay, listing, cash, bonus)


def load_inputs(stress_path: Path, snapshots: Sequence[Path], db_path: Path,
                alignment_path: Path) -> tuple[dict, list[str], dict[str, list[float]],
                                               dict[str, list[Action]], dict]:
    stress_bytes = stress_path.read_bytes()
    stress = json.loads(stress_bytes)
    if stress.get('study_version') != 'qlib-deterministic-pool-stress-v2':
        raise ValueError('unexpected frozen stress report')
    if (stress.get('cost_rate_per_side') != FEE
            or stress.get('budget') != {'max_gross_weight': TARGET_GROSS, 'max_stock_weight': .35}
            or stress.get('sample_count_per_segment_horizon_seed') != 64):
        raise ValueError('frozen fee, budget or sample count differs from raw replay')
    pools = stress['pool_symbols']
    if len(pools) != 12 or any(len(pool) != 3 for pool in pools.values()):
        raise ValueError('expected twelve frozen three-stock pools')
    ids = sorted({symbol for pool in pools.values() for symbol in pool})
    if len(ids) != 36:
        raise ValueError('expected 36 distinct frozen symbols')
    align_bytes = alignment_path.read_bytes()
    alignment = json.loads(align_bytes)
    price_bytes = (alignment_path.parent / 'qlib-raw-price-factor-audit-v1.json').read_bytes()
    price_audit = json.loads(price_bytes)
    if (alignment.get('study_version') != 'pool-corporate-action-alignment-v1'
            or alignment['price_audit']['sha256'] != hashlib.sha256(price_bytes).hexdigest()
            or price_audit['frozen_stress_report']['sha256'] != hashlib.sha256(stress_bytes).hexdigest()
            or price_audit['source_lineage']['raw_archive_run_id'] != RUN_ID
            or alignment['same_day_supplier_action_match_count'] != 60):
        raise ValueError('corporate action alignment is incomplete or stale')
    actions: dict[str, list[Action]] = {identifier: [] for identifier in ids}
    snap_sources = []
    if len(snapshots) != 3:
        raise ValueError('expected three action snapshots')
    for year, path in zip(YEARS, snapshots):
        payload = path.read_bytes()
        snap = json.loads(payload)
        expected = next((item for item in alignment['supplier_snapshots']
                         if item['operate_year'] == year), None)
        if (expected is None or expected['sha256'] != hashlib.sha256(payload).hexdigest()
                or snap.get('operate_year') != year or snap.get('requested_stock_count') != 36):
            raise ValueError(f'{year}: corporate action snapshot differs from aligned source')
        if {row['instrument_id'] for row in snap['responses']} != set(ids):
            raise ValueError(f'{year}: action stock set differs from frozen pool')
        for response in snap['responses']:
            identifier = response['instrument_id']
            if response['row_count'] != len(response['rows']):
                raise ValueError('action response count mismatch')
            for item in response['rows']:
                actions[identifier].append(parse_action(identifier, item))
        snap_sources.append({'year': year, 'path': str(path.resolve().relative_to(BASE)),
                             'sha256': hashlib.sha256(payload).hexdigest()})
    connection = sqlite3.connect(db_path.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('archive integrity check failed')
        rows = connection.execute('''
            SELECT b.instrument_id,b.trade_date,b.close,b.source_id,b.adjustment,
                   s.run_id,s.tradestatus,s.is_st
            FROM daily_bars b LEFT JOIN baostock_daily_status s
              ON s.instrument_id=b.instrument_id AND s.trade_date=b.trade_date
            WHERE b.run_id=? ORDER BY b.instrument_id,b.trade_date
        ''', (RUN_ID,)).fetchall()
    finally:
        connection.close()
    raw: dict[str, dict[str, float]] = {identifier: {} for identifier in ids}
    for identifier, day, close, source, basis, status_run, trade, st in rows:
        if (identifier not in raw or source != 'baostock_daily' or basis != 'none'
                or status_run != RUN_ID or trade != 1 or st != 0
                or not math.isfinite(close) or close <= 0 or day in raw[identifier]):
            raise ValueError('raw bar source, state or price invalid')
        raw[identifier][day] = float(close)
    dates = sorted(raw[ids[0]])
    if len(dates) != 727 or dates[0] != '2021-01-04' or dates[-1] != '2023-12-29':
        raise ValueError('unexpected raw trading calendar')
    if any(sorted(raw[identifier]) != dates for identifier in ids):
        raise ValueError('raw stock date coverage differs')
    for identifier, stock_actions in actions.items():
        seen = set()
        for item in stock_actions:
            if (item.key in seen or item.operate_date not in dates
                    or item.record_date not in dates
                    or (item.pay_date and item.pay_date not in dates)
                    or (item.stock_list_date and item.stock_list_date not in dates)):
                raise ValueError(f'{identifier}: action timing outside raw calendar')
            seen.add(item.key)
    panel = {identifier: [raw[identifier][day] for day in dates] for identifier in ids}
    lineage = {'stress_report_sha256': hashlib.sha256(stress_bytes).hexdigest(),
               'alignment_report_sha256': hashlib.sha256(align_bytes).hexdigest(),
               'action_snapshots': snap_sources,
               'raw_archive_run_id': RUN_ID,
               'raw_archive_path': str(db_path.resolve().relative_to(BASE))}
    return stress, dates, panel, actions, lineage


def simulate_hold(dates: Sequence[str], panel: Mapping[str, Sequence[float]],
                  actions: Mapping[str, Sequence[Action]], *, entry: int, exit_index: int,
                  lot_size: int | None, capital: float = CAPITAL,
                  fee: float = FEE) -> dict:
    """Buy 80% equally at entry close; track entitled cash/shares; sell at exit close."""
    if (entry < 0 or exit_index <= entry or exit_index >= len(dates)
            or len(panel) != 3 or set(panel) != set(actions)
            or any(len(values) != len(dates) for values in panel.values())
            or not 0 <= fee < .5 or capital <= 0
            or (lot_size is not None and lot_size < 1)):
        raise ValueError('invalid hold episode contract')
    symbols = sorted(panel)
    stock_events = [item for symbol in symbols for item in actions[symbol]]
    on_record: dict[str, list[Action]] = {}
    on_operate: dict[str, list[Action]] = {}
    for item in stock_events:
        on_record.setdefault(item.record_date, []).append(item)
        on_operate.setdefault(item.operate_date, []).append(item)
    tradable = {symbol: 0.0 for symbol in symbols}
    pending_shares: list[tuple[str, float, str]] = []
    pending_cash: list[tuple[float, str]] = []
    entitled: dict[tuple[str, str], float] = {}
    cash = capital
    total_buy = total_sell = total_fee = cash_dividends = bonus_shares = 0.0
    peak, max_drawdown = capital, 0.0
    eligible_action_count = 0
    initial_fills = []
    for index in range(entry, exit_index + 1):
        day = dates[index]
        for symbol, quantity, release in tuple(pending_shares):
            if release == day:
                tradable[symbol] += quantity
                pending_shares.remove((symbol, quantity, release))
        for amount, release in tuple(pending_cash):
            if release == day:
                cash += amount
                pending_cash.remove((amount, release))
        for item in on_operate.get(day, []):
            base = entitled.pop(item.key, 0.0)
            if base <= 0:
                continue
            eligible_action_count += 1
            if item.cash_per_share:
                dividend = base * item.cash_per_share
                cash_dividends += dividend
                if item.pay_date == day:
                    cash += dividend
                else:
                    pending_cash.append((dividend, item.pay_date))
            if item.bonus_per_share:
                extra = base * item.bonus_per_share
                bonus_shares += extra
                if lot_size is not None and abs(extra - round(extra)) > 1e-6:
                    raise ValueError(f'{item.instrument_id} {day}: fractional bonus settlement unknown')
                if item.stock_list_date == day:
                    tradable[item.instrument_id] += extra
                else:
                    pending_shares.append((item.instrument_id, extra, item.stock_list_date))
        if index == entry:
            for symbol in symbols:
                price = panel[symbol][index]
                if not math.isfinite(price) or price <= 0:
                    raise ValueError(f'{symbol} {day}: invalid entry close')
                desired = capital * TARGET_GROSS / len(symbols) / price
                quantity = (desired if lot_size is None else
                            math.floor(desired / lot_size) * lot_size)
                gross = quantity * price
                cost = gross * fee
                if cost > cash - gross + 1e-8:
                    raise ValueError('buy fees exceed cash sleeve')
                tradable[symbol] += quantity
                cash -= gross + cost
                total_buy += gross
                total_fee += cost
                initial_fills.append({'instrument_id': symbol, 'shares': quantity,
                                      'raw_close': price, 'gross': gross, 'fee': cost})
        if index == exit_index:
            for symbol in symbols:
                price = panel[symbol][index]
                quantity = tradable[symbol]
                gross = quantity * price
                cost = gross * fee
                cash += gross - cost
                total_sell += gross
                total_fee += cost
                tradable[symbol] = 0.0
        for item in on_record.get(day, []):
            base = tradable[item.instrument_id] + sum(
                quantity for symbol, quantity, _ in pending_shares
                if symbol == item.instrument_id)
            if base > 0:
                entitled[item.key] = base
        marked = cash + sum(
            (tradable[symbol] + sum(quantity for pending_symbol, quantity, _ in pending_shares
                                    if pending_symbol == symbol)) * panel[symbol][index]
            for symbol in symbols) + sum(amount for amount, _ in pending_cash)
        peak = max(peak, marked)
        max_drawdown = min(max_drawdown, marked / peak - 1)
    pending = len(pending_shares) + len(pending_cash) + len(entitled)
    if pending:
        return {'status': 'unsettled_corporate_action_at_horizon', 'net_return': None,
                'marked_return_at_horizon': None if entitled else marked / capital - 1,
                'pending_claim_count': pending, 'initial_fills': initial_fills,
                'eligible_action_count': eligible_action_count}
    return {'status': 'fully_liquidated_raw_action_proxy',
            'net_return': cash / capital - 1, 'max_drawdown': max_drawdown,
            'initial_gross_weight': total_buy / capital,
            'cash_dividend_before_tax': cash_dividends,
            'bonus_shares_credited': bonus_shares,
            'buy_gross': total_buy, 'sell_gross': total_sell,
            'total_fee': total_fee, 'pending_claim_count': 0,
            'initial_fills': initial_fills,
            'eligible_action_count': eligible_action_count}


def _distribution(values: Sequence[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    def at(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        left = math.floor(position)
        weight = position - left
        return ordered[left] * (1 - weight) + ordered[min(left + 1, len(ordered) - 1)] * weight
    return {'median': median(ordered), 'p10': at(.1), 'p90': at(.9),
            'min': ordered[0], 'max': ordered[-1]}


def evaluate(stress: dict, dates: Sequence[str], panel: Mapping[str, Sequence[float]],
             actions: Mapping[str, Sequence[Action]]) -> dict:
    date_index = {day: index for index, day in enumerate(dates)}
    cases = []
    seen_case_ids = set()
    for horizon in (126, 252):
        for seed in (202, 404):
            frozen = stress['cases']['early'][str(horizon)][str(seed)]['pools']
            for pool_name, record in frozen.items():
                symbols = record['symbols']
                pool_panel = {symbol: panel[symbol] for symbol in symbols}
                pool_actions = {symbol: actions[symbol] for symbol in symbols}
                if record['episodes'] != 64 or len(record['cases']) != 64:
                    raise ValueError('frozen pool episode count changed')
                for case in record['cases']:
                    decision, final = case['entry'], case['exit']
                    if decision not in date_index or final not in date_index:
                        raise ValueError('frozen case outside raw history')
                    start, end = date_index[decision], date_index[final]
                    if end - start != horizon:
                        raise ValueError('raw calendar differs from frozen horizon')
                    fractional = simulate_hold(dates, pool_panel, pool_actions,
                                               entry=start + 1, exit_index=end,
                                               lot_size=None)
                    lots = simulate_hold(dates, pool_panel, pool_actions,
                                         entry=start + 1, exit_index=end,
                                         lot_size=LOT)
                    case_id = hashlib.sha256(
                        f'{VERSION}|early|{horizon}|{seed}|{pool_name}|{decision}'.encode()
                    ).hexdigest()[:20]
                    if case_id in seen_case_ids:
                        raise ValueError('duplicate frozen case ID')
                    seen_case_ids.add(case_id)
                    cases.append({'case_id': case_id,
                                  'segment': 'early', 'horizon_sessions': horizon,
                                  'seed': seed, 'pool': pool_name,
                                  'decision_date': decision, 'first_fill_date': dates[start + 1],
                                  'scheduled_exit_date': final,
                                  'frozen_adjusted_close_static_80_return': case['static_80_return'],
                                  'fractional_raw_action_hold': fractional,
                                  'lot_100_raw_action_hold': lots})
    if len(cases) != 12 * 64 * 2 * 2:
        raise ValueError('frozen case denominator changed')
    summaries = {}
    for horizon in (126, 252):
        for seed in (202, 404):
            selected = [row for row in cases if row['horizon_sessions'] == horizon
                        and row['seed'] == seed]
            complete = [row for row in selected if all(
                row[key]['status'] == 'fully_liquidated_raw_action_proxy'
                for key in ('fractional_raw_action_hold', 'lot_100_raw_action_hold'))]
            summaries[f'{horizon}:{seed}'] = {
                'sample_count': len(selected),
                'jointly_liquidated_count': len(complete),
                'fractional_minus_adjusted_proxy': _distribution([
                    row['fractional_raw_action_hold']['net_return']
                    - row['frozen_adjusted_close_static_80_return'] for row in complete]),
                'lot_100_minus_fractional': _distribution([
                    row['lot_100_raw_action_hold']['net_return']
                    - row['fractional_raw_action_hold']['net_return'] for row in complete]),
                'lot_100_raw_action_hold_return': _distribution([
                    row['lot_100_raw_action_hold']['net_return'] for row in complete]),
                'frozen_adjusted_proxy_return_on_joint_subset': _distribution([
                    row['frozen_adjusted_close_static_80_return'] for row in complete]),
            }
    complete_all = [row for row in cases if row['fractional_raw_action_hold']['net_return'] is not None
                    and row['lot_100_raw_action_hold']['net_return'] is not None]
    no_entitled = [row for row in complete_all
                   if row['fractional_raw_action_hold']['eligible_action_count'] == 0]
    with_entitled = [row for row in complete_all
                     if row['fractional_raw_action_hold']['eligible_action_count'] > 0]
    def fractional_excess(row: dict) -> float:
        return (row['fractional_raw_action_hold']['net_return']
                - row['frozen_adjusted_close_static_80_return'])
    return {'case_count': len(cases), 'cases': cases, 'summaries': summaries,
            'unsettled_fractional_count': sum(row['fractional_raw_action_hold']['net_return'] is None
                                               for row in cases),
            'unsettled_lot_count': sum(row['lot_100_raw_action_hold']['net_return'] is None
                                      for row in cases),
            'entitled_action_case_count': len(with_entitled),
            'no_entitled_action_case_count': len(no_entitled),
            'fractional_minus_adjusted_proxy_on_no_entitlement_cases':
                _distribution([fractional_excess(row) for row in no_entitled]),
            'fractional_minus_adjusted_proxy_on_action_cases':
                _distribution([fractional_excess(row) for row in with_entitled]),
            'lot_initial_gross_weight': _distribution([
                row['lot_100_raw_action_hold']['initial_gross_weight'] for row in complete_all]),
            'lot_cases_with_zero_initial_name': sum(any(
                fill['shares'] == 0 for fill in row['lot_100_raw_action_hold']['initial_fills'])
                for row in complete_all)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stress-report', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-deterministic-pool-stress-v2.json'))
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--db', type=Path, default=Path('data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/raw-action-aware-36-pool-hold-stress-v1.json'))
    args = parser.parse_args()
    snapshots = [args.source_dir / f'baostock-36-pool-corporate-actions-{year}-v1.json'
                 for year in YEARS]
    stress, dates, panel, actions, lineage = load_inputs(
        args.stress_report, snapshots, args.db,
        args.source_dir / 'pool-corporate-action-alignment-v1.json')
    result = evaluate(stress, dates, panel, actions)
    report = {'study_version': VERSION,
              'status': 'retrospective_raw_action_aware_hold_stress_not_stable_baseline',
              'capital_cny': CAPITAL, 'buy_fee_per_side': FEE, 'sell_fee_per_side': FEE,
              'initial_stock_target_gross': TARGET_GROSS,
              'lot_size_buy_shares': LOT,
              'price_basis': 'BaoStock unadjusted close for buy/sell/mark',
              'historical_point_in_time_universe_confirmed': False,
              'actual_exchange_execution_confirmed': False,
              'actions': 'BaoStock later snapshot; gross cash dividends, stock bonus on listed date; receivables marked but not tradable',
              'entry_rule': 'frozen SMA-conditional random decision date t; buy at t+1 raw close',
              'exit_rule': 'sell settled shares at frozen horizon close; retain unliquidated cases',
              'source_lineage': lineage,
              **result,
              'limitations': [
                  'Only the frozen 2021–2023 survivor-conditioned three-stock pools are covered; no point-in-time universe or independent later-period validation.',
                  'Supplier action rows were fetched in 2026; historical publication timing is not verified.',
                  'Corporate-action rights, taxes, odd-lot settlement, price-limit queues and intraday fillability are not fully modeled.',
                  'Cash dividends use gross pre-tax amounts; the Qlib adjusted-close proxy may imply a different reinvestment convention.',
                  'Small proxy differences can remain even without an entitled action because raw prices are cent-rounded while Qlib factor/adjusted close vary slightly.',
                  'This evaluates the passive fixed-80% comparator on original sampled dates, not the daily SMA trading strategy or a stable portfolio baseline.',
              ],
              'generated_at': datetime.now(timezone.utc).isoformat(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()), 'cases': result['case_count'],
                      'unsettled_fractional': result['unsettled_fractional_count'],
                      'unsettled_lot': result['unsettled_lot_count'],
                      'status': report['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
