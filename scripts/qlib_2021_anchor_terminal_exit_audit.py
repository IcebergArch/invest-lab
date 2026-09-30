"""Audit impossible entries and holdings across two terminal A-share exits.

Official event dates are checked against archived per-day provider trading
statuses. This does not assign cash value to a delisted or exchanged claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from qlib_2021_anchor_raw_status_audit import audit as status_audit
from qlib_2021_anchor_reconciled_bounds import COMPLETE, merge_cases
from qlib_csi300_entry_eligibility import load_verified_source

BASE = Path(__file__).resolve().parents[1]
VERSION = 'qlib-2021-anchor-terminal-exit-audit-v1'
EVENTS = {
    'SZ000671': {
        'instrument_id': 'stock:000671.SZ',
        'last_trading_day': '2023-06-09',
        'first_permanent_halt_day': '2023-06-12',
        'exchange_delisting_day': '2023-08-16',
        'provider_out_date': '2023-08-16',
        'terminal_treatment': 'A_share_delisted_then_transferred_to_delisted_share_board',
        'post_exit_security': '400199',
        'official_or_issuer_sources': [
            'https://www.szse.cn/disclosure/notice/t20230804_602471.html',
            'https://www.neeq.com.cn/disclosure/2023/2023-08-21/1692604408_215408.pdf',
        ],
    },
    'SH601989': {
        'instrument_id': 'stock:601989.SH',
        'last_trading_day': '2025-08-12',
        'first_permanent_halt_day': '2025-08-13',
        'exchange_delisting_day': '2025-09-05',
        'provider_out_date': '2025-09-05',
        'terminal_treatment': 'share_exchange_absorbed_by_listed_successor',
        'post_exit_security': 'stock:600150.SH',
        'published_exchange_ratio': 0.1339,
        'official_or_issuer_sources': [
            'https://big5.sse.com.cn/site/cht/www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2025-08-09/601989_20250809_7OO5.pdf',
            'https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20250829_10790128.shtml',
            'https://big5.sse.com.cn/site/cht/www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2025-07-19/601989_20250719_EEVQ.pdf',
        ],
    },
}


def classify_window(first_fill: str, exit_day: str, last_trade: str) -> str:
    if first_fill > last_trade:
        return 'entry_after_last_trade_unexecutable'
    if exit_day > last_trade:
        return 'window_crosses_terminal_halt_potential_claim'
    return 'entire_window_before_last_trade'


def evaluate(window: dict, merged: list[dict]) -> dict:
    outcomes = {case['case_id']: case for case in merged}
    if len(outcomes) != 6144 or window['case_count'] != 6144:
        raise ValueError('merged outcomes do not cover frozen windows')
    cases = []
    counts = Counter()
    by_symbol = defaultdict(Counter)
    by_segment = defaultdict(Counter)
    by_group = defaultdict(Counter)
    for source in window['cases']:
        event_symbols = [s for s in source['members'] if s in EVENTS]
        if len(event_symbols) > 1:
            raise ValueError('one pool unexpectedly holds two terminal names')
        if not event_symbols:
            continue
        symbol = event_symbols[0]
        event = EVENTS[symbol]
        label = classify_window(source['first_fill_date'], source['exit_date'],
                                event['last_trading_day'])
        outcome = outcomes[source['case_id']]
        if label != 'entire_window_before_last_trade' and outcome['status'] in COMPLETE:
            raise ValueError('exit crossing unexpectedly has a liquidated A-share outcome')
        if label == 'entry_after_last_trade_unexecutable' and source['entry_all_three_trading']:
            raise ValueError('post-final-trade entry labelled trading')
        counts[label] += 1
        by_symbol[symbol][label] += 1
        by_segment[source['segment']][label] += 1
        by_group[f"{source['segment']}:{source['horizon_sessions']}:{source['seed']}"][label] += 1
        cases.append({
            'case_id': source['case_id'], 'pool': source['pool'],
            'segment': source['segment'],
            'horizon_sessions': source['horizon_sessions'], 'seed': source['seed'],
            'terminal_symbol': symbol, 'exit_path_class': label,
            'first_fill_date': source['first_fill_date'],
            'scheduled_exit_date': source['exit_date'],
            'has_supplier_outdate_gap': source['has_supplier_outdate_gap'],
            'reconciled_outcome_status': outcome['status'],
        })
    if (len(cases) != 1024
            or counts != Counter({
                'entire_window_before_last_trade': 557,
                'entry_after_last_trade_unexecutable': 303,
                'window_crosses_terminal_halt_potential_claim': 164,
            })
            or sum(case['has_supplier_outdate_gap'] for case in cases) != 434):
        raise ValueError('terminal path counts differ from frozen windows')
    return {
        'event_stock_window_count': len(cases),
        'classification_totals': dict(counts),
        'classification_by_symbol': {k: dict(v) for k, v in by_symbol.items()},
        'classification_by_segment': {k: dict(v) for k, v in by_segment.items()},
        'classification_by_group': {k: dict(v) for k, v in by_group.items()},
        'outdate_gap_window_count': sum(case['has_supplier_outdate_gap'] for case in cases),
        'outdate_gap_by_class': dict(Counter(
            case['exit_path_class'] for case in cases
            if case['has_supplier_outdate_gap'])),
        'cases': cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1'))
    parser.add_argument('--snapshot', type=Path, default=Path(
        'reports/quant/snapshots/2026-09-29-baostock-historical-shsz-590b41a3bd0a.json'))
    parser.add_argument('--db', type=Path, default=Path('data/quant/historical-baostock-raw.sqlite3'))
    parser.add_argument('--qlib-root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path(
        'data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path, default=Path(
        'studies/random-entry-baseline-v1/qlib-2021-anchor-terminal-exit-audit-v1.json'))
    args = parser.parse_args()
    names = {
        'window': 'qlib-2021-anchor-random-window-eligibility-v1.json',
        'anchor': 'qlib-2021-anchor-pool-coverage-audit-v1.json',
        'raw': 'qlib-2021-anchor-raw-status-audit-v1.json',
        'conditional': 'qlib-2021-anchor-conditional-raw-returns-v1.json',
        'halt': 'qlib-2021-anchor-pure-halt-replay-v1.json',
        'reconciled': 'qlib-2021-anchor-reconciled-median-bounds-v1.json',
    }
    payloads = {key: (args.source_dir / name).read_bytes() for key, name in names.items()}
    reports = {key: json.loads(data) for key, data in payloads.items()}
    if (reports['window']['case_count'] != 6144
            or reports['conditional']['case_count'] != 6144
            or reports['reconciled']['case_count'] != 6144
            or reports['reconciled']['source_lineage']['halt_report_sha256']
            != hashlib.sha256(payloads['halt']).hexdigest()
            or reports['reconciled']['source_lineage']['conditional_report_sha256']
            != hashlib.sha256(payloads['conditional']).hexdigest()):
        raise ValueError('frozen exit-audit lineage differs')
    fresh_raw = status_audit(
        args.source_dir / names['anchor'], args.snapshot, args.db,
        args.qlib_root, args.qlib_manifest)
    if (fresh_raw['raw_archive'] != reports['raw']['raw_archive']
            or fresh_raw['per_symbol'] != reports['raw']['per_symbol']):
        raise ValueError('raw archive differs from stock-day status audit')
    calendar, _, _, lineage = load_verified_source(args.qlib_root, args.qlib_manifest)
    if lineage != reports['anchor']['source_lineage']:
        raise ValueError('calendar source differs from frozen anchor')
    calendar_days = [day.isoformat() for day in calendar]
    raw_by_symbol = {s['symbol']: s for s in reports['raw']['per_symbol']}
    connection = sqlite3.connect(args.db.resolve().as_uri() + '?mode=rw', uri=True)
    verified_events = {}
    try:
        for symbol, event in EVENTS.items():
            if raw_by_symbol[symbol]['supplier_out_date_at_snapshot'] != event['provider_out_date']:
                raise ValueError('supplier outDate differs from terminal audit')
            rows = connection.execute('''
                SELECT trade_date,tradestatus FROM baostock_daily_status
                WHERE instrument_id=? AND trade_date BETWEEN ? AND ?
                ORDER BY trade_date
            ''', (event['instrument_id'], '2021-01-04', '2026-09-28')).fetchall()
            trading_days = [day for day, trading in rows if trading == 1]
            if (not trading_days or trading_days[-1] != event['last_trading_day']
                    or any(trading != 0 for day, trading in rows
                           if day > event['last_trading_day'])
                    or rows[-1][0] > event['provider_out_date']):
                raise ValueError(f'{symbol}: provider terminal trading path differs')
            next_session = calendar_days[calendar_days.index(event['last_trading_day']) + 1]
            first_halt = next(day for day, trading in rows if day > event['last_trading_day'])
            if (first_halt != next_session
                    or first_halt != event['first_permanent_halt_day']):
                raise ValueError(f'{symbol}: terminal halt start differs')
            verified_events[symbol] = {
                **event, 'archived_last_tradable_day': trading_days[-1],
                'archived_first_halted_day': first_halt,
                'archived_terminal_halted_days': sum(
                    day > event['last_trading_day'] and trading == 0
                    for day, trading in rows),
                'archived_last_status_day': rows[-1][0],
            }
    finally:
        connection.close()
    eligibility = {case['case_id']: case for case in reports['window']['cases']}
    merged = merge_cases(reports['conditional']['cases'], reports['halt']['cases'],
                         eligibility)
    result = evaluate(reports['window'], merged)
    report = {
        'study_version': VERSION,
        'status': 'frozen_pool_has_unexecutable_post_exit_entries',
        'source_lineage': {key + '_report_sha256': hashlib.sha256(data).hexdigest()
                           for key, data in payloads.items()},
        'official_source_urls': 'Exchange and issuer URLs record the terminal events; this script validates their dates against the local raw status archive.',
        'events': verified_events,
        'classification_definition': {
            'entry_after_last_trade_unexecutable': 'First scheduled fill day is later than the A-share last trading day; no original-symbol buy can execute.',
            'window_crosses_terminal_halt_potential_claim': 'First scheduled fill day is on/before the A-share last trade but exit is later; whether a policy actually owns a claim requires its order ledger.',
            'entire_window_before_last_trade': 'This terminal event does not interrupt the scheduled A-share window; other failures may still exist.',
        },
        **result,
        'limitations': [
            'This is retrospective path classification; only actual same-day trading status, not a future last-trade date, is usable in a live entry filter.',
            'Post-final-trade entry cases are invalid fixed-three-stock trials, not zero-return trades or liquidation losses.',
            'A crossing window may own a transferable or exchangeable claim; no A-share close after cessation is invented.',
            'The 2021 Qlib member label is not verified official historical point-in-time membership.',
        ],
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'out': str(args.out.resolve()),
                      'counts': result['classification_totals']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
