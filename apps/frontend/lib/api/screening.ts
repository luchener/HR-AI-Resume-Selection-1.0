import type { AnalysisResult } from '@/components/workbench/analysis-context';
import { getStoredToken } from '@/components/workbench/auth-context';
import { API_URL, SQL_ANALYSIS_ENABLED } from './config';
import { runSqlAnalysis, SqlAnalysisConnectionError, type SqlAnalysisCheckpoint } from './sql-analysis-stream';
export { SqlAnalysisConnectionError } from './sql-analysis-stream';
export type { SqlAnalysisCheckpoint } from './sql-analysis-stream';

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) };
  const token = getStoredToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

function handleUnauthorized(response: Response): void {
  if (response.status === 401) {
    try {
      window.sessionStorage.removeItem('resume-screening-token');
      window.sessionStorage.removeItem('resume-screening-user');
      window.localStorage.removeItem('resume-screening-token');
      window.localStorage.removeItem('resume-screening-user');
      window.sessionStorage.removeItem('resume-screening-result');
    } catch { /* ignore */ }
    if (typeof window !== 'undefined' && window.location.pathname !== '/login') window.location.replace('/login');
  }
}

async function errorDetail(response: Response): Promise<string> {
  const text = await response.text();
  try { return (JSON.parse(text) as { detail?: string }).detail || text; } catch { return text; }
}

export interface ResumeReviewMarker {
  id: string;
  category: 'strength' | 'match' | 'risk' | 'missing' | 'verify';
  label: string;
  quote: string;
  reason: string;
  start: number;
  end: number;
  confidence: string;
}

export interface ResumeReviewData {
  candidate_name: string;
  annotations: ResumeReviewMarker[];
  summary: { final_score: number; recommendation: string; highlights: number; risks: number; verify_count?: number };
  notice: string;
}

export async function fetchResumeReviewMarkers(resumeId: string, analysis: Record<string, unknown>, candidateName?: string, signal?: AbortSignal): Promise<ResumeReviewData> {
  const response = await fetch(`${API_URL}/api/v1/resumes/review-markers`, { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ resume_id: resumeId, analysis, candidate_name: candidateName }), signal });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `简历重点标记生成失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: ResumeReviewData };
  if (!payload.data || !Array.isArray(payload.data.annotations) || !payload.data.summary ||
      !Number.isFinite(payload.data.summary.final_score)) throw new Error('服务未返回有效的简历重点标记。');
  return payload.data;
}

export async function fetchResumeView(resumeId: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/resumes?resume_id=${encodeURIComponent(resumeId)}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `简历内容读取失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: { raw_resume?: { content?: string } } };
  return payload.data?.raw_resume?.content || '';
}

export async function uploadResume(file: File, signal?: AbortSignal): Promise<string> {
  const formData = new FormData(); formData.append('file', file);
  const response = await fetch(`${API_URL}/api/v1/resumes/upload`, { method: 'POST', headers: authHeaders(), body: formData, signal });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `简历上传失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { resume_id?: string };
  if (!payload.resume_id) throw new Error('上传成功，但服务未返回简历编号。'); return payload.resume_id;
}

export async function uploadJobDescription(description: string, resumeId: string, signal?: AbortSignal): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/jobs/upload`, { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ job_descriptions: [description], resume_id: resumeId }), signal });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `岗位描述上传失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { job_id?: string[] }; const jobId = payload.job_id?.[0];
  if (!jobId) throw new Error('上传成功，但服务未返回岗位编号。'); return jobId;
}

/** 删除简历原文（归档会移入回收站，可恢复）。返回被移入回收站的归档数量。 */
export async function deleteResumeSource(resumeId: string): Promise<{ archivedToTrash: number }> {
  const response = await fetch(`${API_URL}/api/v1/resumes/${encodeURIComponent(resumeId)}`, { method: 'DELETE', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `简历原文删除失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: { archived_to_trash?: number } };
  return { archivedToTrash: payload.data?.archived_to_trash ?? 0 };
}

export async function analyzeResumes(resumeIds: string | string[], jobId: string, signal?: AbortSignal): Promise<AnalysisResult> {
  if (SQL_ANALYSIS_ENABLED) return analyzeResumesStream(resumeIds, jobId, undefined, undefined, signal);
  const response = await fetch(`${API_URL}/api/v1/resumes/hr-analysis`, { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(Array.isArray(resumeIds) ? { resume_ids: resumeIds, job_id: jobId } : { resume_id: resumeIds, job_id: jobId }), signal });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `招聘分析失败（HTTP ${response.status}），请稍后重试。`); }
  return (await response.json()) as AnalysisResult;
}

export interface AgentConfig {
  web_search?: boolean;
}

export async function analyzeResumesStream(
  resumeIds: string | string[],
  jobId: string,
  agentConfig: AgentConfig | undefined,
  onProgress?: (status: string, message: string, index?: number, total?: number) => void,
  signal?: AbortSignal,
  durable?: { checkpoint?: SqlAnalysisCheckpoint; onCheckpoint: (checkpoint: SqlAnalysisCheckpoint) => void },
): Promise<AnalysisResult> {
  if (SQL_ANALYSIS_ENABLED) {
    return runSqlAnalysis({ baseUrl: API_URL, resumeIds: Array.isArray(resumeIds) ? resumeIds : [resumeIds],
      jobId, token: getStoredToken(), getToken: getStoredToken, onUnauthorized: handleUnauthorized,
      onProgress, signal, webSearch: agentConfig?.web_search === true, ...durable });
  }
  const body: Record<string, unknown> = { job_id: jobId };
  if (Array.isArray(resumeIds)) body.resume_ids = resumeIds; else body.resume_id = resumeIds;
  if (agentConfig && (agentConfig.web_search)) body.agent_config = agentConfig;

  const response = await fetch(`${API_URL}/api/v1/resumes/hr-analysis?stream=true`, { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json', Accept: 'text/event-stream' }), body: JSON.stringify(body), signal });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `招聘分析失败（HTTP ${response.status}），请稍后重试。`); }
  if (!response.body) throw new Error('招聘分析服务未返回数据流。');

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let finalResult: AnalysisResult | null = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let separator: number;
    while ((separator = buffer.indexOf('\n\n')) !== -1) {
      const rawEvent = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      const dataLine = rawEvent.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('');
      if (!dataLine) continue;
      let event: { status: string; message?: string; result?: AnalysisResult; index?: number; total?: number };
      try { event = JSON.parse(dataLine) as typeof event; } catch { continue; }
      if (event.status === 'completed' && event.result) finalResult = event.result;
      else if (event.status === 'error') throw new Error(event.message || '招聘分析失败。');
      else onProgress?.(event.status, event.message ?? '', event.index, event.total);
    }
  }
  if (!finalResult) throw new Error('招聘分析数据流提前结束。');
  return finalResult;
}

let improvementOwner = '';
const improvementRequests = new Map<string, { checkpoint?: SqlAnalysisCheckpoint; control?: AbortController }>();
export function resetSqlImprovementRequests(): void {
  for (const request of improvementRequests.values()) request.control?.abort();
  improvementRequests.clear();
  improvementOwner = '';
}

export async function improveResumeStream(resumeId: string, jobId: string, onProgress?: (status: string, message: string) => void, signal?: AbortSignal): Promise<AnalysisResult> {
  if (SQL_ANALYSIS_ENABLED) {
    const token = getStoredToken();
    if (improvementOwner !== token) resetSqlImprovementRequests();
    improvementOwner = token;
    const key = `${resumeId}:${jobId}`;
    let slot = improvementRequests.get(key);
    if (slot?.control) throw new Error('该简历优化任务正在运行。');
    if (!slot) {
      if (improvementRequests.size >= 16) throw new Error('请先恢复已有的优化任务。');
      slot = {};
      improvementRequests.set(key, slot);
    }
    const saved = slot;
    const control = new AbortController();
    saved.control = control;
    const abort = () => control.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) control.abort();
    try {
      const result = await runSqlAnalysis({ baseUrl: API_URL, resumeIds: [resumeId], jobId, kind: 'improvement',
        token, getToken: getStoredToken, onUnauthorized: handleUnauthorized, signal: control.signal, onProgress,
        checkpoint: saved.checkpoint, onCheckpoint: checkpoint => { saved.checkpoint = checkpoint; } });
      if (improvementRequests.get(key) === saved) improvementRequests.delete(key);
      return result;
    } catch (error) {
      if (error instanceof SqlAnalysisConnectionError) {
        error.message = '优化连接暂时无法恢复，请再次点击深度优化恢复原任务进度。';
      }
      if (!(error instanceof SqlAnalysisConnectionError) && !(error instanceof DOMException && error.name === 'AbortError')) {
        if (improvementRequests.get(key) === saved) improvementRequests.delete(key);
      }
      throw error;
    } finally {
      saved.control = undefined;
      signal?.removeEventListener('abort', abort);
    }
  }
  const response = await fetch(`${API_URL}/api/v1/resumes/improve?stream=true`, { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json', Accept: 'text/event-stream' }), body: JSON.stringify({ resume_id: resumeId, job_id: jobId }), signal });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `深度优化失败（HTTP ${response.status}）`); }
  if (!response.body) throw new Error('深度优化服务未返回数据流。');
  const reader = response.body.getReader(); const decoder = new TextDecoder('utf-8'); let buffer = ''; let finalResult: AnalysisResult | null = null;
  while (true) {
    const { done, value } = await reader.read(); if (done) break; buffer += decoder.decode(value, { stream: true });
    let separator: number;
    while ((separator = buffer.indexOf('\n\n')) !== -1) {
      const rawEvent = buffer.slice(0, separator); buffer = buffer.slice(separator + 2);
      const dataLine = rawEvent.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join(''); if (!dataLine) continue;
      let event: { status: string; message?: string; result?: AnalysisResult }; try { event = JSON.parse(dataLine) as typeof event; } catch { continue; }
      if (event.status === 'completed' && event.result) finalResult = event.result; else if (event.status === 'error') throw new Error(event.message || '深度优化失败。'); else onProgress?.(event.status, event.message ?? '');
    }
  }
  if (!finalResult) throw new Error('深度优化数据流提前结束。'); return finalResult;
}

export async function fetchImprovedMarkdown(resumeId: string, jobId: string, analysisResult: string, requireOptimized: boolean): Promise<{ markdown: string; source: string }> {
  const response = await fetch(`${API_URL}/api/v1/resumes/improved-markdown`, { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ resume_id: resumeId, job_id: jobId, analysis_result: analysisResult, require_optimized: requireOptimized }) });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `编辑器内容生成失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: { markdown?: string; source?: string } };
  return { markdown: payload.data?.markdown || '', source: payload.data?.source || 'none' };
}

export async function changePassword(oldPassword: string, newPassword: string): Promise<{ message: string; requireRelogin: boolean }> {
  const response = await fetch(`${API_URL}/api/v1/auth/change-password`, { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }) });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `密码修改失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: { message?: string; require_relogin?: boolean } };
  return { message: payload.data?.message || '密码修改成功。', requireRelogin: payload.data?.require_relogin ?? true };
}
