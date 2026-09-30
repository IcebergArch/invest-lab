"""Check Qlib adjusted/raw identity and large factor jumps for 2021 anchor 36.

Only same-day supplier operations are matched. This is not total-return or
historical publication verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from quant_lab.qlib_local import read_stock
from qlib_2021_anchor_raw_status_audit import audit as status_audit
from raw_action_aware_hold_stress import parse_action

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-action-alignment-v1'
FIRST, LAST = '2021-01-04', '2026-09-28'


def reconcile_same_day_action(identifier: str, day: str,
                              rows: list[dict]) -> tuple[dict, dict | None]:
    """Collapse compatible provider revisions, never add their cash twice."""
    if not rows:
        raise ValueError('empty action group')
    numeric = ('dividCashPsBeforeTax', 'dividStocksPs', 'dividReserveToStockPs')
    dates = ('dividRegistDate', 'dividOperateDate', 'dividPayDate',
             'dividStockMarketDate')
    for field in (*numeric, *dates):
        nonempty = [row[field] for row in rows if row[field]]
        if field in numeric:
            values = [float(value) for value in nonempty]
            if any(not math.isfinite(value) or value < 0 for value in values):
                raise ValueError(f'{identifier} {day}: invalid {field}')
            if values and max(values) - min(values) > 1e-8:
                raise ValueError(f'{identifier} {day}: conflicting {field}')
        elif len(set(nonempty)) > 1:
            raise ValueError(f'{identifier} {day}: conflicting {field}')
    chosen = max(rows, key=lambda row: (
        bool(row['dividCashPsBeforeTax']),
        sum(bool(value) for value in row.values())))
    merged = dict(chosen)
    for field in (*numeric, *dates):
        if not merged[field]:
            merged[field] = next((row[field] for row in rows if row[field]), '')
    parse_action(identifier, merged)
    diagnostic = None
    if len(rows) > 1:
        diagnostic = {'instrument_id': identifier, 'operate_date': day,
                      'provider_row_count': len(rows),
                      'selected_economic_fields': {
                          field: merged[field] for field in (*numeric, *dates)},
                      'provider_rows': rows}
    return merged, diagnostic


def audit(source_dir: Path, snapshot_path: Path, db_path: Path,
          qlib_root: Path, qlib_manifest: Path) -> dict:
    anchor_path = source_dir / 'qlib-2021-anchor-pool-coverage-audit-v1.json'
    raw_path = source_dir / 'qlib-2021-anchor-raw-status-audit-v1.json'
    anchor_bytes, raw_bytes = anchor_path.read_bytes(), raw_path.read_bytes()
    anchor, stored_raw = json.loads(anchor_bytes), json.loads(raw_bytes)
    fresh = status_audit(anchor_path, snapshot_path, db_path,
                         qlib_root, qlib_manifest)
    if (fresh['raw_archive'] != stored_raw['raw_archive']
            or fresh['classification_totals'] != stored_raw['classification_totals']
            or stored_raw['anchor_report']['sha256'] != hashlib.sha256(anchor_bytes).hexdigest()):
        raise ValueError('anchor raw status source changed')
    symbols = anchor['selected_symbols_in_hash_order']
    ids = {'stock:' + s[2:] + '.' + s[:2]: s for s in symbols}
    grouped_action_rows = {}
    action_map = {}
    snapshots = []
    outside_cutoff = []
    snap_script_sha = hashlib.sha256((
        BASE / 'scripts/baostock_anchor_corporate_action_snapshot.py').read_bytes()).hexdigest()
    for year in range(2021, 2027):
        path = source_dir / f'baostock-2021-anchor-corporate-actions-{year}-v1.json'
        payload = path.read_bytes()
        snap = json.loads(payload)
        if (snap.get('snapshot_version') != 'baostock-2021-anchor-corporate-actions-v1'
                or snap.get('status') != 'provider_response_captured_not_pit'
                or snap.get('operate_year') != year or snap.get('year_type') != 'operate'
                or snap.get('script_sha256') != snap_script_sha
                or snap['source']['anchor_report_sha256'] != hashlib.sha256(anchor_bytes).hexdigest()
                or snap.get('requested_stock_count') != 36
                or len(snap['responses']) != 36
                or {item['instrument_id'] for item in snap['responses']} != set(ids)
                or snap['response_row_count'] != sum(item['row_count']
                                                     for item in snap['responses'])):
            raise ValueError(f'{year}: action snapshot source differs')
        for response in snap['responses']:
            if response['row_count'] != len(response['rows']):
                raise ValueError('action response count differs')
            for item in response['rows']:
                if not item['dividOperateDate'].startswith(str(year)):
                    raise ValueError('supplier operation year differs')
                key = (response['instrument_id'], item['dividOperateDate'])
                grouped_action_rows.setdefault(key, []).append(item)
        snapshots.append({'year': year,
                          'path': str(path.resolve().relative_to(BASE)),
                          'sha256': hashlib.sha256(payload).hexdigest(),
                          'row_count': snap['response_row_count']})
    duplicates = []
    for key, group in grouped_action_rows.items():
        merged, diagnostic = reconcile_same_day_action(key[0], key[1], group)
        if diagnostic:
            duplicates.append(diagnostic)
        if key[1] > LAST:
            outside_cutoff.append({'instrument_id': key[0], 'operate_date': key[1]})
        else:
            action_map[key] = merged
    connection = sqlite3.connect(db_path.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        placeholders = ','.join('?' for _ in ids)
        rows = connection.execute(f'''
            SELECT b.instrument_id,b.trade_date,b.close,s.tradestatus
            FROM daily_bars b JOIN baostock_daily_status s
              ON b.instrument_id=s.instrument_id AND b.trade_date=s.trade_date
            WHERE b.instrument_id IN ({placeholders})
              AND b.trade_date BETWEEN ? AND ?
            ORDER BY b.instrument_id,b.trade_date
        ''', (*ids, FIRST, LAST)).fetchall()
    finally:
        connection.close()
    raw = {symbol: {} for symbol in symbols}
    for identifier, day, close, trade in rows:
        if identifier not in ids or day in raw[ids[identifier]]:
            raise ValueError('unexpected raw stock or duplicate date')
        raw[ids[identifier]][day] = (close, trade)
    if len(rows) != stored_raw['raw_archive']['selected_joined_row_count']:
        raise ValueError('raw stock-day count changed')
    errors = []
    jumps = []
    skipped_gap_pairs = 0
    action_on_trading = Counter()
    for symbol in symbols:
        response = read_stock(qlib_root, qlib_manifest, '2026-09-28', symbol,
                              start=FIRST, end=LAST, fields=('close', 'factor'), limit=2000)
        if (response['adjustment'] != 'qlib_adjusted'
                or response['missing_fields'] or response['truncated_to_latest']
                or not response['rows'] or response['rows'][0]['date'] != FIRST
                or response['rows'][-1]['date'] > LAST
                or [item['date'] for item in response['rows']]
                != sorted({item['date'] for item in response['rows']})):
            raise ValueError(f'{symbol}: Qlib coverage differs')
        previous = None
        for item in response['rows']:
            day = item['date']
            observation = raw[symbol].get(day)
            if observation is None or observation[1] != 1:
                previous = None
                continue
            raw_close = observation[0]
            adjusted, factor = item['close'], item['factor']
            if any(not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0
                   for x in (raw_close, adjusted, factor)):
                raise ValueError(f'{symbol} {day}: valid-trade Qlib/raw price absent')
            errors.append(abs(adjusted / (raw_close * factor) - 1))
            identifier = 'stock:' + symbol[2:] + '.' + symbol[:2]
            if (identifier, day) in action_map:
                action_on_trading[identifier] += 1
            if previous is not None:
                prior_raw, prior_adjusted, prior_factor = previous
                ratio = (adjusted / prior_adjusted) / (raw_close / prior_raw)
                factor_ratio = factor / prior_factor
                if abs(ratio / factor_ratio - 1) > 1e-6:
                    raise ValueError(f'{symbol} {day}: factor does not explain return gap')
                if abs(ratio - 1) > .01:
                    jumps.append({'instrument_id': identifier, 'date': day,
                                  'gross_return_ratio_adjusted_to_raw': ratio,
                                  'factor_ratio': factor_ratio,
                                  'same_day_supplier_action': (identifier, day) in action_map})
            else:
                skipped_gap_pairs += 1
            previous = (raw_close, adjusted, factor)
    if len(errors) != stored_raw['classification_totals']['positive_qlib_raw_trading']:
        raise ValueError('matched trading-day denominator changed')
    if max(errors) > 1e-6:
        raise ValueError('adjusted price differs from raw price times factor')
    unmatched = [item for item in jumps if not item['same_day_supplier_action']]
    return {
        'study_version': VERSION,
        'status': ('all_large_trading_day_factor_jumps_match_supplier_actions'
                   if not unmatched else 'unmatched_large_factor_jumps_need_review'),
        'anchor_report_sha256': hashlib.sha256(anchor_bytes).hexdigest(),
        'raw_status_report_sha256': hashlib.sha256(raw_bytes).hexdigest(),
        'action_snapshots': snapshots,
        'selected_stock_count': len(symbols),
        'matched_normal_trading_stock_days': len(errors),
        'max_adjusted_close_vs_raw_times_factor_relative_error': max(errors),
        'first_valid_or_after_gap_return_pairs_not_compared': skipped_gap_pairs,
        'large_factor_jump_count': len(jumps),
        'large_factor_jump_same_day_action_match_count': len(jumps) - len(unmatched),
        'unmatched_large_factor_jumps': unmatched,
        'large_factor_jumps': jumps,
        'supplier_action_rows_within_qlib_cutoff': len(action_map),
        'supplier_action_rows_after_qlib_cutoff': outside_cutoff,
        'supplier_duplicate_same_day_groups_reconciled': duplicates,
        'supplier_action_rows_on_normal_trading_day': sum(action_on_trading.values()),
        'historical_point_in_time_publication_verified': False,
        'executable_total_return_verified': False,
        'limitations': [
            'Source actions were queried in 2026 and do not establish publication-time availability.',
            'Factor identity is a mathematical relationship between two supplier archives, not independent total-return verification.',
            'Return-pair comparison skips a gap; a corporate action during a suspension may not have a tradable same-day price pair.',
            'Actions after the fixed Qlib cutoff are retained in the supplier snapshot but excluded from this comparison.',
            'Delisting or merger consideration, rights subscription and dividend tax are not established by these action rows.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


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
        'studies/random-entry-baseline-v1/qlib-2021-anchor-action-alignment-v1.json'))
    args = parser.parse_args()
    report = audit(args.source_dir, args.snapshot, args.db,
                   args.qlib_root, args.qlib_manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'matched_stock_days': report['matched_normal_trading_stock_days'],
                      'large_jumps': report['large_factor_jump_count'],
                      'unmatched': len(report['unmatched_large_factor_jumps'])},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
