import { useState } from 'react';
import { finite, text } from '../format';
import type { ConsoleStatus } from '../types';
import { Notice, SectionHeader } from './ui';

type Source = 'main' | 'historical' | 'qlib';

const integer = new Intl.NumberFormat('zh-CN');

function count(value: unknown): string {
  return finite(value) ? integer.format(value) : '—';
}

function when(value: unknown): string {
  if (typeof value !== 'string') return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short',
  }).format(date);
}

function volumeUnit(value: unknown): string {
  if (value === 'lot') return '手';
  if (value === 'share') return '股';
  return text(value, '未标明');
}

function runState(value: unknown): string {
  if (value === 'running') return '运行中';
  if (value === 'completed') return '已完成';
  if (value === 'failed') return '失败';
  if (value === 'partial') return '部分完成';
  if (value === 'interrupted') return '中断';
  return text(value, '状态未知');
}

function Metric({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return <div className="card mini-card">
    <div className="label">{label}</div>
    <div className="value">{value}</div>
    {detail && <div className="minor">{detail}</div>}
  </div>;
}

function SourceUnavailable({ reason }: { reason?: string }) {
  return <Notice error>{text(reason, '此来源的盘点摘要不可校验，数量未知。')}</Notice>;
}

function MainSource({ data }: { data: ConsoleStatus }) {
  const main = data.raw_data_inventory?.main;
  const groups = data.market_store?.groups ?? {};
  return <div>
    <div className="section-label"><h3>主库前复权股票 · 盘点快照</h3><span>OHLC、成交量和成交额按已入库记录统计</span></div>
    {main?.status === 'ready' ? <>
      <div className="grid three">
        <Metric label="股票代码" value={count(main.stock_count)} detail="仅已选研究股票" />
        <Metric label="股票日线" value={count(main.bar_count)} detail={`前复权价格；成交量单位：${volumeUnit(main.volume_unit)}`} />
        <Metric label="人民币成交额" value={count(main.amount_cny_rows)} detail={`${count(main.amount_null_rows)} 条日线成交额缺失`} />
      </div>
      <p className="helper">快照中的成交量单位：{volumeUnit(main.volume_unit)}。主库前复权价不能与 BaoStock 原价或 Qlib 调整价直接混用。</p>
      {main.latest_run?.run_id && <p className="helper">盘点时所见最近采集 Run ID：<span className="trace-id">{main.latest_run.run_id}</span>；状态仅代表盘点时：{runState(main.latest_run.status_at_snapshot)}。</p>}
    </> : <SourceUnavailable reason={main?.reason} />}

    <div className="section-label"><h3>活跃行情库分组</h3><span>此摘要与上方盘点快照口径不同</span></div>
    <div className="grid three">
      {([
        ['stock', '个股'], ['index', '宽基指数'], ['industry', '行业指数'],
      ] as const).map(([key, label]) => <Metric key={key} label={label}
        value={`${count(groups[key]?.instrument_count)} 个代码`}
        detail={`${count(groups[key]?.bar_count)} 条日线 · ${text(groups[key]?.first_date)} 至 ${text(groups[key]?.last_date)}`} />)}
    </div>
  </div>;
}

function HistoricalSource({ data }: { data: ConsoleStatus }) {
  const historical = data.raw_data_inventory?.historical;
  const archive = data.historical_archive;
  const batch = data.historical_backfill_batch;
  const lastBatch = batch?.last_batch;
  const years = Array.isArray(historical?.year_exchange) ? historical.year_exchange : [];
  const missingYears = Array.isArray(historical?.zero_stock_years) ? historical.zero_stock_years : [];
  const missing2005to2023 = Array.from({ length: 19 }, (_, index) => String(2005 + index))
    .every(year => missingYears.includes(year));
  const recentShanghaiGap = years.filter(item => item.year >= 2024 && item.sh_bars === 0)
    .map(item => item.year);

  return <div>
    <div className="section-label"><h3>BaoStock 原价归档 · 盘点快照</h3><span>与主库前复权价分开保存</span></div>
    {historical?.status === 'ready' ? <>
      <div className="grid three">
        <Metric label="已有股票日线的代码" value={count(historical.stock_count)} detail={`目录有 ${count(historical.catalogue_stock_count)} 个股票代码`} />
        <Metric label="规范化日线" value={count(historical.bar_count)} detail={`未复权价格；成交量单位：${volumeUnit(historical.volume_unit)}`} />
        <Metric label="原始状态行" value={count(historical.status_rows)} detail="保留采集状态和原始字段" />
      </div>
      <p className="helper">{count(historical.amount_cny_rows)} 条规范化日线有人民币成交额；另有 {count(historical.raw_amount_only_rows)} 条只在原始状态表保留成交额。{historical.archive_complete === false ? '盘点时归档尚未补齐。' : ''}</p>

      <div className="section-label"><h3>逐年覆盖缺口</h3><span>首末日期不能代表中间年份已覆盖</span></div>
      {missing2005to2023 ? <Notice>盘点快照显示：2005—2023 年沪深两市均无股票日线。</Notice>
        : missingYears.length > 0 ? <Notice>盘点快照中没有股票日线的年份：{missingYears.join('、')}。</Notice>
          : <p className="helper">快照未列出完全空白的年份；可查看下方逐年记录核对覆盖。</p>}
      {recentShanghaiGap.length > 0 && <p className="helper">{recentShanghaiGap.join('、')} 年沪市股票日线为 0 条（盘点时）。</p>}
      {years.length > 0 && <details className="console-detail">
        <summary>查看逐年沪深日线条数</summary>
        <div className="table-wrap"><table><thead><tr><th>年份</th><th>沪市日线</th><th>深市日线</th></tr></thead><tbody>
          {years.map(item => <tr key={item.year}><td>{item.year}</td><td>{count(item.sh_bars)}</td><td>{count(item.sz_bars)}</td></tr>)}
        </tbody></table></div>
      </details>}
      {historical.latest_run?.run_id && <Notice>
        盘点时最近回填 Run ID <span className="trace-id">{historical.latest_run.run_id}</span>，当时状态为“{runState(historical.latest_run.status_at_snapshot)}”。这是盘点快照，不能据此判断后台任务当前是否仍在运行。
      </Notice>}
    </> : <SourceUnavailable reason={historical?.reason} />}

    <div className="section-label"><h3>最近已完成批次</h3><span>批次记录，不是后台进程当前状态</span></div>
    {batch?.status === 'ready' && lastBatch ? <div className="card console-data-card">
      <div className="grid three">
        <Metric label="请求窗口" value={count(lastBatch.requested_window_count)} detail={`${count(lastBatch.completed_window_count)} 完成 · ${count(lastBatch.empty_window_count)} 空窗 · ${count(lastBatch.failed_window_count)} 失败`} />
        <Metric label="本批次股票日线" value={count(lastBatch.bar_count)} detail={`${count(lastBatch.status_count)} 条原始状态行`} />
        <Metric label="剩余窗口" value={count(lastBatch.remaining_window_count)} detail={`${count(lastBatch.deferred_failure_count)} 个延后重试失败窗口`} />
      </div>
      <p className="helper">记录文件更新于 {when(batch.file_modified_at)} · Run ID <span className="trace-id">{text(lastBatch.run_id)}</span> · 检查点 ID <span className="trace-id">{text(lastBatch.snapshot_id)}</span>。</p>
      {lastBatch.error && <p className="console-error">批次错误：{lastBatch.error}</p>}
      <p className="helper">这里只能确认这份已完成批次记录及当时剩余工作量；不能据此判断回填进程现在是否在运行。</p>
    </div> : <p className="helper">{batch?.status === 'unavailable' ? '最近批次记录暂不可读取。' : '当前没有可展示的已完成批次记录。'} 无法由此判断回填进程当前状态。</p>}

    <div className="section-label"><h3>已发布归档检查点</h3><span>独立于上方盘点快照</span></div>
    {archive ? <div className="card console-data-card">
      <p className="console-data-value">{count(archive.stock_count)} <small>只有日线的股票</small></p>
      <p className="sub">{count(archive.bar_count)} 条原始日线 · {count(archive.completed_window_count)} 个完成分段 · {count(archive.failed_window_count)} 个失败分段</p>
      <p className="helper">发布于 {when(archive.published_at)}。检查点 ID：<span className="trace-id">{text(archive.snapshot_id)}</span>。这份归档不代表已纳入策略回测或荐股。</p>
    </div> : <p className="helper">没有可读取的已发布归档检查点。</p>}
  </div>;
}

function QlibSource({ data }: { data: ConsoleStatus }) {
  const qlib = data.raw_data_inventory?.qlib;
  const archive = data.qlib_archive;
  return <div>
    <div className="section-label"><h3>Qlib 固定发布包 · 文件盘点</h3><span>复权研究数据，独立于主库和 BaoStock</span></div>
    {qlib?.status === 'ready' ? <>
      <div className="grid three">
        <Metric label="股票代码" value={count(qlib.stock_count)} detail="来源包内的股票目录" />
        <Metric label="交易日历" value={count(qlib.calendar_sessions)} detail={`${text(qlib.calendar_start)} 至 ${text(qlib.calendar_end)}`} />
        <Metric label="特征种类" value={count(qlib.feature_count)} detail={`每种 ${count(qlib.files_per_feature)} 个文件`} />
      </div>
      <p className="helper">{qlib.field_coverage_is_file_presence_only ? '字段覆盖仅按文件是否存在核验，未扫描各字段有效值。' : '字段有效值覆盖率未在此页核验。'} 成交量单位：{volumeUnit(qlib.volume_unit)}；成交额单位：{text(qlib.amount_unit, '来源原生，未核实')}。</p>
      {qlib.feature_fields?.length ? <details className="console-detail"><summary>查看盘点到的特征字段</summary><p>{qlib.feature_fields.join('、')}</p></details> : null}
    </> : <SourceUnavailable reason={qlib?.reason} />}

    <div className="section-label"><h3>有效收盘价归档</h3><span>另一次校验的结果</span></div>
    {archive ? <div className="card console-data-card">
      <p className="console-data-value">{count(archive.stock_count_with_valid_close)} <small>个有效收盘代码</small></p>
      <p className="sub">{count(archive.bar_count)} 条有效收盘日线 · {text(archive.calendar_first)} 至 {text(archive.calendar_last)}</p>
      <p className="helper">校验于 {when(archive.validated_at)} · 发布标识 {text(archive.release_tag)}。归档尚未用于当前策略回测或荐股。</p>
    </div> : <p className="helper">没有可读取的有效收盘价归档摘要。</p>}
  </div>;
}

function SyncRuns({ data }: { data: ConsoleStatus }) {
  const runs = Array.isArray(data.market_store?.latest_sync_runs) ? data.market_store.latest_sync_runs : [];
  return <section className="space" aria-label="主库最近同步记录">
    <div className="section-label"><h3>主库最近同步记录</h3><span>独立于原始数据盘点快照；不代表 BaoStock 回填当前状态</span></div>
    {runs.length ? <div className="table-wrap"><table><thead><tr><th>开始时间 / Run ID</th><th>结果</th><th>请求区间</th><th>代码数</th><th>入库行数</th><th>结束时间</th></tr></thead><tbody>
      {runs.map((run, index) => <tr key={run.run_id || index}>
        <td className="console-run-id"><strong>{when(run.started_at)}</strong><small className="trace-id">{text(run.run_id)}</small></td>
        <td>{runState(run.status)}{run.error && <small className="console-error">{run.error}</small>}</td>
        <td>{text(run.requested_start)} 至 {text(run.requested_end)}</td>
        <td>{count(run.instrument_count)}</td><td>{count(run.row_count)}</td><td>{when(run.finished_at)}</td>
      </tr>)}
    </tbody></table></div> : <div className="card loading-card">{data.market_store?.status === 'unavailable' ? '主库同步记录暂不可读取。' : '当前没有可展示的主库同步记录。'}</div>}
  </section>;
}

export default function QuantDataManager({ data }: { data: ConsoleStatus }) {
  const [source, setSource] = useState<Source>('main');
  const inventory = data.raw_data_inventory;
  const inventoryAvailable = inventory?.status === 'ready' || inventory?.status === 'partial';
  const sources: { id: Source; title: string; detail: string }[] = [
    { id: 'main', title: '主库前复权', detail: '活跃行情与已选股票' },
    { id: 'historical', title: 'BaoStock 原价', detail: '历史回填与逐年缺口' },
    { id: 'qlib', title: 'Qlib 调整价', detail: '固定发布包与字段' },
  ];

  return <div className="quant-data-manager">
    <SectionHeader title="数据管理" subtitle="按来源查看库存、覆盖缺口和同步记录；所有操作均为只读。" badge={inventoryAvailable ? `盘点于 ${when(inventory.snapshot_at)}` : '盘点不可用'} />
    {inventoryAvailable ? <div className="card console-data-card">
      <strong>盘点快照</strong>
      <p className="sub">{when(inventory.snapshot_at)} · 报告 {text(inventory.report_filename)} · 距今 {count(inventory.snapshot_age_minutes)} 分钟</p>
      <p className="helper">下方来源库存是当时保存的观测。后续采集可能改变数量；盘点中记录的 Run 状态也只代表盘点时。</p>
    </div> : <Notice error>{text(inventory?.reason, '原始数据盘点快照暂不可读取或校验。')}</Notice>}

    <div className="section-label"><h3>数据来源</h3><span>选择一种口径查看细节</span></div>
    <div className="quant-data-sources" role="group" aria-label="选择数据来源">
      {sources.map(item => <button key={item.id} type="button" className={`quant-data-source${source === item.id ? ' active' : ''}`}
        aria-pressed={source === item.id} onClick={() => setSource(item.id)}>
        <strong>{item.title}</strong><small>{item.detail}</small>
      </button>)}
    </div>
    <section className="quant-data-source-panel" aria-label={`${sources.find(item => item.id === source)?.title}数据详情`}>
      {source === 'main' ? <MainSource data={data} /> : source === 'historical' ? <HistoricalSource data={data} /> : <QlibSource data={data} />}
    </section>

    <SyncRuns data={data} />
    <p className="helper">本页没有启动或停止同步的操作。不同来源的价格与成交量单位保持隔离；已归档数据不自动进入回测、预测或荐股。盘点摘要不能证明历史时点数据当时已公开。</p>
  </div>;
}
