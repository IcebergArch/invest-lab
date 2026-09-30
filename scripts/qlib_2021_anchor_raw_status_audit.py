"""Join 2021 anchor-pool Qlib gaps to BaoStock raw status and exit metadata.

The provider's current outDate is retrospective metadata, not proof of when
an investor knew or could sell a corporate event.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from qlib_2021_anchor_pool_audit import ANCHOR, LAST, SEGMENTS, VERSION as ANCHOR_VERSION
from qlib_csi300_entry_eligibility import load_verified_source
from quant_lab.baostock_source import read_baostock_historical_snapshot

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-raw-status-audit-v1'


def audit(anchor_path: Path, snapshot_path: Path, db_path: Path,
          qlib_root: Path, manifest: Path) -> dict:
    anchor_bytes = anchor_path.read_bytes()
    anchor = json.loads(anchor_bytes)
    if (anchor.get('study_version') != ANCHOR_VERSION
            or anchor.get('anchor_label') != ANCHOR
            or anchor.get('script_sha256') != hashlib.sha256((
                BASE / 'scripts/qlib_2021_anchor_pool_audit.py').read_bytes()).hexdigest()):
        raise ValueError('anchor study source changed')
    calendar, masks, features, qlineage = load_verified_source(qlib_root, manifest)
    if anchor['source_lineage'] != qlineage:
        raise ValueError('Qlib release differs from anchor audit')
    symbols = anchor['selected_symbols_in_hash_order']
    if len(symbols) != 36 or len(set(symbols)) != 36:
        raise ValueError('anchor symbol count differs')
    ids = {symbol: 'stock:' + symbol[2:] + '.' + symbol[:2] for symbol in symbols}
    snapshot_bytes = snapshot_path.read_bytes()
    catalogue = read_baostock_historical_snapshot(snapshot_path)
    listings = {item.instrument.instrument_id: item for item in catalogue.listings
                if item.instrument.instrument_id in ids.values()}
    if len(listings) != 36:
        raise ValueError('historical supplier catalogue lacks an anchor name')
    connection = sqlite3.connect(db_path.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('raw archive failed quick_check')
        placeholders = ','.join('?' for _ in ids)
        rows = connection.execute(f'''
            SELECT s.instrument_id,s.trade_date,s.tradestatus,s.is_st,
                   s.raw_amount_cny,s.has_valid_bar,s.run_id,s.payload_hash,
                   b.close,b.amount,b.run_id,b.source_id,b.adjustment,b.payload_hash
            FROM baostock_daily_status s LEFT JOIN daily_bars b
              ON b.instrument_id=s.instrument_id AND b.trade_date=s.trade_date
            WHERE s.instrument_id IN ({placeholders})
              AND s.trade_date BETWEEN ? AND ?
            ORDER BY s.instrument_id,s.trade_date
        ''', (*ids.values(), ANCHOR, LAST)).fetchall()
    finally:
        connection.close()
    raw = {symbol: {} for symbol in symbols}
    raw_rows_hash = hashlib.sha256()
    source_runs = {symbol: set() for symbol in symbols}
    reverse = {identifier: symbol for symbol, identifier in ids.items()}
    for row in rows:
        (identifier, day, trade, st, raw_amount, valid, status_run, status_hash,
         close, amount, bar_run, source, adjustment, bar_hash) = row
        symbol = reverse[identifier]
        if (day in raw[symbol] or source != 'baostock_daily' or adjustment != 'none'
                or bar_run != status_run or not status_hash or not bar_hash
                or valid != 1 or close is None or not math.isfinite(close) or close <= 0
                or (trade == 1 and not (
                    (amount is not None and amount > 0)
                    or (raw_amount is not None and raw_amount > 0)))
                or trade not in (0, 1) or st not in (0, 1)):
            raise ValueError(f'{identifier} {day}: raw bar/status inconsistency')
        raw[symbol][day] = {'tradestatus': trade, 'is_st': st,
                            'raw_amount_cny': raw_amount, 'close': close}
        source_runs[symbol].add(status_run)
        raw_rows_hash.update(json.dumps(row, ensure_ascii=False,
                                        separators=(',', ':')).encode())
        raw_rows_hash.update(b'\n')
    indices = [i for i, day in enumerate(calendar)
               if ANCHOR <= day.isoformat() <= LAST]
    if len(indices) != 1391:
        raise ValueError('release calendar window differs')
    counts = Counter()
    segment_counts = {segment: Counter() for segment in SEGMENTS}
    per_symbol = []
    for symbol in symbols:
        listing = listings[ids[symbol]]
        local = Counter()
        outdate = listing.out_date or None
        for index in indices:
            day = calendar[index].isoformat()
            valid_qlib = all(
                isinstance(features[symbol][field].at(index), (int, float))
                and math.isfinite(features[symbol][field].at(index))
                and features[symbol][field].at(index) > 0
                for field in ('close', 'volume'))
            observation = raw[symbol].get(day)
            if observation is None:
                if outdate is None or day < outdate or valid_qlib:
                    raise ValueError(f'{symbol} {day}: unexplained missing raw status')
                label = 'missing_qlib_after_supplier_outdate'
            elif observation['tradestatus'] == 0:
                if valid_qlib:
                    raise ValueError(f'{symbol} {day}: Qlib positive on halted raw day')
                label = 'missing_qlib_raw_halted'
            else:
                if not valid_qlib:
                    raise ValueError(f'{symbol} {day}: Qlib missing on raw-trading day')
                label = 'positive_qlib_raw_trading'
            local[label] += 1
            counts[label] += 1
            for segment, (first, last) in SEGMENTS.items():
                if first <= day <= last:
                    segment_counts[segment][label] += 1
                    break
            else:
                raise ValueError(f'{day}: no segment')
        expected_missing = sum(
            item['days_missing_positive_close_or_volume']
            for item in next(row for row in anchor['per_symbol']
                             if row['symbol'] == symbol)['segments'].values())
        if (local['missing_qlib_after_supplier_outdate']
                + local['missing_qlib_raw_halted'] != expected_missing):
            raise ValueError(f'{symbol}: source missing-day counts differ')
        per_symbol.append({
            'symbol': symbol, 'instrument_id': ids[symbol],
            'supplier_provider_status_at_snapshot': listing.provider_status,
            'supplier_out_date_at_snapshot': outdate,
            'raw_status_days': len(raw[symbol]),
            'raw_halted_days': local['missing_qlib_raw_halted'],
            'days_after_supplier_outdate_without_raw_status':
                local['missing_qlib_after_supplier_outdate'],
            'positive_qlib_and_raw_trading_days': local['positive_qlib_raw_trading'],
            'raw_st_days': sum(row['is_st'] == 1 for row in raw[symbol].values()),
            'raw_run_ids': sorted(source_runs[symbol]),
        })
    if (sum(counts.values()) != 36 * 1391
            or sum(sum(group.values()) for group in segment_counts.values()) != 36 * 1391
            or counts['positive_qlib_raw_trading'] != len(rows) - counts['missing_qlib_raw_halted']):
        raise ValueError('reconciliation denominator differs')
    return {
        'study_version': VERSION,
        'status': 'all_2021_anchor_qlib_gaps_classified_by_supplier_status_or_outdate',
        'anchor_report': {'path': str(anchor_path.resolve().relative_to(BASE)),
                          'sha256': hashlib.sha256(anchor_bytes).hexdigest()},
        'supplier_catalogue': {'path': str(snapshot_path.resolve().relative_to(BASE)),
                               'sha256': hashlib.sha256(snapshot_bytes).hexdigest(),
                               'snapshot_id': catalogue.snapshot_id,
                               'collected_at': catalogue.collected_at},
        'raw_archive': {'path': str(db_path.resolve().relative_to(BASE)),
                        'selected_joined_row_count': len(rows),
                        'selected_joined_rows_sha256': raw_rows_hash.hexdigest(),
                        'quick_check': 'ok'},
        'calendar_session_count': len(indices),
        'selected_symbol_count': 36,
        'expected_symbol_days': 36 * 1391,
        'classification_totals': dict(counts),
        'classification_by_segment': {
            segment: dict(group) for segment, group in segment_counts.items()},
        'per_symbol': per_symbol,
        'historical_point_in_time_membership_confirmed': False,
        'historical_supplier_metadata_vintage_confirmed': False,
        'limitations': [
            'The 2021 member label is from a 2026 Qlib release, not verified official 2021 PIT membership.',
            'BaoStock outDate and statuses were fetched in 2026; they classify retrospective coverage and do not prove historical publication time.',
            'A halted day may require a later tradable exit; a supplier outDate does not provide delisting or merger cash/share consideration.',
            'No return is imputed across nontrading or post-exit dates, so this is not a backtest.',
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
        'studies/random-entry-baseline-v1/qlib-2021-anchor-raw-status-audit-v1.json'))
    args = parser.parse_args()
    result = audit(args.source_dir / 'qlib-2021-anchor-pool-coverage-audit-v1.json',
                   args.snapshot, args.db, args.qlib_root, args.qlib_manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'classification_totals': result['classification_totals']},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
