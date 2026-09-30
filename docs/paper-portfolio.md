# 前瞻策略模拟池

三个当前账户都以 2026-09-29 收盘数据为首个信号输入，初始模拟资金各 100,000 元；截至本次核验均有 **2026-09-30 一个未来代理成交日**。开户记录实际在北京时间 2026-09-30 11:29（单策略）及 11:59（两个 runtime2 账户）创建，早于 9 月 30 日代理收盘成交时点，但不是 9 月 29 日收盘当时已经冻结的决策。单策略、暂定风险基线和多函数策略是三种决策来源，共用未复权收盘价、100 股买入单位、模拟现金约束及买入和卖出各 0.05% 的统一费用。个人实际持仓不进入策略验证。

| 账户 | 决策来源 | 研究状态 |
| --- | --- | --- |
| `reports/quant/paper/sma-trend-raw-lot-v3-five-bps-2026-09-29/` | 冻结 SMA 趋势单策略 | 单策略对照；1 个前瞻代理成交日 |
| `reports/quant/paper/random-entry-risk-baseline-v1-runtime2-2026-09-29/` | [2021 年后随机入场研究](random-entry-baseline.md)选出的 SMA 三股风险基线；总仓位最多 80%、单股最多 35% | 暂定比较基线；1 个前瞻代理成交日 |
| `reports/quant/paper/ensemble-focus-three-v2-five-bps-runtime2-2026-09-29/` | SMA 趋势、横截面动量、Z 分数均值回归，各函数同权提案；组合总仓位最多 80%、单股最多 35% | 多函数实验；1 个前瞻代理成交日，不可作为操作建议 |

多函数政策固定在 [`policies/ensemble-focus-three-v2-five-bps.json`](../policies/ensemble-focus-three-v2-five-bps.json)。每个交易日的决策保存因子值、三套函数的目标、汇聚贡献、组合预算、输入与决策哈希。下一交易日收盘后，通用模拟引擎才按**未复权真实日线收盘价代理**生成股数、订单、费用、现金和净值；同池等权买入持有作基准。信号计算使用前复权价，二者来源分开记录。多函数账户同时维护买卖各 0.05% 基础费用与买卖各 0.15% 压力费用两套独立模拟仓位和同池基准。记录只追加，历史信号价被修订或冻结代码变化时停止续记。

量化空间总览通过只读 `/api/console-status` 展示三个账户的账本校验状态、前瞻样本、收益与门槛，不展示未获验证的买卖目标。

当前通用账户的 `baseline` 目标是 **100% 等权买入持有**（整手撮合后可能留有现金），而风险基线与多函数策略的目标总仓位最多 **80%**。两者的收益和回撤差同时含有仓位差异，不能单凭现有 `preliminary_gate_passed` 或 `human_review_candidate` 判断决策函数优劣。[只读同预算评估器](paper-same-budget-evaluator.md)已在这两份冻结账本之外重放 **80% 等权买入持有** 和逐日同目标总仓位等权两条对照；它们使用同池、同日、同费用和整手撮合，并不改写原账本。当前各有一个未来代理成交日，数值基本只反映首日开仓费用。逐日同目标仓位对照用于观察配置与换手的合计影响，不能把收益差单独解释成选股能力。

2026-09-30 手动同步的核心 8 个标的共 16 条 9 月 29/30 日日线，主库同步 `run_id=811c7872b9d2490aad7ce54ff076cc0e`。三个账户各新增 `sessions/2026-09-30.json`；账户外评估器校验两个 runtime2 账户的会话哈希链、原价/前复权价条和冻结运行时代码。首日按同一收盘价买入并估值，股价路径尚未展开：单策略净值 99,951.18 元，风险基线 99,983.02 元，多函数 99,968.08 元，差额主要是各自开仓金额对应的买入费用。它们都仍为 `research_only`，1/126 不能判断稳定性。

手动运行，不设置定时任务：

```bash
cd /Users/shatang/Project/nexus-os/invest-lab
PYTHONPATH=src .venv/bin/python -m quant_lab sync --start 2026-09-29 --end YYYY-MM-DD --group core
PYTHONPATH=src .venv/bin/python scripts/strategy_simulator.py advance
PYTHONPATH=src .venv/bin/python scripts/quant_paper_pool.py advance --policy policies/ensemble-focus-three-v2-five-bps.json --out reports/quant/paper/ensemble-focus-three-v2-five-bps-runtime2-2026-09-29
PYTHONPATH=src .venv/bin/python scripts/quant_paper_pool.py advance --policy policies/random-entry-risk-baseline-v1.json --out reports/quant/paper/random-entry-risk-baseline-v1-runtime2-2026-09-29
PYTHONPATH=src .venv/bin/python scripts/strategy_simulator.py status
PYTHONPATH=src .venv/bin/python scripts/quant_paper_pool.py status --out reports/quant/paper/ensemble-focus-three-v2-five-bps-runtime2-2026-09-29
PYTHONPATH=src .venv/bin/python scripts/quant_paper_pool.py status --out reports/quant/paper/random-entry-risk-baseline-v1-runtime2-2026-09-29
PYTHONPATH=src:scripts .venv/bin/python scripts/paper_same_budget_evaluator.py reports/quant/paper/random-entry-risk-baseline-v1-runtime2-2026-09-29 reports/quant/paper/ensemble-focus-three-v2-five-bps-runtime2-2026-09-29
```

较早的两个 `quant_paper_pool` 开户账本因运行时数据源修正与冻结代码哈希不一致，均只有开户记录、没有未来成交，保留作审计；上表 `runtime2` 账户在同一 2026-09-29 输入上重新开户并通过当前哈希检查。操作时须明确传 `--out` 指向上表账户。

其中 `YYYY-MM-DD` 改为已经收盘的交易日。当天北京时间 16:00 前、信号日线缺失、原价缺失或零量停牌时不能生成模拟成交。重复运行不会重复入账。早期 `sma-trend-v1-2026-09-29` 仅用复权权重代理；`sma-trend-raw-lot-v2-2026-09-29` 与 `ensemble-focus-three-v1-2026-09-29` 则使用旧费率。它们均未积累未来样本，保留作实现与费率变更审计，不纳入当前对照。

## 历史诊断与准入

[多函数政策历史诊断](../reports/quant/policy-evaluations/ensemble-focus-three-v2-five-bps-2026-09-29.json)覆盖 2021-01-04 至 2026-09-29：买卖各 0.05% 成本下累计收益约 25.0%，同池等权持有约 14.3%，策略最大回撤约 38.9%，累计换手约 150.9；假设买卖各 0.15% 的压力费率时策略累计收益约 **7.5%**。这反映明显的费用敏感性。三股池和政策均在这些历史数据已知后形成，因此前后段只能作回看诊断，不能称为独立样本外证明。

三个账户的初步前瞻门槛均为至少 126 个新交易日，净收益高于同池等权基准且最大回撤不更差。多函数账户还要求买卖各 0.15% 的假设压力费用下优于同期基准；通过数值门槛只会标记为 `human_review_candidate`，不会自动发布操作建议。达到初步门槛也不自动进入操作辅助：仍需核对更真实的费用、公司行动、涨跌停/停牌、样本覆盖、跨行情环境稳定性以及失败案例。当前系统明确标为研究状态。

日线收盘价只是模拟成交代理，不能保证盘中或收盘集合竞价可按该价成交。费用按成交额在买卖两侧分别计入；尚未建模最低佣金、单列税费、滑点、涨跌停排队、部分成交、分红和送转现金流；遇到公司行动时应核对原价和持仓变化后再解释收益。账户数据被 Git 忽略，应通过 `scripts/package-quant-data.py` 做本地备份。
