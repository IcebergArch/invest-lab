"""Generate a self-contained HTML dashboard of a country's economy.

The dashboard combines two layers:

* macro indicators (GDP growth, CPI inflation, unemployment, policy rate)
* market data read from the project sample bars (normalised to 100)

Run::

    PYTHONPATH=src python3 -m auto_invest.apps.economy_dashboard \
        --bars data/sample/bars.csv \
        --out dashboards/us_economy.html

Then open the generated HTML file in any browser.

Macro figures are annual values for the United States, sourced from
BEA (GDP), BLS (CPI / unemployment) and the Federal Reserve (policy rate).
Replace the ``MACRO`` block below to refresh or switch country.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# ---------------------------------------------------------------------------
# Macro data for major economies, annual values for 2016-2025.
# GDP growth = real GDP YoY %. Inflation = headline CPI/HICP YoY %.
# Unemployment = annual rate %. Policy rate = central bank rate, approx year-end %.
# Sources: US — BEA/BLS/Fed; China — NBS/World Bank/IMF; Euro area — Eurostat/ECB;
# Japan — Cabinet Office/Statistics Bureau/BOJ. Some 2016-2019 values are
# authoritative approximations; latest years are the most precise.
# ---------------------------------------------------------------------------
YEARS = [2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]

COUNTRIES = {
    "US": {
        "name": "美国",
        "flag": "🇺🇸",
        "years": YEARS,
        "gdp_growth": [1.8, 2.5, 3.0, 2.6, -2.2, 5.9, 2.5, 2.9, 2.8, 2.1],
        "inflation": [2.1, 2.1, 1.9, 2.3, 1.4, 7.0, 6.5, 3.4, 2.9, 2.7],
        "unemployment": [4.9, 4.4, 3.9, 3.7, 8.1, 5.4, 3.6, 3.6, 4.0, 4.2],
        "policy_rate": [0.55, 1.30, 2.40, 1.55, 0.09, 0.08, 4.10, 5.33, 4.33, 3.83],
    },
    "CN": {
        "name": "中国",
        "flag": "🇨🇳",
        "years": YEARS,
        "gdp_growth": [6.8, 6.9, 6.7, 6.0, 2.2, 8.6, 3.0, 5.4, 5.0, 5.0],
        "inflation": [2.0, 1.6, 2.1, 2.9, 2.5, 0.9, 2.0, 0.2, 0.2, 0.1],
        "unemployment": [4.0, 3.9, 3.8, 3.6, 4.2, 4.0, 5.5, 5.2, 5.1, 5.2],
        "policy_rate": [4.35, 4.35, 4.35, 4.15, 3.85, 3.80, 3.65, 3.45, 3.10, 3.00],
    },
    "EU": {
        "name": "欧元区",
        "flag": "🇪🇺",
        "years": YEARS,
        "gdp_growth": [1.9, 2.6, 1.8, 1.6, -6.1, 5.9, 3.4, 0.4, 0.9, 1.4],
        "inflation": [0.2, 1.5, 1.8, 1.2, 0.3, 2.6, 8.4, 5.4, 2.4, 2.1],
        "unemployment": [10.0, 9.1, 8.2, 7.6, 8.0, 7.7, 6.7, 6.6, 6.4, 6.3],
        "policy_rate": [-0.40, -0.40, -0.40, -0.50, -0.50, -0.50, 2.00, 4.00, 3.00, 2.00],
    },
    "JP": {
        "name": "日本",
        "flag": "🇯🇵",
        "years": YEARS,
        "gdp_growth": [0.8, 1.7, 0.6, -0.4, -4.2, 2.6, 1.0, 1.5, 0.1, 1.1],
        "inflation": [-0.1, 0.5, 1.0, 0.5, 0.0, -0.2, 2.5, 3.3, 2.7, 2.9],
        "unemployment": [3.1, 2.8, 2.4, 2.4, 2.8, 2.8, 2.6, 2.6, 2.5, 2.5],
        "policy_rate": [-0.10, -0.10, -0.10, -0.10, -0.10, -0.10, -0.10, -0.10, 0.25, 0.75],
    },
}

# 中文指标标签 + 方向(good 表示哪个方向是有利的)
MACRO_METRICS = [
    {"key": "gdp_growth", "label": "GDP 增速", "good": "up"},
    {"key": "inflation", "label": "CPI 通胀", "good": "down"},
    {"key": "unemployment", "label": "失业率", "good": "down"},
    {"key": "policy_rate", "label": "政策利率", "good": "down"},
]


def country_payload(code: str) -> dict:
    """Build the macro + KPI payload for one country."""
    c = COUNTRIES[code]
    macro = {
        "years": c["years"],
        "gdp_growth": c["gdp_growth"],
        "inflation": c["inflation"],
        "unemployment": c["unemployment"],
        "policy_rate": c["policy_rate"],
    }
    kpis = []
    for m in MACRO_METRICS:
        series = c[m["key"]]
        kpis.append({
            "label": m["label"],
            "value": series[-1],
            "prev": series[-2],
            "unit": "%",
            "good": m["good"],
        })
    return {"code": code, "name": c["name"], "flag": c["flag"], "macro": macro, "kpis": kpis}


# Backward-compatible aliases (used by economy_dashboard static page).
COUNTRY = COUNTRIES["US"]["name"]
MACRO = country_payload("US")["macro"]
KPIS = country_payload("US")["kpis"]

# ---------------------------------------------------------------------------
# Industry / sector layer: S&P 500 GICS 11-sector annual total return (%).
# Lets the user see "where the economy is going" by sector rotation.
# Sources: RBC Wealth Mgmt, S&P Dow Jones, Novel Investor. 2021 figures are
# approximations; 2022-2025 are well-documented.
# ---------------------------------------------------------------------------
SECTOR_YEARS = [2022, 2023, 2024, 2025]
SECTORS = {
    "信息技术": [-28.2, 57.8, 36.6, 24.0],
    "通信服务": [-39.9, 55.8, 39.7, 33.7],
    "工业": [-5.5, 18.1, 17.3, 19.4],
    "公用事业": [1.6, -7.1, 20.1, 16.0],
    "金融": [-10.5, 12.1, 30.5, 15.0],
    "医疗健康": [-2.0, 2.1, 2.6, 14.6],
    "能源": [65.4, -1.3, 5.6, 8.3],
    "可选消费": [-37.0, 42.4, 30.1, 6.0],
    "必需消费": [-0.6, 0.5, 14.9, 3.9],
    "房地产": [-26.1, 12.4, 5.2, 3.2],
    "材料": [-12.3, 12.6, -0.0, -10.5],
}

NAME_BY_ID = {
    "CN_A:000001.SZ": "平安银行 (CN)",
    "HK:00700.HK": "腾讯 (HK)",
    "US:AAPL": "苹果 (US)",
    "CRYPTO:BTC-USD": "比特币",
}


def load_market(bars_path: Path) -> Dict[str, object]:
    """Read bars.csv and return normalised (base=100) close-price series."""
    rows_by_id: Dict[str, List[tuple]] = defaultdict(list)
    with bars_path.open("r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            rows_by_id[row["instrument_id"]].append((ts, float(row["close"])))

    # Use a common, sorted set of dates (date part only) across instruments.
    all_dates = sorted({ts.date().isoformat() for series in rows_by_id.values() for ts, _ in series})

    series_out = []
    for instrument_id, series in sorted(rows_by_id.items()):
        series.sort(key=lambda x: x[0])
        by_date = {ts.date().isoformat(): close for ts, close in series}
        base = series[0][1]
        values = []
        last = None
        for d in all_dates:
            if d in by_date:
                last = round(by_date[d] / base * 100, 2)
            values.append(last)
        series_out.append({
            "id": instrument_id,
            "name": NAME_BY_ID.get(instrument_id, instrument_id),
            "values": values,
        })

    return {"dates": all_dates, "series": series_out}


def build_html(macro: dict, kpis: list, market: dict, country: str) -> str:
    payload = json.dumps(
        {"macro": macro, "kpis": kpis, "market": market, "country": country},
        ensure_ascii=False,
    )
    return _TEMPLATE.replace("__PAYLOAD__", payload).replace("__COUNTRY__", country)


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__COUNTRY__ · 经济情况面板</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px;
    background: #0b1f24;
    color: #e7f3f1;
    font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  h1 { font-size: 22px; margin: 0 0 4px; font-weight: 650; }
  .sub { color: #6f8f8a; font-size: 13px; margin-bottom: 24px; }
  .kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 28px; }
  .kpi {
    background: #11313a; border: 1px solid #1d4750; border-radius: 12px; padding: 16px 18px;
  }
  .kpi .lab { color: #8fb3ad; font-size: 13px; }
  .kpi .val { font-size: 28px; font-weight: 680; margin-top: 6px; }
  .kpi .delta { font-size: 12px; margin-top: 4px; }
  .up { color: #4fd1a5; }
  .down { color: #ff7b72; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .card {
    background: #11313a; border: 1px solid #1d4750; border-radius: 12px; padding: 18px 18px 8px;
  }
  .card h2 { font-size: 15px; margin: 0 0 12px; font-weight: 600; color: #cfe9e4; }
  .card.wide { grid-column: 1 / -1; }
  .chart-wrap { position: relative; height: 260px; }
  .foot { color: #5d7d78; font-size: 11px; margin-top: 22px; line-height: 1.6; }
  @media (max-width: 860px) {
    .kpis, .grid { grid-template-columns: 1fr 1fr; }
  }
</style>
</head>
<body>
  <h1>__COUNTRY__ · 经济情况面板</h1>
  <div class="sub">宏观指标(年度) + 市场表现(归一化至 100) · 数据来源见页脚</div>
  <div class="kpis" id="kpis"></div>
  <div class="grid">
    <div class="card"><h2>GDP 实际增速 (%)</h2><div class="chart-wrap"><canvas id="gdp"></canvas></div></div>
    <div class="card"><h2>CPI 通胀率 (%)</h2><div class="chart-wrap"><canvas id="cpi"></canvas></div></div>
    <div class="card"><h2>失业率 (%)</h2><div class="chart-wrap"><canvas id="unemp"></canvas></div></div>
    <div class="card"><h2>政策利率 (%)</h2><div class="chart-wrap"><canvas id="rate"></canvas></div></div>
    <div class="card wide"><h2>市场表现 · 收盘价归一化 (起点=100)</h2><div class="chart-wrap" style="height:320px"><canvas id="market"></canvas></div></div>
  </div>
  <div class="foot">
    宏观数据为美国年度值,口径来源:GDP 增速 (BEA)、CPI 通胀 (BLS,年末同比)、失业率 (BLS,年均)、政策利率 (Federal Reserve / FRED,年末近似)。
    市场曲线由项目 data/sample/bars.csv 的收盘价归一化生成,仅用于演示。
  </div>
<script>
const DATA = __PAYLOAD__;
const GRID = "rgba(120,160,155,0.15)";
const TICK = "#8fb3ad";
const LINE = "#4fd1a5";

Chart.defaults.color = TICK;
Chart.defaults.font.family = "-apple-system, 'PingFang SC', sans-serif";

function lineChart(id, labels, values, color) {
  new Chart(document.getElementById(id), {
    type: "line",
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: color,
        backgroundColor: color + "22",
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 4,
        tension: 0.3,
        fill: true,
        spanGaps: true,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: GRID }, ticks: { color: TICK } },
        y: { grid: { color: GRID }, ticks: { color: TICK } },
      },
    },
  });
}

// KPI cards
const kbox = document.getElementById("kpis");
DATA.kpis.forEach(k => {
  const delta = (k.value - k.prev);
  const rising = delta >= 0;
  // "good" = which direction is favourable; colour accordingly
  const favourable = (k.good === "up" && rising) || (k.good === "down" && !rising);
  const cls = favourable ? "up" : "down";
  const arrow = rising ? "▲" : "▼";
  kbox.insertAdjacentHTML("beforeend", `
    <div class="kpi">
      <div class="lab">${k.label}</div>
      <div class="val">${k.value}${k.unit}</div>
      <div class="delta ${cls}">${arrow} ${Math.abs(delta).toFixed(2)} pct vs 上期</div>
    </div>`);
});

const yrs = DATA.macro.years;
lineChart("gdp",   yrs, DATA.macro.gdp_growth,   "#4fd1a5");
lineChart("cpi",   yrs, DATA.macro.inflation,    "#ffb454");
lineChart("unemp", yrs, DATA.macro.unemployment, "#6cb6ff");
lineChart("rate",  yrs, DATA.macro.policy_rate,  "#d2a8ff");

// Market multi-line chart
const palette = ["#4fd1a5", "#ffb454", "#6cb6ff", "#ff7b72"];
new Chart(document.getElementById("market"), {
  type: "line",
  data: {
    labels: DATA.market.dates,
    datasets: DATA.market.series.map((s, i) => ({
      label: s.name,
      data: s.values,
      borderColor: palette[i % palette.length],
      backgroundColor: "transparent",
      borderWidth: 2,
      pointRadius: 2,
      tension: 0.3,
      spanGaps: true,
    })),
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { labels: { color: TICK, boxWidth: 12 } } },
    scales: {
      x: { grid: { color: GRID }, ticks: { color: TICK, maxRotation: 0 } },
      y: { grid: { color: GRID }, ticks: { color: TICK } },
    },
  },
});
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an economy dashboard HTML page.")
    parser.add_argument("--bars", default="data/sample/bars.csv", help="path to bars CSV")
    parser.add_argument("--out", default="dashboards/us_economy.html", help="output HTML path")
    args = parser.parse_args()

    bars_path = Path(args.bars)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    market = load_market(bars_path)
    html = build_html(MACRO, KPIS, market, COUNTRY)
    out_path.write_text(html, encoding="utf-8")
    print(f"Dashboard written to {out_path} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
