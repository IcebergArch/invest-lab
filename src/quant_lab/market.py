"""EOD A-share market overview using broad and SW level-one indices."""
from __future__ import annotations

import json
from html import escape
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from quant_lab.flow_view import build_flow_view
from quant_lab.storage import MarketStore

# Reader-facing groups; every current SW level-one code appears exactly once.
GROUPS: dict[str, tuple[str, ...]] = {
    "资源能源": ("801030", "801040", "801050", "801950", "801960"),
    "制造建设": ("801710", "801720", "801730", "801740", "801880", "801890"),
    "科技信息": ("801080", "801750", "801760", "801770"),
    "消费健康": ("801010", "801110", "801120", "801130", "801140", "801150", "801200", "801210", "801980"),
    "金融与公共服务": ("801160", "801170", "801180", "801780", "801790", "801970"),
    "其他（综合）": ("801230",),
}
BROAD = {
    "index:000300.SH": "沪深300",
    "index:000905.SH": "中证500",
    "index:000852.SH": "中证1000",
    "index:000001.SH": "上证指数",
    "index:000688.SH": "科创50",
}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _returns(closes: list[float]) -> dict[str, Optional[float]]:
    return {f"{n}d": closes[-1] / closes[-n - 1] - 1 if len(closes) > n else None
            for n in (1, 5, 20)}


def build_market_pulse(store: MarketStore, asof: Optional[date] = None) -> dict[str, object]:
    group_codes = {code for codes in GROUPS.values() for code in codes}
    if sum(map(len, GROUPS.values())) != len(group_codes):
        raise ValueError("SW group mapping has duplicate codes")
    with store.connect() as connection:
        industry_meta = {row["instrument_id"].split(":")[1].split(".")[0]:
                         (row["instrument_id"], row["name"])
                         for row in connection.execute(
                             "SELECT instrument_id,name FROM instruments WHERE family='sw_level_1'")}
        if set(industry_meta) != group_codes:
            raise ValueError(f"SW group mapping mismatch: missing={sorted(set(industry_meta)-group_codes)}, "
                             f"unknown={sorted(group_codes-set(industry_meta))}")
        ids = [*BROAD, *(item[0] for item in industry_meta.values())]
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT instrument_id,trade_date,close,amount,amount_unit,source_id,payload_hash,run_id "
            f"FROM daily_bars WHERE instrument_id IN ({placeholders}) "
            + ("AND trade_date<=? " if asof else "")
            + "ORDER BY trade_date,instrument_id",
            [*ids, asof.isoformat()] if asof else ids,
        ).fetchall()
    by_id: dict[str, dict[str, dict[str, object]]] = {item: {} for item in ids}
    for row in rows:
        by_id[row["instrument_id"]][row["trade_date"]] = dict(row)
    empty = [key for key, values in by_id.items() if not values]
    if empty:
        raise ValueError(f"missing market data: {empty}")
    industry_ids = [item[0] for item in industry_meta.values()]
    dates = sorted(set.intersection(*(set(by_id[key]) for key in industry_ids)))
    broad_dates = sorted(set.intersection(*(set(by_id[key]) for key in BROAD)))
    if len(dates) < 21 or len(broad_dates) < 21:
        raise ValueError("market pulse needs at least 21 common trading days")
    day = dates[-1]
    broad_day = broad_dates[-1]
    # Source-native amounts are used only as dimensionless shares and ratios.
    amount_usable = all(
        by_id[key][d]["amount"] is not None
        and float(by_id[key][d]["amount"]) > 0
        and by_id[key][d]["amount_unit"] == "source_native"
        and by_id[key][d]["source_id"] == "sw_research"
        for key in industry_ids for d in dates[-21:]
    )
    shares: dict[str, float] = {}
    prior_shares: dict[str, float] = {}
    if amount_usable:
        for d in dates[-21:]:
            total = sum(float(by_id[key][d]["amount"]) for key in industry_ids)
            for key in industry_ids:
                share = float(by_id[key][d]["amount"]) / total
                if d == day:
                    shares[key] = share
                else:
                    prior_shares[key] = prior_shares.get(key, 0.0) + share / 20
    industries = []
    for code, (key, name) in sorted(industry_meta.items()):
        result = {"code": code, "name": name, "group": next(g for g, codes in GROUPS.items() if code in codes),
                  "returns": _returns([float(by_id[key][d]["close"]) for d in dates]),
                  "source_id": by_id[key][day]["source_id"],
                  "payload_hash": by_id[key][day]["payload_hash"],
                  "run_id": by_id[key][day]["run_id"]}
        if amount_usable:
            amount = float(by_id[key][day]["amount"])
            prior = _mean([float(by_id[key][d]["amount"]) for d in dates[-21:-1]])
            result.update({"activity_ratio": amount / prior,
                           "amount_share": shares[key],
                           "share_change_pp": (shares[key] - prior_shares[key]) * 100})
        industries.append(result)
    groups = []
    for name, codes in GROUPS.items():
        members = [row for row in industries if row["code"] in codes]
        entry = {"name": name, "member_count": len(members),
                 "returns": {h: _mean([row["returns"][h] for row in members])
                             for h in ("1d", "5d", "20d")},
                 "up_1d": sum(row["returns"]["1d"] > 0 for row in members),
                 "leaders_5d": [row["name"] for row in sorted(members,
                                   key=lambda row: row["returns"]["5d"], reverse=True)[:2]]}
        if amount_usable:
            entry.update({"amount_share": sum(shares[industry_meta[code][0]] for code in codes),
                          "share_change_pp": sum((shares[industry_meta[code][0]] -
                                                  prior_shares[industry_meta[code][0]]) * 100
                                                 for code in codes),
                          "activity_ratio": sum(float(by_id[industry_meta[code][0]][day]["amount"])
                                                for code in codes) /
                          _mean([sum(float(by_id[industry_meta[code][0]][d]["amount"])
                                     for code in codes) for d in dates[-21:-1]])})
        groups.append(entry)
    groups.sort(key=lambda row: row["returns"]["5d"], reverse=True)
    broad = [{"name": name, "instrument_id": key,
              "returns": _returns([float(by_id[key][d]["close"]) for d in broad_dates]),
              "source_id": by_id[key][broad_day]["source_id"],
              "run_id": by_id[key][broad_day]["run_id"]} for key, name in BROAD.items()]
    up = sum(row["returns"]["1d"] > 0 for row in industries)
    return {"asof": broad_day, "industry_asof": day,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "industry_common_days": len(dates), "broad_common_days": len(broad_dates),
            "industry_breadth": {"up": up, "down": 31-up, "total": 31},
            "broad": broad, "groups": groups, "industries": industries,
            "activity_available": amount_usable,
            "net_flow_available": False,
            "definitions": {
                "group_return": "member SW level-one index returns, equally weighted; not an investable index",
                "amount_share": "SW industry published amount divided by the sum of 31 SW industry amounts on the same day",
                "share_change_pp": "today share minus mean of previous 20 common trading-day shares, percentage points",
                "activity_ratio": "today SW industry amount divided by mean amount of previous 20 common trading days",
                "net_flow": "unavailable; traded amount and amount-share changes do not measure net fund inflow",
                "breadth": "count of rising SW level-one indices, not count of rising A-shares",
            },
            "limitations": ["SW amount source-native unit has not been normalized; only ratios and shares are reported.",
                            "SW index amounts may not equal exchange-wide A-share turnover.",
                            "Broad-index source lacks traded amount; market-wide turnover cannot be computed from it."]}


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:+.2%}"


def render_market_markdown(report: dict[str, object], stock_screen: Optional[dict[str, object]] = None,
                           industry_validation: Optional[dict[str, object]] = None,
                           research_validation: Optional[dict[str, object]] = None,
                           forecast_validation: Optional[dict[str, object]] = None) -> str:
    flow = build_flow_view(report)
    lines = ["# A 股市场脉搏", "", "## 一分钟看懂", "",
             *(f"- {line}" for line in flow["reader_lines"]), "",
             f"宽基截至 **{report['asof']}**；申万行业截至 **{report['industry_asof']}**。两个日期不同，不能把行业轮动解释为宽基最新交易日的资金去向。",
             f"申万一级行业上涨 {report['industry_breadth']['up']}/31，下跌 {report['industry_breadth']['down']}/31。",
             "这里的宽度是行业指数数量，不是上涨股票数量。", "",
             "## 市场大势", "", "| 指数 | 1日 | 5日 | 20日 |", "| --- | ---: | ---: | ---: |"]
    for row in report["broad"]:
        r = row["returns"]
        lines.append(f"| {row['name']} | {_pct(r['1d'])} | {_pct(r['5d'])} | {_pct(r['20d'])} |")
    if stock_screen is not None:
        lines += ["", "## 股票筛选", ""]
        if stock_screen["candidate_count"]:
            for item in stock_screen["candidates"]:
                candidate_id = f"，回执 ID {item['candidate_id']}" if item.get("candidate_id") else ""
                lines.append(f"- {item['name']}（{item['instrument_id']}{candidate_id}）：{item['reason']}"
                             "历史涨幅不是预期收益；上涨概率尚无可靠估计。")
        else:
            lines.append(f"本次不发布股票候选：{stock_screen['reason']}")
        if stock_screen.get("universe_snapshot_id"):
            lines.append(
                f"当前清单：{stock_screen['universe_snapshot_scope']}，"
                f"清单 {stock_screen.get('expected_universe_count', 0)} 只，"
                f"本地已登记 {stock_screen['universe_count']} 只，"
                f"具备完整可用行情 {stock_screen.get('data_ready_count', 0)} 只；"
                f"快照 ID {stock_screen['universe_snapshot_id']}。"
            )
    if industry_validation is not None:
        count = industry_validation["selected_origin_count"]
        lines += ["", "## 行业方向的历史检验", "",
                  f"截至 {industry_validation['industry_data_asof']}，有候选的非重叠历史起点 {count} 次。"]
        if count:
            lines.append(
                f"随后5日平均表现为正的比例 {industry_validation['positive_future_rate']:.1%}；"
                f"相对五条主线等权平均每次差 {industry_validation['mean_excess_vs_equal_weight']*100:+.2f} 个百分点。"
            )
        lines.append("这是行业指数走势检验，不是可实现的交易收益；详见 industry-validation.md。")
    if research_validation is not None:
        metrics = research_validation["backtest"]["metrics"]
        lines += ["", "## 个股策略的试运行", "",
                  f"仅 {len(research_validation['universe'])} 只重点股：策略累计 {metrics['total_return']:+.1%}，"
                  f"最大回撤 {metrics['max_drawdown']:.1%}；独立时间外样本尚无。详见 backtest.md。"]
    if forecast_validation is not None:
        result = forecast_validation["pooled"].get("5", {})
        if result.get("status") == "ready":
            lines.append(f"20日动量外推在5日预测上的误差相对不变价基线改善 "
                         f"{result['mae_return_skill_vs_random_walk']:+.1%}；"
                         "模型结果不进入筛选。详见 forecast-momentum.md。")
    lines += ["", "## 五条主线 + 综合", "",
              "关系：资源能源 → 制造建设 → 消费健康；科技信息横跨生产和消费；金融与公共服务提供基础支持。这是帮助阅读的框架，不是因果模型。", "",
              "行业表现为类内申万一级指数等权收益，不代表可交易指数。",
              "成交份额变化与活跃度只表示成交关注度，不是资金净流入。", "",
              "| 大类 | 行业数 | 1日 | 5日 | 20日 | 1日上涨数 | 成交份额 | 较前20日均值 | 活跃度 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in report["groups"]:
        r = row["returns"]
        share = f"{row['amount_share']:.1%}" if report["activity_available"] else "—"
        delta = f"{row['share_change_pp']:+.2f}pp" if report["activity_available"] else "—"
        activity = f"{row['activity_ratio']:.2f}×" if report["activity_available"] else "—"
        lines.append(f"| {row['name']} | {row['member_count']} | {_pct(r['1d'])} | {_pct(r['5d'])} | "
                     f"{_pct(r['20d'])} | {row['up_1d']}/{row['member_count']} | {share} | {delta} | {activity} |")
    lines += ["", "## 细分行业：最近5日领先与落后", "",
              "| 行业 | 大类 | 5日 | 成交份额变化 |", "| --- | --- | ---: | ---: |"]
    ranked = sorted(report["industries"], key=lambda row: row["returns"]["5d"], reverse=True)
    for row in ranked[:5] + ranked[-5:]:
        delta = f"{row['share_change_pp']:+.2f}pp" if report["activity_available"] else "—"
        lines.append(f"| {row['name']} | {row['group']} | {_pct(row['returns']['5d'])} | {delta} |")
    lines += ["", "## 资金流向口径", "",
              "当前库没有可核验的买卖方向净流入数据，故不输出‘主力净流入’或‘资金流入某行业’结论。",
              "成交份额上升意味着该行业相对其他申万行业更活跃；买卖双方的成交额相等，不能据此判断净买入。",
              "", "## 你的使用节奏", "",
              "收盘后先看大盘和五条行业主线；次日 10:30 后复核变化，再由你决定是否人工交易。日线报告不能代替 10:30 的实时行情。",
              "", "## 数据与限制", "",
              f"- 宽基截至 {report['asof']}，行业截至 {report['industry_asof']}；仅用 31 个申万一级行业与 5 个宽基指数。",
              "- 行业指数：申万宏源研究；宽基历史行情：报告 JSON 中记录每条来源与同步批次。",
              "- 如截至日期早于今天，报告为历史观察，不代表当前盘面。",
              "- 这份报告是市场观察，不构成买卖建议。", ""]
    return "\n".join(lines)


def render_market_html(report: dict[str, object], candidates: Optional[list[dict[str, object]]] = None,
                       stock_screen: Optional[dict[str, object]] = None,
                       industry_validation: Optional[dict[str, object]] = None,
                       research_validation: Optional[dict[str, object]] = None,
                       forecast_validation: Optional[dict[str, object]] = None) -> str:
    """Readable single-page view; all conclusions stay tied to their data date."""
    broad = report["broad"]
    flow = build_flow_view(report)
    groups = report["groups"]
    breadth = report["industry_breadth"]
    falling = sum(row["returns"]["1d"] < 0 for row in broad)
    max_move = max(abs(row["returns"]["1d"] or 0) for row in broad) or 1
    broad_rows = []
    for row in broad:
        change = row["returns"]["1d"]
        width = max(3, round(abs(change or 0) / max_move * 100))
        tone = "down" if change < 0 else "up"
        broad_rows.append(
            f'<div class="index-row"><div class="index-name">{escape(row["name"])}</div>'
            f'<div class="track"><div class="bar {tone}" style="width:{width}%"></div></div>'
            f'<div class="index-value {tone}">{_pct(change)}</div></div>'
        )
    group_rows = []
    for row in groups:
        change = row["returns"]["5d"]
        tone = "up" if change > 0 else "down" if change < 0 else "flat"
        attention = (f'{row["share_change_pp"]:+.2f} 个百分点'
                     if report["activity_available"] else "暂无")
        group_rows.append(
            f'<div class="group-row"><div class="group-title">{escape(row["name"])}</div>'
            f'<div class="group-return {tone}">{_pct(change)}<small>过去 5 个交易日</small></div>'
            f'<div class="group-attention">成交份额 {attention}</div></div>'
        )
    main_groups = [row for row in groups if row["name"] != "其他（综合）"]
    strongest = ", ".join(escape(row["name"]) for row in main_groups[:2])
    if report["activity_available"]:
        more = max(main_groups, key=lambda row: row["share_change_pp"])
        less = min(main_groups, key=lambda row: row["share_change_pp"])
        attention = (
            f'<strong>{escape(more["name"])}</strong>的成交占比比过去 20 日平均高 '
            f'<strong>{more["share_change_pp"]:.2f} 个百分点</strong>；'
            f'<strong>{escape(less["name"])}</strong>低 '
            f'<strong>{abs(less["share_change_pp"]):.2f} 个百分点</strong>。'
        )
    else:
        attention = "申万成交额缺失，暂时无法判断哪些方向的交易更活跃。"
    shortlist = ""
    if candidates:
        cards = []
        for item in candidates:
            cards.append(
                f'<div class="group-row"><div class="group-title">{item["rank"]}. {escape(item["group"])}</div>'
                f'<div class="group-return">{_pct(item["five_day_return"])}<small>过去5日</small></div>'
                f'<div class="group-attention">{escape(item["status"])}<br><small>ID {escape(item["candidate_id"])}</small></div></div>'
            )
        shortlist = (
            '<section class="section"><h2>我先替你筛出的方向</h2>'
            '<p class="section-intro">按最近5日相对表现排序；你决定是否继续观察。'
            '数据若落后于大盘日期，先等待更新，不作为当天行动建议。</p>'
            + ''.join(cards) + '</section>'
        )
    stock_section = ""
    if stock_screen is not None:
        if stock_screen["candidate_count"]:
            rows = []
            for item in stock_screen["candidates"]:
                metrics = item["metrics"]
                rows.append(
                    f'<div class="group-row"><div class="group-title">{escape(item["name"])}<small> '
                    f'{escape(item["instrument_id"])}</small></div>'
                    f'<div class="group-return">{_pct(metrics["momentum_20d"])}'
                    f'<small>过去20日涨跌</small></div>'
                    f'<div class="group-attention">近60日最大回撤 {metrics["max_drawdown_60d"]:.1%}<br>'
                    f'近20日年化波动 {metrics["annualized_volatility_20d"]:.1%}'
                    + (f'<br><small>回执 ID {escape(item["candidate_id"])}</small>'
                       if item.get("candidate_id") else "") + '</div></div>'
                )
            content = "".join(rows)
        else:
            snapshot_line = (
                f'已核对的沪深当前清单 {stock_screen.get("expected_universe_count", 0)} 只，'
                '不含北交所；'
                if stock_screen.get("universe_snapshot_id") else ""
            )
            content = (f'<p><strong>本次不发布股票名单。</strong> {escape(stock_screen["reason"])}</p>'
                       f'<p class="explain">{snapshot_line}本地在该范围内已登记 '
                       f'{stock_screen["universe_count"]} 只，具备完整可用行情 '
                       f'{stock_screen.get("data_ready_count", 0)} 只；'
                       '现阶段的三只试验股不能代表全 A 股。</p>')
        stock_section = (
            '<section class="section"><h2>我替你筛股票</h2>' + content +
            '<p class="explain">涨跌是历史表现，不是预期收益；上涨概率尚无可靠估计。'
            '所有候选都需你在 10:30 后复核，系统不下单。</p></section>'
        )
    validation_section = ""
    if industry_validation is not None:
        count = industry_validation["selected_origin_count"]
        if count:
            result_text = (
                f'历史上有候选的 {count} 次非重叠观察中，随后 5 日平均表现为正的比例 '
                f'<strong>{industry_validation["positive_future_rate"]:.1%}</strong>；'
                f'相对五条主线等权平均，每次差 '
                f'<strong>{industry_validation["mean_excess_vs_equal_weight"]*100:+.2f} 个百分点</strong>。'
            )
        else:
            result_text = "历史样本中没有符合规则的观察起点。"
        validation_section = (
            '<section class="section"><h2>行业筛法的历史表现</h2><p>' + result_text +
            '</p><p class="explain">这是行业指数的后续走势检验，不是可交易收益。'
            '<a href="industry-validation.md">查看样本和限制</a>。</p></section>'
        )
    research_section = ""
    if research_validation is not None:
        metrics = research_validation["backtest"]["metrics"]
        research_section = (
            '<section class="section"><h2>股票策略与预测的试运行</h2>'
            f'<p>仅对 {len(research_validation["universe"])} 只重点股回看：策略累计 '
            f'<strong>{metrics["total_return"]:+.1%}</strong>，最大回撤 '
            f'<strong>{metrics["max_drawdown"]:.1%}</strong>；独立时间外样本尚无。'
            '<a href="backtest.md">查看回测对照</a>。</p>'
        )
        if forecast_validation is not None:
            result = forecast_validation["pooled"].get("5", {})
            if result.get("status") == "ready":
                research_section += (
                    f'<p>简单的20日动量外推在5日预测上的误差，相对不变价基线改善 '
                    f'<strong>{result["mae_return_skill_vs_random_walk"]:+.1%}</strong>。'
                    '目前不把预测加入股票排名。<a href="forecast-momentum.md">查看预测检验</a>。</p>'
                )
        research_section += '</section>'
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>A 股市场，一眼看懂</title>
<style>
:root{{--ink:#17212b;--muted:#526272;--line:#dbe4eb;--bg:#f5f8fa;--card:#fff;--red:#b53b48;--green:#167b68;--blue:#1a5577}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:20px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1100px;margin:auto;padding:28px 24px 80px}}h1{{font-size:clamp(38px,5vw,64px);line-height:1.12;margin:20px 0 12px}}h2{{font-size:30px;margin:0 0 20px}}p{{margin:10px 0}}.lead{{font-size:23px;color:var(--muted)}}
.datebox{{background:#fff4db;border:2px solid #ecc477;border-radius:18px;padding:18px 22px;margin:24px 0;font-size:20px}}.datebox strong{{font-size:24px}}
.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin:30px 0}}.card,.section{{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:24px;box-shadow:0 6px 22px #213c5010}}
.eyebrow{{font-size:17px;font-weight:700;color:var(--muted)}}.big{{font-size:clamp(36px,4vw,52px);font-weight:800;line-height:1.15;margin:10px 0}}.explain{{font-size:18px;color:var(--muted)}}.down{{color:var(--red)}}.up{{color:var(--green)}}.flat{{color:var(--muted)}}
.section{{margin:22px 0}}.section-intro{{font-size:18px;color:var(--muted);margin-top:-10px;margin-bottom:22px}}.index-row{{display:grid;grid-template-columns:150px 1fr 105px;gap:18px;align-items:center;padding:12px 0;border-top:1px solid var(--line)}}.index-name{{font-weight:700}}.index-value{{text-align:right;font-size:23px;font-weight:800;font-variant-numeric:tabular-nums}}.track{{height:18px;border-radius:20px;background:#e9eff3;overflow:hidden}}.bar{{height:100%;border-radius:20px;background:var(--green)}}.bar.down{{background:var(--red)}}
.group-row{{display:grid;grid-template-columns:1fr 190px 210px;gap:14px;align-items:center;padding:17px 0;border-top:1px solid var(--line)}}.group-title{{font-size:23px;font-weight:700}}.group-return{{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums}}.group-return small{{display:block;font-size:14px;font-weight:500;color:var(--muted)}}.group-attention{{font-size:17px;color:var(--muted)}}
.chain{{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:20px 0}}.node{{border:2px solid #bad3e0;background:#eef6fa;border-radius:14px;padding:14px 18px;font-weight:700}}.arrow{{font-size:28px;color:var(--blue);font-weight:800}}.subchain{{font-size:18px;color:var(--muted)}}
.note{{border-left:5px solid var(--blue);padding:6px 0 6px 18px;background:#eef6fa}}.footer{{font-size:16px;color:var(--muted);padding-top:12px}}
@media(max-width:760px){{.wrap{{padding:16px 14px 50px}}.cards{{grid-template-columns:1fr}}.card,.section{{padding:19px}}.index-row{{grid-template-columns:105px 1fr 88px;gap:8px}}.index-value{{font-size:19px}}.group-row{{grid-template-columns:1fr auto}}.group-attention{{grid-column:1 / -1}}}}
</style></head><body><main class="wrap">
<h1>A 股市场，一眼看懂</h1><p class="lead">先看整体，再看方向。这里展示的是已经取得的历史数据。</p>
<div class="datebox"><strong>请先看日期</strong><br>大盘指数：{escape(report['asof'])}　｜　行业：{escape(report['industry_asof'])}<br>行业数据较旧，不能用它解释大盘最新一天的变化。</div>
<section class="section"><h2>一句话看资金</h2><p>{escape(flow['reader_lines'][0])}</p><p>{escape(flow['reader_lines'][1])}</p><p class="note">{escape(flow['reader_lines'][2])}</p></section>
<div class="cards">
<div class="card"><div class="eyebrow">① 大盘当天</div><div class="big down">{falling} / 5 下跌</div><div class="explain">截至 {escape(report['asof'])}，观察的五个主要指数中有 {falling} 个下跌。</div></div>
<div class="card"><div class="eyebrow">② 行业当天</div><div class="big down">{breadth['down']} / 31 下跌</div><div class="explain">截至 {escape(report['industry_asof'])}，多数申万一级行业下跌。这说的是行业指数，不是股票家数。</div></div>
<div class="card"><div class="eyebrow">③ 过去五日相对强</div><div class="big" style="font-size:32px">{strongest}</div><div class="explain">只是相对表现靠前，不表示现在应该买入。</div></div>
</div>
{shortlist}
{stock_section}
{validation_section}
{research_section}
<section class="section"><h2>大盘：最近一个交易日</h2><p class="section-intro">红色越长，跌得越多。每个数字是该指数自身的单日涨跌幅。</p>{''.join(broad_rows)}</section>
<section class="section"><h2>把行业连成几条线</h2><div class="chain"><span class="node">资源能源</span><span class="arrow">→</span><span class="node">制造建设</span><span class="arrow">→</span><span class="node">消费健康</span></div><p class="subchain">科技信息影响生产与消费；金融与公共服务支撑这些环节。箭头表示阅读顺序，不表示涨跌必然传导。</p></section>
<section class="section"><h2>五条主线，看强弱</h2><p class="section-intro">看过去 5 个交易日的相对表现；“其他（综合）”单列。数字是类内行业指数的简单平均，不是可买卖的指数。行业数据截至 {escape(report['industry_asof'])}。</p>{''.join(group_rows)}</section>
<section class="section"><h2>资金去了哪里？</h2><p>{attention}</p><p class="note"><strong>这只能说明哪里交易得更活跃，不能说明资金净流入。</strong><br>一笔成交同时有买方和卖方。当前数据库没有可靠的买卖方向数据，所以这里不画“主力资金流入”图。</p></section>
<section class="section"><h2>按你的时间怎么用</h2><p><strong>收盘后：</strong>看大盘和五条主线，决定观察哪些方向。</p><p><strong>次日 10:30 后：</strong>复核实时行情，再由你决定是否人工交易。</p><p class="explain">日线报告不包含 10:30 的价格，不能当作那个时点的下单信号。</p></section>
<p class="footer">数据：宽基历史行情与申万一级行业指数。具体来源、同步批次和计算口径见同名 JSON 与市场脉搏说明。报告用于观察，不构成交易建议。</p>
</main></body></html>'''


def write_market_pulse(report: dict[str, object], output: str | Path,
                       candidates: Optional[list[dict[str, object]]] = None,
                       stock_screen: Optional[dict[str, object]] = None,
                       industry_validation: Optional[dict[str, object]] = None,
                       research_validation: Optional[dict[str, object]] = None,
                       forecast_validation: Optional[dict[str, object]] = None) -> tuple[Path, Path, Path]:
    md = Path(output)
    md.parent.mkdir(parents=True, exist_ok=True)
    data = md.with_suffix(".json")
    html = md.with_suffix(".html")
    md.write_text(render_market_markdown(report, stock_screen, industry_validation,
                                         research_validation, forecast_validation), encoding="utf-8")
    data.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html.write_text(render_market_html(report, candidates, stock_screen, industry_validation,
                                       research_validation, forecast_validation), encoding="utf-8")
    return md, data, html
