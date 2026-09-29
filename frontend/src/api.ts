import type { AcceptedTask, ConsoleStatus, DashboardPayload, FeedbackCaseInput, FeedbackCaseResult, StockAnalysis } from './types';

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

async function readResponse(response: Response): Promise<Record<string, unknown>> {
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new Error('服务返回的数据无法读取。');
  }
  const value = record(data);
  if (!response.ok) {
    throw new Error(typeof value.reason === 'string' ? value.reason : `服务返回 ${response.status}。`);
  }
  return value;
}

export async function loadDashboard(signal?: AbortSignal): Promise<DashboardPayload> {
  const response = await fetch('/api/dashboard', { cache: 'no-store', signal });
  const data = await readResponse(response);
  return {
    market: record(data.market),
    flow: record(data.flow),
    stock_shortlist: record(data.stock_shortlist),
    recommendations: record(data.recommendations),
    historical_archive: record(data.historical_archive),
    qlib_archive: data.qlib_archive ? record(data.qlib_archive) : undefined,
    research: record(data.research),
    forecast: record(data.forecast),
    run: record(data.run),
  } as DashboardPayload;
}

export async function loadConsoleStatus(signal?: AbortSignal): Promise<ConsoleStatus> {
  const response = await fetch('/api/console-status', { cache: 'no-store', signal });
  const data = await readResponse(response);
  if (typeof data.status !== 'string') throw new Error('控制台状态缺少状态信息。');
  return data as unknown as ConsoleStatus;
}

export async function saveFeedbackCase(input: FeedbackCaseInput): Promise<FeedbackCaseResult> {
  const response = await fetch('/api/feedback-case', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  const data = await readResponse(response);
  if (data.status !== 'saved' || typeof data.case_id !== 'string') {
    throw new Error('回执已提交，但服务未确认保存。');
  }
  return data as unknown as FeedbackCaseResult;
}

async function startTask(path: string, payload: object): Promise<AcceptedTask> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await readResponse(response);
  if (data.status !== 'accepted' || typeof data.task_id !== 'string') {
    throw new Error('任务请求未被服务确认。');
  }
  return data as unknown as AcceptedTask;
}

export function startOptimization(): Promise<AcceptedTask> {
  return startTask('/api/optimize-run', { strategy: 'all' });
}

export function startCaseReview(): Promise<AcceptedTask> {
  return startTask('/api/review-cases', {});
}

export async function analyzeStock(
  symbol: string,
  costPrice: number | null,
  signal?: AbortSignal,
): Promise<StockAnalysis> {
  const response = await fetch('/api/stock-report', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol, cost_price: costPrice }),
    signal,
  });
  const data = await readResponse(response);
  if (typeof data.status !== 'string') throw new Error('个股报告缺少状态信息。');
  return data as unknown as StockAnalysis;
}
