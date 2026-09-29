"""Read-only inspection of a verified investment_data Qlib release archive.

The archive is never extracted and its adjusted features never enter MarketStore.
Inspection is deliberately separate from publication, screening, and backtests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import posixpath
import re
import struct
import sys
import tarfile
from collections import defaultdict
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO


SOURCE_ID = "investment_data_qlib_release"
MANIFEST_KEYS = (
    "release_tag", "target_trade_date", "future_start_date", "future_end_date",
    "dolt_commit", "investment_data_commit", "qlib_commit", "image_digest",
    "archive_size_bytes", "archive_sha256",
)
REQUIRED_MEMBERS = (
    "qlib_bin/calendars/day.txt", "qlib_bin/calendars/day_future.txt",
    "qlib_bin/instruments/all.txt", "qlib_bin/instruments/csi300.txt",
    "qlib_bin/instruments/csi500.txt", "qlib_bin/instruments/csi800.txt",
    "qlib_bin/instruments/csi1000.txt", "qlib_bin/instruments/csiall.txt",
)
FEATURE_RE = re.compile(r"qlib_bin/features/([a-z0-9]+)/([a-z0-9_]+)\.day\.bin\Z")
SYMBOL_RE = re.compile(r"(SH|SZ|BJ)[0-9]{6}\Z")
SHA_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
GIT_RE = re.compile(r"[0-9a-f]{40}\Z")
DOLT_RE = re.compile(r"[0-9a-v]{32}\Z")
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024 * 1024


class QlibArchiveError(ValueError):
    """An archive is unverified, malformed, or inconsistent."""


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise QlibArchiveError(f"invalid {label}: {value!r}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise QlibArchiveError(f"invalid {label}: {value!r}") from exc
    if parsed.isoformat() != value:
        raise QlibArchiveError(f"invalid {label}: {value!r}")
    return parsed


def _manifest(path: Path, expected_tag: str) -> tuple[dict[str, Any], str]:
    _iso_date(expected_tag, "expected tag")
    raw = path.read_bytes()
    if not raw or len(raw) > 16_384:
        raise QlibArchiveError("manifest is empty or unexpectedly large")
    try:
        pairs = json.loads(raw.decode("utf-8"), object_pairs_hook=lambda items: items)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QlibArchiveError("manifest is not valid UTF-8 JSON") from exc
    if (not isinstance(pairs, list) or len(pairs) != len(MANIFEST_KEYS)
            or any(not isinstance(item, tuple) or len(item) != 2 for item in pairs)
            or tuple(key for key, _ in pairs) != MANIFEST_KEYS):
        raise QlibArchiveError("manifest keys do not match the canonical release schema")
    manifest = dict(pairs)
    if manifest["release_tag"] != expected_tag:
        raise QlibArchiveError("release tag does not match expected tag")
    target = _iso_date(manifest["target_trade_date"], "target trade date")
    future_start = _iso_date(manifest["future_start_date"], "future start date")
    future_end = _iso_date(manifest["future_end_date"], "future end date")
    if not target <= _iso_date(expected_tag, "release tag") or not target < future_start <= future_end:
        raise QlibArchiveError("manifest date order is inconsistent")
    if (not isinstance(manifest["dolt_commit"], str)
            or DOLT_RE.fullmatch(manifest["dolt_commit"]) is None):
        raise QlibArchiveError("invalid Dolt commit")
    for field in ("investment_data_commit", "qlib_commit"):
        if not isinstance(manifest[field], str) or GIT_RE.fullmatch(manifest[field]) is None:
            raise QlibArchiveError(f"invalid {field}")
    if (not isinstance(manifest["image_digest"], str)
            or SHA_RE.fullmatch(manifest["image_digest"]) is None):
        raise QlibArchiveError("manifest is not publishable: missing image digest")
    size = manifest["archive_size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise QlibArchiveError("invalid archive size")
    if (not isinstance(manifest["archive_sha256"], str)
            or SHA_RE.fullmatch(manifest["archive_sha256"]) is None):
        raise QlibArchiveError("invalid archive SHA-256")
    return manifest, "sha256:" + hashlib.sha256(raw).hexdigest()


def _verify_archive_bytes(stream: BinaryIO, manifest: dict[str, Any]) -> None:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    if size != manifest["archive_size_bytes"] or "sha256:" + digest.hexdigest() != manifest["archive_sha256"]:
        raise QlibArchiveError("archive size or SHA-256 does not match manifest")
    stream.seek(0)


def _text_lines(raw: bytes, label: str) -> list[str]:
    try:
        value = raw.decode("utf-8")
    except UnicodeError as exc:
        raise QlibArchiveError(f"{label} is not UTF-8") from exc
    if not value or "\r" in value or not value.endswith("\n"):
        raise QlibArchiveError(f"{label} has invalid line endings")
    lines = value[:-1].split("\n")
    if any(not line for line in lines):
        raise QlibArchiveError(f"{label} has an empty line")
    return lines


def parse_calendar(raw: bytes, label: str = "calendar") -> tuple[date, ...]:
    days = tuple(_iso_date(value, label) for value in _text_lines(raw, label))
    if list(days) != sorted(set(days)):
        raise QlibArchiveError(f"{label} is not sorted and unique")
    return days


def parse_instruments(raw: bytes, label: str = "instruments") -> dict[str, tuple[tuple[date, date], ...]]:
    rows: list[tuple[str, date, date]] = []
    for line in _text_lines(raw, label):
        fields = line.split("\t")
        if len(fields) != 3:
            raise QlibArchiveError(f"{label} row does not have three fields")
        symbol, start_text, end_text = fields
        if not symbol or not re.fullmatch(r"[A-Za-z0-9_]+", symbol):
            raise QlibArchiveError(f"{label} has invalid symbol: {symbol!r}")
        start, end = _iso_date(start_text, label), _iso_date(end_text, label)
        if start > end:
            raise QlibArchiveError(f"{label} interval is reversed for {symbol}")
        rows.append((symbol.upper(), start, end))
    if rows != sorted(set(rows)):
        raise QlibArchiveError(f"{label} rows are not sorted and unique")
    result: dict[str, list[tuple[date, date]]] = defaultdict(list)
    for symbol, start, end in rows:
        if result[symbol] and start <= result[symbol][-1][1]:
            raise QlibArchiveError(f"{label} has overlapping intervals for {symbol}")
        result[symbol].append((start, end))
    return {symbol: tuple(intervals) for symbol, intervals in result.items()}


def _feature_header(raw: bytes, length: int, label: str) -> tuple[int, int]:
    if length < 8 or length % 4 or len(raw) < 4:
        raise QlibArchiveError(f"{label} has invalid float32 length")
    (header,) = struct.unpack("<f", raw[:4])
    if not math.isfinite(header) or header < 0 or not header.is_integer():
        raise QlibArchiveError(f"{label} has invalid calendar start index")
    return int(header), length // 4 - 1


def read_feature(raw: bytes, calendar_size: int, label: str = "feature") -> tuple[int, tuple[float, ...]]:
    """Decode one official Qlib day.bin: float32 calendar index, then values."""
    start, count = _feature_header(raw, len(raw), label)
    if start + count > calendar_size:
        raise QlibArchiveError(f"{label} extends beyond day calendar")
    values = tuple(item[0] for item in struct.iter_unpack("<f", raw[4:]))
    return start, values


def _safe_member(member: tarfile.TarInfo) -> str:
    name = member.name
    trimmed = name[:-1] if name.endswith("/") else name
    if (not trimmed or name.startswith("/") or "\\" in name
            or posixpath.normpath(trimmed) != trimmed
            or any(part in ("", ".", "..") for part in PurePosixPath(trimmed).parts)
            or (trimmed != "qlib_bin" and not trimmed.startswith("qlib_bin/"))
            or not (member.isdir() or member.isfile()) or getattr(member, "sparse", None)):
        raise QlibArchiveError(f"unsafe archive member: {name!r}")
    if member.size < 0 or member.size > MAX_MEMBER_BYTES:
        raise QlibArchiveError(f"archive member exceeds size limit: {name!r}")
    return trimmed


def _normalize_sample(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    normalized = symbol.strip().upper()
    if normalized.startswith("STOCK:"):
        normalized = normalized[6:]
    match = re.fullmatch(r"([0-9]{6})\.(SH|SZ|BJ)", normalized)
    if match:
        normalized = match.group(2) + match.group(1)
    if SYMBOL_RE.fullmatch(normalized) is None:
        raise QlibArchiveError("sample must be SH600000, SZ000001, or 600000.SH form")
    return normalized


def _stock_id(symbol: str) -> str:
    return f"stock:{symbol[2:]}.{symbol[:2]}"


def inspect_release(
    archive_path: str | Path,
    manifest_path: str | Path,
    expected_tag: str,
    *,
    sample: str | None = None,
    reference_snapshot: str | Path | None = None,
    sample_rows: int = 3,
) -> dict[str, Any]:
    """Verify complete release bytes, then stream its tar once without extracting.

    `sample_rows` chooses up to that many first and last valid rows of one stock.
    Reference catalogue comparison is descriptive; neither it nor `all.txt`
    establishes a point-in-time investable universe.
    """
    if isinstance(sample_rows, bool) or not 1 <= sample_rows <= 20:
        raise QlibArchiveError("sample_rows must be between 1 and 20")
    sample_symbol = _normalize_sample(sample)
    archive, manifest_file = Path(archive_path), Path(manifest_path)
    manifest, manifest_sha256 = _manifest(manifest_file, expected_tag)
    required: dict[str, bytes] = {}
    features: dict[tuple[str, str], tuple[int, int]] = {}
    close_flags: dict[str, tuple[int, bytearray]] = {}
    sample_features: dict[str, bytes] = {}
    members_seen: set[str] = set()
    uncompressed = 0
    with archive.open("rb") as archive_stream:
        identity_before = os.fstat(archive_stream.fileno())
        _verify_archive_bytes(archive_stream, manifest)
        try:
            with tarfile.open(fileobj=archive_stream, mode="r|gz") as bundle:
                for member in bundle:
                    name = _safe_member(member)
                    if name in members_seen:
                        raise QlibArchiveError(f"duplicate archive member: {name}")
                    members_seen.add(name)
                    uncompressed += member.size
                    if uncompressed > MAX_UNCOMPRESSED_BYTES:
                        raise QlibArchiveError("archive exceeds uncompressed size limit")
                    if not member.isfile():
                        continue
                    stream = bundle.extractfile(member)
                    if stream is None:
                        raise QlibArchiveError(f"cannot read archive member: {name}")
                    if name in REQUIRED_MEMBERS:
                        required[name] = stream.read()
                        if len(required[name]) != member.size:
                            raise QlibArchiveError(f"truncated archive member: {name}")
                        continue
                    match = FEATURE_RE.fullmatch(name)
                    if match is None:
                        if name.startswith("qlib_bin/features/") and name.endswith(".day.bin"):
                            raise QlibArchiveError(f"invalid day feature path: {name}")
                        continue
                    symbol, field = match.group(1).upper(), match.group(2)
                    keep_values = field == "close" or symbol == sample_symbol
                    raw = stream.read() if keep_values else stream.read(4)
                    if len(raw) != (member.size if keep_values else 4):
                        raise QlibArchiveError(f"truncated day feature: {name}")
                    start, count = _feature_header(raw, member.size, name)
                    features[(symbol, field)] = (start, count)
                    if field == "close":
                        flags = bytearray(
                            0 if math.isnan(item[0]) else
                            1 if math.isfinite(item[0]) and item[0] > 0 else 2
                            for item in struct.iter_unpack("<f", raw[4:])
                        )
                        close_flags[symbol] = (start, flags)
                    if symbol == sample_symbol:
                        sample_features[field] = raw
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise QlibArchiveError("archive cannot be read as a complete gzip tar") from exc
        identity_after = os.fstat(archive_stream.fileno())
        if (identity_after.st_size != identity_before.st_size
                or identity_after.st_mtime_ns != identity_before.st_mtime_ns):
            raise QlibArchiveError("archive changed during inspection")

    missing = sorted(set(REQUIRED_MEMBERS) - required.keys())
    if missing:
        raise QlibArchiveError(f"archive missing required members: {missing}")
    calendar = parse_calendar(required["qlib_bin/calendars/day.txt"], "day calendar")
    future = parse_calendar(required["qlib_bin/calendars/day_future.txt"], "future calendar")
    target = _iso_date(manifest["target_trade_date"], "target trade date")
    if calendar[-1] != target or future[:len(calendar)] != calendar:
        raise QlibArchiveError("trade calendar target or future prefix mismatch")
    if (len(future) <= len(calendar)
            or future[len(calendar)] != _iso_date(manifest["future_start_date"], "future start")
            or future[-1] != _iso_date(manifest["future_end_date"], "future end")):
        raise QlibArchiveError("future calendar endpoint mismatch")
    instruments = parse_instruments(required["qlib_bin/instruments/all.txt"], "all instruments")
    for name in REQUIRED_MEMBERS[3:]:
        pool = parse_instruments(required[name], name)
        if max(end for spans in pool.values() for _, end in spans) != target:
            raise QlibArchiveError(f"{name} target date mismatch")
    if max(end for spans in instruments.values() for _, end in spans) != target:
        raise QlibArchiveError("all instruments target date mismatch")
    for (symbol, field), (start, count) in features.items():
        if start + count > len(calendar):
            raise QlibArchiveError(f"{symbol}/{field}.day.bin exceeds day calendar")

    field_symbols: dict[str, set[str]] = defaultdict(set)
    for symbol, field in features:
        if symbol in instruments:
            field_symbols[field].add(symbol)
    exchange_counts: dict[str, int] = defaultdict(int)
    for symbol in instruments:
        exchange_counts[symbol[:2] if SYMBOL_RE.fullmatch(symbol) else "other"] += 1

    year_rows: dict[int, int] = defaultdict(int)
    year_symbols: dict[int, set[str]] = defaultdict(set)
    valid_by_symbol: dict[str, int] = {}
    first_valid: date | None = None
    last_valid: date | None = None
    missing_closes = 0
    invalid_closes = 0
    outside_intervals = 0
    for symbol, spans in instruments.items():
        item = close_flags.get(symbol)
        if item is None:
            valid_by_symbol[symbol] = 0
            continue
        start, flags = item
        valid_by_symbol[symbol] = flags.count(1)
        missing_closes += flags.count(0)
        invalid_closes += flags.count(2)
        for offset, flag in enumerate(flags):
            if flag != 1:
                continue
            day = calendar[start + offset]
            if not any(left <= day <= right for left, right in spans):
                outside_intervals += 1
            year_rows[day.year] += 1
            year_symbols[day.year].add(symbol)
            first_valid = day if first_valid is None else min(first_valid, day)
            last_valid = day if last_valid is None else max(last_valid, day)

    result: dict[str, Any] = {
        "status": "inspected",
        "source_id": SOURCE_ID,
        "release": {
            "tag": expected_tag,
            "target_trade_date": manifest["target_trade_date"],
            "archive_sha256": manifest["archive_sha256"],
            "manifest_sha256": manifest_sha256,
            "archive_size_bytes": manifest["archive_size_bytes"],
            "dolt_commit": manifest["dolt_commit"],
            "investment_data_commit": manifest["investment_data_commit"],
            "qlib_commit": manifest["qlib_commit"],
            "image_digest": manifest["image_digest"],
        },
        "calendar": {
            "first": calendar[0].isoformat(), "last": calendar[-1].isoformat(),
            "sessions": len(calendar), "future_last": future[-1].isoformat(),
        },
        "instruments": {
            "count": len(instruments),
            "interval_count": sum(len(value) for value in instruments.values()),
            "sh_sz_count": sum(bool(re.fullmatch(r"(SH|SZ)[0-9]{6}", symbol)) for symbol in instruments),
            "exchange_counts": dict(sorted(exchange_counts.items())),
            "ended_before_target_count": sum(all(end < target for _, end in spans)
                                             for spans in instruments.values()),
            "without_close_feature_count": sum(symbol not in close_flags for symbol in instruments),
            "feature_file_count": len(features),
            "stocks_with_field": {field: len(symbols)
                                  for field, symbols in sorted(field_symbols.items())},
            "orphan_feature_symbol_count": len({symbol for symbol, _ in features} - instruments.keys()),
        },
        "daily_close": {
            "valid_bar_count": sum(valid_by_symbol.values()),
            "stocks_with_valid_close": sum(count > 0 for count in valid_by_symbol.values()),
            "first_valid": first_valid.isoformat() if first_valid else None,
            "last_valid": last_valid.isoformat() if last_valid else None,
            "nan_count": missing_closes,
            "invalid_non_nan_count": invalid_closes,
            "valid_outside_instrument_intervals": outside_intervals,
            "per_year": [
                {"year": year, "stocks": len(year_symbols[year]), "bars": year_rows[year]}
                for year in range(calendar[0].year, target.year + 1)
            ],
        },
        "price_adjustment": "qlib_adjusted; factor must be validated before any raw-price comparison",
        "point_in_time_universe": False,
    }
    if sample_symbol is not None:
        if sample_symbol not in instruments:
            raise QlibArchiveError(f"sample symbol absent from all.txt: {sample_symbol}")
        dates = []
        item = close_flags.get(sample_symbol)
        if item:
            start, flags = item
            dates = [calendar[start + offset] for offset, flag in enumerate(flags) if flag == 1]
        selected = sorted(set(dates[:sample_rows] + dates[-sample_rows:]))
        decoded = {field: read_feature(raw, len(calendar), f"{sample_symbol}/{field}")
                   for field, raw in sample_features.items()}
        index_by_day = {day: index for index, day in enumerate(calendar)}
        sample_data = []
        for day in selected:
            index = index_by_day[day]
            row: dict[str, Any] = {"date": day.isoformat()}
            for field in sorted(decoded):
                field_start, values = decoded[field]
                offset = index - field_start
                value = values[offset] if 0 <= offset < len(values) else math.nan
                row[field] = value if math.isfinite(value) else None
            sample_data.append(row)
        result["sample"] = {
            "symbol": sample_symbol, "instrument_id": _stock_id(sample_symbol),
            "intervals": [{"start": left.isoformat(), "end": right.isoformat()}
                          for left, right in instruments[sample_symbol]],
            "available_fields": sorted(field for sym, field in features if sym == sample_symbol),
            "valid_close_count": valid_by_symbol[sample_symbol], "rows": sample_data,
        }
    if reference_snapshot is not None:
        from quant_lab.baostock_source import read_baostock_historical_snapshot

        catalogue = read_baostock_historical_snapshot(reference_snapshot)
        reference = {item.instrument.instrument_id: item for item in catalogue.listings}
        available = {_stock_id(symbol) for symbol in instruments if SYMBOL_RE.fullmatch(symbol)}
        matched = reference.keys() & available
        inactive = {identifier for identifier, item in reference.items() if item.provider_status == "0"}
        valid_ids = {_stock_id(symbol) for symbol, count in valid_by_symbol.items()
                     if count > 0 and SYMBOL_RE.fullmatch(symbol)}
        result["reference_coverage"] = {
            "snapshot_id": catalogue.snapshot_id,
            "catalogue_count": len(reference),
            "catalogue_present_in_all": len(matched),
            "catalogue_with_valid_close": len(reference.keys() & valid_ids),
            "catalogue_missing_symbols_sample": sorted(reference.keys() - available)[:20],
            "inactive_count": len(inactive),
            "inactive_present_in_all": len(inactive & available),
            "inactive_with_valid_close": len(inactive & valid_ids),
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify and inspect a Qlib release archive without extracting it")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-tag", required=True)
    parser.add_argument("--sample")
    parser.add_argument("--sample-rows", type=int, default=3)
    parser.add_argument("--reference-snapshot")
    args = parser.parse_args(argv)
    try:
        result = inspect_release(args.archive, args.manifest, args.expected_tag,
                                 sample=args.sample, sample_rows=args.sample_rows,
                                 reference_snapshot=args.reference_snapshot)
    except (QlibArchiveError, OSError, ValueError) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
