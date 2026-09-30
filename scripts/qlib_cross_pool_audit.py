"""Retrospective, paired random-entry transfer audit on one verified Qlib release.

The original SMA policy and the transfer stock pool are frozen before this run.
Qlib's adjusted close and volume are research features, not verified original
transaction prices or a historical point-in-time tradability record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

from quant_lab.decision_ensemble import PortfolioBudget, apply_budget
from quant_lab.qlib_local import MAX_ROWS, _load_index, read_stock
from quant_lab.strategy_catalog import get_strategy_definition
from random_entry_baseline import (cash_matched_hold_episode, entry_indices, episode,
                                   exposure_matched_targets, hold_episode, quantile)

VERSION = 'qlib-cross-pool-migration-v1'
POOLS = {
    'frozen_baseline': ('stock:000338.SZ', 'stock:002475.SZ', 'stock:002714.SZ'),
    'user_named_transfer': ('stock:000977.SZ', 'stock:002594.SZ', 'stock:603993.SH'),
}
SEGMENTS = {
    'selection_history': ('2021-01-01', '2023-12-29'),
    'later_history': ('2024-01-02', '2026-09-28'),
}
HORIZONS = (126, 252)
SEEDS = (202, 404, 606)
SAMPLES_PER_SEGMENT = 128
FEE = 0.0005
BUDGET = PortfolioBudget(0.8, 0.35)
RELEASE_TAG = '2026-09-28'


def sha_json(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def qlib_symbol(instrument_id: str) -> str:
    asset, code = instrument_id.split(':', 1)
    if asset != 'stock' or not (code.endswith('.SZ') or code.endswith('.SH')):
        raise ValueError(f'unsupported Qlib stock: {instrument_id}')
    return code


def validate_frozen_policy(policy: Mapping[str, object]) -> None:
    definition = get_strategy_definition('sma-trend')
    if (policy.get('mode') != 'single'
            or policy.get('strategy_ids') != ['sma-trend']
            or sorted(policy.get('universe', [])) != sorted(POOLS['frozen_baseline'])
            or policy.get('budget') != {'max_gross_weight': BUDGET.max_gross_weight,
                                        'max_stock_weight': BUDGET.max_stock_weight}
            or policy.get('cost_rate') != FEE
            or definition.parameters() != {'fast_window': 20, 'slow_window': 60}):
        raise ValueError('frozen SMA policy or installed strategy differs from audit contract')


def validate_stock_payloads(payloads: Mapping[str, Mapping[str, object]]) -> tuple[list[str], dict[str, dict[str, list[float]]]]:
    """Require a rectangular, positive-price and positive-volume panel for all six names.

    Nonpositive source volume is treated as an untradeable proxy and aborts the
    run. Positive Qlib volume cannot prove freedom from ST, limit or other
    execution restrictions.
    """
    expected = {key for pool in POOLS.values() for key in pool}
    if set(payloads) != expected:
        raise ValueError('stock payloads do not match the frozen two-pool universe')
    dates: list[str] | None = None
    closes: dict[str, list[float]] = {}
    release_identity: tuple[str, str] | None = None
    for instrument_id in sorted(expected):
        payload = payloads[instrument_id]
        if (payload.get('adjustment') != 'qlib_adjusted'
                or payload.get('missing_fields')
                or payload.get('truncated_to_latest')
                or payload.get('fields') != ['close', 'volume']):
            raise ValueError(f'{instrument_id}: incomplete or wrong-basis Qlib payload')
        stock = payload.get('stock')
        if not isinstance(stock, dict) or stock.get('instrument_id') != instrument_id:
            raise ValueError(f'{instrument_id}: source identity mismatch')
        release = payload.get('release')
        if not isinstance(release, dict) or release.get('tag') != RELEASE_TAG:
            raise ValueError(f'{instrument_id}: release identity mismatch')
        identity = (release.get('manifest_sha256'), release.get('archive_sha256'))
        if None in identity or (release_identity is not None and identity != release_identity):
            raise ValueError(f'{instrument_id}: mixed Qlib release')
        release_identity = identity
        rows = payload.get('rows')
        if not isinstance(rows, list) or len(rows) != payload.get('calendar_rows_in_range'):
            raise ValueError(f'{instrument_id}: missing Qlib calendar rows')
        stock_dates = [row.get('date') for row in rows]
        if not stock_dates or stock_dates != sorted(set(stock_dates)):
            raise ValueError(f'{instrument_id}: empty or nonunique Qlib dates')
        if dates is not None and stock_dates != dates:
            raise ValueError(f'{instrument_id}: nonrectangular Qlib calendar')
        dates = stock_dates
        series: list[float] = []
        for row in rows:
            close, volume = row.get('close'), row.get('volume')
            if (close is None or volume is None
                    or isinstance(close, bool) or isinstance(volume, bool)
                    or not isinstance(close, (int, float)) or not isinstance(volume, (int, float))
                    or not math.isfinite(close) or not math.isfinite(volume)
                    or close <= 0 or volume <= 0):
                raise ValueError(f"{instrument_id}: missing or nonpositive close/volume on {row.get('date')}")
            series.append(float(close))
        closes[instrument_id] = series
    assert dates is not None
    return dates, {name: {key: closes[key] for key in sorted(ids)}
                   for name, ids in POOLS.items()}


def load_verified_panels(root: Path, manifest: Path) -> tuple[list[str], dict[str, dict[str, list[float]]], dict]:
    """Read and verify every required feature through the Qlib release reader."""
    root = root.resolve()
    index = _load_index(root, manifest, RELEASE_TAG)
    payloads = {}
    source_files = {}
    for instrument_id in sorted({key for pool in POOLS.values() for key in pool}):
        symbol = qlib_symbol(instrument_id)
        payloads[instrument_id] = read_stock(
            root, manifest, RELEASE_TAG, symbol,
            start='2021-01-01', end=RELEASE_TAG,
            fields=('close', 'volume'), limit=MAX_ROWS,
            include_close_coverage=True,
        )
        source_files[instrument_id] = {}
        for field in ('close', 'volume'):
            name = f"qlib_bin/features/{symbol.split('.')[1].lower()}{symbol.split('.')[0]}/{field}.day.bin"
            # qlib_local normalizes 000338.SZ to SZ000338.
            if name not in index['files']:
                raise ValueError(f'{instrument_id}: verified feature absent from index')
            source_files[instrument_id][field] = index['files'][name]['sha256']
    dates, panels = validate_stock_payloads(payloads)
    if dates[0] != '2021-01-04' or dates[-1] != RELEASE_TAG:
        raise ValueError('Qlib common calendar endpoints differ from frozen audit')
    lineage = {'source_id': 'investment_data_qlib_release',
               'release': payloads[next(iter(sorted(payloads)))]['release'],
               'published_index_sha256': hashlib.sha256((root / 'release-index.json').read_bytes()).hexdigest(),
               'verified_feature_sha256': source_files,
               'price_basis': 'qlib_adjusted_close',
               'volume_basis': 'Qlib adjusted/source-native; positive-value tradability proxy only'}
    return dates, panels, lineage


def sma_targets(panel: Mapping[str, Sequence[float]]) -> list[dict[str, float]]:
    strategy = get_strategy_definition('sma-trend').build()
    count = len(next(iter(panel.values())))
    results = []
    for index in range(count - 1):
        history = {key: prices[:index + 1] for key, prices in panel.items()}
        proposed = strategy.weights(history)
        raw = {key: float(proposed.get(key, 0.0)) for key in panel}
        results.append(apply_budget(raw, BUDGET))
    return results


def numeric_summary(values: Sequence[float]) -> dict[str, float]:
    if not values:
        raise ValueError('cannot summarize zero episodes')
    return {'median': median(values), 'p10': quantile(values, .1),
            'p90': quantile(values, .9)}


def summarize_rows(rows: Sequence[dict]) -> dict:
    result = {'episodes': len(rows), 'pools': {}}
    for pool_name in POOLS:
        records = [row['pools'][pool_name] for row in rows]
        result['pools'][pool_name] = {
            'sma_return': numeric_summary([r['sma']['return'] for r in records]),
            'full_equal_hold_return': numeric_summary([r['full_equal_hold']['return'] for r in records]),
            'same_cap_equal_hold_return': numeric_summary([r['same_cap_equal_hold']['return'] for r in records]),
            'exposure_matched_equal_hold_return': numeric_summary([r['exposure_matched_equal_hold']['return'] for r in records]),
            'sma_max_drawdown': numeric_summary([r['sma']['max_drawdown'] for r in records]),
            'full_equal_hold_max_drawdown': numeric_summary([r['full_equal_hold']['max_drawdown'] for r in records]),
            'same_cap_equal_hold_max_drawdown': numeric_summary([r['same_cap_equal_hold']['max_drawdown'] for r in records]),
            'exposure_matched_equal_hold_max_drawdown': numeric_summary([r['exposure_matched_equal_hold']['max_drawdown'] for r in records]),
            'sma_turnover_with_exit': numeric_summary([r['sma']['turnover'] for r in records]),
        }
        for comparator in ('full_equal_hold', 'same_cap_equal_hold', 'exposure_matched_equal_hold'):
            excess = [r['sma']['return'] - r[comparator]['return'] for r in records]
            result['pools'][pool_name][f'excess_vs_{comparator}'] = numeric_summary(excess)
            result['pools'][pool_name][f'beat_{comparator}_fraction'] = sum(v > 0 for v in excess) / len(excess)
    paired_strategy = [row['pools']['user_named_transfer']['sma']['return']
                       - row['pools']['frozen_baseline']['sma']['return'] for row in rows]
    result['paired_transfer_minus_baseline_sma_return'] = numeric_summary(paired_strategy)
    for comparator in ('same_cap_equal_hold', 'exposure_matched_equal_hold'):
        paired_excess = [(
            row['pools']['user_named_transfer']['sma']['return']
            - row['pools']['user_named_transfer'][comparator]['return']
            - row['pools']['frozen_baseline']['sma']['return']
            + row['pools']['frozen_baseline'][comparator]['return'])
            for row in rows]
        result[f'paired_transfer_minus_baseline_excess_vs_{comparator}'] = numeric_summary(paired_excess)
    return result


def evaluate(dates: Sequence[str], panels: Mapping[str, Mapping[str, Sequence[float]]],
             *, segments: Mapping[str, tuple[str, str]] = SEGMENTS,
             horizons: Sequence[int] = HORIZONS,
             seeds: Sequence[int] = SEEDS,
             sample_count: int = SAMPLES_PER_SEGMENT) -> dict:
    if (list(dates) != sorted(set(dates)) or set(panels) != set(POOLS)
            or not dates or any(set(panels[name]) != set(POOLS[name]) for name in POOLS)):
        raise ValueError('invalid paired Qlib panel')
    if any(len(prices) != len(dates) or any(not math.isfinite(v) or v <= 0 for v in prices)
           for panel in panels.values() for prices in panel.values()):
        raise ValueError('incomplete paired Qlib close panel')
    targets = {name: sma_targets(panel) for name, panel in panels.items()}
    matched = {name: exposure_matched_targets(targets[name], panel.keys())
               for name, panel in panels.items()}
    cases = {}
    for segment, (first, last) in segments.items():
        cases[segment] = {}
        for horizon in horizons:
            cases[segment][str(horizon)] = {}
            for seed in seeds:
                indices = entry_indices(dates, first, last, horizon, seed, sample_count)
                rows = []
                for index in indices:
                    pools = {}
                    for pool_name, panel in panels.items():
                        pools[pool_name] = {
                            'sma': episode(dates, panel, targets[pool_name], index, horizon, FEE),
                            'full_equal_hold': hold_episode(dates, panel, index, horizon, FEE),
                            'same_cap_equal_hold': cash_matched_hold_episode(
                                dates, panel, index, horizon, FEE, BUDGET.max_gross_weight),
                            'exposure_matched_equal_hold': episode(
                                dates, panel, matched[pool_name], index, horizon, FEE),
                        }
                    rows.append({'entry_date': dates[index], 'exit_date': dates[index + horizon],
                                 'pools': pools})
                cases[segment][str(horizon)][str(seed)] = {
                    'entry_dates_sha256': sha_json([row['entry_date'] for row in rows]),
                    'summary': summarize_rows(rows), 'episodes': rows,
                }
    return {'schema_version': 1, 'audit_version': VERSION,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'status': 'retrospective_cross_pool_transfer_audit',
            'independent_out_of_sample': False,
            'pools': {name: list(ids) for name, ids in POOLS.items()},
            'first_date': dates[0], 'last_date': dates[-1],
            'common_sessions': len(dates), 'segments': segments,
            'horizons_sessions': list(horizons), 'sampling_seeds': list(seeds),
            'sample_count_per_segment_horizon_seed': sample_count,
            'strategy': {'id': 'sma-trend', 'parameters': {'fast_window': 20, 'slow_window': 60}},
            'budget': {'max_gross_weight': BUDGET.max_gross_weight,
                       'max_stock_weight': BUDGET.max_stock_weight},
            'fee_rate_per_side': FEE,
            'benchmark_definitions': {
                'full_equal_hold': '100% equal-weight purchase at t+1 close, then hold until exit',
                'same_cap_equal_hold': '80% equal-weight purchase at t+1 close with 20% cash, then hold until exit',
                'exposure_matched_equal_hold': 'daily equal-weight target with the same daily gross exposure as SMA; comparator uses SMA exposure decisions',
            },
            'execution_rule': 'signal at close t; rebalance at adjusted close t+1; liquidate at horizon close',
            'price_basis': 'Qlib adjusted close (single release; return proxy, not original transaction price)',
            'panel_sha256': sha_json({'dates': dates, 'closes': panels}),
            'cases': cases,
            'limitations': [
                'Both pools and all rules are hindsight selected; this is a paired transfer stress test, not independent forward validation.',
                'Episodes and seeds overlap heavily; beat fractions are descriptive, not independent probabilities.',
                'The dynamic equal-weight comparator inherits the SMA gross exposure path and is not an independently investable passive rule.',
                '2026 Qlib adjusted features have no verified historical first-publication time or historical universe membership snapshots.',
                'Positive Qlib source volume is a suspension proxy only; ST status, price limits, delistings and true execution are unverified.',
                'Fractional weight simulation uses adjusted next closes; lot size, slippage, minimum commission, separate taxes and corporate-action cash flows are not modeled.',
            ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--manifest', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--policy', type=Path, default=Path('policies/random-entry-risk-baseline-v1.json'))
    parser.add_argument('--out', type=Path, default=Path('studies/random-entry-baseline-v1/qlib-cross-pool-migration-v1.json'))
    args = parser.parse_args()
    policy_bytes = args.policy.read_bytes()
    validate_frozen_policy(json.loads(policy_bytes))
    root = Path(__file__).resolve().parents[1]
    sources = [Path(__file__), root / 'scripts/random_entry_baseline.py',
               *[root / 'src/quant_lab' / name for name in
                 ('strategies.py', 'strategy_catalog.py', 'factor_library.py',
                  'decision_ensemble.py', 'backtest.py', 'qlib_local.py')]]
    code_before = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sources}
    dates, panels, lineage = load_verified_panels(args.root, args.manifest)
    report = evaluate(dates, panels)
    code_after = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sources}
    if code_before != code_after or hashlib.sha256(args.policy.read_bytes()).hexdigest() != hashlib.sha256(policy_bytes).hexdigest():
        raise ValueError('audited code or policy changed during the run')
    report['source_lineage'] = lineage
    report['frozen_policy_sha256'] = hashlib.sha256(policy_bytes).hexdigest()
    report['code_file_sha256'] = code_after
    report['code_sha256'] = sha_json(code_after)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    compact = {segment: {horizon: {seed: value['summary'] for seed, value in by_seed.items()}
                         for horizon, by_seed in by_horizon.items()}
               for segment, by_horizon in report['cases'].items()}
    print(json.dumps({'output': str(args.out.resolve()), 'panel_sha256': report['panel_sha256'],
                      'summary': compact}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
