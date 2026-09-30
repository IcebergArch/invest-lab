# 2000 年起沪深股票日线归档

`quant_lab.baostock_backfill` 是**限量、可续跑**的原始日线归档器。归档库必须独立于 `data/quant/market.sqlite3`：主库的既有前复权股票来自腾讯/东方财富，不能按相同股票日期主键把原始价或其他提供方的价格直接覆盖进去。

BaoStock 是可选依赖。首次使用时，在项目根目录准备独立的 Python 环境：

```bash
python3 -m venv .venv-history
.venv-history/bin/python -m pip install 'baostock==0.9.4' 'pandas>=2,<3'
```

## 股票清单与可覆盖范围

先通过 BaoStock 的 `query_stock_basic()` 保存当下返回的沪深 A 股目录，包括 `status=1` 在市和 `status=0` 已退出条目，同时记录 `ipoDate`、`outDate`、供应商版本、查询时间及原始列表哈希。归档器用上市/退出日期缩小请求范围。北交所代码单独计数并排除；其他非沪深 A 股条目也单独计数。目录仍是**查询当下的供应商元数据**，不能替代历史每日股票池；历史回测还需逐日成分、退市处理、公司行动及当时可得数据版本。`query_stock_basic()` 返回字段需以实际响应核对。[BaoStock 官方包和日线调用示例](https://pypi.org/project/baostock/)。

```bash
PYTHONPATH=src .venv-history/bin/python -m quant_lab.baostock_backfill universe \
  --out reports/quant/snapshots/baostock-historical-shsz.json
```

2026-09-29 首次实时 BaoStock 登录返回 `10002007 网络接收错误`；随后单次重试成功。从 `query_stock_basic()` 的 **8,983** 个原始条目中筛得沪深 A 股 **5,559** 个代码，其中供应商 `status=1` 的 **5,222** 个，`status=0` 的 **337** 个。全部 5,559 个条目有 `ipoDate`，337 个退出条目有 `outDate`。快照保存在 `reports/quant/snapshots/2026-09-29-baostock-historical-shsz-590b41a3bd0a.json`，内容 ID 已复读校验。该响应未返回北交所股票，不能据此断言北交所没有股票。原来的 5,222 只快照仅是当前在市沪深股票；若显式使用 `--allow-current-only`，归档范围会缺少退出条目。

## 运行方法

先看无需联网的计划；`plan` 会在独立归档库建立快照与断点表，但不会抓行情。

```bash
PYTHONPATH=src .venv-history/bin/python -m quant_lab.baostock_backfill plan \
  --snapshot reports/quant/snapshots/2026-09-29-baostock-historical-shsz-590b41a3bd0a.json \
  --db data/quant/historical-baostock-raw.sqlite3 \
  --start 2000-01-01 --end 2026-09-28 --stock stock:600000.SH
```

一次运行默认最多 **10 个三年日历窗查询**（从 2000–2002 起每三年对齐），同一 BaoStock 登录会话复用，日线查询间隔至少 **1.5 秒**；每个响应最多接收 1,000 行。三年内最多约 784 个工作日，仍受 1,000 行硬限保护。可用 `--max-windows` 调整到 1–50，`--interval-seconds` 下限 1 秒。出现一次源错误立即停止，本次成功窗口保留，失败窗口记错误与时间，默认一小时后才可重试；必要时可显式传 `--retry-failed-after-seconds 0` 对修复后的单个失败窗口立即重试。再次运行自动跳过已成功或明确空结果的区间，只请求缺失日期；当年新增交易日只形成增量尾段。原有年度断点会按日期从新三年窗中扣除。BaoStock 未公开适用于本项目的稳定请求频率阈值，1.5 秒是本地默认值，**不能保证供应商不限制访问**。可先以 `--max-windows 10 --interval-seconds 3` 串行核对，按实际错误率再调整。

```bash
PYTHONPATH=src .venv-history/bin/python -m quant_lab.baostock_backfill run \
  --snapshot reports/quant/snapshots/2026-09-29-baostock-historical-shsz-590b41a3bd0a.json \
  --db data/quant/historical-baostock-raw.sqlite3 \
  --start 2000-01-01 --end 2026-09-28 \
  --max-windows 10 --interval-seconds 3
```

本地脚本将上述限量运行串行执行，默认最多 10 批、每批 10 个三年窗口；同批请求间隔至少 3 秒，两批之间暂停 30 秒。任一批遇到来源错误，脚本立即退出；再次运行会从成功或空结果以外的缺口续跑。它只负责历史归档，**不属于**只启动 Docker 服务的 `scripts/deploy-local-docker.sh`。先用一批核对来源响应和入库状态：

```bash
BACKFILL_PYTHON=.venv-history/bin/python BACKFILL_BATCHES=1 ./scripts/backfill-history-local.sh
PYTHONPATH=src .venv-history/bin/python -m quant_lab.baostock_backfill status \
  --db data/quant/historical-baostock-raw.sqlite3
```

核对后省略 `BACKFILL_BATCHES=1` 即运行默认上限。可用 `BACKFILL_SNAPSHOT`、`BACKFILL_DB`、`BACKFILL_INTERVAL_SECONDS`、`BACKFILL_BATCH_PAUSE_SECONDS`、`BACKFILL_END_DATE` 指定清单、归档库、节流和截止日。不要并行启动多个回补进程。

需要长时间续跑时，可以单独启动 Docker 回补容器。它每批最多 10 个窗口，批间暂停 30 秒；每个已完成窗口有断点，首次数据源错误会停止容器，不会自动重试。它与只启动量化 API 和可视化服务的脚本分离：

```bash
./scripts/start-history-worker.sh
docker compose -f compose.quant-history.yaml logs -f history-backfill
```

要暂停回补，用 `docker compose -f compose.quant-history.yaml stop history-backfill`；再次运行启动脚本会从断点续跑。页面只读每批发布的覆盖状态。主库的个股报告与荐股不会自动使用这份未复权归档。

BaoStock 不可用时，可显式选择 `--source eastmoney`。它沿用同一股票目录，但需要**独立的东方财富归档库**；提供原始开高低收和人民币成交额，未提供 BaoStock 的 `tradestatus`、`isST` 审计行。东方财富和 BaoStock 两种来源不能写到同一个归档库。历史清单示例：

```bash
PYTHONPATH=src .venv-history/bin/python -m quant_lab.baostock_backfill run \
  --snapshot reports/quant/snapshots/2026-09-29-baostock-historical-shsz-590b41a3bd0a.json \
  --source eastmoney \
  --db data/quant/historical-raw.sqlite3 \
  --start 2000-01-01 --end 2001-12-31 \
  --stock stock:600000.SH --max-windows 2 --interval-seconds 3
```

`backfill_windows` 以来源、股票、复权口径、窗口起止日期为断点键，保存成功、空结果或失败、尝试次数、行数、来源清单 ID、运行 ID 与错误。`daily_bars` 保存来源、原始响应哈希、运行 ID、抓取时间；BaoStock 的交易/ST状态保存到 `baostock_daily_status`。每个窗口的行情、状态与断点在同一 SQLite 事务中提交。`empty` 表示该查询无行，**不是已证明该区间没有交易**；需后续交易日历对账。`success` 表示供应商响应按结构与范围入库，仍需检查交易日缺口和异常价。

只读查看清单范围、已入库股票数/行情行数/日期范围、窗口状态和最新运行 ID：

```bash
PYTHONPATH=src .venv-history/bin/python -m quant_lab.baostock_backfill status \
  --db data/quant/historical-baostock-raw.sqlite3
```

`status` 仅执行只读查询；SQLite 以可写模式打开已存在的 WAL 库，以便缺少附属文件时创建 WAL sidecar。数据库不存在时返回错误，不建立新库。它报告当前存量，不能以成功窗口数替代逐交易日缺口检查。回补脚本每批结束还会原子发布 `reports/quant/backfill/historical-archive-status.json` 供页面读取，页面不会直接访问归档数据库。

## 实测边界

2026-09-29，东方财富 `600000.SH`、2000-01-01 至 2001-12-31、日频、`fqt=0` 原始价，返回 `rc=0`、**475 行**，首日 2000-01-04，末日 2001-12-31；每行成交额可解析。同区间 BaoStock 原始价返回 **479 行**，多出的 4 行是停牌状态、成交额不可用；475 个重叠收盘价逐日完全一致。两源部分日期成交额差异明显，最大绝对差约 **4,047.6 万元**，因此归档以 BaoStock 为主，并把早期成交额口径差异留待核查。此实测仅证明该股票该区间；不证明全市场、退市股票或北交所覆盖。

对上述 5,559 个沪深代码按上市/退出日期规划至 2026-09-28，年度分窗需 **76,795** 次查询；三年分窗需 **27,350** 次，理论请求数减少约 **64.4%**。这只是规划量，不是已完成数据量。现有年度断点可以复用，少数剩余三年窗会缩短成缺失日期区间，不重抓成功日期。

长期回测不能直接对原始收盘价算跨分红、拆股的投资收益。先归档公司行动、建立当时可得的复权/总回报序列及版本，再做时间外验证。东方财富当前锚定的前复权历史价在 2000 年样本中出现非正值，不能静默删行或与原始价混成一条序列；原始价 `fqt=0` 作为长期底层归档口径。

## 2026-09-30 现场覆盖复核

以下为 2026-09-30 四股定向回补**之前**的存量快照。只读查询 `data/quant/historical-baostock-raw.sqlite3`，`PRAGMA quick_check=ok`。`daily_bars` 与 `baostock_daily_status` 均为 696,200 行，按股票和日期一一对应，来源哈希、运行 ID、抓取时间也对齐。2000–2004 年共 2,158 行；2005–2023 年为 0 行。2024、2025、2026 年（末年截至 9 月 28 日）分别有 249,990、253,295、190,757 行，但 2021 年以后的记录**全部为深市**，沪市为 0；逐年股票数分别为 1,036、1,051、1,070。已完成 1,075 个成功归档窗口不等于 2021 年以来市场覆盖完成。

2024–2026 年的记录有未复权 OHLC、`share` 单位成交量、状态表中的 `tradestatus` 和 `is_st`。三年的正量行分别为 249,726、252,876、190,468；其余 264、419、289 行为零量且与停牌状态计数相同。适配器会将来源缺失成交量规范化为零，零量本身不能当作来源原始值。`daily_bars.amount` 的人民币元成交额非空行分别为 241,372、241,741、180,867；余下行不能直接用这个规范化列计算 Amihud 因子。部分 ST 交易日的原始成交额仍保存在状态表 `raw_amount_cny`，须将金额可得性与交易资格分开处理。当前 `DAILY_FIELDS` 未请求 BaoStock 已公开可用的 `preclose`；本地字段和表也没有日涨跌停价或公司行动现金流。ST 标记行分别为 8,454、11,196、9,694（与停牌可能重叠）；`is_st` 有记录只限这些已归档股票日，不能据此推断缺失日期的 ST 状态。

若仅拿 **2026 年 Qlib 发布包的月度沪深300快照**作诊断性对齐，2021 年以来快照成员并集为 459 个代码，均见于当前 BaoStock 历史清单，但回补前本地原价库只有 101 个代码在 2021 年以后出现过。回补前月度快照成员日原价覆盖：2021–2023 年为 0；2024 年 15,365/72,600（21.16%）、2025 年 15,781/72,900（21.65%）、2026 年至发布包截止日 11,341/53,700（21.12%）。这些分母只是月度快照的成员日，不是已核实的官方逐日成分，也不是可交易日的完整验证。当前数据足以对部分股票做规则筛查，不能支持 2021 年以来的宽池原价成交收益回测。

另一独立东方财富归档库 `data/quant/historical-raw.sqlite3` 与 BaoStock 在 2026 年两只深市股票的 358 个重叠股票日上，OHLC 和人民币成交额均逐行相同；东方财富成交量记录为整数 `lot`，乘 100 后与 BaoStock 的 `share` 相差 -50 至 +49 股，不能混作精确股数；具体取整算法尚未核验。该库没有 BaoStock 的停牌/ST 状态，也没有填补上述 2021–2023 年的宽池缺口。

### 四股定向回补（2026-09-30）

[官方 2021 年 6 月调样来源审计](csi300-official-source-audit.md)后，选择其中两个已核实增删配对的四只沪市股票 `600004`、`600079`、`603156`、`688111`，只请求 2021-01-01 至 2023-12-31 四个三年窗口，BaoStock 0.9.4、原价、请求间隔 1.5 秒。运行 ID 为 `2481df1b03194749bb5683bdec51a4b6`，四窗均 `success`，新增 **2,908** 条日线和同数交易状态，各股票 727 条，实际日期均为 2021-01-04 至 2023-12-29。数据库 `quick_check=ok`，行情与状态逐股票日配对无缺口，新增行成交额均有值，`tradestatus=1`、`is_st=0`；没有发现这四股在该区间的停牌/ST 行，不外推至其他股票。当时全库为 **699,108** 条日线。

按**仍有已证实时间偏差的 Qlib 月度沪深300快照**仅作覆盖诊断，2021、2022、2023 年新增可对齐的成员日分别为 486/72,900、358/72,600、242/72,600，约 0.67%、0.49%、0.33%。因此 2021–2023 年已不再是全空，但仍远不能支持 20 股组合各日原价估值与可交易性检验。这四窗只改善局部数据，并没有修复官方逐日成分、公司行动或涨跌停约束。

### 冻结的 12 个三股池定向回补（2026-09-30）

为核验既有[12 池压力研究](random-entry-baseline.md)的价格口径，直接从冻结报告取出 36 只不同股票，限量规划并完成 2021-01-01 至 2023-12-31 的 36 个 BaoStock 原价窗口。运行 ID `a80b4ab1cd8145b8bb59acb565c239d2`；36 窗均成功，新增 **26,172** 条日线和同数状态，各股票 727 条、实际日期 2021-01-04 至 2023-12-29。`quick_check=ok`，来源均为 `baostock_daily`、`adjustment=none`，逐日期状态对齐，成交额无空值；本批状态均为正常交易、非 ST。全库现有 **725,280** 条日线。

连同前述四股回补，2021、2022、2023 年分别有 9,720、9,680、9,680 条原价，涉及每年40只股票。只按存在时间偏差的 Qlib 沪深300月度快照作覆盖诊断，成员日交集分别为 **1,215/72,900（1.67%）**、**1,084/72,600（1.49%）**、**968/72,600（1.33%）**；这批36只并非按当时沪深300成分选出，分母仅用于呈现原价覆盖，不能转成真实指数股票池覆盖率。[原价与复权因子审计](qlib-raw-price-factor-audit.md)发现，26,172 个重叠股票日的 Qlib 复权收盘价几乎等于 BaoStock 原价乘 Qlib 因子，60 个超过 1% 的日收益差恰与因子跳变日对应。不能把原价直接连乘为跨公司行动的组合收益，也不能将两套价格当成独立收益验证。[三年公司行动供应商快照及逐日连接](qlib-raw-price-factor-audit.md)进一步核对了全部 60 个大于 1% 的因子跳变日；历史发布时间和完整总回报账本仍未验证。

已用这批原价、状态与公司行动记录完成[固定 80% 买入持有的配对记账压力测试](raw-action-aware-hold-stress.md)：冻结的 3,072 个早期随机入场窗口全部保留，按 100 股单位买入的初始总股票仓位可降至 65.26%。这仍不是每日 SMA 策略的原价收益，也不是可成交或独立前瞻证明。

### 同池 2024–2026 年定向补齐（2026-09-30）

沿用冻结的 36 只股票，规划原价缺口并完成 **29** 个 2024-01-01 至 2026-09-28 三年窗，运行 ID `11ad85da963f4f2ebddafab286def835`，新增 **19,256** 条日线和同数状态，29 窗均 `success`；其余 7 只股票已有覆盖窗口。核验后 36 股在 2024-01-02 至 2026-09-28 各有 664 个共同交易日，总计 **23,904** 个股票日；原价、状态、成交额、正常交易及非 ST 对齐，`quick_check=ok`。全库现有 **744,536** 条日线。不同股票使用了不同来源运行 ID，逐股来源见[较晚段重放报告](../studies/random-entry-baseline-v1/raw-action-aware-36-pool-later-stress-v1.json)。

另按操作年分别捕获 2024/2025/2026 年 BaoStock 公司行动 **31/33/24** 条。Qlib 复权收盘价与原价乘因子在上述全部股票日吻合，49 个超过 1% 的因子跳变日都有同股同日行动记录。原价、公司行动、100 股整手及买卖各万分之五费用现已用于[较晚段 3,072 个冻结窗口的三策略重放](raw-action-aware-later-stress.md)。这只补足既有 36 股回看，不能替代全市场时点股票池或真实成交数据。

### 2021 日期标签锚定 36 股（2026-09-30）

为检查按未来完整行情筛股的影响，另从 2026 Qlib 发布包的 2021-01-04 沪深300日期标签中按预定代码哈希选择 36 股，**不按未来价量过滤**。详见[锚定池覆盖与原价状态审计](qlib-2021-anchor-pool-audit.md)。本次定向补齐 62 个三年窗口，两个运行 ID `4e00b47c7a2140bab0b33334467f841e`、`e7b003f527d94dda90c0aad0cf153b64` 共新增 **43,088** 条原价日线与状态，全部成功，无剩余窗口。全库现有 **787,624** 条日线。

连同原有窗口，36 股在 2021-01-04 至 2026-09-28 共 **49,064** 个有原价状态的股票日。潜在 50,076 个股票日中，Qlib 有正价量且 BaoStock 正常交易 48,918 日，BaoStock 停牌状态 146 日，供应商退出日期后无原价状态 1,012 日；这些数逐日精确对账。供应商还标记了 72 个 ST 日。2021 日期标签与供应商目录均为后来取得的回溯资料，不能据此声称已建立官方历史时点股票池或完整企业行动结算。

下一步先核实官方历史成分和临时调样，再按经过核实的候选股票池定向补齐 **2021 年以来的原价、状态、交易日历和公司行动**，逐股票日记录缺口与来源版本；随后才能复核整手、T+1、停牌/涨跌停及总回报口径。未完成这些检查时，Qlib 复权收盘成交仅能标为回溯代理结果。

复核年度存量的只读 SQL：

```sql
PRAGMA quick_check;
SELECT substr(trade_date, 1, 4) AS year, count(*) AS bars,
       count(DISTINCT instrument_id) AS stocks
FROM daily_bars GROUP BY year ORDER BY year;
SELECT substr(trade_date, 1, 4) AS year,
       substr(instrument_id, -2) AS exchange, count(*) AS bars
FROM daily_bars WHERE trade_date >= '2021-01-01'
GROUP BY year, exchange ORDER BY year, exchange;
```
