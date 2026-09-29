import type { ReactNode } from 'react';

export function SectionHeader({ title, subtitle, badge }: {
  title: string;
  subtitle: string;
  badge?: string;
}) {
  return <div className="section-head">
    <div><h2>{title}</h2><p className="sub">{subtitle}</p></div>
    {badge && <span className="date-pill">{badge}</span>}
  </div>;
}

export function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <div className={`notice${error ? ' error' : ''}`} role={error ? 'alert' : undefined}>{children}</div>;
}

export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return <div className="card empty">
    <div className="empty-mark" aria-hidden="true">⌁</div>
    <h3>{title}</h3>
    <div className="empty-content">{children}</div>
  </div>;
}

export function Fact({ label, value, valueClass = '' }: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return <div className="fact"><span>{label}</span><strong className={valueClass}>{value}</strong></div>;
}
