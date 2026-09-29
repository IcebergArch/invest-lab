"""Publish a bounded, read-only checkpoint summary for the visual service.

The backfill worker opens the archive with SQLite write access only so WAL
sidecars can be created when absent.  ``query_only`` prevents database edits.
The HTTP service reads only the atomically published JSON artifact.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quant_lab.baostock_source import SOURCE_ID as BAOSTOCK_SOURCE_ID


def checkpoint_summary(path: str | Path) -> dict[str, Any]:
    archive = Path(path).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"archive DB does not exist: {archive}")
    connection = sqlite3.connect(archive.as_uri() + "?mode=rw", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        catalogue = connection.execute("""
            SELECT snapshot_id,scope,collected_at,listing_count
            FROM backfill_snapshots ORDER BY recorded_at DESC LIMIT 1
        """).fetchone()
        if catalogue is None:
            raise ValueError("archive has no catalogue snapshot")
        progress = connection.execute("""
            SELECT COUNT(DISTINCT CASE WHEN status='success' AND bar_count>0
                                       THEN instrument_id END) AS stock_count,
                   COALESCE(SUM(CASE WHEN status IN ('success','empty')
                                     THEN 1 ELSE 0 END),0) AS completed_window_count,
                   COALESCE(SUM(CASE WHEN status='success'
                                     THEN bar_count ELSE 0 END),0) AS bar_count,
                   COALESCE(SUM(CASE WHEN status='failed'
                                     THEN 1 ELSE 0 END),0) AS failed_window_count
            FROM backfill_windows WHERE source_id=? AND adjustment='none'
        """, (BAOSTOCK_SOURCE_ID,)).fetchone()
        return {
            "status": "ready",
            "source_id": BAOSTOCK_SOURCE_ID,
            "snapshot_id": catalogue["snapshot_id"],
            "catalogue_scope": catalogue["scope"],
            "catalogue_collected_at": catalogue["collected_at"],
            "catalogue_stock_count": catalogue["listing_count"],
            "stock_count": progress["stock_count"],
            "completed_window_count": progress["completed_window_count"],
            "bar_count": progress["bar_count"],
            "failed_window_count": progress["failed_window_count"],
            "adjustment": "none",
            "eligible_for_screening": False,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        connection.close()


def publish_checkpoint(path: str | Path, output: str | Path) -> dict[str, Any]:
    summary = checkpoint_summary(path)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=f".{target.name}.", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(summary, stream, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return summary
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish archive checkpoint summary")
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(publish_checkpoint(args.db, args.out), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
