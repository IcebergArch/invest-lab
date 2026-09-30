"""Audit the frozen random-entry baseline by later-period entry year.

Read-only analysis of completed historical closes. Does not alter policy or
paper accounts and does not claim an independent forward sample.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from quant_lab.decision_pipeline import sha256_json
from paper_portfolio import load_panel
from random_entry_baseline import (HORIZONS, UNIVERSE, VALIDATE_START,
                                   candidate_targets, cash_matched_hold_episode,
                                   entry_indices, episode, exposure_matched_targets,
                                   hold_episode, quantile)

VERSION = 'later-entry-year-cohorts-v2'


def evaluate(dates: list[str], panel: dict[str, list[float]], fee: float) -> dict:
    targets = candidate_targets(panel)['sma']
    exposure_targets = exposure_matched_targets(targets, sorted(panel))
    result = {}
    for horizon in HORIZONS:
        indices = entry_indices(dates, VALIDATE_START, dates[-1], horizon, 0, None)
        groups: dict[str, list[dict]] = defaultdict(list)
        for index in indices:
            strategy = episode(dates, panel, targets, index, horizon, fee)
            full = hold_episode(dates, panel, index, horizon, fee)
            matched = cash_matched_hold_episode(dates, panel, index, horizon, fee)
            exposure = episode(dates, panel, exposure_targets, index, horizon, fee)
            groups[dates[index][:4]].append({'entry_date': dates[index],
                                               'exit_date': dates[index + horizon],
                                               'strategy': strategy, 'full': full,
                                               'matched': matched, 'exposure': exposure})
        result[str(horizon)] = {}
        for year, rows in sorted(groups.items()):
            def stats(name: str) -> dict:
                return {'median_return': median(row[name]['return'] for row in rows),
                        'p10_return': quantile([row[name]['return'] for row in rows], .1),
                        'median_max_drawdown': median(row[name]['max_drawdown'] for row in rows)}
            matched_excess = [row['strategy']['return'] - row['matched']['return'] for row in rows]
            full_excess = [row['strategy']['return'] - row['full']['return'] for row in rows]
            exposure_excess = [row['strategy']['return'] - row['exposure']['return'] for row in rows]
            exposure_drawdown = [row['strategy']['max_drawdown'] - row['exposure']['max_drawdown']
                                 for row in rows]
            result[str(horizon)][year] = {
                'entries': len(rows), 'first_entry': rows[0]['entry_date'],
                'last_entry': rows[-1]['entry_date'],
                'latest_exit': max(row['exit_date'] for row in rows),
                'strategy': stats('strategy'),
                'cash_matched_equal_hold': stats('matched'),
                'full_equal_hold': stats('full'),
                'signal_exposure_equal_weight': stats('exposure'),
                'median_excess_vs_signal_exposure_equal_weight': median(exposure_excess),
                'median_paired_drawdown_improvement_vs_signal_exposure_equal_weight': median(exposure_drawdown),
                'median_excess_vs_cash_matched_hold': median(matched_excess),
                'beat_cash_matched_hold_fraction': sum(x > 0 for x in matched_excess) / len(rows),
                'median_excess_vs_full_hold': median(full_excess),
                'beat_full_hold_fraction': sum(x > 0 for x in full_excess) / len(rows),
            }
    return {'schema_version': 1, 'audit_version': VERSION,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'status': 'retrospective_entry_year_audit',
            'independent_out_of_sample': False,
            'universe': list(UNIVERSE), 'period_start': VALIDATE_START,
            'period_end': dates[-1], 'fee_rate_per_side': fee,
            'horizons_sessions': list(HORIZONS),
            'entry_rule': 'all eligible signal dates, grouped by entry calendar year; exit inside later period',
            'panel_sha256': sha256_json({'dates': dates, 'closes': panel}),
            'cohorts': result,
            'limitations': ['Years and episodes share market regimes and overlap; they are not independent trials.',
                            'Hindsight-selected three-stock universe and adjusted-close proxy.',
                            'Policy and audit were designed after historical outcomes were available.']}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/quant/market.sqlite3')
    parser.add_argument('--end', default='2026-09-29')
    parser.add_argument('--out', default='studies/random-entry-baseline-v1/later-entry-year-cohorts-v2.json')
    args = parser.parse_args()
    dates, panel, _ = load_panel(Path(args.db), list(UNIVERSE), args.end)
    report = evaluate(dates, panel, .0005)
    root = Path(__file__).resolve().parents[1]
    tracked = [Path(__file__), root / 'scripts' / 'random_entry_baseline.py',
               root / 'scripts' / 'paper_portfolio.py',
               *[root / 'src' / 'quant_lab' / name for name in
                 ('strategies.py', 'backtest.py', 'decision_ensemble.py',
                  'strategy_catalog.py', 'cost_model.py', 'factor_library.py', 'features.py')]]
    report['code_manifest_sha256'] = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in tracked}
    report['code_sha256'] = sha256_json(report['code_manifest_sha256'])
    policy = json.loads((root / 'policies' / 'random-entry-risk-baseline-v1.json').read_text())
    report['policy_sha256'] = sha256_json(policy)
    output = Path(args.out)
    if output.exists():
        raise ValueError('audit already exists; use a versioned output')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'output': str(output.resolve()), 'cohorts': report['cohorts']},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
