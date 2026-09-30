"""Audit a verified 2021 CSI 300 scheduled change against the Qlib monthly mask.

This proves only the named scheduled change and release-date discrepancy. It does
not reconstruct all daily historical constituents or simulate executable P&L.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from qlib_csi300_entry_eligibility import load_verified_source, sha_json
from qlib_csi300_monthly_snapshot_stress import POLICIES, signal_day, simulate_episode

VERSION = 'csi300-official-2021-06-delta-audit-v1'
EFFECTIVE_AFTER_CLOSE = '2021-06-11'
NOTICE_URL = 'https://www.csindex.com.cn/#/about/newsDetail?id=12470'
SSE_NOTICE_URL = 'https://www.sse.com.cn/market/sseindex/diclosure/c/c_20210528_5476672.shtml'
SOURCE_FILES = {
    '2021-06-01': {
        'name': '2021-06-01-index-adjustments.xlsx',
        'url': 'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210601/1622509386248149.xlsx',
        'sha256': 'ea291da157e677490e8d7287f1fa33aa17f69665e76fe6b71b295b038b360e54',
    },
    '2021-06-10': {
        'name': '2021-06-10-index-adjustments.xlsx',
        'url': 'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210610/1623319091660435.xlsx',
        'sha256': '07e8c58d5f02cbaaf5e5b4a98f7c541208218d7a3de9b9627fbff37f31770a07',
    },
}
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'p': 'http://schemas.openxmlformats.org/package/2006/relationships'}
BASE = Path(__file__).resolve().parents[1]


def _cell_text(cell: ET.Element, strings: list[str]) -> str:
    kind = cell.get('t')
    if kind == 'inlineStr':
        return ''.join((node.text or '') for node in cell.findall('.//m:t', NS)).strip()
    node = cell.find('m:v', NS)
    if node is None or node.text is None:
        return ''
    return strings[int(node.text)] if kind == 's' else node.text.strip()


def _sheet_rows(archive: ZipFile, name: str) -> list[dict[str, str]]:
    wb = ET.fromstring(archive.read('xl/workbook.xml'))
    rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
    links = {item.get('Id'): item.get('Target') for item in rels.findall('p:Relationship', NS)}
    sheet_id = next((item.get(f'{{{NS["r"]}}}id') for item in wb.findall('.//m:sheet', NS)
                     if item.get('name') == name), None)
    if sheet_id is None or sheet_id not in links:
        raise ValueError(f'{name}: missing worksheet')
    target = links[sheet_id].lstrip('/')
    location = target if target.startswith('xl/') else 'xl/' + target
    shared_root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
    strings = [''.join((t.text or '') for t in si.findall('.//m:t', NS))
               for si in shared_root.findall('m:si', NS)]
    root = ET.fromstring(archive.read(location))
    rows: list[dict[str, str]] = []
    for row in root.findall('.//m:sheetData/m:row', NS):
        cells: dict[str, str] = {}
        for cell in row.findall('m:c', NS):
            ref = cell.get('r', '')
            match = re.match(r'[A-Z]+', ref)
            if match:
                cells[match.group()] = _cell_text(cell, strings)
        if cells:
            rows.append(cells)
    return rows


def _code(value: str) -> str:
    code = value.strip()
    if not code.isdigit() or len(code) > 6:
        raise ValueError(f'invalid six-digit security or index code: {value!r}')
    return code.zfill(6)


def _symbol(code: str) -> str:
    if code.startswith('6'):
        return 'SH' + code
    if code.startswith(('0', '3')):
        return 'SZ' + code
    raise ValueError(f'unsupported CSI 300 A-share code {code}')


def extract_csi300(path: Path, expected_sha256: str) -> dict[str, list[dict]]:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f'{path}: source SHA-256 mismatch')
    result: dict[str, list[dict]] = {}
    with ZipFile(path) as archive:
        for sheet in ('调入', '调出', '备选名单'):
            rows = _sheet_rows(archive, sheet)
            if not rows or [rows[0].get(col) for col in 'ABCD'] != [
                    '指数代码', '指数简称', '排序' if sheet == '备选名单' else '证券代码',
                    '证券代码' if sheet == '备选名单' else '证券简称']:
                raise ValueError(f'{path}: unexpected {sheet} header')
            selected: list[dict] = []
            for row in rows[1:]:
                if not row.get('A') or not row['A'].strip().isdigit() or _code(row['A']) != '000300':
                    continue
                code = _code(row['D' if sheet == '备选名单' else 'C'])
                item = {'code': code, 'symbol': _symbol(code),
                        'name': row.get('E' if sheet == '备选名单' else 'D', '').strip()}
                if not item['name']:
                    raise ValueError(f'{path}: CSI300 {sheet} name missing for {code}')
                if sheet == '备选名单':
                    item['rank'] = int(row['C'])
                selected.append(item)
            if len(selected) != len({item['symbol'] for item in selected}):
                raise ValueError(f'{path}: duplicate CSI300 {sheet} code')
            result[sheet] = selected
    if [len(result[name]) for name in ('调入', '调出', '备选名单')] != [25, 25, 15]:
        raise ValueError(f'{path}: unexpected CSI300 adjustment count')
    if {item['symbol'] for item in result['调入']} & {item['symbol'] for item in result['调出']}:
        raise ValueError(f'{path}: same security both enters and exits')
    return result


def audit_membership(calendar, masks, adjustment: dict) -> dict:
    adds = {item['symbol'] for item in adjustment['调入']}
    removes = {item['symbol'] for item in adjustment['调出']}
    effective = date.fromisoformat(EFFECTIVE_AFTER_CLOSE)
    dates = [day for day in calendar if effective <= day <= date(2021, 7, 1)]
    if not dates or dates[0] != effective:
        raise ValueError('Qlib calendar omits official effective-after-close date')
    by_date = {}
    for day in dates:
        index = calendar.index(day)
        members = {symbol for symbol, mask in masks.items() if mask[index]}
        by_date[day.isoformat()] = {'members': members,
                                    'adds_present': len(adds & members),
                                    'removes_present': len(removes & members)}
        if len(members) != 300:
            raise ValueError(f'{day}: Qlib snapshot is not 300 names')
    switches = [day for day in dates if by_date[day.isoformat()]['adds_present'] == 25
                and by_date[day.isoformat()]['removes_present'] == 0]
    if not switches:
        raise ValueError('official scheduled replacements never appear in Qlib window')
    switch = switches[0]
    prior = by_date[EFFECTIVE_AFTER_CLOSE]['members']
    after = by_date[switch.isoformat()]['members']
    if after - prior != adds or prior - after != removes:
        raise ValueError('Qlib monthly switch does not exactly equal official 25-name delta')
    lag = [day for day in dates if effective < day < switch]
    if not lag:
        raise ValueError('Qlib switch has no lag against official effective date')
    if any(by_date[day.isoformat()]['adds_present'] != 0 or
           by_date[day.isoformat()]['removes_present'] != 25 or
           by_date[day.isoformat()]['members'] != prior for day in lag):
        raise ValueError('Qlib lag interval contains a partial or unrelated adjustment')
    return {'official_effective_after_close': EFFECTIVE_AFTER_CLOSE,
            'official_first_effective_trading_date': lag[0].isoformat(),
            'qlib_switch_date': switch.isoformat(),
            'lagged_trading_dates': [day.isoformat() for day in lag],
            'lagged_trading_session_count': len(lag),
            'all_25_entering_names_absent_during_lag': True,
            'all_25_exiting_names_still_present_during_lag': True,
            'qlib_delta_matches_official_25_names_at_switch': True,
            'qlib_snapshot_members_on_effective_after_close': sorted(prior)}


def affected_stress_cases(stress_report: dict, membership: dict, adjustment: dict) -> list[dict]:
    lag = set(membership['lagged_trading_dates'])
    adds = {item['symbol'] for item in adjustment['调入']}
    removes = {item['symbol'] for item in adjustment['调出']}
    found = []
    for segment, horizons in stress_report['cases'].items():
        for horizon, seeds in horizons.items():
            for seed, cases in seeds.items():
                for case in cases:
                    day = case['decision_date']
                    if day not in lag:
                        continue
                    original = set(case['signal_at_t']['monthly_snapshot_members'])
                    if original != set(membership['qlib_snapshot_members_on_effective_after_close']):
                        raise ValueError(f'{case["case_id"]}: stress case membership differs from verified lag snapshot')
                    old_ranked = sorted(original, key=lambda symbol: (
                        hashlib.sha256(f'{seed}|{day}|{symbol}'.encode('ascii')).digest(), symbol))[:20]
                    if set(old_ranked) != set(case['signal_at_t']['episode_fixed_pool']):
                        raise ValueError(f'{case["case_id"]}: stress case original selection differs from hash rule')
                    corrected = (original - removes) | adds
                    if len(corrected) != 300:
                        raise ValueError(f'{case["case_id"]}: official delta does not preserve 300')
                    ranked = sorted(corrected, key=lambda symbol: (
                        hashlib.sha256(f'{seed}|{day}|{symbol}'.encode('ascii')).digest(), symbol))[:20]
                    old = set(case['signal_at_t']['episode_fixed_pool'])
                    found.append({'case_id': case['case_id'], 'segment': segment,
                                  'horizon_sessions': int(horizon), 'seed': int(seed),
                                  'decision_date': day, 'old_selected': sorted(old),
                                  'official_delta_selected': ranked,
                                  'name_replacements': len(old - set(ranked)),
                                  'old_selected_exiting_names': sorted(old & removes),
                                  'new_selected_entering_names': sorted(set(ranked) & adds)})
    return found



def selected_pool_sensitivity(calendar, masks, features, stress_report: dict,
                              affected: list[dict]) -> list[dict]:
    """Replay only affected fixed-pool episodes; this is adjusted-close sensitivity."""
    rows = {row['case_id']: row for horizons in stress_report['cases'].values()
            for seeds in horizons.values() for cases in seeds.values() for row in cases}
    output = []
    for item in affected:
        case = rows[item['case_id']]
        start = case['start_index']
        horizon = item['horizon_sessions']
        if calendar[start].isoformat() != item['decision_date']:
            raise ValueError(f"{item['case_id']}: decision date and calendar differ")

        def replay(pool: list[str]) -> tuple[dict, dict]:
            cache = {start: signal_day(calendar, masks, features, start,
                                       seed=item['seed'], fixed_symbols=pool)}

            def target_at(index: int, policy: str) -> dict[str, float]:
                if index not in cache:
                    cache[index] = signal_day(calendar, masks, features, index,
                                              seed=item['seed'], fixed_symbols=pool)
                return cache[index]['targets'][policy]

            policies = {policy: simulate_episode(
                calendar, masks, features, target_at, start=start,
                horizon=horizon, policy=policy, case_id=item['case_id'])
                for policy in POLICIES}
            return cache[start], policies

        original_signal, original_policies = replay(case['signal_at_t']['episode_fixed_pool'])
        if original_policies != case['policies'] or any(
                sha_json(target) != case['signal_at_t']['target_weights_sha256'][policy]
                for policy, target in original_signal['targets'].items()):
            raise ValueError(f"{item['case_id']}: original episode did not replay exactly")
        corrected_signal, corrected_policies = replay(item['official_delta_selected'])
        output.append({
            'case_id': item['case_id'], 'decision_date': item['decision_date'],
            'original_signal': {
                'eligible_count': len(original_signal['eligible_members']),
                'active_count': len(original_signal['active_sma_members']),
                'sma_target_gross': original_signal['sma_target_gross']},
            'one_delta_corrected_signal': {
                'eligible_count': len(corrected_signal['eligible_members']),
                'active_count': len(corrected_signal['active_sma_members']),
                'sma_target_gross': corrected_signal['sma_target_gross']},
            'policy_comparison': {policy: {
                'original_status': original_policies[policy]['status'],
                'original_proxy_return': original_policies[policy]['proxy_return'],
                'one_delta_corrected_status': corrected_policies[policy]['status'],
                'one_delta_corrected_proxy_return': corrected_policies[policy]['proxy_return'],
                'one_delta_corrected_first_failure': corrected_policies[policy]['first_failure'],
            } for policy in POLICIES},
        })
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path('studies/random-entry-baseline-v1/official-sources'))
    parser.add_argument('--qlib-root', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--qlib-manifest', type=Path, default=Path('data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--stress-report', type=Path, default=Path('studies/random-entry-baseline-v1/qlib-csi300-monthly-snapshot-stress-v4.json'))
    parser.add_argument('--out', type=Path, default=Path('studies/random-entry-baseline-v1/csi300-official-2021-06-delta-audit-v1.json'))
    args = parser.parse_args()
    files = {day: args.source_dir / spec['name'] for day, spec in SOURCE_FILES.items()}
    versions = {day: extract_csi300(files[day], spec['sha256'])
                for day, spec in SOURCE_FILES.items()}
    if versions['2021-06-01'] != versions['2021-06-10']:
        raise ValueError('CSI300 rows differ between published attachments; manual review required')
    calendar, masks, features, qlib_lineage = load_verified_source(args.qlib_root, args.qlib_manifest)
    membership = audit_membership(calendar, masks, versions['2021-06-10'])
    stress_bytes = args.stress_report.read_bytes()
    stress = json.loads(stress_bytes)
    if stress.get('study_version') != 'qlib-csi300-monthly-snapshot-stress-v4':
        raise ValueError('unexpected stress study version')
    if stress.get('source_lineage') != qlib_lineage:
        raise ValueError('stress study uses a different Qlib source lineage')
    affected = affected_stress_cases(stress, membership, versions['2021-06-10'])
    sensitivity = selected_pool_sensitivity(calendar, masks, features, stress, affected)
    total_cases = sum(len(cases) for horizons in stress['cases'].values()
                      for seeds in horizons.values() for cases in seeds.values())
    report = {'study_version': VERSION, 'status': 'verified_single_scheduled_delta_only',
              'full_daily_pit_universe_confirmed': False,
              'actual_execution_confirmed': False,
              'notice_url': NOTICE_URL, 'corroborating_sse_notice_url': SSE_NOTICE_URL,
              'attachment_sources': {day: {'url': spec['url'],
                                            'relative_path': str(files[day].resolve().relative_to(BASE)),
                                            'sha256': spec['sha256']}
                                     for day, spec in SOURCE_FILES.items()},
              'two_attachment_versions_identical_for_csi300': True,
              'official_change': versions['2021-06-10'],
              'monthly_release_mismatch': membership,
              'stress_source': {'path': str(args.stress_report.resolve().relative_to(BASE)),
                                'sha256': hashlib.sha256(stress_bytes).hexdigest(),
                                'total_sampled_cases': total_cases},
              'affected_sampled_cases': affected,
              'affected_sampled_case_count': len(affected),
              'one_delta_selected_pool_proxy_sensitivity': sensitivity,
              'qlib_source_lineage': qlib_lineage,
              'limitations': [
                  'Only one official scheduled adjustment is checked. No verified full initial 300-name list or all temporary/revised changes.',
                  'Official attachment dates are inferred from official paths; no independent publication timestamps were preserved.',
                  'The 2026 Qlib price and monthly-membership release is not historical point-in-time vintage.',
                  'Only selected fixed pools for two affected cases are rerun. Adjusted-close proxy outcomes are not fully corrected PIT or executable P&L.'
              ],
              'generated_at': datetime.now(timezone.utc).isoformat(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'report': str(args.out.resolve()),
                      'lagged_sessions': membership['lagged_trading_session_count'],
                      'affected_sampled_cases': len(affected),
                      'status': report['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
