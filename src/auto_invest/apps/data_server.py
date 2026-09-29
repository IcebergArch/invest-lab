"""A lightweight, zero-dependency local web app for exploring data.

The system is organised around a top-down analysis framework:

* 框架    — the analysis philosophy (哲学 → 理论 → 技术) and how to read each metric
* 大势    — macro environment: GDP / CPI / unemployment / policy rate
* 关键方·事件 — key actors (central bank / policy) and a timeline of key events
* 行业    — industry layer: instruments grouped by sector, relative strength
* 个股    — single-instrument research: price + MAs, volume, returns, rich stats
* 市场·相关 — all instruments normalised to 100 + a return-correlation matrix
* 回测    — reuse the project BacktestEngine: equity curve + performance metrics

Uses only the Python standard library (http.server). Run::

    PYTHONPATH=src python3 -m auto_invest.apps.data_server \
        --bars data/sample/bars.csv \
        --instruments configs/universe.example.json --port 8000

Then open http://127.0.0.1:8000 in a browser. Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict
from urllib.parse import parse_qs, urlparse

# Three-layer analysis backend (see auto_invest.analysis):
#   datasources  -> raw data (bars, macro, sectors, events)
#   analytics    -> statistics / correlation / sector rotation / backtest
#   narrative    -> research-note style 要点解读 per view
# This module is the API/routing + presentation layer on top of them.
from auto_invest.analysis import analytics, datasources, narrative


# ---------------------------------------------------------------------------
# Aggregation for the 总览 dashboard (cross-layer snapshot)
# ---------------------------------------------------------------------------
def dashboard(bars_path: Path, country_code: str = "US") -> dict:
    """One-screen snapshot aggregating the most important item from each layer:
    macro KPIs (大势), leading/lagging sector (行业), and a market snapshot."""
    c = datasources.country(country_code)
    rot = analytics.sector_rotation()
    market = analytics.normalised_market(bars_path)
    # market snapshot: latest normalised value + change per instrument
    snapshot = []
    for s in market["series"]:
        vals = [v for v in s["values"] if v is not None]
        if not vals:
            continue
        last = vals[-1]
        prev = vals[-2] if len(vals) >= 2 else vals[0]
        snapshot.append({
            "name": s["name"],
            "last": last,
            "change": round(last - prev, 2),
            "total": round(last - 100, 2),
        })
    snapshot.sort(key=lambda x: x["total"], reverse=True)
    return {
        "country": {"code": c["code"], "name": c["name"], "flag": c["flag"]},
        "kpis": c["kpis"],
        "sector_best": rot["latest_best"],
        "sector_worst": rot["latest_worst"],
        "sector_year": rot["latest_year"],
        "market": snapshot,
        "macro_summary": narrative.overview(country_code)["summary"],
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
def make_handler(bars_path: Path, instruments_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quieter console
            pass

        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            route = parsed.path
            qs = parse_qs(parsed.query)
            try:
                if route == "/" or route == "/index.html":
                    self._send_html(PAGE)
                elif route == "/api/dashboard":
                    code = qs.get("country", ["US"])[0]
                    if code not in datasources.COUNTRIES:
                        self._send_json({"error": f"未知国家: {code}"}, 404)
                        return
                    self._send_json(dashboard(bars_path, code))
                elif route == "/api/countries":
                    self._send_json({"countries": datasources.list_countries()})
                elif route == "/api/overview":
                    code = qs.get("country", ["US"])[0]
                    try:
                        payload = datasources.country(code)
                    except KeyError:
                        self._send_json({"error": f"未知国家: {code}"}, 404)
                        return
                    payload["narrative"] = narrative.overview(code)
                    self._send_json(payload)
                elif route == "/api/compare":
                    metric = qs.get("metric", ["gdp_growth"])[0]
                    try:
                        payload = datasources.compare_metric(metric)
                    except ValueError:
                        self._send_json({"error": f"未知指标: {metric}"}, 400)
                        return
                    payload["narrative"] = narrative.compare(metric)
                    self._send_json(payload)
                elif route == "/api/sectors":
                    payload = analytics.sector_rotation()
                    payload["narrative"] = narrative.sectors()
                    self._send_json(payload)
                elif route == "/api/market":
                    self._send_json(analytics.normalised_market(bars_path))
                elif route == "/api/instruments":
                    self._send_json(datasources.list_instruments(datasources.read_bars(bars_path)))
                elif route == "/api/actors":
                    payload = datasources.actors_and_events()
                    payload["narrative"] = narrative.actors()
                    self._send_json(payload)
                elif route == "/api/industry":
                    self._send_json(analytics.industry_breakdown(datasources.read_bars(bars_path)))
                elif route == "/api/correlation":
                    self._send_json(analytics.correlation_matrix(datasources.read_bars(bars_path)))
                elif route == "/api/backtest":
                    strat = qs.get("strategy", ["ma-cross"])[0]
                    fast = int(qs.get("fast", ["2"])[0])
                    slow = int(qs.get("slow", ["3"])[0])
                    cash = qs.get("cash", ["100000"])[0]
                    if strat == "ma-cross" and (fast <= 0 or slow <= 0 or fast >= slow):
                        self._send_json({"error": "需满足 0 < fast < slow"}, 400)
                        return
                    try:
                        data = analytics.run_backtest(
                            bars_path, instruments_path, strat, fast, slow, cash
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._send_json({"error": f"回测失败: {exc}"}, 400)
                        return
                    self._send_json(data)
                elif route == "/api/stock":
                    iid = qs.get("id", [""])[0]
                    fast = int(qs.get("fast", ["2"])[0])
                    slow = int(qs.get("slow", ["3"])[0])
                    if fast <= 0 or slow <= 0 or fast >= slow:
                        self._send_json({"error": "需满足 0 < fast < slow"}, 400)
                        return
                    try:
                        data = analytics.stock_research(datasources.read_bars(bars_path), iid, fast, slow)
                    except KeyError:
                        self._send_json({"error": f"未知标的: {iid}"}, 404)
                        return
                    data["narrative"] = narrative.stock(data)
                    self._send_json(data)
                else:
                    self._send_json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001 - surface errors to client
                self._send_json({"error": str(exc)}, 500)

    return Handler


# ---------------------------------------------------------------------------
# Front-end (single page, Chart.js from CDN — loaded by the user's browser)
# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Auto Invest · 数据系统</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230b1f24'/%3E%3Cpolyline points='5,21 12,14 17,18 27,7' fill='none' stroke='%234fd1a5' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3Ccircle cx='27' cy='7' r='2.6' fill='%234fd1a5'/%3E%3C/svg%3E">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; background:#0b1f24; color:#e7f3f1;
    font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
  header { padding:18px 32px; border-bottom:1px solid #1d4750; display:flex; align-items:baseline; gap:16px; }
  header h1 { font-size:18px; margin:0; font-weight:650; }
  header .sub { color:#6f8f8a; font-size:12px; }
  .layout { display:flex; min-height:calc(100vh - 57px); }
  nav { width:230px; border-right:1px solid #1d4750; padding:18px 0; flex-shrink:0; }
  nav .group { color:#5d7d78; font-size:11px; letter-spacing:.08em; padding:14px 24px 6px; }
  nav button { display:block; width:100%; text-align:left; background:transparent; color:#cfe9e4;
    border:none; border-left:3px solid transparent; padding:9px 24px; font-size:14px; cursor:pointer; }
  nav button:hover { background:#11313a; }
  nav button.active { background:#11313a; border-left-color:#2e8b7f; color:#eafff8; font-weight:600; }
  nav button .layer { color:#5d7d78; font-size:11px; margin-left:6px; }
  main { flex:1; padding:22px 32px 40px; overflow:auto; }
  .vtitle { font-size:16px; font-weight:650; margin:0 0 4px; }
  .vdesc { color:#6f8f8a; font-size:12px; margin:0 0 20px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
  .card { background:#11313a; border:1px solid #1d4750; border-radius:12px; padding:18px 18px 8px; }
  .card h2 { font-size:14px; margin:0 0 12px; font-weight:600; color:#cfe9e4; }
  .card.wide { grid-column:1 / -1; }
  .chart-wrap { position:relative; height:260px; }
  .kpis { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:20px; }
  .kpi { background:#11313a; border:1px solid #1d4750; border-radius:12px; padding:13px 15px; }
  .kpi .lab { color:#8fb3ad; font-size:12px; }
  .kpi .val { font-size:23px; font-weight:680; margin-top:4px; }
  .kpi .delta { font-size:11px; margin-top:3px; }
  .up { color:#4fd1a5; } .down { color:#ff7b72; }
  .controls { display:flex; gap:14px; align-items:center; margin-bottom:18px; flex-wrap:wrap; }
  select, input[type=number] { background:#0b1f24; color:#e7f3f1; border:1px solid #2e8b7f;
    border-radius:8px; padding:7px 10px; font-size:14px; }
  label { color:#8fb3ad; font-size:13px; }
  button.apply { background:#1b5a52; color:#eafff8; border:1px solid #2e8b7f; border-radius:8px;
    padding:7px 16px; cursor:pointer; font-size:14px; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { padding:8px 10px; text-align:center; border:1px solid #1d4750; }
  th { color:#8fb3ad; font-weight:600; }
  .foot { color:#5d7d78; font-size:11px; margin-top:24px; line-height:1.6; }
  .hidden { display:none; }
  .note { background:#0e2a26; border:1px solid #1d4750; border-radius:8px; padding:8px 12px;
    color:#8fb3ad; font-size:12px; margin-bottom:16px; }
  .prose { max-width:780px; line-height:1.85; font-size:14px; color:#cfe9e4; }
  .prose h3 { color:#eafff8; font-size:15px; margin:22px 0 8px; }
  .prose .tag { display:inline-block; background:#1b5a52; color:#eafff8; border-radius:6px;
    padding:1px 9px; font-size:12px; margin-right:6px; }
  .timeline { border-left:2px solid #2e8b7f; margin:8px 0 0 8px; padding-left:18px; }
  .tl-item { margin-bottom:16px; position:relative; }
  .tl-item::before { content:""; position:absolute; left:-25px; top:4px; width:10px; height:10px;
    border-radius:50%; background:#4fd1a5; }
  .tl-item .yr { color:#4fd1a5; font-weight:650; }
  .tl-item .ty { color:#5d7d78; font-size:11px; margin-left:8px; }
  .actor { background:#0e2a26; border:1px solid #1d4750; border-radius:8px; padding:10px 14px; margin-bottom:10px; }
  .actor .nm { font-weight:600; color:#eafff8; }
  .actor .rl { color:#4fd1a5; font-size:12px; margin-left:8px; }
  .actor .nt { color:#8fb3ad; font-size:12px; margin-top:3px; }
  /* narrative box — research-note style 要点解读 */
  .narr { background:linear-gradient(180deg,#0e2a26,#0c2420); border:1px solid #1d4750;
    border-left:3px solid #4fd1a5; border-radius:10px; padding:14px 18px; margin-bottom:20px; }
  .narr .sm { color:#eafff8; font-size:14px; font-weight:600; line-height:1.6; }
  .narr ul { margin:10px 0 0; padding-left:18px; }
  .narr li { color:#cfe9e4; font-size:13px; line-height:1.7; }
  .narr .rk { color:#ffb454; font-size:12px; margin-top:10px; }
  .narr .rk b { color:#ffb454; }
  /* dashboard home */
  .dash-hero { display:flex; gap:14px; align-items:baseline; margin-bottom:4px; }
  .dash-hero .big { font-size:22px; font-weight:700; }
  .snap-row { display:flex; justify-content:space-between; padding:7px 4px; border-bottom:1px solid #16383f; font-size:13px; }
  .snap-row:last-child { border-bottom:none; }
  .pill { display:inline-block; border-radius:6px; padding:1px 8px; font-size:12px; }
  .pill.g { background:#1b5a52; color:#eafff8; } .pill.r { background:#5a2b2b; color:#ffd9d6; }
</style>
</head>
<body>
<header>
  <h1>Auto Invest · 数据系统</h1>
  <span class="sub">自上而下:大势 → 关键方·事件 → 国家/行业/个股</span>
  <span style="margin-left:auto"></span>
  <label style="font-size:13px">经济体
    <select id="sel-country" style="margin-left:6px"></select>
  </label>
</header>
<div class="layout">
<nav id="nav">
  <div class="group">总览</div>
  <button data-view="dashboard" class="active">仪表盘<span class="layer">首页</span></button>
  <button data-view="framework">框架 / 论点<span class="layer">哲学·理论</span></button>
  <div class="group">① 大势</div>
  <button data-view="overview">宏观概览<span class="layer">国家</span></button>
  <button data-view="compare">全球对比<span class="layer">跨国</span></button>
  <div class="group">② 关键方 · 事件</div>
  <button data-view="actors">关键方与事件<span class="layer">驱动</span></button>
  <div class="group">③ 细分下钻</div>
  <button data-view="industry">行业轮动<span class="layer">行业</span></button>
  <button data-view="stock">个股研究<span class="layer">企业</span></button>
  <div class="group">横向 · 验证</div>
  <button data-view="market">市场 · 相关性<span class="layer">技术</span></button>
  <button data-view="backtest">策略回测<span class="layer">验证</span></button>
</nav>
<main>
  <!-- DASHBOARD HOME -->
  <section id="view-dashboard">
    <div class="dash-hero">
      <span class="big" id="dash-flag">📊</span>
      <h1 class="vtitle" id="dash-title">总览仪表盘</h1>
    </div>
    <p class="vdesc">一屏聚合各层最关键的信息:大势 KPI、行业领涨/掉队、市场快照。右上角切换经济体。</p>
    <div class="narr"><div class="sm" id="dash-summary">加载中…</div></div>
    <div class="kpis" id="dash-kpis"></div>
    <div class="grid">
      <div class="card"><h2 id="dash-sec-title">行业冷热(最新年度)</h2><div id="dash-sectors"></div></div>
      <div class="card"><h2>市场快照(归一化,起点=100)</h2><div id="dash-market"></div></div>
    </div>
  </section>
  <!-- FRAMEWORK -->
  <section id="view-framework" class="hidden">
    <h1 class="vtitle">分析框架 / 论点</h1>
    <p class="vdesc">这个系统按一套自上而下的逻辑组织数据,先回答"为什么看",再看"看什么"。</p>
    <div class="prose">
      <p><span class="tag">哲学</span><span class="tag">理论</span><span class="tag">技术</span>
      三部曲是认知的递进:先有判断世界的<b>哲学</b>(自上而下、宏观决定方向),再有方法论<b>理论</b>(用相关性分散风险、用均线捕捉趋势、用回测验证假设),最后才落到具体<b>技术</b>指标与工具。指标本身不是目的,它只是验证某个判断的证据。</p>
      <h3>① 大势:先定方向</h3>
      <p>任何个股都活在宏观环境里。GDP 增速决定企业盈利的水温,CPI 通胀和政策利率决定流动性和估值。看大势,是为了知道现在该进攻还是防守。</p>
      <h3>② 关键方、关键性事件:再定时机</h3>
      <p>大势是由<b>关键方</b>推动的——央行的利率决策、财政与关税、数据发布方的关键数字。<b>关键事件/拐点</b>(衰退、加息见顶、政策转向)往往是行情的分水岭。识别关键方在做什么、下一个关键事件在哪,是把握时机的核心。</p>
      <h3>③ 细分下钻:国家 → 行业 → 个体企业</h3>
      <p>方向和时机确定后,逐级下钻:从国家(宏观),到行业(谁在轮动、谁强谁弱),再到个体企业(个股的趋势、波动、回撤与性价比)。最后用<b>相关性</b>看组合分散度,用<b>回测</b>验证策略是否真的成立。</p>
      <h3>指标口径</h3>
      <p>年化波动 = 日收益标准差 × √252;夏普 = 年化收益 / 年化波动(无风险利率取 0);索提诺只罚下行波动;最大回撤用历史高水位法,与回测引擎口径一致。相关性为日收益的 Pearson 系数。</p>
    </div>
  </section>
  <!-- OVERVIEW -->
  <section id="view-overview" class="hidden">
    <h1 class="vtitle" id="ov-title">① 大势 · 宏观概览</h1>
    <p class="vdesc" id="ov-desc">右上角切换经济体。年度数据,口径来自各国央行 / 统计局。</p>
    <div class="narr" id="narr-overview"></div>
    <div class="kpis" id="kpis"></div>
    <div class="grid">
      <div class="card"><h2>GDP 实际增速 (%)</h2><div class="chart-wrap"><canvas id="c-gdp"></canvas></div></div>
      <div class="card"><h2>CPI 通胀率 (%)</h2><div class="chart-wrap"><canvas id="c-cpi"></canvas></div></div>
      <div class="card"><h2>失业率 (%)</h2><div class="chart-wrap"><canvas id="c-unemp"></canvas></div></div>
      <div class="card"><h2>政策利率 (%)</h2><div class="chart-wrap"><canvas id="c-rate"></canvas></div></div>
    </div>
  </section>
  <!-- COMPARE (global) -->
  <section id="view-compare" class="hidden">
    <h1 class="vtitle">① 大势 · 全球对比</h1>
    <p class="vdesc">同一指标下,中美欧日横向对比,看全球格局与分化。数据为各国年度值。</p>
    <div class="narr" id="narr-compare"></div>
    <div class="controls">
      <label>指标
        <select id="sel-metric">
          <option value="gdp_growth">GDP 实际增速</option>
          <option value="inflation">CPI 通胀率</option>
          <option value="unemployment">失业率</option>
          <option value="policy_rate">政策利率</option>
        </select>
      </label>
    </div>
    <div class="grid">
      <div class="card wide"><h2 id="cmp-title">多国对比</h2>
        <div class="chart-wrap" style="height:360px"><canvas id="c-compare"></canvas></div></div>
      <div class="card wide"><h2>最新年度排行</h2><div id="cmp-rank"></div></div>
    </div>
  </section>
  <!-- ACTORS & EVENTS -->
  <section id="view-actors" class="hidden">
    <h1 class="vtitle">② 关键方 · 关键性事件</h1>
    <p class="vdesc">推动大势的关键参与者,以及近年的关键拐点。事件为示例数据,真实事件源后续接入。</p>
    <div class="narr" id="narr-actors"></div>
    <div class="note">示例数据 · 占位结构。后续可接入财经日历 / 政策公告 / 央行决议作为真实事件源。</div>
    <div class="grid">
      <div class="card"><h2>关键参与方</h2><div id="actors-box"></div></div>
      <div class="card"><h2>关键事件时间线</h2><div id="events-box" class="timeline"></div></div>
    </div>
  </section>
  <!-- INDUSTRY / SECTOR ROTATION -->
  <section id="view-industry" class="hidden">
    <h1 class="vtitle">③ 细分 · 行业轮动</h1>
    <p class="vdesc">GICS 11 大行业的年度回报。看哪些行业在领涨、哪些在掉队——这是判断"经济往哪走"的核心。数据为标普 500 各板块年度总回报。</p>
    <div class="narr" id="narr-sectors"></div>
    <div class="kpis" id="sector-kpis"></div>
    <div class="grid">
      <div class="card wide"><h2 id="sec-bar-title">行业年度回报排行</h2>
        <div class="chart-wrap" style="height:340px"><canvas id="c-sector"></canvas></div></div>
      <div class="card wide"><h2>行业轮动表(按年,排名 = 当年涨幅高→低)</h2>
        <div id="sector-quilt" style="overflow-x:auto"></div></div>
    </div>
  </section>
  <!-- STOCK -->
  <section id="view-stock" class="hidden">
    <h1 class="vtitle">③ 细分 · 个股研究</h1>
    <p class="vdesc">单标的下钻:趋势、波动、回撤与性价比。</p>
    <div class="controls">
      <label>标的 <select id="sel-stock"></select></label>
      <label>快线 <input id="in-fast" type="number" min="1" value="2" style="width:64px"></label>
      <label>慢线 <input id="in-slow" type="number" min="2" value="3" style="width:64px"></label>
      <button class="apply" id="btn-apply">更新</button>
    </div>
    <div class="narr" id="narr-stock"></div>
    <div class="kpis" id="stock-stats-1"></div>
    <div class="kpis" id="stock-stats-2"></div>
    <div class="grid">
      <div class="card wide"><h2>收盘价 + 均线</h2><div class="chart-wrap" style="height:320px"><canvas id="c-price"></canvas></div></div>
      <div class="card"><h2>成交量</h2><div class="chart-wrap"><canvas id="c-vol"></canvas></div></div>
      <div class="card"><h2>日收益率 (%)</h2><div class="chart-wrap"><canvas id="c-ret"></canvas></div></div>
    </div>
  </section>
  <!-- MARKET + CORRELATION -->
  <section id="view-market" class="hidden">
    <h1 class="vtitle">横向 · 市场与相关性</h1>
    <p class="vdesc">所有标的归一化对比 + 日收益相关性矩阵(分散度参考)。</p>
    <div class="grid">
      <div class="card wide"><h2>市场表现 · 收盘价归一化 (起点=100)</h2>
        <div class="chart-wrap" style="height:340px"><canvas id="c-market"></canvas></div></div>
      <div class="card wide"><h2>收益相关性矩阵</h2><div id="corr-table"></div>
        <p class="vdesc" style="margin-top:10px" id="corr-note"></p></div>
    </div>
  </section>
  <!-- BACKTEST -->
  <section id="view-backtest" class="hidden">
    <h1 class="vtitle">验证 · 策略回测</h1>
    <p class="vdesc">调用项目回测引擎 (BacktestEngine),验证策略在历史数据上的表现。</p>
    <div class="controls">
      <label>策略 <select id="sel-strat"><option value="ma-cross">均线交叉</option><option value="buy-and-hold">买入持有</option></select></label>
      <label>快线 <input id="bt-fast" type="number" min="1" value="2" style="width:64px"></label>
      <label>慢线 <input id="bt-slow" type="number" min="2" value="3" style="width:64px"></label>
      <label>初始资金 <input id="bt-cash" type="number" min="1" value="100000" style="width:110px"></label>
      <button class="apply" id="btn-bt">运行回测</button>
    </div>
    <div class="kpis" id="bt-stats"></div>
    <div class="grid">
      <div class="card wide"><h2>资金曲线 (权益)</h2><div class="chart-wrap" style="height:320px"><canvas id="c-equity"></canvas></div></div>
    </div>
  </section>
  <div class="foot">
    宏观为各经济体年度值(美国 BEA/BLS/Fed;中国 NBS/世行/IMF;欧元区 Eurostat/ECB;日本内阁府/统计局/BOJ);部分 2016–2019 为权威近似。
    行业轮动为标普 500 GICS 11 板块年度总回报(RBC / S&P / Novel Investor)。市场、个股、相关性、回测基于项目 bars.csv,统计为演示性。
    关键方与事件为示例占位数据。
  </div>
</main>
</div>
<script>
const GRID="rgba(120,160,155,0.15)", TICK="#8fb3ad";
Chart.defaults.color=TICK;
Chart.defaults.font.family="-apple-system,'PingFang SC',sans-serif";
const charts={};
function draw(id,cfg){ if(charts[id]) charts[id].destroy(); charts[id]=new Chart(document.getElementById(id),cfg); }
function axes(){ return {x:{grid:{color:GRID},ticks:{color:TICK}},y:{grid:{color:GRID},ticks:{color:TICK}}}; }
function lineCfg(labels,data,color,fill){
  return {type:"line",data:{labels,datasets:[{data,borderColor:color,backgroundColor:fill?color+"22":"transparent",
    borderWidth:2,pointRadius:2,tension:0.3,fill:!!fill,spanGaps:true}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:axes()}};
}
async function getJSON(u){ const r=await fetch(u); return r.json(); }
function kpi(box,lab,val,unit,cls){ box.insertAdjacentHTML("beforeend",
  `<div class="kpi"><div class="lab">${lab}</div><div class="val ${cls||""}">${val}${unit||""}</div></div>`); }
// render a research-note style narrative box from {summary, points, risk}
function renderNarr(id,n){
  if(!n){ document.getElementById(id).innerHTML=""; return; }
  let html=`<div class="sm">${n.summary}</div>`;
  if(n.points&&n.points.length) html+="<ul>"+n.points.map(p=>`<li>${p}</li>`).join("")+"</ul>";
  if(n.risk) html+=`<div class="rk"><b>风险/提示:</b> ${n.risk}</div>`;
  document.getElementById(id).innerHTML=html;
}

// global country state
let curCountry="US";
async function initCountries(){
  const d=await getJSON("/api/countries");
  const sel=document.getElementById("sel-country");
  d.countries.forEach(c=>{ const o=document.createElement("option");
    o.value=c.code; o.textContent=`${c.flag} ${c.name}`; sel.appendChild(o); });
  sel.value=curCountry;
  sel.onchange=()=>{ curCountry=sel.value; ov=false; dash=false;
    // refresh whichever view is visible
    if(!document.getElementById("view-dashboard").classList.contains("hidden")) loadDashboard();
    if(!document.getElementById("view-overview").classList.contains("hidden")) loadOverview();
  };
}

// 总览 dashboard home
let dash=false;
async function loadDashboard(){
  if(dash) return; dash=true;
  const d=await getJSON("/api/dashboard?country="+curCountry);
  document.getElementById("dash-flag").textContent=d.country.flag;
  document.getElementById("dash-title").textContent=`${d.country.name} · 总览仪表盘`;
  document.getElementById("dash-summary").textContent=d.macro_summary;
  const kb=document.getElementById("dash-kpis"); kb.innerHTML="";
  d.kpis.forEach(k=>{
    const delta=k.value-k.prev, rising=delta>=0;
    const fav=(k.good==="up"&&rising)||(k.good==="down"&&!rising);
    kb.insertAdjacentHTML("beforeend",
      `<div class="kpi"><div class="lab">${k.label}</div><div class="val">${k.value}${k.unit}</div>
       <div class="delta ${fav?"up":"down"}">${rising?"▲":"▼"} ${Math.abs(delta).toFixed(2)} pct</div></div>`);
  });
  document.getElementById("dash-sec-title").textContent=`行业冷热(${d.sector_year} 年度)`;
  document.getElementById("dash-sectors").innerHTML=
    `<div class="snap-row"><span>领涨 <span class="pill g">${d.sector_best.sector}</span></span><span class="up">+${d.sector_best.ret}%</span></div>`+
    `<div class="snap-row"><span>掉队 <span class="pill r">${d.sector_worst.sector}</span></span><span class="down">${d.sector_worst.ret}%</span></div>`;
  document.getElementById("dash-market").innerHTML=d.market.map(m=>
    `<div class="snap-row"><span>${m.name}</span><span class="${m.total>=0?"up":"down"}">${m.total>=0?"+":""}${m.total}% (起点=100→${m.last})</span></div>`).join("");
}

// ① Overview (country-aware)
let ov=false;
async function loadOverview(){
  if(ov) return; ov=true;
  const d=await getJSON("/api/overview?country="+curCountry);
  document.getElementById("ov-title").textContent=`① 大势 · ${d.flag} ${d.name} 宏观概览`;
  document.getElementById("ov-desc").textContent=`${d.name}宏观环境,年度数据。右上角可切换经济体。`;
  renderNarr("narr-overview",d.narrative);
  const kb=document.getElementById("kpis"); kb.innerHTML="";
  d.kpis.forEach(k=>{
    const delta=k.value-k.prev, rising=delta>=0;
    const fav=(k.good==="up"&&rising)||(k.good==="down"&&!rising);
    kb.insertAdjacentHTML("beforeend",
      `<div class="kpi"><div class="lab">${k.label}</div><div class="val">${k.value}${k.unit}</div>
       <div class="delta ${fav?"up":"down"}">${rising?"▲":"▼"} ${Math.abs(delta).toFixed(2)} pct vs 上期</div></div>`);
  });
  const y=d.macro.years;
  draw("c-gdp",lineCfg(y,d.macro.gdp_growth,"#4fd1a5",true));
  draw("c-cpi",lineCfg(y,d.macro.inflation,"#ffb454",true));
  draw("c-unemp",lineCfg(y,d.macro.unemployment,"#6cb6ff",true));
  draw("c-rate",lineCfg(y,d.macro.policy_rate,"#d2a8ff",true));
}

// ① Global compare
let cmpInit=false;
const METRIC_LABEL={gdp_growth:"GDP 实际增速 (%)",inflation:"CPI 通胀率 (%)",
  unemployment:"失业率 (%)",policy_rate:"政策利率 (%)"};
function initCompare(){
  if(cmpInit) return; cmpInit=true;
  document.getElementById("sel-metric").onchange=loadCompare;
  loadCompare();
}
async function loadCompare(){
  const metric=document.getElementById("sel-metric").value;
  const d=await getJSON("/api/compare?metric="+metric);
  const pal=["#4fd1a5","#ff7b72","#6cb6ff","#ffb454"];
  document.getElementById("cmp-title").textContent=METRIC_LABEL[metric]+" · 中美欧日对比";
  draw("c-compare",{type:"line",data:{labels:d.years,datasets:d.series.map((s,i)=>({
    label:`${s.flag} ${s.name}`,data:s.values,borderColor:pal[i%pal.length],
    backgroundColor:"transparent",borderWidth:2,pointRadius:2,tension:0.3}))},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
    plugins:{legend:{labels:{color:TICK,boxWidth:12}}},scales:axes()}});
  // latest-year ranking
  const li=d.years.length-1;
  const ranked=d.series.map(s=>({name:`${s.flag} ${s.name}`,v:s.values[li]}))
    .sort((a,b)=>b.v-a.v);
  let html=`<table><tr><th>排名</th><th>经济体</th><th>${d.years[li]} 值 (%)</th></tr>`;
  ranked.forEach((r,i)=>html+=`<tr><td>${i+1}</td><td>${r.name}</td><td>${r.v}</td></tr>`);
  html+="</table>";
  document.getElementById("cmp-rank").innerHTML=html;
  renderNarr("narr-compare",d.narrative);
}

// ② Actors & events
let ac=false;
async function loadActors(){
  if(ac) return; ac=true;
  const d=await getJSON("/api/actors");
  const ab=document.getElementById("actors-box"); ab.innerHTML="";
  d.actors.forEach(a=>ab.insertAdjacentHTML("beforeend",
    `<div class="actor"><span class="nm">${a.name}</span><span class="rl">${a.role}</span><div class="nt">${a.note}</div></div>`));
  const eb=document.getElementById("events-box"); eb.innerHTML="";
  d.events.slice().reverse().forEach(e=>eb.insertAdjacentHTML("beforeend",
    `<div class="tl-item"><span class="yr">${e.year}</span><span class="ty">${e.type}</span>
     <div style="margin-top:2px"><b>${e.title}</b></div><div class="nt" style="color:#8fb3ad;font-size:12px">${e.detail}</div></div>`));
  renderNarr("narr-actors",d.narrative);
}

// ③ Industry — GICS sector rotation
let ind=false;
function quiltColor(v){ if(v>=30) return "#1b6e58"; if(v>=15) return "#2e8b7f";
  if(v>=0) return "#3a5d57"; if(v>=-15) return "#7a3a3a"; return "#a33"; }
async function loadIndustry(){
  if(ind) return; ind=true;
  const d=await getJSON("/api/sectors");
  // KPI: latest best/worst + spread
  const kb=document.getElementById("sector-kpis"); kb.innerHTML="";
  kpi(kb,`${d.latest_year} 领涨`,d.latest_best.sector,"","up");
  kpi(kb,`${d.latest_year} 涨幅`,d.latest_best.ret,"%","up");
  kpi(kb,`${d.latest_year} 掉队`,d.latest_worst.sector,"","down");
  kpi(kb,`${d.latest_year} 跌幅`,d.latest_worst.ret,"%","down");
  // bar: latest-year ranking
  const latest=d.rankings[0];
  document.getElementById("sec-bar-title").textContent=`${latest.year} 行业年度回报排行 (%)`;
  const labels=latest.ranked.map(r=>r.sector), vals=latest.ranked.map(r=>r.ret);
  draw("c-sector",{type:"bar",data:{labels,datasets:[{data:vals,
    backgroundColor:vals.map(v=>v>=0?"#2e8b7f":"#a35")}]},
    options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false}},scales:axes()}});
  // quilt table: columns = years, rank rows
  const n=Object.keys(d.sectors).length;
  let html="<table><tr><th>排名</th>"+d.rankings.map(r=>`<th>${r.year}</th>`).join("")+"</tr>";
  for(let rank=0;rank<n;rank++){
    html+=`<tr><td>${rank+1}</td>`+d.rankings.map(r=>{
      const cell=r.ranked[rank];
      return `<td style="background:${quiltColor(cell.ret)};color:#eafff8;font-size:12px">
        ${cell.sector}<br><b>${cell.ret}%</b></td>`;
    }).join("")+"</tr>";
  }
  html+="</table>";
  document.getElementById("sector-quilt").innerHTML=html;
  renderNarr("narr-sectors",d.narrative);
}

// ③ Stock
let stockInit=false;
async function initStock(){
  if(stockInit) return; stockInit=true;
  const items=await getJSON("/api/instruments");
  const sel=document.getElementById("sel-stock");
  items.forEach(it=>{ const o=document.createElement("option"); o.value=it.id; o.textContent=it.name; sel.appendChild(o); });
  document.getElementById("btn-apply").onclick=loadStock;
  await loadStock();
}
async function loadStock(){
  const id=document.getElementById("sel-stock").value;
  const fast=document.getElementById("in-fast").value, slow=document.getElementById("in-slow").value;
  const d=await getJSON(`/api/stock?id=${encodeURIComponent(id)}&fast=${fast}&slow=${slow}`);
  if(d.error){ alert(d.error); return; }
  const s=d.stats;
  const b1=document.getElementById("stock-stats-1"); b1.innerHTML="";
  kpi(b1,"最新收盘",s.last_close,"");
  kpi(b1,"累计收益",s.cum_return,"%",s.cum_return>=0?"up":"down");
  kpi(b1,"年化收益",s.ann_return,"%",s.ann_return>=0?"up":"down");
  kpi(b1,"最大回撤",s.max_drawdown,"%","down");
  const b2=document.getElementById("stock-stats-2"); b2.innerHTML="";
  kpi(b2,"年化波动",s.ann_vol,"%");
  kpi(b2,"夏普比率",s.sharpe,"",s.sharpe>=0?"up":"down");
  kpi(b2,"索提诺",s.sortino,"",s.sortino>=0?"up":"down");
  kpi(b2,"胜率",s.win_rate,"%");
  draw("c-price",{type:"line",data:{labels:d.dates,datasets:[
    {label:"收盘价",data:d.close,borderColor:"#4fd1a5",borderWidth:2,pointRadius:2,tension:0.3},
    {label:`快线 MA${d.fast}`,data:d.fast_ma,borderColor:"#ffb454",borderWidth:1.5,pointRadius:0,tension:0.3,spanGaps:true},
    {label:`慢线 MA${d.slow}`,data:d.slow_ma,borderColor:"#6cb6ff",borderWidth:1.5,pointRadius:0,tension:0.3,spanGaps:true},
  ]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
    plugins:{legend:{labels:{color:TICK,boxWidth:12}}},scales:axes()}});
  draw("c-vol",{type:"bar",data:{labels:d.dates,datasets:[{data:d.volume,backgroundColor:"#2e8b7f"}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:axes()}});
  draw("c-ret",lineCfg(d.dates,d.returns,"#d2a8ff",false));
  renderNarr("narr-stock",d.narrative);
}

// market + correlation
let mk=false;
function corrColor(v){ if(v===null) return "#11313a";
  // -1 red .. 0 grey .. +1 green
  if(v>=0) return `rgba(79,209,165,${0.15+0.6*v})`;
  return `rgba(255,123,114,${0.15+0.6*Math.abs(v)})`; }
async function loadMarket(){
  if(mk) return; mk=true;
  const d=await getJSON("/api/market");
  const pal=["#4fd1a5","#ffb454","#6cb6ff","#ff7b72"];
  draw("c-market",{type:"line",data:{labels:d.dates,datasets:d.series.map((s,i)=>({
    label:s.name,data:s.values,borderColor:pal[i%pal.length],backgroundColor:"transparent",
    borderWidth:2,pointRadius:2,tension:0.3,spanGaps:true}))},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
    plugins:{legend:{labels:{color:TICK,boxWidth:12}}},scales:axes()}});
  const c=await getJSON("/api/correlation");
  let html="<table><tr><th></th>"+c.names.map(n=>`<th>${n}</th>`).join("")+"</tr>";
  c.matrix.forEach((row,i)=>{
    html+=`<tr><th>${c.names[i]}</th>`+row.map(v=>
      `<td style="background:${corrColor(v)};color:#0b1f24;font-weight:600">${v===null?"–":v}</td>`).join("")+"</tr>";
  });
  html+="</table>";
  document.getElementById("corr-table").innerHTML=html;
  document.getElementById("corr-note").textContent=
    `基于 ${c.points} 个对齐交易日的日收益 Pearson 相关。绿=正相关(同涨同跌),红=负相关(可分散风险)。`;
}

// backtest
let btInit=false;
function initBacktest(){
  if(btInit) return; btInit=true;
  document.getElementById("btn-bt").onclick=runBacktest;
  runBacktest();
}
async function runBacktest(){
  const strat=document.getElementById("sel-strat").value;
  const fast=document.getElementById("bt-fast").value, slow=document.getElementById("bt-slow").value;
  const cash=document.getElementById("bt-cash").value;
  const d=await getJSON(`/api/backtest?strategy=${strat}&fast=${fast}&slow=${slow}&cash=${cash}`);
  if(d.error){ alert(d.error); return; }
  const m=d.metrics;
  const b=document.getElementById("bt-stats"); b.innerHTML="";
  kpi(b,"期末权益",Math.round(m.final_equity),"");
  kpi(b,"总收益",(m.total_return*100).toFixed(2),"%",m.total_return>=0?"up":"down");
  kpi(b,"最大回撤",(m.max_drawdown*100).toFixed(2),"%","down");
  kpi(b,"交易笔数",m.trade_count,"");
  draw("c-equity",{type:"line",data:{labels:d.curve.map(p=>p.date),datasets:[
    {label:"权益",data:d.curve.map(p=>p.equity),borderColor:"#4fd1a5",backgroundColor:"#4fd1a522",
     borderWidth:2,pointRadius:2,tension:0.3,fill:true}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:axes()}});
}

// nav
const views={dashboard:loadDashboard,framework:()=>{},overview:loadOverview,compare:initCompare,
  actors:loadActors,industry:loadIndustry,stock:initStock,market:loadMarket,backtest:initBacktest};
const allViews=["dashboard","framework","overview","compare","actors","industry","stock","market","backtest"];
document.querySelectorAll("nav button").forEach(btn=>{
  btn.onclick=()=>{
    document.querySelectorAll("nav button").forEach(x=>x.classList.remove("active"));
    btn.classList.add("active");
    const v=btn.dataset.view;
    allViews.forEach(x=>document.getElementById("view-"+x).classList.toggle("hidden",x!==v));
    views[v]();
  };
});
initCountries().then(loadDashboard);
</script>
</body>
</html>
"""


def _iter_py_files(root: Path):
    """Yield all .py files under the package source tree."""
    for path in root.rglob("*.py"):
        if "__pycache__" not in path.parts:
            yield path


def _snapshot(root: Path) -> Dict[str, float]:
    """Map each .py file to its modification time."""
    snap = {}
    for path in _iter_py_files(root):
        try:
            snap[str(path)] = path.stat().st_mtime
        except OSError:
            pass
    return snap


def _watch_and_reexec(watch_root: Path, interval: float = 1.0) -> None:
    """Block, polling for .py changes; re-exec the whole process on any change.

    Pure standard library — no watchdog dependency. The server itself runs in a
    daemon thread, so re-exec cleanly replaces this process with a fresh one.
    """
    baseline = _snapshot(watch_root)
    while True:
        time.sleep(interval)
        current = _snapshot(watch_root)
        if current != baseline:
            changed = sorted(set(current) ^ set(baseline)) or [
                p for p in current if current[p] != baseline.get(p)
            ]
            name = Path(changed[0]).name if changed else "源码"
            print(f"\n检测到改动 ({name}),正在重启…")
            # replace the current process image with a fresh interpreter run
            os.execv(sys.executable, [sys.executable, "-m", "auto_invest.apps.data_server", *sys.argv[1:]])


def main() -> None:
    parser = argparse.ArgumentParser(description="Local data-explorer web server.")
    parser.add_argument("--bars", default="data/sample/bars.csv", help="path to bars CSV")
    parser.add_argument(
        "--instruments",
        default="configs/universe.example.json",
        help="path to instrument universe JSON (used by the backtest view)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="改动 .py 源码后自动重启服务(热重载)。数据文件 CSV 本就每次请求重读,刷新即生效。",
    )
    args = parser.parse_args()

    bars_path = Path(args.bars)
    instruments_path = Path(args.instruments)
    if not bars_path.exists():
        raise SystemExit(f"bars file not found: {bars_path}")
    if not instruments_path.exists():
        raise SystemExit(f"instruments file not found: {instruments_path}")

    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(bars_path, instruments_path)
    )
    url = f"http://{args.host}:{args.port}"
    mode = " · 热重载已开启" if args.reload else ""
    print(f"Auto Invest 数据系统已启动: {url}{mode}  (Ctrl-C 停止)")

    if args.reload:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        watch_root = Path(__file__).resolve().parents[1]  # src/auto_invest
        try:
            _watch_and_reexec(watch_root)
        except KeyboardInterrupt:
            print("\n已停止。")
            server.shutdown()
    else:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
            server.shutdown()


if __name__ == "__main__":
    main()
