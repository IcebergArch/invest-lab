import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { analyzeStock } from '../api';
import { errorMessage, finite, magnitudePercent, percent, text, yuan } from '../format';
import type { BacktestMetrics, StockAnalysis } from '../types';
import StockChart from './StockChart';
import { EmptyState, Fact, Notice, SectionHeader } from './ui';

function BacktestFacts({ title, metrics }: { title: string; metrics?: BacktestMetrics }) {
  if (!metrics) return null;
  return <>
    <p className="helper">{title}</p>
    <div className="fact-list">
      <Fact label="累计收益" value={percent(metrics.total_return)} />
      <Fact label="年化收益" value={percent(metrics.cagr)} />
      <Fact label="最大回撤" value={magnitudePercent(metrics.max_drawdown)} />
      {finite(metrics.sharpe_rf0) && <Fact label="夏普比率" value={metrics.sharpe_rf0.toFixed(2)} />}
    </div>
  </>;
}

function DecisionCard({ report, isQlib }: { report: StockAnalysis; isQlib: boolean }) {
  const decision = report.decision;
  const source = Array.isArray(report.sources) ? report.sources[0] : undefined;
  const originalPriceUnverified = isQlib && source?.factor_verified !== true;
  const buyLabel = text(decision?.buy?.label, '');
  const sellLabel = text(decision?.sell?.label, '');
  const hasJudgment = Boolean(buyLabel || sellLabel);
  const buySupported = !originalPriceUnverified || decision?.buy?.stance === 'wait'
    || decision?.buy?.stance === 'avoid' || decision?.buy?.stance === 'unavailable';
  const sellSupported = !originalPriceUnverified || decision?.sell?.stance === 'unavailable';
  const judgmentReady = decision?.status === 'ready' && !originalPriceUnverified
    && Boolean(buyLabel && sellLabel) && decision.buy?.stance !== 'unavailable'
    && decision.sell?.stance !== 'unavailable';
  const reasons = Array.isArray(decision?.reasons)
    ? decision.reasons.filter(item => typeof item === 'string' && item.trim()) : [];
  const risks = Array.isArray(decision?.risks)
    ? decision.risks.filter(item => typeof item === 'string' && item.trim()) : [];
  const costContext = text(decision?.cost_context, '');
  const stockLabel = text(report.name, report.instrument_id?.replace(/^stock:/, '') || '个股');

  return <section className={`card decision-card ${judgmentReady ? 'decision-ready' : 'decision-limited'}`} aria-label="个股操作参考">
    <div className="decision-heading">
      <div>
        <p className="kicker">个股操作参考</p>
        <h3>{stockLabel} · {hasJudgment ? '买入与卖出判断' : '暂不给买卖判断'}</h3>
      </div>
      <span className="decision-asof">数据截至 {text(decision?.asof, text(report.asof, '未知'))}</span>
    </div>
    {!judgmentReady && <div className="decision-caution" role="note">
      <strong>使用限制：</strong>{text(decision?.reason,
        risks.find(item => item.includes('报告基于前一日') || item.includes('报告日期已落后'))
        || '证据尚不足以形成可直接执行的买卖判断；以下结论仅是有条件的观察参考。')}
      {originalPriceUnverified && <span>该股原价换算尚未核验，不能将复权价与输入成本价直接比较。</span>}
    </div>}
    {hasJudgment ? <>
      {decision?.horizon && <p className="decision-horizon">观察周期：{decision.horizon}</p>}
      <div className="decision-sides">
        <div className="decision-side" data-stance={decision?.buy?.stance}>
          <span className="decision-side-title">尚未买入</span>
          <strong>{buySupported && buyLabel ? buyLabel : '无法判断'}</strong>
          <p>{buySupported && buyLabel ? text(decision?.buy?.reason, '请结合下方判断依据与风险检查。') : '该侧缺少可核验的判断依据。'}</p>
        </div>
        <div className="decision-side" data-stance={decision?.sell?.stance}>
          <span className="decision-side-title">已经持有</span>
          <strong>{sellSupported && sellLabel ? sellLabel : '无法判断'}</strong>
          <p>{sellSupported && sellLabel ? text(decision?.sell?.reason, '请结合下方判断依据与风险检查。') : '该侧缺少可核验的判断依据。'}</p>
        </div>
      </div>
      <div className="decision-details">
        {reasons.length > 0 && <div><h4>关键依据</h4><ul>{reasons.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
        {risks.length > 0 && <div><h4>主要风险</h4><ul>{risks.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
      </div>
      {finite(report.cost_price) && <p className="decision-cost"><strong>成本价影响：</strong>{originalPriceUnverified
        ? '原价换算尚未核验，不能计算本次成本价差异。'
        : costContext || (finite(report.cost_return)
        ? `最新收盘价相对输入成本价 ${percent(report.cost_return)}；这只是价格差异，不是账户盈亏。`
        : '输入成本价已记录，但本次没有可核验的成本价比较。')}</p>}
      {decision?.next_check && <p className="decision-next"><strong>下一步关注：</strong>{decision.next_check}</p>}
      {decision?.rule_version && <p className="helper">规则版本：{decision.rule_version}</p>}
    </> : <>
      {report.trend?.label && <p className="decision-observation"><strong>目前观察：</strong>{report.trend.label}。{Array.isArray(report.risk?.flags) && report.risk.flags.length > 0 ? report.risk.flags[0] : ''}</p>}
    </>}
  </section>;
}

function StockReport({ report }: { report: StockAnalysis }) {
  if (report.status !== 'ready' && report.status !== 'partial_data') {
    return <EmptyState title="暂时无法生成完整报告">
      <p>{text(report.reason, '这只股票的已入库数据不足。')}</p>
    </EmptyState>;
  }
  const trend = report.trend;
  const risk = report.risk;
  const forecast = report.forecast;
  const backtest = report.backtest;
  const source = Array.isArray(report.sources) ? report.sources[0] : undefined;
  const isQlib = source?.latest_source_id === 'investment_data_qlib_release';
  const originalPriceAvailable = isQlib && source?.factor_verified === true && finite(report.latest_close);
  const displayCode = report.instrument_id?.replace(/^stock:/, '') || '个股分析';

  return <div className="report" aria-live="polite">
    <DecisionCard report={report} isQlib={isQlib} />
    <div className="card report-head">
      <div className="report-title"><h3>{text(report.name, displayCode)}</h3><span className="stock-id">{displayCode}</span></div>
      <div className="sub">数据截至 {text(report.asof, '未知')} · {isQlib ? `Qlib 归档 Release ${text(source?.release_tag)} 个股观察` : '本地日线研究报告'}</div>
      <p className="report-summary">{text(report.summary, '报告已生成。')}</p>
      {isQlib && finite(source?.bar_count) && <p className="helper">该股归档有 {source.bar_count.toLocaleString('zh-CN')} 条有效收盘记录（{text(source.first_valid_close_date)} 至 {text(source.last_valid_close_date)}）；下方走势只使用最近的有效数据。</p>}
      <div className="fact-list">
        {isQlib
          ? <>
            <Fact label="Qlib 复权收盘价" value={finite(report.adjusted_close) ? `¥${report.adjusted_close.toFixed(4)}` : '—'} />
            {originalPriceAvailable && <Fact label="因子换算原价（已抽样核验）" value={yuan(report.latest_close)} />}
          </>
          : <Fact label="最新入库收盘价" value={yuan(report.latest_close)} />}
        <Fact label="输入成本价" value={finite(report.cost_price) ? yuan(report.cost_price) : '未提供'} />
        {(!isQlib || originalPriceAvailable) && <Fact label="相对成本价的价格差异" value={percent(report.cost_return)} />}
      </div>
      {isQlib && !originalPriceAvailable && <p className="helper">这是复权研究价格；该股原价换算尚未通过核验，不能直接与输入成本价比较。</p>}
      {finite(report.cost_price) && (!isQlib || originalPriceAvailable) && <p className="helper">此差异未核对买入日期、分红送转、持仓数量和交易费用，不是账户盈亏。</p>}
    </div>
    {report.status === 'partial_data' && <Notice><strong>数据提示：</strong>{text(report.reason, '部分字段不足，以下仅显示可用指标。')}</Notice>}
    <StockChart report={report} />
    <div className="grid two space">
      <section className="card report-block">
        <h3>走势观察</h3>
        <p className="sub">{text(trend?.label, '暂无完整趋势结论')}</p>
        {trend?.detail && <p className="helper">{trend.detail}</p>}
        <div className="fact-list">
          <Fact label="近 5 日" value={percent(trend?.metrics?.return_5d)} />
          <Fact label="近 20 日" value={percent(trend?.metrics?.return_20d)} />
          <Fact label="近 60 日" value={percent(trend?.metrics?.return_60d)} />
        </div>
      </section>
      <section className="card report-block">
        <h3>风险检查</h3>
        <p className="sub">{text(risk?.summary, '暂无完整风险结论。')}</p>
        <div className="fact-list">
          <Fact label="近 20 日年化波动" value={magnitudePercent(risk?.metrics?.annualized_volatility_20d)} />
          <Fact label="近 60 日最大回撤" value={magnitudePercent(risk?.metrics?.max_drawdown_60d)} />
          <Fact label="近 20 日平均成交额" value={finite(risk?.metrics?.avg_amount_20d_cny) ? `${(risk.metrics.avg_amount_20d_cny / 100_000_000).toFixed(2)} 亿元` : '不可用'} />
        </div>
        {Array.isArray(risk?.flags) && risk.flags.length > 0 && <ul className="simple-list">{risk.flags.map((flag, index) => <li key={index}>{flag}</li>)}</ul>}
      </section>
      <section className="card report-block">
        <h3>预测检验</h3>
        <p className="sub">{isQlib && !forecast ? '当前 Qlib 归档未运行预测检验。' : forecast?.status === 'baseline_only' ? '仅有不变价基线；尚无可靠上涨概率。' : text(forecast?.status, '暂无可用结论。')}</p>
        <div className="fact-list">
          <Fact label="5 日不变价基线" value={yuan(forecast?.baseline_close_5d)} />
          <Fact label="20 日不变价基线" value={yuan(forecast?.baseline_close_20d)} />
        </div>
        {['5', '20'].map(horizon => {
          const validation = forecast?.rolling_validation?.[horizon];
          return validation?.status === 'ready' && finite(validation.mean_absolute_return_error)
            ? <p className="helper" key={horizon}>{horizon} 日滚动检验：{validation.count ?? '—'} 次，平均绝对收益误差 {magnitudePercent(validation.mean_absolute_return_error)}。</p>
            : null;
        })}
        {forecast?.explanation && <p className="helper">{forecast.explanation}</p>}
        {forecast?.model_research_status && <p className="helper">{forecast.model_research_status}</p>}
      </section>
      <section className="card report-block">
        <h3>回测证据</h3>
        {backtest?.status === 'historical_diagnostic_only' ? <p className="sub">历史策略示例，未完成独立时间外检验。</p> : <p className="sub">{isQlib && !backtest ? '当前 Qlib 归档未运行策略回测。' : text(backtest?.reason, '暂无可用回测。')}</p>}
        {backtest?.start && backtest?.end && <p className="helper">区间：{backtest.start} 至 {backtest.end} · {backtest.observations ?? '—'} 个交易日</p>}
        <BacktestFacts title="20/60 日均线策略示例" metrics={backtest?.strategy_metrics} />
        <BacktestFacts title="买入持有对照" metrics={backtest?.buy_hold_metrics} />
        {backtest?.reason && <p className="helper">{backtest.reason}</p>}
      </section>
    </div>
    {report.research_record?.status === 'saved' && <p className="research-record trace-id">
      研究记录已保存到本地 · ID {text(report.research_record.record_id)}
    </p>}
    {report.research_record?.status === 'save_failed' && <Notice error>
      <strong>本地记录未保存：</strong>{text(report.research_record.reason, '本次报告可以查看，但复盘记录写入失败。')}
    </Notice>}
    <div className="grid two space">
      <section className="card report-block">
        <h3>数据来源</h3>
        {source ? <ul className="simple-list">
          {isQlib ? <>
            <li>来源：investment_data 发布的 Qlib 复权归档 · Release {text(source.release_tag)}</li>
            <li>此股归档有效收盘 {source.bar_count ?? '—'} 条；最早 {text(source.first_valid_close_date)}，最近 {text(source.last_valid_close_date)}。各年份覆盖与缺值区间不同。</li>
            <li>最近有效收盘 {text(source.last_trade_date)}；本次读取 {source.read_window_calendar_rows ?? '—'} 个日历行，最近连续有效收盘 {source.analyzed_contiguous_close_rows ?? '—'} 条。</li>
            <li>原价换算：{source.factor_verified ? '已完成本 Release 的跨源抽样核验' : '未通过报告核验'}；人民币成交额：{source.amount_verified ? '已完成本 Release 的跨源抽样核验' : '未通过报告核验'}。</li>
            {source.archive_sha256 && <li className="trace-id">归档 SHA-256：{source.archive_sha256}</li>}
          </> : <>
            <li>最近日线来源：{text(source.latest_source_id)}</li>
            <li>截至 {text(source.last_trade_date)}，共 {source.bar_count ?? '—'} 条日线</li>
            {source.latest_run_id && <li className="trace-id">运行 ID：{source.latest_run_id}</li>}
          </>}
        </ul> : <p className="sub">来源记录暂不可用。</p>}
      </section>
      <section className="card report-block">
        <h3>报告边界</h3>
        {Array.isArray(report.limitations) && report.limitations.length > 0
          ? <ul className="simple-list">{report.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>
          : <p className="sub">暂无补充说明。</p>}
      </section>
    </div>
  </div>;
}

export default function StockTab() {
  const [symbol, setSymbol] = useState('');
  const [cost, setCost] = useState('');
  const [report, setReport] = useState<StockAnalysis | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => request.current?.abort(), []);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const query = symbol.trim();
    const number = cost.trim() === '' ? null : Number(cost);
    if (!query) { setError('请输入股票代码或名称。'); return; }
    if (number !== null && (!Number.isFinite(number) || number <= 0)) {
      setError('成本价需要是大于 0 的数字。'); return;
    }
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true); setError(null); setReport(null);
    try {
      setReport(await analyzeStock(query, number, controller.signal));
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  return <>
    <SectionHeader title="查一只股票" subtitle="输入六位代码或交易所代码；已登记股票也可输入名称。成本价可选。"
      badge="使用本地研究数据" />
    <form className="card form-card" onSubmit={event => void submit(event)}>
      <div className="form-row">
        <div className="field"><label htmlFor="symbol">股票代码或名称</label>
          <input id="symbol" value={symbol} onChange={event => setSymbol(event.target.value)}
            placeholder="例如 000338 或 000338.SZ" maxLength={40} autoComplete="off" required /></div>
        <div className="field"><label htmlFor="cost-price">你的成本价（可选，元 / 股）</label>
          <input id="cost-price" type="number" value={cost} onChange={event => setCost(event.target.value)}
            min="0.001" step="any" inputMode="decimal" placeholder="例如 15.80" /></div>
        <button className="primary" type="submit" disabled={busy}>{busy ? '生成中…' : '生成分析报告'}</button>
      </div>
      <p className="helper">成本价用于价格比较，并会随报告尝试保存到本地研究记录，供日后复盘；不会发送至第三方平台，也不会自动下单。</p>
    </form>
    {error && <Notice error>{error}</Notice>}
    {busy && <div className="card loading-card" role="status">正在读取已入库行情并生成报告…</div>}
    {report && <StockReport report={report} />}
  </>;
}
