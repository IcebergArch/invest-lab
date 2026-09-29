# 行情规范化流水线（第一阶段）

## 目标与当前边界

量化系统继续由现有采集器写入主库和 BaoStock 历史回填库。`quant_lab.canonical_data`
在读取时把两种 SQLite 记录转换成同一日线契约，不迁移大表，也不改变正在运行的采集与报告。
下游可以按标的和日期分批读取，再逐步接入特征、回测和模型训练。

```text
东方财富 / 腾讯 / 申万 -> MarketStore(SQLite) ----┐
                                             ├-> 流式提取 -> 规范化 -> 校验 -> 同口径价格面板
BaoStock 历史回填 -> 独立原价 SQLite ----------┘

Qlib release -> 已单独验证并发布的归档；因复权因子和成交额口径尚未逐股核验，
                目前不转为原价，也不混入上述面板。
```

## 契约

`CanonicalDailyBar` 是不可变日线记录，版本为 `canonical-daily-bar-v1`。每行保留：

| 字段 | 含义 |
| --- | --- |
| `instrument_id`, `asset_type`, `trade_date` | 统一标的身份、资产类型和交易日 |
| `open/high/low/close`, `price_basis`, `price_unit` | 价格和明确的口径；股票 `raw_unadjusted` 或 `forward_adjusted`，指数 `index_points` |
| `source_volume`, `source_volume_unit`, `volume_shares` | 源值、源单位和可核验的股票股数；`lot` 按 100 股转换，未知单位不估算 |
| `source_amount`, `source_amount_unit`, `amount_cny` | 源值、源单位和已知人民币成交额；未知单位或不适合交易的状态留空 |
| `source_id`, `run_id`, `payload_hash`, `dataset_id` | 提供方、采集批次、原响应或原行哈希和所在数据集；主库接口多为整个响应的哈希，BaoStock 为单行哈希 |
| `observed_at`, `source_published_at` | 实际抓取时间与上游首次可得时间；后者目前未知，保持 `None` |
| `trade_status`, `is_st`, `raw_amount_cny` | BaoStock 原始交易状态、ST 状态及源端成交额；主库未提供时留空 |

校验拒绝无穷值、非正价格、非法 OHLC、负量额、未知单位、资产类型与复权口径不匹配、
缺失或错误的 SHA-256。`tradability` 仅在 BaoStock 交易/ST 状态可核对时返回
`tradable` 或 `ineligible`；主库返回 `unknown`。当前全部记录的
`point_in_time_eligible` 为 `False`，因为抓取时间并非历史首次公开时间，历史股票池和
复权版本也尚未冻结。这个字段不能被股票筛选器当作可交易许可。

## 适配器和校验顺序

- `MarketStoreAdapter(path).iter_bars(...)` 对 `daily_bars` 与 `instruments` 做只读查询，
  支持最多 1000 个标的及起止日期筛选，按 `batch_size`（最多 10000 行）流式转换。
- `BaoStockArchiveAdapter(path).iter_bars(...)` 只接受 `baostock_daily` 的
  `adjustment=none` 股票行。每条日线必须与 `baostock_daily_status` 的标的、日期、
  `run_id` 和 `payload_hash` 一致，且状态确认存在有效价格。停牌或 ST 的原始金额保留
  在 `raw_amount_cny`，不会进入供流动性筛选使用的 `amount_cny`。无有效价格的状态日
  不伪造行情，也不前向填充。
- `build_close_panel(..., expected_price_basis=...)` 只构造同一价格口径、同一价格单位的
  共同交易日收盘价面板；混合原价与前复权、混合股票与指数、重复标的日期均报错。
  `input_fingerprint_sha256` 纳入价格、来源行哈希、采集批次及观察时间，方便核对
  某次回测究竟读取了哪批数据。

SQLite 以事务读取一致快照，查询开启 `PRAGMA query_only=ON`。WAL 数据库没有侧车文件
时，SQLite 可能创建 `-wal`/`-shm` 文件；适配器不会修改业务表。大规模研究应按标的、
时间窗口读取，避免一次把全部股票历史面板载入内存。

```python
from datetime import date
from quant_lab.canonical_data import (
    BaoStockArchiveAdapter, MarketStoreAdapter, build_close_panel,
)

main = MarketStoreAdapter("data/quant/market.sqlite3")
bars = main.iter_bars(
    instrument_ids=["stock:000338.SZ", "stock:002475.SZ"],
    start=date(2025, 1, 1), end=date(2026, 9, 29),
)
adjusted_panel = build_close_panel(bars, expected_price_basis="forward_adjusted")

archive = BaoStockArchiveAdapter("data/quant/historical-baostock-raw.sqlite3")
raw_bars = archive.iter_bars(instrument_ids=["stock:000001.SZ"], batch_size=1000)
raw_panel = build_close_panel(raw_bars, expected_price_basis="raw_unadjusted")
```

## 后续接入点

现有 `quant_lab run/research` 的三股回测仍使用 `MarketStore.load_close_panel`；
新增 `quant_lab optimize` 已从 `CanonicalDailyBar` 建同口径面板，并把契约版本、
采集血缘输入指纹、策略参数版本写入优化实验归档。主研究流水线迁移时，应继续把
数据集标识与这些版本一同写入不可变运行归档。针对 Qlib 的适配器
必须保留 `qlib_adjusted` 为独立价格口径，并先完成价格因子、量额单位和 release 可得时间
的核验；未核验时不能把 Qlib 复权价当作可与用户成本价比较的原价。历史当时股票池、
停复牌/涨跌停、公司行为和费用数据也需要各自的来源与时间戳，才能进入严格样本外检验。
