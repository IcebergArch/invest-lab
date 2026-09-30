"""Audit a 2021-labelled CSI300 anchor sample without future data filters.

The 2026 Qlib release's date label is not verified official PIT membership.
This reports later feature loss, not trading returns or a valid live universe.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

from qlib_csi300_entry_eligibility import load_verified_source

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-pool-coverage-audit-v1'
ANCHOR = '2021-01-04'
LAST = '2026-09-28'
NONCE = '2021-01-04-csi300-anchor-v1'
STOCK_COUNT = 36
SEGMENTS = {'early': ('2021-01-04', '2023-12-29'),
            'later': ('2024-01-02', LAST)}


def anchor_symbols(calendar, masks, *, anchor: str = ANCHOR,
                   nonce: str = NONCE, count: int = STOCK_COUNT) -> tuple[list[str], int]:
    position = calendar.index(date.fromisoformat(anchor))
    members = [symbol for symbol, mask in masks.items() if mask[position]]
    if len(members) != 300 or count < 1 or count > len(members):
        raise ValueError('2021 label member count or selection size differs')
    ranked = sorted(members, key=lambda symbol: (
        hashlib.sha256(f'{nonce}:{symbol}'.encode()).digest(), symbol))
    return ranked[:count], len(members)


def audit(calendar, masks, features, source_lineage: dict,
          stress_path: Path) -> dict:
    chosen, members = anchor_symbols(calendar, masks)
    if len(chosen) != len(set(chosen)) or len(chosen) != STOCK_COUNT:
        raise ValueError('anchor selection duplicates a name')
    stress_bytes = stress_path.read_bytes()
    stress = json.loads(stress_bytes)
    if stress.get('study_version') != 'qlib-deterministic-pool-stress-v2':
        raise ValueError('comparison stress report differs')
    old_symbols = {('SH' if item.endswith('.SH') else 'SZ') + item[6:12]
                   for pool in stress['pool_symbols'].values() for item in pool}
    if len(old_symbols) != STOCK_COUNT:
        raise ValueError('comparison survivor pool dimensions differ')
    per_symbol = []
    segment_sessions = {}
    for segment, (first, last) in SEGMENTS.items():
        segment_sessions[segment] = [i for i, day in enumerate(calendar)
                                     if first <= day.isoformat() <= last]
        if not segment_sessions[segment]:
            raise ValueError(f'{segment}: no release sessions')
    for symbol in chosen:
        by_segment = {}
        for segment, indices in segment_sessions.items():
            missing = []
            for index in indices:
                for field in ('close', 'volume'):
                    value = features[symbol][field].at(index)
                    if (not isinstance(value, (int, float)) or
                            not math.isfinite(value) or value <= 0):
                        missing.append((calendar[index].isoformat(), field))
            missing_days = sorted({day for day, _ in missing})
            by_segment[segment] = {
                'expected_sessions': len(indices),
                'days_missing_positive_close_or_volume': len(missing_days),
                'missing_close_count': sum(field == 'close' for _, field in missing),
                'missing_volume_count': sum(field == 'volume' for _, field in missing),
                'first_missing_day': missing_days[0] if missing_days else None,
                'last_missing_day': missing_days[-1] if missing_days else None,
                'full_positive_price_volume_coverage': not missing_days,
            }
        per_symbol.append({'symbol': symbol, 'segments': by_segment})
    totals = {}
    for segment in SEGMENTS:
        rows = [item['segments'][segment] for item in per_symbol]
        totals[segment] = {
            'sessions_per_symbol': len(segment_sessions[segment]),
            'selected_symbols': len(rows),
            'symbols_with_full_positive_price_volume_coverage': sum(
                row['full_positive_price_volume_coverage'] for row in rows),
            'symbols_with_any_missing_day': sum(
                not row['full_positive_price_volume_coverage'] for row in rows),
            'total_symbol_days_missing_price_or_volume': sum(
                row['days_missing_positive_close_or_volume'] for row in rows),
        }
    return {
        'study_version': VERSION,
        'status': 'retrospective_anchor_coverage_not_pit_membership_or_returns',
        'anchor_label': ANCHOR,
        'anchor_membership_source': '2026 Qlib release csi300.txt date-labelled intervals',
        'anchor_membership_official_pit_confirmed': False,
        'selection_nonce': NONCE,
        'selection_rule': 'hash rank 2021-labelled 300 members before inspecting any future close/volume; take first 36',
        'member_count_at_anchor': members,
        'selected_symbols_in_hash_order': chosen,
        'segment_summary': totals,
        'per_symbol': per_symbol,
        'comparison_survivor_pool': {
            'frozen_stress_report_path': str(stress_path.resolve().relative_to(BASE)),
            'frozen_stress_report_sha256': hashlib.sha256(stress_bytes).hexdigest(),
            'symbol_count': len(old_symbols),
            'overlap_with_anchor_selection': len(set(chosen) & old_symbols),
            'selection_difference': 'frozen stress requires complete positive close and volume through 2026; anchor sample imposes no future data filter',
        },
        'source_lineage': source_lineage,
        'limitations': [
            'Qlib 2026 date-labelled CSI300 membership is known to lag the official 2021 June change; its 2021 January list is not independently confirmed official PIT.',
            'This compares availability of selected features, not delisting, tradability, or investment performance.',
            'Missing volume may mean a suspension or missing/revised provider data; no cause is assigned without raw-status evidence.',
            'The two stock selections use different parent universes and hash nonces, so their overlap is descriptive, not a causal performance estimate.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path,
                        default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--manifest', type=Path,
                        default=Path('data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--stress-report', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-deterministic-pool-stress-v2.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-pool-coverage-audit-v1.json'))
    args = parser.parse_args()
    calendar, masks, features, lineage = load_verified_source(args.root, args.manifest)
    report = audit(calendar, masks, features, lineage, args.stress_report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'anchor_names': len(report['selected_symbols_in_hash_order']),
                      'early_missing_names': report['segment_summary']['early']['symbols_with_any_missing_day'],
                      'later_missing_names': report['segment_summary']['later']['symbols_with_any_missing_day']},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
