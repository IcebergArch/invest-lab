export function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

export function percent(value: unknown, digits = 1): string {
  if (!finite(value)) return '—';
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`;
}

export function magnitudePercent(value: unknown, digits = 1): string {
  return finite(value) ? `${(Math.abs(value) * 100).toFixed(digits)}%` : '—';
}

export function yuan(value: unknown): string {
  return finite(value) ? `¥${value.toFixed(2)}` : '—';
}

export function signedClass(value: unknown): string {
  if (!finite(value)) return 'neutral';
  return value > 0 ? 'positive' : value < 0 ? 'negative' : 'neutral';
}

export function text(value: unknown, fallback = '—'): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '发生未知错误。';
}
