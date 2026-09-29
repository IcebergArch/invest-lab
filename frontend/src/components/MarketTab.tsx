import { finite, percent, signedClass, text } from '../format';
import type { DashboardPayload } from '../types';
import { EmptyState, Notice, SectionHeader } from './ui';

interface Props {
  data: DashboardPayload | null;
  loading: boolean;
  error: string | null;
}

export default function MarketTab({ data, loading, error }: Props) {
  const market = data?.market;
  const flow = data?.flow;
  const lines = Array.isArray(flow?.reader_lines) ? flow.reader_lines : [];
  const broad = Array.isArray(market?.broad) ? market.broad : [];
  const groups = Array.isArray(market?.groups) ? market.groups : [];
  const dataMissing = !market?.asof || market.status === 'missing_report' || market.status === 'invalid_report';

  return <>
    <SectionHeader title="今日股市趋势分析" subtitle="先看方向，再看钱集中在哪些大类。"
      badge={market?.asof ? `宽基截至 ${market.asof}` : '暂无行情'} />
    {error && <Notice error>{error}</Notice>}
    {loading && !data && <div className="card loading-card" role="status">正在读取行情报告…</div>}
    {!loading && dataMissing && <EmptyState title="行情报告暂不可用"><p>{text(market?.reason, '请先运行量化研究链路。')}</p></EmptyState>}
    {data && !dataMissing && <>
      <div className="card hero-card">
        <div className="kicker">一分钟看盘</div>
        <p className="hero-line">{text(lines[0], '目前没有可读的行情结论。')}</p>
        <p className="hero-caption">宽基截至 {market?.asof}；行业截至 {text(market?.industry_asof, '未知')}。这是收盘后报告。</p>
      </div>
      {market?.asof && market?.industry_asof && market.asof !== market.industry_asof &&
        <Notice><strong>日期提示：</strong>{text(flow?.date_line, `行业只更新到 ${market.industry_asof}，不能用它解释 ${market.asof} 的大盘变化。`)}</Notice>}

      <div className="section-label"><h3>大盘怎么走</h3><span>宽基指数 · 截至 {market?.asof}</span></div>
      <div className="grid five">
        {broad.length ? broad.map(row => <div key={row.instrument_id || row.name} className="card mini-card">
          <div className="label">{text(row.name, text(row.instrument_id, '指数'))}</div>
          <div className={`value ${signedClass(row.returns?.['1d'])}`}>{percent(row.returns?.['1d'], 2)}</div>
          <div className="minor">近 5 日 {percent(row.returns?.['5d'], 2)}</div>
        </div>) : <div className="card mini-card muted">宽基指数数据暂不可用。</div>}
      </div>

      <div className="section-label"><h3>懒人版资金观察</h3><span>成交关注度，只表示交易活跃</span></div>
      <div className="grid three">
        {[
          ['01 / 大盘方向', lines[0]],
          ['02 / 行业热度', lines[1]],
          ['03 / 净流入状态', flow?.net_flow_line || lines[2]],
        ].map(([label, content]) => <div className="card flow-card" key={label}>
          <div className="num">{label}</div><p>{text(content, '暂无可核验的数据。')}</p>
        </div>)}
      </div>

      <div className="section-label"><h3>行业主线</h3><span>申万一级行业合并阅读 · 截至 {text(market?.industry_asof)}</span></div>
      <div className="table-wrap"><table>
        <thead><tr><th scope="col">大类</th><th scope="col">1 日</th><th scope="col">5 日</th><th scope="col">成交占比变化</th><th scope="col">上涨行业数</th></tr></thead>
        <tbody>{groups.length ? groups.map(row => <tr key={row.name}>
          <td>{text(row.name)}</td>
          <td className={signedClass(row.returns?.['1d'])}>{percent(row.returns?.['1d'], 2)}</td>
          <td className={signedClass(row.returns?.['5d'])}>{percent(row.returns?.['5d'], 2)}</td>
          <td>{finite(row.share_change_pp) ? `${row.share_change_pp >= 0 ? '+' : ''}${row.share_change_pp.toFixed(2)} 个百分点` : '无可比成交额'}</td>
          <td>{row.up_1d ?? '—'} / {row.member_count ?? '—'}</td>
        </tr>) : <tr><td colSpan={5}>行业分组数据暂不可用。</td></tr>}</tbody>
      </table></div>
      <p className="helper">成交占比变化＝该大类当日成交占比减去此前 20 个共同交易日平均值；它不能说明谁在净买入。</p>
    </>}
  </>;
}
