"""Retrospective CSI 300 snapshot random-entry eligibility on one Qlib release.

This measures whether names selected from the release's t-labelled, forward-filled
index-weight snapshots have usable history and later price coverage. These
snapshots can lag official constituents. This does not calculate returns or
establish that the member list was known or correct at t.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import struct
import sys
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

from quant_lab.qlib_archive import parse_calendar, parse_instruments
from quant_lab.qlib_local import _load_index, _verified_file

VERSION = 'qlib-csi300-entry-eligibility-v1'
RELEASE_TAG = '2026-09-28'
PROVENANCE_CHECKED_ON = '2026-09-30'
UPSTREAM_INDEX_EXPORT_URL = (
    'https://raw.githubusercontent.com/chenditc/investment_data/'
    'b8c129b4d9b838f050eac8b1135111b3b79fc894/qlib/dump_index_weight.py'
)
UPSTREAM_TUSHARE_COLLECTION_URL = (
    'https://raw.githubusercontent.com/chenditc/investment_data/'
    'b8c129b4d9b838f050eac8b1135111b3b79fc894/tushare/dump_index_weight.py'
)
OFFICIAL_2021_CHANGE_URL = (
    'https://www.sse.com.cn/market/sseindex/diclosure/c/c_20210528_5476672.shtml'
)
SEGMENTS = {
    'selection_history': ('2021-01-01', '2023-12-29'),
    'later_history': ('2024-01-02', RELEASE_TAG),
}
HORIZONS = (126, 252)
SEEDS = (202, 404, 606)
SAMPLES_PER_SEGMENT = 128
PICKS_PER_ENTRY = 20
WARMUP_SESSIONS = 60
FLAG_NAMES = (
    'warmup_missing_close', 'warmup_missing_volume',
    'next_missing_close', 'next_nonpositive_volume',
    'holding_missing_close', 'holding_missing_close_after_entry',
    'holding_nonpositive_volume', 'constituent_exited',
    'ready_at_t', 'next_session_proxy_ok',
    'price_volume_coverage_through_window',
    'continuous_member_and_price_volume_coverage',
)


def sha_json(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False).encode('utf-8')
    return 'sha256:' + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class FloatSeries:
    first_calendar_index: int
    values: Sequence[float]

    def at(self, calendar_index: int) -> float | None:
        offset = calendar_index - self.first_calendar_index
        return self.values[offset] if 0 <= offset < len(self.values) else None


def _positive(series: FloatSeries | None, calendar_index: int) -> bool:
    value = series.at(calendar_index) if series is not None else None
    return (value is not None and not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def _read_float_series(raw: bytes, calendar_size: int, label: str) -> FloatSeries:
    if len(raw) < 8 or len(raw) % 4:
        raise ValueError(f'{label}: invalid Qlib feature length')
    (header,) = struct.unpack_from('<f', raw, 0)
    if not math.isfinite(header) or header < 0 or not header.is_integer():
        raise ValueError(f'{label}: invalid Qlib start index')
    first = int(header)
    values = array('f')
    values.frombytes(raw[4:])
    if sys.byteorder != 'little':
        values.byteswap()
    if first + len(values) > calendar_size:
        raise ValueError(f'{label}: Qlib feature extends beyond calendar')
    return FloatSeries(first, values)


def build_membership_masks(
    calendar: Sequence[date],
    intervals: Mapping[str, Sequence[tuple[date, date]]],
) -> dict[str, bytearray]:
    """One byte per calendar session; adjoining review intervals stay continuous."""
    if not calendar or list(calendar) != sorted(set(calendar)):
        raise ValueError('calendar must be sorted and unique')
    masks = {}
    for symbol, spans in intervals.items():
        mask = bytearray(len(calendar))
        for first, last in spans:
            if first > last:
                raise ValueError(f'{symbol}: reversed constituent interval')
            low = bisect_left(calendar, first)
            high = bisect_right(calendar, last)
            mask[low:high] = b'\x01' * (high - low)
        masks[symbol] = mask
    return masks


def sampled_entry_indices(
    calendar: Sequence[date], first: str, last: str,
    horizon: int, seed: int, sample_count: int,
) -> list[int]:
    if horizon < 2 or sample_count < 1:
        raise ValueError('horizon and sample_count must be positive')
    left, right = date.fromisoformat(first), date.fromisoformat(last)
    eligible = [i for i, day in enumerate(calendar)
                if left <= day <= right and i + horizon < len(calendar)
                and calendar[i + horizon] <= right]
    if len(eligible) < sample_count:
        raise ValueError(f'only {len(eligible)} eligible signal dates in {first}..{last}')
    return sorted(random.Random(seed).sample(eligible, sample_count))


def selected_members(
    masks: Mapping[str, Sequence[int]], calendar: Sequence[date],
    signal_index: int, seed: int, pick_count: int,
) -> tuple[tuple[str, ...], int]:
    """Select from the release's t-labelled row by a predeclared hash ranking."""
    if pick_count < 1 or not 0 <= signal_index < len(calendar):
        raise ValueError('invalid selection index or pick count')
    members = [symbol for symbol, mask in masks.items() if mask[signal_index]]
    if len(members) < pick_count:
        raise ValueError(f'{calendar[signal_index]}: only {len(members)} constituents')
    day = calendar[signal_index].isoformat()
    ranked = sorted(members, key=lambda symbol: (
        hashlib.sha256(f'{seed}|{day}|{symbol}'.encode('ascii')).digest(), symbol))
    return tuple(ranked[:pick_count]), len(members)


def audit_entry(
    calendar: Sequence[date], masks: Mapping[str, Sequence[int]],
    features: Mapping[str, Mapping[str, FloatSeries]], signal_index: int,
    horizon: int, seed: int, pick_count: int = PICKS_PER_ENTRY,
    warmup: int = WARMUP_SESSIONS,
) -> dict:
    if (warmup < 1 or horizon < 2 or signal_index < 0
            or signal_index + horizon >= len(calendar)):
        raise ValueError('invalid audit entry window')
    selected, member_count = selected_members(masks, calendar, signal_index, seed, pick_count)
    next_index = signal_index + 1
    end_index = signal_index + horizon
    warmup_start = signal_index - warmup + 1
    flags_by_symbol = {}
    for symbol in selected:
        values = features.get(symbol, {})
        close = values.get('close')
        volume = values.get('volume')
        warmup_missing_close = (warmup_start < 0 or any(
            not _positive(close, j) for j in range(max(0, warmup_start), signal_index + 1)))
        warmup_missing_volume = (warmup_start < 0 or any(
            not _positive(volume, j) for j in range(max(0, warmup_start), signal_index + 1)))
        next_missing_close = not _positive(close, next_index)
        next_nonpositive_volume = not _positive(volume, next_index)
        holding_missing_close = any(not _positive(close, j)
                                    for j in range(next_index, end_index + 1))
        holding_missing_close_after_entry = any(not _positive(close, j)
                                                for j in range(next_index + 1, end_index + 1))
        holding_nonpositive_volume = any(not _positive(volume, j)
                                         for j in range(next_index, end_index + 1))
        constituent_exited = 0 in masks[symbol][next_index:end_index + 1]
        ready_at_t = not (warmup_missing_close or warmup_missing_volume)
        next_session_proxy_ok = ready_at_t and not (next_missing_close or next_nonpositive_volume)
        price_volume_coverage = (next_session_proxy_ok and not holding_missing_close
                                 and not holding_nonpositive_volume)
        continuous_member_and_data = price_volume_coverage and not constituent_exited
        flags_by_symbol[symbol] = {
            'warmup_missing_close': warmup_missing_close,
            'warmup_missing_volume': warmup_missing_volume,
            'next_missing_close': next_missing_close,
            'next_nonpositive_volume': next_nonpositive_volume,
            'holding_missing_close': holding_missing_close,
            'holding_missing_close_after_entry': holding_missing_close_after_entry,
            'holding_nonpositive_volume': holding_nonpositive_volume,
            'constituent_exited': constituent_exited,
            'ready_at_t': ready_at_t,
            'next_session_proxy_ok': next_session_proxy_ok,
            'price_volume_coverage_through_window': price_volume_coverage,
            'continuous_member_and_price_volume_coverage': continuous_member_and_data,
        }
    return {
        'signal_date': calendar[signal_index].isoformat(),
        'window_end_date': calendar[end_index].isoformat(),
        'constituents_at_t': member_count,
        'selected': selected,
        'flags_by_symbol': flags_by_symbol,
    }


def evaluate(
    calendar: Sequence[date], masks: Mapping[str, Sequence[int]],
    features: Mapping[str, Mapping[str, FloatSeries]],
    *, segments: Mapping[str, tuple[str, str]] = SEGMENTS,
    horizons: Sequence[int] = HORIZONS, seeds: Sequence[int] = SEEDS,
    sample_count: int = SAMPLES_PER_SEGMENT,
    pick_count: int = PICKS_PER_ENTRY, warmup: int = WARMUP_SESSIONS,
) -> dict:
    if not calendar or list(calendar) != sorted(set(calendar)):
        raise ValueError('calendar must be sorted and unique')
    if any(len(mask) != len(calendar) for mask in masks.values()):
        raise ValueError('constituent mask length differs from calendar')
    cases = {}
    for segment, (first, last) in segments.items():
        cases[segment] = {}
        for horizon in horizons:
            cases[segment][str(horizon)] = {}
            for seed in seeds:
                indices = sampled_entry_indices(calendar, first, last, horizon, seed,
                                                sample_count)
                counts: Counter[str] = Counter()
                episode_counts: Counter[str] = Counter()
                member_counts = []
                selections = []
                examples = []
                exemplified_flags: set[str] = set()
                example_flag_names = (
                    'warmup_missing_close', 'next_missing_close',
                    'holding_missing_close_after_entry', 'constituent_exited',
                )
                for index in indices:
                    row = audit_entry(calendar, masks, features, index, horizon, seed,
                                      pick_count, warmup)
                    member_counts.append(row['constituents_at_t'])
                    selections.append((row['signal_date'], row['selected']))
                    flagged_symbols = {
                        flag: [symbol for symbol, values in row['flags_by_symbol'].items()
                               if values[flag]]
                        for flag in example_flag_names
                    }
                    if (len(examples) < 2 or any(
                            flagged_symbols[flag] and flag not in exemplified_flags
                            for flag in example_flag_names)):
                        examples.append({
                            'signal_date': row['signal_date'],
                            'window_end_date': row['window_end_date'],
                            'selected': list(row['selected']),
                            'flagged_symbols': flagged_symbols,
                        })
                        exemplified_flags.update(flag for flag in example_flag_names
                                                 if flagged_symbols[flag])
                    counts['selected'] += len(row['selected'])
                    for flag in FLAG_NAMES:
                        flagged = sum(bool(values[flag])
                                      for values in row['flags_by_symbol'].values())
                        counts[flag] += flagged
                        episode_counts[flag] += int(flagged > 0)
                selected_count = counts['selected']
                ready_count = counts['ready_at_t']
                next_ok_count = counts['next_session_proxy_ok']
                cases[segment][str(horizon)][str(seed)] = {
                    'episodes': len(indices),
                    'sampled_entry_dates': [calendar[i].isoformat() for i in indices],
                    'entry_dates_sha256': sha_json([calendar[i].isoformat() for i in indices]),
                    'selected_symbols_sha256': sha_json(selections),
                    'selection_examples': examples,
                    'constituents_at_t_min': min(member_counts),
                    'constituents_at_t_max': max(member_counts),
                    'selected_name_count': selected_count,
                    'name_counts': {flag: counts[flag] for flag in FLAG_NAMES},
                    'name_fractions_of_selected': {
                        flag: counts[flag] / selected_count for flag in FLAG_NAMES},
                    'conditional_fractions': {
                        'next_session_failure_among_ready_at_t': (
                            (ready_count - next_ok_count) / ready_count if ready_count else None),
                        'price_volume_coverage_among_next_session_ok': (
                            counts['price_volume_coverage_through_window'] / next_ok_count
                            if next_ok_count else None),
                        'continuous_membership_and_data_coverage_among_next_session_ok': (
                            counts['continuous_member_and_price_volume_coverage'] / next_ok_count
                            if next_ok_count else None),
                    },
                    'episodes_with_any_flag': {
                        flag: episode_counts[flag] for flag in FLAG_NAMES},
                }
    return {
        'schema_version': 1,
        'audit_version': VERSION,
        'status': 'retrospective_entry_data_eligibility_audit',
        'release_tag': RELEASE_TAG,
        'point_in_time_confirmed': False,
        'official_daily_membership_confirmed': False,
        'actual_execution_confirmed': False,
        'return_or_pnl_computed': False,
        'segments': segments,
        'horizons_sessions': list(horizons),
        'sampling_seeds': list(seeds),
        'sample_count_per_segment_horizon_seed': sample_count,
        'selection': {
            'source': 'fixed 2026 Qlib csi300.txt Tushare index_weight snapshots, forward-filled to a t-labelled interval; not verified official membership on t',
            'rule': 'SHA-256(seed|signal_date|symbol), ascending; first N; no replacement',
            'names_per_entry': pick_count,
            'ranking_or_replacement_reads_post_t_feature_values': False,
            'constituent_publication_time_verified': False,
        },
        'data_checks': {
            'warmup_sessions_through_t': warmup,
            'next_session': 't+1 adjusted close and source volume must be positive',
            'holding_window': 't+1 through t+horizon inclusive; missing/nonpositive close or volume and gaps in the release membership mask are separate events',
            'coverage_definitions': {
                'price_volume_coverage_through_window': '60-session warmup, t+1 entry proxy and all holding-window close/volume are positive; membership exit is independent',
                'continuous_member_and_price_volume_coverage': 'price_volume_coverage_through_window and no gap in the fixed release membership mask through the window; not a tradability rate or official index membership rate',
            },
            'fractions_denominator': 'selected names; conditional fractions state their own denominators',
        },
        'cases': cases,
        'limitations': [
            'The 2026 release derives CSI 300 intervals from Tushare index_weight snapshot dates and forward-fills to the next snapshot; a t-labelled row is not verified official CSI 300 membership on t.',
            'Official 2021-06-11 close-effective CSI 300 adjustment included SH688111 and removed SH603156, but this release keeps the old pair through 2021-06-29 and changes only on 2021-06-30 (11 intervening trading sessions).',
            'Qlib adjusted prices and source-native volume are data-coverage proxies, not original transaction prices or original shares.',
            'No original-price, ST, suspension, limit-price or order-execution crosscheck is available for this full 2021+ sample.',
            'A constituent exit flag means a gap in this release-derived membership mask; it does not prove actual official removal or that the stock could not be held or sold.',
            'Overlapping sampled windows and seeds are descriptive observations, not independent trials.',
        ],
    }


def load_verified_source(root: Path, manifest: Path) -> tuple[tuple[date, ...], dict[str, bytearray], dict, dict]:
    root = root.resolve()
    index = _load_index(root, manifest, RELEASE_TAG)
    with _verified_file(root, index, 'qlib_bin/calendars/day.txt') as stream:
        calendar = parse_calendar(stream.read(), 'day calendar')
    with _verified_file(root, index, 'qlib_bin/instruments/csi300.txt') as stream:
        constituents = parse_instruments(stream.read(), 'CSI 300 constituents')
    with _verified_file(root, index, 'qlib_bin/instruments/all.txt') as stream:
        all_instruments = parse_instruments(stream.read(), 'all instruments')
    if calendar[-1].isoformat() != RELEASE_TAG:
        raise ValueError('fixed release calendar endpoint mismatch')
    source_anomalies = sorted(symbol for symbol in constituents
                              if re.fullmatch(r'(SH|SZ)[0-9]{6}', symbol) is None
                              or symbol not in all_instruments)
    if any(any(last >= date(2021, 1, 1) for _, last in constituents[symbol])
           for symbol in source_anomalies):
        raise ValueError('2021+ CSI 300 constituent absent from valid SH/SZ release symbols')
    audited_constituents = {symbol: spans for symbol, spans in constituents.items()
                            if symbol not in source_anomalies}
    masks = build_membership_masks(calendar, audited_constituents)

    def release_members_on(day_text: str) -> set[str]:
        day = date.fromisoformat(day_text)
        position = bisect_left(calendar, day)
        if position >= len(calendar) or calendar[position] != day:
            raise ValueError(f'fixed release lacks evidence date {day_text}')
        return {symbol for symbol, mask in masks.items() if mask[position]}

    june10 = release_members_on('2021-06-10')
    june15 = release_members_on('2021-06-15')
    june29 = release_members_on('2021-06-29')
    june30 = release_members_on('2021-06-30')
    membership_lag_example = {
        'official_change_effective_after_close': '2021-06-11',
        'official_added_example': 'SH688111',
        'official_removed_example': 'SH603156',
        'release_2021_06_15_added_example_present': 'SH688111' in june15,
        'release_2021_06_15_removed_example_present': 'SH603156' in june15,
        'release_2021_06_30_added_example_present': 'SH688111' in june30,
        'release_2021_06_30_removed_example_present': 'SH603156' in june30,
        'release_set_difference_2021_06_10_to_06_15': len(june10 ^ june15),
        'release_added_2021_06_29_to_06_30': len(june30 - june29),
        'release_removed_2021_06_29_to_06_30': len(june29 - june30),
        'calendar_sessions_2021_06_15_through_06_29': sum(
            date(2021, 6, 15) <= day <= date(2021, 6, 29) for day in calendar),
    }
    if (membership_lag_example['release_2021_06_15_added_example_present']
            or not membership_lag_example['release_2021_06_15_removed_example_present']
            or not membership_lag_example['release_2021_06_30_added_example_present']
            or membership_lag_example['release_2021_06_30_removed_example_present']
            or membership_lag_example['release_set_difference_2021_06_10_to_06_15'] != 0
            or membership_lag_example['release_added_2021_06_29_to_06_30'] != 25
            or membership_lag_example['release_removed_2021_06_29_to_06_30'] != 25
            or membership_lag_example['calendar_sessions_2021_06_15_through_06_29'] != 11):
        raise ValueError('fixed release membership-lag evidence changed')
    features: dict[str, dict[str, FloatSeries]] = {}
    checked_feature_hashes = []
    for symbol in sorted(audited_constituents):
        features[symbol] = {}
        for field in ('close', 'volume'):
            name = f'qlib_bin/features/{symbol.lower()}/{field}.day.bin'
            with _verified_file(root, index, name) as stream:
                series = _read_float_series(stream.read(), len(calendar), name)
            features[symbol][field] = series
            checked_feature_hashes.append((name, index['files'][name]['sha256']))
    lineage = {
        'source_id': 'investment_data_qlib_release',
        'release': index['release'],
        'published_index_sha256': 'sha256:' + hashlib.sha256(
            (root / 'release-index.json').read_bytes()).hexdigest(),
        'calendar_sha256': index['files']['qlib_bin/calendars/day.txt']['sha256'],
        'csi300_intervals_sha256': index['files']['qlib_bin/instruments/csi300.txt']['sha256'],
        'all_instruments_sha256': index['files']['qlib_bin/instruments/all.txt']['sha256'],
        'verified_feature_file_count': len(checked_feature_hashes),
        'verified_feature_index_digest': sha_json(checked_feature_hashes),
        'csi300_source_symbol_count': len(constituents),
        'audited_sh_sz_symbol_count': len(audited_constituents),
        'csi300_interval_count': sum(len(spans) for spans in constituents.values()),
        'excluded_pre_2021_source_anomalies': [
            {'symbol': symbol,
             'last_interval_end': max(last for _, last in constituents[symbol]).isoformat()}
            for symbol in source_anomalies],
        'membership_provenance': {
            'external_sources_checked_on': PROVENANCE_CHECKED_ON,
            'pinned_upstream_qlib_index_export_code': UPSTREAM_INDEX_EXPORT_URL,
            'pinned_upstream_tushare_index_weight_collection_code': UPSTREAM_TUSHARE_COLLECTION_URL,
            'official_2021_csi300_change_notice': OFFICIAL_2021_CHANGE_URL,
            'fixed_release_local_lag_example': membership_lag_example,
        },
    }
    return calendar, masks, features, lineage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path,
                        default=Path('data/quant/qlib-releases/2026-09-28/published'))
    parser.add_argument('--manifest', type=Path,
                        default=Path('data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json'))
    parser.add_argument('--out', type=Path,
                        default=Path('studies/random-entry-baseline-v1/qlib-csi300-entry-eligibility-v1.json'))
    args = parser.parse_args()
    source_code = Path(__file__).read_bytes()
    source_digest = 'sha256:' + hashlib.sha256(source_code).hexdigest()
    calendar, masks, features, lineage = load_verified_source(args.root, args.manifest)
    report = evaluate(calendar, masks, features)
    if 'sha256:' + hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != source_digest:
        raise ValueError('audit source changed during run')
    report['first_calendar_date'] = calendar[0].isoformat()
    report['last_calendar_date'] = calendar[-1].isoformat()
    report['calendar_sessions'] = len(calendar)
    report['source_lineage'] = lineage
    report['audit_source_sha256'] = source_digest
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True,
                                   indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'out': str(args.out),
                      'cases': sum(len(seeds) for horizons in report['cases'].values()
                                   for seeds in horizons.values())}, ensure_ascii=False))


if __name__ == '__main__':
    main()
