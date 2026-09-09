'use client';

import { ChangeEvent, DragEvent, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  ArrowRightIcon,
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

const MAX_FILES = 3;
const MAX_FILE_SIZE = 30 * 1024 * 1024;
const ACCEPTED_EXTENSIONS = ['pdf', 'docx'];

const formatSize = (size: number) => `${(size / 1024 / 1024).toFixed(size > 1024 * 1024 * 10 ? 0 : 1)} MB`;

export default function AnalysisWorkbench() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const { setAnalysisResult } = useAnalysis();
  const session = useAnalysisSession();
  const [isDragging, setIsDragging] = useState(false);
  // 记录挂载时是否已有旧结果：切回页面（此时 result 非空）不应再次强制跳转报告页，
  // 只有「挂载时无结果、之后任务在本进程内完成」才自动跳转。
  const hadResultOnMount = useRef(session.result);

  const files = session.files;
  const { jobDescription, webSearch } = session;
  const busy = session.running;
  const canAnalyze = files.length > 0 && jobDescription.trim().length >= 20 && !busy;

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
    if (!canAnalyze) return;
    startAnalysisTask({ files, fileObjects: session.fileObjects, jobDescription, webSearch });
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
    <AppShell active="home">
      <div className="mx-auto w-full max-w-[1480px] px-5 py-8 sm:px-8 lg:px-10 lg:py-10 xl:px-14">
        <header className="border-b border-line pb-6">
          <h1 className="text-2xl font-semibold text-ink sm:text-3xl">AI 简历智选 · 全维度量化人才评估</h1>
          <p className="mt-2 text-sm leading-6 text-sub">上传简历并粘贴岗位描述，生成可直接用于招聘决策的标准化分析报告</p>
        </header>

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
                disabled={!canAnalyze}
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