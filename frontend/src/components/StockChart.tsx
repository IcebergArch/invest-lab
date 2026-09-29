import { finite, text } from '../format';
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
  return Array.isArray(history) && history.length >= 2 && history.length <= 1000
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
  const chart = report.chart;
  const history = chart?.history;
  if (chart?.status !== 'ready' || !validHistory(history) || !validDate(chart.asof)
    || (chart.price_basis !== 'qfq_cny' && chart.price_basis !== 'qlib_adjusted')
    || chart.asof !== history[history.length - 1].date || (report.asof && chart.asof !== report.asof)) {
    return <section className="card report-block stock-chart-card" aria-label="价格和信号图">
      <h3>价格与买卖信号</h3>
      <p className="sub">暂无可核验的价格曲线。{text(report.asof, '') && `报告数据截至 ${report.asof}。`}</p>
    </section>;
  }

  const forecast = validForecast(chart, chart.asof);
  const future = forecast?.points ?? [];
  const shownHistory = history.length > 180 ? history.slice(-180) : history;
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
  const position = Array.isArray(positionRows) && positionRows.length >= 2 && positionStart >= 0
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
  const signals = Array.isArray(chart.signals)
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

  return <section className="card report-block stock-chart-card" aria-label="历史价格、规则信号与研究预测图">
    <div className="chart-heading">
      <div><h3>价格与买卖信号</h3><p className="sub">历史收盘价与历史规则信号 · 数据截至 {chart.asof} · {priceLabel}</p></div>
      <div className="chart-legend" aria-label="图例">
        <span><i className="chart-key history-key" />历史价格</span>
        {signals.length > 0 && <span><i className="chart-key buy-key" />买 / 卖信号</span>}
        {position && <span><i className="chart-key position-key" />历史规则持仓</span>}
        {forecast && <span><i className="chart-key forecast-key" />{forecast.status === 'research_only' ? '实验预测' : '已验证预测'}</span>}
      </div>
    </div>
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
    <div className="chart-notes">
      <p>{signals.length > 0
        ? <>图中“买 / 卖”只来自历史 20/60 日均线，{rule ? `规则 ${rule.rule_id} · 版本 ${rule.rule_version}；` : ''}与上方结合波动、回撤、流动性和数据日期的操作参考不同。两者可能给出不同判断，图中“买”不等于当前买入建议，也不代表真实成交。</>
        : '当前数据未生成可展示的历史买卖标记。'}{position ? '下方阶梯线为历史规则持仓状态。' : '尚无可展示的规则持仓曲线。'}{chart.chart_version ? ` 图表版本 ${chart.chart_version}。` : ''}</p>
      {forecast
        ? <p className={forecast.status === 'research_only' ? 'chart-research-note' : ''}><strong>{forecast.status === 'research_only' ? '实验预测·不能作为买卖依据' : '已验证预测参考'}</strong> · {forecast.model_id} {forecast.model_version} · 基准日 {forecast.asof} · {validationText}。未来横轴为第几个交易观察点，不是实际交易日期。</p>
        : <p>暂无可展示的预测曲线；未通过模型和价格口径检查的数据不会连入历史价格。</p>}
    </div>
  </section>;
}
