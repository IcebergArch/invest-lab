import { useCallback, useEffect, useRef, useState } from 'react';
import type { MouseEvent } from 'react';
import { loadDashboard } from './api';
import { errorMessage, text } from './format';
import MarketTab from './components/MarketTab';
import PicksTab from './components/PicksTab';
import StockTab from './components/StockTab';
import ConsolePage from './components/ConsolePage';
import type { QuantModule } from './components/ConsolePage';
import type { DashboardPayload } from './types';

type TabKey = 'market' | 'picks' | 'stock';
type SpaceKey = 'research' | 'quant';

const researchPages: { id: TabKey; label: string; detail: string; number: string }[] = [
  { id: 'market', label: '今日股市趋势', detail: '行情与资金观察', number: '01' },
  { id: 'picks', label: '三只候选', detail: '筛选与研究证据', number: '02' },
  { id: 'stock', label: '查一只股票', detail: '个股分析报告', number: '03' },
];

const quantSections: { id: QuantModule; label: string; detail: string; number: string }[] = [
  { id: 'overview', label: '总览', detail: '状态 · 任务 · 入口', number: '01' },
  { id: 'data', label: '数据管理', detail: '来源 · 库存 · 同步', number: '02' },
  { id: 'strategy', label: '策略与因子', detail: '策略 · 算法 · 因子', number: '03' },
  { id: 'experiments', label: '实验与回测', detail: '优化 · 记录 · 结果', number: '04' },
  { id: 'forecast', label: '预测评估', detail: '模型 · 误差 · 门禁', number: '05' },
  { id: 'feedback', label: '复盘反馈', detail: '回执 · 到期复盘', number: '06' },
];

function routeFromHash(): { space: SpaceKey; active: TabKey; module: QuantModule } {
  const hash = window.location.hash;
  const quant = quantSections.find(item => hash === `#quant-${item.id}`);
  if (quant || hash === '#tab-console' || hash === '#space-quant') {
    return { space: 'quant', active: 'market', module: quant?.id ?? 'overview' };
  }
  const key = hash.replace('#tab-', '');
  const page = researchPages.find(item => item.id === key);
  return { space: 'research', active: page?.id ?? 'market', module: 'overview' };
}

export default function App() {
  const [route, setRoute] = useState(routeFromHash);
  const [dashboard, setDashboard] = useState<DashboardPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const mainRef = useRef<HTMLElement | null>(null);
  const lastResearch = useRef<TabKey>(route.space === 'research' ? route.active : 'market');
  const space = route.space;
  const active = route.active;
  const module = route.module;

  const reload = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setLoadError(null);
    try {
      setDashboard(await loadDashboard(controller.signal));
    } catch (error) {
      if (!controller.signal.aborted) setLoadError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    return () => request.current?.abort();
  }, [reload]);

  useEffect(() => {
    const onHashChange = () => {
      const next = routeFromHash();
      if (next.space === 'research') lastResearch.current = next.active;
      setRoute(next);
      window.requestAnimationFrame(() => {
        window.scrollTo({ top: 0, behavior: 'instant' });
        mainRef.current?.focus({ preventScroll: true });
      });
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const skipToContent = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    mainRef.current?.focus();
  };

  return <>
    <a className="skip-link" href="#main-content" onClick={skipToContent}>跳到主要内容</a>
    <header className="mast">
      <div className="shell mast-inner topline">
        <div className="brand"><span aria-hidden="true">◉</span>A 股研究台</div>
        <div className="mast-actions">
          <div className="top-status"><span className="dot" aria-hidden="true" />
            <span>{space === 'quant' ? '本地量化系统 · 策略与任务状态' : loadError ? '报告读取失败' : loading ? '读取已生成报告…' : `本地报告 · 宽基截至 ${text(dashboard?.market.asof, '未知')}`}</span>
          </div>
          <nav className="space-switch" aria-label="系统空间">
            <a className={`space-button${space === 'research' ? ' selected' : ''}`} href={`#tab-${lastResearch.current}`} aria-current={space === 'research' ? 'page' : undefined}>研究空间</a>
            <a className={`space-button${space === 'quant' ? ' selected' : ''}`} href="#tab-console" aria-current={space === 'quant' ? 'page' : undefined}>量化空间</a>
          </nav>
        </div>
      </div>
    </header>
    <div className="shell workspace">
      <aside className="workspace-sidebar" aria-label={space === 'quant' ? '量化空间目录' : '研究空间目录'}>
        <div className="workspace-nav-head"><strong>{space === 'quant' ? '量化空间' : '研究空间'}</strong><span>{space === 'quant' ? 'QUANT SYSTEM' : 'RESEARCH DESK'}</span></div>
        <nav aria-label={space === 'quant' ? '量化模块' : '研究页面'}>
          {space === 'research' ? <div className="workspace-nav-group">
            {researchPages.map(page => <a
              key={page.id}
              id={`nav-${page.id}`}
              className={`workspace-nav-link${active === page.id ? ' selected' : ''}`}
              href={`#tab-${page.id}`}
              aria-current={active === page.id ? 'page' : undefined}
            ><span className="workspace-nav-number" aria-hidden="true">{page.number}</span><span><strong>{page.label}</strong><small>{page.detail}</small></span></a>)}
          </div> : <div className="workspace-nav-group">
            {quantSections.map(section => <a key={section.id} className={`workspace-nav-link${module === section.id ? ' selected' : ''}`} href={`#quant-${section.id}`} aria-current={module === section.id ? 'page' : undefined}>
              <span className="workspace-nav-number" aria-hidden="true">{section.number}</span><span><strong>{section.label}</strong><small>{section.detail}</small></span>
            </a>)}
          </div>}
        </nav>
        <p className="workspace-nav-foot">研究结论与系统实验各有证据边界。</p>
      </aside>
      <main id="main-content" className="workspace-main" ref={mainRef} tabIndex={-1}>
        {space === 'research' && <div className="workspace-intro">
          <p className="eyebrow">MARKET · SCREEN · REVIEW</p>
          <h1>先看懂市场，再决定看哪只股票。</h1>
          <p className="lead">行情、研究证据和个股查询集中在这里。所有数字均标注数据日期；股票候选由数据和风控门槛决定。</p>
        </div>}
        {space === 'quant' && <ConsolePage module={module} onNavigate={next => { window.location.hash = `#quant-${next}`; }} />}
        <section id="panel-market" aria-labelledby="nav-market" hidden={space !== 'research' || active !== 'market'}>
          <MarketTab data={dashboard} loading={loading} error={loadError} />
        </section>
        <section id="panel-picks" aria-labelledby="nav-picks" hidden={space !== 'research' || active !== 'picks'}>
          <PicksTab data={dashboard} loading={loading} error={loadError} />
        </section>
        <section id="panel-stock" aria-labelledby="nav-stock" hidden={space !== 'research' || active !== 'stock'}>
          <StockTab />
        </section>
      </main>
    </div>
    <footer className="footer">
      <div className="shell footer-inner">
        <span>量化研究与人工决策支持 · 数据、回测和预测均有适用范围</span>
        {space === 'research' && <button type="button" className="refresh" onClick={() => void reload()} disabled={loading}>刷新已生成报告</button>}
      </div>
    </footer>
  </>;
}
