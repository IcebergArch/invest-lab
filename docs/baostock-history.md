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
