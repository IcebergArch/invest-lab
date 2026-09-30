"""Reconcile frozen Qlib 12-pool prices with BaoStock raw bars, without estimating P&L.

The 2026-vintage Qlib factor is checked as a mathematical conversion only;
it is not evidence that this factor was available at the historical decision date.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from quant_lab.qlib_local import _load_index, read_stock

VERSION = 'qlib-raw-price-factor-audit-v1'
STRESS_VERSION = 'qlib-deterministic-pool-stress-v2'
START = '2021-01-04'
END = '2023-12-29'
EXPECTED_RUN_ID = 'a80b4ab1cd8145b8bb59acb565c239d2'
BASE = Path(__file__).resolve().parents[1]


def price_comparison(raw_previous: float, raw_current: float,
                     adjusted_previous: float, adjusted_current: float,
                     factor_previous: float, factor_current: float) -> dict[str, float]:
    values = (raw_previous, raw_current, adjusted_previous, adjusted_current,
              factor_previous, factor_current)
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError('price and factor inputs must be finite positive')
    raw_return = raw_current / raw_previous - 1
    adjusted_return = adjusted_current / adjusted_previous - 1
    ratio = (1 + adjusted_return) / (1 + raw_return)
    factor_ratio = factor_current / factor_previous
    return {'raw_return': raw_return, 'adjusted_return': adjusted_return,
            'adjusted_to_raw_gross_return_ratio': ratio,
            'factor_ratio': factor_ratio,
            'factor_explanation_residual': abs(ratio / factor_ratio - 1)}


def audit(stress_path: Path, db_path: Path, root: Path, manifest: Path,
          run_id: str = EXPECTED_RUN_ID) -> dict:
    stress_bytes = stress_path.read_bytes()
    stress = json.loads(stress_bytes)
    if stress.get('study_version') != STRESS_VERSION:
        raise ValueError('unexpected frozen stress report version')
    pools = stress['pool_symbols']
    if len(pools) != 12 or any(len(pool) != 3 for pool in pools.values()):
        raise ValueError('frozen study must contain twelve three-stock pools')
    ids = sorted({symbol for pool in pools.values() for symbol in pool})
    if len(ids) != 36:
        raise ValueError('frozen study pools are not 36 distinct stocks')
    index = _load_index(root.resolve(), manifest.resolve(), '2026-09-28')
    lineage = stress['source_lineage']
    for field in ('manifest_sha256', 'archive_sha256'):
        if index['release'][field] != lineage[field]:
            raise ValueError(f'Qlib {field} differs from frozen study')
    connection = sqlite3.connect(db_path.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('raw archive integrity check failed')
        windows = connection.execute('''
            SELECT instrument_id,status,bar_count,status_count
            FROM backfill_windows WHERE run_id=? ORDER BY instrument_id
        ''', (run_id,)).fetchall()
        if len(windows) != 36 or {row[0] for row in windows} != set(ids) or any(
                row[1:] != ('success', 727, 727) for row in windows):
            raise ValueError('archive run does not contain 36 complete success windows')
        rows = connection.execute('''
            SELECT b.instrument_id,b.trade_date,b.close,b.source_id,b.adjustment,
                   s.tradestatus,s.is_st,s.run_id
            FROM daily_bars b LEFT JOIN baostock_daily_status s
              ON s.instrument_id=b.instrument_id AND s.trade_date=b.trade_date
            WHERE b.run_id=? ORDER BY b.instrument_id,b.trade_date
        ''', (run_id,)).fetchall()
    finally:
        connection.close()
    by_stock: dict[str, list[tuple]] = {identifier: [] for identifier in ids}
    for row in rows:
        identifier, _day, raw_close, source, adjustment, trade, st, status_run = row
        if identifier not in by_stock or source != 'baostock_daily' or adjustment != 'none':
            raise ValueError('archive row has unexpected stock, source or price basis')
        if status_run != run_id or trade != 1 or st != 0:
            raise ValueError('archive status missing or unexpected for frozen pool')
        by_stock[identifier].append(row)

    all_residuals: list[float] = []
    events: list[dict] = []
    per_stock: list[dict] = []
    total_daily_returns = 0
    for identifier in ids:
        symbol = ('SH' if identifier.endswith('.SH') else 'SZ') + identifier[6:12]
        payload = read_stock(root, manifest, '2026-09-28', symbol,
                             start=START, end=END, fields=('close', 'factor'), limit=1000)
        qrows = payload['rows']
        raw_rows = by_stock[identifier]
        if (payload['adjustment'] != 'qlib_adjusted' or payload['missing_fields']
                or payload['truncated_to_latest'] or len(qrows) != 727
                or len(raw_rows) != 727):
            raise ValueError(f'{identifier}: Qlib or raw coverage incomplete')
        if [row['date'] for row in qrows] != [row[1] for row in raw_rows]:
            raise ValueError(f'{identifier}: Qlib and raw dates differ')
        maximum_identity_error = 0.0
        count_over_one_percent = 0
        previous = None
        for qrow, raw_row in zip(qrows, raw_rows):
            day, raw_close = raw_row[1], float(raw_row[2])
            adjusted_close = qrow['close']
            factor = qrow['factor']
            if any(not isinstance(value, (int, float)) or
                   not math.isfinite(value) or value <= 0
                   for value in (raw_close, adjusted_close, factor)):
                raise ValueError(f'{identifier} {day}: nonpositive or missing price/factor')
            identity_error = abs(adjusted_close / (raw_close * factor) - 1)
            all_residuals.append(identity_error)
            maximum_identity_error = max(maximum_identity_error, identity_error)
            if previous is not None:
                total_daily_returns += 1
                prior_raw, prior_adjusted, prior_factor = previous
                comparison = price_comparison(prior_raw, raw_close, prior_adjusted,
                                              adjusted_close, prior_factor, factor)
                return_difference = abs(comparison['adjusted_to_raw_gross_return_ratio'] - 1)
                factor_change = abs(comparison['factor_ratio'] - 1)
                if (return_difference > .01) != (factor_change > .01):
                    raise ValueError(f'{identifier} {day}: factor change does not explain return difference')
                if return_difference > .01:
                    count_over_one_percent += 1
                    events.append({'instrument_id': identifier, 'date': day,
                                   **comparison})
            previous = (raw_close, adjusted_close, factor)
        per_stock.append({'instrument_id': identifier, 'matched_days': len(raw_rows),
                          'max_abs_close_identity_error': maximum_identity_error,
                          'daily_return_difference_over_1pct_count': count_over_one_percent})
    if len(all_residuals) != 36 * 727 or max(all_residuals) > 1e-6:
        raise ValueError('Qlib close does not match raw close times factor within tolerance')
    if any(item['factor_explanation_residual'] > 1e-6 for item in events):
        raise ValueError('factor fails to explain a large return discrepancy')
    sorted_residuals = sorted(all_residuals)
    events.sort(key=lambda item: abs(item['adjusted_to_raw_gross_return_ratio'] - 1),
                reverse=True)
    return {
        'study_version': VERSION,
        'status': 'verified_mathematical_price_identity_not_executable_returns',
        'frozen_stress_report': {'path': str(stress_path.resolve().relative_to(BASE)),
                                 'sha256': hashlib.sha256(stress_bytes).hexdigest()},
        'source_lineage': {'qlib_release': lineage['release_tag'],
                           'qlib_manifest_sha256': lineage['manifest_sha256'],
                           'qlib_archive_sha256': lineage['archive_sha256'],
                           'raw_archive_path': str(db_path.resolve().relative_to(BASE)),
                           'raw_archive_run_id': run_id,
                           'raw_price_source': 'baostock_daily',
                           'raw_adjustment': 'none'},
        'period': {'first': START, 'last': END},
        'pool_count': 12, 'stock_count': 36,
        'matched_stock_days': len(all_residuals),
        'matched_stock_daily_returns': total_daily_returns,
        'max_abs_relative_error_adjusted_close_vs_raw_close_times_factor': max(all_residuals),
        'p99_abs_relative_error': sorted_residuals[int(.99 * len(sorted_residuals))],
        'return_difference_over_1pct_count': len(events),
        'return_difference_over_5pct_count': sum(
            abs(item['adjusted_to_raw_gross_return_ratio'] - 1) > .05 for item in events),
        'stocks_with_return_difference_over_1pct': sum(
            item['daily_return_difference_over_1pct_count'] > 0 for item in per_stock),
        'over_1pct_return_difference_dates_equal_factor_change_dates': True,
        'factor_jump_events_over_1pct': events,
        'largest_return_differences': events[:20],
        'per_stock': per_stock,
        'limitations': [
            'This checks price conversion in a 2026 Qlib release against a BaoStock archive fetched later; it is not historical point-in-time factor availability.',
            'A mathematical price identity is not independent cross-provider validation or a corporate-action cash-flow ledger.',
            'Raw-close returns across factor jumps are not directly comparable to adjusted-close returns; neither proves executable total returns.',
            'The 36 stocks were selected using complete future histories in the frozen study, so this does not fix survivor or uninterrupted-trading bias.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stress-report', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-deterministic-pool-stress-v2.json'))
    parser.add_argument('--db', type=Path, default=Path('data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--qlib-root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-raw-price-factor-audit-v1.json'))
    args = parser.parse_args()
    report = audit(args.stress_report, args.db, args.qlib_root, args.qlib_manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()), 'matched_days': report['matched_stock_days'],
                      'large_return_differences': report['return_difference_over_1pct_count'],
                      'status': report['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
