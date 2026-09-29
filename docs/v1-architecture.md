# A 股研究与决策支持 v1

## 范围与运行口径

v1 是个人日频研究系统，输出候选和证据，由人决定是否交易。收盘后生成信号。当前只有三只重点股票与五个宽基的已落地日线，不宣称已覆盖全 A 股。交易日次日 10:30 执行需要分钟线和可交易性数据；现有日线回测使用**下一交易日收盘价代理成交**，收益从成交后开始。它适合验证规则与数据链路，不能代表真实 10:30 成交收益。

个人工作流优先级已明确为：收盘后看市场报告与五条行业主线 → 系统给出有日期的待观察方向 → 用户在 10:30 后人工决策 → 记录决定 → 到期复盘 → 验证后提出下一版规则与候选。`quant_lab run` 已把数据、特征、策略、回测、行业筛选、信号和静态报告连成单命令；`decide` 与 `review` 承接后两步。现在的三个股票信号仅是回测接口试运行，不是全市场个股筛选。

```
Provider → sync → SQLite daily_bars → features → Strategy.weights
                                            ├→ next-close backtest → metrics
                                            └→ as-of signals → JSON report
```

运行：

```bash
PYTHONPATH=src python3 -m quant_lab sync --group stocks --start 2021-01-01
PYTHONPATH=src python3 -m quant_lab research sma-trend --date 2026-09-08 --out reports/quant/research.json
python3 -m unittest discover -s tests
```

`--date` 是截至日期，不是强制该日开市；报告的 `asof` 是不晚于它的最新共同交易日。报告含策略参数、特征版本、成本、股票池、每只股票最新行情的来源 ID、原始响应哈希、同步 run ID、抓取时间、信号原因及回测曲线。`score` 是规则排序分数，没有概率含义。

## 核心领域模型

| 对象 | 主键或身份 | 含义与时间约束 |
| --- | --- | --- |
| Instrument | `instrument_id` | 代码、交易所、类别、数据来源；历史上市/ST状态尚未建模 |
| DailyBar | `(instrument_id, trade_date, adjustment)` | OHLCV、量额单位、来源与哈希；日线价格 |
| FeatureValue | `(instrument_id, trade_date, feature_id, version)` | 由截至当日的历史序列计算；当前按需计算，尚未落库 |
| Strategy | `name + parameters + version` | 输入截至日的特征/历史，输出目标权重；不访问网络或数据库 |
| Signal | `instrument_id + date + strategy/version` | 动作、分数、原因、价格和目标权重；当前在报告中，持久化在 P1 |
| BacktestPoint | `strategy + trade_date` | 成交后的权益、收益、换手、权重 |
| ResearchRun | `report + source run IDs` | 参数、数据口径、信号及结果的可追溯快照 |

未来的 `Position`, `TargetWeight`, `RiskDecision`, `ExecutionRequest` 只保留概念边界，v1 无下单接口。策略的目标权重是研究引擎与未来组合层之间的接口；买卖动作由前后目标权重变化派生，`HOLD` 表示目标未变。

## 模块边界与接口

### 技术选型与目录

第一阶段沿用 Python 3.9+ 标准库、SQLite、`unittest`、JSON 报告和 CLI。单机本地运行足够验证日频研究；数据量或查询压力达到瓶颈后再评估列式存储。Notebook 可直接导入 `quant_lab` 的纯函数，Markdown/HTML 报告列入 P2。

```text
src/quant_lab/
  data_sources.py  sync.py  storage.py  models.py
  features.py      strategies.py       backtest.py
  research.py      verification.py     cli.py
tests/            docs/                reports/quant/
```

- `data_sources.py` / `sync.py`：向远端取数、字段映射与同步批次。上游异常应报错，不静默补缺。
- `storage.py`：SQLite 仓储，`upsert_bars`、`load_close_panel(ids, start, end)`；共同交易日取交集。全市场扩展时必须支持动态历史股票池，而非仅从当前上市股票构造。
- `features.py`：`sma(prices, n)`、`momentum(prices, n)`、`zscore(prices, n)`、批量动量；不足样本返回 `None`。策略从这里取特征。
- `strategies.py`：`Strategy.weights(history) -> Mapping[instrument_id, target_weight]`。每个策略独立实现，主流程只依赖该协议；下一步扩充 `universe / entry / exit / score / metadata`，同时保留当前最小协议的兼容性。
- `backtest.py`：`run_backtest(strategy, dates, close_panel, cost_rate)`；t 日收盘产生目标，t+1 日收盘代理调仓，按当日成交后权重承担下段收益，成本按绝对权重变化计。无杠杆、无融券。
- `research.py`：编排截至日的加载、策略、回测、信号和 JSON 报告；CLI 不包含策略规则。

## 存储设计

当前 SQLite 表：`instruments`、`daily_bars`、`sync_runs`、`metadata`。`daily_bars` 包含 `source_id / payload_hash / run_id / fetched_at`，可追到抓取批次。当前 upsert 会覆盖历史价格，前复权价可能在未来公司行为后被重算；因此仅凭当前库**不能**复现历史时点可见价格。P0 下一步是不可变 `dataset_snapshot(snapshot_id, created_at, source_version)` 与 `bar_observation(snapshot_id, instrument_id, trade_date, adjustment, ...)`，研究运行锁定 snapshot ID。

P1 新表建议：

```sql
CREATE TABLE research_runs (
  run_id TEXT PRIMARY KEY, asof_date TEXT NOT NULL, strategy_id TEXT NOT NULL,
  strategy_version TEXT NOT NULL, parameters_json TEXT NOT NULL,
  feature_version TEXT NOT NULL, dataset_snapshot_id TEXT NOT NULL,
  universe_json TEXT NOT NULL, cost_rate REAL NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE signals (
  signal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES research_runs(run_id),
  instrument_id TEXT NOT NULL, signal_date TEXT NOT NULL, action TEXT NOT NULL,
  score REAL NOT NULL, reason_json TEXT NOT NULL, price_at_signal REAL NOT NULL,
  prediction_json TEXT, UNIQUE(run_id, instrument_id)
);
CREATE TABLE signal_outcomes (
  signal_id TEXT NOT NULL REFERENCES signals(signal_id), horizon_days INTEGER NOT NULL,
  return REAL, max_up REAL, max_drawdown REAL, evaluated_at TEXT NOT NULL,
  PRIMARY KEY(signal_id, horizon_days)
);
```

基本面和估值另建带 `published_at`、`effective_date`、`fetched_at` 的观测表；查询以**公开时间**过滤，不用报告期末日代替公开日。股票状态表按生效日期保存上市、退市、ST、停牌及行业变化。历史股票池按当日状态构建，避免幸存者偏差。

## 回测与预测验收

当前回测输出累计/年化收益、年化波动、零无风险利率夏普、最大回撤、总换手与权益曲线，并给出同池等权买入持有和日期完整对齐时的沪深300价格指数参照。`backtest.md` 展示前后段历史诊断，明确真正独立时间外样本尚无；具体口径见 [回测评估说明](backtest-evaluation.md)。尚缺交易次数、胜率、盈亏比、平均持仓周期。后续增加交易流水，按明确的建平仓规则计算这些指标。费用当前是单一费率，未拆印花税、佣金、滑点、涨跌停及停牌；实际买卖方向和 A 股 T+1 约束也需在可交易性模型中实现。

未来 5/20/60 日事件研究必须只对已成熟样本统计，并按发信号时点可用数据训练概率。训练/验证采用滚动时间窗，不能把测试区间结果用于当时的预测。概率、收益区间及风险区间在校准验证完成前应显示“暂无可靠估计”，不能用规则分数冒充概率。

## 分阶段 Story

| 阶段 | Story | 验收 |
| --- | --- | --- |
| P0 已落地 | 三只股票同步、SQLite、价格特征、三个独立策略、下一收盘代理回测、信号与 JSON 报告 | CLI 可运行；时序与手算 oracle 测试通过；报告可追到最新 bar 来源 |
| P0 下一步 | 不可变数据快照、交易日历与股票历史状态、前复权时点口径 | 相同 run ID 可重建同一输入；退市/ST样本进入历史股票池；历史信号不会读到后来修订值 |
| P0 下一步 | 扩全 A 股和财务/估值数据，含公开时间 | 数据字典及缺失率可查；任意日期只返回当时公开数据 |
| P1 | 交易流水、成本/可交易性、5/20/60 日事件分析 | 指标可由流水复算；涨跌停/停牌不可成交；未成熟事件不出结果 |
| P1 | 每日扫描、信号日志、结果回填与策略复盘 | 幂等保存信号；按策略/行业/市值过滤；到期自动回填实际收益 |
| P1 | 概率校准与区间估计 | 时间外验证和样本数展示；不可靠时明确缺省 |
| P2 | Markdown/HTML 报告、策略与市场环境比较 | 可对齐同期间、同股票池、同成本比较 |
| P3 以后 | 组合、风险、半自动交易接口 | 待前述研究质量门槛达成后单独设计 |

不在 v1 实现自动下单、分布式任务框架或 LLM 决定交易。
