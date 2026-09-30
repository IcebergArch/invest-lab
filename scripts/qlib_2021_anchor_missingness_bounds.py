"""Sharp median bounds when anchor-window outcomes remain unresolved.

Unresolved outcomes may take any value. Bounds concern the median of the
frozen 768 cases per group, not a causal effect or an independent win rate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-missingness-bounds-v1'
SOURCE_VERSION = 'qlib-2021-anchor-conditional-raw-returns-v1'
COMPLETE = 'fully_liquidated_conditional_proxy'


def median_bounds(values: list[float], total: int) -> dict:
    """Return sharp finite bounds for an even-sized full-sample median.

    Lower assigns every missing value below all observed values, upper above.
    Both middle order statistics must then fall inside the observed values.
    """
    if total <= 0 or total % 2 or len(values) > total:
        raise ValueError('expected a positive even total and no excess observations')
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in values):
        raise ValueError('observed outcomes must be finite')
    ordered = sorted(values)
    missing = total - len(ordered)
    left, right = total // 2 - 1, total // 2
    if left - missing < 0 or right >= len(ordered):
        raise ValueError('too many missing outcomes for finite median bounds')
    return {
        'observed': len(ordered),
        'unknown': missing,
        'lower': (ordered[left - missing] + ordered[right - missing]) / 2,
        'upper': (ordered[left] + ordered[right]) / 2,
    }


def paired_value(case: dict, field: str) -> float:
    returns = case['returns']
    return returns['sma'][field] - returns['same_exposure_equal'][field]


def analyze(source: dict) -> dict:
    if source.get('study_version') != SOURCE_VERSION or source.get('case_count') != 6144:
        raise ValueError('conditional source version or case count differs')
    groups = defaultdict(list)
    for case in source['cases']:
        groups[(case['segment'], case['horizon_sessions'], case['seed'])].append(case)
    expected = {(segment, horizon, seed)
                for segment in ('early', 'later')
                for horizon in (126, 252) for seed in (202, 404)}
    if set(groups) != expected or len({case['case_id'] for case in source['cases']}) != 6144:
        raise ValueError('frozen group or case identity differs')
    result = {}
    for segment, horizon, seed in sorted(groups):
        cases = groups[(segment, horizon, seed)]
        if len(cases) != 768:
            raise ValueError('frozen group denominator differs')
        statuses = Counter(case['status'] for case in cases)
        if any((case['returns'] is None) != (case['status'] != COMPLETE)
               for case in cases):
            raise ValueError('source status and return availability differ')
        if set(statuses) - {COMPLETE, 'daily_raw_close_proxy_unavailable',
                            'unsupported_fractional_bonus_settlement'}:
            raise ValueError('new case status requires review')
        ordinary = [case for case in cases if case['status'] == COMPLETE]
        conservative = [case for case in ordinary if not case['factor_anomaly_overlap']]
        scenarios = {}
        for name, observed in (('unresolved_settlement_only', ordinary),
                               ('also_unknown_factor_date_overlap', conservative)):
            scenarios[name] = {
                'paired_net_return_median_bounds': median_bounds(
                    [paired_value(case, 'net_return') for case in observed], len(cases)),
                'paired_max_drawdown_median_bounds': median_bounds(
                    [paired_value(case, 'max_drawdown') for case in observed], len(cases)),
            }
            for key in ('paired_net_return_median_bounds',
                        'paired_max_drawdown_median_bounds'):
                bound = scenarios[name][key]
                bound['sign'] = ('strictly_positive' if bound['lower'] > 0
                                 else 'strictly_negative' if bound['upper'] < 0
                                 else 'undetermined')
        result[f'{segment}:{horizon}:{seed}'] = {
            'total_windows': len(cases),
            'status_counts': dict(statuses),
            'factor_date_overlap_in_complete': len(ordinary) - len(conservative),
            'scenarios': scenarios,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-conditional-raw-returns-v1.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-missingness-bounds-v1.json'))
    args = parser.parse_args()
    source_bytes = args.source.read_bytes()
    source = json.loads(source_bytes)
    source_script = BASE / 'scripts/qlib_2021_anchor_conditional_returns.py'
    if source.get('script_sha256') != hashlib.sha256(source_script.read_bytes()).hexdigest():
        raise ValueError('conditional replay source script changed')
    report = {
        'study_version': VERSION,
        'status': 'retrospective_partial_identification_not_stable_baseline',
        'source': {'path': str(args.source.resolve().relative_to(BASE)),
                   'sha256': hashlib.sha256(source_bytes).hexdigest()},
        'method': ('For each 768-case group, assign all unresolved outcomes below '
                   'all observed outcomes for the lower median, then above them '
                   'for the upper median. This gives sharp finite order-statistic '
                   'bounds without imputing P&L. A second scenario also treats '
                   'every complete window touching the known Qlib factor-date '
                   'mismatch as unknown.'),
        'paired_max_drawdown_sign': ('SMA max_drawdown minus same-exposure equal '
                                      'max_drawdown; negative means deeper SMA drawdown.'),
        'groups': analyze(source),
        'limitations': [
            'Bounds describe case-weighted medians only; they do not bound tails, means, or individual unresolved outcomes.',
            'Cases share calendar dates, securities, and seeds; these are not independent observations.',
            'The 2021 Qlib membership label is not verified official historical point-in-time membership.',
            'Observed outcomes use close-price fills, retrospective data, and incomplete execution assumptions.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()), 'groups': len(report['groups'])},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
