# 资本研究 · Invest Lab

[2026-09-30 变更汇总](docs/change-summary-2026-09-30.md)说明共享因子、组合决策、综合时间轴、预测留痕和版本边界的实现进度。

先看[行情、政策扰动与预测稳定性的一页结论](reports/quant/policy-behavior-brief-2026-09-29.md)：当前预测证据属于第 3 档，系统弃答，不把回顾性曲线当成稳定买卖信号。

已建立[单策略与多函数策略的前瞻模拟池](docs/paper-portfolio.md)：日期化决策共同进入通用模拟成交和收益评估；均与同池等权持有对照。截至 2026-09-30，三个当前账户均有 1 个前瞻代理成交日，距离 126 日观察门槛仍很远；量化总览只读展示其验证进度。

## Git 与本地数据

Git 保存源码、配置、测试、文档、经审核的事件清单及少量离线核验所需的官方附件。行情 SQLite、Qlib 历史发布包、运行报告、人工回执和回测逐笔证据属于本地数据，保存在 `data/quant/`、`reports/quant/`、`studies/`，不随代码提交；上面的实测报告链接需在本机生成或恢复数据包后才能打开。整理 Git 不会删除这些文件。

数据较大时可生成包含 `data/quant/`、`reports/quant/`、`studies/` 和 `policies/` 的 ZIP。策略配置随包保存，便于对齐实验上下文。脚本会对正在使用的 SQLite 建立一致性备份，保留原文件，并在包内写入文件大小与 SHA-256 清单：

```bash
python3 scripts/package-quant-data.py
python3 scripts/package-quant-data.py --verify backups/<生成的文件名>.zip
```

ZIP 默认保存在被 Git 忽略的 `backups/`，可由你自行复制到 iCloud Drive；项目不会自动上传，也不把大数据包纳入 Git。要迁移到另一份项目，先在空目录解包，再按[本地数据快照](docs/data-archive.md)的恢复步骤对齐四个目录；恢复 `policies/` 前须核对代码版本和账户冻结哈希。这些目录包含实验记录和人工回执，应按本地个人数据保存。

## 本地可视化服务与量化系统

这是两套可独立启动的系统，使用很薄的 JSON/HTTP 桥接。展示层是 React + TypeScript 应用，量化层是独立 Python 服务：

```text
浏览器 → 可视化系统 React + visual_lab (:8765)
                ├─ 研究空间：市场 / 三只候选 / 个股报告
                ├─ 量化空间：因子 / 策略 / 回测 / 优化实验 / 数据与回执
                └─ 仅代理白名单 /api/* 请求
                         ↓
          量化服务 quant_lab (:8766；本地开发绑定 127.0.0.1)
                ├─ 读取已有 JSON 报告：行情、资金观察、回测、预测、推荐门禁
                ├─ 只读查询主行情库或独立 Qlib Release：按股票代码生成个股分析
                ├─ 追加本地研究记录与逐例人工回执
                └─ 展示实验归档；按明确请求启动离线优化或到期复盘
```

可视化系统没有数据库路径，也不导入量化代码。行情保存在 SQLite，策略、实验、任务事件与人工回执以附带哈希的 JSON 记录追加保存。`/api/dashboard` 返回研究报告，`/api/stock-report` 接受 `{"symbol":"002475","cost_price":50}`（成本价可省略），`/api/console-status` 返回量化空间摘要和任务状态，`/api/feedback-case` 保存一例有来源研究记录的人工回执。量化空间可显式调用 `/api/optimize-run`、`/api/review-cases`，任务 ID 和终态存于 `reports/quant/task-runs`。部署脚本只构建并启动服务，不同步行情、不重跑研究。

先确保 `data/quant/market.sqlite3` 与 `reports/quant/latest/run.json` 已由研究流水线生成。开发时一条命令启动量化 API、可视化 HTTP 桥接和 Vite 热更新页面：

```bash
./scripts/start-local-dev.py
```

打开 [研究空间](http://127.0.0.1:8765/#tab-market) 或 [量化空间](http://127.0.0.1:8765/#quant-overview)；右上角切换空间，左侧显示当前空间的模块。前端修改会自动更新；修改 Python 服务后按 `Ctrl+C`，再运行同一命令。页面、量化 API、可视化桥接分别绑定本机 8765、8766、8767；可通过脚本的 `--port`、`--quant-port`、`--bridge-port` 改端口。此命令不执行行情同步或历史回填。

个股页的[综合时间轴](docs/analysis-timeline.md)可在同一股价图启停因子、政策、中证指数调样、公司定期报告、策略信号和规则持仓图层；每个事件保留原始日期、图上交易日、已知生效时间及出处。当前只核实沪深 300 的 2021 年 6 月调样和洛阳钼业 2026 年 6 月调入中证 A50，公司报告只人工登记三只关注股各一份 2026 年一季报和半年报，不代表完整公告或指数成分历史。

[预测留痕与策略版本](docs/forecast-evidence-and-lifecycle.md)记录类型识别、按类型启用预测/决策规则、全已入库股票的手动预测捕获及后续事实核验，并明确 dev、pre_online、online 的冻结和准出边界。

需要 Docker 环境时仍可手动使用：

```bash
./scripts/deploy-local-docker.sh
```

Docker Compose 使用两容器；量化服务只暴露在内部网络，页面端口只映射到本机回环地址。主行情库以只读卷挂载，报告卷用于追加研究和回执记录。停止服务：`docker compose -f compose.quant-dashboard.yaml down`。

研究空间的三个页面分别是市场趋势和成交关注度、3 只股票的推荐证据、输入股票及可选成本价的个股分析。量化空间展示已保存的策略、同池回测对照、0–10 分北极星目标、参数优化实验、因子库、算法组件、数据任务和人工回执，并可手动启动离线参数优化和到期回执复盘。算法组件库登记 13 项预测模型与基线，区分候选、已有适配器和已归档实验；TimesFM 2.5 与 Chronos-Bolt tiny 的实验、稳定性记录可追溯，但当前都未绑定可回测交易策略。当前主库覆盖 4 只股票（含 603993）；个股查询优先使用主库，主库没有的代码再从已发布的 Qlib 历史归档读取。该归档从 2000-01-04 至 2026-09-28 有 6,161 个代码、17,388,847 条有效收盘记录，但各股历史长短不同；个股报告标出该股全史有效收盘数与首末日期，走势指标只分析最近最多 256 个日历行。归档价格是复权口径，未经该股原价核验时不能与输入成本价比较，也不生成该股的预测或回测。[归档验收、发布和边界](docs/qlib-archive.md)记录了具体来源与检查结果。

个股报告现在分别显示“尚未买入”和“已经持有”的操作参考，并画历史价格、历史规则买卖/仓位曲线和主库股票的实验预测虚线。数据过期或价格口径未核验时会收紧判断。每次成功查询都会把当时报告、输入成本价、来源哈希和规则版本保存到本地研究记录，便于之后按回执复盘。[规则、曲线与留存口径](docs/stock-decision.md)列出当前阈值和边界。

这批归档仍与荐股研究池隔离。量化系统会明确显示“证据不足，0 只正式推荐”，不会把三股试验回测误作全市场选股结果；不在两个本地来源里的代码会提示缺少数据。成交份额不是资金净流入。需要更新正式研究报告时，单独运行 `quant_lab run`；前端只读取它发布的结果。

2000 年起的沪深日线正在按股票上市/退出日期分批归档，使用独立的原始价数据库和可续跑断点；[归档范围、限速脚本与核验方法](docs/baostock-history.md)有具体步骤。归档数据尚未并入上述 4 只股票的分析主库。跨公司行动的收益口径、交易日缺口和历史股票池核验完成前，不能直接把原始归档用于推荐或回测。

原始数据储备按字段、来源单位、年份与交易所分别盘点：[原始数据储备盘点](docs/raw-data-inventory.md)。当前主库和 BaoStock 归档已有 OHLC、成交量、成交额及部分交易状态；Qlib 发布包还有 `factor` 等十类文件，但其调整过的量价不当作原始成交股数。活跃原价回填的首末日期不代表中间年份和全市场已经补齐。

历史回补需要独立运行，可用 `./scripts/start-history-worker.sh` 启动限速、可续跑的 Docker 回补容器；`scripts/deploy-local-docker.sh` 仍只启动展示与量化接口服务。荐股页会显示原始归档进度，并与当前可用于分析的股票数分别标注。

已检查并安全发布的 Qlib Release 可用下列命令重新生成页面覆盖摘要；它核对检查报告与发布索引后原子写入 JSON。历史补数、下载和摘要生成均不在服务启动脚本里：

```bash
python3 scripts/publish-qlib-summary.py \
  --inspection reports/quant/qlib/inspect-2026-09-28.json \
  --published-index data/quant/qlib-releases/2026-09-28/published/release-index.json \
  --output reports/quant/qlib/latest-summary.json
```

## 简版量化数据闭环（新）

系统按**数据采集层 → 数据存储层 → 实验系统 → 应用层**四层组织。多来源记录通过规范化日线契约保留价格口径、单位和采集血缘；因子定义与[因子观测存储](docs/shared-factor-store.md)供分析和量化共用，分析解释个股变化，量化以点时合格的因子供决策函数与预算使用；回测引擎与优化器输出不可变实验记录。在线策略引擎是下一阶段扩展，当前控制台的运行按钮只触发离线任务。完整边界见[系统架构](docs/system-architecture.md)与[数据规范化流水线](docs/data-pipeline.md)。

最小闭环一条命令（已有本地数据时可离线运行）：

```bash
PYTHONPATH=src python3 -m quant_lab run
```

运行后先打开 `reports/quant/latest/market-pulse.html` 看大盘、资金活跃度与五条行业主线。主报告同时标明行业筛法的历史表现、三股试运行回测和简单预测基线。同目录的 `industry-validation.md`、`backtest.md`、`forecast-baseline.md`、`forecast-momentum.md` 分别给出证据和限制；`run.json` 记录本次运行 ID、数据日期与文件路径。需要先联网更新时加 `--sync`；网络或上游不可用会报错，不会伪装成最新行情。

同目录的 `candidates.json` 是待观察行业方向，`stock-shortlist.json` 记录股票筛选结果或数据阻断原因。你决定后可告诉我，由我记录；命令行也可使用 `quant_lab decide <candidate_id> watch|act|skip --note "原因"`。之后用 `quant_lab review` 查看已到期的 5/20 日行业或股票价格结果。`act` 只记录你的选择，不代表系统下单或个人账户实际收益。样本不足时系统不改筛选规则。

当前分析主库只有四只股票，所以全市场个股名单会被数据覆盖门槛阻断；不能把重点股票的回测胜负当作 A 股选股能力。现有行业筛法的初步历史检验也没有显示相对五条主线等权基线的优势。详见 [`docs/backtest-evaluation.md`](docs/backtest-evaluation.md) 与 [`docs/quant-roadmap.md`](docs/quant-roadmap.md)。

量化空间的三套重点股策略历史归档保留原费用口径；当前研究与模拟统一按同一股票池、买卖各 0.05% 费用和同一数据截止日计算。北极星分数是回测优化的目标：年化收益差与最大回撤差各占 50%，同池等权持有为 5.0 分，范围 0.0–10.0、保留一位小数。分数只描述历史试验，不替代独立前瞻验证。按时间顺序训练和历史留出段运行参数搜索：

```bash
PYTHONPATH=src python3 -m quant_lab optimize all --date 2026-09-29
```

优化器只根据训练段分数选参数，留出段仅验证已选参数；所有候选设定和结果保存在 `reports/quant/optimizations`。量化空间可把个股研究记录关联到你逐例的观察、跳过、买入或卖出回执。新交易日数据入库后，显式执行 `PYTHONPATH=src python3 -m quant_lab review-cases` 回填已成熟的 5/20/60 交易日价格结果。这个复盘不等于账户收益，也不会自动改策略或下单。公式和闭环状态见[实验与复盘说明](docs/experiment-loop.md)。

预测模块已接入无额外依赖的随机游走和 20 日动量基线，并提供可选的 Google TimesFM 2.5、Amazon Chronos-Bolt tiny 适配器。外部模型需要独立 Python 环境、固定的包版本与权重 commit；预测输出不参与股票排名。命令示例：

```bash
PYTHONPATH=src python3 -m quant_lab forecast --provider momentum-20

# 同一数据和起点比较预测模型，逐笔结果只追加保存
PYTHONPATH=src python3 -m quant_lab forecast-benchmark
PYTHONPATH=src python3 -m quant_lab forecast-benchmark --dataset qlib
# 独立环境安装 Chronos 后，可加 --provider chronos-bolt-tiny --chronos-revision 40位权重SHA
```

接口、许可与检验口径见 [`docs/forecast.md`](docs/forecast.md)。数据适配优先复用 [BaoStock](https://pypi.org/project/baostock/) 和 [AKShare](https://github.com/akfamily/akshare) 等现有工具；具体源需逐项核对时点、字段单位和覆盖范围。当前 BaoStock 现场能取沪深股票与人民币成交额日线，但不覆盖北交所，不能单独视为全 A 股清单。

已用官方公告登记 2022、2023、2024、2025 年四个央行政策节点，并生成[主库 4 股固定窗口事件报告](reports/quant/event-studies/2026-09-29/20260929T090740253389+0000-5140698b270c4bdd8502d6dedefb18fb.md)和[固定分层 Qlib 40 股事件报告](reports/quant/event-studies-qlib/2026-09-29/20260929T091230521019+0000-439ab42e72ce4e20821c2f1e3fbb45f5.md)。报告分别保留公告前、首个完整交易日后和政策邻近应激期的行情；主库核实了单位的量额单列，Qlib 样本的量额留空。应激期只用于事后诊断，不能当作未来已知的择时信号。[事件数据与方法](docs/event-study.md)说明公告时间可信度、量额单位、对照指数及局限。独立的[多模型回顾实验](docs/forecast-benchmark.md)已经实跑 TimesFM 2.5 和 Chronos-Bolt tiny，现有同口径样本的总体预测误差均没有优于随机游走基线，因此不把预测曲线升级为稳定的买卖结论。

另有[40 股群体量价行为试验](docs/group-behavior-pilot.md)：从固定 Qlib Release 读取同股相邻日复权成交量变化、群体涨跌宽度等起点已知特征，滚动预测后续 5/20 日群体收益代理。当前相对零收益基线的误差改善为 -4.39% / -16.22%，未证明稳定可用；四次政策公告前的预测另作应激诊断，样本太少且不参与模型训练时的事件标签。

[预测稳定性分层审计](docs/forecast-stability.md)将同起点随机游走对照、普通阶段和政策应激阶段逐笔配对；量化空间现在直接显示三档结论与各档样本数。当前 Chronos-Bolt tiny 与 TimesFM 2.5 的 5/20 步均为“粗预测/证据不足，弃答”。等级计算使用全部配对样本，事后剔除政策日只供诊断，不能把临时挑出的好区间当作可行交易区间。

BaoStock 保持可选依赖，在独立 Python 环境安装 `baostock==0.9.4` 和 `pandas` 后可运行：

```bash
PYTHONPATH=src python3 -m quant_lab baostock-universe
PYTHONPATH=src python3 -m quant_lab baostock-pilot \
  --snapshot reports/quant/snapshots/2026-09-29-baostock-shsz-8705cc042df3.json \
  --pilot-db data/quant/baostock-pilot.sqlite3 \
  --stock stock:002475.SZ --start 2026-08-17 --end 2026-09-28
PYTHONPATH=src python3 -m quant_lab run \
  --stock-snapshot reports/quant/snapshots/2026-09-29-baostock-shsz-8705cc042df3.json
```

试点库必须与主库分开：当前主库同一股票、日期和复权方式只能保留一个供应商的价格版本。具体约束与试点对照见 [`docs/baostock-source.md`](docs/baostock-source.md)。

要先看行情与板块轮动，可运行：

```bash
PYTHONPATH=src python3 -m quant_lab sync --start 2026-09-09 --group broad --group industry
PYTHONPATH=src python3 -m quant_lab market --out reports/quant/market-pulse.md
```

`market` 将 31 个申万一级行业整理为五条主线加“其他（综合）”，同时展示 5 个宽基指数、行业上涨宽度和成交份额变化。宽基与申万行业可能停在不同日期，报告会分别标注。成交份额是交易关注度，**不是资金净流入**；市场观察口径见 [`docs/market-pulse.md`](docs/market-pulse.md)。

`quant_lab` 是一套独立、零第三方依赖的实现，不依赖下方旧版 CSV/网页架构。它提供：

- 三只重点股票：立讯精密（002475.SZ）、潍柴动力（000338.SZ）、牧原股份（002714.SZ）。
- 科创50、上证指数、沪深300、中证500、中证1000。
- 申万一级 31 个行业指数；清单和日线直接从申万宏源研究发布接口核对。
- SQLite 幂等落库、同步批次、来源 ID、抓取时间和原始响应 SHA-256。
- 三个策略：双均线趋势、横截面动量、Z-Score 均值回归。
- 公式样例、回测记账、未来数据泄漏和真实数据完整性验证。

最短使用路径：

```bash
PYTHONPATH=src python3 -m quant_lab sync --start 2021-01-01
PYTHONPATH=src python3 -m quant_lab status
PYTHONPATH=src python3 -m quant_lab verify
```

上述方式不要求安装包；如需使用 `quant-lab` 短命令，请先确保 pip 版本不低于 21.3，
再执行 `python3 -m pip install -e .`。

默认数据库为 `data/quant/market.sqlite3`，验证报告为
`reports/quant/verification.json`。如只想先同步三只股票：

```bash
PYTHONPATH=src python3 -m quant_lab sync --group stocks
```

数据源分级、字段口径、已知限制和策略公式见
[`docs/quant-data.md`](docs/quant-data.md)。

v1 架构、领域模型、接口、数据模型和分阶段 Story 见
[`docs/v1-architecture.md`](docs/v1-architecture.md)。最小研究闭环：

```bash
PYTHONPATH=src python3 -m quant_lab research sma-trend --out reports/quant/research.json
```

该命令读取已同步的重点股票，计算特征与策略，在下一交易日收盘代理成交口径下回测，并输出最新信号与 JSON 报告。


一个轻量级多市场量化研究系统，初期支持 A 股、港股、美股和 BTC 的统一标的模型、CSV 行情接入、简单策略和本地回测。

原始远端仓库定位：A structured repository for learning investment theories and applying them in practice.

## 架构

```text
src/auto_invest/
  domain/       # 标的、K 线、组合、交易等核心领域模型
  ports/        # 数据源、交易执行等外部依赖接口
  adapters/     # CSV、本地配置等适配器
  strategies/   # 策略接口与简单策略
  backtest/     # 回测引擎、撮合、绩效报告
  apps/         # CLI 等应用入口
```

核心思路是 Clean Architecture + Ports and Adapters：策略只关心市场快照和目标仓位，回测引擎负责组合再平衡，数据源通过端口替换。

## 快速运行

```bash
PYTHONPATH=src python3 -m auto_invest.apps.backtest_cli \
  --bars data/sample/bars.csv \
  --instruments configs/universe.example.json \
  --strategy ma-cross \
  --fast-window 2 \
  --slow-window 3 \
  --initial-cash 100000
```

运行测试：

```bash
python3 -m unittest discover -s tests
```

## 数据系统（本地网页）

启动一个零依赖的本地 Web 服务。参数都有默认值，最简启动只要：

```bash
PYTHONPATH=src python3 -m auto_invest.apps.data_server
```

装包后（`pip install -e .`）还可以直接用注册好的命令：

```bash
auto-invest-serve              # 等价于上面那条
auto-invest-serve --reload     # 开启热重载
```

热重载：加 `--reload`，改动 `src/` 下任何 `.py` 源码会自动重启服务。数据文件（`bars.csv`）本就每次请求重读，改完刷新浏览器即生效、无需重启。需要自定义时仍可传参：

```bash
PYTHONPATH=src python3 -m auto_invest.apps.data_server \
  --bars data/sample/bars.csv \
  --instruments configs/universe.example.json --port 8000 --reload
```

然后访问 http://127.0.0.1:8000 （Ctrl-C 停止）。系统按「自上而下」的分析框架组织，左侧导航分层，右上角可切换经济体（中 / 美 / 欧 / 日）。每个视图顶部都有一段「要点解读」叙事框（参考经济研报结构：摘要 / 论据 / 风险），由当前数据自动生成：

- 仪表盘（首页）：一屏聚合各层最关键信息——大势 KPI、行业领涨/掉队、市场快照。
- 框架 / 论点：分析哲学（哲学 → 理论 → 技术）、三层逻辑与各指标口径说明。
- ① 大势 · 宏观概览：所选经济体的 GDP 增速、CPI 通胀、失业率、政策利率 + KPI 卡片。
- ① 大势 · 全球对比：中美欧日同一指标横向对比曲线 + 最新年度排行榜。
- ② 关键方 · 事件：关键参与方（央行 / 财政 / 数据发布方）与关键事件时间线（示例数据）。
- ③ 行业轮动：标普 500 GICS 11 大行业的年度回报排行 + 历年轮动表（看「经济往哪走」）。
- ③ 个股研究：单标的下钻——收盘价 + 快/慢均线、成交量、日收益率，以及累计/年化收益、年化波动、夏普、索提诺、胜率、最大回撤、最大连亏等统计（口径与回测引擎一致）。
- 市场 · 相关性：所有标的归一化对比曲线 + 日收益相关性矩阵（分散度参考）。
- 策略回测：在网页里选策略 / 参数 / 初始资金，调用项目 `BacktestEngine`，画资金曲线与绩效指标。

宏观数据为各经济体年度值（美国 BEA/BLS/Fed；中国 NBS/世行/IMF；欧元区 Eurostat/ECB；日本内阁府/统计局/BOJ），部分 2016–2019 为权威近似。行业轮动为标普 500 GICS 板块年度总回报。要更换数据，编辑 `economy_dashboard.py` 顶部的 `COUNTRIES` 与 `SECTORS`。

后端按投资平台思路分三层（`src/auto_invest/analysis/`）：`datasources`（数据源层，读 bars/宏观/行业/事件）→ `analytics`（分析服务层，统计/相关性/行业轮动/回测计算）→ `narrative`（叙事层，生成各视图的要点解读）。`apps/data_server` 是其上的 API 路由 + 展示层。这样以后把 CSV/宏观换成真实数据源时，只需改 `datasources`，analytics 与 UI 不受影响。

如只需生成一张静态 HTML 面板（不起服务），用：

```bash
PYTHONPATH=src python3 -m auto_invest.apps.economy_dashboard \
  --bars data/sample/bars.csv --out dashboards/us_economy.html
```

## 初期能力

- 统一市场抽象：A 股、港股、美股、BTC。
- 统一 K 线格式：`timestamp,instrument_id,open,high,low,close,volume`。
- 支持简单策略：买入持有、均线交叉。
- 支持按市场规则做数量步长约束，例如 A 股 100 股一手、BTC 0.0001。
- 输出基础绩效：期末权益、总收益、最大回撤、交易数。

## 下一步扩展

- 接入 AkShare/Tushare、IBKR、币安等真实数据适配器。
- 增加事件驱动撮合、订单簿、风控和组合优化模块。
- 增加因子研究、特征仓库和 walk-forward 验证。

暂定组合比较政策及 2021 年后随机入场实验见 [docs/random-entry-baseline.md](docs/random-entry-baseline.md)。七候选筛查、沪深300月度快照宽池压力测试、[36 股原价公司行动重放](docs/raw-action-aware-later-stress.md)与[不按信号筛日的日历随机复核](docs/unconditional-random-entry-stress.md)均未证明策略稳定；[2021 锚定股票池审计](docs/qlib-2021-anchor-pool-audit.md)量化了未来完整行情筛选排除的停牌及退出样本，[停牌约束重放](docs/qlib-2021-anchor-halt-replay.md)把其中 500 个窗口补出代理结果。[终止交易审计](docs/qlib-2021-anchor-terminal-exit.md)确认 303 个固定三股窗口的期初买入不可执行；[决策日候补覆盖检查](docs/qlib-2021-anchor-asof-reserve-coverage.md)将其降到 1 个，[原价回放](docs/qlib-2021-anchor-asof-reserve-replay.md)则在同一 6,144 个窗口中取得 5,879 个条件代理结果、265 个未知，仍未证明策略稳定。尚无未来交易日验证；该发布包成分日期与官方生效日存在已核实偏差，来源与缺口见 [沪深300官方调样审计](docs/csi300-official-source-audit.md)。新增日线候选因子与高频数据门槛见 [docs/factor-research-v2.md](docs/factor-research-v2.md)。
