"""Human decision journal and outcome review for screened market directions."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Mapping, Optional
from uuid import uuid4

from quant_lab.market import GROUPS
from quant_lab.storage import MarketStore

SCREEN_VERSION = "group-observation-v1"


def screen_groups(market: Mapping[str, object], run_id: str,
                  validation: Optional[Mapping[str, object]] = None) -> list[dict[str, object]]:
    """Select relative leaders for human review, without asserting a buy signal."""
    leaders = [row for row in market["groups"]
               if row["name"] != "其他（综合）" and row["returns"]["5d"] > 0][:3]
    stale = (market["industry_asof"] != market["asof"] or
             date.fromisoformat(market["industry_asof"]) < date.today() - timedelta(days=3))
    no_validated_edge = bool(validation and validation.get("selected_origin_count") and
                             (validation.get("mean_excess_vs_equal_weight") is None or
                              validation["mean_excess_vs_equal_weight"] <= 0))
    candidates = []
    for rank, row in enumerate(leaders, 1):
        candidates.append({
            "candidate_id": sha256(f"{run_id}:{row['name']}".encode()).hexdigest()[:16],
            "run_id": run_id,
            "screen_version": SCREEN_VERSION,
            "asof": market["industry_asof"],
            "group": row["name"],
            "member_codes": list(GROUPS[row["name"]]),
            "rank": rank,
            "status": ("等待行业数据更新" if stale else
                       "仅观察，历史优势未验证" if no_validated_edge else "待人工复核"),
            "reason": (f"过去5个交易日类内指数平均涨跌 {row['returns']['5d']:+.2%}；"
                       f"最近交易日 {row['up_1d']}/{row['member_count']} 个细分行业上涨"),
            "five_day_return": row["returns"]["5d"],
            "up_1d": row["up_1d"],
            "member_count": row["member_count"],
            "share_change_pp": row.get("share_change_pp"),
            "note": ("这是待观察的行业方向，不是买入建议；"
                     + ("当前历史检验未显示相对等权基线优势；" if no_validated_edge else "")
                     + "行情日期见 asof。"),
        })
    return candidates


def save_candidates(store: MarketStore, candidates: list[dict[str, object]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with store.connect() as connection:
        connection.executemany(
            """INSERT INTO screen_candidates(candidate_id,run_id,asof_date,group_name,rank,metrics_json,status,created_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO NOTHING""",
            [(item["candidate_id"], item["run_id"], item["asof"], item["group"], item["rank"],
              json.dumps(item, ensure_ascii=False, sort_keys=True), item["status"], now)
             for item in candidates],
        )


def save_stock_candidates(store: MarketStore, screen: Mapping[str, object],
                          run_id: str) -> list[dict[str, object]]:
    """Persist publishable stock candidates with their exact screening evidence."""
    if screen.get("status") != "ready":
        return []
    now = datetime.now(timezone.utc).isoformat()
    candidates = []
    for item in screen["candidates"]:
        enriched = dict(item)
        enriched.update({
            "candidate_id": sha256(f"{run_id}:{item['instrument_id']}".encode()).hexdigest()[:16],
            "run_id": run_id,
            "subject_type": "stock",
            "screen_version": screen["screen_version"],
        })
        candidates.append(enriched)
    with store.connect() as connection:
        connection.executemany(
            """INSERT INTO screen_candidates(
                candidate_id,run_id,asof_date,group_name,rank,metrics_json,status,
                subject_type,instrument_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO NOTHING""",
            [(item["candidate_id"], item["run_id"], item["asof"], item["name"], item["rank"],
              json.dumps(item, ensure_ascii=False, sort_keys=True), item["status"],
              "stock", item["instrument_id"], now) for item in candidates],
        )
    return candidates


def record_decision(store: MarketStore, candidate_id: str, choice: str,
                    note: str = "") -> str:
    if choice not in ("watch", "act", "skip"):
        raise ValueError("choice must be watch, act or skip")
    with store.connect() as connection:
        row = connection.execute("SELECT candidate_id FROM screen_candidates WHERE candidate_id=?",
                                 (candidate_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown candidate_id: {candidate_id}")
        existing = connection.execute("SELECT decision_id FROM user_decisions WHERE candidate_id=?",
                                      (candidate_id,)).fetchone()
        decision_id = existing[0] if existing else uuid4().hex
        connection.execute(
            """INSERT INTO user_decisions(decision_id,candidate_id,choice,note,decided_at)
            VALUES(?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET
            choice=excluded.choice,note=excluded.note,decided_at=excluded.decided_at""",
            (decision_id, candidate_id, choice, note, datetime.now(timezone.utc).isoformat()),
        )
    return decision_id


def _forward_group_return(store: MarketStore, member_codes: list[str], signal_day: date,
                          horizon: int) -> Optional[float]:
    ids = [f"industry:{code}.SW" for code in member_codes]
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT instrument_id,trade_date,close FROM daily_bars WHERE instrument_id IN (" +
            ",".join("?" for _ in ids) + ") AND trade_date>=? ORDER BY trade_date",
            [*ids, signal_day.isoformat()],
        ).fetchall()
    by_id: dict[str, dict[str, float]] = {key: {} for key in ids}
    for row in rows:
        by_id[row["instrument_id"]][row["trade_date"]] = float(row["close"])
    dates = sorted(set.intersection(*(set(values) for values in by_id.values())))
    if not dates or dates[0] != signal_day.isoformat() or len(dates) <= horizon:
        return None
    start, end = dates[0], dates[horizon]
    return sum(by_id[key][end] / by_id[key][start] - 1 for key in ids) / len(ids)


def _forward_stock_return(store: MarketStore, instrument_id: str, signal_day: date,
                          horizon: int) -> Optional[float]:
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT trade_date,close FROM daily_bars
               WHERE instrument_id=? AND trade_date>=? ORDER BY trade_date LIMIT ?""",
            (instrument_id, signal_day.isoformat(), horizon + 1),
        ).fetchall()
    if len(rows) <= horizon or rows[0]["trade_date"] != signal_day.isoformat():
        return None
    return float(rows[horizon]["close"]) / float(rows[0]["close"]) - 1


def review_decisions(store: MarketStore) -> dict[str, object]:
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT d.decision_id,d.candidate_id,d.choice,d.note,d.decided_at,
                      c.group_name,c.asof_date,c.run_id,c.status,c.metrics_json,
                      c.subject_type,c.instrument_id
               FROM user_decisions d JOIN screen_candidates c USING(candidate_id)
               ORDER BY d.decided_at"""
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        evidence = json.loads(item.pop("metrics_json"))
        signal_day = date.fromisoformat(item["asof_date"])
        if item["subject_type"] == "stock":
            item["return_5d"] = _forward_stock_return(store, item["instrument_id"], signal_day, 5)
            item["return_20d"] = _forward_stock_return(store, item["instrument_id"], signal_day, 20)
        else:
            member_codes = evidence["member_codes"]
            item["member_codes"] = member_codes
            item["return_5d"] = _forward_group_return(store, member_codes, signal_day, 5)
            item["return_20d"] = _forward_group_return(store, member_codes, signal_day, 20)
        items.append(item)
    matured = [item for item in items if item["return_20d"] is not None]
    by_type = {}
    for subject_type in ("group", "stock"):
        by_choice = {}
        for choice in ("watch", "act", "skip"):
            values = [item["return_20d"] for item in matured
                      if item["choice"] == choice and item["subject_type"] == subject_type]
            by_choice[choice] = {"samples": len(values),
                                 "mean_20d_return": sum(values) / len(values) if values else None}
        by_type[subject_type] = by_choice
    return {
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "decision_count": len(items),
        "matured_20d_count": len(matured),
        "by_type": by_type,
        "items": items,
        "optimization": ("样本不足：至少20条已满20交易日的决定后，再比较筛选规则；当前不自动调整规则。"
                         if len(matured) < 20 else
                         "样本已达初步门槛；需做时间外验证后才调整下一版规则。"),
        "outcome_definition": "行业为组内申万指数等权收益；股票为信号日到未来第N个有价交易日的收盘收益。均非个人账户盈亏",
    }
