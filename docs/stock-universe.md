# 当前股票池与受控日线试点

`quant_lab.stock_universe` 增加了两个独立入口：

1. `EastMoneyStockUniverseSource().list_current()`：读取东方财富公开列表，分页核对总数、每页行数、重复代码和第一页稳定性后，返回上海、深圳、北京三市**当前**股票池。
2. `sync_stock_pilot(store, snapshot, selected_ids, start, end)`：只同步明确传入的 1–30 只股票、最多 550 个自然日。每行日线仍由现有 `daily_bars` 记录 `source_id`、`payload_hash`、`fetched_at` 与 `run_id`。同步运行记录关联股票池 `snapshot_id`。

使用示例（运行会联网并写入指定快照和数据库）：

```python
from datetime import date
from quant_lab.stock_universe import (
    EastMoneyStockUniverseSource, sync_stock_pilot, write_stock_snapshot,
    read_stock_snapshot,
)
from quant_lab.storage import MarketStore

snapshot = EastMoneyStockUniverseSource().list_current()
write_stock_snapshot(snapshot, "reports/quant/current-stocks.json")
snapshot = read_stock_snapshot("reports/quant/current-stocks.json")
selected = ["stock:002475.SZ", "stock:600000.SH"]
result = sync_stock_pilot(
    MarketStore("data/quant/market.sqlite3"), snapshot, selected,
    date(2025, 9, 1), date(2026, 9, 28),
)
print(result.run_id)
```

## 真实接口核对与当前限制

- 2026-09-29 本地约 11:52 的只读请求中，组合筛选 `m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048` 返回 `rc=0`、`data.total=5920`。第一页实际包含 `301716`、`920202`、`920779`，其中 `f13=0` 同时用于深圳和北京股票，必须按代码段区分。北交所[新旧代码对照表](https://www.bse.cn/service/code_mapping.html)也列有 `830779 → 920779`。这是**第一页证据，不等于已经取得完整 5920 只快照**。
- 随后的分页和扩展字段请求多次返回 `curl: (52) Empty reply from server`。因此当前没有写出全量快照，不能声称全量个股筛选已完成。代码会在缺页、缺行、总数变化、重复代码或第一页变化时失败，不保存不完整快照。
- 列表请求会带 `f6/f20/f21` 并在快照中按**原字段名**保留数值；本轮扩字段真实响应未取得，字段单位和时点尚未现场核对。筛选器在完成核对前不应把这些值当作已验证的成交额、市值或历史流动性。
- `collected_at` 是本系统发起读取的 UTC 时间，不是交易所或行情提供方确认的报价时点；盘中按变动字段排序可能改变分页。代码在页首复核失败时中止，仍不能把多页请求当成交易所原子快照。
- 本模块的「完整」只表示**东方财富该次列表过滤条件报告的总数与读取行数一致**。还需与[上交所股票列表](https://www.sse.com.cn/assortment/stock/)、[深交所产品目录](https://www.szse.cn/market/product/stock/list/index.html)和[北交所股票列表](https://www.bse.cn/nq/listedcompany.html)按交易所核对，才能主张交易所口径覆盖。
- 这是**当前上市股票池**，不含已退市股票的历史成员状态。用它回测历史区间会产生生存者偏差。前复权历史价格也会随公司行为重算；要做可信历史回测仍需按日期保存股票池和不可变行情快照。
- 公开行情没有可用 SLA。北京股票日线只请求东方财富；不会把北京代码误交给现有腾讯回退源的深圳映射。试点筛选需要人民币成交额，若回退源仅给价格、不给成交额，同步运行会标记失败；不会把部分成功标为完整成功。

下一步是取得完整现时快照，核对报价字段与交易所代码总数；然后以有流动性和风险门槛的短名单为输入运行受控日线试点，并建立逐日股票池档案。
