"""Reproducible random-entry comparison for single and combined decision rules.

The stock universe is hindsight selected. The later segment is a chronological
retrospective check, not prospective or point-in-time-universe validation.
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

from quant_lab.backtest import DatedTarget, run_backtest_targets, run_buy_and_hold
from quant_lab.cost_model import FEE_RATE_PER_SIDE
from quant_lab.decision_ensemble import PortfolioBudget, apply_budget
from quant_lab.strategy_catalog import get_strategy_definition
from paper_portfolio import load_panel

VERSION = 'random-entry-risk-baseline-v8'
UNIVERSE = ('stock:000338.SZ', 'stock:002475.SZ', 'stock:002714.SZ')
STRATEGIES = ('sma-trend', 'cross-sectional-momentum', 'mean-reversion-zscore')
TRAIN_END = '2023-12-29'
VALIDATE_START = '2024-01-02'
HORIZONS = (126, 252)
SAMPLE_PER_SEGMENT = 128
CANDIDATES = {
    'sma': {'sma-trend': 1},
    'momentum': {'cross-sectional-momentum': 1},
    'zscore': {'mean-reversion-zscore': 1},
    'sma_momentum': {'sma-trend': 1, 'cross-sectional-momentum': 1},
    'sma_zscore': {'sma-trend': 1, 'mean-reversion-zscore': 1},
    'momentum_zscore': {'cross-sectional-momentum': 1, 'mean-reversion-zscore': 1},
    'all_three': {key: 1 for key in STRATEGIES},
}
BUDGET = PortfolioBudget(0.8, 0.35)


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def quantile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    left = math.floor(position)
    weight = position - left
    return ordered[left] * (1 - weight) + ordered[min(left + 1, len(ordered) - 1)] * weight


def candidate_targets(panel: Mapping[str, Sequence[float]]) -> dict[str, list[dict[str, float]]]:
    """Compute each signal from all prior closes, preserving warmup at random entry."""
    strategies = {key: get_strategy_definition(key).build() for key in STRATEGIES}
    count = len(next(iter(panel.values())))
    if count < max(HORIZONS) + 61 or any(len(x) != count for x in panel.values()):
        raise ValueError('incomplete or short close panel')
    results = {name: [] for name in CANDIDATES}
    for index in range(count - 1):
        history = {key: values[:index + 1] for key, values in panel.items()}
        signals = {key: strategy.weights(history) for key, strategy in strategies.items()}
        for name, mix in CANDIDATES.items():
            total = sum(mix.values())
            raw = {key: sum(weight / total * signals[sid].get(key, 0.0)
                            for sid, weight in mix.items()) for key in panel}
            results[name].append(apply_budget(raw, BUDGET))
    return results


def exposure_matched_targets(targets: Sequence[Mapping[str, float]],
                             universe: Sequence[str]) -> list[dict[str, float]]:
    """Keep each signal day's gross exposure; diversify it equally by name."""
    keys = sorted(universe)
    if not keys:
        raise ValueError('empty exposure-match universe')
    result = []
    for target in targets:
        gross = sum(float(value) for value in target.values())
        if set(target) - set(keys) or not math.isfinite(gross) or not 0 <= gross <= 1:
            raise ValueError('invalid exposure target')
        result.append({key: gross / len(keys) for key in keys})
    return result


def entry_indices(dates: Sequence[str], first: str, last: str, horizon: int,
                  seed: int, count: int | None) -> list[int]:
    """Uniform sample of eligible signal dates; both entry and exit stay in segment."""
    eligible = [i for i, day in enumerate(dates) if first <= day <= last
                and i + horizon < len(dates) and dates[i + horizon] <= last]
    if count is None:
        return eligible
    if len(eligible) < count:
        raise ValueError(f'only {len(eligible)} eligible entries in {first}..{last}')
    return sorted(random.Random(seed).sample(eligible, count))


def episode(dates: Sequence[str], panel: Mapping[str, Sequence[float]],
            targets: Sequence[Mapping[str, float]], start: int, horizon: int,
            fee: float) -> dict[str, float]:
    """Signal on start close, fill next close, liquidate at horizon close."""
    if horizon < 2:
        raise ValueError('episode needs separate entry and exit sessions')
    end = start + horizon
    window_dates = [date.fromisoformat(day) for day in dates[start:end + 1]]
    window_panel = {key: values[start:end + 1] for key, values in panel.items()}
    decisions = [DatedTarget(window_dates[i], targets[start + i])
                 for i in range(horizon - 1)]
    decisions.append(DatedTarget(window_dates[-2], {}))
    result = run_backtest_targets('dated-target', window_dates, window_panel, decisions, fee)
    return {'return': result.points[-1].equity - 1,
            'max_drawdown': result.metrics['max_drawdown'],
            'turnover': result.metrics['turnover']}


def hold_episode(dates: Sequence[str], panel: Mapping[str, Sequence[float]],
                 start: int, horizon: int, fee: float) -> dict[str, float]:
    end = start + horizon
    result = run_buy_and_hold([date.fromisoformat(day) for day in dates[start:end + 1]],
                              {key: values[start:end + 1] for key, values in panel.items()}, fee)
    terminal_equity = result.points[-1].equity * (1 - fee)
    peak = max(point.equity for point in result.points)
    return {'return': terminal_equity - 1,
            'max_drawdown': min(result.metrics['max_drawdown'], terminal_equity / peak - 1),
            'turnover': 2.0}


def cash_matched_hold_episode(dates: Sequence[str], panel: Mapping[str, Sequence[float]],
                              start: int, horizon: int, fee: float,
                              fraction: float = BUDGET.max_gross_weight) -> dict[str, float]:
    """Buy an equal-weight stock sleeve once; leave the remainder in cash."""
    if not 0 < fraction <= 1 or not 0 <= fee < .5:
        raise ValueError('invalid passive allocation or fee')
    keys = sorted(panel)
    entry = start + 1
    cash = 1 - fraction - fraction * fee
    if cash < 0:
        raise ValueError('fee cannot be funded from cash sleeve')
    peak = 1.0
    drawdown = 0.0
    final_stock_value = 0.0
    final_equity = 1.0
    for index in range(entry, start + horizon + 1):
        stock_value = sum(fraction / len(keys) * panel[key][index] / panel[key][entry]
                          for key in keys)
        equity = cash + stock_value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
        final_stock_value, final_equity = stock_value, equity
    terminal_equity = final_equity - final_stock_value * fee
    return {'return': terminal_equity - 1,
            'max_drawdown': min(drawdown, terminal_equity / peak - 1),
            'turnover': fraction + final_stock_value / final_equity}


def summarize(rows: Sequence[dict]) -> dict:
    returns = [row['strategy']['return'] for row in rows]
    excess = [row['strategy']['return'] - row['hold']['return'] for row in rows]
    drawdowns = [row['strategy']['max_drawdown'] for row in rows]
    drawdown_improvement = [row['strategy']['max_drawdown'] - row['hold']['max_drawdown']
                            for row in rows]
    return {'episodes': len(rows), 'median_return': median(returns),
            'p10_return': quantile(returns, .1), 'p90_return': quantile(returns, .9),
            'median_excess_vs_equal_hold': median(excess),
            'p10_excess_vs_equal_hold': quantile(excess, .1),
            'beat_equal_hold_fraction': sum(x > 0 for x in excess) / len(excess),
            'positive_return_fraction': sum(x > 0 for x in returns) / len(returns),
            'median_max_drawdown': median(drawdowns),
            'median_paired_drawdown_improvement_vs_equal_hold': median(drawdown_improvement),
            'p10_paired_drawdown_improvement_vs_equal_hold': quantile(drawdown_improvement, .1),
            'median_turnover_with_exit': median([row['strategy']['turnover'] for row in rows])}


def evaluate(dates: Sequence[str], panel: Mapping[str, Sequence[float]],
             *, sample_count: int | None = SAMPLE_PER_SEGMENT, fee: float = FEE_RATE_PER_SIDE,
             later_seed: int = 202) -> dict:
    if len(dates) != len(next(iter(panel.values()))) or dates != sorted(set(dates)):
        raise ValueError('invalid common-date panel')
    if not 0 <= fee < .5:
        raise ValueError('invalid fee')
    targets = candidate_targets(panel)
    segments = {'selection': (dates[0], TRAIN_END),
                'later_check': (VALIDATE_START, dates[-1])}
    results = {}
    entries = {}
    for segment, (first, last) in segments.items():
        results[segment] = {}
        entries[segment] = {}
        for horizon in HORIZONS:
            indices = entry_indices(dates, first, last, horizon,
                                    0 if segment == 'selection' else later_seed,
                                    None if segment == 'selection' else sample_count)
            entries[segment][str(horizon)] = [dates[i] for i in indices]
            holds = {i: hold_episode(dates, panel, i, horizon, fee) for i in indices}
            matched = {i: cash_matched_hold_episode(dates, panel, i, horizon, fee)
                       for i in indices}
            results[segment][str(horizon)] = {}
            for name, daily_targets in targets.items():
                rows = [{'strategy': episode(dates, panel, daily_targets, i, horizon, fee),
                         'hold': holds[i]} for i in indices]
                summary = summarize(rows)
                matched_excess = [row['strategy']['return'] - matched[i]['return']
                                  for i, row in zip(indices, rows)]
                matched_drawdown_improvement = [
                    row['strategy']['max_drawdown'] - matched[i]['max_drawdown']
                    for i, row in zip(indices, rows)]
                summary['median_excess_vs_cash_matched_hold'] = median(matched_excess)
                summary['p10_excess_vs_cash_matched_hold'] = quantile(matched_excess, .1)
                summary['beat_cash_matched_hold_fraction'] = (
                    sum(value > 0 for value in matched_excess) / len(matched_excess))
                summary['median_paired_drawdown_improvement_vs_cash_matched_hold'] = (
                    median(matched_drawdown_improvement))
                summary['p10_paired_drawdown_improvement_vs_cash_matched_hold'] = (
                    quantile(matched_drawdown_improvement, .1))
                summary['fraction_with_drawdown_improvement_vs_cash_matched_hold'] = (
                    sum(value > 0 for value in matched_drawdown_improvement)
                    / len(matched_drawdown_improvement))
                results[segment][str(horizon)][name] = summary
            results[segment][str(horizon)]['equal_hold'] = summarize([
                {'strategy': holds[i], 'hold': holds[i]} for i in indices])
            results[segment][str(horizon)]['cash_matched_equal_hold'] = summarize([
                {'strategy': matched[i], 'hold': holds[i]} for i in indices])
            cash = {'return': 0.0, 'max_drawdown': 0.0, 'turnover': 0.0}
            results[segment][str(horizon)]['cash'] = summarize([
                {'strategy': cash, 'hold': holds[i]} for i in indices])
    def selection_key(name: str) -> tuple:
        values = [results['selection'][str(h)][name] for h in HORIZONS]
        return (min(x['p10_return'] for x in values),
                min(x['median_return'] for x in values),
                -max(x['median_turnover_with_exit'] for x in values), name)
    winner = max(CANDIDATES, key=selection_key)
    exposure_targets = exposure_matched_targets(targets[winner], sorted(panel))
    for segment, (first, last) in segments.items():
        for horizon in HORIZONS:
            indices = entry_indices(dates, first, last, horizon,
                                    0 if segment == 'selection' else later_seed,
                                    None if segment == 'selection' else sample_count)
            strategy_episodes = [episode(dates, panel, targets[winner], i, horizon, fee)
                                 for i in indices]
            exposure_episodes = [episode(dates, panel, exposure_targets, i, horizon, fee)
                                 for i in indices]
            full_episodes = [hold_episode(dates, panel, i, horizon, fee) for i in indices]
            result = results[segment][str(horizon)]
            result['signal_exposure_equal_weight'] = summarize([
                {'strategy': exposure, 'hold': full}
                for exposure, full in zip(exposure_episodes, full_episodes)])
            paired_return = [strategy['return'] - exposure['return']
                             for strategy, exposure in zip(strategy_episodes, exposure_episodes)]
            paired_drawdown = [strategy['max_drawdown'] - exposure['max_drawdown']
                               for strategy, exposure in zip(strategy_episodes, exposure_episodes)]
            result[winner].update({
                'median_excess_vs_signal_exposure_equal_weight': median(paired_return),
                'p10_excess_vs_signal_exposure_equal_weight': quantile(paired_return, .1),
                'beat_signal_exposure_equal_weight_fraction': sum(x > 0 for x in paired_return) / len(indices),
                'median_paired_drawdown_improvement_vs_signal_exposure_equal_weight': median(paired_drawdown),
                'p10_paired_drawdown_improvement_vs_signal_exposure_equal_weight': quantile(paired_drawdown, .1),
                'fraction_with_drawdown_improvement_vs_signal_exposure_equal_weight':
                    sum(x > 0 for x in paired_drawdown) / len(indices)})
    checks = [results['later_check'][str(h)][winner] for h in HORIZONS]
    typical_risk_reduction = all(
        x['median_paired_drawdown_improvement_vs_signal_exposure_equal_weight'] > 0
        and x['fraction_with_drawdown_improvement_vs_signal_exposure_equal_weight'] > .5
        for x in checks)
    stable_risk_gate = all(
        x['p10_paired_drawdown_improvement_vs_signal_exposure_equal_weight'] >= 0
        for x in checks)
    stable_return_gate = all(
        x['median_excess_vs_signal_exposure_equal_weight'] > 0
        and x['p10_excess_vs_signal_exposure_equal_weight'] >= 0
        for x in checks)
    selection_cash_dominates_downside = all(
        results['selection'][str(h)]['cash']['p10_return']
        > results['selection'][str(h)][winner]['p10_return'] for h in HORIZONS)
    daily_gross = [sum(target.values()) for target in targets[winner]]
    exposure_by_segment = {name: {'mean_target_gross': sum(daily_gross[i] for i, day in enumerate(dates[:-1])
                                                       if first <= day <= last) / sum(first <= day <= last for day in dates[:-1]),
                                  'median_target_gross': median([daily_gross[i] for i, day in enumerate(dates[:-1])
                                                                 if first <= day <= last])}
                           for name, (first, last) in segments.items()}
    return {'schema_version': 1, 'experiment_version': VERSION,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'status': 'retrospective_baseline_candidate',
            'independent_out_of_sample': False,
            'universe': list(UNIVERSE), 'first_date': dates[0], 'last_date': dates[-1],
            'price_basis': 'qfq close', 'signal_rule': 'full available history through t',
            'execution_rule': 'decision t close, fill t+1 close; final close sells holdings without a prior same-close rebalance',
            'cost_rate_per_side': fee, 'buy_lot_modeled': False,
            'budget': {'max_gross_weight': BUDGET.max_gross_weight,
                       'max_stock_weight': BUDGET.max_stock_weight},
            'horizons_sessions': list(HORIZONS),
            'selection_entry_rule': 'all eligible dates, each episode exits before selection boundary',
            'later_check_sample_count_per_horizon': sample_count,
            'sampling_seed_later_check': later_seed if sample_count is not None else None,
            'selection_end': TRAIN_END, 'later_check_start': VALIDATE_START,
            'candidate_function_weights': CANDIDATES,
            'entry_dates': entries, 'results': results,
            'selection_rule': 'max of minimum selection-period p10 absolute return across both horizons; then minimum median return, lower turnover, name',
            'selected_candidate': winner,
            'selection_cash_dominates_downside': selection_cash_dominates_downside,
            'selected_daily_exposure': exposure_by_segment,
            'same_cap_passive_benchmark_fraction': BUDGET.max_gross_weight,
            'same_cap_benchmark_note': 'fixed initial 80% investment; not matched to actual daily strategy exposure',
            'signal_exposure_benchmark_note': 'same daily gross target as selected strategy, equally spread across all stocks',
            'typical_risk_reduction_observed': typical_risk_reduction,
            'stable_risk_gate_passed': stable_risk_gate,
            'stable_return_gate_passed': stable_return_gate,
            'gate_definition': {
                'typical_risk_reduction': 'positive median paired drawdown improvement and more than half the entries improve vs signal-exposure equal weight, at both horizons',
                'stable_risk': 'nonnegative p10 paired drawdown improvement vs signal-exposure equal weight, at both horizons',
                'stable_return': 'positive median and nonnegative p10 paired return excess vs signal-exposure equal weight, at both horizons'},
            'limitations': ['Focus-stock universe selected after history was known.',
                            'Random episodes overlap; fractions are descriptive, not independent success probabilities.',
                            'Adjusted daily closes proxy fills; lot size, price limits, dividends and separate taxes not modeled.']}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/quant/market.sqlite3')
    parser.add_argument('--end', default='2026-09-29')
    parser.add_argument('--out', default='reports/quant/random-entry-risk-baseline-v8-seed202.json')
    parser.add_argument('--later-seed', type=int, default=202)
    parser.add_argument('--samples', type=int, default=SAMPLE_PER_SEGMENT)
    args = parser.parse_args()
    dates, panel, lineage = load_panel(Path(args.db), list(UNIVERSE), args.end)
    report = evaluate(dates, panel, sample_count=args.samples,
                      later_seed=args.later_seed)
    report['source_lineage_last_date'] = lineage
    report['panel_sha256'] = _sha({'dates': dates, 'closes': panel})
    root = Path(__file__).resolve().parents[1]
    tracked = [Path(__file__), root / 'scripts' / 'paper_portfolio.py',
               *[root / 'src' / 'quant_lab' / name for name in
                 ('strategies.py', 'backtest.py', 'decision_ensemble.py',
                  'strategy_catalog.py', 'cost_model.py', 'factor_library.py', 'features.py')]]
    code_manifest = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in tracked}
    report['code_manifest_sha256'] = code_manifest
    report['code_sha256'] = _sha(code_manifest)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError('report already exists; choose a versioned output')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    selected = report['selected_candidate']
    print(json.dumps({'output': str(output.resolve()), 'selected': selected,
                      'typical_risk_reduction_observed': report['typical_risk_reduction_observed'],
                      'stable_risk_gate_passed': report['stable_risk_gate_passed'],
                      'stable_return_gate_passed': report['stable_return_gate_passed'],
                      'selection_126': report['results']['selection']['126'][selected],
                      'later_126': report['results']['later_check']['126'][selected],
                      'later_252': report['results']['later_check']['252'][selected]},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
