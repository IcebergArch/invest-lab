"""Replay frozen SMA and exposure-matched random entries with a raw-price ledger.

This is an early-history diagnostic, not a point-in-time A-share strategy proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

from qlib_cross_pool_audit import sma_targets
from qlib_deterministic_pool_stress_v2 import fingerprint, load_pools
from random_entry_baseline import exposure_matched_targets
from raw_action_aware_hold_stress import (Action, CAPITAL, FEE, LOT, TARGET_GROSS,
                                          _distribution, load_inputs)

BASE = Path(__file__).resolve().parents[1]
VERSION = 'raw-action-aware-36-pool-sma-stress-v1'
POLICIES = ('sma', 'same_exposure_equal')


def simulate_rebalanced(dates: Sequence[str], panel: Mapping[str, Sequence[float]],
                        actions: Mapping[str, Sequence[Action]],
                        targets: Mapping[str, Mapping[str, float]], *,
                        entry: int, exit_index: int, lot_size: int | None,
                        capital: float = CAPITAL, fee: float = FEE) -> dict:
    if (entry < 1 or exit_index <= entry or exit_index >= len(dates)
            or len(panel) != 3 or set(panel) != set(actions)
            or any(len(series) != len(dates) for series in panel.values())
            or (lot_size is not None and lot_size < 1)
            or not 0 <= fee < .5 or capital <= 0):
        raise ValueError('invalid rebalanced episode contract')
    symbols = sorted(panel)
    on_record: dict[str, list[Action]] = defaultdict(list)
    on_operate: dict[str, list[Action]] = defaultdict(list)
    for symbol in symbols:
        for item in actions[symbol]:
            on_record[item.record_date].append(item)
            on_operate[item.operate_date].append(item)
    tradable = {symbol: 0.0 for symbol in symbols}
    pending_shares: list[tuple[str, float, str]] = []
    pending_cash: list[tuple[float, str]] = []
    entitled: dict[tuple[str, str], float] = {}
    cash, peak, max_drawdown = capital, capital, 0.0
    total_buy = total_sell = total_fee = cash_dividends = bonus_shares = 0.0
    target_gap_sum = max_target_gap = 0.0
    order_count = cash_restricted_buy_count = pending_sale_shortfall_count = 0
    lot_rounding_sale_residual_count = 0
    eligible_action_count = 0
    first_orders = []
    for index in range(entry, exit_index + 1):
        day = dates[index]
        for item in tuple(pending_shares):
            symbol, quantity, release = item
            if release == day:
                tradable[symbol] += quantity
                pending_shares.remove(item)
        for item in tuple(pending_cash):
            amount, release = item
            if release == day:
                cash += amount
                pending_cash.remove(item)
        for action in on_operate.get(day, []):
            base = entitled.pop(action.key, 0.0)
            if base <= 0:
                continue
            eligible_action_count += 1
            if action.cash_per_share:
                amount = base * action.cash_per_share
                cash_dividends += amount
                if action.pay_date == day:
                    cash += amount
                else:
                    pending_cash.append((amount, action.pay_date))
            if action.bonus_per_share:
                quantity = base * action.bonus_per_share
                bonus_shares += quantity
                if lot_size is not None and abs(quantity - round(quantity)) > 1e-6:
                    raise ValueError(f'{action.instrument_id} {day}: unknown fractional bonus settlement')
                if action.stock_list_date == day:
                    tradable[action.instrument_id] += quantity
                else:
                    pending_shares.append((action.instrument_id, quantity,
                                           action.stock_list_date))
        prices = {symbol: panel[symbol][index] for symbol in symbols}
        if any(not math.isfinite(price) or price <= 0 for price in prices.values()):
            raise ValueError(f'{day}: invalid raw close')
        def pending_quantity(symbol: str) -> float:
            return sum(quantity for key, quantity, _ in pending_shares if key == symbol)
        pre_equity = (cash + sum((tradable[symbol] + pending_quantity(symbol))
                                 * prices[symbol] for symbol in symbols)
                      + sum(amount for amount, _ in pending_cash))
        if pre_equity <= 0:
            raise ValueError('nonpositive pretrade equity')
        decision_day = dates[index - 1]
        desired = {} if index == exit_index else dict(targets[decision_day])
        if (set(desired) - set(symbols) or any(
                not math.isfinite(weight) or weight < 0 or weight > .35 + 1e-12
                for weight in desired.values())
                or sum(desired.values()) > TARGET_GROSS + 1e-12):
            raise ValueError(f'{decision_day}: target violates frozen budget')
        target_qty = {}
        for symbol in symbols:
            quantity = desired.get(symbol, 0.0) * pre_equity / prices[symbol]
            target_qty[symbol] = (quantity if lot_size is None else
                                  math.floor(quantity / lot_size + 1e-12) * lot_size)
        # Sell first. Receivable bonus shares cannot be sold before their listing day.
        for symbol in symbols:
            excess = max(0.0, tradable[symbol] + pending_quantity(symbol)
                         - target_qty[symbol])
            unavailable_due_to_pending = excess > tradable[symbol] + 1e-7
            sale = min(tradable[symbol], excess)
            if lot_size is not None and target_qty[symbol] > 0:
                sale = math.floor(sale / lot_size + 1e-12) * lot_size
            if unavailable_due_to_pending:
                pending_sale_shortfall_count += 1
            elif lot_size is not None and excess - sale > 1e-7:
                lot_rounding_sale_residual_count += 1
            if sale > 1e-10:
                gross = sale * prices[symbol]
                cost = gross * fee
                cash += gross - cost
                tradable[symbol] -= sale
                total_sell += gross
                total_fee += cost
                order_count += 1
                if index == entry:
                    first_orders.append({'side': 'sell', 'instrument_id': symbol,
                                         'shares': sale, 'raw_close': prices[symbol]})
        # Target weights are based on pretrade equity; buy in deterministic symbol order.
        for symbol in symbols:
            shortfall = max(0.0, target_qty[symbol] - tradable[symbol]
                            - pending_quantity(symbol))
            buy = (shortfall if lot_size is None else
                   math.floor(shortfall / lot_size + 1e-12) * lot_size)
            if buy <= 1e-10:
                continue
            affordable = cash / (prices[symbol] * (1 + fee))
            if buy > affordable + 1e-9:
                cash_restricted_buy_count += 1
                buy = (max(0.0, affordable) if lot_size is None else
                       math.floor(max(0.0, affordable) / lot_size + 1e-12) * lot_size)
            if buy <= 1e-10:
                continue
            gross = buy * prices[symbol]
            cost = gross * fee
            cash -= gross + cost
            if cash < -1e-7:
                raise ValueError('negative cash after buy')
            tradable[symbol] += buy
            total_buy += gross
            total_fee += cost
            order_count += 1
            if index == entry:
                first_orders.append({'side': 'buy', 'instrument_id': symbol,
                                     'shares': buy, 'raw_close': prices[symbol]})
        for action in on_record.get(day, []):
            base = tradable[action.instrument_id] + pending_quantity(action.instrument_id)
            if base > 0:
                entitled[action.key] = base
        marked = (cash + sum((tradable[symbol] + pending_quantity(symbol))
                             * prices[symbol] for symbol in symbols)
                  + sum(amount for amount, _ in pending_cash))
        if cash < -1e-7 or marked <= 0:
            raise ValueError('invalid posttrade cash or equity')
        if index != exit_index:
            gap = sum(abs((tradable[symbol] + pending_quantity(symbol))
                          * prices[symbol] / marked - desired.get(symbol, 0.0))
                      for symbol in symbols)
            target_gap_sum += gap
            max_target_gap = max(max_target_gap, gap)
        peak = max(peak, marked)
        max_drawdown = min(max_drawdown, marked / peak - 1)
    pending_count = len(pending_shares) + len(pending_cash) + len(entitled)
    common = {'order_count': order_count, 'buy_gross': total_buy,
              'sell_gross': total_sell, 'fee_cny': total_fee,
              'cash_dividend_before_tax': cash_dividends,
              'bonus_shares_credited': bonus_shares,
              'eligible_action_count': eligible_action_count,
              'cash_restricted_buy_count': cash_restricted_buy_count,
              'pending_sale_shortfall_count': pending_sale_shortfall_count,
              'lot_rounding_sale_residual_count': lot_rounding_sale_residual_count,
              'mean_target_weight_l1_gap': target_gap_sum / (exit_index - entry),
              'max_target_weight_l1_gap': max_target_gap,
              'first_orders': first_orders, 'pending_claim_count': pending_count}
    if pending_count:
        return {'status': 'unsettled_corporate_action_at_horizon',
                'net_return': None, 'max_drawdown': None, **common}
    return {'status': 'fully_liquidated_raw_action_proxy',
            'net_return': cash / capital - 1, 'max_drawdown': max_drawdown,
            **common}


def evaluate(stress: dict, raw_dates: Sequence[str], raw_panel: Mapping[str, Sequence[float]],
             actions: Mapping[str, Sequence[Action]], hold_report: dict,
             qlib_dates: Sequence[str], qlib_pools: Mapping[str, Mapping[str, Sequence[float]]]) -> dict:
    if qlib_dates[0] != '2020-09-01' or qlib_dates[-1] != '2026-09-28':
        raise ValueError('Qlib signal calendar differs from frozen study')
    if fingerprint({'dates': qlib_dates, 'pools': qlib_pools}) != stress['panel_sha256']:
        raise ValueError('Qlib signal panel differs from frozen study')
    if set(qlib_pools) != set(stress['pool_symbols']):
        raise ValueError('Qlib pool set differs from frozen study')
    hold_cases = {(row['pool'], row['horizon_sessions'], row['seed'], row['decision_date']): row
                  for row in hold_report['cases']}
    if len(hold_cases) != hold_report['case_count'] or hold_report['case_count'] != 3072:
        raise ValueError('raw hold comparator does not have frozen cases')
    raw_index = {day: index for index, day in enumerate(raw_dates)}
    cases = []
    for pool_name, qpanel in qlib_pools.items():
        symbols = sorted(qpanel)
        if symbols != stress['pool_symbols'][pool_name]:
            raise ValueError(f'{pool_name}: symbol list changed')
        signals = sma_targets(qpanel)
        matched = exposure_matched_targets(signals, symbols)
        target_maps = {
            'sma': {day: target for day, target in zip(qlib_dates, signals)},
            'same_exposure_equal': {day: target for day, target in zip(qlib_dates, matched)},
        }
        panel = {symbol: raw_panel[symbol] for symbol in symbols}
        pool_actions = {symbol: actions[symbol] for symbol in symbols}
        for horizon in (126, 252):
            for seed in (202, 404):
                frozen = stress['cases']['early'][str(horizon)][str(seed)]['pools'][pool_name]
                if len(frozen['cases']) != 64:
                    raise ValueError('frozen case count changed')
                for source in frozen['cases']:
                    key = (pool_name, horizon, seed, source['entry'])
                    hold = hold_cases[key]
                    start = raw_index[source['entry']]
                    end = raw_index[source['exit']]
                    if end - start != horizon or hold['scheduled_exit_date'] != source['exit']:
                        raise ValueError('case calendar or passive comparator differs')
                    outputs = {}
                    for policy in POLICIES:
                        outputs[policy] = {
                            'fractional': simulate_rebalanced(
                                raw_dates, panel, pool_actions, target_maps[policy],
                                entry=start + 1, exit_index=end, lot_size=None),
                            'lot_100': simulate_rebalanced(
                                raw_dates, panel, pool_actions, target_maps[policy],
                                entry=start + 1, exit_index=end, lot_size=LOT),
                        }
                    cases.append({
                        'case_id': hold['case_id'], 'pool': pool_name,
                        'horizon_sessions': horizon, 'seed': seed,
                        'decision_date': source['entry'], 'exit_date': source['exit'],
                        'frozen_adjusted_sma_return': source['sma_return'],
                        'frozen_adjusted_same_exposure_return': source['exposure_equal_return'],
                        'raw_lot_100_passive_return': hold['lot_100_raw_action_hold']['net_return'],
                        'policies': outputs,
                    })
    if len(cases) != 3072 or len({row['case_id'] for row in cases}) != 3072:
        raise ValueError('raw dynamic sample denominator changed')
    summaries = {}
    for horizon in (126, 252):
        for seed in (202, 404):
            selected = [row for row in cases if row['horizon_sessions'] == horizon and row['seed'] == seed]
            joint = [row for row in selected if row['raw_lot_100_passive_return'] is not None
                     and all(row['policies'][policy][basis]['net_return'] is not None
                             for policy in POLICIES for basis in ('fractional', 'lot_100'))]
            def ret(row: dict, policy: str, basis: str) -> float:
                return row['policies'][policy][basis]['net_return']
            pool_medians = {}
            passive_pool_medians = {}
            disjoint = []
            for pool in stress['pool_symbols']:
                part = sorted((row for row in joint if row['pool'] == pool),
                              key=lambda row: row['decision_date'])
                pool_medians[pool] = (median([ret(row, 'sma', 'lot_100')
                                              - ret(row, 'same_exposure_equal', 'lot_100')
                                              for row in part]) if part else None)
                passive_pool_medians[pool] = (median([
                    ret(row, 'sma', 'lot_100') - row['raw_lot_100_passive_return']
                    for row in part]) if part else None)
                last_exit = ''
                for row in part:
                    if row['decision_date'] > last_exit:
                        disjoint.append(row)
                        last_exit = row['exit_date']
            summaries[f'{horizon}:{seed}'] = {
                'sample_count': len(selected), 'jointly_liquidated_count': len(joint),
                'sma_fractional_minus_frozen_proxy': _distribution([
                    ret(row, 'sma', 'fractional') - row['frozen_adjusted_sma_return']
                    for row in joint]),
                'sma_lot_100_minus_fractional': _distribution([
                    ret(row, 'sma', 'lot_100') - ret(row, 'sma', 'fractional')
                    for row in joint]),
                'sma_lot_100_minus_same_exposure_lot_100': _distribution([
                    ret(row, 'sma', 'lot_100')
                    - ret(row, 'same_exposure_equal', 'lot_100') for row in joint]),
                'sma_lot_100_minus_passive_lot_100': _distribution([
                    ret(row, 'sma', 'lot_100') - row['raw_lot_100_passive_return']
                    for row in joint]),
                'sma_lot_100_return': _distribution([
                    ret(row, 'sma', 'lot_100') for row in joint]),
                'same_exposure_lot_100_return': _distribution([
                    ret(row, 'same_exposure_equal', 'lot_100') for row in joint]),
                'pool_median_excess_vs_same_exposure': pool_medians,
                'positive_pool_median_excess_count': sum(
                    value is not None and value > 0 for value in pool_medians.values()),
                'pool_median_excess_vs_passive': passive_pool_medians,
                'positive_pool_median_excess_vs_passive_count': sum(
                    value is not None and value > 0 for value in passive_pool_medians.values()),
                'within_pool_disjoint_jointly_liquidated_count': len(disjoint),
                'within_pool_disjoint_sma_lot_100_minus_same_exposure': _distribution([
                    ret(row, 'sma', 'lot_100')
                    - ret(row, 'same_exposure_equal', 'lot_100') for row in disjoint]),
                'within_pool_disjoint_sma_lot_100_minus_passive': _distribution([
                    ret(row, 'sma', 'lot_100') - row['raw_lot_100_passive_return']
                    for row in disjoint]),
                'sma_lot_100_cash_restricted_case_count': sum(
                    row['policies']['sma']['lot_100']['cash_restricted_buy_count'] > 0
                    for row in selected),
                'sma_lot_100_pending_sale_shortfall_case_count': sum(
                    row['policies']['sma']['lot_100']['pending_sale_shortfall_count'] > 0
                    for row in selected),
                'sma_lot_100_rounding_sale_residual_case_count': sum(
                    row['policies']['sma']['lot_100']['lot_rounding_sale_residual_count'] > 0
                    for row in selected),
            }
    return {'case_count': len(cases), 'cases': cases, 'summaries': summaries}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--db', type=Path, default=Path('data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--qlib-root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/raw-action-aware-36-pool-sma-stress-v1.json'))
    args = parser.parse_args()
    stress_path = args.source_dir / 'qlib-deterministic-pool-stress-v2.json'
    hold_path = args.source_dir / 'raw-action-aware-36-pool-hold-stress-v1.json'
    snapshots = [args.source_dir / f'baostock-36-pool-corporate-actions-{year}-v1.json'
                 for year in (2021, 2022, 2023)]
    stress, raw_dates, raw_panel, actions, raw_lineage = load_inputs(
        stress_path, snapshots, args.db,
        args.source_dir / 'pool-corporate-action-alignment-v1.json')
    hold_bytes = hold_path.read_bytes()
    hold_report = json.loads(hold_bytes)
    if (hold_report.get('study_version') != 'raw-action-aware-36-pool-hold-stress-v1'
            or hold_report['source_lineage'] != raw_lineage):
        raise ValueError('passive raw report differs from current sources')
    for relative, expected_sha in stress['code_file_sha256'].items():
        actual_sha = hashlib.sha256((BASE / relative).read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            raise ValueError(f'frozen stress dependency changed: {relative}')
    qlib_dates, qlib_pools, qlib_lineage = load_pools(args.qlib_root, args.qlib_manifest)
    if qlib_lineage != stress['source_lineage']:
        raise ValueError('Qlib release or pool selection differs from frozen stress')
    result = evaluate(stress, raw_dates, raw_panel, actions, hold_report,
                      qlib_dates, qlib_pools)
    report = {
        'study_version': VERSION,
        'status': 'retrospective_raw_action_aware_sma_stress_not_stable_baseline',
        'early_history_only': True,
        'historical_point_in_time_universe_confirmed': False,
        'actual_exchange_execution_confirmed': False,
        'source_lineage': {**raw_lineage,
                           'passive_hold_report_path': str(hold_path.resolve().relative_to(BASE)),
                           'passive_hold_report_sha256': hashlib.sha256(hold_bytes).hexdigest(),
                           'qlib_release': qlib_lineage['release_tag'],
                           'qlib_manifest_sha256': qlib_lineage['manifest_sha256'],
                           'qlib_panel_sha256': stress['panel_sha256'],
                           'frozen_implementation_sha256_verified': True},
        'capital_cny': CAPITAL, 'buy_fee_per_side': FEE, 'sell_fee_per_side': FEE,
        'buy_lot_shares': LOT,
        'entry_rule': 'Frozen SMA-positive random date t; target at t, raw-close proxy fill at t+1.',
        'policy_rules': {'sma': 'Frozen 20/60-day SMA with 80% aggregate and 35% per-name caps, refreshed daily.',
                         'same_exposure_equal': 'Each SMA day gross target spread equally across all three names, refreshed daily.'},
        **result,
        'limitations': [
            '2021–2023 only; all 36 names were selected with full future history and are not a point-in-time investable universe.',
            'Qlib 2026 adjusted prices supply SMA signals; raw prices and later BaoStock action snapshots supply fills and valuation.',
            'Gross cash dividends, simplified bonus delivery and share settlement; no dividend tax, rights subscription or full odd-lot rules.',
            'Trading status is observed, but price-limit queue, intraday fillability and slippage are not modeled.',
            'Many 126/252-day windows overlap and two seeds repeat dates; case counts are not independent success trials.',
            'This retrospective stress cannot certify a stable or deployable portfolio strategy.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()), 'cases': result['case_count'],
                      'status': report['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
