#!/usr/bin/env python3
"""Resume one fixed GitHub Qlib release with a few verified byte ranges.

The manifest supplies the expected size and SHA-256. An existing archive file
is treated as a prefix from an interrupted single-stream download; numbered
part files make the remaining ranges independently resumable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen


CHUNK = 1024 * 1024
RELEASE_BASE = "https://github.com/chenditc/investment_data/releases/download"


def download_part(url: str, path: Path, start: int, end: int, total: int) -> None:
    for attempt in range(3):
        existing = path.stat().st_size if path.exists() else 0
        wanted = end - start + 1
        if existing == wanted:
            return
        if existing > wanted:
            raise ValueError(f"part is too large: {path}")
        offset = start + existing
        request = Request(url, headers={"Range": f"bytes={offset}-{end}"})
        try:
            with urlopen(request, timeout=90) as response:
                actual_range = response.headers.get("Content-Range", "")
                if response.status != 206 or actual_range != f"bytes {offset}-{end}/{total}":
                    raise ValueError(f"unexpected range response: {response.status} {actual_range}")
                with path.open("ab") as output:
                    while block := response.read(CHUNK):
                        output.write(block)
            if path.stat().st_size == wanted:
                return
        except (OSError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"download failed for {path.name}: {exc}") from exc
            time.sleep(2 ** attempt + 2)
    raise RuntimeError(f"incomplete range: {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        parser.error("workers must be 1..4")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    tag = manifest["release_tag"]
    total = manifest["archive_size_bytes"]
    expected = manifest["archive_sha256"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", tag) or not isinstance(total, int) or total <= 0:
        raise ValueError("invalid release manifest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise ValueError("invalid archive digest")
    args.archive.parent.mkdir(parents=True, exist_ok=True)
    args.archive.touch(exist_ok=True)
    prefix = args.archive.stat().st_size
    if prefix > total:
        raise ValueError("existing archive is larger than manifest")
    url = f"{RELEASE_BASE}/{tag}/qlib_bin.tar.gz"
    remaining = total - prefix
    parts: list[tuple[Path, int, int]] = []
    if remaining:
        count = min(args.workers, remaining)
        width = (remaining + count - 1) // count
        for index in range(count):
            start = prefix + index * width
            end = min(total - 1, start + width - 1)
            if start <= end:
                parts.append((args.archive.with_name(args.archive.name + f".part-{index}"), start, end))
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(download_part, url, path, start, end, total)
                       for path, start, end in parts]
            for future in futures:
                future.result()
    combined = args.archive.with_name(args.archive.name + ".verified.tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with combined.open("wb") as output:
            for source in [args.archive, *(path for path, _, _ in parts)]:
                with source.open("rb") as stream:
                    while block := stream.read(CHUNK):
                        output.write(block)
                        digest.update(block)
                        size += len(block)
            output.flush()
            os.fsync(output.fileno())
        actual = "sha256:" + digest.hexdigest()
        if size != total or actual != expected:
            raise ValueError(f"archive identity mismatch: {size} bytes, {actual}")
        os.replace(combined, args.archive)
        for path, _, _ in parts:
            path.unlink()
        print(json.dumps({"archive": str(args.archive), "bytes": size,
                          "sha256": actual, "tag": tag}))
        return 0
    finally:
        combined.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
