"""Narrative layer.

Turns analytics output into short, research-note style "要点解读" for each
view. Structured the way an economics piece reads: a one-line takeaway
(摘要), supporting observations (论据), and a risk/caveat (风险) where useful.

All text is generated from the actual data, so it stays in sync when the
underlying numbers change. Nothing here computes statistics — it only phrases
what :mod:`analytics` already produced.
"""

from __future__ import annotations

from typing import List

from auto_invest.analysis import analytics, datasources as ds


def _trend(series: List[float]) -> str:
    if len(series) < 2:
        return "持平"
    delta = series[-1] - series[-2]
    if delta > 0.05:
        return "回升"
    if delta < -0.05:
        return "回落"
    return "基本持平"


def overview(code: str) -> dict:
    """Macro takeaway for one country."""
    c = ds.country(code)
    m = c["macro"]
    gdp = m["gdp_growth"][-1]
    cpi = m["inflation"][-1]
    une = m["unemployment"][-1]
    rate = m["policy_rate"][-1]
    name = c["name"]

    # crude regime read
    if cpi >= 4:
        price = "通胀偏高"
    elif cpi < 1:
        price = "通胀低迷甚至通缩压力"
    else:
        price = "通胀温和"
    growth = "增长稳健" if gdp >= 2.5 else ("增长放缓" if gdp >= 0 else "经济收缩")

    summary = f"{name}最新一年 GDP 增速 {gdp}%、CPI {cpi}%、失业率 {une}%、政策利率 {rate}%,整体呈现「{growth}、{price}」。"
    points = [
        f"增长:GDP 增速 {gdp}%,较上一年{_trend(m['gdp_growth'])}。",
        f"物价:CPI {cpi}%,较上一年{_trend(m['inflation'])};政策利率 {rate}%,较上一年{_trend(m['policy_rate'])}。",
        f"就业:失业率 {une}%,较上一年{_trend(m['unemployment'])}。",
    ]
    risk = "政策利率与通胀的组合决定流动性环境;若通胀反复,降息节奏可能生变。"
    return {"summary": summary, "points": points, "risk": risk}


def compare(metric: str) -> dict:
    d = ds.compare_metric(metric)
    li = len(d["years"]) - 1
    ranked = sorted(
        ({"name": f"{s['flag']} {s['name']}", "v": s["values"][li]} for s in d["series"]),
        key=lambda x: x["v"],
        reverse=True,
    )
    label = {"gdp_growth": "GDP 增速", "inflation": "CPI 通胀", "unemployment": "失业率", "policy_rate": "政策利率"}[metric]
    top, bottom = ranked[0], ranked[-1]
    summary = f"{d['years'][li]} 年{label}对比中,{top['name']} 最高 ({top['v']}%),{bottom['name']} 最低 ({bottom['v']}%)。"
    points = [f"{r['name']}:{r['v']}%" for r in ranked]
    risk = "各国口径与统计方法存在差异,横向对比宜看趋势与量级,而非小数点。"
    return {"summary": summary, "points": points, "risk": risk}


def sectors() -> dict:
    rot = analytics.sector_rotation()
    best, worst = rot["latest_best"], rot["latest_worst"]
    yr = rot["latest_year"]
    summary = f"{yr} 年行业分化明显:{best['sector']} 领涨 ({best['ret']}%),{worst['sector']} 掉队 ({worst['ret']}%)。"
    # find a sector that flipped from worst-half to best-half or vice versa
    points = [
        f"领涨:{best['sector']} +{best['ret']}%,通常对应当年的主导叙事(如 AI、降息受益)。",
        f"掉队:{worst['sector']} {worst['ret']}%,往往是资金流出或基本面承压的方向。",
        f"价差:首尾相差约 {round(best['ret'] - worst['ret'], 1)} 个百分点,价差越大说明行业轮动越剧烈。",
    ]
    risk = "行业年度回报是结果而非预测;领涨行业次年未必延续,需结合大势与估值判断。"
    return {"summary": summary, "points": points, "risk": risk}


def stock(research: dict) -> dict:
    s = research["stats"]
    name = research["name"]
    cum, vol, mdd, sharpe = s["cum_return"], s["ann_vol"], s["max_drawdown"], s["sharpe"]
    perf = "上涨" if cum >= 0 else "下跌"
    quality = "性价比较好" if sharpe >= 1 else ("性价比一般" if sharpe >= 0 else "风险调整后回报为负")
    summary = f"{name} 区间累计{perf} {cum}%,年化波动 {vol}%,最大回撤 {mdd}%,夏普 {sharpe}({quality})。"
    points = [
        f"收益:累计 {cum}%、年化 {s['ann_return']}%、胜率 {s['win_rate']}%。",
        f"风险:年化波动 {vol}%、最大回撤 {mdd}%、最大连亏 {s['max_losing_streak']} 日。",
        f"风险调整:夏普 {sharpe}、索提诺 {s['sortino']}(越高说明每单位风险换来的回报越多)。",
    ]
    risk = f"样本仅 {s['points']} 个交易日,统计量易被极端值放大,换长序列后更可靠。"
    return {"summary": summary, "points": points, "risk": risk}


def actors() -> dict:
    feed = ds.actors_and_events()
    latest = feed["events"][-1]
    summary = f"当前所处阶段:{latest['year']} 年「{latest['title']}」——{latest['detail']}。"
    points = [f"{e['year']} · {e['title']}:{e['detail']}" for e in reversed(feed["events"])]
    risk = "事件为示例占位数据;接入真实财经日历后,这里应反映最新央行决议与数据发布。"
    return {"summary": summary, "points": points, "risk": risk}
