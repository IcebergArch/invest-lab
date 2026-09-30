"""Strategy-agnostic daily paper execution from frozen target weights.

Inputs are a prior decision, current unadjusted close bars, account state, and
cost model. No factor or strategy code is imported here.
"""
from __future__ import annotations

import math
from typing import Mapping


def execute_target(account: dict, target: Mapping[str, float], raw: Mapping[str, dict],
                   cost_rate: float, *, buy_lot: int = 100,
                   track_cost_basis: bool = True) -> tuple[dict, list[dict]]:
    keys = set(account['shares'])
    if set(target) != keys or set(raw) != keys:
        raise ValueError('decision, account and prices must cover the same universe')
    if (not math.isfinite(cost_rate) or not 0 <= cost_rate < 0.5
            or not isinstance(buy_lot, int) or buy_lot <= 0):
        raise ValueError('invalid execution cost or buy lot')
    if any(not math.isfinite(float(w)) or w < 0 for w in target.values()) or sum(target.values()) > 1 + 1e-12:
        raise ValueError('invalid target weights')
    if any(not math.isfinite(float(raw[key]['close'])) or raw[key]['close'] <= 0
           or raw[key].get('volume_lots', 1) <= 0 for key in keys):
        raise ValueError('invalid or untradable market bar')
    cash = float(account['cash'])
    shares = dict(account['shares'])
    basis = dict(account.get('cost_basis', {key: 0.0 for key in shares}))
    if cash < 0 or any(qty < 0 or not isinstance(qty, int) for qty in shares.values()):
        raise ValueError('invalid account state')
    pre_equity = cash + sum(shares[key] * raw[key]['close'] for key in shares)
    desired = {key: math.floor(target[key] * pre_equity / raw[key]['close'] / buy_lot) * buy_lot
               for key in shares}
    orders = []
    for key in sorted(shares):
        qty = max(0, shares[key] - desired[key])
        if not qty:
            continue
        price = raw[key]['close']
        fee = qty * price * cost_rate
        realized = qty * (price - basis[key]) - fee if track_cost_basis else None
        cash += qty * price - fee
        shares[key] -= qty
        if not shares[key]:
            basis[key] = 0.0
        orders.append({'instrument_id': key, 'side': 'sell', 'shares': qty,
                       'raw_close_price': price, 'gross_amount': qty * price,
                       'fee': fee, 'realized_pnl': realized})
    for key in sorted(shares):
        qty = max(0, desired[key] - shares[key])
        if not qty:
            continue
        price = raw[key]['close']
        affordable = math.floor(cash / (price * (1 + cost_rate)) / buy_lot) * buy_lot
        qty = min(qty, affordable)
        if not qty:
            continue
        fee = qty * price * cost_rate
        old_value = shares[key] * basis[key]
        cash -= qty * price + fee
        shares[key] += qty
        if track_cost_basis:
            basis[key] = (old_value + qty * price + fee) / shares[key]
        orders.append({'instrument_id': key, 'side': 'buy', 'shares': qty,
                       'raw_close_price': price, 'gross_amount': qty * price,
                       'fee': fee, 'realized_pnl': None})
    if cash < -1e-6:
        raise ValueError('negative cash after simulated fills')
    equity = cash + sum(shares[key] * raw[key]['close'] for key in shares)
    result = {'cash': max(0.0, cash), 'shares': shares, 'equity': equity}
    if track_cost_basis:
        result['cost_basis'] = basis
    return result, orders
