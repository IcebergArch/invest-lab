"""Join large Qlib factor jumps to later BaoStock corporate-action responses.

The join verifies source consistency, not historical publication or total return.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
VERSION = 'pool-corporate-action-alignment-v1'


def join_events(price_report: dict, snapshots: list[dict]) -> tuple[list[dict], int]:
    if price_report.get('study_version') != 'qlib-raw-price-factor-audit-v1':
        raise ValueError('unexpected price audit version')
    if len(snapshots) != 3 or [item.get('operate_year') for item in snapshots] != [2021, 2022, 2023]:
        raise ValueError('expected three ordered operation years')
    all_actions: dict[tuple[str, str], dict] = {}
    stock_sets = []
    for snap in snapshots:
        if (snap.get('snapshot_version') != 'baostock-36-pool-corporate-actions-v1'
                or snap.get('status') != 'provider_response_captured_not_pit'
                or snap.get('requested_stock_count') != 36
                or snap.get('year_type') != 'operate'):
            raise ValueError('corporate action snapshot has unexpected source contract')
        responses = snap['responses']
        if len(responses) != 36 or snap['response_row_count'] != sum(
                response['row_count'] for response in responses):
            raise ValueError('corporate action snapshot rows do not reconcile')
        stocks = {response['instrument_id'] for response in responses}
        if len(stocks) != 36:
            raise ValueError('corporate action snapshot duplicates a stock')
        stock_sets.append(stocks)
        for response in responses:
            if response['row_count'] != len(response['rows']):
                raise ValueError('corporate action response count mismatch')
            for action in response['rows']:
                day = action['dividOperateDate']
                if not day or not day.startswith(str(snap['operate_year'])):
                    raise ValueError('corporate action has missing or wrong operation year')
                key = (response['instrument_id'], day)
                if key in all_actions:
                    raise ValueError(f'duplicate action for stock and operation date {key}')
                all_actions[key] = action
    if stock_sets[0] != stock_sets[1] or stock_sets[1] != stock_sets[2]:
        raise ValueError('stock sets differ across yearly supplier snapshots')
    if price_report['stock_count'] != 36:
        raise ValueError('price audit stock count differs from supplier snapshots')
    jumps = price_report['factor_jump_events_over_1pct']
    if len(jumps) != price_report['return_difference_over_1pct_count']:
        raise ValueError('large factor jump count differs from price audit')
    jump_keys = {(item['instrument_id'], item['date']) for item in jumps}
    if len(jump_keys) != len(jumps) or any(key[0] not in stock_sets[0] for key in jump_keys):
        raise ValueError('duplicate or outside-stock factor jump')
    missing = sorted(jump_keys - all_actions.keys())
    if missing:
        raise ValueError(f'{len(missing)} large factor jumps lack same-day supplier actions: {missing[:3]}')
    joined = []
    for jump in jumps:
        action = all_actions[(jump['instrument_id'], jump['date'])]
        joined.append({
            'instrument_id': jump['instrument_id'], 'operation_date': jump['date'],
            'adjusted_to_raw_gross_return_ratio': jump['adjusted_to_raw_gross_return_ratio'],
            'factor_ratio': jump['factor_ratio'],
            'provider_record': {field: action[field] for field in (
                'dividPlanAnnounceDate', 'dividRegistDate', 'dividOperateDate',
                'dividPayDate', 'dividCashStock', 'dividCashPsBeforeTax',
                'dividStocksPs', 'dividReserveToStockPs')},
        })
    return joined, len(all_actions) - len(jump_keys)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--price-report', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-raw-price-factor-audit-v1.json'))
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/pool-corporate-action-alignment-v1.json'))
    args = parser.parse_args()
    price_bytes = args.price_report.read_bytes()
    price = json.loads(price_bytes)
    sources = []
    snapshots = []
    for year in (2021, 2022, 2023):
        path = args.source_dir / f'baostock-36-pool-corporate-actions-{year}-v1.json'
        payload = path.read_bytes()
        snapshots.append(json.loads(payload))
        sources.append({'operate_year': year, 'path': str(path.resolve().relative_to(BASE)),
                        'sha256': hashlib.sha256(payload).hexdigest(),
                        'supplier_package_version': snapshots[-1]['source']['package_version'],
                        'collected_at': snapshots[-1]['collected_at']})
    joined, smaller = join_events(price, snapshots)
    report = {
        'study_version': VERSION,
        'status': 'all_large_factor_jumps_match_later_supplier_operations',
        'price_audit': {'path': str(args.price_report.resolve().relative_to(BASE)),
                        'sha256': hashlib.sha256(price_bytes).hexdigest()},
        'supplier_snapshots': sources,
        'large_factor_jump_count': len(joined),
        'same_day_supplier_action_match_count': len(joined),
        'supplier_action_dates_without_large_factor_jump': smaller,
        'total_supplier_action_dates': len(joined) + smaller,
        'matched_events': joined,
        'historical_point_in_time_publication_verified': False,
        'total_return_or_executable_pnl_verified': False,
        'limitations': [
            'The 86 action rows are BaoStock responses fetched in 2026, not independently verified historical publications.',
            'Only factor/return differences above 1% are joined here; smaller actions and factor rounding need further audit.',
            'Matching operation dates does not model share delivery, cash dividend, tax, rights, order timing, or total return.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'matched': len(joined), 'supplier_action_dates': len(joined) + smaller,
                      'status': report['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
