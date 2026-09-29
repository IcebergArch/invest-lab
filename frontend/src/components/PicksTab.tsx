import { finite, magnitudePercent, percent, text } from '../format';
import type { DashboardPayload, RecommendedStock } from '../types';
import { EmptyState, Notice, SectionHeader } from './ui';

interface Props {
  data: DashboardPayload | null;
  loading: boolean;
  error: string | null;
}

function EvidenceCard({ title, value, detail }: { title: string; value: string; detail: string }) {
  return <div className="card evidence-card"><div className="kicker">{title}</div>
    <div className="evidence-value">{value}</div><p className="sub">{detail}</p></div>;
}

const integer = new Intl.NumberFormat('zh-CN');
function number(value: unknown): string {
  return finite(value) ? integer.format(value) : '—';
}

function archiveUpdated(value: unknown): string {
  const timestamp = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(timestamp)
    ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short' }).format(new Date(timestamp))
    : '时间未知';
}

function CandidateCard({ stock, rank }: { stock: RecommendedStock; rank: number }) {
  return <article className="card stock-card">
    <div className="stock-top">
      <div><div className="stock-name">{text(stock.name, stock.instrument_id)}</div><div className="stock-id">{stock.instrument_id} · {text(stock.industry_group, '行业未标注')}</div></div>
      <span className="pill">候选 {rank}</span>
    </div>
    <p className="reason">{text(stock.basis, '本次研究门槛已通过；请结合风险指标人工复核。')}</p>
    <div className="stock-metrics">
      <div><span>近 20 日走势</span><strong>{percent(stock.risk_metrics?.momentum_20d)}</strong></div>
      <div><span>近 60 日最大回撤</span><strong>{magnitudePercent(stock.risk_metrics?.max_drawdown_60d)}</strong></div>
      <div><span>预测 20 日收益</span><strong>{percent(stock.forecast_expected_return_20d)}</strong></div>
      <div><span>预测下档 10% 分位</span><strong>{percent(stock.forecast_p10_return_20d)}</strong></div>
    </div>
  </article>;
}

export default function PicksTab({ data, loading, error }: Props) {
  const screen = data?.stock_shortlist;
  const rec = data?.recommendations;
  const archive = data?.historical_archive;
  const qlib = data?.qlib_archive;
  const research = data?.research;
  const forecast = data?.forecast;
  const candidates = Array.isArray(rec?.recommendations) ? rec.recommendations : [];
  const ready = rec?.status === 'ready_for_human_review' && candidates.length === 3
    && candidates.every(item => typeof item?.instrument_id === 'string' && item.instrument_id.length > 0);
  const failed = Array.isArray(rec?.checks) ? rec.checks.filter(check => check.passed === false) : [];
  const metrics = research?.backtest?.metrics;
  const future = research?.backtest?.prospective_out_of_sample;
  const skill = forecast?.pooled?.['5'];
  const count = Array.isArray(research?.universe) ? research.universe.length : null;
  const asof = rec?.asof || screen?.asof || data?.run?.stock_signal_asof;

  return <>
    <SectionHeader title="基于近期数据的三只候选" subtitle="先过数据覆盖、流动性与风险门槛，再看回测和预测验证。"
      badge={asof ? `数据截至 ${asof}` : '暂无数据'} />
    {error && <Notice error>{error}</Notice>}
    {loading && !data && <div className="card loading-card" role="status">正在读取研究报告…</div>}
    {data && (ready ? <>
      <div className="grid three">{candidates.map((stock, index) => <CandidateCard key={stock.instrument_id} stock={stock} rank={index + 1} />)}</div>
      <Notice>这三只是研究候选，不是收益承诺；预测与历史检验已按本次门槛核对，最终由你决策。</Notice>
    </> : <EmptyState title="本次暂不发布三只候选">
      <p>{text(screen?.reason, text(rec?.reason, '数据或研究验证尚未达到发布门槛。'))}</p>
      {failed.length > 0 && <ul className="simple-list">
        {failed.slice(0, 3).map((check, index) => <li key={check.key || index}>{text(check.detail, '证据门槛尚未通过。')}</li>)}
        {failed.length > 3 && <li>另有 {failed.length - 3} 项门槛待核验。</li>}
      </ul>}
      <div className="coverage">
        {finite(screen?.expected_universe_count) && <span className="pill">股票池清单 {screen?.expected_universe_count} 只</span>}
        {finite(screen?.universe_count) && <span className="pill">荐股主库 {screen?.universe_count} 只</span>}
        {finite(screen?.data_ready_count) && <span className="pill">可用行情 {screen?.data_ready_count} 只</span>}
      </div>
    </EmptyState>)}

    {data && <>
      <div className="section-label"><h3>历史原始归档</h3><span>与当前荐股研究池分开统计 · 统计更新 {archiveUpdated(archive?.published_at)}</span></div>
      {archive?.status === 'ready' ? <>
        <div className="grid three">
          <EvidenceCard title="有日线的股票" value={`${number(archive.stock_count)} / ${number(archive.catalogue_stock_count)} 只`}
            detail={`${archive.catalogue_scope === 'provider-active-and-inactive-sh-sz-a' ? '清单含当前及已退出的沪深 A 股，不含北交所。' : '按已保存的沪深 A 股清单统计。'}至少有一段数据，不代表该股自 2000 年至今已补齐。`} />
          <EvidenceCard title="已归档原始日线" value={`${number(archive.bar_count)} 条`}
            detail={`已完成 ${number(archive.completed_window_count)} 个分段；这是未复权日线的归档检查点数量。`} />
          <EvidenceCard title="当前用途" value="仅供数据整理"
            detail="需先校验公司行动与历史股票池。归档数据目前不进入荐股、回测或个股查询。" />
        </div>
        {finite(archive.failed_window_count) && archive.failed_window_count > 0 &&
          <Notice error>有 {number(archive.failed_window_count)} 个分段回补失败，相关覆盖还需核查。</Notice>}
      </> : <Notice>历史原始归档尚不可读取；上方“本地入库”和“可用行情”仅代表当前研究库。</Notice>}
    </>}

    {qlib?.status === 'inspected' && <>
      <div className="section-label"><h3>Qlib 复权研究归档</h3><span>Release {qlib.release_tag} · 检验于 {archiveUpdated(qlib.validated_at)}</span></div>
      <div className="grid three">
        <EvidenceCard title="有有效收盘的代码" value={`${number(qlib.stock_count_with_valid_close)} 个`}
          detail={`交易日历 ${qlib.calendar_first} 至 ${qlib.calendar_last}；各股票的上市、退出和缺值区间不同。`} />
        <EvidenceCard title="有效收盘日线" value={`${number(qlib.bar_count)} 条`}
          detail="Qlib 复权口径，可用于个股历史走势观察；复权价不能直接与成交成本价比较。" />
        <EvidenceCard title="已退出股票对照" value={`${number(qlib.inactive_reference_covered)} 只`}
          detail="与参考清单中已退出股票的代码交集；不能据此确认历史每日可交易状态。" />
      </div>
      <Notice>这个归档已支持个股查询和走势、风险观察；成本价比较取决于报告中的原价核验状态。它尚未进入荐股股票池、策略回测或预测检验。</Notice>
    </>}

    <div className="section-label"><h3>研究证据</h3><span>仅说明已验证范围</span></div>
    <div className="grid three">
      <EvidenceCard title="回测试验范围" value={count === null ? '暂无' : `${count} 只试验股`}
        detail={finite(metrics?.max_drawdown) ? `策略最大回撤 ${magnitudePercent(metrics?.max_drawdown)}；独立时间外验证${future?.status === 'pending' ? '待完成' : '状态未确认'}。` : '还没有可展示的策略回测结果。'} />
      <EvidenceCard title="现有动量预测检验"
        value={skill?.status === 'ready' && finite(skill.mae_return_skill_vs_random_walk) ? `${percent(skill.mae_return_skill_vs_random_walk)} 相对基线` : '尚无可用结论'}
        detail={skill?.status === 'ready' ? `近 5 日预测误差相对不变价基线；范围：${text(forecast?.universe_scope, count === null ? '仅已验证样本' : `仅 ${count} 只试验股`)}。` : text(forecast?.reason, '模型尚未完成验证。')} />
      <EvidenceCard title="发布门槛" value={ready ? '本次通过' : '本次未通过'}
        detail={ready ? '候选仅供人工复核，不自动下单。' : '先补齐股票池、日线和独立验证，再发布三只候选。'} />
    </div>
    <Notice>{ready ? '候选使用已验证模型的同日预测和风险下档；它仍不能保证收益或覆盖所有黑天鹅事件。' : '当前预测结果尚未通过用于荐股的独立验证，因此不会参与股票排序。'}</Notice>
  </>;
}
