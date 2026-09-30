"""Choose entry-date reserves without looking at future stock coverage.

Keep the original 36 hash-ranked slots. At each decision close, a slot whose
stock has no positive Qlib close and volume receives the first eligible unused
reserve from the 2021-labelled 300. The choice does not inspect t+1 or later.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from qlib_2021_anchor_pool_audit import ANCHOR, NONCE, STOCK_COUNT
from qlib_csi300_entry_eligibility import load_verified_source

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-asof-reserve-coverage-v1'


def select_asof_slots(ranked: list[str], is_eligible: Callable[[str], bool],
                      count: int = STOCK_COUNT) -> list[str]:
    if len(ranked) < count or len(set(ranked)) != len(ranked):
        raise ValueError('invalid fixed candidate ranking')
    selected = list(ranked[:count])
    reserve = iter(ranked[count:])
    used = set(selected)
    for slot, original in enumerate(selected):
        if is_eligible(original):
            continue
        for candidate in reserve:
            if candidate not in used and is_eligible(candidate):
                selected[slot] = candidate
                used.add(candidate)
                break
        else:
            raise ValueError(f'no eligible reserve for slot {slot}')
    if len(set(selected)) != count or any(not is_eligible(symbol) for symbol in selected):
        raise ValueError('selected slots are not distinct and eligible at decision close')
    return selected


def positive_at(features: dict, symbol: str, index: int) -> bool:
    return all(isinstance(value, (int, float)) and math.isfinite(value)
               and value > 0 for value in (
                   features[symbol]['close'].at(index),
                   features[symbol]['volume'].at(index)))


def evaluate(anchor: dict, window: dict, calendar, masks, features) -> dict:
    anchor_index = calendar.index(date.fromisoformat(ANCHOR))
    members = [symbol for symbol, mask in masks.items() if mask[anchor_index]]
    if len(members) != 300:
        raise ValueError('2021 labelled parent universe size differs')
    ranked = sorted(members, key=lambda symbol: (
        hashlib.sha256(f'{NONCE}:{symbol}'.encode()).digest(), symbol))
    if (ranked[:STOCK_COUNT] != anchor['selected_symbols_in_hash_order']
            or anchor['selection_nonce'] != NONCE):
        raise ValueError('saved 2021 anchor ranking differs')
    dates = [day.isoformat() for day in calendar]
    index_by_day = {day: index for index, day in enumerate(dates)}
    decision_dates = sorted({case['decision_date'] for case in window['cases']})
    snapshots = {}
    rank_by_symbol = {symbol: rank for rank, symbol in enumerate(ranked, 1)}
    for day in decision_dates:
        index = index_by_day[day]
        selected = select_asof_slots(
            ranked, lambda symbol: positive_at(features, symbol, index))
        replacements = [{
            'slot': slot + 1, 'original': ranked[slot], 'reserve': symbol,
            'reserve_rank': rank_by_symbol[symbol],
        } for slot, symbol in enumerate(selected) if symbol != ranked[slot]]
        snapshots[day] = {'selected_symbols_by_frozen_slot': selected,
                          'replacements': replacements}
    cases = []
    changed = Counter()
    next_day_status = Counter()
    replacement_use = Counter()
    original_skips = Counter()
    groups = defaultdict(Counter)
    known_terminal_final_trade = {'SZ000671': '2023-06-09',
                                  'SH601989': '2025-08-12'}
    original_post_terminal_entry = dynamic_post_terminal_entry = 0
    for source in window['cases']:
        day = source['decision_date']
        slot_index = int(source['pool'][5:]) - 1
        if slot_index not in range(12):
            raise ValueError('unexpected pool identity')
        selection = snapshots[day]['selected_symbols_by_frozen_slot']
        chosen = selection[3 * slot_index:3 * slot_index + 3]
        if len(chosen) != 3:
            raise ValueError('as-of pool slot count differs')
        index = index_by_day[day]
        if dates[index + 1] != source['first_fill_date']:
            raise ValueError('t+1 calendar differs from frozen random case')
        next_day_all_positive = all(
            positive_at(features, symbol, index + 1) for symbol in chosen)
        original_invalid = any(
            source['first_fill_date'] > known_terminal_final_trade[symbol]
            for symbol in source['members'] if symbol in known_terminal_final_trade)
        dynamic_invalid = any(
            source['first_fill_date'] > known_terminal_final_trade[symbol]
            for symbol in chosen if symbol in known_terminal_final_trade)
        original_post_terminal_entry += original_invalid
        dynamic_post_terminal_entry += dynamic_invalid
        changes = [
            {'slot_within_pool': j + 1, 'original': old, 'reserve': new,
             'reserve_rank': rank_by_symbol[new]}
            for j, (old, new) in enumerate(zip(source['members'], chosen))
            if old != new]
        if any(change['reserve_rank'] <= STOCK_COUNT for change in changes):
            raise ValueError('replacement is not outside original 36')
        for change in changes:
            replacement_use[change['reserve']] += 1
            original_skips[change['original']] += 1
        changed[bool(changes)] += 1
        next_day_status[next_day_all_positive] += 1
        group = f"{source['segment']}:{source['horizon_sessions']}:{source['seed']}"
        groups[group]['cases'] += 1
        groups[group]['changed_pool'] += bool(changes)
        groups[group]['t_plus_1_all_positive'] += next_day_all_positive
        groups[group]['original_entry_after_terminal_last_trade'] += original_invalid
        groups[group]['dynamic_entry_after_terminal_last_trade'] += dynamic_invalid
        cases.append({
            'case_id': source['case_id'], 'segment': source['segment'],
            'horizon_sessions': source['horizon_sessions'], 'seed': source['seed'],
            'pool': source['pool'], 'decision_date': day,
            'first_fill_date': source['first_fill_date'],
            'exit_date': source['exit_date'],
            'original_members': source['members'], 'asof_members': chosen,
            'changes': changes,
            't_plus_1_qlib_positive_close_volume_all_three': next_day_all_positive,
            'original_entry_after_known_terminal_last_trade': original_invalid,
            'asof_entry_after_known_terminal_last_trade': dynamic_invalid,
        })
    if (len(cases) != 6144 or len({case['case_id'] for case in cases}) != 6144
            or changed[True] != 324 or original_post_terminal_entry != 303
            or dynamic_post_terminal_entry != 1
            or sum(replacement_use.values()) != 329):
        raise ValueError('as-of reserve sample differs from frozen 2021 pool audit: '
                         f'cases={len(cases)} unique={len({case["case_id"] for case in cases})} '
                         f'changed={changed[True]} original_terminal={original_post_terminal_entry} '
                         f'dynamic_terminal={dynamic_post_terminal_entry} replacements={sum(replacement_use.values())}')
    return {
        'parent_2021_labelled_member_count': len(ranked),
        'unique_decision_dates': len(decision_dates),
        'case_count': len(cases),
        'changed_pool_case_count': changed[True],
        'unchanged_pool_case_count': changed[False],
        't_plus_1_qlib_positive_close_volume_all_three_count': next_day_status[True],
        't_plus_1_qlib_missing_at_least_one_count': next_day_status[False],
        'original_entry_after_known_terminal_last_trade_count': original_post_terminal_entry,
        'asof_entry_after_known_terminal_last_trade_count': dynamic_post_terminal_entry,
        'replacement_stock_use_count': dict(replacement_use),
        'original_stock_skip_count': dict(original_skips),
        'replacement_symbols_needing_raw_archive': sorted(replacement_use),
        'groups': {group: dict(summary) for group, summary in groups.items()},
        'decision_date_selections': snapshots,
        'cases': cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--qlib-root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-asof-reserve-coverage-v1.json'))
    args = parser.parse_args()
    names = {
        'anchor': 'qlib-2021-anchor-pool-coverage-audit-v1.json',
        'window': 'qlib-2021-anchor-random-window-eligibility-v1.json',
        'terminal': 'qlib-2021-anchor-terminal-exit-audit-v1.json',
    }
    payloads = {key: (args.source_dir / name).read_bytes() for key, name in names.items()}
    reports = {key: json.loads(data) for key, data in payloads.items()}
    if (reports['window']['case_count'] != 6144
            or reports['window']['source_lineage']['anchor_report_sha256']
            != hashlib.sha256(payloads['anchor']).hexdigest()
            or reports['terminal']['source_lineage']['window_report_sha256']
            != hashlib.sha256(payloads['window']).hexdigest()):
        raise ValueError('frozen source lineage differs')
    calendar, masks, features, lineage = load_verified_source(
        args.qlib_root, args.qlib_manifest)
    if lineage != reports['anchor']['source_lineage']:
        raise ValueError('Qlib release differs from frozen anchor')
    result = evaluate(reports['anchor'], reports['window'], calendar, masks, features)
    report = {
        'study_version': VERSION,
        'status': 'asof_qlib_coverage_only_not_raw_execution_or_pit_universe',
        'source_lineage': {key + '_report_sha256': hashlib.sha256(data).hexdigest()
                           for key, data in payloads.items()},
        'selection_rule': ('Preserve the 36 original hash-ranked slot positions. '
                           'On decision close t, replace each missing/nonpositive '
                           'Qlib close-or-volume slot in slot order with the first '
                           'unused 2021-ranked reserve having positive close and '
                           'volume at t. No t+1 or later feature is read for selection.'),
        'selection_uses_future_feature_values': False,
        'historical_official_csi300_membership_confirmed': False,
        'raw_tradability_or_action_verified_for_reserves': False,
        **result,
        'limitations': [
            'The parent 2021 constituent label is from a 2026 Qlib release and is not confirmed official point-in-time membership.',
            'Qlib close/volume are retrospective as-of-date data proxies, not archived same-day publication snapshots or raw trading status.',
            'The t+1 coverage count is a separate after-selection diagnostic; a missing t+1 bar still needs an order-failure rule.',
            'Each episode freezes its three symbols at t; a later delisting still needs an entitlement and execution ledger.',
            'This report changes only selection identities and calculates no strategy returns.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'changed_pool_cases': result['changed_pool_case_count'],
                      'raw_needed': result['replacement_symbols_needing_raw_archive']},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
