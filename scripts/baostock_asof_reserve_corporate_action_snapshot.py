"""Capture later BaoStock dividend/bonus records for the three as-of reserves.

These supplier responses are retrospective evidence, not historical first
publication times or proof that all corporate entitlements are represented.
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

from baostock_pool_corporate_action_snapshot_later import FIELDS

BASE = Path(__file__).resolve().parents[1]
VERSION = 'baostock-asof-reserve-corporate-actions-v1'
YEARS = tuple(range(2021, 2027))
EXPECTED = ('SH600886', 'SH601658', 'SH603658')


def selected_codes(report: dict) -> list[tuple[str, str]]:
    if (report.get('study_version') != 'qlib-2021-anchor-asof-reserve-coverage-v1'
            or report.get('script_sha256') != hashlib.sha256((
                BASE / 'scripts/qlib_2021_anchor_asof_reserve_audit.py').read_bytes()).hexdigest()
            or tuple(sorted(report.get('replacement_symbols_needing_raw_archive', []))) != EXPECTED):
        raise ValueError('as-of reserve source report changed')
    return [('stock:' + symbol[2:] + '.SH', 'sh.' + symbol[2:])
            for symbol in EXPECTED]


def capture(report_path: Path, interval: float, provider,
            package_version: str) -> dict:
    if not math.isfinite(interval) or interval < 1:
        raise ValueError('provider interval must be at least one second')
    source = report_path.read_bytes()
    codes = selected_codes(json.loads(source))
    login = provider.login()
    if login.error_code != '0':
        raise RuntimeError(f'BaoStock login failed: {login.error_code} {login.error_msg}')
    responses = []
    try:
        for year in YEARS:
            for identifier, code in codes:
                if responses:
                    time.sleep(interval)
                requested_at = datetime.now(timezone.utc).isoformat()
                result = provider.query_dividend_data(code, year=str(year),
                                                      yearType='operate')
                if result.error_code != '0' or list(result.fields) != FIELDS:
                    raise RuntimeError(f'{identifier} {year}: action query failed or fields changed: '
                                       f'{result.error_code} {result.error_msg}')
                rows = []
                while result.next():
                    values = result.get_row_data()
                    if len(values) != len(FIELDS) or values[0] != code:
                        raise ValueError(f'{identifier} {year}: malformed action row')
                    row = dict(zip(FIELDS, values))
                    if not row['dividOperateDate'].startswith(str(year)):
                        raise ValueError(f'{identifier} {year}: operation year mismatch')
                    rows.append(row)
                if result.error_code != '0':
                    raise RuntimeError(f'{identifier} {year}: action pagination failed')
                responses.append({'instrument_id': identifier, 'provider_code': code,
                                  'operate_year': year, 'requested_at': requested_at,
                                  'row_count': len(rows), 'rows': rows})
    finally:
        provider.logout()
    dependency = BASE / 'scripts/baostock_pool_corporate_action_snapshot_later.py'
    return {
        'snapshot_version': VERSION,
        'status': 'provider_response_captured_not_pit',
        'query': 'query_dividend_data', 'year_type': 'operate',
        'requested_stock_count': len(codes), 'requested_years': list(YEARS),
        'response_query_count': len(responses),
        'response_row_count': sum(item['row_count'] for item in responses),
        'fields': FIELDS, 'responses': responses,
        'source': {'provider': 'BaoStock',
                   'package_version': package_version,
                   'asof_reserve_report_path': str(report_path.resolve().relative_to(BASE)),
                   'asof_reserve_report_sha256': hashlib.sha256(source).hexdigest(),
                   'fields_dependency_sha256': hashlib.sha256(dependency.read_bytes()).hexdigest()},
        'collected_at': datetime.now(timezone.utc).isoformat(),
        'historical_point_in_time_publication_verified': False,
        'limitations': [
            'Supplier response is captured later; original historical publication times are unverified.',
            'Empty dividend responses do not prove absence of rights, mergers or other entitlements.',
        ],
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reserve-report', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-asof-reserve-coverage-v1.json'))
    parser.add_argument('--interval-seconds', type=float, default=1.5)
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/baostock-asof-reserve-corporate-actions-v1.json'))
    args = parser.parse_args()
    import baostock
    report = capture(args.reserve_report, args.interval_seconds, baostock,
                     importlib.metadata.version('baostock'))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'query_count': report['response_query_count'],
                      'action_rows': report['response_row_count']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
