"""Conditional raw-action returns for a 2021-labelled, non-survivor anchor pool.

All frozen random windows remain in the denominator. Returns are calculated
only when the three-stock window has a daily normal trading-price proxy;
unpriceable or unsettled cases remain explicit, never assigned zero P&L.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from qlib_csi300_entry_eligibility import load_verified_source
from qlib_csi300_monthly_snapshot_stress import signal_day
from qlib_2021_anchor_action_alignment import reconcile_same_day_action
from raw_action_aware_hold_stress import (CAPITAL, FEE, LOT, _distribution,
                                          parse_action, simulate_hold)
from raw_action_aware_sma_stress import simulate_rebalanced

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-conditional-raw-returns-v1'
POLICIES = ('sma', 'same_exposure_equal', 'passive_fixed_80')
FACTOR_ANOMALY_SYMBOL = 'SZ002202'
FACTOR_ANOMALY_FIRST = '2025-08-15'
FACTOR_ANOMALY_LAST = '2025-08-27'


def load_actions(source_dir: Path, anchor_sha: str, alignment: dict,
                 symbols: list[str]) -> dict:
    allowed = {'stock:' + symbol[2:] + '.' + symbol[:2] for symbol in symbols}
    grouped = defaultdict(list)
    for source in alignment['action_snapshots']:
        path = BASE / source['path']
        payload = path.read_bytes()
        snap = json.loads(payload)
        if (hashlib.sha256(payload).hexdigest() != source['sha256']
                or snap['source']['anchor_report_sha256'] != anchor_sha
                or {item['instrument_id'] for item in snap['responses']} != allowed):
            raise ValueError('supplier action snapshot differs from aligned source')
        for response in snap['responses']:
            for row in response['rows']:
                grouped[(response['instrument_id'], row['dividOperateDate'])].append(row)
    actions = {identifier: [] for identifier in allowed}
    for key, rows in grouped.items():
        merged, _ = reconcile_same_day_action(key[0], key[1], rows)
        if key[1] <= '2026-09-28':
            actions[key[0]].append(parse_action(key[0], merged))
    if sum(len(items) for items in actions.values()) != alignment['supplier_action_rows_within_qlib_cutoff']:
        raise ValueError('deduplicated action count differs')
    return actions


def replay(window_report: dict, calendar, masks, features,
           raw: dict, actions: dict) -> dict:
    dates = [day.isoformat() for day in calendar]
    date_index = {day: index for index, day in enumerate(dates)}
    target_maps = {}
    for pool_name, members in window_report['pool_symbols'].items():
        targets = {'sma': {}, 'same_exposure_equal': {}}
        for index, day in enumerate(dates):
            if day < '2021-01-04' or day >= '2026-09-28':
                continue
            signal = signal_day(calendar, masks, features, index,
                                seed=202, fixed_symbols=members)
            for policy, source_key in (('sma', 'sma_active_equal'),
                                       ('same_exposure_equal', 'sma_exposure_equal_20')):
                targets[policy][day] = {
                    'stock:' + symbol[2:] + '.' + symbol[:2]: weight
                    for symbol, weight in signal['targets'][source_key].items()}
        target_maps[pool_name] = targets
    out = []
    for source in window_report['cases']:
        common = {key: source[key] for key in (
            'case_id', 'segment', 'horizon_sessions', 'seed', 'pool',
            'members', 'decision_date', 'first_fill_date', 'exit_date')}
        overlaps_factor_anomaly = (FACTOR_ANOMALY_SYMBOL in source['members']
                                   and source['decision_date'] <= FACTOR_ANOMALY_LAST
                                   and source['exit_date'] >= FACTOR_ANOMALY_FIRST)
        if not source['continuous_normal_non_st_status']:
            out.append({**common, 'status': 'daily_raw_close_proxy_unavailable',
                        'factor_anomaly_overlap': overlaps_factor_anomaly,
                        'first_halt_or_exit_gap': source['first_halt_or_exit_gap'],
                        'has_st': source['has_st'], 'returns': None})
            continue
        start = date_index[source['decision_date']]
        end = date_index[source['exit_date']]
        if end - start != source['horizon_sessions']:
            raise ValueError('window calendar differs')
        episode_dates = dates[start:end + 1]
        pool_ids = ['stock:' + symbol[2:] + '.' + symbol[:2]
                    for symbol in source['members']]
        panel = {}
        for identifier in pool_ids:
            prices = [raw[identifier].get(day) for day in episode_dates]
            if any(price is None or price <= 0 for price in prices[1:]):
                raise ValueError(f'{identifier}: trading-status eligible episode has missing raw price')
            # The decision-day raw close is not used for a t+1 fill or mark.
            # A halted t may lack a raw close, while the signal filter handles it.
            if prices[0] is None or prices[0] <= 0:
                prices[0] = prices[1]
            panel[identifier] = prices
        pool_actions = {identifier: actions[identifier] for identifier in pool_ids}
        try:
            outputs = {
                'passive_fixed_80': simulate_hold(
                    episode_dates, panel, pool_actions, entry=1,
                    exit_index=end - start, lot_size=LOT),
                'sma': simulate_rebalanced(
                    episode_dates, panel, pool_actions,
                    target_maps[source['pool']]['sma'], entry=1,
                    exit_index=end - start, lot_size=LOT),
                'same_exposure_equal': simulate_rebalanced(
                    episode_dates, panel, pool_actions,
                    target_maps[source['pool']]['same_exposure_equal'],
                    entry=1, exit_index=end - start, lot_size=LOT),
            }
        except ValueError as exc:
            if 'fractional bonus settlement' not in str(exc):
                raise
            out.append({**common, 'status': 'unsupported_fractional_bonus_settlement',
                        'factor_anomaly_overlap': overlaps_factor_anomaly,
                        'first_halt_or_exit_gap': None, 'has_st': False,
                        'returns': None, 'reason': str(exc)})
            continue
        status = ('fully_liquidated_conditional_proxy' if all(
            value['status'] == 'fully_liquidated_raw_action_proxy'
            for value in outputs.values()) else 'unsettled_corporate_action_at_horizon')
        out.append({**common, 'status': status,
                    'factor_anomaly_overlap': overlaps_factor_anomaly,
                    'first_halt_or_exit_gap': None, 'has_st': False,
                    'returns': outputs})
    if len(out) != window_report['case_count'] or len({x['case_id'] for x in out}) != len(out):
        raise ValueError('conditional replay dropped a window')
    summaries = {}
    for segment in ('early', 'later'):
        summaries[segment] = {}
        for horizon in (126, 252):
            for seed in (202, 404):
                selected = [x for x in out if x['segment'] == segment
                            and x['horizon_sessions'] == horizon and x['seed'] == seed]
                complete = [x for x in selected if x['status'] == 'fully_liquidated_conditional_proxy']
                without_factor_anomaly = [
                    x for x in complete if not x['factor_anomaly_overlap']]
                def ret(item: dict, policy: str) -> float:
                    return item['returns'][policy]['net_return']
                pool_medians = {}
                disjoint = []
                for pool_name in window_report['pool_symbols']:
                    subset = sorted((x for x in complete if x['pool'] == pool_name),
                                    key=lambda x: x['decision_date'])
                    pool_medians[pool_name] = median([
                        ret(x, 'sma') - ret(x, 'same_exposure_equal')
                        for x in subset]) if subset else None
                    last_exit = ''
                    for item in subset:
                        if item['decision_date'] > last_exit:
                            disjoint.append(item)
                            last_exit = item['exit_date']
                summaries[segment][f'{horizon}:{seed}'] = {
                    'total_windows': len(selected),
                    'raw_status_ineligible_count': sum(
                        x['status'] == 'daily_raw_close_proxy_unavailable' for x in selected),
                    'unsupported_bonus_count': sum(
                        x['status'] == 'unsupported_fractional_bonus_settlement' for x in selected),
                    'unsettled_action_count': sum(
                        x['status'] == 'unsettled_corporate_action_at_horizon' for x in selected),
                    'jointly_liquidated_conditional_count': len(complete),
                    'factor_anomaly_overlap_total_count': sum(
                        x['factor_anomaly_overlap'] for x in selected),
                    'factor_anomaly_overlap_in_liquidated_count': sum(
                        x['factor_anomaly_overlap'] for x in complete),
                    'conditional_count_excluding_known_factor_date_mismatch': len(
                        without_factor_anomaly),
                    'sma_return_on_conditional_subset': _distribution([
                        ret(x, 'sma') for x in complete]),
                    'equal_return_on_conditional_subset': _distribution([
                        ret(x, 'same_exposure_equal') for x in complete]),
                    'passive_return_on_conditional_subset': _distribution([
                        ret(x, 'passive_fixed_80') for x in complete]),
                    'sma_minus_equal_on_conditional_subset': _distribution([
                        ret(x, 'sma') - ret(x, 'same_exposure_equal') for x in complete]),
                    'sma_minus_equal_excluding_known_factor_date_mismatch': _distribution([
                        ret(x, 'sma') - ret(x, 'same_exposure_equal')
                        for x in without_factor_anomaly]),
                    'sma_minus_passive_on_conditional_subset': _distribution([
                        ret(x, 'sma') - ret(x, 'passive_fixed_80') for x in complete]),
                    'sma_minus_equal_drawdown_on_conditional_subset': _distribution([
                        x['returns']['sma']['max_drawdown']
                        - x['returns']['same_exposure_equal']['max_drawdown']
                        for x in complete]),
                    'positive_pool_median_sma_minus_equal_count': sum(
                        value is not None and value > 0 for value in pool_medians.values()),
                    'within_pool_disjoint_conditional_count': len(disjoint),
                    'within_pool_disjoint_sma_minus_equal': _distribution([
                        ret(x, 'sma') - ret(x, 'same_exposure_equal') for x in disjoint]),
                }
    return {'case_count': len(out), 'summaries': summaries, 'cases': out}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--db', type=Path, default=Path('data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--qlib-root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-conditional-raw-returns-v1.json'))
    args = parser.parse_args()
    paths = {key: args.source_dir / filename for key, filename in {
        'anchor': 'qlib-2021-anchor-pool-coverage-audit-v1.json',
        'window': 'qlib-2021-anchor-random-window-eligibility-v1.json',
        'raw': 'qlib-2021-anchor-raw-status-audit-v1.json',
        'alignment': 'qlib-2021-anchor-action-alignment-v1.json',
    }.items()}
    payloads = {key: path.read_bytes() for key, path in paths.items()}
    reports = {key: json.loads(payload) for key, payload in payloads.items()}
    if (reports['alignment']['status'] != 'unmatched_large_factor_jumps_need_review'
            or len(reports['alignment']['unmatched_large_factor_jumps']) != 1
            or reports['window']['case_count'] != 6144
            or reports['window']['source_lineage']['anchor_report_sha256']
            != hashlib.sha256(payloads['anchor']).hexdigest()
            or reports['window']['source_lineage']['raw_status_report_sha256']
            != hashlib.sha256(payloads['raw']).hexdigest()):
        raise ValueError('anchored source report contract differs')
    symbols = reports['anchor']['selected_symbols_in_hash_order']
    actions = load_actions(args.source_dir, hashlib.sha256(payloads['anchor']).hexdigest(),
                           reports['alignment'], symbols)
    calendar, masks, features, qlineage = load_verified_source(
        args.qlib_root, args.qlib_manifest)
    if qlineage != reports['anchor']['source_lineage']:
        raise ValueError('Qlib release differs from anchor source')
    ids = ['stock:' + symbol[2:] + '.' + symbol[:2] for symbol in symbols]
    connection = sqlite3.connect(args.db.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        placeholders = ','.join('?' for _ in ids)
        rows = connection.execute(f'''
            SELECT instrument_id,trade_date,close FROM daily_bars
            WHERE instrument_id IN ({placeholders})
              AND trade_date BETWEEN ? AND ?
        ''', (*ids, '2021-01-04', '2026-09-28')).fetchall()
    finally:
        connection.close()
    if len(rows) != reports['raw']['raw_archive']['selected_joined_row_count']:
        raise ValueError('raw price row count differs from status audit')
    raw = {identifier: {} for identifier in ids}
    for identifier, day, close in rows:
        raw[identifier][day] = close
    result = replay(reports['window'], calendar, masks, features, raw, actions)
    report = {
        'study_version': VERSION,
        'status': 'conditional_priceable_subset_only_not_stable_baseline',
        'historical_point_in_time_universe_confirmed': False,
        'independent_out_of_sample_confirmed': False,
        'source_lineage': {key + '_report_sha256': hashlib.sha256(value).hexdigest()
                           for key, value in payloads.items()},
        'source_signal_rule': 'Qlib 2026 release; each t uses last 60 adjusted closes, current volume, SMA20>SMA60; unavailable names excluded.',
        'cost_per_side': FEE, 'buy_lot_shares': LOT, 'capital_cny': CAPITAL,
        **result,
        'limitations': [
            'P&L is conditional on every held stock having a normal raw trading-day price in the window; missing windows remain in the denominator without imputed returns.',
            '2021 Qlib member label and adjusted signal prices are a 2026-vintage retrospective source, not verified historical PIT data.',
            'One 2025-08-27 factor jump for SZ002202 is not same-day aligned to the supplier dividend operation; affected windows are flagged.',
            'Bonus-share fractional settlement, taxes, rights, merger/delisting consideration, limit queues and intraday execution remain incomplete.',
            'Many windows overlap and seeds can repeat dates; conditional returns do not estimate whole-universe expected performance.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'cases': result['case_count'],
                      'liquidated': sum(s['jointly_liquidated_conditional_count']
                                        for group in result['summaries'].values()
                                        for s in group.values())}, ensure_ascii=False))


if __name__ == '__main__':
    main()
