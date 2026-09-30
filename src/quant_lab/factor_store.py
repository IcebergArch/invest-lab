"""One append-only factor observation store for research and decisions.

Analysis may inspect retrospective observations. Decision reads require evidence
that the source was captured and the final input was available by decision_at.
Registration of an observation never validates a trading strategy.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence


STORE_VERSION = "factor-observations-v1"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _timestamp(value: datetime | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone aware")
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class FactorObservation:
    factor_id: str
    factor_version: str
    parameters: dict[str, int | float | str]
    instrument_id: str
    asof_date: date
    value: float | None
    status: str
    output_unit: str
    price_basis: str
    source_id: str
    source_snapshot_id: str
    input_sha256: str
    source_captured_at: datetime
    first_available_at: datetime | None
    availability_evidence: str  # verified_first_available | retrospective
    availability_evidence_id: str | None = None

    def record(self) -> dict[str, object]:
        if (not all(isinstance(item, str) and item for item in (
                self.factor_id, self.factor_version, self.instrument_id,
                self.output_unit, self.price_basis, self.source_id,
                self.source_snapshot_id)) or type(self.asof_date) is not date):
            raise ValueError("incomplete factor identity")
        if len(self.input_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.input_sha256):
            raise ValueError("input_sha256 must be a lowercase SHA-256")
        if (self.status not in ("ok", "insufficient_history", "missing_input")
                or (self.status == "ok") != (self.value is not None)):
            raise ValueError("factor value and status disagree")
        if self.value is not None and (isinstance(self.value, bool)
                                       or not isinstance(self.value, (float, int))
                                       or not math.isfinite(self.value)):
            raise ValueError("factor value must be finite")
        if self.availability_evidence not in ("verified_first_available", "retrospective"):
            raise ValueError("unknown availability evidence")
        captured = _timestamp(self.source_captured_at, "source_captured_at")
        available = _timestamp(self.first_available_at, "first_available_at")
        if self.availability_evidence == "verified_first_available" and (
                available is None or not self.availability_evidence_id):
            raise ValueError("verified input needs a first availability timestamp and evidence ID")
        if available is not None and available < self.asof_date.isoformat():
            raise ValueError("input cannot be available before its as-of date")
        if not isinstance(self.parameters, dict):
            raise ValueError("factor parameters must be an object")
        params = _canonical(self.parameters)
        return {
            "factor_id": self.factor_id, "factor_version": self.factor_version,
            "parameters_json": params, "instrument_id": self.instrument_id,
            "asof_date": self.asof_date.isoformat(), "value": self.value,
            "status": self.status, "output_unit": self.output_unit,
            "price_basis": self.price_basis, "source_id": self.source_id,
            "source_snapshot_id": self.source_snapshot_id,
            "input_sha256": self.input_sha256, "source_captured_at": captured,
            "first_available_at": available,
            "availability_evidence": self.availability_evidence,
            "availability_evidence_id": self.availability_evidence_id,
        }


class FactorStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS factor_observations (
                    observation_id TEXT PRIMARY KEY,
                    identity_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    factor_id TEXT NOT NULL,
                    factor_version TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    asof_date TEXT NOT NULL,
                    price_basis TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_snapshot_id TEXT NOT NULL,
                    source_captured_at TEXT NOT NULL,
                    first_available_at TEXT,
                    availability_evidence TEXT NOT NULL,
                    status TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_factor_analysis
                    ON factor_observations(instrument_id, asof_date, factor_id);
                CREATE INDEX IF NOT EXISTS idx_factor_decision
                    ON factor_observations(source_snapshot_id, asof_date, instrument_id);
            """)

    def append(self, observations: Sequence[FactorObservation]) -> list[str]:
        rows = []
        for observation in observations:
            payload = observation.record()
            identity = {key: payload[key] for key in (
                "factor_id", "factor_version", "parameters_json", "instrument_id",
                "asof_date", "price_basis", "source_id", "source_snapshot_id",
                "input_sha256")}
            identity_sha = _hash(identity)
            rows.append((identity_sha, _canonical(payload), payload))
        if not rows:
            return []
        recorded = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as connection:
            for identity_sha, serialized, payload in rows:
                existing = connection.execute(
                    "SELECT payload_json FROM factor_observations WHERE identity_sha256=?",
                    (identity_sha,)).fetchone()
                if existing:
                    saved = json.loads(existing[0])
                    saved.setdefault("availability_evidence_id", None)
                    if _canonical(saved) != serialized:
                        raise ValueError("factor recomputation conflicts with saved input identity")
                    continue
                connection.execute("""
                    INSERT INTO factor_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (identity_sha, identity_sha, serialized, payload["factor_id"],
                          payload["factor_version"], payload["parameters_json"],
                          payload["instrument_id"], payload["asof_date"],
                          payload["price_basis"], payload["source_id"],
                          payload["source_snapshot_id"], payload["source_captured_at"],
                          payload["first_available_at"], payload["availability_evidence"],
                          payload["status"], recorded))
        return [item[0] for item in rows]

    def summary(self) -> dict[str, object]:
        """Read the most recently recorded source snapshot per instrument."""
        if not self.path.exists():
            return {"status": "missing", "store_version": STORE_VERSION}
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            row = connection.execute("""
                WITH snapshots AS (
                    SELECT instrument_id, source_snapshot_id,
                           MAX(recorded_at) AS last_recorded
                    FROM factor_observations
                    GROUP BY instrument_id, source_snapshot_id
                ), latest AS (
                    SELECT instrument_id, source_snapshot_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY instrument_id
                               ORDER BY last_recorded DESC, source_snapshot_id DESC
                           ) AS rank
                    FROM snapshots
                )
                SELECT COUNT(*), COUNT(DISTINCT f.instrument_id), MAX(f.asof_date),
                       SUM(CASE WHEN f.status='ok' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN f.status='missing_input' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN f.availability_evidence='verified_first_available'
                                THEN 1 ELSE 0 END)
                FROM factor_observations f
                JOIN latest l ON f.instrument_id=l.instrument_id
                             AND f.source_snapshot_id=l.source_snapshot_id
                             AND l.rank=1
                """).fetchone()
            total = connection.execute("SELECT COUNT(*) FROM factor_observations").fetchone()[0]
        return {"status": "ready", "store_version": STORE_VERSION,
                "latest_snapshot_observation_count": row[0],
                "instrument_count": row[1], "latest_asof": row[2],
                "latest_snapshot_valid_count": row[3] or 0,
                "latest_snapshot_missing_input_count": row[4] or 0,
                "latest_snapshot_evidenced_availability_count": row[5] or 0,
                "all_vintage_observation_count": total}

    def analysis_insights(self, instrument_id: str, *, asof: date,
                          limit: int = 12) -> list[dict[str, object]]:
        """Latest and preceding valid observation per definition and data basis."""
        if not self.path.exists():
            return []
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            rows = connection.execute("""
                SELECT payload_json FROM factor_observations
                WHERE instrument_id=? AND asof_date<=?
                ORDER BY asof_date DESC, recorded_at DESC, observation_id DESC
                """, (instrument_id, asof.isoformat())).fetchall()
        groups: dict[tuple[str, str, str, str], list[dict]] = {}
        for (serialized,) in rows:
            item = json.loads(serialized)
            key = (item["factor_id"], item["factor_version"], item["parameters_json"],
                   item["price_basis"])
            group = groups.setdefault(key, [])
            if group and (item["source_snapshot_id"] != group[0]["source_snapshot_id"]
                          or item["source_id"] != group[0]["source_id"]):
                continue
            if len(group) < 2 and not any(old["asof_date"] == item["asof_date"] for old in group):
                group.append(item)
        insights = []
        for group in groups.values():
            latest = group[0]
            preceding = group[1] if len(group) > 1 else None
            insights.append({
                "factor_id": latest["factor_id"], "version": latest["factor_version"],
                "parameters": json.loads(latest["parameters_json"]),
                "asof": latest["asof_date"], "value": latest["value"],
                "status": latest["status"],
                "previous_asof": preceding["asof_date"] if preceding else None,
                "previous_value": preceding["value"] if preceding else None,
                "change": (latest["value"] - preceding["value"]
                           if preceding and latest["value"] is not None
                           and preceding["value"] is not None else None),
                "unit": latest["output_unit"], "price_basis": latest["price_basis"],
                "source_id": latest["source_id"],
                "source_snapshot_id": latest["source_snapshot_id"],
                "availability_evidence": latest["availability_evidence"],
                "input_sha256": latest["input_sha256"],
            })
        insights.sort(key=lambda item: (item["factor_id"], _canonical(item["parameters"])))
        return insights[:limit]

    def decision_snapshot(self, instrument_ids: Sequence[str],
                          specifications: Sequence[tuple[str, str, dict]], *,
                          asof: date, decision_at: datetime,
                          source_snapshot_id: str, price_basis: str) -> dict:
        """Fail closed unless every requested factor is point-in-time eligible."""
        decision = _timestamp(decision_at, "decision_at")
        if (not instrument_ids or len(set(instrument_ids)) != len(instrument_ids)
                or not specifications or not source_snapshot_id or not price_basis):
            raise ValueError("incomplete factor decision request")
        if not self.path.exists():
            raise ValueError("factor observation store missing")
        values: dict[str, dict[str, float]] = {}
        lineage: dict[str, dict[str, str]] = {}
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            for instrument_id in instrument_ids:
                current = {}
                current_lineage = {}
                for factor_id, version, parameters in specifications:
                    label = factor_id + ":" + _hash(parameters)[:12]
                    rows = connection.execute("""
                        SELECT payload_json FROM factor_observations
                        WHERE instrument_id=? AND asof_date=? AND factor_id=?
                          AND factor_version=? AND parameters_json=?
                          AND source_snapshot_id=? AND price_basis=?
                        """, (instrument_id, asof.isoformat(), factor_id, version,
                              _canonical(parameters), source_snapshot_id, price_basis)).fetchall()
                    eligible = []
                    for (serialized,) in rows:
                        item = json.loads(serialized)
                        if (item["status"] == "ok"
                                and item["availability_evidence"] == "verified_first_available"
                                and item.get("availability_evidence_id")
                                and item["first_available_at"] <= decision
                                and item["source_captured_at"] <= decision):
                            eligible.append(item)
                    if len(eligible) != 1:
                        raise ValueError(f"missing or ambiguous point-in-time factor: {instrument_id} {label}")
                    current[label] = eligible[0]["value"]
                    current_lineage[label] = eligible[0]["input_sha256"]
                values[instrument_id] = current
                lineage[instrument_id] = current_lineage
        return {"asof": asof.isoformat(), "decision_at": decision,
                "source_snapshot_id": source_snapshot_id, "price_basis": price_basis,
                "values": values, "input_sha256": _hash(lineage),
                "store_version": STORE_VERSION}
