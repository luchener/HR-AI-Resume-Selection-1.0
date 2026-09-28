'use client';

import { ChangeEvent, DragEvent, useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  ArrowRightIcon,
  BanIcon,
  BriefcaseBusinessIcon,
  CheckIcon,
  FileTextIcon,
  GlobeIcon,
  LoaderCircleIcon,
  ShieldCheckIcon,
  SparklesIcon,
  UploadCloudIcon,
  XIcon,
} from 'lucide-react';
import AppShell from './app-shell';
import { useAnalysis } from './analysis-context';
import { setDraft, startAnalysisTask, useAnalysisSession, type WorkbenchPhase } from '@/lib/analysis-session';
import { fetchMyQuota, type MyQuota } from '@/lib/api/quota';
import { QuotaModal, type QuotaModalData } from './quota-modal';

const MAX_FILES = 3;
const MAX_FILE_SIZE = 30 * 1024 * 1024;
const ACCEPTED_EXTENSIONS = ['pdf', 'docx'];

const formatSize = (size: number) => `${(size / 1024 / 1024).toFixed(size > 1024 * 1024 * 10 ? 0 : 1)} MB`;

/** 配额按 UTC 自然日（与后端 record_user_usage 同口径） */
const quotaTodayKey = () => new Date().toISOString().slice(0, 10);

export default function AnalysisWorkbench() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const { setAnalysisResult } = useAnalysis();
  const session = useAnalysisSession();
  const [isDragging, setIsDragging] = useState(false);
  // 记录挂载时是否已有旧结果：切回页面（此时 result 非空）不应再次强制跳转报告页，
  // 只有「挂载时无结果、之后任务在本进程内完成」才自动跳转。
  const hadResultOnMount = useRef(session.result);

  // ── 使用次数配额 ────────────────────────────────────────────
  const [quota, setQuota] = useState<MyQuota | null>(null);
  const [quotaModal, setQuotaModal] = useState<QuotaModalData | null>(null);
  // 会话内去重：同一天同一类型弹窗只弹一次（用户主动点开不受限制）
  const modalShownRef = useRef<Set<string>>(new Set());

  /** 账号被管理员禁用分析：前端必须与后端拦截口径一致（403）。 */
  const quotaDisabled = quota !== null && quota.analysis_enabled === false;

  const openQuotaModal = useCallback((data: QuotaModalData, once = false) => {
    const key = data.variant + ':' + quotaTodayKey();
    if (once && modalShownRef.current.has(key)) return;
    modalShownRef.current.add(key);
    setQuotaModal(data);
  }, []);

  /** 被禁用时的弹窗内容（写明原因 + 处理方式，避免「点了没反应」）。 */
  const disabledModalData = useCallback((): QuotaModalData => ({
    variant: 'disabled',
    title: '分析功能已被禁用',
    detail: '该账号的分析功能已被管理员关闭，请联系管理员重新开启后再使用。',
  }), []);

  const refreshQuota = useCallback(async () => {
    try {
      const q = await fetchMyQuota();
      setQuota(q);
      if (q && q.analysis_enabled === false) {
        // ① 被禁用：进入页面即弹一次，并写明原因
        openQuotaModal(disabledModalData(), true);
      } else if (q && q.unlimited === false && q.remaining === 0) {
        // ② 今日已用完：进入页面即弹一次
        openQuotaModal({
          variant: 'exhausted',
          title: '今日分析次数已用完',
          limit: q.daily_limit,
          used: q.used,
          resets_at: q.resets_at,
          detail: '如需更多次数，请联系管理员调整配额。',
        }, true);
      }
    } catch { /* 配额读取失败不阻塞主流程 */ }
  }, [openQuotaModal, disabledModalData]);

  useEffect(() => { void refreshQuota(); }, [refreshQuota]);

  // 页面重新获得焦点时刷新（管理员改完配额后用户无需重登）
  useEffect(() => {
    const onFocus = () => void refreshQuota();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [refreshQuota]);

  // 分析失败（含后端 403/429）→ 同步成弹窗，让用户看到明确原因
  useEffect(() => {
    const message = session.error;
    if (!message) return;
    if (message.includes('已被禁用')) openQuotaModal(disabledModalData(), true);
    else if (message.includes('已用完')) openQuotaModal({ variant: 'exhausted', title: '今日分析次数已用完', detail: message }, true);
    else if (message.includes('本次需要') || message.includes('剩余')) openQuotaModal({ variant: 'insufficient', title: '本次分析次数不足', detail: message }, true);
  }, [session.error, openQuotaModal, disabledModalData]);

  const files = session.files;
  const { jobDescription, webSearch } = session;
  const busy = session.running;
  // 被禁用时不允许发起分析（按钮仍可点击，用于弹出「为什么不能分析」）
  const canAnalyze = files.length > 0 && jobDescription.trim().length >= 20 && !busy && !quotaDisabled;

  // 任务完成后跳转分析页。仅在「挂载时无结果、之后任务在本进程内完成」时触发：
  // 从报告页切回工作台时挂载 snapshot 已有旧结果（hadResultOnMount 非空），不再弹回报告页。
  useEffect(() => {
    if (!session.result || hadResultOnMount.current) return;
    setAnalysisResult(session.result);
    router.push('/dashboard');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.result]);

  const addFiles = (incoming: File[]) => {
    setDraft({ error: '' });
    const combined = [...files];
    const combinedObjects = [...session.fileObjects];
    for (const file of incoming) {
      const extension = file.name.split('.').pop()?.toLowerCase() || '';
      if (!ACCEPTED_EXTENSIONS.includes(extension)) {
        setDraft({ error: '仅支持 PDF 和 DOCX 格式。' });
        continue;
      }
      if (file.size > MAX_FILE_SIZE) {
        setDraft({ error: `${file.name} 超过 30 MB，无法添加。` });
        continue;
      }
      if (combined.some((item) => item.name === file.name && item.size === file.size)) continue;
      if (combined.length >= MAX_FILES) {
        setDraft({ error: '一次最多分析 3 份简历。' });
        break;
      }
      combined.push({ name: file.name, size: file.size, status: 'pending' });
      combinedObjects.push(file);
    }
    setDraft({ files: combined, fileObjects: combinedObjects });
  };

  const handleFileInput = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files || []));
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    if (!busy) addFiles(Array.from(event.dataTransfer.files));
  };
  const handleAnalyze = () => {
    if (busy) return;
    // 配额前置校验（与后端口径一致；后端仍会二次强校验）
    if (quota) {
      if (quota.analysis_enabled === false) {
        setDraft({ error: '该账号的分析功能已被禁用，请联系管理员。' });
        openQuotaModal(disabledModalData());
        return;
      }
      if (quota.unlimited === false && quota.remaining !== undefined) {
        if (quota.remaining <= 0) {
          openQuotaModal({ variant: 'exhausted', title: '今日分析次数已用完', limit: quota.daily_limit, used: quota.used, resets_at: quota.resets_at, detail: '如需更多次数，请联系管理员调整配额。' });
          return;
        }
        if (files.length > quota.remaining) {
          openQuotaModal({ variant: 'insufficient', title: '本次分析次数不足', need: files.length, remaining: quota.remaining, limit: quota.daily_limit, used: quota.used, resets_at: quota.resets_at, detail: '请减少所选简历份数，或明天 0 点重置后再试。' });
          return;
        }
      }
    }
    if (!canAnalyze) return;
    startAnalysisTask({ files, fileObjects: session.fileObjects, jobDescription, webSearch });
    // 分析结束后刷新配额（成功则会 +1 次已用）
    window.setTimeout(() => void refreshQuota(), 1500);
  };
  const phaseLabel =
    session.phase === 'uploading'
      ? '正在读取简历'
      : session.phase === 'job'
        ? '正在解析岗位要求'
        : session.phase === 'analyzing'
          ? `正在分析 ${Math.max(1, session.candidateCount)} 位候选人`
          : '开始分析';

  return (
    <>
      <AppShell active="home">
      <div className="mx-auto w-full max-w-[1480px] px-5 py-8 sm:px-8 lg:px-10 lg:py-10 xl:px-14">
        <header className="border-b border-line pb-6">
          <h1 className="text-2xl font-semibold text-ink sm:text-3xl">AI 简历智选 · 全维度量化人才评估</h1>
          <p className="mt-2 text-sm leading-6 text-sub">上传简历并粘贴岗位描述，生成可直接用于招聘决策的标准化分析报告</p>
        </header>

        {/* 使用次数配额状态条 */}
        {quota && quotaDisabled && (
          <div role="alert" className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-bad-border bg-bad-soft px-5 py-3">
            <span className="inline-flex items-center gap-2 text-sm font-medium text-bad">
              <BanIcon className="size-4" />
              当前账号的分析功能已被管理员禁用，无法发起分析。
            </span>
            <button
              type="button"
              onClick={() => openQuotaModal(disabledModalData())}
              className="rounded-md border border-bad-border bg-white px-3 py-1.5 text-xs font-medium text-bad transition-colors hover:bg-bad-soft"
            >
              查看原因
            </button>
          </div>
        )}
        {quota && !quotaDisabled && quota.quota_enabled !== false && (
          <div className="mt-5 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-md border border-line bg-white px-5 py-3">
            <span className="text-xs font-semibold text-sub">今日额度</span>
            {quota.unlimited ? (
              <span className="inline-flex items-center gap-1.5 text-sm text-body">不限次数</span>
            ) : (
              <span className="inline-flex items-center gap-1.5 text-sm text-body">
                <span className="font-semibold text-ink">{quota.used}</span>
                <span className="text-sub">/</span>
                <span className="text-ink">{quota.daily_limit}</span>
                <span className="text-sub">次</span>
              </span>
            )}
            <span className="text-xs text-sub">{quota.unlimited ? '管理员不受额度限制' : (quota.remaining !== undefined && quota.remaining <= 2 ? '今日剩余不多，请合理规划' : '每天 0 点重置' )}</span>
            {quota.remaining !== undefined && quota.unlimited === false && (
              <span className={"ml-auto inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs " + (quota.remaining === 0 ? 'border-bad-border bg-bad-soft text-bad' : quota.remaining <= 2 ? 'border-warn-border bg-warn-soft text-warn' : 'border-good-border bg-good-soft text-good-deep')}>
                剩余 {quota.remaining} 次
              </span>
            )}
          </div>
        )}
        <div className="mt-5 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-md border border-line bg-white px-5 py-3">
          <span className="text-xs font-semibold text-sub">分析流程</span>
          {['解析硬性门槛', '比对履历证据', '生成招聘建议'].map((label, index) => (
            <span key={label} className="flex items-center gap-2">
              <span className="flex size-5 items-center justify-center rounded-full bg-brand-soft text-[10px] font-semibold text-brand">{index + 1}</span>
              <span className="text-xs text-body">{label}</span>
            </span>
          ))}
          {busy && (
            <span className="ml-auto flex items-center gap-2 text-xs font-medium text-brand">
              <LoaderCircleIcon className="size-3.5 animate-spin" /> 分析进行中，切换页面不会中断任务
            </span>
          )}
        </div>

        <div className="mt-5 grid gap-6 xl:grid-cols-[minmax(340px,0.82fr)_minmax(500px,1.18fr)]">
          <section className="flex min-h-[550px] flex-col rounded-md border border-line bg-white p-5 sm:p-7">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-semibold uppercase text-brand">01 · Resume files</p>
                <h2 className="mt-2 text-xl font-semibold text-ink">添加候选人简历</h2>
              </div>
              <span className="rounded-full bg-brand-soft px-3 py-1 text-xs font-medium text-brand-deep">{files.length}/{MAX_FILES}</span>
            </div>

            <div
              role="button"
              tabIndex={busy ? -1 : 0}
              onClick={() => !busy && inputRef.current?.click()}
              onKeyDown={(event) => {
                if (!busy && (event.key === 'Enter' || event.key === ' ')) inputRef.current?.click();
              }}
              onDragEnter={(event) => { event.preventDefault(); if (!busy) setIsDragging(true); }}
              onDragOver={(event) => event.preventDefault()}
              onDragLeave={() => setIsDragging(false)}
              onDrop={handleDrop}
              className={`mt-6 flex min-h-64 cursor-pointer flex-col items-center justify-center rounded-md border border-dashed px-6 text-center transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand ${isDragging ? 'border-brand bg-brand-soft' : 'border-line bg-mist hover:border-brand hover:bg-soft'} ${busy ? 'pointer-events-none opacity-60' : ''}`}
            >
              <input ref={inputRef} type="file" accept=".pdf,.docx" multiple className="hidden" onChange={handleFileInput} />
              <span className="flex size-14 items-center justify-center rounded-md bg-brand-deep text-white">
                <UploadCloudIcon className="size-6" aria-hidden="true" />
              </span>
              <p className="mt-7 text-lg font-semibold text-ink">拖放简历到这里</p>
              <p className="mt-2 text-sm text-sub">PDF / DOCX · 每份最大 30 MB</p>
              <button type="button" className="mt-5 rounded-md border border-line-soft bg-white px-4 py-2 text-sm font-medium text-ink">选择文件</button>
            </div>

            <div className="mt-5 space-y-2" aria-live="polite">
              {files.length === 0 ? (
                <div className="flex items-center gap-3 rounded-md border border-line-soft px-4 py-3 text-sm text-sub">
                  <ShieldCheckIcon className="size-4 text-good" /> 文件仅用于本次招聘分析
                </div>
              ) : files.map((file, index) => (
                <div key={`${file.name}-${file.size}`} className={`flex items-center gap-3 rounded-md border px-3 py-3 ${file.status === 'failed' ? 'border-bad-border bg-bad-soft' : 'border-line-soft'}`}>
                  <span className={`flex size-9 shrink-0 items-center justify-center rounded-md ${file.status === 'failed' ? 'bg-bad-soft text-bad' : 'bg-brand-soft text-brand'}`}><FileTextIcon className="size-4" /></span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-ink">{file.name}</p>
                    <p className="mt-0.5 text-xs text-sub">候选人 {index + 1} · {formatSize(file.size)}{file.status === 'failed' ? ` · ${file.error}` : file.status === 'success' ? ' · 已上传' : ''}</p>
                  </div>
                  <button type="button" disabled={busy} onClick={(event) => {
                    event.stopPropagation();
                    const nextFiles = files.filter((item) => !(item.name === file.name && item.size === file.size));
                    const nextObjects = session.fileObjects.filter((obj) => !(obj.name === file.name && obj.size === file.size));
                    setDraft({ files: nextFiles, fileObjects: nextObjects });
                  }} className="relative -m-1 flex size-8 items-center justify-center rounded-md p-1 text-sub transition-colors hover:bg-mist hover:text-ink disabled:opacity-40 after:absolute after:-inset-1 after:content-['']" aria-label={`移除 ${file.name}`}>
                    <XIcon className="size-4" />
                  </button>
                </div>
              ))}
            </div>
          </section>

          <section className="flex min-h-[550px] flex-col rounded-md border border-line bg-white p-5 sm:p-7">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-semibold uppercase text-brand">02 · Job description</p>
                <h2 className="mt-2 text-xl font-semibold text-ink">输入目标岗位描述</h2>
              </div>
              <BriefcaseBusinessIcon className="size-5 text-sub" />
            </div>

            <label htmlFor="job-description" className="mt-6 text-sm font-medium text-ink">岗位职责与任职要求</label>
            <textarea
              id="job-description"
              value={jobDescription}
              onChange={(event) => setDraft({ jobDescription: event.target.value })}
              disabled={busy}
              placeholder="粘贴完整 JD，包括岗位职责、经验年限、技能要求、学历与地点等信息..."
              className="mt-2 min-h-56 w-full resize-y rounded-md border border-line bg-mist p-4 text-sm leading-6 text-ink outline-none transition focus:border-brand focus:ring-2 focus:ring-brand disabled:opacity-60"
            />
            <div className="mt-2 flex items-center justify-between text-xs text-sub">
              <span>{jobDescription.trim().length < 20 ? '至少输入 20 个字符' : '岗位信息已就绪'}</span>
              <span>{jobDescription.length} 字</span>
            </div>

            <div className="mt-auto border-t border-line-soft pt-5">
              {session.error && (
                <div role="alert" className="mb-4 flex items-center justify-between gap-3 rounded-md border border-bad-border bg-bad-soft px-4 py-3 text-sm text-bad">
                  <span>{session.error}</span>
                  <button type="button" onClick={() => setDraft({ error: '' })} className="shrink-0 font-medium underline underline-offset-2">关闭</button>
                </div>
              )}
              {!busy && (
                <label className="mb-4 flex cursor-pointer items-center justify-between gap-3 rounded-md border border-line-soft px-3 py-2.5">
                  <span className="flex items-center gap-2 text-xs text-sub">
                    <GlobeIcon className="size-4 text-brand" />
                    <span>
                      联网核验公司信息
                      <span className="ml-1 hidden text-[10px] text-sub sm:inline">将检索公司公开信息用于交叉核验</span>
                    </span>
                  </span>
                  <input
                    type="checkbox"
                    checked={webSearch}
                    onChange={(event) => setDraft({ webSearch: event.target.checked })}
                    className="size-4 accent-check-accent"
                  />
                </label>
              )}
              <button
                type="button"
                disabled={!canAnalyze && !quotaDisabled}
                onClick={handleAnalyze}
                className="flex h-12 w-full items-center justify-center gap-2 rounded-md bg-brand-deep px-5 text-sm font-semibold text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:bg-disabled-bg disabled:text-disabled-fg"
              >
                {busy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SparklesIcon className="size-4" />}
                {phaseLabel}
                {!busy && <ArrowRightIcon className="size-4" />}
              </button>
              {busy ? (
                <AnalysisProgress phase={session.phase} progress={session.progress} candidateProgress={session.candidateProgress} />
              ) : (
                <p className="mt-3 flex items-center justify-center gap-2 text-xs text-sub">
                  <CheckIcon className="size-3.5 text-good" /> 分析将覆盖履历、技能、项目、稳定性与招聘风险
                </p>
              )}
            </div>
          </section>
        </div>
      </div>
    </AppShell>

      <QuotaModal open={Boolean(quotaModal)} data={quotaModal} onClose={() => setQuotaModal(null)} onRefresh={() => { setQuotaModal(null); void refreshQuota(); }} />
    </>
  );
}

/* 分析进度指示器：直接消费 store 快照，跨页面一致（P3-2 + 切页不中断） */
function AnalysisProgress({ phase, progress, candidateProgress }: { phase: WorkbenchPhase; progress: string; candidateProgress: string }) {
  const currentIndex = phase === 'uploading' ? 0 : phase === 'job' ? 1 : 2;
  const steps = ['读取简历', '解析岗位', '生成分析'] as const;

  return (
    <>
      <ol className="mt-4 flex items-center justify-between gap-2" aria-live="polite">
        {steps.map((label, index) => {
          const done = index < currentIndex;
          const active = index === currentIndex;
          return (
            <li key={label} className="flex min-w-0 flex-1 items-center gap-2">
              <span className={`flex size-6 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold ${done ? 'bg-good-soft text-good-deep' : active ? 'bg-brand-soft text-brand-deep' : 'bg-mist text-sub'}`}>
                {done ? <CheckIcon className="size-3" /> : active ? <LoaderCircleIcon className="size-3 animate-spin" /> : index + 1}
              </span>
              <span className={`truncate text-xs ${active ? 'font-medium text-ink' : 'text-sub'}`}>{label}</span>
            </li>
          );
        })}
      </ol>
      {(progress || candidateProgress) && (
        <p className="mt-3 flex items-center justify-center gap-2 text-center text-xs text-brand" aria-live="polite">
          {candidateProgress && <span className="font-medium">{candidateProgress}</span>}
          {progress && <span className="truncate">{progress}</span>}
        </p>
      )}
    </>
  );
}