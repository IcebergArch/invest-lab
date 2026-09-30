"""Recompute frozen 2021-anchor median bounds after pure-halt replay.

The 6144 original cases remain the denominator. Only source-verified,
liquidated suspension cases replace previously unknown outcomes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from qlib_2021_anchor_missingness_bounds import median_bounds

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-reconciled-median-bounds-v1'
ORIGINAL_COMPLETE = 'fully_liquidated_conditional_proxy'
HALT_COMPLETE = 'fully_liquidated_halt_mark_proxy'
COMPLETE = {ORIGINAL_COMPLETE, HALT_COMPLETE}


def merge_cases(conditional: list[dict], halt: list[dict],
                eligibility: dict[str, dict]) -> list[dict]:
    original = {case['case_id']: case for case in conditional}
    if len(original) != len(conditional):
        raise ValueError('duplicate conditional case ID')
    replacements = {}
    for replay in halt:
        case_id = replay['case_id']
        if case_id in replacements or case_id not in original or case_id not in eligibility:
            raise ValueError('duplicate or unknown halt replay case ID')
        source = original[case_id]
        window = eligibility[case_id]
        if (source['status'] != 'daily_raw_close_proxy_unavailable'
                or not window['has_halt'] or window['has_supplier_outdate_gap']
                or window['has_st'] or not window['entry_all_three_normal_non_st']
                or not window['exit_all_three_normal_non_st']
                or any(replay[key] != source[key] for key in (
                    'segment', 'horizon_sessions', 'seed', 'pool'))):
            raise ValueError('halt replay case is outside predeclared eligible subset')
        if replay['status'] == HALT_COMPLETE:
            policies = replay['returns']
            if (set(policies) != {'sma', 'same_exposure_equal', 'passive_fixed_80'}
                    or any(policies[p]['status'] != 'fully_liquidated_raw_action_proxy'
                           or not all(math.isfinite(policies[p][field])
                                      for field in ('net_return', 'max_drawdown'))
                           for p in policies)):
                raise ValueError('halt replay does not have three liquidated policies')
            replacements[case_id] = {**source, 'status': HALT_COMPLETE,
                                     'returns': policies,
                                     'halted_stock_days': replay['halted_stock_days']}
        elif replay['status'] == 'unsettled_corporate_action_at_horizon':
            replacements[case_id] = {**source, 'status': replay['status'],
                                     'returns': None}
        else:
            raise ValueError('unexpected halt replay status')
    return [replacements.get(case['case_id'], case) for case in conditional]


def summarize(cases: list[dict]) -> dict:
    groups = defaultdict(list)
    for case in cases:
        groups[(case['segment'], case['horizon_sessions'], case['seed'])].append(case)
    if len(cases) != 6144 or len(groups) != 8:
        raise ValueError('frozen case denominator differs')
    out = {}
    for key in sorted(groups):
        group = groups[key]
        if len(group) != 768:
            raise ValueError('frozen group denominator differs')
        scenarios = {}
        for name, include_factor_overlap in (
                ('unresolved_settlement_only', True),
                ('also_unknown_factor_date_overlap', False)):
            observed = [case for case in group
                        if case['status'] in COMPLETE
                        and (include_factor_overlap or not case['factor_anomaly_overlap'])]
            if any(case['returns'] is None for case in observed):
                raise ValueError('complete case lacks policy outcomes')
            result = {}
            for name_field, field in (('paired_net_return', 'net_return'),
                                     ('paired_max_drawdown', 'max_drawdown')):
                values = [case['returns']['sma'][field]
                          - case['returns']['same_exposure_equal'][field]
                          for case in observed]
                bound = median_bounds(values, len(group))
                bound['observed_median'] = median(values)
                bound['sign'] = ('strictly_positive' if bound['lower'] > 0
                                 else 'strictly_negative' if bound['upper'] < 0
                                 else 'undetermined')
                result[name_field] = bound
            scenarios[name] = result
        out[':'.join(map(str, key))] = {
            'total_windows': len(group),
            'status_counts': dict(Counter(case['status'] for case in group)),
            'factor_date_overlap_in_complete': sum(
                case['status'] in COMPLETE and case['factor_anomaly_overlap']
                for case in group),
            'scenarios': scenarios,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-reconciled-median-bounds-v1.json'))
    args = parser.parse_args()
    names = {
        'conditional': 'qlib-2021-anchor-conditional-raw-returns-v1.json',
        'halt': 'qlib-2021-anchor-pure-halt-replay-v1.json',
        'window': 'qlib-2021-anchor-random-window-eligibility-v1.json',
    }
    payloads = {key: (args.source_dir / name).read_bytes() for key, name in names.items()}
    reports = {key: json.loads(data) for key, data in payloads.items()}
    if (reports['conditional']['case_count'] != 6144
            or reports['window']['case_count'] != 6144
            or reports['halt']['eligible_pure_halt_cases'] != 501
            or reports['halt']['source_lineage']['conditional_report_sha256']
            != hashlib.sha256(payloads['conditional']).hexdigest()
            or reports['halt']['source_lineage']['window_report_sha256']
            != hashlib.sha256(payloads['window']).hexdigest()
            or reports['halt']['script_sha256'] != hashlib.sha256((
                BASE / 'scripts/qlib_2021_anchor_halt_replay.py').read_bytes()).hexdigest()):
        raise ValueError('reconciled source lineage differs')
    eligibility = {case['case_id']: case for case in reports['window']['cases']}
    if len(eligibility) != 6144:
        raise ValueError('duplicate window case ID')
    merged = merge_cases(reports['conditional']['cases'], reports['halt']['cases'],
                         eligibility)
    statuses = Counter(case['status'] for case in merged)
    if (len(merged) != 6144 or len(reports['halt']['cases']) != 501
            or statuses[HALT_COMPLETE] != 500
            or statuses[ORIGINAL_COMPLETE] != 5063):
        raise ValueError('reconciled denominator or recovery count differs')
    report = {
        'study_version': VERSION,
        'status': 'retrospective_halt_proxy_partial_identification_not_stable_baseline',
        'source_lineage': {key + '_report_sha256': hashlib.sha256(data).hexdigest()
                           for key, data in payloads.items()},
        'case_count': len(merged),
        'status_counts': dict(statuses),
        'recovered_halt_case_ids': sorted(
            case['case_id'] for case in merged if case['status'] == HALT_COMPLETE),
        'median_bounds_method': ('Unknown case outcomes are assigned below all '
                                 'observed values for the lower median and above '
                                 'all observed values for the upper median. The '
                                 'known factor-date mismatch is separately treated '
                                 'as unknown in the conservative scenario.'),
        'groups': summarize(merged),
        'limitations': [
            'Suspended names are marked at supplier-carried previous close and cannot trade until their next normal session.',
            'Normal-day fills use the raw closing price without price-limit queues, volume, slippage or tax.',
            'ST, entry/exit suspension, post-outDate, pending corporate claims and fractional bonus cases remain unknown.',
            'Median bounds do not cover tails, means, causal effects or independent success probabilities.',
            'The 2021 member label is not confirmed as official historical point-in-time membership.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'statuses': report['status_counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
