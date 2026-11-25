'use client';

import Link from 'next/link';
import { ChangeEvent, DragEvent, useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import { useRouter } from 'next/navigation';
import {
  ArrowRightIcon,
  BanIcon,
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
import { setDraft, startAnalysisTask, resumeAnalysisTask, useAnalysisSession, type WorkbenchPhase } from '@/lib/analysis-session';
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
  const dropZoneRef = useRef<HTMLDivElement>(null);
  const receiptId = useRef(0);
  const { setAnalysisResult } = useAnalysis();
  const session = useAnalysisSession();
  const [isDragging, setIsDragging] = useState(false);
  const [fileErrors, setFileErrors] = useState<string[]>([]);
  const [receipt, setReceipt] = useState<{ id: number; name: string; count: number; x: number; y: number } | null>(null);
  useEffect(() => {
    if (!receipt) return;
    const timer = window.setTimeout(() => setReceipt(null), 1450);
    return () => window.clearTimeout(timer);
  }, [receipt]);
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
  const editingLocked = busy || session.recoverable;
  // 被禁用时不允许发起分析（按钮仍可点击，用于弹出「为什么不能分析」）
  const missingInputs = [
    ...(files.length === 0 ? ['请上传至少一份简历'] : []),
    ...(jobDescription.trim().length < 20 ? ['岗位描述至少 20 字'] : []),
  ];
  const canAnalyze = !busy && (session.recoverable || (missingInputs.length === 0 && !quotaDisabled));

  // 跳过挂载时已有的旧报告，仅在当前页面收到新结果时跳转。
  useEffect(() => {
    if (!session.result || session.result === hadResultOnMount.current) return;
    setAnalysisResult(session.result);
    router.push('/dashboard');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.result]);

  const addFiles = (incoming: File[], position?: { x: number; y: number }) => {
    if (editingLocked) return;
    setDraft({ error: '' });
    const rejected: string[] = [];
    const combined = [...files];
    const combinedObjects = [...session.fileObjects];
    for (const file of incoming) {
      const extension = file.name.split('.').pop()?.toLowerCase() || '';
      if (!ACCEPTED_EXTENSIONS.includes(extension)) {
        rejected.push(`${file.name}：仅支持 PDF 和 DOCX 格式。`);
        continue;
      }
      if (file.size === 0) {
        rejected.push(`${file.name}：文件为空，无法添加。`);
        continue;
      }
      if (file.size > MAX_FILE_SIZE) {
        rejected.push(`${file.name}：超过 30 MB，无法添加。`);
        continue;
      }
      if (combined.some((item) => item.name === file.name && item.size === file.size)) continue;
      if (combined.length >= MAX_FILES) {
        rejected.push(`${file.name}：一次最多分析 ${MAX_FILES} 份简历。`);
        continue;
      }
      combined.push({ name: file.name, size: file.size, status: 'pending' });
      combinedObjects.push(file);
    }
    setFileErrors(rejected);
    setDraft({ files: combined, fileObjects: combinedObjects });
    const accepted = combined.slice(files.length);
    const zone = dropZoneRef.current;
    if (accepted.length) {
      const bounds = zone?.getBoundingClientRect();
      const x = position && bounds ? position.x - bounds.left - bounds.width / 2 : 0;
      const y = position && bounds ? position.y - bounds.top - bounds.height / 2 : -72;
      setReceipt({ id: ++receiptId.current, name: accepted[0].name, count: accepted.length, x, y });
    } else if (rejected.length) {
      setReceipt(null);
      if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        zone?.animate(
          [0, -7, 6, -4, 2, 0].map(x => ({ transform: `translateX(${x}px)` })),
          { duration: 360, easing: 'ease-out' },
        );
      }
    }
  };

  const handleFileInput = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files || []));
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    if (!editingLocked) addFiles(Array.from(event.dataTransfer.files), { x: event.clientX, y: event.clientY });
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
      <div className="mx-auto w-full max-w-[1300px] px-5 py-8 sm:px-8 lg:px-12 lg:py-10">
        <header className="flex flex-col gap-6 border-b border-line pb-6 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-sub">01 / Screening</p>
            <h1 className="mt-4 text-[42px] font-medium leading-[1.06] tracking-[-0.028em] text-ink">新建筛选</h1>
            <p className="mt-3 max-w-[62ch] text-sm leading-6 text-sub">
              上传 1 至 3 份候选人简历与目标岗位描述，生成可追溯的分析报告。
            </p>
          </div>
          <div className="flex items-end gap-8">
            <div className="text-right">
              <p className="font-mono text-[9px] uppercase tracking-[0.16em] text-sub">今日额度</p>
              <p className="mt-2 text-[13px] text-ink">
                {!quota
                  ? '读取中'
                  : quotaDisabled
                    ? '已停用'
                    : quota.quota_enabled === false || quota.unlimited || quota.daily_limit == null
                      ? '不限次数'
                      : `${quota.used ?? 0} / ${quota.daily_limit} 次`}
              </p>
              {quota && !quota.unlimited && quota.quota_enabled !== false && quota.remaining !== undefined && (
                <p className={`mt-1 text-[10.5px] ${quota.remaining === 0 ? 'text-bad' : quota.remaining <= 2 ? 'text-warn' : 'text-sub'}`}>
                  剩余 {quota.remaining} 次
                </p>
              )}
            </div>
            <div className="text-right">
              <p className="font-mono text-[9px] uppercase tracking-[0.16em] text-sub">工作模式</p>
              <p className="mt-2 text-[13px] text-ink">内部招聘</p>
            </div>
          </div>
        </header>

        {quota && quotaDisabled && (
          <div role="alert" className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-2 border border-bad-border bg-bad-soft px-4 py-3">
            <span className="inline-flex items-center gap-2 text-[12.5px] font-medium text-bad">
              <BanIcon className="size-4" />
              当前账号的分析功能已被管理员禁用，无法发起分析。
            </span>
            <button
              type="button"
              onClick={() => openQuotaModal(disabledModalData())}
              className="border border-bad-border bg-white px-3 py-1.5 text-[11.5px] font-medium text-bad transition-colors hover:bg-bad-soft"
            >
              查看原因
            </button>
          </div>
        )}

        <div className="mt-6 flex flex-wrap items-stretch border-y border-line">
          {[
            ['01', '解析岗位门槛'],
            ['02', '比对履历证据'],
            ['03', '生成招聘建议'],
          ].map(([code, label], index) => (
            <div
              key={code}
              className={`flex flex-1 items-center gap-3 py-3.5 text-[12px] ${index === 0 ? 'font-medium text-ink' : 'text-sub'} ${
                index > 0 ? 'border-l border-line-soft pl-6' : ''
              }`}
            >
              <span className="font-mono text-[10px] tracking-[0.14em] text-sub">{code}</span>
              <span>{label}</span>
              {index < 2 && <span className="ml-auto pr-4 font-mono text-line-strong">→</span>}
            </div>
          ))}
          {busy && (
            <span className="flex items-center gap-2 py-3.5 pl-6 text-[11px] text-sub">
              <LoaderCircleIcon className="size-3.5 animate-spin" /> 分析进行中，切换页面不会中断任务
            </span>
          )}
        </div>

        <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)]">
          <section className="flex min-h-[560px] flex-col border border-line bg-white">
            <div className="flex items-center justify-between gap-4 border-b border-line-soft px-5 py-4">
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-[10px] tracking-[0.1em] text-sub">A</span>
                <h2 className="text-[14.5px] font-semibold text-ink">候选人简历</h2>
              </div>
              <span className="font-mono text-[10px] tracking-[0.06em] text-sub">
                {files.length} / {MAX_FILES} 份
              </span>
            </div>

            <div className="flex flex-1 flex-col p-5">
              <div
                ref={dropZoneRef}
                role="button"
                tabIndex={editingLocked ? -1 : 0}
                aria-disabled={editingLocked}
                aria-label="选择简历文件"
                aria-describedby="resume-upload-hint"
                onClick={() => !editingLocked && inputRef.current?.click()}
                onKeyDown={(event) => {
                  if (editingLocked || (event.key !== 'Enter' && event.key !== ' ')) return;
                  event.preventDefault();
                  inputRef.current?.click();
                }}
                onDragEnter={(event) => {
                  event.preventDefault();
                  if (!editingLocked && event.dataTransfer.types.includes('Files')) {
                    setReceipt(null);
                    setIsDragging(true);
                  }
                }}
                onDragOver={(event) => event.preventDefault()}
                onDragLeave={(event) => {
                  if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setIsDragging(false);
                }}
                onDrop={handleDrop}
                data-dragging={isDragging || undefined}
                data-receipt={!!receipt || undefined}
                className={`resume-dropzone relative isolate flex min-h-64 cursor-pointer flex-col items-center justify-center border border-dashed px-6 text-center transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink ${
                  isDragging ? 'border-ink bg-soft' : 'border-line-strong bg-mist hover:border-ink hover:bg-soft'
                } ${editingLocked ? 'cursor-default opacity-60' : ''}`}
              >
                <input ref={inputRef} type="file" accept=".pdf,.docx" multiple disabled={editingLocked} className="hidden" onChange={handleFileInput} />
                <div className="resume-dropzone-prompt flex w-full flex-col items-center">
                <span className="resume-dropzone-icon flex size-12 items-center justify-center border border-ink bg-white text-ink">
                  <UploadCloudIcon className="size-5" aria-hidden="true" />
                </span>
                <p className="mt-6 text-[13.5px] font-medium text-ink">{isDragging ? '松开即可添加简历' : '拖放简历到这里'}</p>
                <span className="mt-4 inline-block border border-control-border bg-control px-3 py-1.5 text-[12px] font-medium text-ink">
                  选择文件
                </span>
                <p id="resume-upload-hint" className="mt-4 font-mono text-[10.5px] uppercase tracking-[0.12em] text-sub">PDF / DOCX · 每份最大 30 MB</p>
                </div>
                {receipt && (
                  <div key={receipt.id} className="resume-dropzone-feedback" aria-hidden="true"
                    style={{ '--drop-x': `${receipt.x}px`, '--drop-y': `${receipt.y}px` } as CSSProperties}>
                    <span className="resume-dropzone-flight"><FileTextIcon className="size-7" /></span>
                    <span className="resume-dropzone-receipt">
                      <FileTextIcon className="size-5 shrink-0" />
                      <span className="min-w-0 flex-1 text-left">
                        <span className="block truncate text-sm font-medium">{receipt.name}</span>
                        <span className="block text-xs text-sub">已添加 {receipt.count} 份 · 开始分析后上传</span>
                      </span>
                      <CheckIcon className="size-5 shrink-0 text-good" />
                    </span>
                  </div>
                )}
              </div>
              <p role="status" className="sr-only">{receipt ? `已添加 ${receipt.count} 份简历` : ''}</p>

              {fileErrors.length > 0 && (
                <ul role="alert" className="mt-3 space-y-1 border border-bad-border bg-bad-soft px-3 py-2 text-xs text-bad">
                  {fileErrors.map((message, index) => <li key={index} className="break-words">{message}</li>)}
                </ul>
              )}
              <div className="mt-4 space-y-2" aria-live="polite">
                {files.length === 0 ? (
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border border-line-soft px-4 py-3 text-[11.5px] leading-5 text-sub">
                    <ShieldCheckIcon className="size-4 shrink-0 text-good" />
                    <span>文件仅用于招聘筛选与系统审计，请确认已取得候选人授权</span>
                    <Link href="/privacy" className="text-ink underline underline-offset-2">
                      《隐私政策》
                    </Link>
                  </div>
                ) : files.map((file, index) => (
                  <div key={`${file.name}-${file.size}`} className={`flex items-center gap-3 border px-3 py-2.5 ${file.status === 'failed' ? 'border-bad-border bg-bad-soft' : 'border-line-soft'}`}>
                    <span className={`flex size-8 shrink-0 items-center justify-center border ${file.status === 'failed' ? 'border-bad-border text-bad' : 'border-line-strong text-sub'}`}>
                      <FileTextIcon className="size-4" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[12.5px] font-medium text-ink">{file.name}</p>
                      <p className="mt-0.5 text-[10.5px] text-sub">
                        候选人 {index + 1} · {formatSize(file.size)}
                        {file.status === 'failed' ? ` · ${file.error}` : file.status === 'success' ? ' · 已上传' : ''}
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={editingLocked}
                      onClick={(event) => {
                        event.stopPropagation();
                        const nextFiles = files.filter((item) => !(item.name === file.name && item.size === file.size));
                        const nextObjects = session.fileObjects.filter((obj) => !(obj.name === file.name && obj.size === file.size));
                        setDraft({ files: nextFiles, fileObjects: nextObjects });
                      }}
                      className="relative -m-1 flex size-8 items-center justify-center p-1 text-sub transition-colors hover:bg-mist hover:text-ink disabled:opacity-40 after:absolute after:-inset-1 after:content-['']"
                      aria-label={`移除 ${file.name}`}
                    >
                      <XIcon className="size-4" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <section className="flex min-h-[560px] flex-col border border-line bg-white">
            <div className="flex items-center justify-between gap-4 border-b border-line-soft px-5 py-4">
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-[10px] tracking-[0.1em] text-sub">B</span>
                <h2 className="text-[14.5px] font-semibold text-ink">目标岗位描述</h2>
              </div>
              <span className="font-mono text-[10px] tracking-[0.06em] text-sub">必填</span>
            </div>

            <div className="flex flex-1 flex-col p-5">
              <label htmlFor="job-description" className="text-[11.5px] font-medium text-body">
                岗位职责与任职要求
              </label>
              <textarea
                id="job-description"
                value={jobDescription}
                onChange={(event) => setDraft({ jobDescription: event.target.value })}
                disabled={editingLocked}
                placeholder="粘贴完整 JD，包括岗位职责、经验年限、技能要求、学历与地点等信息..."
                className="mt-2 min-h-56 w-full resize-none border border-line-strong bg-white p-4 text-[13px] leading-7 text-ink outline-none transition focus:border-ink disabled:opacity-60"
              />
              <div className="mt-2 flex items-center justify-between font-mono text-[9.5px] tracking-[0.04em] text-sub">
                <span>{jobDescription.trim().length < 20 ? '至少输入 20 个字符' : '岗位信息已就绪'}</span>
                <span>{jobDescription.length} 字</span>
              </div>

              <div className="mt-auto border-t border-line-soft pt-4">
                {session.error && (
                  <div role="alert" className="mb-4 flex items-center justify-between gap-3 border border-bad-border bg-bad-soft px-4 py-3 text-[12.5px] text-bad">
                    <span>{session.error}</span>
                    <button type="button" onClick={() => setDraft({ error: '' })} className="shrink-0 font-medium underline underline-offset-2">
                      关闭
                    </button>
                  </div>
                )}
                {!busy && (
                  <label className="mb-4 flex cursor-pointer items-center justify-between gap-3 border border-line-soft px-3 py-2.5">
                    <span className="flex items-center gap-2 text-[11.5px] text-sub">
                      <GlobeIcon className="size-4 text-sub" />
                      <span>
                        联网核验公司信息
                        <span className="ml-1 hidden text-[10px] text-sub sm:inline">将检索公司公开信息用于交叉核验</span>
                      </span>
                    </span>
                    <input
                      type="checkbox"
                      checked={webSearch}
                      disabled={session.recoverable}
                      onChange={(event) => setDraft({ webSearch: event.target.checked })}
                      className="size-4 accent-check-accent"
                    />
                  </label>
                )}
                <button
                  type="button"
                  disabled={!canAnalyze && !quotaDisabled}
                  aria-describedby={!busy && !session.recoverable && missingInputs.length ? 'analysis-missing-inputs' : undefined}
                  onClick={session.recoverable ? resumeAnalysisTask : handleAnalyze}
                  className="flex h-11 w-full items-center justify-center gap-2 bg-ink px-5 text-[13px] font-medium text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:bg-disabled-bg disabled:text-disabled-fg"
                >
                  {busy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SparklesIcon className="size-4" />}
                  {session.recoverable ? '恢复分析进度' : phaseLabel}
                  {!busy && <ArrowRightIcon className="size-4" />}
                </button>
                {busy ? (
                  <AnalysisProgress phase={session.phase} progress={session.progress} candidateProgress={session.candidateProgress} />
                ) : !session.recoverable && missingInputs.length > 0 ? (
                  <p id="analysis-missing-inputs" className="mt-3 text-center text-xs text-sub">{missingInputs.join(' · ')}</p>
                ) : (
                  <p className="mt-3 flex items-center justify-center gap-2 text-[10.5px] text-sub">
                    <CheckIcon className="size-3.5 text-good" /> 分析将覆盖履历、技能、项目、稳定性与招聘风险
                  </p>
                )}
              </div>
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
