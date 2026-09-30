"""Replay calendar-random starts in 2021–2026, independent of SMA signals.

All 12 frozen survivor-selected pools share the same sampled dates. Passive
80% buy-and-hold buys at the next close. SMA and its equal-exposure control
begin from cash on the same date, including dates with no immediate SMA buy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from qlib_cross_pool_audit import sma_targets
from random_entry_baseline import entry_indices, exposure_matched_targets
from raw_action_aware_hold_stress import (CAPITAL, FEE, LOT, TARGET_GROSS,
                                          _distribution, load_inputs, simulate_hold)
from raw_action_aware_later_stress import load_sources
from raw_action_aware_sma_stress import simulate_rebalanced

BASE = Path(__file__).resolve().parents[1]
VERSION = 'unconditional-random-entry-36-pool-stress-v1'
SEGMENTS = {'early': ('2021-01-04', '2023-12-29'),
            'later': ('2024-01-02', '2026-09-28')}
HORIZONS = (126, 252)
SEEDS = (202, 404)
SAMPLES = 64


def sampled_dates(calendar: list[str], first: str, last: str,
                  horizon: int, seed: int, count: int = SAMPLES) -> list[str]:
    """Uniform eligible signal dates, with no strategy-output filter."""
    if calendar != sorted(set(calendar)) or horizon < 2:
        raise ValueError('invalid sampling calendar or horizon')
    return [calendar[i] for i in entry_indices(calendar, first, last,
                                                horizon, seed, count)]


def replay(stress: dict, qdates: list[str], qpools: dict,
           segments: dict[str, tuple[list[str], dict, dict]]) -> dict:
    all_cases = []
    sampled = {}
    signals = {name: sma_targets(panel) for name, panel in qpools.items()}
    targets = {}
    for name, panel in qpools.items():
        symbols = sorted(panel)
        targets[name] = {
            'sma': dict(zip(qdates, signals[name])),
            'same_exposure_equal': dict(zip(
                qdates, exposure_matched_targets(signals[name], symbols)))}
    for segment, (first, last) in SEGMENTS.items():
        dates, raw, actions = segments[segment]
        raw_index = {day: index for index, day in enumerate(dates)}
        if (dates[0] != first or dates[-1] != last
                or [day for day in qdates if first <= day <= last] != dates):
            raise ValueError(f'{segment}: source calendar differs')
        sampled[segment] = {}
        for horizon in HORIZONS:
            sampled[segment][str(horizon)] = {}
            for seed in SEEDS:
                chosen = sampled_dates(qdates, first, last, horizon, seed)
                sampled[segment][str(horizon)][str(seed)] = chosen
                for pool_name, qpanel in qpools.items():
                    symbols = sorted(qpanel)
                    panel = {symbol: [raw[symbol][day] for day in dates]
                             for symbol in symbols}
                    pool_actions = {symbol: actions[symbol] for symbol in symbols}
                    for decision in chosen:
                        start = raw_index[decision]
                        end = start + horizon
                        if end >= len(dates):
                            raise ValueError('sampled horizon escapes segment')
                        hold = simulate_hold(dates, panel, pool_actions,
                                             entry=start + 1, exit_index=end,
                                             lot_size=LOT)
                        strategies = {}
                        for policy in ('sma', 'same_exposure_equal'):
                            strategies[policy] = simulate_rebalanced(
                                dates, panel, pool_actions, targets[pool_name][policy],
                                entry=start + 1, exit_index=end, lot_size=LOT)
                        signal = signals[pool_name][qdates.index(decision)]
                        case_id = hashlib.sha256(
                            f'{VERSION}|{segment}|{horizon}|{seed}|{pool_name}|{decision}'.encode()
                        ).hexdigest()[:20]
                        all_cases.append({
                            'case_id': case_id, 'segment': segment,
                            'horizon_sessions': horizon, 'seed': seed,
                            'pool': pool_name, 'decision_date': decision,
                            'first_fill_date': dates[start + 1],
                            'exit_date': dates[end],
                            'sma_signal_gross_at_decision': sum(signal.values()),
                            'sma_positive_target_at_decision': sum(signal.values()) > 0,
                            'passive_fixed_80': hold,
                            'sma': strategies['sma'],
                            'same_exposure_equal': strategies['same_exposure_equal'],
                        })
    expected = len(SEGMENTS) * len(HORIZONS) * len(SEEDS) * len(qpools) * SAMPLES
    if len(all_cases) != expected or len({x['case_id'] for x in all_cases}) != expected:
        raise ValueError('random case denominator differs')
    summaries = {}
    for segment in SEGMENTS:
        summaries[segment] = {}
        for horizon in HORIZONS:
            for seed in SEEDS:
                selected = [x for x in all_cases if x['segment'] == segment
                            and x['horizon_sessions'] == horizon and x['seed'] == seed]
                joint = [x for x in selected if all(
                    x[policy]['status'] == 'fully_liquidated_raw_action_proxy'
                    for policy in ('passive_fixed_80', 'sma', 'same_exposure_equal'))]
                pool_medians = {}
                pool_passive = {}
                disjoint = []
                for name in stress['pool_symbols']:
                    part = sorted((x for x in joint if x['pool'] == name),
                                  key=lambda x: x['decision_date'])
                    pool_medians[name] = median([
                        x['sma']['net_return'] - x['same_exposure_equal']['net_return']
                        for x in part]) if part else None
                    pool_passive[name] = median([
                        x['sma']['net_return'] - x['passive_fixed_80']['net_return']
                        for x in part]) if part else None
                    last_exit = ''
                    for item in part:
                        if item['decision_date'] > last_exit:
                            disjoint.append(item)
                            last_exit = item['exit_date']
                summaries[segment][f'{horizon}:{seed}'] = {
                    'sample_count': len(selected),
                    'jointly_liquidated_count': len(joint),
                    'unique_calendar_entry_date_count': len(sampled[segment][str(horizon)][str(seed)]),
                    'sma_positive_target_at_decision_count': sum(
                        x['sma_positive_target_at_decision'] for x in selected),
                    'sma_first_fill_bought_case_count': sum(
                        any(order['side'] == 'buy' for order in x['sma']['first_orders'])
                        for x in selected),
                    'sma_return': _distribution([x['sma']['net_return'] for x in joint]),
                    'same_exposure_equal_return': _distribution([
                        x['same_exposure_equal']['net_return'] for x in joint]),
                    'passive_fixed_80_return': _distribution([
                        x['passive_fixed_80']['net_return'] for x in joint]),
                    'sma_minus_same_exposure_equal': _distribution([
                        x['sma']['net_return'] - x['same_exposure_equal']['net_return']
                        for x in joint]),
                    'sma_minus_passive_fixed_80': _distribution([
                        x['sma']['net_return'] - x['passive_fixed_80']['net_return']
                        for x in joint]),
                    'same_exposure_equal_minus_passive_fixed_80': _distribution([
                        x['same_exposure_equal']['net_return']
                        - x['passive_fixed_80']['net_return'] for x in joint]),
                    'sma_minus_same_exposure_drawdown': _distribution([
                        x['sma']['max_drawdown']
                        - x['same_exposure_equal']['max_drawdown'] for x in joint]),
                    'sma_minus_passive_drawdown': _distribution([
                        x['sma']['max_drawdown']
                        - x['passive_fixed_80']['max_drawdown'] for x in joint]),
                    'positive_pool_median_sma_vs_same_exposure_count': sum(
                        value is not None and value > 0 for value in pool_medians.values()),
                    'positive_pool_median_sma_vs_passive_count': sum(
                        value is not None and value > 0 for value in pool_passive.values()),
                    'within_pool_disjoint_count': len(disjoint),
                    'within_pool_disjoint_sma_minus_same_exposure': _distribution([
                        x['sma']['net_return'] - x['same_exposure_equal']['net_return']
                        for x in disjoint]),
                    'within_pool_disjoint_sma_minus_passive': _distribution([
                        x['sma']['net_return'] - x['passive_fixed_80']['net_return']
                        for x in disjoint]),
                }
    return {'case_count': len(all_cases), 'sampled_dates': sampled,
            'summaries': summaries, 'cases': all_cases}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--db', type=Path, default=Path('data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--qlib-root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/unconditional-random-entry-36-pool-stress-v1.json'))
    args = parser.parse_args()
    stress_path = args.source_dir / 'qlib-deterministic-pool-stress-v2.json'
    early_snapshots = [args.source_dir / f'baostock-36-pool-corporate-actions-{year}-v1.json'
                       for year in (2021, 2022, 2023)]
    stress, early_dates, early_raw, early_actions, early_lineage = load_inputs(
        stress_path, early_snapshots, args.db,
        args.source_dir / 'pool-corporate-action-alignment-v1.json')
    (later_stress, later_dates, later_raw, later_actions,
     qdates, qpools, later_lineage) = load_sources(
        stress_path, args.source_dir, args.db, args.qlib_root, args.qlib_manifest)
    if stress != later_stress:
        raise ValueError('early and later frozen stress differs')
    early_by_date = {symbol: dict(zip(early_dates, prices))
                     for symbol, prices in early_raw.items()}
    result = replay(stress, qdates, qpools, {
        'early': (early_dates, early_by_date, early_actions),
        'later': (later_dates, later_raw, later_actions)})
    dependency_paths = [BASE / path for path in (
        'scripts/raw_action_aware_hold_stress.py',
        'scripts/raw_action_aware_sma_stress.py',
        'scripts/raw_action_aware_later_stress.py',
        'scripts/random_entry_baseline.py')]
    report = {
        'study_version': VERSION,
        'status': 'retrospective_unconditional_calendar_random_stress_not_stable_baseline',
        'sample_rule': '64 uniform eligible decision dates per segment/horizon/seed, shared by all 12 pools; no signal filter',
        'entry_rule': 'Passive buys on t+1 raw close; SMA/equal-exposure start from cash on t and may wait in cash.',
        'capital_cny': CAPITAL, 'buy_fee_per_side': FEE,
        'sell_fee_per_side': FEE, 'buy_lot_shares': LOT,
        'historical_point_in_time_universe_confirmed': False,
        'independent_out_of_sample_confirmed': False,
        'source_lineage': {
            'early': early_lineage, 'later': later_lineage,
            'dependency_sha256': {
                str(path.relative_to(BASE)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in dependency_paths}},
        **result,
        'limitations': [
            'Calendar-random starts are independent of SMA signals, but the 36 stocks were selected using full 2026 history.',
            'SMA may hold cash at a sampled start, whereas passive 80% always buys; this tests the full timing rule, not same-day forced-entry stock selection.',
            'All names and signal prices come from a 2026-revised release; historical point-in-time publication is unverified.',
            'Corporate actions are later BaoStock snapshots; gross dividends omit taxes and rights subscription.',
            'Close-price fills omit price-limit queues, intraday slippage and actual order execution.',
            'Windows overlap and the two seeds may select the same date; observations are not independent success trials.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()), 'case_count': result['case_count'],
                      'status': report['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
