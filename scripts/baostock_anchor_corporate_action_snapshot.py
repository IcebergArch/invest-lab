"""Capture one operation year of BaoStock actions for the 2021 anchor 36.

Responses are later supplier snapshots, not point-in-time publication evidence.
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
VERSION = 'baostock-2021-anchor-corporate-actions-v1'


def selected_codes(anchor: dict) -> list[tuple[str, str]]:
    if (anchor.get('study_version') != 'qlib-2021-anchor-pool-coverage-audit-v1'
            or len(anchor.get('selected_symbols_in_hash_order', [])) != 36):
        raise ValueError('unexpected 2021 anchor report')
    result = []
    for symbol in anchor['selected_symbols_in_hash_order']:
        if (len(symbol) != 8 or symbol[:2] not in ('SH', 'SZ')
                or not symbol[2:].isdigit()):
            raise ValueError(f'invalid selected symbol {symbol}')
        result.append(('stock:' + symbol[2:] + '.' + symbol[:2],
                       symbol[:2].lower() + '.' + symbol[2:]))
    if len({item[0] for item in result}) != 36:
        raise ValueError('anchor selected duplicate symbol')
    return result


def capture(year: int, anchor_path: Path, interval: float, provider) -> dict:
    if year not in range(2021, 2027) or not math.isfinite(interval) or interval < 1:
        raise ValueError('year or interval out of range')
    anchor_bytes = anchor_path.read_bytes()
    anchor = json.loads(anchor_bytes)
    if anchor.get('script_sha256') != hashlib.sha256((
            BASE / 'scripts/qlib_2021_anchor_pool_audit.py').read_bytes()).hexdigest():
        raise ValueError('anchor audit code differs from selected universe source')
    codes = selected_codes(anchor)
    login = provider.login()
    if login.error_code != '0':
        raise RuntimeError(f'BaoStock login failed: {login.error_code} {login.error_msg}')
    collected = []
    try:
        for index, (identifier, code) in enumerate(codes):
            if index:
                time.sleep(interval)
            requested_at = datetime.now(timezone.utc).isoformat()
            response = provider.query_dividend_data(code, year=str(year),
                                                    yearType='operate')
            if response.error_code != '0' or list(response.fields) != FIELDS:
                raise RuntimeError(f'{identifier}: query failed or fields changed: '
                                   f'{response.error_code} {response.error_msg}')
            rows = []
            while response.next():
                values = response.get_row_data()
                if len(values) != len(FIELDS) or values[0] != code:
                    raise ValueError(f'{identifier}: malformed action row')
                item = dict(zip(FIELDS, values))
                if (item['dividOperateDate'] and
                        not item['dividOperateDate'].startswith(str(year))):
                    raise ValueError(f'{identifier}: action outside operation year')
                rows.append(item)
            if response.error_code != '0':
                raise RuntimeError(f'{identifier}: action pagination failed')
            collected.append({'instrument_id': identifier,
                              'provider_code': code, 'requested_at': requested_at,
                              'row_count': len(rows), 'rows': rows})
    finally:
        provider.logout()
    dependency = BASE / 'scripts/baostock_pool_corporate_action_snapshot_later.py'
    return {
        'snapshot_version': VERSION,
        'status': 'provider_response_captured_not_pit',
        'query': 'query_dividend_data', 'year_type': 'operate',
        'operate_year': year, 'requested_stock_count': 36,
        'response_row_count': sum(row['row_count'] for row in collected),
        'fields': FIELDS, 'responses': collected,
        'source': {'provider': 'BaoStock',
                   'package_version': importlib.metadata.version('baostock'),
                   'anchor_report_path': str(anchor_path.resolve().relative_to(BASE)),
                   'anchor_report_sha256': hashlib.sha256(anchor_bytes).hexdigest(),
                   'fields_dependency_sha256': hashlib.sha256(dependency.read_bytes()).hexdigest()},
        'collected_at': datetime.now(timezone.utc).isoformat(),
        'historical_point_in_time_publication_verified': False,
        'limitations': [
            'Supplier response is captured in 2026; historical publication timestamps are not independently verified.',
            'Empty responses do not prove that no rights, merger consideration, or other action existed.',
        ],
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--anchor-report', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-pool-coverage-audit-v1.json'))
    parser.add_argument('--interval-seconds', type=float, default=1.5)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    import baostock
    report = capture(args.year, args.anchor_report, args.interval_seconds, baostock)
    out = args.out or Path('studies/random-entry-baseline-v1') / (
        f'baostock-2021-anchor-corporate-actions-{args.year}-v1.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(out.resolve()), 'year': args.year,
                      'action_rows': report['response_row_count']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
