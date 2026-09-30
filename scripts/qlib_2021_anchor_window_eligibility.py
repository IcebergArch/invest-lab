"""Keep every frozen calendar-random window in a 2021 anchor stock pool.

This is a trading-status denominator audit. It does not price corporate exits,
impute missing returns, or call the 2026-revised member label PIT.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from qlib_csi300_entry_eligibility import load_verified_source
from qlib_2021_anchor_raw_status_audit import audit as status_audit
from unconditional_random_entry_stress import SEGMENTS, HORIZONS, SEEDS, sampled_dates

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-random-window-eligibility-v1'


def evaluate(anchor: dict, calendar: list[str], status_by_id: dict,
             outdates: dict, sampled: dict) -> dict:
    symbols = anchor['selected_symbols_in_hash_order']
    pools = {f'pool_{i + 1:02}': symbols[3 * i:3 * i + 3]
             for i in range(12)}
    if any(len(part) != 3 for part in pools.values()):
        raise ValueError('anchor selection does not form twelve three-stock pools')
    date_index = {day: i for i, day in enumerate(calendar)}
    cases = []
    for segment, (first, last) in SEGMENTS.items():
        for horizon in HORIZONS:
            for seed in SEEDS:
                chosen = sampled[segment][str(horizon)][str(seed)]
                if chosen != sampled_dates(calendar, first, last, horizon, seed):
                    raise ValueError('calendar-random sample differs from source study')
                for pool_name, members in pools.items():
                    for decision in chosen:
                        start = date_index[decision]
                        exit_index = start + horizon
                        exit_day = calendar[exit_index]
                        if exit_day > last:
                            raise ValueError('random window escapes its segment')
                        status_counts = Counter()
                        first_blocking = None
                        entry_all_trading = entry_all_normal = True
                        exit_all_trading = exit_all_normal = True
                        for j in range(start + 1, exit_index + 1):
                            day = calendar[j]
                            events = []
                            for symbol in members:
                                identifier = 'stock:' + symbol[2:] + '.' + symbol[:2]
                                row = status_by_id[identifier].get(day)
                                if row is None:
                                    if outdates[identifier] is None or day < outdates[identifier]:
                                        raise ValueError(f'{identifier} {day}: unexplained missing status')
                                    state = 'after_supplier_outdate'
                                elif row['tradestatus'] == 0:
                                    state = 'halted'
                                elif row['is_st'] == 1:
                                    state = 'trading_st'
                                else:
                                    state = 'trading_normal'
                                status_counts[state] += 1
                                if state in ('halted', 'after_supplier_outdate'):
                                    events.append({'symbol': symbol, 'state': state})
                                if j == start + 1:
                                    entry_all_trading &= state in ('trading_st', 'trading_normal')
                                    entry_all_normal &= state == 'trading_normal'
                                if j == exit_index:
                                    exit_all_trading &= state in ('trading_st', 'trading_normal')
                                    exit_all_normal &= state == 'trading_normal'
                            if events and first_blocking is None:
                                first_blocking = {'date': day, 'events': events}
                        total = sum(status_counts.values())
                        if total != 3 * horizon:
                            raise ValueError('per-case stock-day denominator differs')
                        case_id = hashlib.sha256(
                            f'{VERSION}|{segment}|{horizon}|{seed}|{pool_name}|{decision}'.encode()
                        ).hexdigest()[:20]
                        cases.append({
                            'case_id': case_id, 'segment': segment,
                            'horizon_sessions': horizon, 'seed': seed,
                            'pool': pool_name, 'members': members,
                            'decision_date': decision,
                            'first_fill_date': calendar[start + 1],
                            'exit_date': exit_day,
                            'entry_all_three_trading': entry_all_trading,
                            'entry_all_three_normal_non_st': entry_all_normal,
                            'exit_all_three_trading': exit_all_trading,
                            'exit_all_three_normal_non_st': exit_all_normal,
                            'continuous_trading_status': not (
                                status_counts['halted'] or status_counts['after_supplier_outdate']),
                            'continuous_normal_non_st_status': total == status_counts['trading_normal'],
                            'has_halt': status_counts['halted'] > 0,
                            'has_supplier_outdate_gap': status_counts['after_supplier_outdate'] > 0,
                            'has_st': status_counts['trading_st'] > 0,
                            'stock_day_status_counts': dict(status_counts),
                            'first_halt_or_exit_gap': first_blocking,
                        })
    if len(cases) != 6144 or len({item['case_id'] for item in cases}) != 6144:
        raise ValueError('frozen random-window denominator changed')
    summaries = {}
    for segment in SEGMENTS:
        summaries[segment] = {}
        for horizon in HORIZONS:
            for seed in SEEDS:
                selected = [x for x in cases if x['segment'] == segment
                            and x['horizon_sessions'] == horizon and x['seed'] == seed]
                summaries[segment][f'{horizon}:{seed}'] = {
                    'case_count': len(selected),
                    'entry_all_three_trading_count': sum(x['entry_all_three_trading'] for x in selected),
                    'entry_all_three_normal_non_st_count': sum(
                        x['entry_all_three_normal_non_st'] for x in selected),
                    'exit_all_three_trading_count': sum(x['exit_all_three_trading'] for x in selected),
                    'continuous_trading_status_count': sum(
                        x['continuous_trading_status'] for x in selected),
                    'continuous_normal_non_st_status_count': sum(
                        x['continuous_normal_non_st_status'] for x in selected),
                    'cases_with_halt': sum(x['has_halt'] for x in selected),
                    'cases_with_supplier_outdate_gap': sum(
                        x['has_supplier_outdate_gap'] for x in selected),
                    'cases_with_st': sum(x['has_st'] for x in selected),
                    'cases_with_halt_or_outdate_gap': sum(
                        x['has_halt'] or x['has_supplier_outdate_gap'] for x in selected),
                    'continuous_normal_count_by_pool': {
                        name: sum(x['continuous_normal_non_st_status'] for x in selected
                                  if x['pool'] == name) for name in pools},
                }
    return {'case_count': len(cases), 'pool_symbols': pools,
            'summaries': summaries, 'cases': cases}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--snapshot', type=Path, default=Path(
        'reports/quant/snapshots/2026-09-29-baostock-historical-shsz-590b41a3bd0a.json'))
    parser.add_argument('--db', type=Path, default=Path('data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--qlib-root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-random-window-eligibility-v1.json'))
    args = parser.parse_args()
    anchor_path = args.source_dir / 'qlib-2021-anchor-pool-coverage-audit-v1.json'
    raw_path = args.source_dir / 'qlib-2021-anchor-raw-status-audit-v1.json'
    random_path = args.source_dir / 'unconditional-random-entry-36-pool-stress-v1.json'
    anchor_bytes, raw_bytes, random_bytes = (
        path.read_bytes() for path in (anchor_path, raw_path, random_path))
    anchor, stored_raw, random_study = (
        json.loads(payload) for payload in (anchor_bytes, raw_bytes, random_bytes))
    if (random_study.get('study_version') != 'unconditional-random-entry-36-pool-stress-v1'
            or random_study.get('script_sha256') != hashlib.sha256((
                BASE / 'scripts/unconditional_random_entry_stress.py').read_bytes()).hexdigest()
            or stored_raw['anchor_report']['sha256'] != hashlib.sha256(anchor_bytes).hexdigest()):
        raise ValueError('input report or random sampler source differs')
    fresh_raw = status_audit(anchor_path, args.snapshot, args.db,
                             args.qlib_root, args.qlib_manifest)
    if (fresh_raw['raw_archive'] != stored_raw['raw_archive']
            or fresh_raw['classification_totals'] != stored_raw['classification_totals']
            or fresh_raw['per_symbol'] != stored_raw['per_symbol']):
        raise ValueError('current raw archive differs from saved status audit')
    calendar, masks, features, lineage = load_verified_source(
        args.qlib_root, args.qlib_manifest)
    if anchor['source_lineage'] != lineage:
        raise ValueError('Qlib release differs from anchor report')
    days = [day.isoformat() for day in calendar]
    ids = ['stock:' + symbol[2:] + '.' + symbol[:2]
           for symbol in anchor['selected_symbols_in_hash_order']]
    outdates = {row['instrument_id']: row['supplier_out_date_at_snapshot']
                for row in stored_raw['per_symbol']}
    connection = sqlite3.connect(args.db.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        placeholders = ','.join('?' for _ in ids)
        rows = connection.execute(f'''
            SELECT instrument_id,trade_date,tradestatus,is_st
            FROM baostock_daily_status WHERE instrument_id IN ({placeholders})
              AND trade_date BETWEEN ? AND ?
        ''', (*ids, '2021-01-04', '2026-09-28')).fetchall()
    finally:
        connection.close()
    if len(rows) != stored_raw['raw_archive']['selected_joined_row_count']:
        raise ValueError('raw status coverage changed after validation')
    status_by_id = {identifier: {} for identifier in ids}
    for identifier, day, trade, st in rows:
        if day in status_by_id[identifier]:
            raise ValueError('duplicate raw status day')
        status_by_id[identifier][day] = {'tradestatus': trade, 'is_st': st}
    result = evaluate(anchor, days, status_by_id, outdates,
                      random_study['sampled_dates'])
    report = {
        'study_version': VERSION,
        'status': 'retrospective_2021_anchor_window_status_not_executable_return',
        'source_lineage': {
            'anchor_report_sha256': hashlib.sha256(anchor_bytes).hexdigest(),
            'raw_status_report_sha256': hashlib.sha256(raw_bytes).hexdigest(),
            'calendar_random_report_sha256': hashlib.sha256(random_bytes).hexdigest(),
            'raw_selected_joined_rows_sha256': stored_raw['raw_archive']['selected_joined_rows_sha256'],
            'qlib_manifest_sha256': lineage['release']['manifest_sha256'],
        },
        'entry_rule': 'Same signal dates as unconditional random 12-pool stress; examine t+1 through scheduled exit for all 3 anchor names.',
        'continuous_trading_status_definition': 'Every name has BaoStock tradestatus=1 on every day in the interval; ST is separately flagged.',
        **result,
        'limitations': [
            'The 2021 Qlib member label is not verified official historical point-in-time membership.',
            'Continuous trading is stricter than actual holdability; a suspension does not itself force portfolio liquidation.',
            'ST stocks can trade but face different limits; the report flags ST rather than assuming all orders fail.',
            'Supplier outDate is retrospective metadata; no delisting consideration or merger-share conversion is modeled.',
            'This report preserves all windows and does not calculate or impute returns for incomplete price paths.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()), 'cases': result['case_count']},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
