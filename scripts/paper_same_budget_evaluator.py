"""Read-only, same-budget shadow benchmark for runtime2 forward paper accounts.

The shadow buys 80% equally across the frozen universe at the first observed
future raw close. It is then marked at each observed raw close without
rebalancing. All original account sessions are verified before comparison.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_portfolio import digest, read_json
from quant_lab.decision_pipeline import PIPELINE_VERSION, sha256_json, validate_policy
from quant_lab.paper_execution import execute_target
from quant_paper_pool import runtime_hash, sessions

NORMAL_FEE = 0.0005
STRESS_FEE = 0.0015
TARGET_GROSS = 0.8
BUY_LOT = 100
CHINA_ZONE = ZoneInfo('Asia/Shanghai')
ACCOUNT_FIELDS = ('strategy', 'baseline', 'stress_strategy', 'stress_baseline')
ORDER_FIELDS = ('orders', 'baseline_orders', 'stress_orders', 'stress_baseline_orders')


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _finite_positive(value: object, label: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool), f'{label}: not numeric')
    number = float(value)
    _require(math.isfinite(number) and number > 0, f'{label}: not finite and positive')
    return number


def _aware_timestamp(value: object, label: str) -> datetime:
    _require(isinstance(value, str), f'{label}: missing timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(parsed.tzinfo is not None, f'{label}: timezone missing')
    return parsed.astimezone(CHINA_ZONE)


def _timestamp(value: object, label: str, day: str) -> None:
    parsed = _aware_timestamp(value, label)
    cutoff = datetime.combine(date.fromisoformat(day), time(15, 5), CHINA_ZONE)
    _require(parsed >= cutoff,
             f'{label}: fetched before session close cutoff')
    _require(parsed <= datetime.now(CHINA_ZONE), f'{label}: fetched in the future')


def _sha(value: object, label: str) -> None:
    _require(isinstance(value, str) and len(value) == 64
             and all(c in '0123456789abcdef' for c in value), f'{label}: invalid SHA-256')


def _check_bars(row: dict, day: str, keys: list[str]) -> None:
    raw = row.get('raw_bars')
    qfq = row.get('qfq_lineage')
    _require(isinstance(raw, dict) and set(raw) == set(keys), f'{day}: incomplete raw bars')
    _require(isinstance(qfq, dict) and set(qfq) == set(keys), f'{day}: incomplete qfq lineage')
    for key in keys:
        bar = raw[key]
        _require(isinstance(bar, dict) and bar.get('adjustment') == 'none'
                 and bar.get('source_id') == 'eastmoney_kline', f'{day} {key}: wrong raw source')
        _finite_positive(bar.get('close'), f'{day} {key} raw close')
        _finite_positive(bar.get('volume_lots'), f'{day} {key} volume')
        _sha(bar.get('payload_hash'), f'{day} {key} raw payload')
        _timestamp(bar.get('fetched_at'), f'{day} {key} raw fetched_at', day)
        signal = qfq[key]
        _require(isinstance(signal, dict) and signal.get('instrument_id') == key
                 and signal.get('trade_date') == day and signal.get('adjustment') == 'qfq'
                 and signal.get('source_id') == 'eastmoney_kline',
                 f'{day} {key}: wrong qfq lineage')
        _finite_positive(signal.get('close'), f'{day} {key} qfq close')
        _sha(signal.get('payload_hash'), f'{day} {key} qfq payload')
        _require(isinstance(signal.get('run_id'), str) and bool(signal['run_id']),
                 f'{day} {key}: qfq run id missing')
        _timestamp(signal.get('fetched_at'), f'{day} {key} qfq fetched_at', day)


def _check_decision(decision: dict, config: dict, day: str) -> None:
    policy = config['policy']
    _require(isinstance(decision, dict), f'{day}: decision missing')
    claimed = decision.get('decision_sha256')
    _sha(claimed, f'{day} decision')
    unhashed = {key: value for key, value in decision.items() if key != 'decision_sha256'}
    _require(sha256_json(unhashed) == claimed, f'{day}: decision hash mismatch')
    _require(decision.get('pipeline_version') == PIPELINE_VERSION
             and decision.get('policy_id') == policy['policy_id']
             and decision.get('policy_sha256') == config['policy_sha256']
             and decision.get('mode') == policy['mode']
             and decision.get('asof') == day
             and decision.get('budget') == policy['budget'], f'{day}: decision identity mismatch')
    _sha(decision.get('signal_panel_sha256'), f'{day} signal panel')
    targets = decision.get('target_weights')
    _require(isinstance(targets, dict) and set(targets) == set(config['universe']),
             f'{day}: decision universe mismatch')
    _require(all(isinstance(weight, (int, float)) and not isinstance(weight, bool)
                 and math.isfinite(weight) and weight >= 0 for weight in targets.values()),
             f'{day}: invalid decision target')
    _require(sum(targets.values()) <= policy['budget']['max_gross_weight'] + 1e-12
             and all(weight <= policy['budget']['max_stock_weight'] + 1e-12
                     for weight in targets.values()), f'{day}: decision budget exceeded')
    _require(isinstance(decision.get('cash_target_weight'), (int, float))
             and math.isclose(decision['cash_target_weight'], 1 - sum(targets.values()),
                              rel_tol=0, abs_tol=1e-12), f'{day}: decision cash mismatch')


def _empty_account(initial: float, keys: list[str], *, basis: bool) -> dict:
    result = {'cash': initial, 'shares': {key: 0 for key in keys}, 'equity': initial}
    if basis:
        result['cost_basis'] = {key: 0.0 for key in keys}
    return result


def _mark(account: dict, raw: dict, keys: list[str]) -> dict:
    return {**account, 'equity': account['cash']
            + sum(account['shares'][key] * raw[key]['close'] for key in keys)}


def _drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def _fees(orders: list[dict]) -> float:
    return sum(order['fee'] for order in orders)


def _latest_final_date() -> date:
    now = datetime.now(CHINA_ZONE)
    return now.date() if now.time() >= time(16, 0) else now.date() - timedelta(days=1)


def _check_config(config: dict, current_runtime: str) -> tuple[dict, list[str], float]:
    _require(config.get('schema_version') == 1
             and config.get('kind') == 'forward_strategy_agnostic_paper_pool',
             'unsupported paper account configuration')
    policy = config.get('policy')
    _require(isinstance(policy, dict), 'policy missing')
    validate_policy(policy)
    _require(config.get('policy_sha256') == sha256_json(policy), 'policy hash mismatch')
    _require(config.get('runtime_sha256') == current_runtime, 'frozen runtime hash mismatch')
    keys = sorted(policy['universe'])
    _require(config.get('universe') == keys and config.get('start_asof') == policy['start_asof'],
             'policy and config universe or date differ')
    _require(config.get('signal_price_basis') == 'qfq'
             and config.get('fill_price_basis') == 'EastMoney unadjusted daily close'
             and config.get('fill_assumption') == 'decision at close t, proxy fill at close t+1',
             'unsupported price or execution basis')
    created = _aware_timestamp(config.get('created_at'), 'config created_at')
    _require(created <= datetime.now(CHINA_ZONE), 'config created in the future')
    _require(policy['cost_rate'] == NORMAL_FEE
             and policy['stress_cost_rate'] == STRESS_FEE
             and policy['buy_lot'] == BUY_LOT,
             'fee or lot size differs from benchmark contract')
    _require(policy['budget']['max_gross_weight'] == TARGET_GROSS
             and TARGET_GROSS / len(keys) <= policy['budget']['max_stock_weight'] + 1e-12,
             '80% equal allocation is outside frozen budget')
    initial = _finite_positive(policy['initial_cash'], 'initial cash')
    return policy, keys, initial


def evaluate_account(root: Path) -> dict:
    """Verify a runtime2 ledger and replay both equal-weight controls read-only."""
    root = Path(root)
    start_runtime = runtime_hash()
    config = read_json(root / 'config.json')
    policy, keys, initial = _check_config(config, start_runtime)
    rows = sessions(root)  # verifies each filename and previous-record hash
    opening = rows[0]
    start = config['start_asof']
    latest_final = _latest_final_date()
    _require(date.fromisoformat(start) <= latest_final,
             'opening session date is future or not yet final')
    _require(opening.get('date') == start
             and opening.get('status') == 'pending_first_future_session'
             and opening.get('prior_sha256') is None, 'invalid opening session')
    _check_bars(opening, start, keys)
    account_created = _aware_timestamp(config['created_at'], 'config created_at')
    for key in keys:
        for field in ('raw_bars', 'qfq_lineage'):
            fetched = _aware_timestamp(opening[field][key]['fetched_at'],
                                       f'{start} {key} {field} fetched_at')
            _require(fetched <= account_created,
                     f'{start}: opening input fetched after account creation')
    _check_decision(opening.get('pending_decision'), config, start)
    for field, order_field in zip(ACCOUNT_FIELDS, ORDER_FIELDS):
        if order_field in opening:
            _require(opening[order_field] == [], f'opening {order_field} not empty')
        _require(opening.get(field) == _empty_account(initial, keys,
                                                     basis=field in ('strategy', 'stress_strategy')),
                 f'opening {field} not empty')
    _require(opening.get('orders') == [] and opening.get('baseline_orders') == [],
             'opening order ledger not empty')

    previous = opening
    original = {field: opening[field] for field in ACCOUNT_FIELDS}
    shadow = {
        'normal': _empty_account(initial, keys, basis=False),
        'stress': _empty_account(initial, keys, basis=False),
    }
    matched = {
        'normal': _empty_account(initial, keys, basis=False),
        'stress': _empty_account(initial, keys, basis=False),
    }
    series = {name: [initial] for name in
              ('strategy', 'shadow', 'matched_exposure_equal',
               'stress_strategy', 'stress_shadow', 'stress_matched_exposure_equal')}
    fee_totals = {name: 0.0 for name in series}
    timeline = []
    first_orders = {'normal': [], 'stress': []}
    matched_order_counts = {'normal': 0, 'stress': 0}
    for index, row in enumerate(rows[1:], start=1):
        day = row.get('date')
        _require(isinstance(day, str), f'session {index}: missing date')
        _require(date.fromisoformat(day) > date.fromisoformat(previous['date']),
                 f'{day}: date not increasing')
        _require(date.fromisoformat(day) <= latest_final,
                 f'{day}: session date is future or not yet final')
        _require(row.get('status') == 'observed', f'{day}: not an observed session')
        fill_close = datetime.combine(date.fromisoformat(day), time(15, 0), CHINA_ZONE)
        if index == 1:
            _require(_aware_timestamp(config['created_at'], 'config created_at') < fill_close,
                     f'{day}: account created after first proxy fill close')
        for key in keys:
            for field in ('raw_bars', 'qfq_lineage'):
                fetched = _aware_timestamp(previous[field][key]['fetched_at'],
                                           f'{previous["date"]} {key} {field} fetched_at')
                _require(fetched < fill_close,
                         f'{day}: prior session input fetched after proxy fill close')
        _check_bars(row, day, keys)
        _check_decision(row.get('pending_decision'), config, day)
        prior_decision = previous['pending_decision']
        target = prior_decision['target_weights']
        _require(row.get('executed_decision_sha256') == prior_decision['decision_sha256']
                 and row.get('executed_decision_asof') == previous['date']
                 and row.get('executed_targets') == target,
                 f'{day}: executed decision link mismatch')
        raw = row['raw_bars']
        fees_before = fee_totals.copy()
        day_matched_orders = {}
        for field, order_field, rate in (
                ('strategy', 'orders', NORMAL_FEE),
                ('stress_strategy', 'stress_orders', STRESS_FEE)):
            expected, orders = execute_target(original[field], target, raw, rate,
                                              buy_lot=BUY_LOT)
            _require(row.get(field) == expected and row.get(order_field) == orders,
                     f'{day}: {field} ledger or orders mismatch')
            original[field] = expected
            fee_totals[field] += _fees(orders)
        for field, order_field, rate in (
                ('baseline', 'baseline_orders', NORMAL_FEE),
                ('stress_baseline', 'stress_baseline_orders', STRESS_FEE)):
            if index == 1:
                equal_target = {key: 1 / len(keys) for key in keys}
                expected, orders = execute_target(original[field], equal_target, raw,
                                                  rate, buy_lot=BUY_LOT,
                                                  track_cost_basis=False)
            else:
                expected, orders = _mark(original[field], raw, keys), []
            _require(row.get(field) == expected and row.get(order_field) == orders,
                     f'{day}: {field} ledger or orders mismatch')
            original[field] = expected

        for name, rate in (('normal', NORMAL_FEE), ('stress', STRESS_FEE)):
            if index == 1:
                equal_80 = {key: TARGET_GROSS / len(keys) for key in keys}
                shadow[name], orders = execute_target(shadow[name], equal_80, raw, rate,
                                                     buy_lot=BUY_LOT, track_cost_basis=False)
                first_orders[name] = orders
            else:
                shadow[name], orders = _mark(shadow[name], raw, keys), []
            fee_totals['shadow' if name == 'normal' else 'stress_shadow'] += _fees(orders)
            exposure_target = {key: sum(target.values()) / len(keys) for key in keys}
            matched[name], matched_orders = execute_target(
                matched[name], exposure_target, raw, rate, buy_lot=BUY_LOT,
                track_cost_basis=False)
            matched_order_counts[name] += len(matched_orders)
            day_matched_orders[name] = len(matched_orders)
            fee_totals['matched_exposure_equal' if name == 'normal'
                       else 'stress_matched_exposure_equal'] += _fees(matched_orders)
        equity = {
            'strategy': original['strategy']['equity'],
            'shadow': shadow['normal']['equity'],
            'matched_exposure_equal': matched['normal']['equity'],
            'stress_strategy': original['stress_strategy']['equity'],
            'stress_shadow': shadow['stress']['equity'],
            'stress_matched_exposure_equal': matched['stress']['equity'],
        }
        for name in series:
            series[name].append(equity[name])
        timeline.append({'date': day, 'equity': equity,
                         'executed_target_gross': sum(target.values()),
                         'order_counts': {'strategy': len(row['orders']),
                                          'stress_strategy': len(row['stress_orders']),
                                          'matched_exposure_equal': day_matched_orders['normal'],
                                          'stress_matched_exposure_equal': day_matched_orders['stress'],
                                          'shadow': len(first_orders['normal']) if index == 1 else 0,
                                          'stress_shadow': len(first_orders['stress']) if index == 1 else 0},
                         'fees_paid_today': {name: fee_totals[name] - fees_before[name]
                                             for name in fee_totals},
                         'return_from_opening': {name: equity[name] / initial - 1
                                                 for name in equity},
                         'drawdown_to_date': {name: _drawdown(series[name]) for name in equity}})
        previous = row

    _require(runtime_hash() == start_runtime, 'runtime changed while evaluating')
    _require(read_json(root / 'config.json') == config and sessions(root) == rows,
             'account changed while evaluating')
    observed = len(rows) - 1
    if observed:
        returns = {name: values[-1] / initial - 1 for name, values in series.items()}
        drawdowns = {name: _drawdown(values) for name, values in series.items()}
        gaps = {'return': returns['strategy'] - returns['shadow'],
                'max_drawdown': drawdowns['strategy'] - drawdowns['shadow'],
                'matched_exposure_return': returns['strategy'] - returns['matched_exposure_equal'],
                'matched_exposure_max_drawdown':
                    drawdowns['strategy'] - drawdowns['matched_exposure_equal'],
                'stress_return': returns['stress_strategy'] - returns['stress_shadow'],
                'stress_max_drawdown': drawdowns['stress_strategy'] - drawdowns['stress_shadow'],
                'stress_matched_exposure_return':
                    returns['stress_strategy'] - returns['stress_matched_exposure_equal'],
                'stress_matched_exposure_max_drawdown':
                    drawdowns['stress_strategy'] - drawdowns['stress_matched_exposure_equal']}
        last_raw = previous['raw_bars']
        exit_fee_if_liquidated = {
            name: sum(shadow[name]['shares'][key] * last_raw[key]['close'] * rate for key in keys)
            for name, rate in (('normal', NORMAL_FEE), ('stress', STRESS_FEE))
        }
    else:
        returns = drawdowns = gaps = None
        exit_fee_if_liquidated = None
    return {
        'account': str(root.resolve()),
        'status': 'observed' if observed else 'pending_first_future_session',
        'policy_id': policy['policy_id'],
        'asof': previous['date'],
        'observed_sessions': observed,
        'universe': keys,
        'same_budget_shadow_target': {key: TARGET_GROSS / len(keys) for key in keys},
        'comparisons': {
            'shadow': '80% equal-weight same-pool buy-and-hold; one opening fill then daily mark',
            'matched_exposure_equal':
                'daily equal-weight rebalance using that session executed target gross exposure',
        },
        'fee_rates_per_side': {'normal': NORMAL_FEE, 'stress': STRESS_FEE},
        'buy_lot': BUY_LOT,
        'equity': {name: values[-1] for name, values in series.items()},
        'net_return': returns,
        'max_drawdown': drawdowns,
        'strategy_minus_shadow': gaps,
        'fees_paid': fee_totals,
        'shadow_shares': {name: account['shares'] for name, account in shadow.items()},
        'shadow_cash': {name: account['cash'] for name, account in shadow.items()},
        'shadow_first_day_orders': first_orders,
        'matched_exposure_equal_order_counts': matched_order_counts,
        'matched_exposure_equal_shares': {name: account['shares'] for name, account in matched.items()},
        'shadow_exit_fee_if_liquidated_now': exit_fee_if_liquidated,
        'daily_comparison': timeline,
        'verified': {'config_sha256': digest(config),
                     'latest_session_sha256': digest(previous),
                     'runtime_sha256': start_runtime,
                     'decision_sha256': previous['pending_decision']['decision_sha256'],
                     'session_chain_length': len(rows)},
        'measurement_note': (
            'No future session has been observed; returns and drawdowns are undefined. '
            'This replay cannot independently establish complete trading-calendar coverage or unchanged raw prices.'
            if not observed else
            'Raw-close proxy only; the open shadow has no exit fee in net equity. '
            'This replay cannot independently establish complete trading-calendar coverage or unchanged raw prices.'),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('accounts', nargs='+', type=Path, help='runtime2 account directories')
    args = parser.parse_args()
    print(json.dumps([evaluate_account(root) for root in args.accounts],
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
