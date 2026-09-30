"""Source-labelled events for one stock's displayed price window.

Event time, effective time and the trading-day chart anchor stay separate.
The index layer includes only independently audited official changes; an
archived index membership mask is not treated as official daily history.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from quant_lab.event_study import load_event_registry


TIMELINE_VERSION = "stock-analysis-timeline-v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_REGISTRY = REPO_ROOT / "data/quant/reviewed-policy-events.json"
INDEX_AUDIT = REPO_ROOT / "studies/random-entry-baseline-v1/csi300-official-2021-06-delta-audit-v1.json"
INDEX_EVENTS = REPO_ROOT / "data/quant/reviewed-index-events.json"
COMPANY_DISCLOSURES = REPO_ROOT / "data/quant/reviewed-company-disclosures.json"


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _anchor(days: list[str], event_day: str, *, after_close: bool) -> str | None:
    position = bisect.bisect_right(days, event_day) if after_close else bisect.bisect_left(days, event_day)
    return days[position] if position < len(days) else None


def _event(event_id: str, category: str, title: str, event_at: str,
           event_time_kind: str, anchor_date: str | None, *,
           detail: str, source_id: str, source_url: str | None = None,
           source_sha256: str | None = None, input_sha256: str | None = None,
           effective_at: str | None = None,
           availability: str = "reviewed") -> dict[str, Any]:
    return {"event_id": event_id, "category": category, "title": title,
            "event_at": event_at, "event_time_kind": event_time_kind,
            "anchor_date": anchor_date, "effective_at": effective_at,
            "detail": detail, "source_id": source_id, "source_url": source_url,
            "source_sha256": source_sha256, "input_sha256": input_sha256,
            "availability": availability}


def build_analysis_timeline(
    report: dict[str, Any], *, policy_registry: Path = POLICY_REGISTRY,
    index_audit: Path = INDEX_AUDIT,
    index_events: Path = INDEX_EVENTS,
    company_disclosures: Path = COMPANY_DISCLOSURES,
) -> dict[str, Any]:
    """Build bounded research annotations using only the report's chart dates."""
    chart = report.get("chart") or {}
    history = chart.get("history") or []
    days = [point["date"] for point in history if isinstance(point, dict) and isinstance(point.get("date"), str)]
    if not days or days != sorted(set(days)):
        return {"version": TIMELINE_VERSION, "events": [], "layers": [],
                "status": "price_window_unavailable"}
    start, end = days[0], days[-1]
    events: list[dict[str, Any]] = []
    layers: list[dict[str, Any]] = []

    factors = report.get("factor_insights") or []
    for item in factors:
        observed = item.get("asof")
        if not isinstance(observed, str) or not start <= observed <= end:
            continue
        if item.get("status") != "ok":
            continue
        anchor = _anchor(days, observed, after_close=False)
        if anchor != observed:
            continue
        factor_id = item.get("factor_id")
        if not isinstance(factor_id, str):
            continue
        event_id = "factor:" + ":".join((factor_id, observed, str(item.get("input_sha256", ""))[:12]))
        events.append(_event(event_id, "factor", f"{factor_id} 因子观测",
                             observed, "observation_date", anchor,
                             detail=f"数值 {item.get('value')}；相对上次变化 {item.get('change')}。观测日不等于首次可得时间。",
                             source_id=str(item.get("source_id", "unknown")),
                             input_sha256=item.get("input_sha256"),
                             availability=str(item.get("availability_evidence", "unknown"))))
    layers.append({"category": "factor", "label": "特征因子", "status": "available" if factors else "no_observations",
                   "coverage": "当前报告的已保存观测；观测日期不代表当时已披露"})

    try:
        policy_events = load_event_registry(policy_registry)
        policy_hash = _source_hash(policy_registry)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        policy_events = ()
        policy_hash = None
    for item in policy_events:
        moment = item.announcement_at
        announced = moment.isoformat()
        announced_day = item.announcement_day.isoformat()
        if not start <= announced_day <= end:
            continue
        after_close = (isinstance(moment, datetime) and moment.timetz().replace(tzinfo=None) >= time(15)) \
            or not isinstance(moment, datetime)
        anchor = _anchor(days, announced_day, after_close=after_close)
        events.append(_event(item.event_id, "policy", item.title, announced,
                             "announcement_time" if isinstance(moment, datetime) else "announcement_date",
                             anchor, detail="宏观政策公告；不代表该股因果反应。",
                             source_id="reviewed_policy_events", source_url=item.source_url,
                             source_sha256=policy_hash,
                             effective_at=item.effective_date.isoformat() if item.effective_date else None,
                             availability="official_reviewed"))
    layers.append({"category": "policy", "label": "政策公告", "status": "reviewed_subset" if policy_hash else "source_unavailable",
                   "coverage": "人工审核清单，仅包含部分宏观政策公告"})

    index_status = "source_unavailable"
    try:
        audit = json.loads(index_audit.read_text(encoding="utf-8"))
        if (audit.get("study_version") != "csi300-official-2021-06-delta-audit-v1"
                or audit.get("two_attachment_versions_identical_for_csi300") is not True
                or audit.get("full_daily_pit_universe_confirmed") is not False):
            raise ValueError("index audit lacks required source assertions")
        for attachment in audit["attachment_sources"].values():
            relative = Path(attachment["relative_path"])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts[:2] == ("studies", "random-entry-baseline-v1"):
                raise ValueError("index attachment path outside audited studies")
            if _source_hash(REPO_ROOT / relative) != attachment["sha256"]:
                raise ValueError("index attachment hash differs from audit")
        source_hash = _source_hash(index_audit)
        index_status = "audited_single_adjustment"
        symbol = str(report.get("instrument_id", "")).removeprefix("stock:")
        compact = (symbol[-2:] + symbol[:6]) if len(symbol) == 9 and symbol[6] == "." else ""
        for direction, verb in (("调入", "纳入"), ("调出", "移出")):
            for member in audit["official_change"][direction]:
                if member.get("symbol") != compact:
                    continue
                effective = audit["monthly_release_mismatch"]["official_effective_after_close"]
                first_trade = audit["monthly_release_mismatch"]["official_first_effective_trading_date"]
                # The archive confirms the announcement date and effective date
                # for this single scheduled adjustment, not a complete history.
                anchor = first_trade if first_trade in days else None
                if start <= effective <= end or anchor:
                    events.append(_event(
                        f"csi300:2021-06:{compact}:{direction}", "index",
                        f"沪深 300 {verb} {member['name']}", "2021-05-28",
                        "announcement_date", anchor,
                        detail=f"官方调样；{effective} 收盘后生效，首个生效交易日 {first_trade}。此审计只覆盖 2021 年 6 月这一轮。",
                        source_id="csi300_official_2021_06_delta_audit",
                        source_url=audit.get("notice_url"), source_sha256=source_hash,
                        effective_at=effective + " 收盘后", availability="official_single_adjustment"))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        index_status = "source_unavailable"
    index_2021_verified = index_status == "audited_single_adjustment"
    index_2026_verified = False
    try:
        registry = json.loads(index_events.read_text(encoding="utf-8"))
        if (registry.get("schema_version") != 1 or not isinstance(registry.get("events"), list)
                or not registry["events"]):
            raise ValueError("index event registry version differs")
        prepared = []
        seen_ids: set[str] = set()
        for item in registry["events"]:
            event_id, instrument, announcement, effective = (item[key] for key in (
                "event_id", "instrument_id", "announcement_date", "effective_after_close_date"))
            relative = Path(item["document_relative_path"])
            document_url = urlsplit(item["document_url"])
            notice_url = urlsplit(item["notice_url"])
            if (not isinstance(event_id, str) or not event_id or event_id in seen_ids
                    or not isinstance(instrument, str)
                    or re.fullmatch(r"stock:\d{6}\.(SH|SZ)", instrument) is None
                    or not isinstance(announcement, str)
                    or date.fromisoformat(announcement).isoformat() != announcement
                    or not isinstance(effective, str)
                    or date.fromisoformat(effective).isoformat() != effective
                    or announcement > effective
                    or relative.is_absolute() or ".." in relative.parts
                    or relative.parts[:3] != ("studies", "random-entry-baseline-v1", "official-sources")
                    or document_url.scheme != "https" or document_url.hostname != "oss-ch.csindex.com.cn"
                    or notice_url.scheme != "https" or notice_url.hostname != "www.csindex.com.cn"
                    or item.get("direction") not in ("in", "out")
                    or not isinstance(item.get("title"), str) or not item["title"]
                    or not isinstance(item.get("source_id"), str) or not item["source_id"]
                    or not isinstance(item.get("document_row"), str)
                    or instrument[6:12] not in item["document_row"]
                    or not isinstance(item.get("document_page_one_based"), int)
                    or item["document_page_one_based"] < 1
                    or not isinstance(item.get("document_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", item["document_sha256"]) is None
                    or _source_hash(REPO_ROOT / relative) != item["document_sha256"]):
                raise ValueError("invalid reviewed index event")
            seen_ids.add(event_id)
            prepared.append(item)
        registry_hash = _source_hash(index_events)
        for item in prepared:
            if item["instrument_id"] != report.get("instrument_id"):
                continue
            announced, effective = item["announcement_date"], item["effective_after_close_date"]
            anchor = _anchor(days, effective, after_close=True)
            if not (start <= effective <= end or anchor and start <= anchor <= end):
                continue
            events.append(_event(
                item["event_id"], "index", item["title"], announced,
                "announcement_date", anchor,
                detail=(f"官方调样附件第 {item['document_page_one_based']} 页核实；"
                        "公告仅有日期精度，指数于所列日期收盘后生效。此清单只包含已逐行核实的个别变化。"),
                source_id=item["source_id"], source_url=item["document_url"],
                source_sha256=registry_hash, input_sha256=item["document_sha256"],
                effective_at=effective + " 收盘后", availability="official_single_adjustment"))
        index_2026_verified = True
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    if index_2026_verified:
        index_status = "audited_selected_adjustments"
    if index_2021_verified and index_2026_verified:
        index_coverage = "仅核实沪深 300 的 2021 年 6 月调样及中证 A50 的 2026 年 6 月洛阳钼业调入；其他变化暂无完整官方历史"
    elif index_2021_verified:
        index_coverage = "仅核实沪深 300 的 2021 年 6 月调样；其他变化暂无完整官方历史"
    elif index_2026_verified:
        index_coverage = "仅核实中证 A50 的 2026 年 6 月洛阳钼业调入；其他变化暂无完整官方历史"
    else:
        index_coverage = "官方指数调样审计源暂不可读取"
    layers.append({"category": "index", "label": "中证指数成分变化", "status": index_status,
                   "coverage": index_coverage})
    disclosure_status = "source_unavailable"
    try:
        registry = json.loads(company_disclosures.read_text(encoding="utf-8"))
        if registry.get("schema_version") != 1 or not isinstance(registry.get("events"), list):
            raise ValueError("company disclosure registry version differs")
        seen_ids: set[str] = set()
        prepared = []
        for item in registry["events"]:
            event_id, instrument, day, url = (item[key] for key in (
                "event_id", "instrument_id", "disclosure_date", "document_url"))
            if (not isinstance(event_id, str) or event_id in seen_ids
                    or not isinstance(instrument, str)
                    or re.fullmatch(r"stock:\d{6}\.(SH|SZ)", instrument) is None
                    or not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
                    or not isinstance(url, str)
                    or urlsplit(url).scheme != "https"
                    or urlsplit(url).hostname not in ("disc.static.szse.cn", "www.sse.com.cn", "static.sse.com.cn")
                    or not isinstance(item.get("title"), str) or not item["title"]
                    or not isinstance(item.get("source_id"), str) or not item["source_id"]
                    or item.get("release_time_verified") is not False
                    or item.get("document_identity_verified") is not True
                    or item.get("source_date_basis") not in (
                        "official_exchange_document_url_date",
                        "company_investor_relations_listing_date")):
                raise ValueError("invalid reviewed company disclosure")
            seen_ids.add(event_id)
            prepared.append(item)
        registry_hash = _source_hash(company_disclosures)
        disclosure_status = "curated_date_only_subset"
        for item in prepared:
            if item["instrument_id"] != report.get("instrument_id"):
                continue
            day = item["disclosure_date"]
            anchor = _anchor(days, day, after_close=True)
            if anchor is None or anchor < start:
                continue
            events.append(_event(
                item["event_id"], "disclosure", item["title"], day,
                "document_calendar_date", anchor,
                detail="定期报告原文已核对；仅有公开日历日期，精确披露时刻未核实。为避免同日信息先知，标在下一个已载入交易日。",
                source_id=item["source_id"], source_url=item["document_url"],
                source_sha256=registry_hash, availability="official_document_date_only"))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        disclosure_status = "source_unavailable"
    layers.append({"category": "disclosure", "label": "公司信息披露", "status": disclosure_status,
                   "coverage": "仅人工核实浪潮信息、比亚迪、洛阳钼业各一份 2026 年一季报和半年报；精确披露时间与其他公告尚未接入" if disclosure_status != "source_unavailable" else "公司公告登记源暂不可读取"})
    events.sort(key=lambda item: (item["anchor_date"] or item["event_at"], item["category"], item["event_id"]))
    return {"version": TIMELINE_VERSION, "status": "ready", "window": {"start": start, "end": end},
            "events": events, "layers": layers}
