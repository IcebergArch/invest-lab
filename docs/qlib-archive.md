# Qlib Release 只读覆盖检查

`quant_lab.qlib_archive` 检查 [chenditc/investment_data](https://github.com/chenditc/investment_data) 的 Qlib 日线 Release。它先核对同一 Release 的 manifest 标签、归档大小和 SHA-256，再以流式方式检查 tar 成员；不会解压、写 SQLite、修改当前主库或让这些价格进入选股与回测。

固定 Release 的 manifest 已保存到本地后，可用可续传脚本获取归档；脚本会在完整文件大小和 SHA-256 与 manifest 匹配后才替换目标文件，不属于只启动服务的 Docker 脚本：

```bash
python3 scripts/fetch-qlib-release.py \
  --manifest data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json \
  --archive data/quant/qlib-releases/2026-09-28/qlib_bin.tar.gz \
  --workers 4
```

```bash
PYTHONPATH=src python3 -m quant_lab.qlib_archive \
  --archive data/quant/qlib-releases/2026-09-28/qlib_bin.tar.gz \
  --manifest data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json \
  --expected-tag 2026-09-28 \
  --sample 603993.SH \
  --reference-snapshot reports/quant/snapshots/2026-09-29-baostock-historical-shsz-590b41a3bd0a.json
```

归档**完全下载后**运行。退出码 0 表示归档字节、目录结构和基础格式通过本检查器，JSON 给出交易日历范围、股票数、日线有效值总数、逐年覆盖、`all.txt` 截止日在目标日前的代码数，以及样本股票首末数行。传入 BaoStock 历史股票目录时，还会列出其中在 Qlib `all.txt` 与收盘价特征里出现的代码数量，包含该目录标记为退出的代码。退出代码与 `all.txt` 提前结束均不能单独证明历史逐日可交易状态。

确认检查结果后，可安全解压并原子发布到一个**尚不存在**的目录。发布器再次核对归档哈希，拒绝链接、路径逃逸和重复成员，为每个文件记录 SHA-256；失败时只清除这次创建的临时目录。已发布的目录不会被覆盖。

```bash
PYTHONPATH=src python3 -m quant_lab.qlib_local publish \
  --archive data/quant/qlib-releases/2026-09-28/qlib_bin.tar.gz \
  --manifest data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json \
  --expected-tag 2026-09-28 \
  --out data/quant/qlib-releases/2026-09-28/published
```

随后按代码和日期读取独立归档。每次读取都会对照 manifest 与发布索引，并验证实际读取文件的哈希；`--limit` 默认只返回区间内最近 100 个交易日，避免把多年数据一次输出到终端。以下价格保持 Qlib 复权口径，`factor` 与 `amount` 仅展示原字段，不自动还原为用户成交价。

发布索引是本地生成的完整性记录，并非第三方签名；若怀疑索引和数据一起被更改，应从原始 Release 归档重新验证和发布。

```bash
PYTHONPATH=src python3 -m quant_lab.qlib_local stock \
  --root data/quant/qlib-releases/2026-09-28/published \
  --manifest data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json \
  --expected-tag 2026-09-28 \
  --stock 603993.SH --start 2026-01-01 --end 2026-09-28 \
  --fields close,factor,amount --limit 20
```

本检查器按 [Qlib 官方写入格式](https://github.com/microsoft/qlib/blob/main/scripts/dump_bin.py) 读取 `calendars/day.txt`、`instruments/all.txt` 与 `features/<代码>/<字段>.day.bin`：二进制首个小端 `float32` 是交易日历起始索引，后续是逐日值；NaN 保留为缺失。它会拒绝非规范日历或股票区间、越界二进制索引、危险 tar 成员，并统计缺失与非正/非有限收盘价。样本里的价格是 **Qlib 复权价**，`volume` 也可能复权；不得直接与用户成本价、原始成交量或当前主库价格比较。[Qlib 数据说明](https://github.com/microsoft/qlib/blob/main/docs/component/data.rst)。

## 本地个股查询与覆盖摘要

服务先查当前主行情库；若代码未找到、覆盖摘要有效且对应 Release 已安全发布，则按需从独立 Qlib 目录统计该股全史有效收盘数与首末日期，再用最近最多 256 个日历行生成走势和价格风险观察。只支持六位代码或带交易所的代码；Qlib 原始目录不含中文名称，沪深股票的显示名称另从经过快照 ID 核对的 BaoStock 本地清单补充，并标出它不是历史日期名称。报告显示 Release、归档 SHA-256 和读取窗口；原价、成本价差异、人民币成交额分别受独立的复权因子与成交额单位核验开关控制。当前开关关闭：报告不把复权价格当原价，也不产生预测、回测或荐股。主库已有的 `603993` 继续使用主库报告。

页面覆盖摘要可由实际检查报告和已发布索引生成：

```bash
python3 scripts/publish-qlib-summary.py \
  --inspection reports/quant/qlib/inspect-2026-09-28.json \
  --published-index data/quant/qlib-releases/2026-09-28/published/release-index.json \
  --output reports/quant/qlib/latest-summary.json
```

摘要只说明哪些代码有有效历史收盘价，不代表今天可交易；归档没有逐日当时可得的股票池，也尚未经过市场级样本外策略与预测检验。因此它不进入现有荐股研究池。`scripts/deploy-local-docker.sh` 只负责服务构建启动，不执行归档下载、检查或摘要发布。

覆盖检查还不是全量质量认证：它没有验证所有 OHLC 值之间的关系、成交额单位、公司行动和历史版本，也没有建立当时可得的股票池。正式研究前，需按股票与交易日对照独立原始行情，核验 `$factor` 还原价格、停牌/退市和缺口。当前主库 `daily_bars` 的主键不含数据来源或 Release 版本，因此不能把该归档直接 upsert 进去。

源项目代码许可和行情数据使用条件应分别核对；格式校验与哈希验证只证明拿到预期文件，不代表获得任意再分发或商用权限。归档数据不应打进本仓库或服务镜像。

## 2026-09-29 实际验收

固定 `2026-09-28` Release：源项目的 `validate_archive.py --require-publishable` 返回 `ok: true`；本地检查器复核 566,694,784 字节、SHA-256 `73f17f04b8710809f2881e20c3322990b39ecddf376f4b0e6d0c9acd0a6dede5`。交易日历为 2000-01-04 至 2026-09-28、6,480 日。`all.txt` 共 6,161 个代码（上交所 2,472、深交所 3,092、北交所前缀 `BJ` 597），10 类特征各有 6,161 个 `.day.bin` 文件。全部 6,161 个代码至少有一个有效收盘价，共 17,388,847 个有效收盘值，另有 577,164 个 NaN；本检查器没有发现非正或无穷收盘值。逐年数字见 `reports/quant/qlib/inspect-2026-09-28.json`。

与 2026-09-29 保存的 BaoStock 沪深 A 股目录对照：其 5,559 个代码中 5,557 个存在于该 Qlib Release，缺 `000508.SZ`、`600849.SH`；其 337 个 `status=0` 代码中 335 个有有效收盘价。Qlib `all.txt` 有 599 个代码区间早于目标日结束，但这不能直接等同退市数量。安全发布目录包含 61,618 个文件、726,974,038 字节解压内容及逐文件哈希索引，见 `reports/quant/qlib/publish-2026-09-28.json`。

跨源样本对账保存在 `reports/quant/qlib/crosscheck-2026-09-28.json`：`603993.SH` 最近 3 日与当前东方财富主库，`600000.SH` 早期 9 日、退出代码 `000003.SZ` 早期 5 日和 `000002.SZ` 早期 5 日与 BaoStock 原始日线，共 4 股、22 个同日配对。`Qlib close / factor` 与原始收盘价最大绝对差约 0.00000225 元；`Qlib amount × 1000` 与人民币成交额最大绝对差 109 元、最大相对差约 0.0000051%。这支持**该 Release 的这批样本**中 `amount` 的千元口径推断，仍需扩大股票、年代和公司行动日期的核查后才能作为统一转换规则。当前单股读取器因此仍展示源端 `amount`，不自动换算。

扩大的只读交叉检查可复现为：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/crosscheck-qlib-broad.py \
  --qlib-root data/quant/qlib-releases/2026-09-28/published \
  --manifest data/quant/qlib-releases/2026-09-28/qlib_bin.manifest.json \
  --expected-tag 2026-09-28 \
  --historical-db data/quant/historical-baostock-raw.sqlite3 \
  --main-db data/quant/market.sqlite3 \
  --historical-quantiles 48 \
  --out reports/quant/qlib/crosscheck-broad-2026-09-28.json
```

脚本先通过 SQLite backup API 将两个正在使用的源库复制为一致的临时只读快照，按代码均匀抽样 BaoStock 已入库股票，再逐日配对；报告记录快照 SHA-256、来源、复权口径、Run ID 摘要和误差分布。源库仍在回填，重跑时可选股票与配对数量会变化。2026-09-29 07:29 UTC 的[报告](../reports/quant/qlib/crosscheck-broad-2026-09-28.json)记录：当时有 332 只股票具备至少 30 条 BaoStock 原始记录，抽取其中 50 只，并对主库近期 4 只股票另作配对。合计 32,409 个收盘价配对；其中 32,351 个是 BaoStock 原价配对，30,900 个有双方人民币成交额。历史原价误差中位数约 `0.00000016` 元、99 分位约 `0.00000402` 元；`Qlib amount × 1000` 对人民币成交额的相对误差 99 分位约 `5.42×10⁻⁸`，最大约 `4.37×10⁻⁵`。这为**该版本、该抽样**的千元口径提供更广的实测支持，但单股读取器仍保留源端数值与核验开关。

历史原价配对有 16 个超过 `0.01` 元或 `0.1%` 的价格分歧，均在 `000002.SZ`、`000003.SZ` 的 2000 年数据，最大 `0.03` 元。对应日期的成交额仍大体吻合；目前没有第三个独立原始价格源确认哪一方价格正确。主库的 58 个近期配对标记为 `qfq`；其中 `603993.SH` 在 2026-09-01 至 09-08 的主库价比 Qlib `close/factor` 小 `0.09` 元，9 日起接近一致。这 6 个是不同价格口径间的差异，不能计入原价一致率。

另有 224 条 BaoStock 记录没有 Qlib 收盘价：逐条状态均为 `tradestatus=0`，BaoStock 保存了停牌日的沿用价格；Qlib 对应日期位于交易日历及股票区间内，但行情值为空，`factor` 仍有值。因此这些是**停牌日表示方式差异**，不是归档缺少股票或日期。报告保留这些行与状态计数，价格误差统计只使用可配对值。样本在 2024—2026 年较广，2000—2004 年仅覆盖 1—3 只股票；仍不能推出全市场跨年代的原价、公司行动或上市时点正确性。
