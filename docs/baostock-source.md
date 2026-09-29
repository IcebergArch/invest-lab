# BaoStock 可选数据源：沪深股票试点

`quant_lab.baostock_source` 复用 `Instrument`、`DailyBar` 和 `MarketStore`，供当前股票池与日线流动性核对使用。它不改变现有 `quant-lab run` 的默认链路；只有显式调用才联网、落库。

## 数据来源与使用边界

- [BaoStock 官方 PyPI 页面](https://pypi.org/project/baostock/)（2026-09-29 核对）标注包版本 **0.9.4**、2026-09-21 发布、**BSD License**，说明其使用自有数据服务器，示例调用 `query_history_k_data_plus` 并列出 `volume`、`amount`、`adjustflag`、`tradestatus`、`isST` 等字段。[BaoStock 官网](https://www.baostock.com/)是 PyPI 页面给出的主页。BSD 是**客户端代码**的许可；PyPI 页面没有明确赋予上游行情的再分发或自动交易使用许可，应按实际数据服务条款另行核对。
- 安装为可选依赖：`python -m pip install baostock pandas`。默认项目运行不要求这两个包；测试用注入的假客户端运行，也不要求联网。
- 当前列表调用 `query_stock_basic()`，只接受 `type='1'`、`status='1'` 且代码为 `sh.6xxxxx`、`sz.0xxxxx` 或 `sz.3xxxxx` 的条目。**北交所不在 BaoStock 当前响应中，也不在本适配器股票池中。** 当前列表只反映查询时供应商的上市状态，不可回填为历史时点股票池。供应商没有在该接口响应中提供独立总数，因此最小数量检查只防明显的短响应；不能单凭 `listed_count` 声称交易所口径完整覆盖。
- 日线显式请求 `frequency='d'`、`adjustflag='2'`（前复权），以及 `tradestatus`、`isST`。本地将 `volume` 记为股、`amount` 记为人民币元；这是 [BaoStock 包示例](https://pypi.org/project/baostock/)的字段加上[接口字段单位整理](https://github.com/zxygithub/baostock/blob/master/docs/data_download_plan.md)的口径，真实样本的 `amount / volume` 也与股价同量级。接入其他源时需分别做单位转换，不能沿用本源规则。

## 受控用法

以下代码会联网，并只把一只股票写入独立的 `/tmp` 试点库。`end` 应设为已完成的交易日；不传股票 ID 不会自动下载全市场。

```python
from datetime import date
from quant_lab.baostock_source import (
    BaoStockSource, sync_baostock_pilot, write_baostock_snapshot,
)
from quant_lab.storage import MarketStore

source = BaoStockSource()
snapshot = source.list_current()
write_baostock_snapshot(snapshot, "/tmp/baostock-pilot/current-sh-sz.json")
result = sync_baostock_pilot(
    MarketStore("/tmp/baostock-pilot/market.sqlite3"),
    snapshot,
    ["stock:002475.SZ"],
    date(2026, 8, 17),
    date(2026, 9, 28),
    source=source,
)
print(result.run_id, result.row_count, result.recommendable_ids)
```

单次同步上限是 **30 个显式股票 ID、550 个自然日**。`write_baostock_snapshot` 原子保存股票池、读取时间、供应商版本、原始列表哈希及 `snapshot_id`；`read_baostock_snapshot` 校验内容 ID。`sync_runs.groups_json` 记录 `snapshot_id`，`daily_bars` 每行记录 `source_id`、原始行哈希、`run_id`、获取时间。新增 `baostock_daily_status` 保存每个提供方返回日的 `tradestatus`、`is_st`、原始成交额、行哈希及 `run_id`。

**停牌和 ST 门禁：** 这两类状态的日线即使有有效价格，也以 `amount=NULL`、`amount_unit='ineligible'` 写入，因此现有股票筛选器的近 20 日成交额完整性检查会挡下它们。若价格缺失，不捏造日线；状态仍入审计表。`result.recommendable_ids` 还要求最后状态日期恰为请求的 `end`，且正常交易、非 ST、成交额大于零。它只是数据源状态过滤结果，**不是股票推荐或收益预测**。完整筛选仍要检查历史长度、波动、回撤、流动性和股票池覆盖。

现有 `daily_bars` 以股票、日期和复权方式为唯一键，同一日的不同提供方不能并列保存。请使用独立试点库做对照；并入主库前必须明确主源切换、复权锚点和状态门禁。即使前复权价当前一致，历史公司行为仍可能使其重算。当前快照也不含退市股历史成员，不能直接用于无生存者偏差的长期回测。

## 2026-09-29 当前环境实测

使用官方包 0.9.4，仅写 `/tmp/quant-baostock-pilot-20260929/`；项目主库通过 SQLite `mode=ro` 对照。

| 证据 | 结果 |
| --- | --- |
| `query_stock_basic()` | 返回 8,983 行；严格按上方规则筛得沪深在市 A 股 5,222 只，未见北交所代码。 |
| 试点股票 | `002475.SZ`，2026-08-17 至 2026-09-28 前复权日线 30 行，状态记录 30 行。 |
| 最新状态和成交额 | 2026-09-28 `tradestatus=1`、`isST=0`、`amount=4,654,262,384.05` 元。 |
| 主库只读对照 | 同期 30 个日期全部对应，双方无缺日；收盘价最大绝对差 **0.0 元**。主库这段标记为 `tencent_kline`。这只验证此标的、此区间的收盘价。 |
| lineage | run ID `ed3c20220c874cd0a6c5faed6b0bd0cb`；snapshot ID `8705cc042df34f4aa91569160d63c5289655bbe5ccbad678a29279801083a156`；运行状态 `success`。 |

已将验证后的文件保存在项目中：[股票池快照](../reports/quant/snapshots/2026-09-29-baostock-shsz-8705cc042df3.json)、[只读对照摘要](../reports/quant/evidence/2026-09-29-baostock-focus-price-check.json)、[独立 SQLite 库](../data/quant/baostock-pilot-20260929.sqlite3)。这是单股取数链路的验收，不证明 5,222 只日线已全部同步或核对。再次通过 CLI 获取股票清单时供应商登录返回 `10002007 网络接收错误`；该次请求未覆盖已验证快照。
