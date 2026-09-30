# 本地数据快照

2026-09-30 更新的完整数据包：`quant-full-20260930T094604Z-060041c9.zip`。保存在项目的 `backups/` 目录，可自行转存到 iCloud Drive；ZIP 本体不进入 Git。

| 字段 | 值 |
| --- | --- |
| ZIP 字节数 | 1,287,888,123（约 1.29 GB） |
| 文件数（不含清单） | 61,831 |
| 未压缩数据字节数 | 1,924,165,757 |
| ZIP SHA-256 | `a6f18ca89f8b2bab996209c32d6b4f2f883f00d14385586ca1bdec8805bc9f62` |
| 内容 | `data/quant/`、`reports/quant/`、`studies/`、`policies/`、`manifest.json` |
| 转存校验文件 | `quant-full-20260930T094604Z-060041c9.zip.sha256` |

本次增加共享因子库、随机入场与公司行动回放证据、官方指数调样附件、预测记录及事实核验、模拟账户账本和策略配置。`studies/` 中的逐笔实验文件此前未被旧脚本覆盖，现随完整包保存。数据截止日按数据集分别记录；包生成于 9 月 30 日不代表全部股票或行业数据都已更新到当天。

5 个 SQLite 数据库通过 SQLite backup API 分别取得一致性快照；临时 WAL、SHM、journal 和锁文件不在 ZIP 内。`manifest.json` 保存每个条目的路径、大小及 SHA-256。已逐项通过 ZIP CRC 和清单 SHA-256 校验。五个数据库从 ZIP 实际恢复后均通过 `PRAGMA integrity_check`，结果为 `ok`；这验证数据库完整性，不等于行情与策略正确性证明。原文件和上一版 ZIP 均保留。

在项目根目录重新校验：

```bash
python3 scripts/package-quant-data.py --verify backups/quant-full-20260930T094604Z-060041c9.zip
cd backups
shasum -a 256 -c quant-full-20260930T094604Z-060041c9.zip.sha256
```

恢复时先解包到空目录，再核对清单和代码版本：

```bash
unzip -q /path/to/quant-full-20260930T094604Z-060041c9.zip -d /path/to/empty-restore-dir
```

停用写入服务后，将 `data/quant/`、`reports/quant/` 和 `studies/` 放入目标项目，避免覆盖正在使用的 SQLite。`policies/` 是此次实验的配置上下文；恢复前比较目标代码中的配置及模拟账户已冻结的哈希，不能直接覆盖正在运行的策略参数。源码由本轮代码 PR 提供，ZIP 不包含前端或 Python 源码。

上一版数据包为 `quant-full-20260929T101320Z-ef8df7a8.zip`，SHA-256 为 `b922dee36afd6f3a9a14cad86aaedea5ad55ded462c941ba3a07388627870079`；该包仅包含 `data/quant/`、`reports/quant/`。
