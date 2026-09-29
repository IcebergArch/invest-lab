"""Safely publish and read one immutable Qlib release outside MarketStore.

Publishing verifies the complete archive before extracting it to a new tree.
Random-access reads verify the published per-file hashes and release manifest.
No adjusted feature is silently converted to an original transaction price.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, BinaryIO

from quant_lab.qlib_archive import (
    FEATURE_RE, MAX_UNCOMPRESSED_BYTES, QlibArchiveError, SOURCE_ID, _iso_date,
    _manifest, _normalize_sample, _safe_member, _stock_id,
    _verify_archive_bytes, inspect_release, parse_calendar, parse_instruments,
)


INDEX_NAME = "release-index.json"
INDEX_VERSION = 1
DEFAULT_FIELDS = ("close", "factor", "amount")
MAX_ROWS = 5000
COPY_CHUNK = 1024 * 1024


def _sha256_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for chunk in iter(lambda: stream.read(COPY_CHUNK), b""):
        digest.update(chunk)
        count += len(chunk)
    return "sha256:" + digest.hexdigest(), count


def _copy_member(source: BinaryIO, destination: Path, size: int) -> str:
    digest = hashlib.sha256()
    copied = 0
    with destination.open("xb") as output:
        while copied < size:
            chunk = source.read(min(COPY_CHUNK, size - copied))
            if not chunk:
                raise QlibArchiveError(f"truncated archive member: {destination.name}")
            output.write(chunk)
            digest.update(chunk)
            copied += len(chunk)
        output.flush()
        os.fsync(output.fileno())
    return "sha256:" + digest.hexdigest()


def publish_release_tree(
    archive_path: str | Path,
    manifest_path: str | Path,
    expected_tag: str,
    output: str | Path,
) -> dict[str, Any]:
    """Validate then publish a new extracted directory atomically.

    An existing destination is never overwritten. On any failure, only this
    invocation's private staging directory is removed.
    """
    archive, manifest_file, target = Path(archive_path), Path(manifest_path), Path(output)
    if target.exists() or target.is_symlink():
        raise QlibArchiveError(f"publication target already exists: {target}")
    inspected = inspect_release(archive, manifest_file, expected_tag)
    manifest, manifest_digest = _manifest(manifest_file, expected_tag)
    if manifest_digest != inspected["release"]["manifest_sha256"]:
        raise QlibArchiveError("manifest changed after inspection")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    published = False
    try:
        files: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        total = 0
        with archive.open("rb") as stream:
            identity_before = os.fstat(stream.fileno())
            _verify_archive_bytes(stream, manifest)
            try:
                with tarfile.open(fileobj=stream, mode="r|gz") as bundle:
                    for member in bundle:
                        name = _safe_member(member)
                        if name in seen:
                            raise QlibArchiveError(f"duplicate archive member: {name}")
                        seen.add(name)
                        total += member.size
                        if total > MAX_UNCOMPRESSED_BYTES:
                            raise QlibArchiveError("archive exceeds uncompressed size limit")
                        destination = staging.joinpath(*name.split("/"))
                        if member.isdir():
                            destination.mkdir(parents=True, exist_ok=True)
                            continue
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        source = bundle.extractfile(member)
                        if source is None:
                            raise QlibArchiveError(f"cannot read archive member: {name}")
                        digest = _copy_member(source, destination, member.size)
                        files[name] = {"size": member.size, "sha256": digest}
            except (tarfile.TarError, EOFError, OSError) as exc:
                raise QlibArchiveError("archive could not be extracted safely") from exc
            identity_after = os.fstat(stream.fileno())
            if (identity_before.st_size != identity_after.st_size
                    or identity_before.st_mtime_ns != identity_after.st_mtime_ns):
                raise QlibArchiveError("archive changed during publication")
        if sum(FEATURE_RE.fullmatch(name) is not None for name in files) != inspected["instruments"]["feature_file_count"]:
            raise QlibArchiveError("extracted feature set differs from inspected release")
        index = {
            "index_version": INDEX_VERSION,
            "source_id": SOURCE_ID,
            "release": inspected["release"],
            "calendar": inspected["calendar"],
            "file_count": len(files),
            "uncompressed_file_bytes": sum(item["size"] for item in files.values()),
            "files": files,
        }
        index_path = staging / INDEX_NAME
        with index_path.open("x", encoding="utf-8") as output_stream:
            json.dump(index, output_stream, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))
            output_stream.write("\n")
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if target.exists() or target.is_symlink():
            raise QlibArchiveError(f"publication target appeared during extraction: {target}")
        os.rename(staging, target)
        published = True
        return {
            "status": "published", "source_id": SOURCE_ID,
            "root": str(target.resolve()), "release": inspected["release"],
            "file_count": len(files), "uncompressed_file_bytes": index["uncompressed_file_bytes"],
        }
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)


def _load_index(root: Path, manifest_path: Path, expected_tag: str) -> dict[str, Any]:
    manifest, manifest_digest = _manifest(manifest_path, expected_tag)
    index_path = root / INDEX_NAME
    if index_path.is_symlink() or not index_path.is_file():
        raise QlibArchiveError("published release index is absent or is a symlink")
    raw = index_path.read_bytes()
    if not raw or len(raw) > 64 * 1024 * 1024:
        raise QlibArchiveError("published release index has invalid size")
    try:
        index = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QlibArchiveError("published release index is malformed") from exc
    if (not isinstance(index, dict) or index.get("index_version") != INDEX_VERSION
            or index.get("source_id") != SOURCE_ID or not isinstance(index.get("files"), dict)
            or not isinstance(index.get("release"), dict)):
        raise QlibArchiveError("published release index has invalid schema")
    release = index["release"]
    if (release.get("tag") != expected_tag
            or release.get("target_trade_date") != manifest["target_trade_date"]
            or release.get("archive_sha256") != manifest["archive_sha256"]
            or release.get("manifest_sha256") != manifest_digest):
        raise QlibArchiveError("published release index does not match manifest")
    return index


def _verified_file(root: Path, index: dict[str, Any], name: str) -> BinaryIO:
    files = index["files"]
    entry = files.get(name)
    if (not isinstance(entry, dict) or isinstance(entry.get("size"), bool)
            or not isinstance(entry.get("size"), int) or entry["size"] < 0
            or not isinstance(entry.get("sha256"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", entry["sha256"]) is None):
        raise QlibArchiveError(f"file absent from verified index: {name}")
    if (not name.startswith("qlib_bin/") or "\\" in name
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise QlibArchiveError(f"invalid indexed path: {name}")
    path = root.joinpath(*name.split("/"))
    if os.path.commonpath((str(root.resolve()), str(path.resolve()))) != str(root.resolve()):
        raise QlibArchiveError(f"indexed path escapes release root: {name}")
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise QlibArchiveError(f"symlink in published release: {name}")
        cursor = cursor.parent
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    stream = os.fdopen(descriptor, "rb")
    try:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != entry["size"]:
            raise QlibArchiveError(f"published file size changed: {name}")
        digest, size = _sha256_stream(stream)
        if size != entry["size"] or digest != entry["sha256"]:
            raise QlibArchiveError(f"published file hash changed: {name}")
        stream.seek(0)
        return stream
    except Exception:
        stream.close()
        raise


def _resolve_symbol(query: str, instruments: dict[str, Any]) -> str:
    if re.fullmatch(r"[0-9]{6}", query.strip()):
        code = query.strip()
        matches = [symbol for symbol in instruments if symbol.endswith(code)
                   and re.fullmatch(r"(SH|SZ|BJ)[0-9]{6}", symbol)]
        if not matches:
            raise QlibArchiveError(f"stock absent from all.txt: {code}")
        if len(matches) > 1:
            raise QlibArchiveError(f"six-digit code is ambiguous: {code}")
        return matches[0]
    symbol = _normalize_sample(query)
    if symbol not in instruments:
        raise QlibArchiveError(f"stock absent from all.txt: {symbol}")
    return symbol


def _read_feature_window(
    stream: BinaryIO, calendar_size: int, positions: list[int], label: str,
) -> list[float | None]:
    length = os.fstat(stream.fileno()).st_size
    if length < 8 or length % 4:
        raise QlibArchiveError(f"{label} has invalid float32 length")
    first = stream.read(4)
    if len(first) != 4:
        raise QlibArchiveError(f"{label} is truncated")
    (header,) = struct.unpack("<f", first)
    if not math.isfinite(header) or header < 0 or not header.is_integer():
        raise QlibArchiveError(f"{label} has invalid calendar start index")
    start = int(header)
    count = length // 4 - 1
    if start + count > calendar_size:
        raise QlibArchiveError(f"{label} extends beyond day calendar")
    if not positions:
        return []
    low = max(start, positions[0])
    high = min(start + count, positions[-1] + 1)
    decoded: tuple[float, ...] = ()
    if low < high:
        stream.seek(4 + 4 * (low - start))
        raw = stream.read(4 * (high - low))
        if len(raw) != 4 * (high - low):
            raise QlibArchiveError(f"{label} is truncated")
        decoded = tuple(value[0] for value in struct.iter_unpack("<f", raw))
    result = []
    for index in positions:
        if low <= index < high:
            value = decoded[index - low]
            result.append(value if math.isfinite(value) else None)
        else:
            result.append(None)
    return result


def read_stock(
    root: str | Path,
    manifest_path: str | Path,
    expected_tag: str,
    stock: str,
    *,
    start: str | None = None,
    end: str | None = None,
    fields: tuple[str, ...] = DEFAULT_FIELDS,
    limit: int = 100,
    include_close_coverage: bool = False,
) -> dict[str, Any]:
    """Read a bounded date slice from a published tree after per-file hashes."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise QlibArchiveError(f"limit must be between 1 and {MAX_ROWS}")
    if not fields or len(fields) > 20 or len(set(fields)) != len(fields):
        raise QlibArchiveError("fields must be 1..20 unique feature names")
    if any(re.fullmatch(r"[a-z0-9_]+", field) is None for field in fields):
        raise QlibArchiveError("feature names must be lower-case alphanumeric")
    left = _iso_date(start, "start") if start else None
    right = _iso_date(end, "end") if end else None
    if left and right and left > right:
        raise QlibArchiveError("start must not exceed end")
    directory = Path(root).resolve()
    index = _load_index(directory, Path(manifest_path), expected_tag)
    with _verified_file(directory, index, "qlib_bin/calendars/day.txt") as stream:
        calendar = parse_calendar(stream.read(), "day calendar")
    with _verified_file(directory, index, "qlib_bin/instruments/all.txt") as stream:
        instruments = parse_instruments(stream.read(), "all instruments")
    if calendar[-1].isoformat() != index["release"]["target_trade_date"]:
        raise QlibArchiveError("published day calendar target changed")
    symbol = _resolve_symbol(stock, instruments)
    spans = instruments[symbol]
    lower = bisect.bisect_left(calendar, left) if left else 0
    upper = bisect.bisect_right(calendar, right) if right else len(calendar)
    positions = [index_number for index_number in range(lower, upper)
                 if any(begin <= calendar[index_number] <= finish for begin, finish in spans)]
    total = len(positions)
    truncated = total > limit
    recent_positions = positions[-limit:]
    columns: dict[str, list[float | None]] = {}
    absent = []
    for field in fields:
        name = f"qlib_bin/features/{symbol.lower()}/{field}.day.bin"
        if name not in index["files"]:
            absent.append(field)
            columns[field] = [None] * len(recent_positions)
            continue
        with _verified_file(directory, index, name) as stream:
            columns[field] = _read_feature_window(stream, len(calendar), recent_positions, name)
    close_coverage = None
    if include_close_coverage:
        name = f"qlib_bin/features/{symbol.lower()}/close.day.bin"
        if name in index["files"]:
            with _verified_file(directory, index, name) as stream:
                closes = _read_feature_window(stream, len(calendar), positions, name)
            valid = [position for position, value in zip(positions, closes)
                     if value is not None and value > 0]
            close_coverage = {
                "first_valid": calendar[valid[0]].isoformat() if valid else None,
                "last_valid": calendar[valid[-1]].isoformat() if valid else None,
                "valid_close_count": len(valid),
                "missing_or_invalid_close_count": len(positions) - len(valid),
                "instrument_calendar_rows": len(positions),
            }
    rows = [{"date": calendar[position].isoformat(),
             **{field: columns[field][row_number] for field in fields}}
            for row_number, position in enumerate(recent_positions)]
    return {
        "status": "ready", "source_id": SOURCE_ID,
        "release": index["release"],
        "stock": {"symbol": symbol, "instrument_id": _stock_id(symbol),
                  "intervals": [{"start": begin.isoformat(), "end": finish.isoformat()}
                                for begin, finish in spans]},
        "adjustment": "qlib_adjusted",
        "fields": list(fields), "missing_fields": absent,
        "requested_start": left.isoformat() if left else None,
        "requested_end": right.isoformat() if right else None,
        "calendar_rows_in_range": total,
        "returned_rows": len(rows), "truncated_to_latest": truncated,
        "close_coverage": close_coverage,
        "rows": rows,
        "raw_price_factor_validation": "pending_cross_source_check",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish or read a verified Qlib release tree")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish", help="verify, safely extract, atomically publish")
    publish.add_argument("--archive", required=True)
    publish.add_argument("--manifest", required=True)
    publish.add_argument("--expected-tag", required=True)
    publish.add_argument("--out", required=True)
    stock = commands.add_parser("stock", help="read one bounded stock slice")
    stock.add_argument("--root", required=True)
    stock.add_argument("--manifest", required=True)
    stock.add_argument("--expected-tag", required=True)
    stock.add_argument("--stock", required=True)
    stock.add_argument("--start")
    stock.add_argument("--end")
    stock.add_argument("--fields", default=",".join(DEFAULT_FIELDS))
    stock.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        if args.command == "publish":
            result = publish_release_tree(args.archive, args.manifest, args.expected_tag, args.out)
        else:
            fields = tuple(part.strip() for part in args.fields.split(","))
            result = read_stock(args.root, args.manifest, args.expected_tag, args.stock,
                                start=args.start, end=args.end, fields=fields, limit=args.limit)
    except (QlibArchiveError, OSError, ValueError) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
