"""Capture BaoStock dividend/bonus events for the frozen 36-stock research pool.

The response is a later supplier snapshot, not point-in-time availability evidence.
One bounded run captures one operation year and does not alter the market archive.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = 'baostock-36-pool-corporate-actions-v1'
STRESS_VERSION = 'qlib-deterministic-pool-stress-v2'
FIELDS = ['code', 'dividPreNoticeDate', 'dividAgmPumDate', 'dividPlanAnnounceDate',
          'dividPlanDate', 'dividRegistDate', 'dividOperateDate', 'dividPayDate',
          'dividStockMarketDate', 'dividCashPsBeforeTax', 'dividCashPsAfterTax',
          'dividStocksPs', 'dividCashStock', 'dividReserveToStockPs']
BASE = Path(__file__).resolve().parents[1]


def selected_codes(stress: dict) -> list[tuple[str, str]]:
    if stress.get('study_version') != STRESS_VERSION:
        raise ValueError('unexpected frozen stress report version')
    pools = stress['pool_symbols']
    if len(pools) != 12 or any(len(pool) != 3 for pool in pools.values()):
        raise ValueError('expected twelve frozen three-stock pools')
    ids = sorted({symbol for pool in pools.values() for symbol in pool})
    if len(ids) != 36:
        raise ValueError('expected 36 distinct stock codes')
    result = []
    for identifier in ids:
        if (not identifier.startswith('stock:') or len(identifier) != 15
                or identifier[6:12].isdigit() is False
                or identifier[12:] not in ('.SH', '.SZ')):
            raise ValueError(f'unsupported stock ID {identifier}')
        provider_code = identifier[13:].lower() + '.' + identifier[6:12]
        result.append((identifier, provider_code))
    return result


def capture(year: int, stress_path: Path, interval: float, provider) -> dict:
    if year not in (2021, 2022, 2023) or not math.isfinite(interval) or interval < 1.0:
        raise ValueError('select 2021, 2022, or 2023 and interval >= 1 second')
    stress_bytes = stress_path.read_bytes()
    codes = selected_codes(json.loads(stress_bytes))
    login = provider.login()
    if login.error_code != '0':
        raise RuntimeError(f'BaoStock login failed: {login.error_code} {login.error_msg}')
    collected = []
    try:
        for index, (identifier, code) in enumerate(codes):
            if index:
                time.sleep(interval)
            requested_at = datetime.now(timezone.utc).isoformat()
            result = provider.query_dividend_data(code, year=str(year), yearType='operate')
            if result.error_code != '0' or list(result.fields) != FIELDS:
                raise RuntimeError(f'{identifier}: query failed or fields changed: '
                                   f'{result.error_code} {result.error_msg} {result.fields}')
            rows = []
            while result.next():
                row = result.get_row_data()
                if len(row) != len(FIELDS) or row[0] != code:
                    raise ValueError(f'{identifier}: malformed dividend row')
                item = dict(zip(FIELDS, row))
                if item['dividOperateDate'] and not item['dividOperateDate'].startswith(str(year)):
                    raise ValueError(f'{identifier}: operation date outside requested year')
                rows.append(item)
            if result.error_code != '0':
                raise RuntimeError(f'{identifier}: query pagination failed: {result.error_code}')
            collected.append({'instrument_id': identifier, 'provider_code': code,
                              'requested_at': requested_at, 'row_count': len(rows),
                              'rows': rows})
    finally:
        provider.logout()
    return {
        'snapshot_version': VERSION, 'status': 'provider_response_captured_not_pit',
        'query': 'query_dividend_data', 'year_type': 'operate',
        'operate_year': year, 'requested_stock_count': len(codes),
        'response_row_count': sum(item['row_count'] for item in collected),
        'fields': FIELDS, 'responses': collected,
        'source': {'provider': 'BaoStock',
                   'package_version': importlib.metadata.version('baostock'),
                   'stress_report': str(stress_path.resolve().relative_to(BASE)),
                   'stress_report_sha256': hashlib.sha256(stress_bytes).hexdigest()},
        'collected_at': datetime.now(timezone.utc).isoformat(),
        'historical_point_in_time_publication_verified': False,
        'notes': [
            'These are 2026 supplier query results for historical operation years; announcement and record dates are data fields, not independently verified publication timestamps.',
            'An empty provider response does not independently prove no corporate action occurred.',
            'Cash taxation, rights subscription, and order-level holdings are not modeled here.'
        ],
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--stress-report', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-deterministic-pool-stress-v2.json'))
    parser.add_argument('--interval-seconds', type=float, default=1.5)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    import baostock
    report = capture(args.year, args.stress_report, args.interval_seconds, baostock)
    out = args.out or Path('studies/random-entry-baseline-v1') / (
        f'baostock-36-pool-corporate-actions-{args.year}-v1.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(out.resolve()), 'year': args.year,
                      'stock_count': report['requested_stock_count'],
                      'row_count': report['response_row_count'],
                      'status': report['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
