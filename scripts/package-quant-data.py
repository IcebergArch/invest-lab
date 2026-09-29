#!/usr/bin/env python3
"""Create and verify portable ZIP backups of the local quant data and reports.

Examples:
    python3 scripts/package-quant-data.py
    python3 scripts/package-quant-data.py --mode split
    python3 scripts/package-quant-data.py --verify backups/quant-full-....zip

SQLite files are copied with the SQLite backup API, so an active WAL database is
captured consistently without copying its transient -wal and -shm files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (Path("data/quant"), Path("reports/quant"))
SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
STORED_SUFFIXES = {".gz", ".zip", ".xz", ".bz2", ".zst", ".jpg", ".jpeg", ".png", ".pdf"}
CHUNK_BYTES = 1024 * 1024
MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class SourceFile:
    path: Path
    archive_path: str
    category: str
    dataset: str
    sqlite: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def excluded_name(name: str) -> bool:
    lower = name.lower()
    return (
        lower == ".ds_store"
        or lower.endswith(("-wal", "-shm", "-journal", ".lock", ".tmp", ".partial", ".swp", ".swo"))
        or lower.startswith(".~")
    )


def source_files(root: Path) -> list[SourceFile]:
    result: list[SourceFile] = []
    for category in SOURCES:
        source_root = root / category
        if not source_root.is_dir():
            raise FileNotFoundError(f"数据目录不存在：{source_root}")
        for directory, subdirs, filenames in os.walk(source_root, followlinks=False):
            subdirs[:] = sorted(
                name for name in subdirs
                if not excluded_name(name) and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(filenames):
                path = Path(directory) / name
                if excluded_name(name) or path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(source_root)
                archive_path = (category / relative).as_posix()
                dataset = relative.parts[0] if len(relative.parts) > 1 else "_root"
                result.append(SourceFile(
                    path=path,
                    archive_path=archive_path,
                    category=category.as_posix(),
                    dataset=dataset,
                    sqlite=path.suffix.lower() in SQLITE_SUFFIXES,
                ))
    return result


def snapshot_sqlite(source: Path, snapshot_dir: Path) -> Path:
    snapshot = snapshot_dir / f"{uuid.uuid4().hex}.sqlite3"
    source_uri = f"file:{quote(str(source), safe='/')}?mode=ro"
    reader = sqlite3.connect(source_uri, uri=True, timeout=30)
    writer = sqlite3.connect(str(snapshot), timeout=30)
    try:
        reader.backup(writer, pages=1024, sleep=0.1)
        writer.commit()
    finally:
        writer.close()
        reader.close()
    return snapshot


def write_member(archive: zipfile.ZipFile, source: Path, archive_path: str) -> tuple[int, str]:
    info = zipfile.ZipInfo(archive_path)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_STORED if source.suffix.lower() in STORED_SUFFIXES else zipfile.ZIP_DEFLATED
    digest = hashlib.sha256()
    count = 0
    with source.open("rb") as reader, archive.open(info, "w", force_zip64=True) as writer:
        while chunk := reader.read(CHUNK_BYTES):
            writer.write(chunk)
            digest.update(chunk)
            count += len(chunk)
    return count, digest.hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path) -> dict[str, object]:
    """Read every member once; ZipFile checks CRC while we check manifest hashes."""
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or names.count(MANIFEST_NAME) != 1:
            raise ValueError("ZIP 条目重复或缺少 manifest.json")
        manifest = json.loads(archive.read(MANIFEST_NAME))
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
            raise ValueError("清单格式无效")
        files = manifest["files"]
        expected = {MANIFEST_NAME}
        total_bytes = 0
        for entry in files:
            name = entry["path"]
            safe_path = PurePosixPath(name)
            if not name or safe_path.is_absolute() or ".." in safe_path.parts or name in expected:
                raise ValueError(f"清单路径无效或重复：{name}")
            expected.add(name)
            if name not in names:
                raise ValueError(f"ZIP 缺少文件：{name}")
            digest = hashlib.sha256()
            count = 0
            with archive.open(name, "r") as stream:
                while chunk := stream.read(CHUNK_BYTES):
                    digest.update(chunk)
                    count += len(chunk)
            if count != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
                raise ValueError(f"清单校验失败：{name}")
            total_bytes += count
        if set(names) != expected:
            raise ValueError("ZIP 存在清单外的文件")
    return {
        "path": str(path.resolve()),
        "archive_bytes": path.stat().st_size,
        "archive_sha256": hash_file(path),
        "file_count": len(files),
        "uncompressed_bytes": total_bytes,
        "verified": True,
    }


def backup_one(files: Iterable[SourceFile], output_dir: Path, label: str) -> dict[str, object]:
    selected = list(files)
    if not selected:
        raise ValueError(f"没有可打包文件：{label}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "dataset"
    filename = f"quant-{safe_label}-{stamp}-{uuid.uuid4().hex[:8]}.zip"
    final_path = output_dir / filename
    partial_path = output_dir / f".{filename}.partial"
    manifest: dict[str, object] = {
        "schema_version": 1,
        "generated_at_utc": utc_now(),
        "label": label,
        "sources": sorted({item.category for item in selected}),
        "sqlite_capture": "sqlite3.Connection.backup",
        "files": [],
    }
    try:
        # Both temporary snapshots and the output live outside the source roots.
        with tempfile.TemporaryDirectory(prefix=".quant-snapshot-", dir=output_dir) as temporary:
            snapshot_dir = Path(temporary)
            with zipfile.ZipFile(partial_path, mode="x", allowZip64=True, compresslevel=3) as archive:
                for index, item in enumerate(selected, 1):
                    payload = item.path
                    if item.sqlite:
                        print(f"SQLite 一致性快照：{item.archive_path}", file=sys.stderr, flush=True)
                        payload = snapshot_sqlite(item.path, snapshot_dir)
                    try:
                        size, digest = write_member(archive, payload, item.archive_path)
                    finally:
                        if item.sqlite:
                            payload.unlink(missing_ok=True)
                    manifest["files"].append({
                        "path": item.archive_path,
                        "bytes": size,
                        "sha256": digest,
                        "source_category": item.category,
                        "capture": "sqlite_backup" if item.sqlite else "file_copy",
                    })
                    if index % 2000 == 0 or index == len(selected):
                        print(f"已写入 {index:,}/{len(selected):,} 个文件", file=sys.stderr, flush=True)
                archive.writestr(
                    MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
                    compress_type=zipfile.ZIP_DEFLATED,
                )
        print("校验 ZIP CRC 与清单 SHA256…", file=sys.stderr, flush=True)
        summary = verify_archive(partial_path)
        # Link publication is atomic and fails if the final filename already exists.
        os.link(partial_path, final_path)
        partial_path.unlink()
        summary["path"] = str(final_path.resolve())
        return summary
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="项目根目录，默认脚本所在项目")
    parser.add_argument("--output-dir", type=Path, help="输出目录，默认项目 backups/")
    parser.add_argument("--mode", choices=("single", "split"), default="single", help="完整单包或按顶层数据集拆包")
    parser.add_argument("--verify", type=Path, help="只校验已有 ZIP，不执行打包")
    args = parser.parse_args()
    try:
        if args.verify is not None:
            print(json.dumps(verify_archive(args.verify), ensure_ascii=False, sort_keys=True))
            return 0
        root = args.root.resolve()
        output_dir = (args.output_dir or root / "backups").resolve()
        for source in SOURCES:
            source_root = (root / source).resolve()
            if output_dir == source_root or source_root in output_dir.parents:
                parser.error("输出目录不能位于待打包的数据目录内")
        output_dir.mkdir(parents=True, exist_ok=True)
        files = source_files(root)
        if args.mode == "single":
            summaries = [backup_one(files, output_dir, "full")]
        else:
            groups: dict[tuple[str, str], list[SourceFile]] = {}
            for item in files:
                groups.setdefault((item.category, item.dataset), []).append(item)
            summaries = [
                backup_one(group, output_dir, f"{category.replace('/', '-')}-{dataset}")
                for (category, dataset), group in sorted(groups.items())
            ]
        for summary in summaries:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile, KeyError, TypeError) as exc:
        print(f"打包或校验失败：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
