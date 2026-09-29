"""Data-source layer.

Reads raw data — market bars, the macro country dataset, GICS sector returns,
and the key-actors/events feed — and exposes it as plain Python dicts. No
analysis happens here; that lives in :mod:`analytics`.

Today bars come from a CSV and macro/sector/events are curated constants in
:mod:`auto_invest.apps.economy_dashboard`. To plug in a real data feed later,
reimplement these functions and nothing downstream needs to change.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from auto_invest.apps.economy_dashboard import (
    COUNTRIES,
    NAME_BY_ID,
    SECTOR_YEARS,
    SECTORS,
    country_payload,
)

__all__ = [
    "COUNTRIES", "INDUSTRY_BY_ID", "KEY_ACTORS", "KEY_EVENTS",
    "read_bars", "list_instruments", "instrument_name", "instrument_industry",
    "list_countries", "country", "macro_years", "compare_metric",
    "sectors", "sector_years", "actors_and_events",
]

# Industry / sector mapping for instruments in the sample universe.
# Extend as the universe grows (or replace with a real classification source).
INDUSTRY_BY_ID = {
    "CN_A:000001.SZ": "金融",
    "HK:00700.HK": "科技·互联网",
    "US:AAPL": "科技·硬件",
    "CRYPTO:BTC-USD": "数字资产",
}

# Key actors + events timeline (illustrative; swap for a real event feed later).
KEY_ACTORS = [
    {"name": "美联储 (Fed)", "role": "货币政策", "note": "通过联邦基金利率影响全球流动性与风险偏好"},
    {"name": "美国财政部 / 国会", "role": "财政政策", "note": "赤字、发债与关税直接作用于增长与通胀"},
    {"name": "BLS / BEA", "role": "数据发布方", "note": "CPI、就业、GDP 发布日是市场的关键事件节点"},
]
KEY_EVENTS = [
    {"year": 2020, "type": "冲击", "title": "疫情衰退", "detail": "GDP -2.2%,Fed 紧急降息至零利率"},
    {"year": 2021, "type": "复苏", "title": "强复苏 + 通胀抬头", "detail": "GDP +5.9%,CPI 升至 7.0%"},
    {"year": 2022, "type": "拐点", "title": "激进加息周期", "detail": "政策利率年内升至约 4.1%,压制通胀"},
    {"year": 2023, "type": "高位", "title": "利率见顶", "detail": "政策利率约 5.33%,为金融危机以来最高"},
    {"year": 2024, "type": "转向", "title": "开启降息", "detail": "通胀回落,Fed 年内多次降息"},
    {"year": 2025, "type": "软着陆", "title": "软着陆", "detail": "GDP +2.1%,CPI 2.7%,失业率 4.2%"},
]


def read_bars(bars_path: Path) -> Dict[str, List[dict]]:
    """Read bars CSV into {instrument_id: [{date, open, high, low, close, volume}]}."""
    rows_by_id: Dict[str, List[dict]] = defaultdict(list)
    with bars_path.open("r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            rows_by_id[row["instrument_id"]].append(
                {
                    "date": ts.date().isoformat(),
                    "close": float(row["close"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "open": float(row["open"]),
                    "volume": float(row["volume"]),
                }
            )
    for series in rows_by_id.values():
        series.sort(key=lambda r: r["date"])
    return rows_by_id


def list_instruments(rows_by_id: Dict[str, List[dict]]) -> List[dict]:
    return [{"id": iid, "name": NAME_BY_ID.get(iid, iid)} for iid in sorted(rows_by_id)]


def instrument_name(instrument_id: str) -> str:
    return NAME_BY_ID.get(instrument_id, instrument_id)


def instrument_industry(instrument_id: str) -> str:
    return INDUSTRY_BY_ID.get(instrument_id, "未分类")


def list_countries() -> List[dict]:
    return [
        {"code": code, "name": c["name"], "flag": c["flag"]}
        for code, c in COUNTRIES.items()
    ]


def country(code: str) -> dict:
    """Macro + KPI payload for one country code. Raises KeyError if unknown."""
    if code not in COUNTRIES:
        raise KeyError(code)
    return country_payload(code)


def macro_years() -> List[int]:
    return COUNTRIES["US"]["years"]


def compare_metric(metric: str) -> dict:
    """Cross-country series for one macro metric."""
    valid = {"gdp_growth", "inflation", "unemployment", "policy_rate"}
    if metric not in valid:
        raise ValueError(metric)
    return {
        "metric": metric,
        "years": macro_years(),
        "series": [
            {"code": code, "name": c["name"], "flag": c["flag"], "values": c[metric]}
            for code, c in COUNTRIES.items()
        ],
    }


def sectors() -> Dict[str, List[float]]:
    return dict(SECTORS)


def sector_years() -> List[int]:
    return list(SECTOR_YEARS)


def actors_and_events() -> dict:
    return {"actors": KEY_ACTORS, "events": KEY_EVENTS}
