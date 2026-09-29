import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { loadConsoleStatus, saveFeedbackCase, startCaseReview, startOptimization } from '../api';
import { errorMessage, finite, magnitudePercent, percent, text } from '../format';
import type { AlgorithmComponent, ConsoleStatus, ConsoleStrategy, StrategyMetrics } from '../types';
import { EmptyState, Notice, SectionHeader } from './ui';
import QuantDataManager from './QuantDataManager';

export type QuantModule = 'overview' | 'data' | 'strategy' | 'experiments' | 'forecast' | 'feedback';

const integer = new Intl.NumberFormat('zh-CN');

function count(value: unknown): string {
  return finite(value) ? integer.format(value) : '—';
}

function decimal(value: unknown): string {
  return finite(value) ? value.toFixed(2) : '—';
}

function oneDecimal(value: unknown): string {
  return finite(value) ? value.toFixed(1) : '—';
}

function reviewStatus(value: unknown): string {
  if (value === 'awaiting_outcome') return '等待到期';
  if (value === 'needs_source_review') return '来源修订，需复核';
  if (value === 'complete') return '价格结果已到期';
  if (value === 'partial') return '部分结果已到期';
  if (value === 'unavailable') return '数据不可用';
  return text(value, '状态待核验');
}

function taskKind(value: unknown): string {
  return value === 'optimize' ? '离线参数优化' : value === 'review_cases' ? '到期回执复盘' : text(value, '未知任务');
}

function taskState(value: unknown): string {
  if (value === 'running') return '运行中';
  if (value === 'completed') return '已完成';
  if (value === 'partial') return '部分完成';
  if (value === 'failed') return '失败';
  if (value === 'interrupted') return '执行中断';
  return text(value, '状态待核验');
}

function when(value: unknown): string {
  if (typeof value !== 'string') return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short',
  }).format(date);
}

function metricRows(metrics?: StrategyMetrics) {
  return [
    ['累计收益', percent(metrics?.total_return)],
    ['年化收益', percent(metrics?.cagr)],
    ['最大回撤', percent(metrics?.max_drawdown)],
    ['年化波动', percent(metrics?.annual_volatility)],
    ['夏普（无风险利率 0）', decimal(metrics?.sharpe_rf0)],
    ['换手指标', decimal(metrics?.turnover)],
  ];
}

function StrategyCard({ strategy }: { strategy: ConsoleStrategy }) {
  const backtest = strategy.backtest;
  const metrics = backtest?.metrics;
  const equalHold = backtest?.benchmarks?.focus_equal_weight_hold?.metrics;
  const csi300 = backtest?.benchmarks?.csi300_price_reference?.metrics;
  const split = backtest?.retrospective_split;
  const future = backtest?.prospective_out_of_sample;
  const parameters = strategy.parameters ?? {};
  const isPilot = strategy.status === 'retrospective_pilot';
  const strategyNames: Record<string, string> = {
    'sma-trend': '20 / 60 日均线趋势',
    'cross-sectional-momentum': '横截面动量',
    'mean-reversion-zscore': 'Z 分数均值回归',
  };

  return <article className="card console-strategy">
    <div className="console-strategy-head">
      <div>
        <span className="kicker">已保存的策略 · {text(strategy.strategy_id)}</span>
        <h3>{strategyNames[strategy.strategy_id ?? ''] || text(strategy.strategy_id, '未命名策略')}</h3>
        <p className="sub">版本 {text(strategy.strategy_version)} · {text(strategy.description, '已登记的策略实现')}{backtest?.status === 'available' ? ` · ${count(strategy.universe_count)} 只试验股 · 数据截至 ${text(strategy.asof)}` : ' · 尚无已归档回测'}</p>
      </div>
      <span className={`console-badge${isPilot || strategy.status === 'implemented_unvalidated' ? ' warning' : ''}`}>{isPilot ? '回顾性试验' : strategy.status === 'implemented_unvalidated' ? '已实现 · 未验证' : text(strategy.status, '状态未知')}</span>
    </div>

    {backtest?.status === 'available' ? <>
      <div className="console-metric-grid">
        {metricRows(metrics).map(([label, value]) => <div className="console-metric" key={label}>
          <span>{label}</span><strong>{value}</strong>
        </div>)}
      </div>
      {finite(strategy.north_star?.score) && <div className="console-north-star">
        <div><span className="kicker">回测优化目标</span><strong>{oneDecimal(strategy.north_star.score)} <small>/ 10</small></strong></div>
        <p>收益与最大回撤各占 50%；同一股票池等权持有的中性基线为 5.0。该分数只比较历史回看，不代表前瞻验证通过。<small>规则 {text(strategy.north_star.policy_version)}</small></p>
      </div>}
      <p className="console-context">回测 {text(backtest.start)} 至 {text(backtest.end)} · {count(backtest.observations)} 个观测日 · {count(strategy.universe_count)} 只事后选定试验股；这些指标不代表全市场选股能力。</p>

      <div className="section-label"><h3>与基准对照</h3><span>相同区间；沪深 300 仅为价格指数参考</span></div>
      <div className="table-wrap">
        <table><thead><tr><th>对象</th><th>累计收益</th><th>年化收益</th><th>最大回撤</th><th>夏普</th></tr></thead>
          <tbody>
            <tr><td>{strategyNames[strategy.strategy_id ?? ''] || text(strategy.strategy_id, '当前策略')}</td><td>{percent(metrics?.total_return)}</td><td>{percent(metrics?.cagr)}</td><td>{percent(metrics?.max_drawdown)}</td><td>{decimal(metrics?.sharpe_rf0)}</td></tr>
            <tr><td>试验股等权买入持有</td><td>{percent(equalHold?.total_return)}</td><td>{percent(equalHold?.cagr)}</td><td>{percent(equalHold?.max_drawdown)}</td><td>{decimal(equalHold?.sharpe_rf0)}</td></tr>
            <tr><td>沪深 300 价格指数（仅参考）</td><td>{percent(csi300?.total_return)}</td><td>{percent(csi300?.cagr)}</td><td>{percent(csi300?.max_drawdown)}</td><td>{decimal(csi300?.sharpe_rf0)}</td></tr>
          </tbody>
        </table>
      </div>

      {split?.status === 'retrospective_diagnostic_only' && <>
        <div className="section-label"><h3>分段回看</h3><span>事后诊断，非独立样本外检验</span></div>
        <div className="table-wrap"><table><thead><tr><th>区间</th><th>观测日</th><th>策略累计收益</th><th>等权持有累计收益</th><th>策略最大回撤</th></tr></thead><tbody>
          {(['early_period', 'recent_period'] as const).map((key, index) => {
            const period = split[key];
            return <tr key={key}><td>{index === 0 ? '前段' : '后段'} · {text(period?.start)} 至 {text(period?.end)}</td><td>{count(period?.sessions)}</td><td>{percent(period?.strategy_metrics?.total_return)}</td><td>{percent(period?.equal_hold_metrics?.total_return)}</td><td>{percent(period?.strategy_metrics?.max_drawdown)}</td></tr>;
          })}
        </tbody></table></div>
        <p className="helper">{text(split.reason, '规则没有在分界前冻结，分段结果只能回看。')}</p>
      </>}

      <div className="console-gate">
        <div><span className="kicker">独立前瞻检验</span><strong>{future?.status === 'pending' ? '待积累' : text(future?.status, '状态未知')}</strong></div>
        <p>{text(future?.reason, '尚未取得足够的新交易日验证。')}{finite(future?.matured_sessions) ? ` 已成熟 ${count(future.matured_sessions)} 个交易日。` : ''}</p>
      </div>

      <details className="console-detail"><summary>策略定义与证据链</summary>
        <dl>
          <div><dt>参数</dt><dd>{Object.keys(parameters).length ? Object.entries(parameters).map(([key, value]) => `${key}=${String(value)}`).join(' · ') : '未提供'}</dd></div>
          <div><dt>价格口径 / 股票池</dt><dd>{text(strategy.universe_scope)} · {Array.isArray(backtest.universe) ? backtest.universe.join('、') : '股票代码未提供'}</dd></div>
          <div><dt>执行假设</dt><dd>{text(backtest.execution_assumption)}</dd></div>
          <div><dt>单次成本假设</dt><dd>{percent(backtest.cost_rate, 2)}</dd></div>
          <div><dt>来源运行 ID</dt><dd className="trace-id">{text(strategy.source_run_id)}</dd></div>
          <div><dt>策略快照 ID</dt><dd className="trace-id">{text(strategy.record_id)}</dd></div>
          <div><dt>快照来源</dt><dd>{text(strategy.origin)}</dd></div>
          <div><dt>报告 SHA-256</dt><dd className="trace-id">{text(strategy.research_sha256)}</dd></div>
          <div><dt>输入指纹 SHA-256</dt><dd className="trace-id">{text(backtest.input_fingerprint_sha256)}</dd></div>
        </dl>
      </details>
      {Array.isArray(strategy.limitations) && strategy.limitations.length > 0 && <div className="console-limits"><strong>适用范围</strong><ul>{strategy.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
    </> : <>
      {Object.keys(parameters).length > 0 && <p className="console-context">参数：{Object.entries(parameters).map(([key, value]) => `${key}=${String(value)}`).join(' · ')}</p>}
      <Notice>{text(backtest?.reason, '该策略没有可核验的回测指标。')}</Notice>
    </>}
  </article>;
}

function todayShanghai(): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function FeedbackForm({ records, onSaved, today }: {
  records: NonNullable<NonNullable<ConsoleStatus['research_journal']>['recent']>;
  onSaved: () => void;
  today: string;
}) {
  const [recordId, setRecordId] = useState(records[0]?.record_id || '');
  const [decision, setDecision] = useState<'watch' | 'skip' | 'buy' | 'sell'>('watch');
  const [observedDate, setObservedDate] = useState(today);
  const [price, setPrice] = useState('');
  const [quantity, setQuantity] = useState('');
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedId, setSavedId] = useState<string | null>(null);
  const selected = records.find(record => record.record_id === recordId) || records[0];
  const isTrade = decision === 'buy' || decision === 'sell';

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setSavedId(null);
    if (!selected?.record_id || !selected.generated_at || selected.generated_at.length < 10) {
      setError('所选研究记录缺少日期或 ID，无法关联回执。');
      return;
    }
    const executedPrice = price.trim() ? Number(price) : undefined;
    const executedQuantity = quantity.trim() ? Number(quantity) : undefined;
    if (isTrade && executedPrice !== undefined && (!Number.isFinite(executedPrice) || executedPrice <= 0)) {
      setError('成交价须为大于 0 的数字。');
      return;
    }
    if (isTrade && executedQuantity !== undefined && (!Number.isSafeInteger(executedQuantity) || executedQuantity <= 0)) {
      setError('数量须为正整数。');
      return;
    }
    setSaving(true);
    try {
      const result = await saveFeedbackCase({
        research_record_id: selected.record_id,
        research_record_date: selected.generated_at.slice(0, 10),
        decision,
        observed_date: observedDate,
        ...(isTrade && executedPrice !== undefined ? { executed_price: executedPrice } : {}),
        ...(isTrade && executedQuantity !== undefined ? { quantity: executedQuantity } : {}),
        ...(note.trim() ? { note: note.trim() } : {}),
      });
      setSavedId(result.case_id || null);
      setPrice('');
      setQuantity('');
      setNote('');
      onSaved();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  };

  return <form className="card console-feedback-form" onSubmit={event => void submit(event)}>
    <div className="console-form-intro"><div><h3>记录这一例的人工决定</h3><p className="sub">选择当时看到的研究报告，再填你的回执。这里仅保存记录，不执行买卖。</p></div><span className="pill">本地留痕</span></div>
    <div className="console-form-grid">
      <label className="field"><span>关联研究记录</span><select value={selected?.record_id || ''} onChange={event => setRecordId(event.target.value)} required>
        {records.filter(record => record.record_id && record.generated_at).map(record => <option value={record.record_id} key={record.record_id}>{text(record.name, text(record.instrument_id))} · {text(record.asof)} · {record.record_id?.slice(0, 8)}</option>)}
      </select></label>
      <label className="field"><span>你的决定</span><select value={decision} onChange={event => setDecision(event.target.value as typeof decision)}>
        <option value="watch">观察</option><option value="skip">跳过</option><option value="buy">买入决定</option><option value="sell">卖出决定</option>
      </select></label>
      <label className="field"><span>决定日期</span><input type="date" value={observedDate} min={selected?.asof || undefined} max={today} onChange={event => setObservedDate(event.target.value)} required /></label>
    </div>
    {isTrade && <div className="console-form-grid space">
      <label className="field"><span>实际成交价（已成交时填写，元/股）</span><input type="number" min="0.001" step="any" value={price} onChange={event => setPrice(event.target.value)} placeholder="尚未成交可留空" /></label>
      <label className="field"><span>实际数量（已成交时填写，股）</span><input type="number" min="1" step="1" value={quantity} onChange={event => setQuantity(event.target.value)} placeholder="尚未成交可留空" /></label>
    </div>}
    <label className="field console-note"><span>备注（可选）</span><textarea maxLength={500} rows={2} value={note} onChange={event => setNote(event.target.value)} placeholder="为何观察、跳过或行动？" /></label>
    <div className="console-form-actions"><button type="submit" className="primary" disabled={saving}>{saving ? '保存中…' : '保存回执'}</button><span>买卖决定只记为回执；成交信息由你填写，不向券商发送指令。</span></div>
    {error && <Notice error>{error}</Notice>}
    {savedId && <p className="research-record" role="status">回执已保存 · case ID <span className="trace-id">{savedId}</span></p>}
  </form>;
}

function Operations({ data, onSaved, showFeedbackForm = false }: { data: ConsoleStatus; onSaved: () => void; showFeedbackForm?: boolean }) {
  const groups = data.market_store?.groups ?? {};
  const archive = data.historical_archive;
  const qlib = data.qlib_archive;
  const syncRuns = Array.isArray(data.market_store?.latest_sync_runs) ? data.market_store.latest_sync_runs : [];
  const journal = Array.isArray(data.research_journal?.recent) ? data.research_journal.recent : [];
  const groupNames: Record<string, string> = { stock: '个股主库', index: '宽基指数', industry: '行业指数' };

  return <>
    <div className="section-label"><h3>数据存量与任务</h3><span>只读状态，数字按各自口径统计</span></div>
    <div className="grid three">
      {(['stock', 'index', 'industry'] as const).map(key => <div className="card mini-card" key={key}>
        <div className="label">{groupNames[key]}</div><div className="value">{count(groups[key]?.instrument_count)} <small>个代码</small></div>
        <div className="minor">{count(groups[key]?.bar_count)} 条日线 · {text(groups[key]?.first_date)} 至 {text(groups[key]?.last_date)}</div>
      </div>)}
    </div>
    <div className="grid two space">
      <div className="card console-data-card"><h3>历史原始归档 · 已发布检查点</h3>
        <p className="console-data-value">{count(archive?.stock_count)} <small>只有日线的股票</small></p>
        <p className="sub">{count(archive?.bar_count)} 条原始日线 · {count(archive?.completed_window_count)} 个完成分段 · {count(archive?.failed_window_count)} 个失败分段</p>
        <p className="helper">检查点发布于 {when(archive?.published_at)}{archive?.snapshot_id ? ` · ID ${archive.snapshot_id.slice(0, 12)}…` : ''}。与原始数据储备盘点是不同时间的快照；归档不等于已纳入策略回测或荐股。</p>
      </div>
      <div className="card console-data-card"><h3>Qlib 复权归档</h3>
        <p className="console-data-value">{count(qlib?.stock_count_with_valid_close)} <small>个有效收盘代码</small></p>
        <p className="sub">{count(qlib?.bar_count)} 条有效收盘日线 · {text(qlib?.calendar_first)} 至 {text(qlib?.calendar_last)}</p>
        <p className="helper">复权研究口径；尚未用于当前策略回测或荐股。</p>
      </div>
    </div>

    <div className="section-label"><h3>最近同步任务</h3><span>每条任务保留运行 ID 与结果</span></div>
    {syncRuns.length ? <div className="table-wrap"><table><thead><tr><th>开始时间 / 运行 ID</th><th>状态</th><th>请求区间</th><th>代码数</th><th>入库行数</th></tr></thead><tbody>
      {syncRuns.map((run, index) => <tr key={run.run_id || index}><td className="console-run-id"><strong>{when(run.started_at)}</strong><small className="trace-id">{text(run.run_id)}</small></td><td>{text(run.status)}{run.error ? <small className="console-error">{run.error}</small> : null}</td><td>{text(run.requested_start)} 至 {text(run.requested_end)}</td><td>{count(run.instrument_count)}</td><td>{count(run.row_count)}</td></tr>)}
    </tbody></table></div> : <div className="card loading-card">没有可读取的同步任务记录。</div>}

    <div className="section-label"><h3>研究记录</h3><span>个股报告的本地留痕；仅列最近记录，不显示你的成本价</span></div>
    {journal.length ? <div className="table-wrap"><table><thead><tr><th>生成时间</th><th>股票</th><th>行情日期</th><th>记录 ID</th></tr></thead><tbody>
      {journal.map((item, index) => <tr key={item.record_id || index}><td>{when(item.generated_at)}</td><td>{text(item.name, text(item.instrument_id))}<small className="console-subline">{text(item.instrument_id)}</small></td><td>{text(item.asof)}</td><td className="trace-id">{text(item.record_id)}</td></tr>)}
    </tbody></table></div> : <div className="card loading-card">{data.research_journal?.status === 'unavailable' ? '研究记录库暂不可读取，数量未知。' : '当前没有可展示的研究记录。'}</div>}
    {showFeedbackForm && journal.length > 0 && <FeedbackForm records={journal} onSaved={onSaved} today={text(data.china_today, todayShanghai())} />}
  </>;
}

function RawDataInventory({ data }: { data: ConsoleStatus }) {
  const inventory = data.raw_data_inventory;
  const main = inventory?.main;
  const historical = inventory?.historical;
  const qlib = inventory?.qlib;
  const years = Array.isArray(historical?.year_exchange) ? historical.year_exchange : [];
  const missingYears = Array.isArray(historical?.zero_stock_years) ? historical.zero_stock_years : [];
  const fullMiddleGap = Array.from({ length: 19 }, (_, index) => String(2005 + index))
    .every(year => missingYears.includes(year));
  const recentShanghaiGap = years.filter(item => item.year >= 2024 && item.sh_bars === 0)
    .map(item => item.year);
  const unavailableCard = (reason: unknown) => <p className="sub">{text(reason, '此来源的盘点摘要不可校验，数量未知。')}</p>;
  return <div className="console-section">
    <SectionHeader title="原始数据储备" subtitle="已保存的原始观测与缺口；这里读取盘点快照，不重新扫描活跃行情库。" badge={inventory?.status === 'ready' || inventory?.status === 'partial' ? `快照 ${when(inventory.snapshot_at)}` : '快照不可用'} />
    {inventory?.status === 'unavailable' || !inventory ? <Notice error>{text(inventory?.reason, '原始数据盘点快照暂不可读取或校验。')}</Notice> : <>
      <p className="helper">盘点报告：{text(inventory.report_filename)} · 距今 {count(inventory.snapshot_age_minutes)} 分钟。数字仅代表盘点时的库存；{historical?.latest_run?.status_at_snapshot === 'running' ? `BaoStock 回填在盘点时仍运行（Run ID ${text(historical.latest_run.run_id)}），当前运行状态未由这份快照确认。` : '后续采集可能改变库存。'}</p>
      <div className="grid three space">
        <div className="card console-data-card"><h3>主库前复权股票</h3>
          {main?.status === 'ready' ? <><p className="console-data-value">{count(main.stock_count)} <small>只股票 · {count(main.bar_count)} 条日线</small></p>
            <p className="sub">OHLC 与成交量已存；成交量单位为{main.volume_unit === 'lot' ? '手' : text(main.volume_unit)}。</p>
            <p className="helper">{count(main.amount_cny_rows)} 条有人民币成交额；{count(main.amount_null_rows)} 条成交额缺失。仅覆盖已选研究股票。</p></> : unavailableCard(main?.reason)}
        </div>
        <div className="card console-data-card"><h3>BaoStock 原价归档</h3>
          {historical?.status === 'ready' ? <><p className="console-data-value">{count(historical.stock_count)} <small>只股票 · {count(historical.bar_count)} 条日线</small></p>
            <p className="sub">未复权 OHLC、成交量（{historical.volume_unit === 'share' ? '股' : text(historical.volume_unit)}）与状态行 {count(historical.status_rows)} 条。</p>
            <p className="helper">{count(historical.amount_cny_rows)} 条规范化日线有人民币成交额；另有 {count(historical.raw_amount_only_rows)} 条只在原始状态表保留成交额。股票目录 {count(historical.catalogue_stock_count)} 个代码，归档尚未补齐。</p></> : unavailableCard(historical?.reason)}
        </div>
        <div className="card console-data-card"><h3>Qlib 固定发布包</h3>
          {qlib?.status === 'ready' ? <><p className="console-data-value">{count(qlib.feature_count)} <small>类特征文件 · 每类 {count(qlib.files_per_feature)} 个</small></p>
            <p className="sub">{count(qlib.stock_count)} 个代码 · {count(qlib.calendar_sessions)} 个日历交易日 · {text(qlib.calendar_start)} 至 {text(qlib.calendar_end)}</p>
            <p className="helper">仅校验文件存在，未扫描各字段有效值；成交量为调整或未知单位，成交额为来源原生且未核单位。</p></> : unavailableCard(qlib?.reason)}
        </div>
      </div>
      {historical?.status === 'ready' && <>
        <p className="console-context">归档年份缺口：{fullMiddleGap ? '2005–2023 年沪深两市均无股票日线' : `共有 ${count(missingYears.length)} 个年份无股票日线`}；{recentShanghaiGap.length ? `${recentShanghaiGap.join('、')} 年沪市仍无日线` : '近年沪深覆盖见逐年表'}。首末日期不代表中间年份已覆盖。</p>
        <details className="console-detail"><summary>查看 BaoStock 逐年沪深日线条数</summary>
          <div className="table-wrap"><table><thead><tr><th>年份</th><th>沪市日线</th><th>深市日线</th></tr></thead><tbody>
            {years.map(item => <tr key={item.year}><td>{item.year}</td><td>{count(item.sh_bars)}</td><td>{count(item.sz_bars)}</td></tr>)}
          </tbody></table></div>
        </details>
      </>}
      <p className="helper">主库前复权价、BaoStock 未复权价和 Qlib 调整价保持分开；这份聚合摘要的哈希不是完整数据库哈希，也不能证明历史时点数据当时已公开。</p>
    </>}
  </div>;
}

function ForecastExperimentPool({ data }: { data: ConsoleStatus }) {
  const datasets = Array.isArray(data.forecast_experiments?.datasets)
    ? data.forecast_experiments.datasets : [];
  const verifiedCount = datasets.filter(item => item.latest).length;
  const providerStatus = (value: unknown): string => {
    if (value === 'ready') return '已完成';
    if (value === 'partial_failure') return '部分失败';
    if (value === 'insufficient_history') return '历史不足';
    if (value === 'unavailable') return '不可运行';
    if (value === 'provider_runtime_error') return '运行失败';
    return text(value, '待核验');
  };
  const horizonCell = (item: { status?: string; sample_count?: number | null;
    coverage?: number | null; mae_return_skill_vs_random_walk?: number | null } | undefined) =>
    item?.status === 'ready' ? <>
      <strong>{percent(item.mae_return_skill_vs_random_walk, 2)}</strong>
      <small className="console-subline">{count(item.sample_count)} 个起点 · 覆盖 {magnitudePercent(item.coverage, 0)}</small>
    </> : '—';
  return <div className="console-section">
    <SectionHeader title="预测模型实验" subtitle="同口径滚动预测误差试验；每类数据只展示最新的完整性校验记录。" badge={`${verifiedCount} 类已校验数据`} />
    {datasets.length ? datasets.map((source, index) => {
      const latest = source.latest;
      const label = source.dataset === 'qlib' ? 'Qlib 历史分层样本' : '主库关注股';
      return <div className="card console-data-card" key={source.dataset || index} style={{ marginTop: 12 }}>
        <div className="console-rule-head"><h3>{label}</h3><span className={`console-badge${source.status === 'ready' ? '' : ' warning'}`}>{source.status === 'ready' ? '归档已校验' : source.status === 'partial' ? '部分记录校验失败' : source.status === 'unavailable' ? '归档不可校验' : '暂无归档'}</span></div>
        {source.reason && <p className="helper">{source.reason}</p>}
        {latest ? <>
          <p className="console-context">{count(latest.stock_count)} 只股票 · {text(latest.data_start)} 至 {text(latest.data_asof)} · {latest.price_basis === 'forward_adjusted' ? '主库前复权' : latest.price_basis === 'qlib_adjusted' ? 'Qlib 调整价' : text(latest.price_basis)}{finite(latest.common_sessions) ? ` · ${count(latest.common_sessions)} 个共同交易观测` : ''}</p>
          <p className="console-context">记录于 {when(latest.created_at)} · 记录 ID <span className="trace-id">{text(latest.record_id)}</span> · 输入指纹 <span className="trace-id">{text(latest.input_fingerprint_sha256).slice(0, 16)}…</span></p>
          <div className="table-wrap"><table><thead><tr><th>预测模型</th><th>运行状态</th><th>5 步 MAE 改善</th><th>20 步 MAE 改善</th></tr></thead><tbody>
            {(latest.providers ?? []).map((provider, providerIndex) => <tr key={`${provider.name}-${providerIndex}`}>
              <td className="console-run-id"><strong>{provider.name === 'random-walk' ? '随机游走基线' : provider.name === 'momentum-20-extrapolation' ? '20 日动量外推' : text(provider.name)}</strong><small>{text(provider.model_id)}{provider.model_revision ? ` · ${provider.model_revision.slice(0, 12)}` : ''}</small></td>
              <td>{providerStatus(provider.status)}{provider.reason && <small className="console-error">{provider.reason}</small>}{provider.pretraining_overlap_status === 'unknown' && <small className="console-subline">预训练数据重叠未知</small>}{finite(provider.stocks_with_samples) && <small className="console-subline">{count(provider.stocks_with_samples)} 只有有效样本</small>}</td>
              <td>{horizonCell(provider.horizons?.['5'])}</td><td>{horizonCell(provider.horizons?.['20'])}</td>
            </tr>)}
          </tbody></table></div>
          <p className="helper">MAE 改善是相对同起点随机游走的收益率预测误差变化；正值表示误差更小。{source.dataset === 'qlib' ? '一步为该股下一个有效收盘观测。' : '一步为共同交易日。'}</p>
        </> : <p className="sub">{source.status === 'unavailable' ? '当前无法展示已校验实验结果。' : '当前没有已归档的预测模型实验。'}</p>}
      </div>;
    }) : <EmptyState title="尚无预测模型实验"><p>当前没有可读取的预测实验归档。</p></EmptyState>}
    <p className="helper">以上均为回顾性研究，未验证历史时点可见性、未来样本外表现或扣成本交易收益；不能直接作为买卖建议或策略北极星评分。</p>
  </div>;
}

function ForecastStabilityPool({ data }: { data: ConsoleStatus }) {
  const status = data.forecast_stability;
  const latest = Array.isArray(status?.latest) ? status.latest : [];
  const tierLabel = (value: unknown): string => {
    if (value === 'broadly_stable') return '广域稳定';
    if (value === 'conditional_scope_only') return '限定范围';
    return '粗预测/证据不足，弃答';
  };
  return <div className="console-section">
    <SectionHeader title="预测稳定性分层" subtitle="基于已保存的配对预测审计；事件窗口仅用于事后诊断。" badge={`${count(latest.length)} 个模型有审计`} />
    {status?.reason && <Notice error>{status.reason}</Notice>}
    {latest.length ? latest.map(item => <div className="card console-data-card" key={item.record_id || item.model_name} style={{ marginTop: 12 }}>
      <div className="console-rule-head"><h3>{text(item.model_name)}{item.model_revision ? ` · ${item.model_revision.slice(0, 16)}` : ''}</h3><span className="console-badge warning">仅研究</span></div>
      <p className="console-context">审计于 {when(item.created_at)} · 审计 ID <span className="trace-id">{text(item.record_id)}</span></p>
      <div className="table-wrap"><table><thead><tr><th>窗口</th><th>证据等级</th><th>全部样本 MAE 改善</th><th>普通 / 政策应激样本</th><th>覆盖率</th></tr></thead><tbody>
        {(['5', '20'] as const).map(horizon => {
          const result = item.horizons?.[horizon];
          return result ? <tr key={horizon}><td>{horizon} 步</td><td><strong>{tierLabel(result.tier)}</strong></td><td>{percent(result.mae_return_skill_vs_random_walk, 2)}</td><td>{count(result.core_count)} / {count(result.event_stress_count)} <small className="console-subline">共 {count(result.sample_count)} 条配对样本</small></td><td>{magnitudePercent(result.candidate_coverage, 0)}</td></tr> : null;
        })}
      </tbody></table></div>
      <p className="helper">政策应激标签按事件发生后建立；普通阶段剔除仅用于回看，分档使用全部配对样本。历史时点数据验证：{item.point_in_time_validated ? '通过' : '未通过'}；前瞻样本外：{item.prospective_out_of_sample ? '通过' : '未通过'}。</p>
      <details className="console-detail"><summary>查看来源记录 ID</summary><dl>
        <div><dt>预测实验</dt><dd className="trace-id">{text(item.source_benchmark_record_id)}</dd></div>
        <div><dt>事件研究</dt><dd className="trace-id">{text(item.event_study_record_id)}</dd></div>
      </dl></details>
    </div>) : <EmptyState title="尚无可核验的稳定性审计"><p>当前没有可以展示的预测稳定性分层记录。</p></EmptyState>}
    {latest.length > 0 && <p className="helper">“粗预测/证据不足，弃答”表示当前不输出可执行预测；预测误差等级也不能替代扣成本策略回测或黑天鹅风控验证。</p>}
  </div>;
}

function GroupBehaviorPilot({ data }: { data: ConsoleStatus }) {
  const source = data.group_behavior_pilot;
  const latest = source?.latest;
  return <div className="console-section">
    <SectionHeader title="群体量价预测试验" subtitle="固定股票群的涨跌、广度和相对量能代理；与个股收盘价模型分层分开。" badge={latest ? `审计于 ${when(latest.created_at)}` : '暂无已校验记录'} />
    {source?.reason && <Notice error>{source.reason}</Notice>}
    {latest ? <div className="card console-data-card">
      <div className="console-rule-head"><h3>{text(latest.model_name)} · {count(latest.cohort_size)} 只固定研究股</h3><span className="console-badge warning">仅研究</span></div>
      <p className="console-context">数据截至 {text(latest.calendar_end)} · 记录 ID <span className="trace-id">{text(latest.record_id)}</span></p>
      <div className="table-wrap"><table><thead><tr><th>窗口</th><th>常规配对样本</th><th>常规覆盖率</th><th>相对零收益 MAE 改善</th><th>政策公告前压力样本</th></tr></thead><tbody>
        {(['5', '20'] as const).map(horizon => {
          const result = latest.horizons?.[horizon];
          return result ? <tr key={horizon}><td>{horizon} 步</td><td>{count(result.regular_paired_count)} / {count(result.regular_candidate_origins)}</td><td>{magnitudePercent(result.regular_coverage, 1)}</td><td><strong>{percent(result.regular_mae_skill_vs_zero_return, 2)}</strong></td><td>{count(result.policy_stress_paired_count)} 条 <small className="console-subline">同一批 {count(latest.reviewed_event_count)} 次政策事件</small></td></tr> : null;
        })}
      </tbody></table></div>
      <p className="helper">政策样本使用已核实公告前的最后收盘作为起点，仅作事后压力诊断；4 次同类政策事件不能证明黑天鹅稳健。量价代理不是真实资金净流入，也不能识别机构身份。PIT 与前瞻验证尚未通过，不进入广域或限定范围等级。</p>
    </div> : <EmptyState title="群体量价试验暂无可核验归档"><p>尚未读取到完整性校验通过的试验记录。</p></EmptyState>}
  </div>;
}

function ExperimentPool({ data, mode }: { data: ConsoleStatus; mode: 'experiments' | 'feedback' }) {
  const experiments = Array.isArray(data.experiments?.recent) ? data.experiments.recent : [];
  const optimizations = Array.isArray(data.optimizations?.recent) ? data.optimizations.recent : [];
  const cases = Array.isArray(data.feedback_cases?.recent) ? data.feedback_cases.recent : [];
  const reviews = Array.isArray(data.reviews?.recent) ? data.reviews.recent : [];
  return <>
    {mode === 'experiments' && <>
    <div className="console-section"><SectionHeader title="实验记录池" subtitle="一条快照对应一次可追溯的研究设定与结果；同策略可有多次实验。" badge={`${count(experiments.length)} 条最近记录`} />
      {experiments.length ? <div className="table-wrap"><table><thead><tr><th>策略 / 实验 ID</th><th>截至</th><th>样本</th><th>累计收益</th><th>最大回撤</th><th>回看评分</th><th>验证阶段</th></tr></thead><tbody>
        {experiments.map((item, index) => <tr key={item.experiment_id || index}>
          <td className="console-run-id"><strong>{text(item.strategy_id)} · v{text(item.strategy_version)}</strong><small className="trace-id">{text(item.experiment_id, text(item.snapshot_record_id))}</small></td>
          <td>{text(item.asof)}</td><td>{count(item.universe_count)} 只</td><td>{percent(item.metrics?.total_return)}</td><td>{percent(item.metrics?.max_drawdown)}</td><td>{finite(item.north_star?.score) ? `${oneDecimal(item.north_star?.score)} / 10` : '—'}</td>
          <td>{item.state === 'retrospective_research' ? '回顾性实验' : text(item.state)}<small className="console-subline">前瞻：{item.validation?.prospective_status === 'pending' ? '待积累' : text(item.validation?.prospective_status)}</small></td>
        </tr>)}
      </tbody></table></div> : data.experiments?.status === 'unavailable' ? <Notice error>{text(data.experiments.reason, '实验记录池暂不可校验，数量未知。')}</Notice> : <EmptyState title="实验记录池为空"><p>{text(data.experiments?.reason, '暂无已校验的策略研究快照。')}</p></EmptyState>}
      {experiments.length > 0 && <p className="helper">每条实验保存输入指纹、参数、成本、执行假设与完整权益曲线；回顾性收益不代表独立时间外收益。</p>}
    </div>

    <div className="console-section"><SectionHeader title="参数优化实验" subtitle="训练段选择参数，历史留出段只检验已选参数；两段都属于历史回看。" badge={`${count(optimizations.length)} 条最近记录`} />
      {optimizations.length ? <div className="table-wrap"><table><thead><tr><th>策略 / 优化 ID</th><th>候选 / 选中参数</th><th>训练段</th><th>历史留出段</th><th>验证性质</th></tr></thead><tbody>
        {optimizations.map((item, index) => <tr key={item.optimization_id || index}>
          <td className="console-run-id"><strong>{text(item.strategy_id)} · v{text(item.strategy_version)}</strong><small className="trace-id">{text(item.optimization_id)}</small></td>
          <td className="console-description">{count(item.candidate_count)} 组 · {item.selected_parameters ? Object.entries(item.selected_parameters).map(([key, value]) => `${key}=${String(value)}`).join(' · ') : '参数未提供'}</td>
          <td>{text(item.train?.start)} 至 {text(item.train?.end)}<small className="console-subline">{count(item.train?.sessions)} 日 · {oneDecimal(item.train?.selected_north_star?.score)} / 10</small><small className="console-subline">年化 {percent(item.train?.selected_metrics?.cagr)} · 回撤 {percent(item.train?.selected_metrics?.max_drawdown)}</small></td>
          <td>{text(item.historical_holdout?.start)} 至 {text(item.historical_holdout?.end)}<small className="console-subline">{count(item.historical_holdout?.sessions)} 日 · {oneDecimal(item.historical_holdout?.north_star?.score)} / 10</small><small className="console-subline">年化 {percent(item.historical_holdout?.metrics?.cagr)} · 回撤 {percent(item.historical_holdout?.metrics?.max_drawdown)}</small></td>
          <td>{item.historical_holdout?.status === 'retrospective_holdout_only' ? '历史留出检验' : text(item.historical_holdout?.status)}<small className="console-subline">{item.promotion_status === 'not_eligible' ? '尚不可晋级' : text(item.promotion_status)}</small></td>
        </tr>)}
      </tbody></table></div> : data.optimizations?.status === 'unavailable' ? <Notice error>{text(data.optimizations.reason, '参数优化记录暂不可校验，数量未知。')}</Notice> : <div className="card loading-card">{text(data.optimizations?.reason, '暂无已归档的参数优化实验。')}</div>}
      {optimizations.length > 0 && <p className="helper">0–10 分只比较同池等权基线的历史收益与回撤；历史留出段不能当作已通过未来实盘验证。</p>}
    </div>

    </>}
    {mode === 'feedback' && <div className="console-section"><SectionHeader title="复盘库" subtitle="把你逐例回执与后来到期的结果配对，再比较策略版本。" badge={data.feedback_cases?.status === 'unavailable' || data.reviews?.status === 'unavailable' ? '部分记录暂不可核验' : `${count(cases.length)} 条最近回执 · ${count(reviews.length)} 条最近复盘`} />
      {cases.length ? <div className="table-wrap"><table><thead><tr><th>回执 ID</th><th>股票</th><th>决定</th><th>决定日期</th><th>复盘状态</th></tr></thead><tbody>
        {cases.map((item, index) => {
          const latestReview = reviews.find(review => review.review_id === item.latest_review_id);
          return <tr key={text(item.case_id, String(index))}><td className="trace-id">{text(item.case_id)}</td><td>{text(item.name, text(item.instrument_id))}</td><td>{item.decision === 'watch' ? '观察' : item.decision === 'skip' ? '跳过' : item.decision === 'buy' ? '买入决定' : item.decision === 'sell' ? '卖出决定' : text(item.decision)}</td><td>{text(item.observed_date)}</td><td>{reviewStatus(item.review_status)}{finite(item.latest_review_horizon_sessions) ? <small className="console-subline">最近 {count(item.latest_review_horizon_sessions)} 日复盘</small> : null}{latestReview?.reason && <small className="console-review-reason">{latestReview.reason}</small>}</td></tr>;
        })}
      </tbody></table></div> : data.feedback_cases?.status === 'unavailable' ? <Notice error>{text(data.feedback_cases.reason, '回执库暂不可校验，回执数量未知。')}</Notice> : <EmptyState title="还没有逐例回执"><p>在下方研究记录中填入观察、跳过或买卖决定，系统才能从真实 case 开始复盘。当前不把个股查询次数当作用户行动。</p></EmptyState>}
      {reviews.length > 0 && <><div className="section-label"><h3>已生成的复盘</h3><span>逐例到期后写入，和用户回执分开</span></div>
        <div className="table-wrap"><table><thead><tr><th>复盘 ID</th><th>回执 ID</th><th>生成时间</th><th>窗口</th><th>结果状态</th></tr></thead><tbody>
          {reviews.map((item, index) => <tr key={item.review_id || index}><td className="trace-id">{text(item.review_id)}</td><td className="trace-id">{text(item.case_id)}</td><td>{when(item.created_at)}</td><td>{finite(item.horizon_sessions) ? `${count(item.horizon_sessions)} 个交易日` : '—'}</td><td>{reviewStatus(item.status)}{item.reason && <small className="console-review-reason">{item.reason}</small>}</td></tr>)}
        </tbody></table></div></>}
      {data.reviews?.status === 'unavailable' && <Notice error>{text(data.reviews.reason, '复盘记录暂不可校验，结果数量未知。')}</Notice>}
    </div>}
  </>;
}

function ActionPanel({ data, submitting, lastTaskId, error, onRun, mode = 'all' }: {
  data: ConsoleStatus;
  submitting: 'optimize' | 'review_cases' | null;
  lastTaskId: string | null;
  error: string | null;
  onRun: (kind: 'optimize' | 'review_cases') => void;
  mode?: 'all' | 'optimize' | 'review_cases';
}) {
  const active = data.tasks?.active;
  const recent = Array.isArray(data.tasks?.recent) ? data.tasks.recent : [];
  const task = lastTaskId ? recent.find(item => item.task_id === lastTaskId) : null;
  const accepted = lastTaskId && !task && (!active || active.task_id === lastTaskId);
  const unavailable = data.tasks?.status === 'unavailable';
  const running = Boolean(active) || Boolean(submitting) || unavailable;
  const runs = Array.isArray(task?.result?.runs) ? task.result.runs : [];
  const currentTaskKind = task?.kind || active?.kind;
  const taskLabel = currentTaskKind === 'optimize' ? '离线参数优化' : currentTaskKind === 'review_cases' ? '回执复盘' : '任务';
  const taskStatus = taskState(task?.status || active?.status);

  return <div className="card console-actions">
    <div className="console-actions-head"><div><span className="kicker">本地任务</span><h3>运行控制</h3><p className="sub">明确触发一次离线研究或回执复盘，结果会进入实验记录池或复盘库。</p></div><span className="pill">仅本地</span></div>
    {mode !== 'review_cases' && <div className="console-action-row">
      <div><strong>运行离线参数优化</strong><p>只使用已入库日线；每次保存参数网格、训练段与历史留出结果，不自动晋级策略。</p></div>
      <button type="button" className="primary" disabled={running} onClick={() => onRun('optimize')}>{submitting === 'optimize' ? '正在提交…' : active?.kind === 'optimize' ? '优化运行中…' : '运行离线参数优化'}</button>
    </div>}
    {mode !== 'optimize' && <div className="console-action-row">
      <div><strong>复盘到期回执</strong><p>核对已保存的逐例回执及可用行情；未到期的 case 保持待复盘。</p></div>
      <button type="button" className="primary" disabled={running} onClick={() => onRun('review_cases')}>{submitting === 'review_cases' ? '正在提交…' : active?.kind === 'review_cases' ? '复盘运行中…' : '复盘到期回执'}</button>
    </div>}
    <p className="helper">本页动作只会运行本地离线任务，不会同步行情、下单或自动改写策略。运行结束后控制台自动刷新。</p>
    {unavailable && <Notice error>{text(data.tasks?.reason, '任务状态归档暂不可校验，请先检查本地服务。')}</Notice>}
    {error && <Notice error>{error}</Notice>}
    {(active || task || accepted) && <div className={`console-task-result${task?.status === 'failed' || task?.status === 'interrupted' ? ' failed' : ''}`} role="status">
      <strong>{taskLabel} · {taskStatus}</strong><span className="trace-id">Task ID：{text(task?.task_id, text(active?.task_id, lastTaskId || '—'))}</span>
      {task?.kind === 'optimize' && task.result && <p>新保存 {count(runs.filter(item => item.status === 'saved').length)} 条 · 已有 {count(runs.filter(item => item.status === 'existing').length)} 条 · 失败 {count(runs.filter(item => item.status === 'failed').length)} 条。{runs.filter(item => item.optimization_id).map(item => `${text(item.strategy_id)}: ${text(item.optimization_id)}`).join('；')}</p>}
      {task?.kind === 'review_cases' && task.result && <p>新增复盘 {count(task.result.saved)} 条 · 待到期 {count(task.result.pending)} 条 · 数据不可用 {count(task.result.unavailable)} 条；行情截至 {text(task.result.market_asof)}。</p>}
      {task?.result?.status === 'no_market_calendar' && <p>主库缺少可用的市场交易日历，本次没有计算到期结果。</p>}
      {task?.result?.reason && <p>{task.result.reason}</p>}
      {task?.reason && <p>{task.reason}</p>}
    </div>}
    <details className="console-workbench-more"><summary>最近任务记录 · {count(recent.filter(item => mode === 'all' || item.kind === mode).length)} 条</summary>
    {recent.filter(item => mode === 'all' || item.kind === mode).length ? <div className="table-wrap space"><table><thead><tr><th>任务 / ID</th><th>状态</th><th>提交时间</th><th>结束时间</th><th>结果</th></tr></thead><tbody>
      {recent.filter(item => mode === 'all' || item.kind === mode).slice(0, 10).map((item, index) => {
        const itemRuns = Array.isArray(item.result?.runs) ? item.result.runs : [];
        const result = item.kind === 'optimize'
          ? `新保存 ${itemRuns.filter(run => run.status === 'saved').length} · 已有 ${itemRuns.filter(run => run.status === 'existing').length} · 失败 ${itemRuns.filter(run => run.status === 'failed').length}`
          : item.kind === 'review_cases'
            ? `新增复盘 ${count(item.result?.saved)} · 待到期 ${count(item.result?.pending)} · 不可用 ${count(item.result?.unavailable)}`
            : '结果类型未知';
        const failure = item.result?.reason || item.reason || itemRuns.find(run => run.status === 'failed')?.reason;
        return <tr key={item.task_id || index}>
          <td className="console-run-id"><strong>{taskKind(item.kind)}</strong><small className="trace-id">{text(item.task_id)}</small></td>
          <td>{taskState(item.status)}</td><td>{when(item.created_at)}</td><td>{when(item.finished_at)}</td>
          <td>{item.status === 'interrupted' ? '任务中断，成果需核对' : result}{failure && <small className="console-error">{failure}</small>}</td>
        </tr>;
      })}
    </tbody></table></div> : <p className="helper">当前没有可核验的已结束任务记录。</p>}
    </details>
  </div>;
}

function ArchitectureOverview() {
  const layers = [
    { number: '01', title: '数据采集', detail: '行情源与低频历史回补', stage: '已接入 · 跨源对账待完善' },
    { number: '02', title: '数据存储', detail: '主库、归档与规范化契约', stage: '已接入 · 点时快照待建' },
    { number: '03', title: '实验系统', detail: '离线策略 → 回测 → 优化 → 实验记录', stage: '离线可运行 · 在线待扩展' },
    { number: '04', title: '应用层', detail: '研究系统 ｜ 量化系统', stage: '报告、控制台与回执已接' },
  ];
  return <div className="card console-architecture" aria-label="四层系统架构">
    <div className="console-architecture-head"><span className="kicker">四层系统</span><span>每层通过明确的数据与版本契约连接</span></div>
    <div className="console-architecture-grid">{layers.map(layer => <div className="console-architecture-layer" key={layer.number}>
      <span className="console-layer-number">{layer.number}</span><strong>{layer.title}</strong><p>{layer.detail}</p><small>{layer.stage}</small>
    </div>)}</div>
  </div>;
}

type TaskActionProps = {
  submitting: 'optimize' | 'review_cases' | null;
  lastTaskId: string | null;
  actionError: string | null;
  onRun: (kind: 'optimize' | 'review_cases') => void;
};

function algorithmImplementationStatus(value: unknown): string {
  if (value === 'adapter_available') return '适配器可用';
  if (value === 'candidate') return '候选 · 待适配';
  if (value === 'implemented') return '已实现';
  if (value === 'baseline_available') return '基线可用';
  return text(value, '状态未登记');
}

function algorithmResearchStatus(value: unknown): string {
  if (value === 'archived_experiment') return '有归档实验';
  if (value === 'not_evaluated') return '未运行实验';
  if (value === 'unavailable') return '实验不可用';
  return text(value, '实验状态未知');
}

function algorithmRunStatus(value: unknown): string {
  if (value === 'ready') return '已完成';
  if (value === 'partial_failure') return '部分失败';
  return text(value, '状态未知');
}

function algorithmArchiveStatus(item: AlgorithmComponent): string {
  const status = algorithmResearchStatus(item.research_status);
  return item.evidence?.some(evidence => evidence.provider_status === 'partial_failure') ? `${status} · 部分失败` : status;
}

function algorithmRole(item: AlgorithmComponent): string {
  if (item.algorithm_id === 'random-walk' || item.algorithm_id === 'momentum-20-extrapolation') return '预测对照基线';
  if (item.role === 'forecast_provider') return '候选预测信号';
  return text(item.role, '能力角色未知');
}

function AlgorithmRegistry({ data, onNavigate }: { data: ConsoleStatus; onNavigate: (module: QuantModule) => void }) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const algorithms = Array.isArray(data.algorithm_components) ? data.algorithm_components : [];
  const keyOf = (item: AlgorithmComponent) => `${item.algorithm_id}:${item.version}`;
  const selected = algorithms.find(item => keyOf(item) === selectedKey) ?? algorithms[0];
  const attached = algorithms.filter(item => (item.strategy_ids?.length ?? 0) > 0).length;
  const archived = algorithms.filter(item => item.research_status === 'archived_experiment').length;
  const evidence = Array.isArray(selected?.evidence) ? selected.evidence : [];
  const experimentIds = evidence.filter(item => item.kind === 'forecast_experiment').map(item => item.record_id).filter(Boolean);
  const stabilityIds = evidence.filter(item => item.kind === 'stability_audit').map(item => item.record_id).filter(Boolean);
  const strategyIds = Array.isArray(selected?.strategy_ids) ? selected.strategy_ids : [];
  const isBaseline = selected?.algorithm_id === 'random-walk' || selected?.algorithm_id === 'momentum-20-extrapolation';

  return <div className="console-section algorithm-registry">
    <SectionHeader title="算法组件登记" subtitle="记录预测模型和基线怎样进入研究流水线，以及距离可回测交易策略还差哪一步。" badge={`${count(algorithms.length)} 项组件`} />
    <div className="grid three console-workbench-stats">
      <div className="card mini-card"><div className="label">登记组件</div><div className="value">{count(algorithms.length)}</div><div className="minor">含预测模型及对照基线</div></div>
      <div className="card mini-card"><div className="label">有归档实验</div><div className="value">{count(archived)}</div><div className="minor">包含部分失败记录；不代表预测有效</div></div>
      <div className="card mini-card"><div className="label">已接入交易策略</div><div className="value">{count(attached)}</div><div className="minor">需有信号、仓位和风控映射</div></div>
    </div>
    {algorithms.length ? <div className="console-record-layout">
      <div className="console-record-list" aria-label="已登记算法组件">
        {algorithms.map(item => <button key={keyOf(item)} type="button" className={`console-record-button${selected === item ? ' selected' : ''}`} onClick={() => setSelectedKey(keyOf(item))} aria-current={selected === item ? 'true' : undefined}>
          <strong>{text(item.display_name, text(item.algorithm_id))}</strong>
          <small>v{text(item.version)} · {text(item.provider)} · {algorithmRole(item)}</small>
          <span>{algorithmImplementationStatus(item.implementation_status)} · {algorithmArchiveStatus(item)}</span>
        </button>)}
      </div>
      <article className="card console-record-detail-card" aria-label="算法组件详情">
        {selected && <>
          <span className="kicker">{algorithmRole(selected)} · {text(selected.algorithm_id)}</span>
          <div className="console-strategy-head"><div><h3>{text(selected.display_name, text(selected.algorithm_id))}</h3><p className="sub">{text(selected.provider)} · 版本 {text(selected.version)}</p></div><span className={`console-badge${selected.research_status !== 'archived_experiment' || evidence.some(item => item.provider_status === 'partial_failure') ? ' warning' : ''}`}>{algorithmArchiveStatus(selected)}</span></div>
          <dl className="console-record-dl">
            <div><dt>接入状态</dt><dd>{algorithmImplementationStatus(selected.implementation_status)}</dd></div>
            <div><dt>模型标识</dt><dd className="trace-id">{text(selected.model_id, '未登记')}</dd></div>
            <div><dt>模型修订</dt><dd className="trace-id">{text(selected.model_revision, '未登记')}</dd></div>
            <div><dt>适配器</dt><dd className="trace-id">{text(selected.adapter, '尚未实现')}</dd></div>
            <div><dt>策略角色</dt><dd>{isBaseline ? '预测对照基线' : selected.strategy_roles?.includes('signal_candidate') ? '候选信号' : '未指定'}</dd></div>
            <div><dt>关联策略</dt><dd>{strategyIds.length ? `${strategyIds.length} 项：${strategyIds.join('、')}` : '0 项 · 尚未接入可回测交易策略'}</dd></div>
            <div><dt>实验记录 ID</dt><dd className="trace-id">{experimentIds.length ? experimentIds.join('、') : '暂无可关联记录'}</dd></div>
            <div><dt>稳定性记录 ID</dt><dd className="trace-id">{stabilityIds.length ? stabilityIds.join('、') : '暂无可关联记录'}</dd></div>
            <div><dt>下一道门槛</dt><dd>{text(selected.next_gate, strategyIds.length ? '完成独立样本外及风控验证。' : '定义候选信号到仓位、风控和执行的映射，再运行扣成本回测。')}</dd></div>
          </dl>
          {evidence.length > 0 && <div className="algorithm-evidence"><strong>登记证据</strong><p className="helper">此处显示对应归档记录 ID；预测评估总览可能只展示最新实验。</p><ul>{evidence.map((item, index) => <li key={`${item.kind}-${item.record_id}-${index}`}>
            <span>{item.kind === 'forecast_experiment' ? '预测实验' : item.kind === 'stability_audit' ? '稳定性审计' : '证据'}{item.dataset ? ` · ${item.dataset}` : ''}{item.provider_status ? ` · ${algorithmRunStatus(item.provider_status)}` : ''}{item.created_at ? ` · ${when(item.created_at)}` : ''}</span>
            <code>{text(item.record_id, '记录 ID 未提供')}</code>
            {item.horizons && <small>有效样本 {Object.entries(item.horizons).sort(([a], [b]) => Number(a) - Number(b)).map(([horizon, result]) => `${horizon} 步 ${count(result.sample_count)}`).join(' · ')}</small>}
            {item.source_record_id && <small>源实验 {item.source_record_id}</small>}
          </li>)}</ul></div>}
          {!strategyIds.length && <Notice>{isBaseline ? '该组件用于衡量预测误差，是策略研究的对照基线。' : '当前只是策略体系中的候选信号组件。'}尚缺信号 → 仓位 → 风控映射和扣成本回测，不能按已验证买卖策略使用。</Notice>}
          <div className="algorithm-actions"><button type="button" className="refresh" onClick={() => onNavigate('forecast')}>前往预测评估总览 →</button>
            {selected.source_url?.startsWith('https://') && <a href={selected.source_url} target="_blank" rel="noopener noreferrer">查看模型来源 ↗</a>}
          </div>
        </>}
      </article>
    </div> : <EmptyState title="尚无算法组件登记"><p>状态服务还没有返回可核验的算法组件记录。</p></EmptyState>}
  </div>;
}

function StrategyManager({ data, onNavigate }: { data: ConsoleStatus; onNavigate: (module: QuantModule) => void }) {
  const [view, setView] = useState<'strategies' | 'algorithms' | 'factors' | 'rules'>('strategies');
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const strategies = Array.isArray(data.strategies) ? data.strategies : [];
  const factors = Array.isArray(data.factors) ? data.factors : [];
  const rules = Array.isArray(data.rules) ? data.rules : [];
  const algorithms = Array.isArray(data.algorithm_components) ? data.algorithm_components : [];
  const keyOf = (item: ConsoleStrategy) => `${item.strategy_id}:${item.strategy_version}`;
  const selected = strategies.find(item => keyOf(item) === selectedKey) ?? strategies[0];
  return <>
    <div className="console-subnav" role="group" aria-label="策略与因子视图">
      <button type="button" className={view === 'strategies' ? 'selected' : ''} onClick={() => setView('strategies')}>策略库 <span>{count(strategies.length)}</span></button>
      <button type="button" className={view === 'algorithms' ? 'selected' : ''} onClick={() => setView('algorithms')}>算法组件 <span>{count(algorithms.length)}</span></button>
      <button type="button" className={view === 'factors' ? 'selected' : ''} onClick={() => setView('factors')}>因子库 <span>{count(factors.length)}</span></button>
      <button type="button" className={view === 'rules' ? 'selected' : ''} onClick={() => setView('rules')}>规则库 <span>{count(rules.length)}</span></button>
    </div>
    {view === 'strategies' && <div className="console-record-layout">
      <div className="console-record-list" aria-label="已登记策略">
        {strategies.map(item => <button key={keyOf(item)} type="button" className={`console-record-button${selected === item ? ' selected' : ''}`} onClick={() => setSelectedKey(keyOf(item))} aria-current={selected === item ? 'true' : undefined}>
          <strong>{text(item.strategy_id)}</strong><small>v{text(item.strategy_version)} · {item.status === 'retrospective_pilot' ? '回顾性试验' : text(item.status)}</small>
          <span>{finite(item.north_star?.score) ? `${oneDecimal(item.north_star.score)} / 10` : '回测未验证'}</span>
        </button>)}
        {!strategies.length && <p className="helper">尚无已登记策略。</p>}
      </div>
      <div className="console-record-detail">{selected ? <StrategyCard strategy={selected} /> : <EmptyState title="无可核验策略"><p>策略库尚无可读取的记录。</p></EmptyState>}</div>
    </div>}
    {view === 'algorithms' && <AlgorithmRegistry data={data} onNavigate={onNavigate} />}
    {view === 'factors' && <div className="console-section"><SectionHeader title="因子库" subtitle="已登记计算定义及输入要求。登记不代表预测有效。" />
      {factors.length ? <div className="table-wrap"><table><thead><tr><th>因子</th><th>版本</th><th>定义</th><th>输入</th><th>最低历史长度</th><th>输出单位</th></tr></thead><tbody>
        {factors.map((factor, index) => <tr key={`${factor.factor_id}-${index}`}><td>{text(factor.factor_id)}</td><td>{text(factor.version)}</td><td className="console-description">{text(factor.description)}</td><td>{text(factor.input_field)}</td><td>{text(factor.minimum_history)}</td><td>{text(factor.output_unit)}</td></tr>)}
      </tbody></table></div> : <EmptyState title="因子元数据不可读取"><p>当前没有可展示的因子定义。</p></EmptyState>}
    </div>}
    {view === 'rules' && <div className="console-section"><SectionHeader title="规则与验证进度" subtitle="查询建议和筛选规则单独记录；未回测的规则不混入策略收益。" />
      {rules.length ? <div className="grid two">{rules.map((rule, index) => <div className="card console-rule" key={`${rule.rule_id}-${index}`}><div className="console-rule-head"><strong>{text(rule.rule_id)}</strong><span className="pill">v{text(rule.version)}</span></div><p className="sub">{rule.backtest_status === 'not_evaluated' ? '尚未单独回测' : text(rule.backtest_status)}</p><p>{text(rule.reason)}</p></div>)}</div>
        : <EmptyState title="尚无规则记录"><p>没有可读取的规则版本。</p></EmptyState>}
    </div>}
  </>;
}

function ExperimentManager({ data, actions }: { data: ConsoleStatus; actions: TaskActionProps }) {
  const [view, setView] = useState<'experiment' | 'optimization'>('experiment');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const experiments = Array.isArray(data.experiments?.recent) ? data.experiments.recent : [];
  const optimizations = Array.isArray(data.optimizations?.recent) ? data.optimizations.recent : [];
  const selectedExperiment = experiments.find(item => item.experiment_id === selectedId) ?? experiments[0];
  const selectedOptimization = optimizations.find(item => item.optimization_id === selectedId) ?? optimizations[0];
  return <>
    <ActionPanel data={data} {...actions} error={actions.actionError} mode="optimize" />
    <div className="console-subnav" role="group" aria-label="实验记录类型">
      <button type="button" className={view === 'experiment' ? 'selected' : ''} onClick={() => { setView('experiment'); setSelectedId(null); }}>策略实验 <span>{count(experiments.length)}</span></button>
      <button type="button" className={view === 'optimization' ? 'selected' : ''} onClick={() => { setView('optimization'); setSelectedId(null); }}>参数优化 <span>{count(optimizations.length)}</span></button>
    </div>
    <div className="console-record-layout">
      <div className="console-record-list" aria-label={view === 'experiment' ? '策略实验记录' : '参数优化记录'}>
        {view === 'experiment' ? experiments.map(item => <button key={item.experiment_id || item.snapshot_record_id} type="button" className={`console-record-button${selectedExperiment === item ? ' selected' : ''}`} onClick={() => setSelectedId(item.experiment_id || null)}>
          <strong>{text(item.strategy_id)} · v{text(item.strategy_version)}</strong><small>{text(item.asof)} · {text(item.state)}</small><span>{finite(item.north_star?.score) ? `${oneDecimal(item.north_star.score)} / 10` : '评分未知'}</span>
        </button>) : optimizations.map(item => <button key={item.optimization_id} type="button" className={`console-record-button${selectedOptimization === item ? ' selected' : ''}`} onClick={() => setSelectedId(item.optimization_id || null)}>
          <strong>{text(item.strategy_id)} · v{text(item.strategy_version)}</strong><small>{when(item.created_at)} · {count(item.candidate_count)} 组候选</small><span>{item.promotion_status === 'not_eligible' ? '尚不可晋级' : text(item.promotion_status)}</span>
        </button>)}
        {view === 'experiment' && !experiments.length && <p className="helper">{text(data.experiments?.reason, '尚无可核验实验记录。')}</p>}
        {view === 'optimization' && !optimizations.length && <p className="helper">{text(data.optimizations?.reason, '尚无参数优化记录。')}</p>}
      </div>
      <div className="card console-record-detail-card">
        {view === 'experiment' && selectedExperiment ? <>
          <span className="kicker">策略实验</span><h3>{text(selectedExperiment.strategy_id)} · v{text(selectedExperiment.strategy_version)}</h3>
          <p className="sub">{text(selectedExperiment.asof)} · {count(selectedExperiment.universe_count)} 只试验股 · {text(selectedExperiment.state)}</p>
          <div className="console-detail-grid"><div><span>累计收益</span><strong>{percent(selectedExperiment.metrics?.total_return)}</strong></div><div><span>最大回撤</span><strong>{percent(selectedExperiment.metrics?.max_drawdown)}</strong></div><div><span>回看评分</span><strong>{oneDecimal(selectedExperiment.north_star?.score)} / 10</strong></div><div><span>前瞻验证</span><strong>{text(selectedExperiment.validation?.prospective_status, '未知')}</strong></div></div>
          <dl className="console-record-dl"><div><dt>实验 ID</dt><dd className="trace-id">{text(selectedExperiment.experiment_id)}</dd></div><div><dt>来源运行 ID</dt><dd className="trace-id">{text(selectedExperiment.source_run_id)}</dd></div><div><dt>输入指纹</dt><dd className="trace-id">{text(selectedExperiment.input_fingerprint_sha256)}</dd></div><div><dt>参数</dt><dd>{selectedExperiment.parameters ? Object.entries(selectedExperiment.parameters).map(([key, value]) => `${key}=${String(value)}`).join(' · ') : '未提供'}</dd></div><div><dt>成本与执行</dt><dd>{percent(selectedExperiment.cost_rate, 2)} · {text(selectedExperiment.execution_assumption)}</dd></div></dl>
        </> : view === 'optimization' && selectedOptimization ? <>
          <span className="kicker">参数优化</span><h3>{text(selectedOptimization.strategy_id)} · v{text(selectedOptimization.strategy_version)}</h3>
          <p className="sub">{count(selectedOptimization.candidate_count)} 组候选 · {text(selectedOptimization.historical_holdout?.status)}</p>
          <div className="console-detail-grid"><div><span>训练评分</span><strong>{oneDecimal(selectedOptimization.train?.selected_north_star?.score)} / 10</strong></div><div><span>留出评分</span><strong>{oneDecimal(selectedOptimization.historical_holdout?.north_star?.score)} / 10</strong></div><div><span>留出年化</span><strong>{percent(selectedOptimization.historical_holdout?.metrics?.cagr)}</strong></div><div><span>留出回撤</span><strong>{percent(selectedOptimization.historical_holdout?.metrics?.max_drawdown)}</strong></div></div>
          <dl className="console-record-dl"><div><dt>优化 ID</dt><dd className="trace-id">{text(selectedOptimization.optimization_id)}</dd></div><div><dt>训练区间</dt><dd>{text(selectedOptimization.train?.start)} 至 {text(selectedOptimization.train?.end)}</dd></div><div><dt>留出区间</dt><dd>{text(selectedOptimization.historical_holdout?.start)} 至 {text(selectedOptimization.historical_holdout?.end)}</dd></div><div><dt>选中参数</dt><dd>{selectedOptimization.selected_parameters ? Object.entries(selectedOptimization.selected_parameters).map(([key, value]) => `${key}=${String(value)}`).join(' · ') : '未提供'}</dd></div><div><dt>输入指纹</dt><dd className="trace-id">{text(selectedOptimization.input_fingerprint_sha256)}</dd></div></dl>
          <Notice>{text(selectedOptimization.promotion_reason, '历史留出仅作回顾性检验，未取得前瞻晋级证据。')}</Notice>
        </> : <EmptyState title="尚无选中的记录"><p>运行离线优化后，结果会进入可核验记录池。</p></EmptyState>}
      </div>
    </div>
    <details className="console-workbench-more"><summary>查看完整实验与优化记录表</summary><ExperimentPool data={data} mode="experiments" /></details>
  </>;
}

function ForecastManager({ data }: { data: ConsoleStatus }) {
  const [view, setView] = useState<'models' | 'stability' | 'group'>('models');
  const datasets = Array.isArray(data.forecast_experiments?.datasets) ? data.forecast_experiments.datasets : [];
  const audits = Array.isArray(data.forecast_stability?.latest) ? data.forecast_stability.latest : [];
  const providers = datasets.flatMap(item => item.latest?.providers ?? []).filter(item => item.name !== 'random-walk');
  const ready = providers.filter(item => item.status === 'ready').length;
  const auditedHorizons = audits.flatMap(item => Object.values(item.horizons ?? {}));
  const promotedHorizons = auditedHorizons.filter(item => item.tier === 'broadly_stable' || item.tier === 'conditional_scope_only').length;
  const prospectiveCount = audits.filter(item => item.prospective_out_of_sample).length;
  return <>
    <div className="grid three console-workbench-stats">
      <div className="card mini-card"><div className="label">模型运行结果</div><div className="value">{count(ready)} <small>/ {count(providers.length)} 已完成</small></div><div className="minor">仅归档实验，不代表线上预测服务</div></div>
      <div className="card mini-card"><div className="label">预测稳定性门禁</div><div className="value">{count(promotedHorizons)} <small>/ {count(auditedHorizons.length)} 窗口获证据等级</small></div><div className="minor">5 / 20 步分别判定；其余弃答</div></div>
      <div className="card mini-card"><div className="label">前瞻验证</div><div className="value">{count(prospectiveCount)} <small>/ {count(audits.length)} 个审计通过</small></div><div className="minor">预测误差不等于扣成本交易收益</div></div>
    </div>
    <Notice>当前页面只读取已校验的模型实验与门禁结果；没有可用的页面启动预测任务或自动交易动作。</Notice>
    <div className="console-subnav" role="group" aria-label="预测评估视图">
      <button type="button" className={view === 'models' ? 'selected' : ''} onClick={() => setView('models')}>模型运行</button>
      <button type="button" className={view === 'stability' ? 'selected' : ''} onClick={() => setView('stability')}>稳定性门禁</button>
      <button type="button" className={view === 'group' ? 'selected' : ''} onClick={() => setView('group')}>群体量价</button>
    </div>
    {view === 'models' && <ForecastExperimentPool data={data} />}
    {view === 'stability' && <ForecastStabilityPool data={data} />}
    {view === 'group' && <GroupBehaviorPilot data={data} />}
  </>;
}

function FeedbackManager({ data, onSaved, actions }: { data: ConsoleStatus; onSaved: () => void; actions: TaskActionProps }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const journal = Array.isArray(data.research_journal?.recent) ? data.research_journal.recent : [];
  const cases = Array.isArray(data.feedback_cases?.recent) ? data.feedback_cases.recent : [];
  const reviews = Array.isArray(data.reviews?.recent) ? data.reviews.recent : [];
  const selected = cases.find(item => item.case_id === selectedId) ?? cases[0];
  const matchedReviews = reviews.filter(item => item.case_id === selected?.case_id);
  return <>
    <div className="grid three console-workbench-stats">
      <div className="card mini-card"><div className="label">可关联研究记录</div><div className="value">{count(journal.length)}</div><div className="minor">最近可读取报告</div></div>
      <div className="card mini-card"><div className="label">人工回执</div><div className="value">{count(cases.length)}</div><div className="minor">最近已保存 case</div></div>
      <div className="card mini-card"><div className="label">到期复盘</div><div className="value">{count(reviews.length)}</div><div className="minor">最近已保存结果</div></div>
    </div>
    {journal.length ? <FeedbackForm records={journal} onSaved={onSaved} today={text(data.china_today, todayShanghai())} />
      : <Notice>{data.research_journal?.status === 'unavailable' ? '研究记录库暂不可读取，无法关联新回执。' : '先在研究空间生成个股报告，再在这里保存逐例回执。'}</Notice>}
    <ActionPanel data={data} {...actions} error={actions.actionError} mode="review_cases" />
    <SectionHeader title="回执处理" subtitle="选择一条 case 查看其状态与已到期结果；记录和复盘分开保存。" badge={`${count(cases.length)} 条最近回执`} />
    <div className="console-record-layout">
      <div className="console-record-list" aria-label="已保存回执">
        {cases.map(item => <button key={item.case_id} type="button" className={`console-record-button${selected === item ? ' selected' : ''}`} onClick={() => setSelectedId(item.case_id || null)}>
          <strong>{text(item.name, text(item.instrument_id))}</strong><small>{text(item.observed_date)} · {text(item.decision)}</small><span>{reviewStatus(item.review_status)}</span>
        </button>)}
        {!cases.length && <p className="helper">尚无可核验回执。</p>}
      </div>
      <div className="card console-record-detail-card">{selected ? <>
        <span className="kicker">逐例回执</span><h3>{text(selected.name, text(selected.instrument_id))}</h3>
        <dl className="console-record-dl"><div><dt>Case ID</dt><dd className="trace-id">{text(selected.case_id)}</dd></div><div><dt>研究记录 ID</dt><dd className="trace-id">{text(selected.research_record_id)}</dd></div><div><dt>决定与日期</dt><dd>{text(selected.decision)} · {text(selected.observed_date)}</dd></div><div><dt>复盘状态</dt><dd>{reviewStatus(selected.review_status)}</dd></div></dl>
        <div className="section-label"><h3>已关联复盘</h3><span>{count(matchedReviews.length)} 条</span></div>
        {matchedReviews.length ? matchedReviews.map(item => <div className="console-review-item" key={item.review_id}><strong>{finite(item.horizon_sessions) ? `${count(item.horizon_sessions)} 个交易日` : '窗口未知'} · {reviewStatus(item.status)}</strong><span className="trace-id">{text(item.review_id)}</span>{item.reason && <small>{item.reason}</small>}</div>) : <p className="helper">尚无可关联的到期复盘结果。</p>}
      </> : <EmptyState title="等待第一条回执"><p>用户决定保存后才会出现 case；系统不会把查询视为交易。</p></EmptyState>}</div>
    </div>
    <details className="console-workbench-more"><summary>查看完整回执与复盘记录表</summary><ExperimentPool data={data} mode="feedback" /></details>
  </>;
}

const moduleCopy: Record<QuantModule, { title: string; description: string; eyebrow: string }> = {
  overview: { title: '量化系统总览', description: '分模块查看已保存证据、任务状态和下一步入口。', eyebrow: 'SYSTEM OVERVIEW' },
  data: { title: '数据管理', description: '查看来源、库存、历史缺口和最近同步运行。', eyebrow: 'DATA MANAGEMENT' },
  strategy: { title: '策略与因子', description: '管理交易策略、候选算法组件、因子与规则的版本和验证证据。', eyebrow: 'STRATEGY REGISTRY' },
  experiments: { title: '实验与回测', description: '运行离线优化、跟踪任务、核对实验及留出结果。', eyebrow: 'EXPERIMENT WORKBENCH' },
  forecast: { title: '预测评估', description: '核对模型运行误差与稳定性门禁，保留研究边界。', eyebrow: 'FORECAST EVALUATION' },
  feedback: { title: '复盘反馈', description: '保存你的逐例决定，运行到期复盘并检查结果。', eyebrow: 'FEEDBACK LOOP' },
};

export default function ConsolePage({ module, onNavigate }: { module: QuantModule; onNavigate: (module: QuantModule) => void }) {
  const [data, setData] = useState<ConsoleStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<'optimize' | 'review_cases' | null>(null);
  const [lastTaskId, setLastTaskId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);

  const reload = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    try {
      setData(await loadConsoleStatus(controller.signal));
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    return () => request.current?.abort();
  }, [reload]);

  useEffect(() => {
    if (!data?.tasks?.active?.task_id) return;
    const timer = window.setInterval(() => { void reload(); }, 3000);
    return () => window.clearInterval(timer);
  }, [data?.tasks?.active?.task_id, reload]);

  const runAction = async (kind: 'optimize' | 'review_cases') => {
    if (submitting || data?.tasks?.active || data?.tasks?.status === 'unavailable') return;
    setSubmitting(kind);
    setActionError(null);
    try {
      const accepted = kind === 'optimize' ? await startOptimization() : await startCaseReview();
      setLastTaskId(accepted.task_id);
      await reload();
    } catch (cause) {
      setActionError(errorMessage(cause));
    } finally {
      setSubmitting(null);
    }
  };

  const run = data?.published_run;
  const activeTask = data?.tasks?.active;
  const latestTask = Array.isArray(data?.tasks?.recent) ? data.tasks.recent[0] : undefined;
  const actions: TaskActionProps = {
    submitting, lastTaskId, actionError, onRun: kind => void runAction(kind),
  };

  return <div className="console-page" aria-label={moduleCopy[module].title}>
    <div className="section-head console-page-head">
      <div><span className="kicker">{moduleCopy[module].eyebrow}</span><h1>{moduleCopy[module].title}</h1><p className="sub">{moduleCopy[module].description}</p></div>
      <div className="console-page-actions"><span className="pill">状态读取 {when(data?.generated_at)}</span><button type="button" className="refresh" onClick={() => void reload()} disabled={loading}>刷新状态</button></div>
    </div>
    {error && <Notice error>量化状态读取失败：{error}</Notice>}
    {loading && !data && <div className="card loading-card" role="status">正在读取本地量化状态…</div>}
    {data && module === 'overview' && <>
      <div className="grid three console-workbench-stats">
        <div className="card mini-card"><div className="label">研究运行</div><div className="value">{text(run?.stock_signal_asof, '无')}</div><div className="minor trace-id">Run ID：{text(run?.run_id)}</div></div>
        <div className="card mini-card"><div className="label">候选发布</div><div className="value">{count(run?.recommendation_count)} <small>只</small></div><div className="minor">{run?.recommendation_status === 'blocked_insufficient_evidence' ? '证据不足，发布受阻' : text(run?.recommendation_status, '状态未知')}</div></div>
        <div className="card mini-card"><div className="label">本地任务</div><div className="value">{activeTask ? '运行中' : data.tasks?.status === 'unavailable' ? '不可核验' : '当前无运行任务'}</div><div className="minor trace-id">{activeTask ? `Task ID：${text(activeTask.task_id)}` : latestTask ? `最近：${taskKind(latestTask.kind)} · ${taskState(latestTask.status)}` : '暂无任务记录'}</div></div>
      </div>
      <div className="console-section"><SectionHeader title="进入工作台" subtitle="每个模块仅显示其当前管理视图；状态与动作来自本地服务。" />
        <div className="console-shortcuts">
          {(['data', 'strategy', 'experiments', 'forecast', 'feedback'] as const).map(next => <button type="button" className="card console-shortcut" key={next} onClick={() => onNavigate(next)}><strong>{moduleCopy[next].title}</strong><span>{moduleCopy[next].description}</span><b aria-hidden="true">→</b></button>)}
        </div>
      </div>
      <div className="console-section"><SectionHeader title="模块状态" subtitle="研究发布、数据库存和实验记录属于不同证据口径。" />
        <div className="table-wrap"><table><thead><tr><th>模块</th><th>当前可读状态</th><th>时间或记录</th></tr></thead><tbody>
          <tr><td>数据管理</td><td>{text(data.raw_data_inventory?.status, '未知')} · 主库 {count(data.market_store?.groups?.stock?.instrument_count)} 个代码</td><td>{when(data.raw_data_inventory?.snapshot_at)}</td></tr>
          <tr><td>策略与因子</td><td>{count(data.strategies?.length)} 项策略 · {count(data.algorithm_components?.length)} 个算法组件 · {count(data.factors?.length)} 个因子</td><td>{count(data.strategy_archive?.record_count)} 份策略快照</td></tr>
          <tr><td>实验与回测</td><td>{count(data.experiments?.recent?.length)} 条实验 · {count(data.optimizations?.recent?.length)} 条优化</td><td>{latestTask ? `最近任务 ${text(latestTask.task_id)}` : '暂无任务'}</td></tr>
          <tr><td>预测评估</td><td>{text(data.forecast_experiments?.status, '未知')} · {count(data.forecast_stability?.latest?.length)} 个稳定性审计</td><td>仅回顾性研究</td></tr>
          <tr><td>复盘反馈</td><td>{count(data.feedback_cases?.recent?.length)} 条回执 · {count(data.reviews?.recent?.length)} 条复盘</td><td>按逐例记录核验</td></tr>
        </tbody></table></div>
      </div>
      <details className="console-workbench-more"><summary>查看四层系统架构</summary><ArchitectureOverview /></details>
    </>}
    {data && module === 'data' && <>
      <QuantDataManager data={data} />
      <details className="console-workbench-more"><summary>查看归档盘点原始摘要与研究记录</summary><RawDataInventory data={data} /><Operations data={data} onSaved={() => void reload()} /></details>
    </>}
    {data && module === 'strategy' && <StrategyManager data={data} onNavigate={onNavigate} />}
    {data && module === 'experiments' && <ExperimentManager data={data} actions={actions} />}
    {data && module === 'forecast' && <ForecastManager data={data} />}
    {data && module === 'feedback' && <FeedbackManager data={data} onSaved={() => void reload()} actions={actions} />}
  </div>;
}
