# 预测实验：先与简单基线比较

`quant_lab.forecast` 是研究组件。它接收按交易日排序的历史收盘价，在每个历史起点只向模型传入该日起已有的数据，预测 5 或 20 个**交易观测**后的收盘价。输出保留每笔 `origin_date`、`target_date`、预测价、实际价、随机游走预测价和误差，可审计是否误用未来数据。它不产生买卖信号，也不进入股票筛选评分。

## 已可运行的基线

`RandomWalkProvider` 预测未来收盘价等于起点最近收盘价。`Momentum20Provider` 将最近 20 个交易观测的复合涨跌幅按相同每日增速外推，用作极简方向基线，至少需要 21 个收盘价。`evaluate_forecasts` 默认从第 120 条记录开始、每 20 条记录取一个滚动起点，按 5/20 交易日分别统计价格 MAE、收益率 MAE、相对随机游走的误差改善率。方向命中率只计算预测涨跌不为零的样本，因此随机游走基线的方向命中率为 `null`，覆盖率为 0；不能将其写成 0% 准确率。若模型提供 10%/90% 分位数，还统计实际收盘价落入区间的频率。

误差改善率 `1 - 模型 MAE / 随机游走 MAE` 只是预测误差对比，不等于超额投资收益。真实收益需要另行按交易规则、成交可行性、成本与风险计算。基线使用当前已知的 3 只关注股时，只能标记为小样本研究；不能据此验证全市场选股能力。

```python
from quant_lab.forecast import RandomWalkProvider, evaluate_forecasts

report = evaluate_forecasts(
    dates, closes, RandomWalkProvider(), "stock:002475.SZ", asof=cutoff
)
# report["records"] 是每笔起点/目标；report["summary"]["5"] 和 ["20"] 是汇总。
```

## 可选 TimesFM 2.5

`TimesFM25Provider` 采用 Google 官方 2.5 PyTorch API：`TimesFM_2p5_200M_torch.from_pretrained()`、`ForecastConfig`、`model.forecast(horizon=..., inputs=[一维数组])`。它延迟加载模型；普通报告运行和单元测试不会下载权重。若未安装依赖，评估返回 `missing_optional_dependency`；安装了不匹配的包版本则返回 `incompatible_optional_dependency`。开发测试可注入返回官方 `(1,H)` 点预测、`(1,H,10)` 分位预测形状的 runner。

在**独立 Python 3.10+ 环境**安装并固定 `timesfm[torch]==2.0.2`，使用 `google/timesfm-2.5-200m-pytorch` 权重。实际加载必须传入 40 位 Hugging Face 权重 commit SHA 作为 `model_revision`，传 `main` 或省略会返回 `missing_model_revision`，防止后续仓库更新改变历史结果。主项目仍支持 Python 3.9 且不将重型 ML 包列为常规依赖。默认适配器只用最近 512 条历史价、最多预测 128 步；本机 CPU 已完成 5/20 步推理，其他上下文长度与硬件速度尚未逐项核验。Google 的 [TimesFM 2.5 权重卡](https://huggingface.co/google/timesfm-2.5-200m-pytorch) 标明 Apache-2.0。[TimesFM 官方仓库](https://github.com/google-research/timesfm) 写明 3.0 的预训练权重另有非商业、非生产许可，因此本系统不将 3.0 权重用于未来自动交易路径。包的 [v2.0.2 源码](https://github.com/google-research/timesfm/tree/v2.0.2) 是本适配器的接口依据。

可比较的另一开源候选是 [Amazon Chronos-2](https://github.com/amazon-science/chronos-forecasting)，其[权重卡](https://huggingface.co/amazon/chronos-2)标明 Apache-2.0，并支持分位数、多个时间序列和协变量。它尚未接入；应在同一数据截面、起点和指标下独立比较，不根据公开通用榜单推断 A 股效果。

## 多模型实验路线

统一运行入口是 `quant-lab forecast-benchmark`，记录规则与 [Chronos-Bolt tiny、TimesFM 2.5 实跑结果](forecast-benchmark.md)见多模型实验记录。首批固定随机游走和 20 日动量基线，并已在本机 CPU 检验 Chronos-Bolt tiny 与 TimesFM 2.5；后续模型只有完成**实际权重加载、同口径预测与归档**才标记为“已实验”。模型比较先看 5/20 个交易观测的收益预测误差、方向覆盖、10%–90% 区间覆盖与失败率；这些数值不能直接作为交易北极星分或买卖动作。

| 家族 | 对本项目的实验定位 | 核对依据 |
| --- | --- | --- |
| Google TFT | 需要本地训练与时间切分，作为后续监督学习对照；它不是可直接零样本运行的官方预训练权重。 | [Google 官方代码](https://github.com/google-research/google-research/tree/master/tft) |
| Google TimesFM | 2.5 已有可选适配器；固定权重 SHA 后单变量比较。3.0 支持多变量，但其公开权重有非商业许可，须单独核对用途。 | [2.5 权重卡](https://huggingface.co/google/timesfm-2.5-200m-pytorch)、[3.0 权重卡](https://huggingface.co/google/timesfm-3.0-pytorch) |
| Amazon Chronos | 首批 Chronos-Bolt tiny 做低成本零样本对照；Chronos-2 再检验多变量和协变量能力。Bolt 本身没有 Chronos-2 的协变量接口。 | [官方仓库](https://github.com/amazon-science/chronos-forecasting)、[Bolt tiny 权重卡](https://huggingface.co/amazon/chronos-bolt-tiny) |
| IBM Tiny Time Mixers | 日频 r2.1 分支适合轻量对照；其当前 Torch 依赖与 Chronos 分开安装。 | [模型卡](https://huggingface.co/ibm-granite/granite-timeseries-ttm-r2)、[官方配置](https://github.com/ibm-granite/granite-tsfm/blob/main/tsfm_public/resources/model_paths_config/ttm.yaml) |
| Salesforce Moirai | 可在隔离研究环境比较；Moirai 2.0 的公开权重为 CC-BY-NC-4.0，当前不列入未来自动交易候选。2.0 由早期的多 patch 尺寸改成单一尺寸。 | [权重卡](https://huggingface.co/Salesforce/moirai-2.0-R-small)、[2.0 论文](https://arxiv.org/abs/2511.11698) |
| Time-MoE / Timer | 留在后续隔离研究队列，先解决旧版 Transformers 与远端自定义代码依赖。两者不宜笼统归为“阿里系”：Time-MoE 的论文作者机构包括小红书等，Timer 的公开权重来自清华 THUML。 | [Time-MoE 官方仓库](https://github.com/Time-MoE/Time-MoE)、[论文](https://arxiv.org/html/2409.16040v4)、[Timer 权重卡](https://huggingface.co/thuml/timer-base-84m) |

Chronos-Bolt tiny 的适配器位于 `quant_lab.chronos_adapter`，在普通 Python 3.9 服务中只定义接口，不加载 ML 依赖。实际推理需要独立 Python 3.10+ 环境、`chronos-forecasting`/Torch 和 40 位 Hugging Face commit SHA；模型原生最多接收 2048 个历史观测、预测 64 步。模型包与权重固定后，实验记录应写入依赖版本、输入指纹和逐笔误差；许可与速度只用于排序实验成本，不代表 A 股预测效果。

## 进入选股或自动化交易前的门槛

1. 保存不可变的数据快照、股票池及退市股、复权版本、抓取时间、模型代码与权重 revision；历史前复权价会被后续公司行动重算，当前数据库尚不能证明严格的历史 as-of 价可复原。
2. 每个预测起点仅用当时已发布的数据。若训练或微调，按时间切分并记录训练截止，验证集及测试集不得回流训练；未来财报、未来行业分类和未来指数成分不得成为输入。
3. 在足够长且覆盖不同市场状态的全市场样本外区间，比较随机游走和 20 日动量外推基线的误差、方向覆盖、分位数校准；再将预测转成预先固定的策略，计入交易费用、滑点、停牌/涨跌停与容量，检验收益和最大回撤。
4. 模型优于基线且风险指标过关后，才允许进入纸面交易和后续风控闸门。预测区间不是黑天鹅保护；需要独立限仓、停机、异常数据和极端行情控制。

官方接口与安装要求：[Google TimesFM](https://github.com/google-research/timesfm)、[TimesFM 2.5 API](https://github.com/google-research/timesfm/blob/master/timesfm-forecasting/references/api_reference.md)、[系统要求](https://github.com/google-research/timesfm/blob/master/timesfm-forecasting/references/system_requirements.md)。
