import { useState } from 'react';
import { finite, percent, text } from '../format';
import type { StockAnalysis, StockChart as StockChartData } from '../types';

const WIDTH = 900;
const HEIGHT = 365;
const LEFT = 62;
const RIGHT = 27;
const TOP = 22;
const PRICE_BOTTOM = 244;
const POSITION_TOP = 287;
const POSITION_BOTTOM = 324;

function validDate(value: unknown): value is string {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function validHistory(history: StockChartData['history']): history is NonNullable<StockChartData['history']> {
  return Array.isArray(history) && history.length >= 2 && history.length <= 2000
    && history.every((point, index) => validDate(point.date) && finite(point.close) && point.close > 0
      && (index === 0 || point.date > history[index - 1].date));
}

function validForecast(chart: StockChartData, lastDate: string) {
  const forecast = chart.forecast;
  const points = forecast?.points;
  const status = forecast?.status;
  const validation = forecast?.validation;
  if ((status !== 'research_only' && status !== 'validated') || !Array.isArray(points)
    || points.length === 0 || points.length > 60 || !text(forecast?.model_id, '')
    || !text(forecast?.model_version, '') || forecast?.asof !== lastDate
    || forecast?.price_basis !== chart.price_basis) return null;
  if (status === 'validated' && (validation?.status !== 'passed' || !text(validation.method, '')
    || !finite(validation.sample_count) || validation.sample_count <= 0
    || !text(validation.metric_label, '') || !finite(validation.metric_value))) return null;
  if (!points.every((point, index) => Number.isInteger(point.step) && point.step > 0
    && finite(point.median) && point.median > 0
    && (index === 0 || point.step > points[index - 1].step))) return null;
  return forecast;
}

function pathFromHistory(
  history: NonNullable<StockChartData['history']>,
  x: (index: number) => number,
  y: (value: number) => number,
): string {
  return history.map((point, index) => `${index === 0 || point.gap_before ? 'M' : 'L'}${x(index).toFixed(1)},${y(point.close).toFixed(1)}`).join(' ');
}

export default function StockChart({ report }: { report: StockAnalysis }) {
  const [range, setRange] = useState<'120' | '250' | 'all'>('120');
  const [visible, setVisible] = useState<Record<string, boolean>>({
    factor: true, policy: true, index: true, disclosure: true, signal: true, position: true,
  });
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const chart = report.chart;
  const history = chart?.history;
  if (chart?.status !== 'ready' || !validHistory(history) || !validDate(chart.asof)
    || (chart.price_basis !== 'qfq_cny' && chart.price_basis !== 'qlib_adjusted')
    || chart.asof !== history[history.length - 1].date || (report.asof && chart.asof !== report.asof)) {
    return <section className="card report-block stock-chart-card" aria-label="股价与综合事件图">
      <h3>股价与综合事件</h3>
      <p className="sub">暂无可核验的价格曲线。{text(report.asof, '') && `报告数据截至 ${report.asof}。`}</p>
    </section>;
  }

  const forecast = validForecast(chart, chart.asof);
  const issuedAt = report.research_record?.generated_at;
  const issuedParts = issuedAt && !Number.isNaN(Date.parse(issuedAt))
    ? new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Shanghai', year: 'numeric',
      month: '2-digit', day: '2-digit' }).formatToParts(new Date(issuedAt)) : [];
  const issuedField = (kind: string) => issuedParts.find(part => part.type === kind)?.value;
  const issuedDay = issuedField('year') && issuedField('month') && issuedField('day')
    ? `${issuedField('year')}-${issuedField('month')}-${issuedField('day')}` : null;
  const forecastEvidenceNote = report.research_record?.status !== 'saved'
    ? '本次预测未确认保存，不能计入前瞻核验。'
    : issuedDay && forecast?.asof && issuedDay > forecast.asof
      ? '研究记录晚于价格基准日生成，仅作回溯参考，不能计入前瞻评分。'
      : '本次预测已按研究记录留存，待后续真实价格核验。';
  const future = forecast?.points ?? [];
  const rangeCount = range === '120' ? 120 : range === '250' ? 250 : history.length;
  const shownHistory = history.length > rangeCount ? history.slice(-rangeCount) : history;
  const historyCount = shownHistory.length;
  const total = historyCount + future.length;
  const prices = [...shownHistory.map(point => point.close), ...future.flatMap(point => [point.median,
    ...(finite(point.p10) && point.p10 > 0 ? [point.p10] : []),
    ...(finite(point.p90) && point.p90 > 0 ? [point.p90] : [])])];
  const low = Math.min(...prices);
  const high = Math.max(...prices);
  const padding = Math.max((high - low) * 0.08, high * 0.01);
  const min = Math.max(0, low - padding);
  const max = high + padding;
  const plotWidth = WIDTH - LEFT - RIGHT;
  const x = (index: number) => LEFT + (index / Math.max(total - 1, 1)) * plotWidth;
  const y = (value: number) => PRICE_BOTTOM - ((value - min) / (max - min)) * (PRICE_BOTTOM - TOP);
  const historyPath = pathFromHistory(shownHistory, x, y);
  const forecastPath = forecast
    ? [`M${x(historyCount - 1).toFixed(1)},${y(shownHistory[historyCount - 1].close).toFixed(1)}`,
      ...future.map((point, index) => `L${x(historyCount + index).toFixed(1)},${y(point.median).toFixed(1)}`)].join(' ')
    : '';
  const hasBand = future.length > 0 && future.every(point => finite(point.p10) && finite(point.p90)
    && point.p10 > 0 && point.p10 <= point.median && point.median <= point.p90);
  const bandPath = hasBand
    ? `${future.map((point, index) => `${index === 0 ? 'M' : 'L'}${x(historyCount + index).toFixed(1)},${y(point.p90 as number).toFixed(1)}`).join(' ')} ${future.slice().reverse().map((point, index) => `L${x(total - index - 1).toFixed(1)},${y(point.p10 as number).toFixed(1)}`).join(' ')} Z`
    : '';
  const positionRows = chart.rule_position;
  const positionStart = historyCount - (positionRows?.length ?? 0);
  const position = visible.position && Array.isArray(positionRows) && positionRows.length >= 2 && positionStart >= 0
    && positionRows.every((point, index) => point.date === shownHistory[positionStart + index].date
      && (point.position === 0 || point.position === 1)) ? positionRows : null;
  const positionPath = position
    ? position.map((point, index) => {
      const nextY = point.position === 1 ? POSITION_TOP : POSITION_BOTTOM;
      const pointX = x(positionStart + index).toFixed(1);
      if (index === 0) return `M${pointX},${nextY}`;
      const previousY = position[index - 1].position === 1 ? POSITION_TOP : POSITION_BOTTOM;
      return `L${pointX},${previousY} L${pointX},${nextY}`;
    }).join(' ') : '';
  const historyByDate = new Map(shownHistory.map((point, index) => [point.date, { point, index }]));
  const signals = visible.signal && Array.isArray(chart.signals)
    ? chart.signals.filter(signal => (signal.kind === 'buy' || signal.kind === 'sell')
      && validDate(signal.date) && historyByDate.has(signal.date)
      && text(signal.rule_id, '') && text(signal.rule_version, '')) : [];
  const rule = chart.signal_rule?.rule_id && chart.signal_rule?.rule_version
    ? chart.signal_rule : signals[0] || (chart.rule_id && chart.rule_version
      ? { rule_id: chart.rule_id, rule_version: chart.rule_version } : null);
  const validation = forecast?.validation;
  const methodLabel = validation?.method === 'rolling_origin_same_history_not_independent_out_of_sample'
    ? '同一段历史的滚动起点评估（非独立样本外）' : validation?.method;
  const errorIsReturn = validation?.metric_label === '20-session mean absolute return error';
  const metricLabel = errorIsReturn ? '20 个观察点平均绝对收益误差' : validation?.metric_label;
  const metricValue = finite(validation?.metric_value)
    ? errorIsReturn ? `${(Math.abs(validation.metric_value) * 100).toFixed(1)}%`
      : validation.metric_value.toFixed(3) : '';
  const validationText = validation && finite(validation.sample_count) && validation.sample_count > 0
    && text(validation.method, '') && text(validation.metric_label, '') && finite(validation.metric_value)
    ? `${methodLabel} · ${validation.sample_count} 次滚动检验 · ${metricLabel} ${metricValue}${finite(validation.skill_vs_random_walk)
      ? ` · 相对不变价基线 ${validation.skill_vs_random_walk.toFixed(2)}（负值表示更差）` : ''}`
    : '滚动验证指标尚不完整';
  const priceLabel = chart.price_basis === 'qfq_cny' ? '前复权收盘价（元）' : 'Qlib 复权价（研究尺度）';
  const timeline = report.analysis_timeline;
  const eventGroups = new Map<string, NonNullable<StockAnalysis['analysis_timeline']>['events']>();
  for (const event of timeline?.events ?? []) {
    if (!event.anchor_date || !historyByDate.has(event.anchor_date) || !visible[event.category]) continue;
    const group = eventGroups.get(event.anchor_date) ?? [];
    group.push(event);
    eventGroups.set(event.anchor_date, group);
  }
  const groupedDays = [...eventGroups.keys()].sort();
  const materialDays = groupedDays.filter(day => eventGroups.get(day)?.some(event => event.category !== 'factor'));
  const activeDay = selectedDay && eventGroups.has(selectedDay) ? selectedDay
    : materialDays[materialDays.length - 1] ?? groupedDays[groupedDays.length - 1];
  const activeEvents = activeDay ? eventGroups.get(activeDay) ?? [] : [];
  const eventIndex = activeDay ? historyByDate.get(activeDay)?.index : undefined;
  const showEventContext = eventIndex !== undefined && activeEvents.some(event => event.category !== 'factor');
  const firstCloseChange = showEventContext && eventIndex > 0 && !shownHistory[eventIndex].gap_before
    ? shownHistory[eventIndex].close / shownHistory[eventIndex - 1].close - 1 : null;
  const fiveCloseChange = showEventContext && eventIndex + 5 < historyCount
    && shownHistory.slice(eventIndex + 1, eventIndex + 6).every(point => !point.gap_before)
    ? shownHistory[eventIndex + 5].close / shownHistory[eventIndex].close - 1 : null;
  const toggle = (category: string) => setVisible(previous => ({ ...previous, [category]: !previous[category] }));

  return <section className="card report-block stock-chart-card" aria-label="股价、事件和研究预测图">
    <div className="chart-heading">
      <div><h3>股价与综合事件</h3><p className="sub">历史收盘价与分类事件 · 数据截至 {chart.asof} · {priceLabel}</p></div>
      <div className="chart-legend" aria-label="图例">
        <span><i className="chart-key history-key" />历史价格</span>
        {groupedDays.length > 0 && <span><i className="chart-key event-key" />事件计数</span>}
        {signals.length > 0 && <span><i className="chart-key buy-key" />买 / 卖信号</span>}
        {position && <span><i className="chart-key position-key" />历史规则持仓</span>}
        {forecast && <span><i className="chart-key forecast-key" />{forecast.status === 'research_only' ? '实验预测' : '已验证预测'}</span>}
      </div>
    </div>
    <div className="timeline-controls" aria-label="图层和时间范围">
      <div className="timeline-range" aria-label="图表时间范围">
        {([['120', '近 120 日'], ['250', '近 250 日'], ['all', '全部已载入']] as const).map(([value, label]) =>
          <button key={value} type="button" className={range === value ? 'active' : ''}
            aria-pressed={range === value} onClick={() => setRange(value)}>{label}</button>)}
      </div>
      <div className="timeline-layer-switches">
        {timeline?.layers?.map(layer => <label key={layer.category} title={layer.coverage}>
          <input type="checkbox" checked={Boolean(visible[layer.category])}
            disabled={layer.status === 'source_unavailable' || layer.status === 'no_observations'}
            onChange={() => toggle(layer.category)} />
          {layer.label}<small>{layer.status === 'source_unavailable' ? '未接入' : layer.status === 'no_observations' ? '无观测' : layer.status === 'audited_single_adjustment' ? '仅一轮核实' : layer.status === 'audited_selected_adjustments' ? '仅个别核实' : layer.status === 'curated_date_only_subset' ? '仅少量日期' : ''}</small>
        </label>)}
        <label><input type="checkbox" checked={Boolean(visible.signal)} onChange={() => toggle('signal')} />策略信号</label>
        <label><input type="checkbox" checked={Boolean(visible.position)} onChange={() => toggle('position')} />规则持仓</label>
      </div>
    </div>
    <p className="helper timeline-window">图中价格与事件共用 {shownHistory[0].date} 至 {shownHistory[shownHistory.length - 1].date} 的交易日横轴；已载入 {history.length} 个价格观察点。公告时间、生效时间和标记所在交易日分别列在事件详情中。</p>
    <div className="chart-scroll">
      <svg className="price-chart" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img"
        aria-label={`从 ${shownHistory[0].date} 到 ${chart.asof} 的历史价格${signals.length ? '及买卖信号' : ''}${forecast ? `，另有未来${forecast.status === 'research_only' ? '实验' : '已验证'}预测线` : ''}`}>
        {[0, 1, 2, 3, 4].map(index => {
          const value = min + ((max - min) * (4 - index)) / 4;
          const rowY = TOP + ((PRICE_BOTTOM - TOP) * index) / 4;
          return <g key={index}><line className="chart-gridline" x1={LEFT} y1={rowY} x2={WIDTH - RIGHT} y2={rowY} />
            <text className="chart-axis-label" x={LEFT - 10} y={rowY + 4} textAnchor="end">{value.toFixed(2)}</text></g>;
        })}
        {forecast && <rect className="chart-future-bg" x={x(historyCount - 1)} y={TOP} width={WIDTH - RIGHT - x(historyCount - 1)} height={PRICE_BOTTOM - TOP} />}
        {forecast && <line className="chart-boundary" x1={x(historyCount - 1)} y1={TOP} x2={x(historyCount - 1)} y2={POSITION_BOTTOM} />}
        {bandPath && <path className="chart-forecast-band" d={bandPath} />}
        <path className="chart-history-line" d={historyPath} />
        {groupedDays.map(day => {
          const match = historyByDate.get(day);
          if (!match) return null;
          const grouped = eventGroups.get(day) ?? [];
          return <g key={`event-${day}`} className="timeline-marker" role="button" tabIndex={0}
            aria-label={`${day}，${grouped.length} 条事件，查看详情`}
            onClick={() => setSelectedDay(day)}
            onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelectedDay(day); } }}>
            <title>{day} · {grouped.map(item => item.title).join('；')}</title>
            <line x1={x(match.index)} y1={TOP} x2={x(match.index)} y2={PRICE_BOTTOM} />
            <circle cx={x(match.index)} cy={TOP + 12} r="10" />
            <text x={x(match.index)} y={TOP + 15.5} textAnchor="middle">{grouped.length}</text>
          </g>;
        })}
        {forecast && <path className={`chart-forecast-line ${forecast.status === 'research_only' ? 'research' : ''}`} d={forecastPath} />}
        {signals.map((signal, index) => {
          const match = historyByDate.get(signal.date);
          if (!match) return null;
          return <g key={`${signal.date}-${signal.kind}-${index}`}>
            <circle className={`chart-signal ${signal.kind}`} cx={x(match.index)} cy={y(match.point.close)} r="11" />
            <text className="chart-signal-label" x={x(match.index)} y={y(match.point.close) + 3.5} textAnchor="middle">{signal.kind === 'buy' ? '买' : '卖'}</text>
          </g>;
        })}
        {position && <>
          <text className="chart-axis-label" x={LEFT - 10} y={POSITION_TOP + 4} textAnchor="end">持有</text>
          <text className="chart-axis-label" x={LEFT - 10} y={POSITION_BOTTOM + 4} textAnchor="end">空仓</text>
          <line className="chart-gridline" x1={LEFT} y1={POSITION_TOP} x2={WIDTH - RIGHT} y2={POSITION_TOP} />
          <line className="chart-gridline" x1={LEFT} y1={POSITION_BOTTOM} x2={WIDTH - RIGHT} y2={POSITION_BOTTOM} />
          <path className="chart-position-line" d={positionPath} />
        </>}
        <text className="chart-axis-label" x={x(0)} y={HEIGHT - 16} textAnchor="start">{shownHistory[0].date}</text>
        <text className="chart-axis-label" x={x(historyCount - 1)} y={HEIGHT - 16} textAnchor="end">{chart.asof}</text>
        {forecast && <text className="chart-axis-label" x={x(total - 1)} y={HEIGHT - 16} textAnchor="end">+{future[future.length - 1].step} 观察点</text>}
      </svg>
    </div>
    <div className="timeline-details" aria-live="polite">
      <h4>事件与出处{activeDay ? ` · ${activeDay}` : ''}</h4>
      {activeEvents.length ? <ul>{activeEvents.map(event => <li key={event.event_id}>
        <strong>{event.title}</strong><span className="timeline-category">{timeline?.layers.find(layer => layer.category === event.category)?.label ?? event.category}</span>
        <div>公告或观测：{event.event_at}（{event.event_time_kind === 'observation_date' ? '因子观测日' : event.event_time_kind === 'announcement_time' ? '披露时间' : event.event_time_kind === 'document_calendar_date' ? '文件公开日期' : '公告日期'}）
          {event.effective_at ? ` · 生效：${event.effective_at}` : ''} · 图上交易日：{event.anchor_date ?? '暂无后续交易日'}</div>
        <div>{event.detail}</div>
        <div>来源：{event.source_url ? <a href={event.source_url} target="_blank" rel="noreferrer">{event.source_id}</a> : event.source_id} · 证据：{event.availability}{event.source_sha256 ? ` · 本地登记 SHA-256 ${event.source_sha256.slice(0, 12)}…` : ''}{event.input_sha256 ? ` · 输入 SHA-256 ${event.input_sha256.slice(0, 12)}…` : ''}</div>
      </li>)}</ul> : <p>所选图窗内没有已接入、已开启的事件点。</p>}
      {showEventContext && <p className="timeline-price-context"><strong>附近价格：</strong>
        标记日收盘较前一个已载入收盘 {percent(firstCloseChange)}；随后 5 个已载入交易日 {percent(fiveCloseChange)}。
        只描述当前价格口径下的变化，不能证明由事件引起；缺少完整窗口时显示“—”。</p>}
      {timeline?.layers?.map(layer => <p key={layer.category} className="helper"><strong>{layer.label}：</strong>{layer.coverage}</p>)}
    </div>
    <div className="chart-notes">
      <p>{signals.length > 0
        ? <>图中“买 / 卖”只来自历史 20/60 日均线，{rule ? `规则 ${rule.rule_id} · 版本 ${rule.rule_version}；` : ''}与上方结合波动、回撤、流动性和数据日期的操作参考不同。两者可能给出不同判断，图中“买”不等于当前买入建议，也不代表真实成交。</>
        : '当前数据未生成可展示的历史买卖标记。'}{position ? '下方阶梯线为历史规则持仓状态。' : '尚无可展示的规则持仓曲线。'}{chart.chart_version ? ` 图表版本 ${chart.chart_version}。` : ''}</p>
      {forecast
        ? <p className={forecast.status === 'research_only' ? 'chart-research-note' : ''}><strong>{forecast.status === 'research_only' ? '实验预测·不能作为买卖依据' : '已验证预测参考'}</strong> · {forecast.model_id} {forecast.model_version}{forecast.instrument_type ? ` · 识别类型 ${forecast.instrument_type}` : ''}{forecast.router_version ? ` · 路由 ${forecast.router_version}` : ''} · 基准日 {forecast.asof} · {validationText}。未来横轴为第几个交易观察点，不是实际交易日期；{forecastEvidenceNote}</p>
        : <p>暂无可展示的预测曲线；未通过模型和价格口径检查的数据不会连入历史价格。</p>}
    </div>
  </section>;
}
