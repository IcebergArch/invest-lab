"""Reconcile as-of reserve entries with real raw bars, status and actions.

This audits data eligibility only. It makes no return or fill-success claim.
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

from qlib_2021_anchor_action_alignment import reconcile_same_day_action
from qlib_csi300_entry_eligibility import load_verified_source
from quant_lab.qlib_local import read_stock

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-asof-reserve-raw-audit-v1'
FIRST, LAST = '2021-01-04', '2026-09-28'


def identifier(symbol: str) -> str:
    if len(symbol) != 8 or symbol[:2] not in ('SH', 'SZ') or not symbol[2:].isdigit():
        raise ValueError(f'invalid symbol {symbol}')
    return 'stock:' + symbol[2:] + '.' + symbol[:2]


def audit(asof_path: Path, action_path: Path, db_path: Path,
          qlib_root: Path, manifest: Path) -> dict:
    asof_bytes, action_bytes = asof_path.read_bytes(), action_path.read_bytes()
    asof, action = json.loads(asof_bytes), json.loads(action_bytes)
    asof_sha = hashlib.sha256(asof_bytes).hexdigest()
    if (asof.get('study_version') != 'qlib-2021-anchor-asof-reserve-coverage-v1'
            or asof['case_count'] != 6144
            or asof['script_sha256'] != hashlib.sha256((
                BASE / 'scripts/qlib_2021_anchor_asof_reserve_audit.py').read_bytes()).hexdigest()
            or action.get('snapshot_version') != 'baostock-asof-reserve-corporate-actions-v1'
            or action['source']['asof_reserve_report_sha256'] != asof_sha
            or action['script_sha256'] != hashlib.sha256((
                BASE / 'scripts/baostock_asof_reserve_corporate_action_snapshot.py').read_bytes()).hexdigest()
            or action['response_query_count'] != 18
            or action['response_row_count'] != sum(x['row_count'] for x in action['responses'])):
        raise ValueError('reserve coverage or action source differs')
    symbols = sorted(asof['replacement_symbols_needing_raw_archive'])
    if symbols != ['SH600886', 'SH601658', 'SH603658']:
        raise ValueError('reserve symbols changed')
    calendar, masks, features, lineage = load_verified_source(qlib_root, manifest)
    anchor_path = asof_path.parent / 'qlib-2021-anchor-pool-coverage-audit-v1.json'
    anchor = json.loads(anchor_path.read_bytes())
    if (lineage != anchor['source_lineage']
            or asof['source_lineage']['anchor_report_sha256']
            != hashlib.sha256(anchor_path.read_bytes()).hexdigest()):
        raise ValueError('Qlib release differs from reserve selection')
    dates = [day.isoformat() for day in calendar if FIRST <= day.isoformat() <= LAST]
    if len(dates) != 1391:
        raise ValueError('reserve audit calendar differs')
    selected_ids = {identifier(symbol) for symbol in symbols}
    all_symbols = sorted({symbol for case in asof['cases'] for symbol in case['asof_members']})
    all_ids = {identifier(symbol) for symbol in all_symbols}
    grouped_actions: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for response in action['responses']:
        if (response['instrument_id'] not in selected_ids
                or response['row_count'] != len(response['rows'])):
            raise ValueError('unexpected action response stock or count')
        for row in response['rows']:
            if (row['code'] != response['provider_code']
                    or not row['dividOperateDate'].startswith(str(response['operate_year']))):
                raise ValueError('action row source identity mismatch')
            grouped_actions[(response['instrument_id'], row['dividOperateDate'])].append(row)
    action_map = {}
    duplicates = []
    for key, group in grouped_actions.items():
        merged, duplicate = reconcile_same_day_action(key[0], key[1], group)
        if duplicate:
            duplicates.append(duplicate)
        if key[1] <= LAST:
            action_map[key] = merged
    connection = sqlite3.connect(db_path.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('raw archive quick_check failed')
        placeholders = ','.join('?' for _ in all_ids)
        rows = connection.execute(f'''
            SELECT b.instrument_id,b.trade_date,b.close,b.amount,s.raw_amount_cny,b.source_id,
                   b.adjustment,b.run_id,b.payload_hash,s.run_id,s.tradestatus,
                   s.is_st,s.has_valid_bar,s.payload_hash
            FROM daily_bars b JOIN baostock_daily_status s
              ON s.instrument_id=b.instrument_id AND s.trade_date=b.trade_date
            WHERE b.instrument_id IN ({placeholders})
              AND b.trade_date BETWEEN ? AND ?
            ORDER BY b.instrument_id,b.trade_date
        ''', (*sorted(all_ids), FIRST, LAST)).fetchall()
    finally:
        connection.close()
    raw = {identifier(symbol): {} for symbol in all_symbols}
    row_hash = hashlib.sha256()
    for row in rows:
        (stock, day, close, amount, raw_amount, source, basis, bar_run, bar_hash,
         status_run, trade, st, valid, status_hash) = row
        if (day in raw[stock] or source != 'baostock_daily' or basis != 'none'
                or bar_run != status_run or not bar_hash or not status_hash
                or trade not in (0, 1) or st not in (0, 1) or valid != 1
                or not isinstance(close, (int, float)) or not math.isfinite(close)
                or close <= 0 or (trade == 1 and not (
                    (amount is not None and amount > 0)
                    or (raw_amount is not None and raw_amount > 0)))):
            raise ValueError(f'{stock} {day}: raw price/status source mismatch')
        raw[stock][day] = {'close': close, 'trade': trade, 'is_st': st,
                           'run_id': bar_run}
        row_hash.update(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode())
        row_hash.update(b'\n')
    price_errors = []
    per_symbol = []
    jumps = []
    for symbol in symbols:
        stock = identifier(symbol)
        if sorted(raw[stock]) != dates:
            raise ValueError(f'{stock}: incomplete raw calendar')
        qlib = read_stock(qlib_root, manifest, '2026-09-28', symbol,
                          start=FIRST, end=LAST, fields=('close', 'factor', 'volume'),
                          limit=2000)
        if qlib['truncated_to_latest'] or len(qlib['rows']) != len(dates):
            raise ValueError(f'{stock}: incomplete Qlib calendar')
        local_errors = []
        missing_qlib = halted = st_days = 0
        previous = None
        for day, item in zip(dates, qlib['rows']):
            if item['date'] != day:
                raise ValueError(f'{stock}: Qlib/raw date mismatch')
            record = raw[stock][day]
            halted += record['trade'] == 0
            st_days += record['is_st'] == 1
            adjusted, factor, volume = item['close'], item['factor'], item['volume']
            if not all(isinstance(value, (int, float)) and math.isfinite(value)
                       and value > 0 for value in (adjusted, factor, volume)):
                missing_qlib += 1
                previous = None
                continue
            if record['trade'] != 1:
                raise ValueError(f'{stock} {day}: Qlib positive on raw halt')
            error = abs(adjusted / (record['close'] * factor) - 1)
            local_errors.append(error)
            price_errors.append(error)
            if previous:
                prior_raw, prior_adjusted, prior_factor = previous
                ratio = (adjusted / prior_adjusted) / (record['close'] / prior_raw)
                factor_ratio = factor / prior_factor
                if abs(ratio / factor_ratio - 1) > 1e-6:
                    raise ValueError(f'{stock} {day}: factor return mismatch')
                if abs(ratio - 1) > .01:
                    jumps.append({'instrument_id': stock, 'date': day,
                                  'gross_return_ratio_adjusted_to_raw': ratio,
                                  'factor_ratio': factor_ratio,
                                  'same_day_supplier_action': (stock, day) in action_map})
            previous = (record['close'], adjusted, factor)
        per_symbol.append({'symbol': symbol, 'raw_status_days': len(raw[stock]),
                           'raw_halted_days': halted, 'raw_st_days': st_days,
                           'missing_qlib_positive_close_factor_or_volume_days': missing_qlib,
                           'matched_normal_trading_days': len(local_errors),
                           'max_price_factor_relative_error': max(local_errors) if local_errors else None,
                           'source_run_ids': sorted({x['run_id'] for x in raw[stock].values()}),
                           'supplier_action_count': sum(key[0] == stock for key in action_map)})
    if not price_errors or max(price_errors) > 1e-6:
        raise ValueError('reserve Qlib adjusted close differs from raw close times factor')
    case_counts = Counter()
    changed_cases = []
    for case in asof['cases']:
        chosen = [identifier(symbol) for symbol in case['asof_members']]
        first = case['first_fill_date']
        entry = [raw[stock].get(first) for stock in chosen]
        entry_normal = all(row is not None and row['trade'] == 1 and row['is_st'] == 0
                           for row in entry)
        entry_halt = any(row is not None and row['trade'] == 0 for row in entry)
        entry_missing = any(row is None for row in entry)
        entry_st = any(row is not None and row['is_st'] == 1 for row in entry)
        for label, condition in (
                ('entry_normal_raw_non_st', entry_normal),
                ('entry_halt', entry_halt), ('entry_missing_raw', entry_missing),
                ('entry_st', entry_st)):
            case_counts[label] += condition
            if case['changes']:
                case_counts['changed_' + label] += condition
        if case['changes']:
            changed_cases.append({'case_id': case['case_id'],
                                  'decision_date': case['decision_date'],
                                  'first_fill_date': first,
                                  'asof_members': case['asof_members'],
                                  'entry_normal_raw_non_st': entry_normal,
                                  'entry_halt': entry_halt,
                                  'entry_missing_raw': entry_missing,
                                  'entry_st': entry_st})
    if len(changed_cases) != asof['changed_pool_case_count']:
        raise ValueError('changed-case denominator differs')
    unmatched = [item for item in jumps if not item['same_day_supplier_action']]
    return {'study_version': VERSION,
            'status': 'raw_entry_and_action_coverage_audit_not_return_replay',
            'source_lineage': {
                'asof_reserve_report_sha256': asof_sha,
                'supplier_action_snapshot_sha256': hashlib.sha256(action_bytes).hexdigest(),
                'qlib_release': lineage,
                'raw_archive_selected_join_rows_sha256': row_hash.hexdigest(),
                'raw_archive_selected_join_row_count': len(rows),
            },
            'reserve_stock_count': len(symbols), 'per_reserve': per_symbol,
            'supplier_action_rows': action['response_row_count'],
            'deduplicated_actions_within_release': len(action_map),
            'duplicate_same_day_action_groups': duplicates,
            'large_factor_jump_count': len(jumps),
            'matched_large_factor_jump_count': len(jumps) - len(unmatched),
            'unmatched_large_factor_jumps': unmatched,
            'large_factor_jumps': jumps,
            'total_case_count': len(asof['cases']),
            'changed_case_count': len(changed_cases),
            'entry_case_counts': dict(case_counts),
            'changed_cases': changed_cases,
            'historical_point_in_time_publication_verified': False,
            'full_three_stock_order_execution_verified': False,
            'limitations': [
                'Raw normal trading status and close are necessary, not sufficient, for a fill at the close.',
                'No limit-up/down queue, auction liquidity, order-level cash, or full corporate claim ledger is modeled here.',
                'The 2021-labelled Qlib candidate universe and source responses are retrospective.',
                'Price-factor identity is not independent verification of investable total return.',
            ],
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asof-report', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-asof-reserve-coverage-v1.json'))
    parser.add_argument('--action-snapshot', type=Path, default=Path(
        'studies/random-entry-baseline-v1/baostock-asof-reserve-corporate-actions-v1.json'))
    parser.add_argument('--db', type=Path, default=Path('data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--qlib-root', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-asof-reserve-raw-audit-v1.json'))
    args = parser.parse_args()
    report = audit(args.asof_report, args.action_snapshot, args.db,
                   args.qlib_root, args.qlib_manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'raw_days': report['source_lineage']['raw_archive_selected_join_row_count'],
                      'entry_cases': report['entry_case_counts'],
                      'large_jumps': report['large_factor_jump_count'],
                      'unmatched_jumps': len(report['unmatched_large_factor_jumps'])},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
