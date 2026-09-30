# 沪深300历史成分来源审计（2026-09-30）

本记录区分**官方已核实公告**与**尚未补齐的每日成分链**。在中证指数官网“新闻与公告 → 指数调样 → 中证指数”列表逐项核对下列定期公告的标题、发布日期与详情 ID；**只有 2021-05-28 和 2026-05-29 两条继续打开正文核实了沪深300数量与生效日**。其余行没有在本次审计中独立核实生效日，不应按日历规则代填。

| 公告发布日期 | 官方详情 | 本次核实范围 |
| --- | --- | --- |
| 2021-05-28 | [12470](https://www.csindex.com.cn/#/about/newsDetail?id=12470) | 沪深300调换25只，2021-06-11收盘后生效；正文有25行增删表 |
| 2021-11-26 | [13888](https://www.csindex.com.cn/#/about/newsDetail?id=13888) | 列表标题和日期 |
| 2022-05-27 | [14223](https://www.csindex.com.cn/#/about/newsDetail?id=14223) | 列表标题和日期 |
| 2022-11-25 | [14497](https://www.csindex.com.cn/#/about/newsDetail?id=14497) | 列表标题和日期 |
| 2023-05-26 | [14796](https://www.csindex.com.cn/#/about/newsDetail?id=14796) | 列表标题和日期 |
| 2023-11-24 | [15044](https://www.csindex.com.cn/#/about/newsDetail?id=15044) | 列表标题和日期 |
| 2024-05-31 | [15267](https://www.csindex.com.cn/#/about/newsDetail?id=15267) | 列表标题和日期 |
| 2024-11-29 | [15471](https://www.csindex.com.cn/#/about/newsDetail?id=15471) | 列表标题和日期 |
| 2025-05-30 | [15690](https://www.csindex.com.cn/#/about/newsDetail?id=15690) | 列表标题和日期 |
| 2025-11-28 | [3006000](https://www.csindex.com.cn/#/about/newsDetail?id=3006000) | 列表标题和日期 |
| 2026-05-29 | [3006137](https://www.csindex.com.cn/#/about/newsDetail?id=3006137) | 沪深300调换19只，2026-06-12收盘后生效；有[官方 PDF 名单](https://oss-ch.csindex.com.cn/notice/20260529155822-%E9%99%84%E4%BB%B6%EF%BC%9A%E9%83%A8%E5%88%86%E6%8C%87%E6%95%B0%E6%A0%B7%E6%9C%AC%E8%B0%83%E6%95%B4%E5%90%8D%E5%8D%95.pdf) |

2021 年 5 月公告正文的调出/调入表包含 `600004 白云机场 → 600079 人福医药`、`603156 养元饮品 → 688111 金山办公` 等 25 对；[上交所同日公告](https://www.sse.com.cn/market/sseindex/diclosure/c/c_20210528_5476672.shtml)也明确了调换 25 只及 6 月 11 日收盘后生效。中证详情还链接 [2021-06-01 附件](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210601/1622509386248149.xlsx)与 [2021-06-10 附件](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210610/1623319091660435.xlsx)。两份原文件已分别归档至 [`official-sources`](../studies/random-entry-baseline-v1/official-sources/)；完整文件 SHA-256 分别为 `ea291da157e677490e8d7287f1fa33aa17f69665e76fe6b71b295b038b360e54`、`07e8c58d5f02cbaaf5e5b4a98f7c541208218d7a3de9b9627fbff37f31770a07`。通过[审计脚本](../scripts/csi300_official_delta_audit.py)按源文件哈希读取“调入”“调出”“备选名单”三个工作表后，两个版本在**沪深300相关行**完全相同：调入25、调出25、备选15；其他指数内容未逐项比较。附件只提供调样及备选名单，不提供当时完整300只期初成分。附件日期目前仅由官方链接路径推断，没有独立留存它们的发布时间；构建可得时点数据仍需取得发布时间证据。

[逐行对照报告](../studies/random-entry-baseline-v1/csi300-official-2021-06-delta-audit-v1.json)核实，Qlib 2026 发布包在 2021-06-15 至 06-29 的11个交易日仍保留全部25只调出股票、缺少全部25只调入股票，直到06-30才一次切换。原240个随机窗口中有2个入场日落在该区间，并各有3只入场选股发生变化。保持其他模拟条件不动，只替换这次调样对应的固定20股池，原策略 SMA 的复权收盘价代理收益分别从约 **+2.26% → +6.09%**、**+1.21% → -8.31%**。这是单次已证实调样的配对敏感性结果，不是完整逐日 PIT 修正，也不是真实可成交收益；另外两条对照政策在首个窗口仍因缺失持仓收盘价不可估值。原样本收益的稳健性因此不能以未修正的月度标签判断。

同一官方列表另见与沪深300有关的非定期公告：[2025-02-06](https://www.csindex.com.cn/#/about/newsDetail?id=15546)、[2025-07-25](https://www.csindex.com.cn/#/about/newsDetail?id=1006022)、[2026-09-09](https://www.csindex.com.cn/#/about/newsDetail?id=3006227)。本次只核实了后者正文涉及中金公司合并东兴证券/信达证券、成分处理与退市日相关，并链接 XLSX；前两项的具体调整内容和生效时点仍待核对。[沪深300官方编制方案](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000300_Index_Methodology_cn.pdf)第 7 节还规定新上市、并购、分立、停牌、退市、风险警示等临时调样情形。因此 11 次定期公告不足以保证每日成分完整。

补查的[上交所 2020-11-27 公告](https://www.sse.com.cn/market/sseindex/diclosure/c/c_20201127_5268298.shtml)证实，2020-12-14 生效的上一轮定期调整涉及沪深300更换26只，但其公告正文也只列部分股票，不能据此恢复 2021 年期初完整300只名单。

独立服务交叉检查也显示，**有按日期查询接口不等于有可靠的逐日成分历史**。2026-09-30 使用 BaoStock 0.9.4 的 `query_hs300_stocks(date=...)` 实际读取：2021-01-04 返回300只，与 Qlib 当天集合相同；2021-06-15、06-29、07-12 均仍返回官方已调出的全部25只且缺少全部25只调入股票，其中07-12返回的 `updateDate` 仍为 `2021-06-14`。2021-06-30 和07-12 的结果均与 Qlib 当天相差50个成员标签。这只说明两份回溯服务不能替代官方生效链；没有证明 2021-01-04 的共同名单正确或当年实时可得。

[官方指数详情](https://www.csindex.com.cn/#/indices/family/detail?indexCode=000300)当前有当期“样本列表”下载，但本次未找到可指定 2020/2021 日期的完整 300 只期初名单，也没有取得 2021–2026 全部临时调整及更正的历史链。当前仍须使用 `historical_daily_index_membership_confirmed=false` 和 `point_in_time_source_vintage_confirmed=false`；不能把 2026 Qlib 发布包按月填充的 `index_weight` 标签改称官方逐日 PIT 成分。可复算的下一步是取得带发布/生效时点的官方每日成分历史源，或先取得可信期初 300 名单，再逐条核实所有定期、临时及修订公告后重建、抽样对账；未补全前只作来源差异与回溯压力测试，不产生真实交易收益结论。
