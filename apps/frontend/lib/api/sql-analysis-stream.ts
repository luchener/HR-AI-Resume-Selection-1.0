import type { AnalysisData, AnalysisResult, HrAnalysis } from '../../components/workbench/analysis-context';

type Progress = (status: string, message: string, index?: number, total?: number) => void;
interface Options {
  baseUrl: string;
  kind?: 'screening' | 'improvement';
  webSearch?: boolean;
  resumeIds: string[];
  jobId: string;
  token: string;
  getToken: () => string;
  onUnauthorized: (response: Response) => void;
  onProgress?: Progress;
  signal?: AbortSignal;
  checkpoint?: SqlAnalysisCheckpoint;
  onCheckpoint?: (checkpoint: SqlAnalysisCheckpoint) => void;
}
export interface SqlAnalysisCheckpoint { requestId: string; taskId: string; cursor: number }
export class SqlAnalysisConnectionError extends Error {}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STAGES = new Set(['planning', 'retrieving', 'generating', 'retrying', 'web_search', 'candidate_completed', 'candidate_failed']);
const TERMINAL = new Set(['completed', 'partial_success', 'failed', 'cancelled']);
class ProtocolError extends Error {}
class RetryableError extends Error {}
function aborted(): never { throw new DOMException('分析订阅已停止。', 'AbortError'); }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ProtocolError('分析服务返回了无效数据。');
  return value as Record<string, unknown>;
}

/** Streaming SSE parser: UTF-8, CRLF, comments, multiline data, bounded frames. */
export async function* readSSE(response: Response, signal?: AbortSignal): AsyncGenerator<{ id?: string; data: string }> {
  if (!response.body) throw new RetryableError('分析连接没有返回数据流。');
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '', data: string[] = [], id: string | undefined, size = 0;
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal?.addEventListener('abort', cancel, { once: true });
  try {
    while (true) {
      if (signal?.aborted) aborted();
      const { done, value } = await reader.read();
      if (signal?.aborted) aborted();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      if (buffer.length + size > 1024 * 1024) throw new ProtocolError('分析事件超过大小限制。');
      while (true) {
        const match = /[\r\n]/.exec(buffer);
        if (!match || (buffer[match.index] === '\r' && match.index === buffer.length - 1 && !done)) break;
        const line = buffer.slice(0, match.index);
        const consumed = buffer[match.index] === '\r' && buffer[match.index + 1] === '\n' ? 2 : 1;
        buffer = buffer.slice(match.index + consumed);
        if (!line) {
          if (data.length) yield { id, data: data.join('\n') };
          data = []; id = undefined; size = 0;
        } else if (!line.startsWith(':')) {
          const colon = line.indexOf(':');
          const field = colon < 0 ? line : line.slice(0, colon);
          let text = colon < 0 ? '' : line.slice(colon + 1);
          if (text.startsWith(' ')) text = text.slice(1);
          if (field === 'data') { data.push(text); size += text.length; }
          if (field === 'id') { if (text.includes('\0')) throw new ProtocolError('无效事件序号。'); id = text; }
        }
      }
      // An unfinished frame at EOF is never treated as a committed event.
      if (done) return;
    }
  } finally {
    signal?.removeEventListener('abort', cancel);
    cancel();
    reader.releaseLock();
  }
}

function modelBinding(snapshot: Record<string, unknown>): AnalysisData['model_binding'] {
  let binding: AnalysisData['model_binding'];
  if (snapshot.model_binding !== undefined) {
    const raw = record(snapshot.model_binding);
    if (Object.keys(raw).length !== 3 || typeof raw.binding_id !== 'string' || !UUID.test(raw.binding_id) ||
        typeof raw.config_revision !== 'string' || !/^[0-9a-f]{64}$/.test(raw.config_revision) ||
        typeof raw.policy_revision !== 'string' || !/^[0-9a-f]{64}$/.test(raw.policy_revision)) {
      throw new ProtocolError('无效的任务配置版本。');
    }
    binding = { binding_id: raw.binding_id, config_revision: raw.config_revision, policy_revision: raw.policy_revision };
  }
  return binding;
}

function failedTaskMessage(snapshot: Record<string, unknown>, kind: 'screening' | 'improvement'): string {
  if (kind === 'improvement') {
    if (snapshot.status === 'cancelled') return '深度优化已取消，可以重新发起。';
    const artifact = snapshot.improvement;
    const code = artifact && typeof artifact === 'object' && 'error_code' in artifact ? artifact.error_code : undefined;
    if (code === 'improvement_truncated') return '优化稿超出模型的输出上限。请精简简历或岗位描述后重试。';
    if (code === 'improvement_timeout') return '深度优化等待模型响应超时，请稍后重试。';
    if (code === 'invalid_improvement') return '模型未返回可用的优化稿，请重试深度优化。';
    return '深度优化未生成简历，请重试深度优化。若再次失败，请联系管理员。';
  }
  if (snapshot.status === 'cancelled') return '分析任务已取消，请重新开始。';
  if (Array.isArray(snapshot.analyses) && snapshot.analyses.some(item =>
    item?.error_code === 'model_invalid_response' || item?.error_code === 'invalid_analysis_result'
  )) {
    return '模型没有返回可用的报告格式，请重新开始分析。若再次失败，请联系管理员。';
  }
  return '本次分析未生成报告，请重新开始分析。若再次失败，请联系管理员。';
}

export function sqlResult(value: unknown, resumeIds: string[], jobId: string, taskId: string, kind: 'screening' | 'improvement' = 'screening'): AnalysisResult {
  const envelope = record(value), snapshot = record(envelope.data);
  if (snapshot.task_id !== taskId || !TERMINAL.has(String(snapshot.status)) ||
      snapshot.total_candidates !== resumeIds.length || !Array.isArray(snapshot.analyses) ||
      snapshot.analyses.length > resumeIds.length) throw new ProtocolError('任务结果与本次分析不一致。');
  if ((snapshot.kind ?? 'screening') !== kind) throw new ProtocolError('任务类型不一致。');
  if (kind === 'improvement') {
    if (snapshot.status !== 'completed') throw new ProtocolError(failedTaskMessage(snapshot, kind));
    const result = record(snapshot.improvement);
    if (resumeIds.length !== 1 || snapshot.status !== 'completed' || snapshot.completed_candidates !== 1 ||
        snapshot.failed_candidates !== 0 || snapshot.analyses.length !== 0 || result.resume_id !== resumeIds[0] ||
        result.job_id !== jobId || typeof result.analysis_result !== 'string' || !result.analysis_result.trim() ||
        new TextEncoder().encode(result.analysis_result).length > 262144) throw new ProtocolError('无效的简历优化结果。');
    return { data: { resume_id: resumeIds[0], job_id: jobId, analysis_result: result.analysis_result,
      task_id: taskId, model_binding: modelBinding(snapshot) } };
  }
  const good: AnalysisData[] = [], failed = new Set<string>(), seen = new Set<string>();
  for (const raw of snapshot.analyses) {
    const row = record(raw);
    const resume = String(row.resume_id);
    if (!resumeIds.includes(resume) || seen.has(resume) || row.job_id !== jobId) throw new ProtocolError('候选人结果与本次分析不一致。');
    seen.add(resume);
    if (row.status === 'completed') {
      const report = record(row.hr_analysis);
      if (typeof report.final_score !== 'number' || !Number.isFinite(report.final_score) || report.final_score < 0 || report.final_score > 100) {
        throw new ProtocolError('分析报告缺少有效评分。');
      }
      good.push({ resume_id: resume, job_id: jobId, hr_analysis: report as unknown as HrAnalysis,
        candidate_name: typeof report.candidate_name === 'string' ? report.candidate_name : undefined });
    } else if (row.status === 'failed') failed.add(resume);
    else throw new ProtocolError('无效的候选人结果状态。');
  }
  if (!good.length) throw new ProtocolError(failedTaskMessage(snapshot, kind));
  if (snapshot.completed_candidates !== good.length || snapshot.failed_candidates !== resumeIds.length - good.length) {
    throw new ProtocolError('任务结果数量不一致。');
  }
  for (const resume of resumeIds) if (!seen.has(resume)) failed.add(resume);
  const byId = new Map(good.map(item => [item.resume_id, item]));
  const ordered = resumeIds.flatMap(resume => byId.has(resume) ? [byId.get(resume)!] : []);
  const binding = modelBinding(snapshot);
  const presented = ordered.map(item => ({ ...item, task_id: taskId, ...(binding ? { model_binding: binding } : {}) }));
  return { data: { ...presented[0],
    ...(resumeIds.length > 1 ? { batch_analyses: presented } : {}),
    ...(failed.size ? { batch_failures: [...failed].map(resume_id => ({ resume_id, detail: '该候选人未完成分析。' })) } : {}) } };
}

function pause(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new DOMException('分析订阅已停止。', 'AbortError')); return; }
    const abort = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); reject(new DOMException('分析订阅已停止。', 'AbortError')); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, ms);
    signal?.addEventListener('abort', abort, { once: true });
  });
}

/** Retry only the same SQL idempotent request; once task is known, GET only. */
export async function runSqlAnalysis(options: Options): Promise<AnalysisResult> {
  const { baseUrl, jobId, token, signal, onProgress } = options;
  const resumeIds = [...options.resumeIds];
  const kind = options.kind ?? 'screening';
  if (!['screening', 'improvement'].includes(kind) || (options.webSearch !== undefined && typeof options.webSearch !== 'boolean') ||
      (kind === 'improvement' && (resumeIds.length !== 1 || options.webSearch))) throw new ProtocolError('无效的任务选项。');
  if (!token || !UUID.test(jobId) || !resumeIds.length || resumeIds.length > 3 ||
      new Set(resumeIds).size !== resumeIds.length || resumeIds.some(id => !UUID.test(id))) {
    throw new ProtocolError('需要有效的登录会话和分析输入。');
  }
  const checkpoint = options.checkpoint ? { ...options.checkpoint } : { requestId: crypto.randomUUID(), taskId: '', cursor: 0 };
  if (!UUID.test(checkpoint.requestId) || (checkpoint.taskId && !UUID.test(checkpoint.taskId)) ||
      !Number.isInteger(checkpoint.cursor) || checkpoint.cursor < 0 || checkpoint.cursor > 256 ||
      (!checkpoint.taskId && checkpoint.cursor !== 0)) throw new ProtocolError('无效的任务恢复信息。');
  const requestId = checkpoint.requestId;
  const body = JSON.stringify(kind === 'improvement' ? { resume_id: resumeIds[0], job_id: jobId } :
    { resume_ids: resumeIds, job_id: jobId, ...(options.webSearch ? { agent_config: { web_search: true } } : {}) });
  const started = Date.now();
  let taskId = checkpoint.taskId, cursor = checkpoint.cursor, failures = 0, candidate = '';
  const save = () => options.onCheckpoint?.({ requestId, taskId, cursor });
  const check = () => { if (signal?.aborted || options.getToken() !== token) aborted(); };
  const identify = (value: unknown) => {
    if (typeof value !== 'string' || !UUID.test(value) || (taskId && value !== taskId)) throw new ProtocolError('任务标识不一致。');
    taskId = value;
    save();
  };
  check(); save();
  while (Date.now() - started < 20 * 60 * 1000) {
    check();
    const connection = new AbortController();
    const abort = () => connection.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, 45000);
    const openedAt = Date.now();
    let sawFrame = false;
    try {
      const headers: Record<string, string> = { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' };
      if (taskId) headers['Last-Event-ID'] = String(cursor);
      else { headers['Content-Type'] = 'application/json'; headers['Idempotency-Key'] = requestId; }
      const url = taskId ? `${baseUrl}/api/v1/analysis-tasks/${encodeURIComponent(taskId)}/events?stream=true`
        : `${baseUrl}/api/v1/resumes/${kind === 'improvement' ? 'improve' : 'hr-analysis'}?stream=true`;
      const response = await fetch(url, { method: taskId ? 'GET' : 'POST', headers, body: taskId ? undefined : body,
        signal: connection.signal, cache: 'no-store', redirect: 'error' });
      check();
      if (response.status === 401) { void response.body?.cancel(); options.onUnauthorized(response); throw new ProtocolError('登录已失效，请重新登录。'); }
      if (!response.ok) {
        if (response.status === 404 && kind === 'improvement') {
          if (taskId) {
            void response.body?.cancel();
            throw new ProtocolError('原优化任务已无法恢复，请重新点击「深度优化简历」发起新任务。');
          }
          const payload = await response.json().catch(() => null);
          check();
          if (payload?.detail === 'Resume or job not found or unavailable') {
            throw new ProtocolError('这份报告关联的简历原文或岗位已不可用。请返回筛选工作台，重新上传简历并填写岗位描述，完成分析后再深度优化。');
          }
          throw new ProtocolError('深度优化接口暂不可用（HTTP 404），请稍后重试或联系管理员。');
        }
        void response.body?.cancel();
        if ([408, 502, 503, 504].includes(response.status)) throw new RetryableError('分析服务暂时不可用。');
        throw new ProtocolError(response.status === 429 ? '分析额度不足或请求过于频繁，请查看配额。' : `无法读取分析任务（HTTP ${response.status}）。`);
      }
      if (!response.headers.get('Content-Type')?.toLowerCase().includes('text/event-stream')) {
        void response.body?.cancel(); throw new ProtocolError('分析服务返回了不支持的响应类型。');
      }
      for await (const frame of readSSE(response, connection.signal)) {
        sawFrame = true;
        check();
        let event: Record<string, unknown>;
        try { event = record(JSON.parse(frame.data)); } catch { throw new ProtocolError('无法解析分析事件。'); }
        if (event.task_id !== undefined) identify(event.task_id);
        if (event.status === 'completed' && event.result) {
          const snapshot = record(record(event.result).data);
          identify(snapshot.task_id);
          return sqlResult(event.result, resumeIds, jobId, taskId, kind);
        }
        if (event.status === 'error') {
          if (event.result) {
            const snapshot = record(record(event.result).data);
            identify(snapshot.task_id);
            if (!TERMINAL.has(String(snapshot.status))) throw new ProtocolError('无效的分析任务状态。');
            throw new ProtocolError(failedTaskMessage(snapshot, kind));
          }
          throw new RetryableError('正在恢复分析任务连接。');
        }
        const id = frame.id ?? event.id;
        if (id !== undefined) {
          const text = String(id);
          if (!/^\d{1,3}$/.test(text) || Number(text) < 1 || Number(text) > 256 ||
              (frame.id !== undefined && event.id !== undefined && Number(frame.id) !== event.id) ||
              !taskId || !STAGES.has(String(event.status)) || !resumeIds.includes(String(event.resume_id))) {
            throw new ProtocolError('无效的分析事件。');
          }
          const next = Number(text);
          if (next <= cursor) continue;
          if (next !== cursor + 1) throw new ProtocolError('分析事件序号不连续。');
          if (candidate !== event.resume_id) {
            candidate = String(event.resume_id);
            onProgress?.('candidate', '', resumeIds.indexOf(candidate) + 1, resumeIds.length);
          }
          onProgress?.(String(event.status), typeof event.message === 'string' ? event.message : '');
          cursor = next;
          save();
          failures = 0;
        } else if (!['starting', 'processing'].includes(String(event.status))) {
          throw new ProtocolError('未知的分析事件。');
        }
      }
      if (!taskId || (!sawFrame && Date.now() - openedAt < 1000)) throw new RetryableError('分析连接提前结束。');
      failures = 0; // Clean 30-second subscription rollover is expected.
    } catch (error) {
      check();
      if (error instanceof ProtocolError) throw error;
      failures += 1;
      if (failures >= 3) throw new SqlAnalysisConnectionError('分析连接暂时无法恢复，可稍后恢复进度；后台任务不会因断线自动重跑。');
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      connection.abort();
    }
    check();
    onProgress?.('reconnecting', '正在恢复分析进度…');
    await pause(Math.min(500 * 2 ** failures, 4000), signal);
  }
  throw new SqlAnalysisConnectionError('等待分析完成超时，可稍后恢复进度；任务可能仍在后台运行。');
}
