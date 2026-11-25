'use client';

// 分析会话模块级 store：让分析任务的生命周期独立于页面组件。
// 用户在分析进行中切换到其他页面时，组件卸载但 store 继续持有任务状态、进度与文件引用；
// 回到工作台或分析页时从 store 恢复，任务不会因页面切换而中断。

import { useSyncExternalStore } from 'react';
import type { AnalysisResult } from '@/components/workbench/analysis-context';
import { analyzeResumesStream, uploadJobDescription, uploadResume, resetSqlImprovementRequests, SqlAnalysisConnectionError, type SqlAnalysisCheckpoint } from '@/lib/api/screening';
import { getStoredToken } from '@/components/workbench/auth-context';
import { SQL_ANALYSIS_ENABLED } from '@/lib/api/config';

export type WorkbenchPhase = 'idle' | 'uploading' | 'job' | 'analyzing';

export type FileStatus = 'pending' | 'uploading' | 'success' | 'failed';

export interface StoredFileState {
  name: string;
  size: number;
  status: FileStatus;
  error?: string;
  resumeId?: string;
}

export interface AnalysisSessionState {
  phase: WorkbenchPhase;
  /** 文件状态快照（渲染用）。 */
  files: StoredFileState[];
  /** 真实 File 引用（上传用；仅存内存，页面切换不丢，不做 React 渲染）。 */
  fileObjects: File[];
  jobDescription: string;
  webSearch: boolean;
  error: string;
  /** 进度消息（SSE 过程中实时更新）。 */
  progress: string;
  candidateProgress: string;
  candidateCount: number;
  /** 存在进行中的任务（组件卸载后依然为 true，直到任务结束）。 */
  running: boolean;
  /** 任务完成后的结果。 */
  result: AnalysisResult | null;
  recoverable: boolean;
}

const initialState: AnalysisSessionState = {
  phase: 'idle',
  files: [],
  fileObjects: [],
  jobDescription: '',
  webSearch: false,
  error: '',
  progress: '',
  candidateProgress: '',
  candidateCount: 0,
  running: false,
  result: null,
  recoverable: false,
};

let state: AnalysisSessionState = initialState;
let generation = 0;
let controller: AbortController | null = null;
let pendingSql: { resumeIds: string[]; jobId: string; checkpoint: SqlAnalysisCheckpoint; token: string; webSearch: boolean } | null = null;
const listeners = new Set<() => void>();

/** 任务完成时的全局回调（由 AnalysisProvider 注册，把结果写入 context + sessionStorage，
 *  保证任务在后台完成时任何页面都能拿到结果）。 */
let completionHandler: ((result: AnalysisResult | null) => void) | null = null;

export function setCompletionHandler(handler: ((result: AnalysisResult | null) => void) | null): void {
  completionHandler = handler;
}

function setState(patch: Partial<AnalysisSessionState>): void {
  state = { ...state, ...patch };
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** 供 useSyncExternalStore 读取的不可变快照。 */
function getSnapshot(): AnalysisSessionState {
  return state;
}

/** SSR / hydration 快照：服务端永远渲染初始空状态，客户端交互后才读取真实 store，
 *  避免模块级 store（跨请求共享）把其他用户的任务状态泄漏到服务端渲染输出。 */
function getServerSnapshot(): AnalysisSessionState {
  return initialState;
}

/** 清空会话（含草稿与任务）。 */
export function resetSession(): void {
  generation += 1;
  resetSqlImprovementRequests();
  controller?.abort();
  controller = null;
  pendingSql = null;
  setState({ ...initialState });
  completionHandler?.(null);
}

interface Run { id: number; token: string; control: AbortController }
function beginRun(): Run {
  controller?.abort();
  controller = new AbortController();
  return { id: ++generation, token: getStoredToken(), control: controller };
}
function update(run: Run, patch: Partial<AnalysisSessionState>): void {
  if (run.id !== generation || run.control.signal.aborted || getStoredToken() !== run.token) {
    throw new DOMException('分析会话已停止。', 'AbortError');
  }
  setState(patch);
}
function failed(run: Run, caught: unknown): void {
  if (run.id !== generation) return;
  if (getStoredToken() !== run.token || run.control.signal.aborted) { resetSession(); return; }
  const recoverable = caught instanceof SqlAnalysisConnectionError && pendingSql !== null;
  if (!recoverable) pendingSql = null;
  const message = caught instanceof Error ? caught.message : '';
  setState({ phase: 'idle', running: false, recoverable,
    error: message === 'Failed to fetch' ? '无法连接分析服务，请确认服务可用后重试。' : message || '分析未完成，请稍后重试。' });
}
async function analyze(run: Run, resumeIds: string[], jobId: string, webSearch: boolean, checkpoint?: SqlAnalysisCheckpoint): Promise<void> {
  update(run, { phase: 'analyzing', running: true, recoverable: false, error: '' });
  const result = await analyzeResumesStream(resumeIds, jobId, { web_search: webSearch },
    (status, message, index, total) => update(run, status === 'candidate' ? {
      candidateProgress: (total ?? 0) > 1 ? `正在分析候选人 ${index ?? ''}/${total ?? ''}` : '', progress: '',
    } : { progress: message }), run.control.signal,
    SQL_ANALYSIS_ENABLED ? { checkpoint, onCheckpoint: saved => {
      if (run.id !== generation || run.control.signal.aborted || getStoredToken() !== run.token) {
        throw new DOMException('分析会话已停止。', 'AbortError');
      }
      pendingSql = { resumeIds: [...resumeIds], jobId, checkpoint: saved, token: run.token, webSearch };
    } } : undefined);
  update(run, { phase: 'idle', result, running: false, recoverable: false, progress: '', candidateProgress: '' });
  pendingSql = null;
  completionHandler?.(result);
}

/** Resume the same durable task/request without re-uploading files or a new key. */
export function resumeAnalysisTask(): void {
  if (state.running || !state.recoverable || !pendingSql) return;
  const saved = pendingSql;
  if (saved.token !== getStoredToken()) { resetSession(); return; }
  const run = beginRun();
  void analyze(run, saved.resumeIds, saved.jobId, saved.webSearch, saved.checkpoint).catch(error => failed(run, error));
}

/** 由工作台在编辑草稿时写入（文件/真实 File 引用/JD/联网选项），切页回来可恢复。 */
export function setDraft(patch: {
  files?: StoredFileState[];
  fileObjects?: File[];
  jobDescription?: string;
  webSearch?: boolean;
  error?: string;
}): void {
  setState(patch);
}

/**
 * 启动分析任务。任务本体挂在模块级作用域，组件卸载不影响其继续执行。
 * 完成后自动写入 state.result；调用方通过订阅感知并跳转。
 */
export function startAnalysisTask(input: {
  files: StoredFileState[];
  fileObjects: File[];
  jobDescription: string;
  webSearch: boolean;
}): void {
  if (state.running) return; // 已有一个任务在跑，忽略重复启动
  if (state.recoverable && pendingSql) { resumeAnalysisTask(); return; }
  const run = beginRun();
  pendingSql = null;
  setState({
    phase: 'uploading',
    files: input.files,
    fileObjects: input.fileObjects,
    jobDescription: input.jobDescription,
    webSearch: input.webSearch,
    error: '',
    running: true,
    candidateCount: input.files.length,
    progress: '',
    candidateProgress: '',
    result: null,
    recoverable: false,
  });

  void (async () => {
    try {
      // 1. 上传简历（fileObjects 与 files 按顺序一一对应）
      const uploaded = await Promise.allSettled(
        input.files.map((item, index) => uploadResume(input.fileObjects[index], run.control.signal)),
      );
      const successfulIds: string[] = [];
      const nextFiles = input.files.map((item, index) => {
        const outcome = uploaded[index];
        if (outcome.status === 'fulfilled') {
          successfulIds.push(outcome.value);
          return { ...item, status: 'success' as const, resumeId: outcome.value };
        }
        return { ...item, status: 'failed' as const, error: outcome.reason instanceof Error ? outcome.reason.message : '上传或解析失败。' };
      });
      update(run, { files: nextFiles });
      if (successfulIds.length === 0) throw new Error('所有简历上传失败，请检查文件后重试。');

      // 2. 上传 JD
      update(run, { phase: 'job', progress: '正在解析岗位要求' });
      const jobId = await uploadJobDescription(input.jobDescription.trim(), successfulIds[0], run.control.signal);

      // 3. 流式分析
      await analyze(run, successfulIds, jobId, input.webSearch);
    } catch (caught) {
      failed(run, caught);
    }
  })();
}

/** 用于 useSyncExternalStore 的 React hook。 */
export function useAnalysisSession(): AnalysisSessionState {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
