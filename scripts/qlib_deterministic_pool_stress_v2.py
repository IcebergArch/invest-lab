"""Retrospective, outcome-blind stock-pool stress check for the frozen SMA policy.

This is deliberately a rejection check, not an estimate of live A-share alpha:
Qlib's 2026 release lacks a historical investable-universe snapshot and the
complete-history filter conditions on surviving, uninterrupted stocks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

from quant_lab.qlib_archive import parse_calendar, parse_instruments
from quant_lab.qlib_local import _load_index, _verified_file, read_stock
from qlib_cross_pool_audit import numeric_summary, sma_targets
from random_entry_baseline import (cash_matched_hold_episode, entry_indices,
                                   episode, exposure_matched_targets,
                                   hold_episode)

VERSION = 'qlib-deterministic-pool-stress-v2'
RELEASE_TAG = '2026-09-28'
FIRST = '2020-09-01'
ENTRY_FIRST = '2021-01-04'
LAST = '2026-09-28'
SELECTION_NONCE = 'qlib-deterministic-pool-stress-v1:eligible-stock-order'
EXCLUDED = frozenset(('SH603993', 'SZ000338', 'SZ000977', 'SZ002475', 'SZ002594', 'SZ002714'))
SEGMENTS = {'early': (ENTRY_FIRST, '2023-12-29'), 'later': ('2024-01-02', LAST)}
HORIZONS = (126, 252)
SEEDS = (202, 404)
SAMPLE_COUNT = 64
POOL_SIZE = 3
POOL_COUNT = 12
FEE = .0005
STRESS_FEE = .0015


def fingerprint(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def ordered_candidates(instruments: Mapping[str, Sequence[tuple[date, date]]]) -> list[str]:
    """Select by predeclared symbol hash, before reading any returns."""
    first, last = date.fromisoformat(FIRST), date.fromisoformat(LAST)
    eligible = [symbol for symbol, spans in instruments.items()
                if symbol.startswith(('SH', 'SZ')) and symbol not in EXCLUDED
                and any(begin <= first and finish >= last for begin, finish in spans)]
    return sorted(eligible, key=lambda symbol: (
        hashlib.sha256((SELECTION_NONCE + ':' + symbol).encode()).hexdigest(), symbol))


def load_pools(root: Path, manifest: Path, *, pool_count: int = POOL_COUNT,
               pool_size: int = POOL_SIZE) -> tuple[list[str], dict[str, dict[str, list[float]]], dict]:
    if pool_count < 1 or pool_size < 2:
        raise ValueError('invalid deterministic pool dimensions')
    root = root.resolve()
    index = _load_index(root, manifest, RELEASE_TAG)
    with _verified_file(root, index, 'qlib_bin/instruments/all.txt') as stream:
        instruments = parse_instruments(stream.read())
    with _verified_file(root, index, 'qlib_bin/calendars/day.txt') as stream:
        calendar = parse_calendar(stream.read())
    dates = [day.isoformat() for day in calendar if FIRST <= day.isoformat() <= LAST]
    if not dates or dates[0] != FIRST or dates[-1] != LAST:
        raise ValueError('published Qlib calendar differs from frozen window')
    selected: list[tuple[str, list[float]]] = []
    attempted: list[dict] = []
    for symbol in ordered_candidates(instruments):
        if len(selected) == pool_count * pool_size:
            break
        payload = read_stock(root, manifest, RELEASE_TAG, symbol, start=FIRST, end=LAST,
                             fields=('close', 'volume'), limit=5000)
        rows = payload['rows']
        reason = None
        if (payload.get('adjustment') != 'qlib_adjusted'
                or payload.get('missing_fields') or payload.get('truncated_to_latest')
                or [row['date'] for row in rows] != dates):
            reason = 'missing_or_nonrectangular_history'
        elif any(not isinstance(row[field], (int, float))
                 or not math.isfinite(row[field]) or row[field] <= 0
                 for row in rows for field in ('close', 'volume')):
            reason = 'nonpositive_or_missing_close_or_volume'
        attempted.append({'symbol': symbol, 'result': reason or 'selected'})
        if reason is None:
            selected.append((symbol, [float(row['close']) for row in rows]))
    if len(selected) < pool_count * pool_size:
        raise ValueError('not enough complete, outcome-blind Qlib histories')
    pools = {}
    for number in range(pool_count):
        group = selected[number * pool_size:(number + 1) * pool_size]
        pools[f'pool_{number + 1:02}'] = {
            f'stock:{symbol[2:]}.{symbol[:2]}': prices for symbol, prices in group}
    lineage = {'release_tag': RELEASE_TAG,
               'manifest_sha256': index['release']['manifest_sha256'],
               'archive_sha256': index['release']['archive_sha256'],
               'selection_nonce': SELECTION_NONCE,
               'eligible_span_count': len(ordered_candidates(instruments)),
               'attempted': attempted,
               'selection_note': 'hash order before outcomes; then require complete positive close and volume through 2026, causing survival and uninterrupted-trading bias'}
    return dates, pools, lineage


def buy_entry_indices(dates: Sequence[str], targets: Sequence[Mapping[str, float]],
                      first: str, last: str, horizon: int, seed: int,
                      count: int) -> list[int]:
    """Sample only t dates whose frozen target buys from initial cash at t+1."""
    eligible = entry_indices(dates, first, last, horizon, seed, None)
    active = [index for index in eligible if sum(targets[index].values()) > 0]
    if len(active) < count:
        raise ValueError(f'only {len(active)} true buy-signal dates available')
    return sorted(random.Random(seed).sample(active, count))


def score_pools(dates: Sequence[str], pools: Mapping[str, Mapping[str, Sequence[float]]],
                *, segments: Mapping[str, tuple[str, str]] = SEGMENTS,
                horizons: Sequence[int] = HORIZONS,
                seeds: Sequence[int] = SEEDS,
                sample_count: int = SAMPLE_COUNT) -> dict:
    if not pools or list(dates) != sorted(set(dates)):
        raise ValueError('invalid stress panel')
    if any(len(series) != len(dates) or any(value <= 0 or not math.isfinite(value)
                                          for value in series)
           for panel in pools.values() for series in panel.values()):
        raise ValueError('incomplete stress panel')
    signals = {name: sma_targets(panel) for name, panel in pools.items()}
    matched = {name: exposure_matched_targets(signals[name], sorted(panel))
               for name, panel in pools.items()}
    cases = {}
    aggregates = {}
    for segment, (first, last) in segments.items():
        cases[segment] = {}
        aggregates[segment] = {}
        for horizon in horizons:
            cases[segment][str(horizon)] = {}
            aggregates[segment][str(horizon)] = {}
            for seed in seeds:
                pool_summaries = {}
                for name, panel in pools.items():
                    entries = buy_entry_indices(dates, signals[name], first, last,
                                                horizon, seed, sample_count)
                    if any(dates[index + horizon] > last for index in entries):
                        raise ValueError('entry horizon escapes its segment')
                    paired = []
                    for index in entries:
                        strategy = episode(dates, panel, signals[name], index, horizon, FEE)
                        equal_exposure = episode(dates, panel, matched[name], index, horizon, FEE)
                        stress_strategy = episode(dates, panel, signals[name], index, horizon, STRESS_FEE)
                        stress_equal_exposure = episode(dates, panel, matched[name], index, horizon, STRESS_FEE)
                        static_80 = cash_matched_hold_episode(dates, panel, index, horizon, FEE)
                        full_hold = hold_episode(dates, panel, index, horizon, FEE)
                        paired.append({'entry': dates[index], 'exit': dates[index + horizon],
                                       'sma_return': strategy['return'],
                                       'exposure_equal_return': equal_exposure['return'],
                                       'static_80_return': static_80['return'],
                                       'full_hold_return': full_hold['return'],
                                       'sma_drawdown': strategy['max_drawdown'],
                                       'exposure_equal_drawdown': equal_exposure['max_drawdown'],
                                       'sma_turnover': strategy['turnover'],
                                       'paired_return': strategy['return'] - equal_exposure['return'],
                                       'paired_drawdown': strategy['max_drawdown'] - equal_exposure['max_drawdown'],
                                       'stress_paired_return': stress_strategy['return'] - stress_equal_exposure['return']})
                    pool_summaries[name] = {
                        'symbols': sorted(panel), 'episodes': len(paired),
                        'sma_return': numeric_summary([row['sma_return'] for row in paired]),
                        'exposure_equal_return': numeric_summary([row['exposure_equal_return'] for row in paired]),
                        'static_80_return': numeric_summary([row['static_80_return'] for row in paired]),
                        'full_hold_return': numeric_summary([row['full_hold_return'] for row in paired]),
                        'paired_return': numeric_summary([row['paired_return'] for row in paired]),
                        'paired_drawdown': numeric_summary([row['paired_drawdown'] for row in paired]),
                        'stress_paired_return': numeric_summary([row['stress_paired_return'] for row in paired]),
                        'sma_turnover': numeric_summary([row['sma_turnover'] for row in paired]),
                        'cases': paired}
                rows = list(pool_summaries.values())
                all_ret = [case['paired_return'] for row in rows for case in row['cases']]
                aggregates[segment][str(horizon)][str(seed)] = {
                    'pool_count': len(rows), 'episodes_per_pool': sample_count,
                    'pooled_case_median_paired_return_descriptive': median(all_ret),
                    'pool_median_paired_return': numeric_summary(
                        [row['paired_return']['median'] for row in rows]),
                    'pool_median_paired_drawdown': numeric_summary(
                        [row['paired_drawdown']['median'] for row in rows]),
                    'pool_p10_paired_return': numeric_summary(
                        [row['paired_return']['p10'] for row in rows]),
                    'pool_positive_median_return_count': sum(
                        row['paired_return']['median'] > 0 for row in rows),
                    'pool_nonnegative_p10_return_count': sum(
                        row['paired_return']['p10'] >= 0 for row in rows),
                    'pool_nonnegative_median_drawdown_count': sum(
                        row['paired_drawdown']['median'] >= 0 for row in rows),
                    'pool_positive_stress_median_return_count': sum(
                        row['stress_paired_return']['median'] > 0 for row in rows)}
                cases[segment][str(horizon)][str(seed)] = {
                    'entry_dates_by_pool': {name: [row['entry'] for row in value['cases']]
                                            for name, value in pool_summaries.items()},
                    'pools': pool_summaries}
    return {'schema_version': 1, 'study_version': VERSION,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'status': 'retrospective_survivor_conditioned_transfer_stress',
            'independent_out_of_sample': False,
            'first_date': dates[0], 'first_entry_eligible_date': ENTRY_FIRST,
            'last_date': dates[-1],
            'selection_method': 'SHA-256 symbol ordering and complete-history filter including 2020 signal warmup; within each pool, seeded uniform sample of 2021+ dates with nonzero SMA buy target',
            'entry_date_policy': 'true strategy buy-signal dates at t; every episode starts from cash and buys at t+1 close',
            'price_basis': 'Qlib adjusted close; next-close return proxy',
            'cost_rate_per_side': FEE, 'stress_cost_rate_per_side': STRESS_FEE,
            'horizons': list(horizons), 'seeds': list(seeds),
            'sample_count_per_segment_horizon_seed': sample_count,
            'execution_rule': 'decision t close; fill t+1 adjusted close; sell on final close',
            'budget': {'max_gross_weight': .8, 'max_stock_weight': .35},
            'pool_symbols': {name: sorted(panel) for name, panel in pools.items()},
            'panel_sha256': fingerprint({'dates': dates, 'pools': pools}),
            'aggregates': aggregates, 'cases': cases,
            'limitations': [
                'The 2026 Qlib release is not a historical point-in-time universe; selecting continuously valid survivors biases the pool.',
                '2020-09-01 onward is used only to warm up 20/60-day signals; all sampled buy-signal dates start in 2021.',
                'Historical ST status, price-limit queues, delistings, corporate-action cash flows and executable raw prices are unavailable here.',
                'Pool-specific buy-signal entry dates differ; cross-pool absolute returns are not paired on identical dates.',
                'Windows within each pool and seed repeats overlap heavily; pooled cases are dependent descriptions.',
                'The dynamic equal-weight comparator inherits each SMA daily gross exposure; it tests name selection only.',
                'Parameters were chosen in earlier hindsight research; this stress cannot certify stable A-share returns.',
            ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--manifest', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path('studies/random-entry-baseline-v1/qlib-deterministic-pool-stress-v2.json'))
    args = parser.parse_args()
    dates, pools, lineage = load_pools(args.root, args.manifest)
    report = score_pools(dates, pools)
    report['source_lineage'] = lineage
    base = Path(__file__).resolve().parents[1]
    dependencies = [Path(__file__), base / 'scripts/qlib_cross_pool_audit.py',
                    base / 'scripts/random_entry_baseline.py',
                    *[base / 'src/quant_lab' / name for name in
                      ('qlib_local.py', 'qlib_archive.py', 'strategy_catalog.py',
                       'strategies.py', 'factor_library.py', 'features.py',
                       'backtest.py', 'decision_ensemble.py', 'cost_model.py')]]
    report['code_file_sha256'] = {str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in dependencies}
    report['code_sha256'] = fingerprint(report['code_file_sha256'])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'output': str(args.out.resolve()), 'pool_count': len(pools),
                      'aggregates': report['aggregates']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
