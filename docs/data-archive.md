# 本地数据快照

2026-09-29 的完整数据包：`quant-full-20260929T101320Z-ef8df7a8.zip`。它保存在项目的 `backups/` 目录，由使用者自行转存到 iCloud Drive；ZIP 本体不进入 Git。

| 字段 | 值 |
| --- | --- |
| ZIP 字节数 | 1,258,402,184 |
| 文件数 | 61,722 |
| 未压缩数据字节数 | 1,728,918,392 |
| ZIP SHA-256 | `b922dee36afd6f3a9a14cad86aaedea5ad55ded462c941ba3a07388627870079` |
| 内容 | `data/quant/`、`reports/quant/`、`manifest.json` |

4 个 SQLite 数据库通过 SQLite backup API 取得一致性快照；临时 WAL、SHM、journal 和锁文件不在 ZIP 内。`manifest.json` 保存每个条目的路径、大小及 SHA-256。已逐项通过 ZIP CRC 和清单 SHA-256 校验；原数据未删除。

在项目根目录重新校验：

```bash
python3 scripts/package-quant-data.py --verify backups/quant-full-20260929T101320Z-ef8df7a8.zip
```

恢复时先解包到空目录，再把 `data/quant/` 与 `reports/quant/` 放入目标项目，避免覆盖正在使用的 SQLite：

```bash
unzip -q /path/to/quant-full-20260929T101320Z-ef8df7a8.zip -d /path/to/empty-restore-dir
```
