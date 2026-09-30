"""Replay as-of reserve substitutions with raw close/action-aware paper ledgers.

Unchanged cases reuse their frozen, source-checked earlier replay. Changed cases
get new dated signals and an independent raw-price ledger. Incomplete exits
remain unknown; this is retrospective proxy research, not executable P&L.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from qlib_2021_anchor_action_alignment import reconcile_same_day_action
from qlib_2021_anchor_conditional_returns import load_actions
from qlib_2021_anchor_asof_reserve_raw_audit import audit as raw_reserve_audit, identifier
from qlib_2021_anchor_halt_replay import simulate_rebalanced_with_halts
from qlib_2021_anchor_missingness_bounds import median_bounds
from qlib_csi300_entry_eligibility import load_verified_source
from qlib_csi300_monthly_snapshot_stress import signal_day
from raw_action_aware_hold_stress import (CAPITAL, FEE, LOT, Action,
                                          parse_action, simulate_hold)
from raw_action_aware_sma_stress import simulate_rebalanced

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-asof-reserve-raw-replay-v1'
POLICIES = ('sma', 'same_exposure_equal', 'passive_fixed_80')
COMPLETE = 'fully_liquidated_raw_action_proxy'


def _read_checked(source_dir: Path, name: str, script: str | None = None) -> tuple[dict, str]:
    payload = (source_dir / name).read_bytes()
    report = json.loads(payload)
    if script and report.get('script_sha256') != hashlib.sha256((
            BASE / 'scripts' / script).read_bytes()).hexdigest():
        raise ValueError(f'{name}: source script changed')
    return report, hashlib.sha256(payload).hexdigest()


def _load_reserve_actions(snapshot: dict, symbols: list[str]) -> dict[str, list[Action]]:
    ids = {identifier(symbol) for symbol in symbols}
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for response in snapshot['responses']:
        if response['instrument_id'] not in ids:
            raise ValueError('unexpected reserve action stock')
        for row in response['rows']:
            grouped[(response['instrument_id'], row['dividOperateDate'])].append(row)
    actions: dict[str, list[Action]] = {stock: [] for stock in ids}
    for (stock, day), rows in grouped.items():
        merged, _ = reconcile_same_day_action(stock, day, rows)
        if day <= '2026-09-28':
            actions[stock].append(parse_action(stock, merged))
    return actions


def _metric_view(outputs: dict | None) -> dict | None:
    if outputs is None:
        return None
    return {policy: {'status': outputs[policy]['status'],
                     'net_return': outputs[policy]['net_return'],
                     'max_drawdown': outputs[policy]['max_drawdown']}
            for policy in POLICIES}


def _replay_changed(case: dict, dates: list[str], date_index: dict[str, int],
                    calendar, masks, features, raw: dict, actions: dict,
                    target_cache: dict) -> dict:
    chosen = tuple(case['asof_members'])
    ids = [identifier(symbol) for symbol in chosen]
    start, end = date_index[case['decision_date']], date_index[case['exit_date']]
    if dates[start + 1] != case['first_fill_date'] or end - start != case['horizon_sessions']:
        raise ValueError('changed case dates differ from frozen window')
    episode = dates[start:end + 1]
    panel, trading = {}, {}
    for stock in ids:
        records = [raw[stock].get(day) for day in episode]
        if any(record is None for record in records):
            return {'status': 'missing_raw_stock_day', 'returns': None}
        if any(record['is_st'] == 1 for record in records):
            return {'status': 'st_day_requires_execution_rule', 'returns': None}
        panel[stock] = [record['close'] for record in records]
        trading[stock] = [record['trade'] == 1 for record in records]
    if any(not trading[stock][1] for stock in ids):
        return {'status': 'entry_order_unfilled', 'returns': None}
    if any(not trading[stock][-1] for stock in ids):
        return {'status': 'planned_exit_halted', 'returns': None}
    halted = sum(not value for row in trading.values() for value in row)
    for stock in ids:
        previous = None
        for day, price, can_trade in zip(episode, panel[stock], trading[stock]):
            if not math.isfinite(price) or price <= 0:
                raise ValueError(f'{stock} {day}: invalid raw price')
            if can_trade:
                previous = price
            elif previous is None or abs(price - previous) > 1e-8:
                raise ValueError(f'{stock} {day}: halted price is not prior close')
    targets = {'sma': {}, 'same_exposure_equal': {}}
    for index in range(start, end):
        key = (chosen, index)
        if key not in target_cache:
            signal = signal_day(calendar, masks, features, index,
                                seed=202, fixed_symbols=chosen)
            target_cache[key] = {
                'sma': {identifier(symbol): weight for symbol, weight
                        in signal['targets']['sma_active_equal'].items()},
                'same_exposure_equal': {identifier(symbol): weight for symbol, weight
                                        in signal['targets']['sma_exposure_equal_20'].items()},
            }
        for policy in targets:
            targets[policy][dates[index]] = target_cache[key][policy]
    pool_actions = {stock: actions[stock] for stock in ids}
    try:
        outputs = {
            'passive_fixed_80': simulate_hold(
                episode, panel, pool_actions, entry=1,
                exit_index=len(episode) - 1, lot_size=LOT),
            'sma': simulate_rebalanced_with_halts(
                episode, panel, pool_actions, targets['sma'], trading,
                entry=1, exit_index=len(episode) - 1),
            'same_exposure_equal': simulate_rebalanced_with_halts(
                episode, panel, pool_actions, targets['same_exposure_equal'], trading,
                entry=1, exit_index=len(episode) - 1),
        }
        if not halted:
            for policy in ('sma', 'same_exposure_equal'):
                ordinary = simulate_rebalanced(
                    episode, panel, pool_actions, targets[policy],
                    entry=1, exit_index=len(episode) - 1, lot_size=LOT)
                special = outputs[policy]
                if ordinary['status'] != special['status'] or any(
                        abs(ordinary[field] - special[field]) > 1e-8
                        for field in ('net_return', 'max_drawdown')
                        if ordinary[field] is not None and special[field] is not None):
                    raise ValueError(f'{case["case_id"]}: normal-day replay parity differs')
    except ValueError as exc:
        if 'fractional bonus settlement' not in str(exc):
            raise
        return {'status': 'unsupported_fractional_bonus_settlement',
                'returns': None, 'reason': str(exc), 'halted_stock_days': halted}
    if any(result['status'] != COMPLETE for result in outputs.values()):
        return {'status': 'unsettled_corporate_action_at_horizon',
                'returns': None, 'halted_stock_days': halted,
                'policy_statuses': {key: value['status'] for key, value in outputs.items()}}
    return {'status': 'fully_liquidated_asof_raw_action_proxy',
            'returns': _metric_view(outputs), 'halted_stock_days': halted,
            'fee_cny': {policy: outputs[policy].get('fee_cny',
                                                   outputs[policy].get('total_fee'))
                        for policy in POLICIES},
            'order_count': {policy: outputs[policy].get('order_count')
                            for policy in POLICIES}}


def _summaries(cases: list[dict]) -> dict:
    groups: dict[tuple[str, int, int], list[dict]] = defaultdict(list)
    for case in cases:
        groups[(case['segment'], case['horizon_sessions'], case['seed'])].append(case)
    if len(groups) != 8:
        raise ValueError('eight frozen groups expected')
    result = {}
    for key, selected in sorted(groups.items()):
        if len(selected) != 768:
            raise ValueError('group case count differs')
        complete = [item for item in selected if item['returns'] is not None]
        conservative = [item for item in complete if not item['factor_anomaly_overlap']]
        policy_medians = {
            policy: {
                'net_return': median(item['returns'][policy]['net_return']
                                     for item in complete) if complete else None,
                'max_drawdown': median(item['returns'][policy]['max_drawdown']
                                       for item in complete) if complete else None,
            }
            for policy in POLICIES
        }
        paired_to_passive_medians = {
            policy: {
                'net_return': median(
                    item['returns'][policy]['net_return']
                    - item['returns']['passive_fixed_80']['net_return']
                    for item in complete) if complete else None,
                'max_drawdown': median(
                    item['returns'][policy]['max_drawdown']
                    - item['returns']['passive_fixed_80']['max_drawdown']
                    for item in complete) if complete else None,
            }
            for policy in ('sma', 'same_exposure_equal')
        }
        scenarios = {}
        for label, observations in (
                ('all_complete_proxies', complete),
                ('known_factor_date_overlap_unknown', conservative)):
            paired_return = [item['returns']['sma']['net_return']
                             - item['returns']['same_exposure_equal']['net_return']
                             for item in observations]
            paired_drawdown = [item['returns']['sma']['max_drawdown']
                               - item['returns']['same_exposure_equal']['max_drawdown']
                               for item in observations]
            scenarios[label] = {
                'observed_paired_return_median': median(paired_return) if paired_return else None,
                'observed_paired_drawdown_median': median(paired_drawdown) if paired_drawdown else None,
                'paired_return_median_bounds': median_bounds(paired_return, len(selected)),
                'paired_drawdown_median_bounds': median_bounds(paired_drawdown, len(selected)),
            }
        result[f'{key[0]}:{key[1]}:{key[2]}'] = {
            'total_windows': len(selected),
            'complete_proxy_count': len(complete),
            'status_counts': dict(Counter(item['status'] for item in selected)),
            'changed_pool_count': sum(item['changed_pool'] for item in selected),
            'factor_anomaly_overlap_in_complete_count': len(complete) - len(conservative),
            'complete_proxy_policy_medians': policy_medians,
            'complete_proxy_paired_to_passive_medians': paired_to_passive_medians,
            'scenarios': scenarios,
        }
    return result


def audit(source_dir: Path, db_path: Path, qlib_root: Path,
          manifest: Path) -> dict:
    names = {
        'asof': ('qlib-2021-anchor-asof-reserve-coverage-v1.json',
                 'qlib_2021_anchor_asof_reserve_audit.py'),
        'reserve_raw': ('qlib-2021-anchor-asof-reserve-raw-audit-v1.json',
                        'qlib_2021_anchor_asof_reserve_raw_audit.py'),
        'actions': ('baostock-asof-reserve-corporate-actions-v1.json',
                    'baostock_asof_reserve_corporate_action_snapshot.py'),
        'anchor': ('qlib-2021-anchor-pool-coverage-audit-v1.json',
                   'qlib_2021_anchor_pool_audit.py'),
        'conditional': ('qlib-2021-anchor-conditional-raw-returns-v1.json',
                        'qlib_2021_anchor_conditional_returns.py'),
        'halt': ('qlib-2021-anchor-pure-halt-replay-v1.json',
                 'qlib_2021_anchor_halt_replay.py'),
        'alignment': ('qlib-2021-anchor-action-alignment-v1.json',
                      'qlib_2021_anchor_action_alignment.py'),
    }
    reports = {}
    hashes = {}
    for key, (filename, script) in names.items():
        reports[key], hashes[key] = _read_checked(source_dir, filename, script)
    if (reports['asof']['case_count'] != 6144
            or reports['reserve_raw']['source_lineage']['asof_reserve_report_sha256'] != hashes['asof']
            or reports['reserve_raw']['source_lineage']['supplier_action_snapshot_sha256'] != hashes['actions']
            or reports['conditional']['source_lineage']['anchor_report_sha256'] != hashes['anchor']
            or reports['conditional']['source_lineage']['alignment_report_sha256'] != hashes['alignment']
            or reports['halt']['source_lineage']['conditional_report_sha256'] != hashes['conditional']):
        raise ValueError('frozen source report chain differs')
    fresh = raw_reserve_audit(source_dir / names['asof'][0],
                              source_dir / names['actions'][0], db_path,
                              qlib_root, manifest)
    for field in ('raw_archive_selected_join_rows_sha256',
                  'raw_archive_selected_join_row_count'):
        if fresh['source_lineage'][field] != reports['reserve_raw']['source_lineage'][field]:
            raise ValueError('reserve raw archive differs from saved audit')
    calendar, masks, features, lineage = load_verified_source(qlib_root, manifest)
    if lineage != reports['anchor']['source_lineage']:
        raise ValueError('Qlib source release changed')
    anchor_symbols = reports['anchor']['selected_symbols_in_hash_order']
    actions = load_actions(source_dir, hashes['anchor'], reports['alignment'], anchor_symbols)
    reserve_symbols = reports['asof']['replacement_symbols_needing_raw_archive']
    if set(actions) & {identifier(symbol) for symbol in reserve_symbols}:
        raise ValueError('reserve collides with original anchor')
    actions.update(_load_reserve_actions(reports['actions'], reserve_symbols))
    all_symbols = sorted({symbol for case in reports['asof']['cases']
                          for symbol in case['asof_members']})
    all_ids = {identifier(symbol) for symbol in all_symbols}
    if set(actions) != all_ids:
        raise ValueError('action ledger does not cover dynamic selection')
    connection = sqlite3.connect(db_path.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        placeholders = ','.join('?' for _ in all_ids)
        rows = connection.execute(f'''
            SELECT b.instrument_id,b.trade_date,b.close,s.tradestatus,s.is_st
            FROM daily_bars b JOIN baostock_daily_status s
              ON b.instrument_id=s.instrument_id AND b.trade_date=s.trade_date
            WHERE b.instrument_id IN ({placeholders})
              AND b.trade_date BETWEEN ? AND ?
        ''', (*sorted(all_ids), '2021-01-04', '2026-09-28')).fetchall()
    finally:
        connection.close()
    raw: dict[str, dict] = {stock: {} for stock in all_ids}
    for stock, day, close, trade, st in rows:
        if day in raw[stock] or trade not in (0, 1) or st not in (0, 1):
            raise ValueError('duplicate or invalid raw status')
        raw[stock][day] = {'close': float(close), 'trade': trade, 'is_st': st}
    dates = [day.isoformat() for day in calendar]
    date_index = {day: index for index, day in enumerate(dates)}
    old = {item['case_id']: item for item in reports['conditional']['cases']}
    halt = {item['case_id']: item for item in reports['halt']['cases']}
    if len(old) != 6144 or len(halt) != 501:
        raise ValueError('old replay case identity differs')
    target_cache = {}
    cases = []
    changed_details = []
    for source in reports['asof']['cases']:
        case_id = source['case_id']
        if not source['changes']:
            previous = halt.get(case_id, old[case_id])
            status = previous['status']
            returns = (_metric_view(previous['returns'])
                       if status.startswith('fully_liquidated') else None)
            if (returns is None) == status.startswith('fully_liquidated'):
                raise ValueError('unchanged status and return availability differ')
            provenance = 'frozen_halt_replay' if case_id in halt else 'frozen_conditional_replay'
            factor_anomaly = old[case_id]['factor_anomaly_overlap']
        else:
            replacement = _replay_changed(source, dates, date_index, calendar,
                                          masks, features, raw, actions, target_cache)
            status = replacement['status']
            returns = replacement['returns']
            provenance = 'new_asof_reserve_raw_replay'
            factor_anomaly = ('SZ002202' in source['asof_members']
                              and source['decision_date'] <= '2025-08-27'
                              and source['exit_date'] >= '2025-08-15')
            changed_details.append({'case_id': case_id, **replacement})
        cases.append({
            'case_id': case_id, 'segment': source['segment'],
            'horizon_sessions': source['horizon_sessions'], 'seed': source['seed'],
            'pool': source['pool'], 'decision_date': source['decision_date'],
            'first_fill_date': source['first_fill_date'], 'exit_date': source['exit_date'],
            'asof_members': source['asof_members'], 'changed_pool': bool(source['changes']),
            'status': status, 'returns': returns, 'source': provenance,
            'factor_anomaly_overlap': factor_anomaly,
        })
    if (len(cases) != 6144 or len({item['case_id'] for item in cases}) != 6144
            or len(changed_details) != reports['asof']['changed_pool_case_count']):
        raise ValueError('dynamic replay denominator differs')
    return {
        'study_version': VERSION,
        'status': 'retrospective_action_aware_proxy_with_unresolved_cases',
        'source_report_sha256': hashes,
        'raw_archive_selected_join_rows_sha256': fresh['source_lineage'][
            'raw_archive_selected_join_rows_sha256'],
        'cost_per_side': FEE, 'capital_cny': CAPITAL, 'buy_lot_shares': LOT,
        'case_count': len(cases), 'changed_pool_case_count': len(changed_details),
        'changed_result_status_counts': dict(Counter(item['status']
                                                       for item in changed_details)),
        'all_result_status_counts': dict(Counter(item['status'] for item in cases)),
        'decision_target_cache_count': len(target_cache),
        'group_summaries': _summaries(cases),
        'changed_case_ledgers': changed_details,
        'cases': cases,
        'historical_official_point_in_time_membership_verified': False,
        'independent_forward_validation': False,
        'limitations': [
            'The 2021-labelled constituent parent and daily features are retrospective, not official PIT universe/data vintages.',
            'Normal raw close and trading status do not prove close-auction fill or limit-queue access.',
            'Halted planned exits, delisting claims, fractional bonus, ST periods and unsettled entitlements retain unknown outcomes.',
            'Overlapping random windows are not independent observations; medians and bounds do not establish stable performance.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path(
        'studies/random-entry-baseline-v1'))
    parser.add_argument('--db', type=Path, default=Path(
        'data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--qlib-root', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-asof-reserve-raw-replay-v1.json'))
    args = parser.parse_args()
    report = audit(args.source_dir, args.db, args.qlib_root,
                   args.qlib_manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'changed_statuses': report['changed_result_status_counts'],
                      'all_statuses': report['all_result_status_counts'],
                      'decision_target_cache_count': report['decision_target_cache_count']},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
