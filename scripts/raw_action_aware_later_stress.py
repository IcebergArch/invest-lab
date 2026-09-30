"""Reconcile and replay the frozen later 12-pool cases on raw action-aware prices.

This is a retrospective rejection check. Stock selection uses 2026 hindsight.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from quant_lab.qlib_local import read_stock
from qlib_cross_pool_audit import sma_targets
from qlib_deterministic_pool_stress_v2 import fingerprint, load_pools
from random_entry_baseline import exposure_matched_targets
from raw_action_aware_hold_stress import (CAPITAL, FEE, LOT, TARGET_GROSS,
                                          _distribution, parse_action, simulate_hold)
from raw_action_aware_sma_stress import simulate_rebalanced

BASE = Path(__file__).resolve().parents[1]
VERSION = 'raw-action-aware-36-pool-later-stress-v1'
FIRST, LAST = '2024-01-02', '2026-09-28'
YEARS = (2024, 2025, 2026)


def load_sources(stress_path: Path, source_dir: Path, db_path: Path,
                 qlib_root: Path, qlib_manifest: Path) -> tuple:
    stress_bytes = stress_path.read_bytes()
    stress = json.loads(stress_bytes)
    if (stress.get('study_version') != 'qlib-deterministic-pool-stress-v2'
            or stress.get('cost_rate_per_side') != FEE
            or stress.get('budget') != {'max_gross_weight': TARGET_GROSS,
                                        'max_stock_weight': .35}
            or stress.get('sample_count_per_segment_horizon_seed') != 64):
        raise ValueError('frozen report contract differs')
    for relative, expected in stress['code_file_sha256'].items():
        if hashlib.sha256((BASE / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f'frozen code changed: {relative}')
    pools = stress['pool_symbols']
    ids = sorted({symbol for pool in pools.values() for symbol in pool})
    if len(pools) != 12 or len(ids) != 36 or any(len(pool) != 3 for pool in pools.values()):
        raise ValueError('frozen pool dimensions changed')
    qdates, qpools, qlineage = load_pools(qlib_root, qlib_manifest)
    if (qlineage != stress['source_lineage']
            or fingerprint({'dates': qdates, 'pools': qpools}) != stress['panel_sha256']
            or set(qpools) != set(pools)):
        raise ValueError('frozen Qlib panel differs')
    for name, panel in qpools.items():
        if sorted(panel) != pools[name]:
            raise ValueError(f'{name}: pool membership differs')

    actions = {symbol: [] for symbol in ids}
    snapshots = []
    seen_action = set()
    snapshot_script_sha = hashlib.sha256((
        BASE / 'scripts/baostock_pool_corporate_action_snapshot_later.py'
    ).read_bytes()).hexdigest()
    for year in YEARS:
        path = source_dir / f'baostock-36-pool-corporate-actions-{year}-later-v2.json'
        payload = path.read_bytes()
        snap = json.loads(payload)
        if (snap.get('snapshot_version') != 'baostock-36-pool-corporate-actions-later-v2'
                or snap.get('status') != 'provider_response_captured_not_pit'
                or snap.get('operate_year') != year or snap.get('year_type') != 'operate'
                or snap.get('requested_stock_count') != 36
                or snap.get('response_row_count') != sum(r['row_count'] for r in snap['responses'])
                or snap.get('script_sha256') != snapshot_script_sha
                or snap.get('historical_point_in_time_publication_verified') is not False
                or snap['source']['stress_report_sha256'] != hashlib.sha256(stress_bytes).hexdigest()
                or len(snap['responses']) != 36
                or {r['instrument_id'] for r in snap['responses']} != set(ids)):
            raise ValueError(f'{year}: supplier snapshot contract differs')
        for response in snap['responses']:
            if response['row_count'] != len(response['rows']):
                raise ValueError('supplier row count differs')
            for row in response['rows']:
                action = parse_action(response['instrument_id'], row)
                if not action.operate_date.startswith(str(year)) or action.key in seen_action:
                    raise ValueError('supplier action year or uniqueness differs')
                seen_action.add(action.key)
                actions[action.instrument_id].append(action)
        snapshots.append({'year': year, 'path': str(path.resolve().relative_to(BASE)),
                          'sha256': hashlib.sha256(payload).hexdigest(),
                          'row_count': snap['response_row_count'],
                          'collected_at': snap['collected_at']})

    connection = sqlite3.connect(db_path.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('raw archive failed quick_check')
        covered = connection.execute('''
            SELECT instrument_id,run_id,window_start,window_end,status,bar_count,status_count
            FROM backfill_windows WHERE source_id='baostock_daily' AND adjustment='none'
              AND window_start<=? AND window_end>=? AND status='success'
        ''', (FIRST, LAST)).fetchall()
        windows = {symbol: [] for symbol in ids}
        for row in covered:
            if row[0] in windows:
                if row[5] < 664 or row[5] != row[6]:
                    raise ValueError(f'{row[0]}: raw covering window counts differ')
                windows[row[0]].append(row)
        if any(not windows[symbol] for symbol in ids):
            raise ValueError('raw archive lacks a successful covering window')
        placeholders = ','.join('?' for _ in ids)
        rows = connection.execute(f'''
            SELECT b.instrument_id,b.trade_date,b.close,b.source_id,b.adjustment,
                   b.run_id,b.payload_hash,s.tradestatus,s.is_st,s.run_id,
                   s.raw_amount_cny,s.has_valid_bar,s.payload_hash
            FROM daily_bars b LEFT JOIN baostock_daily_status s
              ON s.instrument_id=b.instrument_id AND s.trade_date=b.trade_date
            WHERE b.instrument_id IN ({placeholders}) AND b.trade_date BETWEEN ? AND ?
            ORDER BY b.instrument_id,b.trade_date
        ''', (*ids, FIRST, LAST)).fetchall()
    finally:
        connection.close()
    raw = {symbol: {} for symbol in ids}
    rows_hash = hashlib.sha256()
    run_ids = {symbol: set() for symbol in ids}
    for row in rows:
        (symbol, day, close, source, adjustment, bar_run, bar_hash, trade, st,
         status_run, amount, valid, status_hash) = row
        if (source != 'baostock_daily' or adjustment != 'none'
                or trade != 1 or st != 0 or valid != 1
                or not isinstance(amount, (int, float)) or amount <= 0
                or not math.isfinite(close) or close <= 0 or not bar_hash or not status_hash
                or bar_run != status_run or day in raw[symbol]
                or not any(bar_run == w[1] for w in windows[symbol])):
            raise ValueError(f'{symbol} {day}: raw bar/status invalid')
        raw[symbol][day] = float(close)
        run_ids[symbol].add(bar_run)
        rows_hash.update(json.dumps(row, separators=(',', ':'), ensure_ascii=False).encode())
        rows_hash.update(b'\n')
    dates = sorted(raw[ids[0]])
    if (len(dates) != 664 or dates[0] != FIRST or dates[-1] != LAST
            or any(sorted(raw[symbol]) != dates for symbol in ids)
            or [day for day in qdates if FIRST <= day <= LAST] != dates):
        raise ValueError('raw or Qlib trading calendar differs')
    for symbol, records in actions.items():
        for item in records:
            if (item.record_date not in raw[symbol] or item.operate_date not in raw[symbol]
                    or (item.pay_date and item.pay_date not in raw[symbol])
                    or (item.stock_list_date and item.stock_list_date not in raw[symbol])):
                raise ValueError(f'{symbol}: action outside raw calendar')

    errors = []
    jumps = []
    unmatched = []
    action_keys = {item.key for group in actions.values() for item in group}
    for symbol in ids:
        qsymbol = ('SH' if symbol.endswith('.SH') else 'SZ') + symbol[6:12]
        response = read_stock(qlib_root, qlib_manifest, '2026-09-28', qsymbol,
                              start=FIRST, end=LAST, fields=('close', 'factor'), limit=1000)
        qrows = response['rows']
        if (response['adjustment'] != 'qlib_adjusted' or response['missing_fields']
                or response['truncated_to_latest'] or [r['date'] for r in qrows] != dates):
            raise ValueError(f'{symbol}: Qlib factor coverage differs')
        previous = None
        for item in qrows:
            day = item['date']
            price, factor = item['close'], item['factor']
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
                   for v in (price, factor)):
                raise ValueError(f'{symbol} {day}: Qlib price/factor invalid')
            raw_close = raw[symbol][day]
            errors.append(abs(price / (raw_close * factor) - 1))
            if previous:
                prior_raw, prior_price, prior_factor = previous
                ratio = (price / prior_price) / (raw_close / prior_raw)
                factor_ratio = factor / prior_factor
                if abs(ratio / factor_ratio - 1) > 1e-6:
                    raise ValueError(f'{symbol} {day}: factor does not explain return gap')
                if abs(ratio - 1) > .01:
                    event = {'instrument_id': symbol, 'date': day,
                             'adjusted_to_raw_gross_return_ratio': ratio,
                             'factor_ratio': factor_ratio}
                    jumps.append(event)
                    if (symbol, day) not in action_keys:
                        unmatched.append(event)
            previous = (raw_close, price, factor)
    if max(errors) > 1e-6 or unmatched:
        raise ValueError(f'Qlib identity or action alignment failed: {len(unmatched)} jumps')
    lineage = {'frozen_stress_report_sha256': hashlib.sha256(stress_bytes).hexdigest(),
               'frozen_panel_sha256': stress['panel_sha256'],
               'frozen_code_sha256_verified': True,
               'qlib_manifest_sha256': qlineage['manifest_sha256'],
               'raw_archive_path': str(db_path.resolve().relative_to(BASE)),
               'raw_row_count': len(rows), 'raw_rows_sha256': rows_hash.hexdigest(),
               'raw_run_ids_by_stock': {key: sorted(value) for key, value in run_ids.items()},
               'supplier_snapshots': snapshots,
               'replay_dependency_sha256': {
                   str(Path(module).relative_to(BASE)): hashlib.sha256(Path(module).read_bytes()).hexdigest()
                   for module in (BASE / 'scripts/raw_action_aware_hold_stress.py',
                                  BASE / 'scripts/raw_action_aware_sma_stress.py')},
               'matched_stock_days': len(errors),
               'max_qlib_raw_factor_identity_relative_error': max(errors),
               'factor_jumps_over_1pct': len(jumps),
               'factor_jumps_matched_to_supplier_action': len(jumps),
               'supplier_action_count': len(action_keys)}
    return stress, dates, raw, actions, qdates, qpools, lineage


def evaluate(stress: dict, dates: list[str], raw: dict, actions: dict,
             qdates: list[str], qpools: dict) -> dict:
    date_index = {day: i for i, day in enumerate(dates)}
    cases = []
    for pool_name, qpanel in qpools.items():
        symbols = sorted(qpanel)
        panel = {symbol: [raw[symbol][day] for day in dates] for symbol in symbols}
        pool_actions = {symbol: actions[symbol] for symbol in symbols}
        sma = sma_targets(qpanel)
        same = exposure_matched_targets(sma, symbols)
        targets = {'sma': dict(zip(qdates, sma)),
                   'same_exposure_equal': dict(zip(qdates, same))}
        for horizon in (126, 252):
            for seed in (202, 404):
                frozen = stress['cases']['later'][str(horizon)][str(seed)]['pools'][pool_name]
                if frozen['episodes'] != 64 or len(frozen['cases']) != 64:
                    raise ValueError('frozen sample count changed')
                for source in frozen['cases']:
                    start, end = date_index[source['entry']], date_index[source['exit']]
                    if end - start != horizon or start + 1 >= end:
                        raise ValueError('frozen horizon differs from raw calendar')
                    outputs = {}
                    for policy, target in targets.items():
                        outputs[policy] = {
                            'fractional': simulate_rebalanced(
                                dates, panel, pool_actions, target, entry=start + 1,
                                exit_index=end, lot_size=None),
                            'lot_100': simulate_rebalanced(
                                dates, panel, pool_actions, target, entry=start + 1,
                                exit_index=end, lot_size=LOT)}
                    hold = {
                        'fractional': simulate_hold(dates, panel, pool_actions,
                                                    entry=start + 1, exit_index=end,
                                                    lot_size=None),
                        'lot_100': simulate_hold(dates, panel, pool_actions,
                                                entry=start + 1, exit_index=end,
                                                lot_size=LOT)}
                    case_id = hashlib.sha256(
                        f'{VERSION}|later|{horizon}|{seed}|{pool_name}|{source["entry"]}'.encode()
                    ).hexdigest()[:20]
                    cases.append({'case_id': case_id, 'pool': pool_name,
                                  'horizon_sessions': horizon, 'seed': seed,
                                  'decision_date': source['entry'],
                                  'first_fill_date': dates[start + 1],
                                  'exit_date': source['exit'],
                                  'frozen_adjusted_sma_return': source['sma_return'],
                                  'frozen_adjusted_same_exposure_return': source['exposure_equal_return'],
                                  'frozen_adjusted_passive_return': source['static_80_return'],
                                  'passive_fixed_80': hold, 'policies': outputs})
    if len(cases) != 3072 or len({item['case_id'] for item in cases}) != 3072:
        raise ValueError('later sample denominator changed')
    summaries = {}
    for horizon in (126, 252):
        for seed in (202, 404):
            selected = [item for item in cases if item['horizon_sessions'] == horizon
                        and item['seed'] == seed]
            joint = [item for item in selected if all(
                result['status'] == 'fully_liquidated_raw_action_proxy'
                for result in (*item['passive_fixed_80'].values(),
                               *(basis for policy in item['policies'].values()
                                 for basis in policy.values())))]
            def ret(item: dict, policy: str, basis: str) -> float:
                return item['policies'][policy][basis]['net_return']
            by_pool_same = {}
            by_pool_passive = {}
            by_pool_equal_passive = {}
            disjoint = []
            for pool_name in stress['pool_symbols']:
                part = sorted((item for item in joint if item['pool'] == pool_name),
                              key=lambda item: item['decision_date'])
                by_pool_same[pool_name] = median([
                    ret(item, 'sma', 'lot_100')
                    - ret(item, 'same_exposure_equal', 'lot_100') for item in part]) if part else None
                by_pool_passive[pool_name] = median([
                    ret(item, 'sma', 'lot_100')
                    - item['passive_fixed_80']['lot_100']['net_return']
                    for item in part]) if part else None
                by_pool_equal_passive[pool_name] = median([
                    ret(item, 'same_exposure_equal', 'lot_100')
                    - item['passive_fixed_80']['lot_100']['net_return']
                    for item in part]) if part else None
                last_exit = ''
                for item in part:
                    if item['decision_date'] > last_exit:
                        disjoint.append(item)
                        last_exit = item['exit_date']
            summaries[f'{horizon}:{seed}'] = {
                'sample_count': len(selected), 'jointly_liquidated_count': len(joint),
                'sma_fractional_minus_frozen_proxy': _distribution([
                    ret(item, 'sma', 'fractional') - item['frozen_adjusted_sma_return']
                    for item in joint]),
                'sma_lot_100_minus_fractional': _distribution([
                    ret(item, 'sma', 'lot_100') - ret(item, 'sma', 'fractional')
                    for item in joint]),
                'passive_fractional_minus_frozen_proxy': _distribution([
                    item['passive_fixed_80']['fractional']['net_return']
                    - item['frozen_adjusted_passive_return'] for item in joint]),
                'passive_lot_100_initial_gross_weight': _distribution([
                    item['passive_fixed_80']['lot_100']['initial_gross_weight']
                    for item in joint]),
                'sma_lot_100_return': _distribution([
                    ret(item, 'sma', 'lot_100') for item in joint]),
                'same_exposure_lot_100_return': _distribution([
                    ret(item, 'same_exposure_equal', 'lot_100') for item in joint]),
                'passive_lot_100_return': _distribution([
                    item['passive_fixed_80']['lot_100']['net_return'] for item in joint]),
                'sma_lot_100_minus_same_exposure_lot_100': _distribution([
                    ret(item, 'sma', 'lot_100')
                    - ret(item, 'same_exposure_equal', 'lot_100') for item in joint]),
                'sma_lot_100_minus_passive_lot_100': _distribution([
                    ret(item, 'sma', 'lot_100')
                    - item['passive_fixed_80']['lot_100']['net_return'] for item in joint]),
                'same_exposure_lot_100_minus_passive_lot_100': _distribution([
                    ret(item, 'same_exposure_equal', 'lot_100')
                    - item['passive_fixed_80']['lot_100']['net_return'] for item in joint]),
                'positive_pool_median_excess_vs_same_exposure_count': sum(
                    value is not None and value > 0 for value in by_pool_same.values()),
                'positive_pool_median_excess_vs_passive_count': sum(
                    value is not None and value > 0 for value in by_pool_passive.values()),
                'positive_pool_median_equal_excess_vs_passive_count': sum(
                    value is not None and value > 0 for value in by_pool_equal_passive.values()),
                'pool_median_excess_vs_same_exposure': by_pool_same,
                'pool_median_excess_vs_passive': by_pool_passive,
                'pool_median_equal_excess_vs_passive': by_pool_equal_passive,
                'within_pool_disjoint_jointly_liquidated_count': len(disjoint),
                'within_pool_disjoint_sma_lot_100_minus_same_exposure': _distribution([
                    ret(item, 'sma', 'lot_100')
                    - ret(item, 'same_exposure_equal', 'lot_100') for item in disjoint]),
                'within_pool_disjoint_sma_lot_100_minus_passive': _distribution([
                    ret(item, 'sma', 'lot_100')
                    - item['passive_fixed_80']['lot_100']['net_return'] for item in disjoint]),
                'within_pool_disjoint_equal_lot_100_minus_passive': _distribution([
                    ret(item, 'same_exposure_equal', 'lot_100')
                    - item['passive_fixed_80']['lot_100']['net_return'] for item in disjoint]),
            }
    return {'case_count': len(cases), 'cases': cases, 'summaries': summaries}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--db', type=Path, default=Path('data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--qlib-root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/raw-action-aware-36-pool-later-stress-v1.json'))
    args = parser.parse_args()
    stress_path = args.source_dir / 'qlib-deterministic-pool-stress-v2.json'
    stress, dates, raw, actions, qdates, qpools, lineage = load_sources(
        stress_path, args.source_dir, args.db, args.qlib_root, args.qlib_manifest)
    result = evaluate(stress, dates, raw, actions, qdates, qpools)
    report = {'study_version': VERSION,
              'status': 'retrospective_later_raw_action_stress_not_stable_baseline',
              'segment': 'later', 'first_date': FIRST, 'last_date': LAST,
              'historical_point_in_time_universe_confirmed': False,
              'independent_out_of_sample_confirmed': False,
              'actual_exchange_execution_confirmed': False,
              'capital_cny': CAPITAL, 'buy_fee_per_side': FEE,
              'sell_fee_per_side': FEE, 'buy_lot_shares': LOT,
              'source_lineage': lineage,
              'entry_rule': 'Frozen SMA-positive date t; first possible fill at t+1 raw close.',
              'policy_rules': {'sma': 'Frozen 20/60-day SMA daily targets.',
                               'same_exposure_equal': 'Daily SMA gross target equally spread across three names.',
                               'passive_fixed_80': 'Buy 80% equal weights on first fill and hold to exit.'},
              **result,
              'limitations': [
                  'The 36 stocks were selected using complete history through 2026; later is chronological only, not an independent point-in-time out-of-sample test.',
                  'BaoStock corporate-action snapshots were collected later, not verified historical publications.',
                  'Gross dividends and simplified bonus delivery omit taxes, rights subscription and full odd-lot settlement.',
                  'Raw closing price proxy ignores limit queues, slippage and intraday fillability.',
                  'Windows overlap heavily and seeds may reuse dates; case count is not an independent success-trial count.',
              ],
              'generated_at': datetime.now(timezone.utc).isoformat(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()), 'cases': result['case_count'],
                      'stock_days': lineage['matched_stock_days'],
                      'factor_jumps': lineage['factor_jumps_over_1pct'],
                      'status': report['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
