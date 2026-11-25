'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  ArrowUpIcon,
  BarChart3Icon,
  BriefcaseBusinessIcon,
  CalendarDaysIcon,
  CheckCircle2Icon,
  ClipboardCheckIcon,
  Clock3Icon,
  FileSearch2Icon,
  GraduationCapIcon,
  LoaderCircleIcon,
  PencilIcon,
  RefreshCwIcon,
  ShieldAlertIcon,
  ShieldCheckIcon,
  SparklesIcon,
  TargetIcon,
  TrophyIcon,
  ArrowRightIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  DownloadIcon,
} from 'lucide-react';
import AppShell from '@/components/workbench/app-shell';
import ArchiveMotionControl from '@/components/workbench/archive-motion-control';
import { useAnalysis, type EmploymentRecord, type HrAnalysis } from '@/components/workbench/analysis-context';
import { RequirementsChecklist } from '@/components/workbench/requirements-checklist';
import { CredibilityPanel } from '@/components/workbench/credibility-panel';
import { agentValidationNeedsReview, agentValidationStatus } from '@/components/workbench/agent-validation-status';
import { evidenceDecisionPending, evidenceRecommendation, evidenceScoreNotice, evidenceScoreProvisional, isResumeJdEvidenceMatch, reportCoreVerdict } from '@/components/workbench/evidence-score-status';
import ReportExportCenter, { AGENT_RULE_LABELS, type ExportFormat, type ReportExportHandle } from '@/components/workbench/report-export';
import ReportCopier from '@/components/workbench/report-copier';
import { analyzeResumes, fetchImprovedMarkdown, improveResumeStream } from '@/lib/api/screening';
import { createArchive, updateArchiveCategory } from '@/lib/api/archives';

type Action = 'reanalyze' | 'improve' | 'editor' | null;

const EXPORT_TIMEOUT_MS = 60_000;

const EMPTY_VALUE = '简历未提供';

function DetailGrid({
  values,
  compact = false,
}: {
  values: Array<[string, string | undefined]>;
  compact?: boolean;
}) {
  return (
    <dl className={`mt-5 grid border-l border-t border-line-soft ${compact ? 'grid-cols-2 xl:grid-cols-4' : 'sm:grid-cols-2 xl:grid-cols-3'}`}>
      {values.map(([label, value]) => (
        <div key={label} className="min-w-0 border-b border-r border-line-soft px-4 py-3.5">
          <dt className="text-xs text-sub">{label}</dt>
          <dd className="mt-1 break-words text-sm font-medium leading-6 text-ink">{value || EMPTY_VALUE}</dd>
        </div>
      ))}
    </dl>
  );
}

function EmploymentGapSummary({ value }: { value?: string }) {
  return (
    <div className="mt-5 flex items-start gap-3 border-y border-line-soft bg-mist px-4 py-3.5">
      <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-brand-soft text-brand">
        <Clock3Icon className="size-4" />
      </span>
      <div className="min-w-0">
        <p className="text-xs font-medium text-sub">空窗期核算</p>
        <p className="mt-1 break-words text-sm font-medium leading-6 text-ink">{value || EMPTY_VALUE}</p>
      </div>
    </div>
  );
}

function EmploymentOverlapWarning({ items }: { items?: Array<{ message: string }> }) {
  if (!items?.length) return null;
  return <div className="mt-5 rounded-md border border-warn-border bg-warn-soft px-4 py-3 text-sm text-warn"><p className="flex items-center gap-1.5 font-semibold"><AlertTriangleIcon className="size-4" /> 发现工作时间冲突</p><ul className="mt-2 space-y-1">{items.map((item, index) => <li key={`${item.message}-${index}`} className="flex items-start gap-1.5"><AlertTriangleIcon className="mt-1 size-3.5 shrink-0" />{item.message}</li>)}</ul></div>;
}

function EmploymentTimeline({ records }: { records?: EmploymentRecord[] }) {
  const rows = records || [];
  return (
    <div className="mt-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <h3 className="text-sm font-semibold text-ink">工作经历明细</h3>
        {rows.length > 0 && <p className="text-xs text-sub">{rows.length} 段经历 · 按开始时间倒序</p>}
      </div>
      <ol className="mt-3 divide-y divide-line-soft border-y border-line-soft">
        {rows.length ? rows.map((record, index) => (
          <li
            key={`${record.company_name}-${record.start_date}-${index}`}
            className="grid min-w-0 gap-3 py-4 sm:grid-cols-[minmax(0,1fr)_minmax(210px,auto)] sm:items-center"
          >
            <div className="min-w-0">
              <p className="break-words text-sm font-semibold leading-6 text-ink">{record.company_name || EMPTY_VALUE}</p>
              <p className="mt-0.5 break-words text-sm leading-6 text-sub">{record.job_title || EMPTY_VALUE}</p>
            </div>
            <div className="min-w-0 text-sm text-body sm:text-right">
              <p className="inline-flex max-w-full items-center gap-2 leading-6">
                <CalendarDaysIcon className="size-4 shrink-0 text-brand" />
                <span className="break-words">{record.start_date || EMPTY_VALUE} 至 {record.end_date || EMPTY_VALUE}</span>
              </p>
              {record.duration && record.duration !== '未提供' && (
                <p className="mt-0.5 text-xs text-sub">任职 {record.duration}</p>
              )}
            </div>
          </li>
        )) : (
          <li className="py-4 text-sm leading-6 text-sub">{EMPTY_VALUE}</li>
        )}
      </ol>
    </div>
  );
}

const INSIGHT_PREFIX = /^(原文支持|部分要求有原文支持)[：:]\s*/;

/** 旧报告把状态和引用拼在一句话里；这里拆成「状态徽章 + 结论 + 原文引用」。 */
function splitInsight(raw: string) {
  const match = raw.match(INSIGHT_PREFIX);
  if (!match) return { status: null as 'met' | 'partial' | null, title: raw, quote: '' };
  const rest = raw.slice(match[0].length);
  const separator = rest.indexOf('。');
  if (separator < 8) {
    return { status: match[1] === '原文支持' ? ('met' as const) : ('partial' as const), title: rest, quote: '' };
  }
  return {
    status: match[1] === '原文支持' ? ('met' as const) : ('partial' as const),
    title: rest.slice(0, separator).trim(),
    quote: rest.slice(separator + 1).trim(),
  };
}

function InsightList({
  items,
  evidence,
  empty = '未发现明确证据',
}: {
  items?: string[];
  evidence?: NonNullable<HrAnalysis['strength_evidence']>;
  empty?: string;
}) {
  if (evidence?.length) {
    return (
      <ul className="mt-4 divide-y divide-line-soft">
        {evidence.map((row, index) => (
          <li key={`${row.requirement_id}-${index}`} className="py-3 text-sm leading-6 text-body first:pt-0 last:pb-0">
            <div className="flex items-start gap-2">
              <span
                className={`mt-0.5 shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-medium ${
                  row.status === 'met'
                    ? 'border-good-border bg-good-soft text-good-deep'
                    : 'border-warn-border bg-warn-soft text-warn-ink-deep'
                }`}
              >
                {row.status === 'met' ? '已满足' : '部分满足'}
              </span>
              <span className="min-w-0 break-words">{row.text}</span>
            </div>
            {row.quote && (
              <blockquote className="mt-1.5 border-l-2 border-line pl-2.5 text-xs leading-5 text-sub">
                <span className="mr-1 font-medium">原文</span>
                {row.quote}
              </blockquote>
            )}
          </li>
        ))}
      </ul>
    );
  }
  const rows = items?.length ? items : [empty];
  return (
    <ul className="mt-4 divide-y divide-line-soft">
      {rows.map((item, index) => {
        const { status, title, quote } = splitInsight(item);
        return (
          <li key={`${item}-${index}`} className="py-3 text-sm leading-6 text-body first:pt-0 last:pb-0">
            <div className="flex items-start gap-2">
              {status === 'met' && (
                <span className="mt-0.5 shrink-0 rounded border border-good-border bg-good-soft px-1.5 py-0.5 text-[10px] font-medium text-good-deep">已满足</span>
              )}
              {status === 'partial' && (
                <span className="mt-0.5 shrink-0 rounded border border-warn-border bg-warn-soft px-1.5 py-0.5 text-[10px] font-medium text-warn-ink-deep">部分满足</span>
              )}
              <span className="min-w-0 break-words">{title}</span>
            </div>
            {quote && (
              <blockquote className="mt-1.5 border-l-2 border-line pl-2.5 text-xs leading-5 text-sub">
                <span className="mr-1 font-medium">原文</span>
                {quote}
              </blockquote>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function Section({
  eyebrow,
  title,
  icon: Icon,
  id,
  children,
  collapsible = false,
  defaultOpen = true,
  summary,
}: {
  eyebrow: string;
  title: string;
  icon: typeof FileSearch2Icon;
  id?: string;
  children: React.ReactNode;
  collapsible?: boolean;
  defaultOpen?: boolean;
  summary?: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section id={id} tabIndex={id ? -1 : undefined} className="scroll-mt-[72px] border border-line bg-white p-5 sm:p-6">
      <div className="flex items-start gap-3 border-b border-line pb-3">
        <span className="flex size-8 shrink-0 items-center justify-center border border-line-strong text-ink">
          <Icon className="size-4" strokeWidth={1.6} />
        </span>
        <div className="min-w-0">
          <p className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-sub">{eyebrow}</p>
          <h2 className="mt-1.5 text-base font-semibold leading-tight text-ink">{title}</h2>
        </div>
        {collapsible && (
          <button
            type="button"
            onClick={() => setOpen((visible) => !visible)}
            aria-expanded={open}
            className="ml-auto inline-flex shrink-0 items-center gap-1 border border-line-strong bg-white px-2.5 py-1 text-[10.5px] font-medium text-ink hover:bg-mist active:scale-[0.98]"
          >
            {open ? '收起' : '展开'}
            {open ? <ChevronUpIcon className="size-3.5" /> : <ChevronDownIcon className="size-3.5" />}
          </button>
        )}
      </div>
      {collapsible && !open && summary && <div className="mt-3 text-xs leading-5 text-sub">{summary}</div>}
      {(!collapsible || open) && <div className="pt-4">{children}</div>}
    </section>
  );
}

function ReportNav({
  items,
  reportRef,
}: {
  items: Array<{ id: string; label: string }>;
  reportRef: React.RefObject<HTMLDivElement | null>;
}) {
  const [activeId, setActiveId] = useState('');
  const progressRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const report = reportRef.current;
    const sections = items.flatMap(({ id }) => {
      const section = document.getElementById(id);
      return section ? [section] : [];
    });
    if (!report || sections.length === 0) return;

    const update = () => {
      const bounds = report.getBoundingClientRect();
      const distance = Math.max(1, bounds.height - window.innerHeight);
      const progress = Math.min(1, Math.max(0, -bounds.top / distance));
      if (progressRef.current) progressRef.current.style.transform = `scaleX(${progress})`;

      let current = sections[0];
      for (const section of sections) {
        if (section.getBoundingClientRect().top <= 88) current = section;
      }
      const reachedEnd = window.scrollY > 0 && bounds.bottom <= window.innerHeight + 1;
      setActiveId(reachedEnd ? sections[sections.length - 1].id : current.id);
    };

    const observer = new ResizeObserver(update);
    observer.observe(report);
    update();
    window.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    return () => {
      observer.disconnect();
      window.removeEventListener('scroll', update);
      window.removeEventListener('resize', update);
    };
  }, [items, reportRef]);

  useEffect(() => {
    const target = document.getElementById(window.location.hash.slice(1));
    if (target && reportRef.current?.contains(target)) target.scrollIntoView();
  }, [reportRef]);

  return (
    <nav className="sticky top-0 z-30 -mx-5 mt-6 flex h-14 items-center border-y border-line bg-white px-5 sm:-mx-8 sm:px-8 lg:-mx-12 lg:px-12" aria-label="报告章节">
      <div className="flex min-w-0 flex-1 items-stretch gap-5 overflow-x-auto px-1 sm:gap-6">
        {items.map(({ id, label }) => (
          <a
            key={id}
            href={`#${id}`}
            aria-current={activeId === id ? 'location' : undefined}
            className={`inline-flex h-[54px] shrink-0 items-center whitespace-nowrap border-b-2 text-[12.5px] hover:text-ink focus-visible:outline-offset-[-2px] ${
              activeId === id ? 'border-ink font-semibold text-ink' : 'border-transparent text-sub'
            }`}
          >
            {label}
          </a>
        ))}
      </div>
      <a href="#report-top" className="ml-3 inline-flex shrink-0 items-center gap-1 border-l border-line pl-3 text-[11.5px] text-sub hover:text-ink">
        回到顶部 <ArrowUpIcon className="size-3" aria-hidden="true" />
      </a>
      <span ref={progressRef} className="pointer-events-none absolute inset-x-0 bottom-0 h-px origin-left scale-x-0 bg-ink" aria-hidden="true" />
    </nav>
  );
}

function MarkdownReport({ content }: { content: string }) {
  return (
    <div className="space-y-3 text-sm leading-7 text-body">
      {content.split(/\r?\n/).filter(Boolean).map((line, index) => {
        const value = line.trim();
        if (value.startsWith('### ')) return <h3 key={index} className="pt-4 text-base font-semibold text-ink">{value.slice(4)}</h3>;
        if (value.startsWith('## ')) return <h2 key={index} className="border-b border-line-soft pb-3 pt-5 text-xl font-semibold text-ink">{value.slice(3)}</h2>;
        if (value.startsWith('# ')) return <h1 key={index} className="border-b border-line-soft pb-4 text-2xl font-semibold text-ink">{value.slice(2)}</h1>;
        if (/^[-*+]\s/.test(value)) return <div key={index} className="flex gap-3"><span className="mt-3 size-1.5 shrink-0 rounded-full bg-brand" /><span>{value.slice(2)}</span></div>;
        if (/^\d+[.)]\s/.test(value)) return <p key={index}>{value}</p>;
        return <p key={index}>{value.replace(/\*\*/g, '')}</p>;
      })}
    </div>
  );
}

export default function DashboardPage() {
  const router = useRouter();
  const { analysisResult, setAnalysisResult, isHydrated } = useAnalysis();
  const [action, setAction] = useState<Action>(null);
  const [optimizedView, setOptimizedView] = useState<{ resumeId: string; jobId: string; markdown: string } | null>(null);
  const [progress, setProgress] = useState('');
  const [error, setError] = useState('');
  const [showAgentValidation, setShowAgentValidation] = useState(false);
  const [archiveFeedback, setArchiveFeedback] = useState<{
    resumeId: string;
    jobId: string;
    phase: 'success' | 'error';
    message: string;
  } | null>(null);
  const [copierOpen, setCopierOpen] = useState(false);
  const [exportFormat, setExportFormat] = useState<ExportFormat>('pdf');
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState('');
  const [exportComplete, setExportComplete] = useState(false);
  const reportRef = useRef<HTMLDivElement>(null);
  const exportCenterRef = useRef<ReportExportHandle>(null);
  const exportControllerRef = useRef<AbortController | null>(null);
  const exportTimeoutRef = useRef<number | null>(null);
  const exportAttemptRef = useRef(0);
  useEffect(() => () => {
    exportAttemptRef.current += 1;
    exportControllerRef.current?.abort();
    if (exportTimeoutRef.current !== null) window.clearTimeout(exportTimeoutRef.current);
  }, []);

  const reportNavItems = useMemo(() => {
    const current = analysisResult?.data;
    if (!current?.hr_analysis) return [];
    return [
      { id: 'sec-overview', label: '核心判定' },
      ...(current.hr_analysis.requirements_checklist?.length || current.hr_analysis.undetermined_requirements?.length
        ? [{ id: 'sec-requirements', label: '岗位要求' }]
        : []),
      { id: 'sec-highlights', label: '风险与亮点' },
      { id: 'sec-details', label: '基础信息' },
      { id: 'sec-education', label: '教育经历' },
      { id: 'sec-employment', label: '工作履历' },
      ...(current.hr_analysis.credibility?.enabled ? [{ id: 'sec-credibility', label: '表述核验' }] : []),
      ...(current.hr_analysis.agent_validation ? [{ id: 'sec-validation', label: 'Agent 校验' }] : []),
      ...(current.comparison ? [{ id: 'sec-ranking', label: isResumeJdEvidenceMatch(current.hr_analysis) ? '匹配证据对比' : '候选人对比' }] : []),
    ];
  }, [analysisResult]);

  if (!isHydrated) {
    return (
      <AppShell active="report">
        <div className="flex min-h-screen items-center justify-center text-sm text-sub">
          <LoaderCircleIcon className="mr-2 size-4 animate-spin" /> 正在载入分析报告
        </div>
      </AppShell>
    );
  }

  if (!analysisResult) {
    return (
      <AppShell active="report">
        <div className="flex min-h-screen items-center justify-center px-5">
          <div className="max-w-md rounded-md border border-line bg-white p-8 text-center">
            <FileSearch2Icon className="mx-auto size-9 text-brand" />
            <h1 className="mt-5 text-xl font-semibold text-ink">暂无可展示的报告</h1>
            <p className="mt-2 text-sm leading-6 text-sub">请先添加简历和岗位描述，完成一次招聘分析。</p>
            <button type="button" onClick={() => router.push('/')} className="mt-6 inline-flex h-10 items-center gap-2 rounded-md bg-brand-deep px-5 text-sm font-medium text-white">
              <ArrowLeftIcon className="size-4" /> 返回分析工作台
            </button>
          </div>
        </div>
      </AppShell>
    );
  }

  const { data } = analysisResult;
  const analysis = data.hr_analysis;
  const savedOptimization = data.optimized_markdown;
  const reportView = savedOptimization && optimizedView?.resumeId === data.resume_id &&
    optimizedView.jobId === data.job_id && optimizedView.markdown === savedOptimization
    ? 'optimized' : 'analysis';
  const batchAnalyses = data.batch_analyses || [];
  const batchFailures = data.batch_failures || [];
  const candidateName = data.candidate_name || analysis?.candidate_name || '候选人';
  const busy = action !== null;
  const currentArchiveFeedback = archiveFeedback?.resumeId === data.resume_id && archiveFeedback?.jobId === data.job_id
    ? archiveFeedback
    : null;
  const reportError = error || (currentArchiveFeedback?.phase === 'error' ? currentArchiveFeedback.message : '');

  const selectCandidate = (selectedResumeId: string) => {
    const selected = batchAnalyses.find((item) => item.resume_id === selectedResumeId);
    if (!selected) return;
    setAnalysisResult({ data: { ...selected, batch_analyses: batchAnalyses, batch_failures: batchFailures, comparison: data.comparison } });
    setOptimizedView(null);
  };

  const handleReanalyze = async () => {
    setAction('reanalyze');
    setError('');
    setProgress('正在重新生成招聘报告');
    try {
      const { data: refreshed } = await analyzeResumes(data.resume_id, data.job_id);
      if (refreshed.resume_id !== data.resume_id || refreshed.job_id !== data.job_id) {
        throw new Error('重新分析结果与本次候选人或岗位不一致。');
      }
      setAnalysisResult((current) => {
        if (!current || current.data.job_id !== data.job_id) return current;
        const latest = current.data;
        const isTarget = (item: typeof latest) => item.resume_id === data.resume_id && item.job_id === data.job_id;
        const target = isTarget(latest) ? latest : latest.batch_analyses?.find(isTarget);
        if (!target || (target.task_id && target.task_id !== data.task_id)) return current;
        const updated = { ...target, ...refreshed };
        return { data: {
          ...(isTarget(latest) ? updated : latest),
          batch_analyses: latest.batch_analyses?.map((item) => isTarget(item) ? { ...item, ...refreshed } : item),
          batch_failures: latest.batch_failures,
          comparison: undefined,
        } };
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '重新分析失败，请稍后重试。');
    } finally {
      setAction(null);
      setProgress('');
    }
  };

  const handleImprove = async () => {
    setAction('improve');
    setError('');
    setProgress('正在准备深度优化');
    try {
      const { data: result } = await improveResumeStream(
        data.resume_id,
        data.job_id,
        (_status, message) => setProgress(message),
      );
      if (result.resume_id !== data.resume_id || result.job_id !== data.job_id) {
        throw new Error('深度优化结果与本次候选人或岗位不一致。');
      }
      const optimizedMarkdown = result.analysis_result?.trim();
      if (!optimizedMarkdown) throw new Error('未生成可用的优化报告，请重试。');
      setAnalysisResult((current) => {
        if (!current || current.data.job_id !== data.job_id) return current;
        const latest = current.data;
        const isTarget = (item: typeof latest) => item.resume_id === data.resume_id && item.job_id === data.job_id && item.task_id === data.task_id;
        if (!isTarget(latest) && !latest.batch_analyses?.some(isTarget)) return current;
        const applyImprovement = (item: typeof latest) => ({
          ...item, optimized_markdown: optimizedMarkdown,
          improvement_task_id: result.task_id, improvement_model_binding: result.model_binding,
        });
        return { data: {
          ...(isTarget(latest) ? applyImprovement(latest) : latest),
          batch_analyses: latest.batch_analyses?.map((item) => isTarget(item) ? applyImprovement(item) : item),
        } };
      });
      setOptimizedView({ resumeId: data.resume_id, jobId: data.job_id, markdown: optimizedMarkdown });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '深度优化失败，请稍后重试。');
    } finally {
      setAction(null);
      setProgress('');
    }
  };

  const handleOpenEditor = async (version: 'optimized' | 'original') => {
    setAction('editor');
    setError('');
    let editor: Window | null = null;
    try {
      editor = window.open('about:blank', '_blank');
      if (!editor) throw new Error('浏览器拦截了编辑器窗口，请允许弹出窗口后重试。');
      editor.opener = null;
      editor.document.title = '正在准备 Resume Studio';
      editor.document.body.textContent = '正在准备简历草稿…';
      let markdown = version === 'original' ? data.studio_markdown : undefined;
      if (!markdown) {
        const draft = await fetchImprovedMarkdown(
          data.resume_id,
          data.job_id,
          version === 'optimized' ? data.optimized_markdown || '' : '',
          version === 'optimized',
        );
        if (version === 'optimized' && (draft.source !== 'extracted' || !draft.markdown.trim())) {
          throw new Error('未能从优化报告提取优化稿，未打开原始简历。你可以重新优化，或选择「打开原始简历」。');
        }
        markdown = draft.markdown;
      }
      if (!markdown) throw new Error('未获取到可编辑的简历内容。');
      if (editor.closed) throw new Error('编辑器窗口已关闭，请重新打开。');
      editor.sessionStorage.setItem('pendingResumeVersion', version);
      editor.sessionStorage.setItem('pendingResumeMD', markdown);
      editor.location.replace(new URL('/a4cv/index.html?pickup=session', window.location.origin).href);
    } catch (caught) {
      editor?.close();
      setError(caught instanceof Error ? caught.message : '编辑器打开失败。');
    } finally {
      setAction(null);
    }
  };

  const openExportCopier = (format: ExportFormat) => {
    if (exportTimeoutRef.current !== null) {
      window.clearTimeout(exportTimeoutRef.current);
      exportTimeoutRef.current = null;
    }
    exportControllerRef.current?.abort();
    exportControllerRef.current = null;
    exportAttemptRef.current += 1;
    setExportFormat(format);
    setExportError('');
    setExportComplete(false);
    setCopierOpen(true);
    void runReportExport(format);
  };

  const closeExportCopier = () => {
    exportAttemptRef.current += 1;
    if (exportTimeoutRef.current !== null) {
      window.clearTimeout(exportTimeoutRef.current);
      exportTimeoutRef.current = null;
    }
    exportControllerRef.current?.abort();
    exportControllerRef.current = null;
    setCopierOpen(false);
    setExporting(false);
    setExportError('');
  };

  const runReportExport = async (format: ExportFormat) => {
    const runner = exportCenterRef.current;
    if (!runner) {
      setExportError('导出组件尚未就绪，请稍后重试。');
      return;
    }
    const attempt = ++exportAttemptRef.current;
    const controller = new AbortController();
    exportControllerRef.current = controller;
    setExporting(true);
    setExportError('');

    let timeoutId = 0;
    const timeout = new Promise<never>((_, reject) => {
      timeoutId = window.setTimeout(() => {
        controller.abort();
        reject(new Error('导出超时，请稍后重试。'));
      }, EXPORT_TIMEOUT_MS);
    });
    exportTimeoutRef.current = timeoutId;

    try {
      await Promise.race([runner.exportReport(format, controller.signal), timeout]);
      if (attempt !== exportAttemptRef.current) return;
      setExportComplete(true);
    } catch (error) {
      if (attempt !== exportAttemptRef.current) return;
      const aborted = error instanceof DOMException && error.name === 'AbortError';
      setExportError(aborted ? '导出超时，请稍后重试。' : error instanceof Error ? error.message : '导出失败，请稍后重试。');
    } finally {
      window.clearTimeout(timeoutId);
      if (exportTimeoutRef.current === timeoutId) exportTimeoutRef.current = null;
      if (attempt === exportAttemptRef.current) {
        exportControllerRef.current = null;
        setExporting(false);
      }
    }
  };

  const handleArchive = async (category: string) => {
    if (!analysis || !data.resume_id || !data.job_id) throw new Error('缺少归档所需的简历或岗位信息。');
    setError('');
    setArchiveFeedback(null);
    try {
      return await createArchive(data.resume_id, data.job_id, {
        candidateName,
        finalScore: analysis.final_score,
        category: category || undefined,
        analysis: analysis as unknown as Record<string, unknown>,
        analysisResult: data.analysis_result,
      });
    } catch (caught) {
      setArchiveFeedback({
        resumeId: data.resume_id,
        jobId: data.job_id,
        phase: 'error',
        message: caught instanceof Error ? caught.message : '归档失败，请稍后重试。',
      });
      throw caught;
    }
  };

  const pendingEvidence = analysis ? evidenceDecisionPending(analysis) : false;
  const provisionalScore = analysis ? evidenceScoreProvisional(analysis) : false;
  const evidenceMatch = analysis ? isResumeJdEvidenceMatch(analysis) : false;
  const hiringRecommendation = analysis ? evidenceRecommendation(analysis) : '';
  const coreVerdict = analysis ? reportCoreVerdict(analysis) : null;
  const evidenceComparison = evidenceMatch || batchAnalyses.some((item) => item.hr_analysis && isResumeJdEvidenceMatch(item.hr_analysis));

  const requirementItems = analysis?.requirements_checklist ?? [];
  const requirementCounts: Record<string, number> = {};
  for (const item of requirementItems) {
    const status = item.effective_status || item.status;
    requirementCounts[status] = (requirementCounts[status] || 0) + 1;
  }
  const requirementsSummary = requirementItems.length
    ? `共 ${requirementItems.length} 条要求 · 部分满足 ${requirementCounts.manual_review || 0} 条 · 未提及 ${requirementCounts.not_mentioned || 0} 条 · 已满足 ${requirementCounts.met || 0} 条${requirementCounts.not_met ? ` · 不符合 ${requirementCounts.not_met} 条` : ''}`
    : '';

  const jobTitle = data.job_title?.trim() || '';
  const batchTotal = batchAnalyses.length + batchFailures.length;
  const analyzedAtRaw = data.analyzed_at || data.created_at || '';
  const analyzedAtDate = analyzedAtRaw ? new Date(analyzedAtRaw) : null;
  const analyzedAt = analyzedAtDate && !Number.isNaN(analyzedAtDate.getTime())
    ? `${analyzedAtDate.getFullYear()}-${String(analyzedAtDate.getMonth() + 1).padStart(2, '0')}-${String(analyzedAtDate.getDate()).padStart(2, '0')} ${String(analyzedAtDate.getHours()).padStart(2, '0')}:${String(analyzedAtDate.getMinutes()).padStart(2, '0')}`
    : '';
  const heroSubtitle = [jobTitle, batchTotal > 1 ? `${batchTotal} 份简历并发分析` : '单份简历分析', analyzedAt].filter(Boolean).join(' · ');
  const scorePct = Math.max(0, Math.min(100, analysis?.final_score ?? 0));
  const gateTotal = requirementItems.length;
  const gateMet = requirementCounts.met || 0;
  const gateFailed = requirementCounts.not_met || 0;
  const gatePending = (requirementCounts.manual_review || 0) + (requirementCounts.not_mentioned || 0);
  const gateNote = gateTotal
    ? [
        gateFailed ? `${gateFailed} 项未满足` : '',
        gatePending ? `${gatePending} 项待核实` : '',
      ].filter(Boolean).join(' · ') || '全部满足'
    : '无硬门槛数据';
  const riskItems = analysis?.risk_points ?? [];
  const riskNote = riskItems[0]
    ? riskItems[0].length > 24
      ? `${riskItems[0].slice(0, 24)}…`
      : riskItems[0]
    : '未见明显风险';
  const experienceYears = analysis?.work_history?.relevant_years || analysis?.work_history?.total_years || '—';
  const employmentYears = (analysis?.work_history?.employment_records ?? [])
    .flatMap((record) => [record.start_date, record.end_date])
    .map((value) => Number((value || '').match(/\d{4}/)?.[0]))
    .filter((value) => Number.isFinite(value));
  const experienceRange = employmentYears.length ? `${Math.min(...employmentYears)}–${Math.max(...employmentYears)}` : '';
  return (
    <AppShell active="report">
      <div id="report-top" ref={reportRef} tabIndex={-1} className="mx-auto w-full max-w-[1300px] px-5 py-8 sm:px-8 lg:px-12 lg:py-10">
        <header className="flex flex-col gap-6 border-b border-line pb-[26px] xl:flex-row xl:items-end xl:justify-between">
          <div>
            <p className="text-[11px] text-sub">
              分析报告 <span className="mx-1.5 text-line-strong">/</span>
              <span className="text-ink">{candidateName}</span>
            </p>
            <div className="mt-[14px] flex items-center gap-[10px]">
              <p className="font-mono text-[11.5px] uppercase tracking-[0.16em] text-sub">02 / Analysis</p>
              <span className="h-px w-[28px] bg-line-strong" aria-hidden="true" />
            </div>
            <h1 className="mt-[18px] text-[42px] font-medium leading-[1.06] tracking-[-0.028em] text-ink">
              {analysis && reportView === 'analysis' ? '候选人分析报告' : '深度优化简历'}
            </h1>
            <p className="mt-3 text-[14px] leading-[1.65] text-sub">{heroSubtitle}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {analysis && data.optimized_markdown && reportView === 'optimized' && (
              <button
                type="button"
                onClick={() => setOptimizedView(null)}
                className="inline-flex h-[38px] items-center gap-2 border border-control-border bg-control px-4 text-[13px] font-medium text-ink transition-colors hover:bg-control-hover"
              >
                <ArrowLeftIcon className="size-4" /> 返回分析报告
              </button>
            )}
            {analysis && reportView === 'analysis' ? (
              <>
                <button
                  type="button"
                  disabled={busy}
                  onClick={handleReanalyze}
                  className="inline-flex h-[38px] items-center gap-2 border border-control-border bg-control px-4 text-[13px] font-medium text-ink transition-colors hover:bg-control-hover disabled:opacity-50"
                >
                  {action === 'reanalyze' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <RefreshCwIcon className="size-4" />} 重新分析
                </button>
                <button
                  type="button"
                  onClick={() => openExportCopier('pdf')}
                  className="inline-flex h-[38px] items-center gap-2 border border-control-border bg-control px-4 text-[13px] font-medium text-ink transition-colors hover:bg-control-hover"
                >
                  <DownloadIcon className="size-4" /> 导出报告
                </button>
                <ArchiveMotionControl
                  candidate={candidateName}
                  jobTitle={jobTitle}
                  disabled={busy || !data.resume_id || !data.job_id}
                  onArchive={handleArchive}
                  onUpdateCategory={updateArchiveCategory}
                  onArchived={(record) => setArchiveFeedback({
                    resumeId: data.resume_id,
                    jobId: data.job_id,
                    phase: 'success',
                    message: `已归档「${record.candidate_name}」到候选人才库`,
                  })}
                />
                <button
                  type="button"
                  disabled={busy}
                  onClick={handleImprove}
                  className="inline-flex h-[38px] items-center gap-2 border border-ink bg-ink px-4 text-[13px] font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-50"
                >
                  {action === 'improve' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SparklesIcon className="size-4" />} 深度优化简历
                  {action !== 'improve' && <ArrowRightIcon className="size-4" />}
                </button>
                {savedOptimization && (
                  <button
                    type="button"
                    onClick={() => setOptimizedView({ resumeId: data.resume_id, jobId: data.job_id, markdown: savedOptimization })}
                    className="inline-flex h-[38px] items-center gap-2 border border-control-border bg-control px-4 text-[13px] font-medium text-ink hover:bg-control-hover"
                  >
                    查看优化稿
                  </button>
                )}
              </>
            ) : analysis && reportView === 'optimized' ? (
              <>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void handleOpenEditor('original')}
                  className="inline-flex h-[38px] items-center gap-2 border border-control-border bg-control px-4 text-[13px] font-medium text-ink hover:bg-control-hover disabled:opacity-50"
                >
                  打开原始简历
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void handleOpenEditor('optimized')}
                  className="inline-flex h-[38px] items-center gap-2 border border-ink bg-ink px-4 text-[13px] font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-50"
                >
                  {action === 'editor' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <PencilIcon className="size-4" />} 在 Resume Studio 中编辑
                </button>
              </>
            ) : (
              <button
                type="button"
                disabled={busy}
                onClick={() => void handleOpenEditor('original')}
                className="inline-flex h-[38px] items-center gap-2 border border-ink bg-ink px-4 text-[13px] font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-50"
              >
                {action === 'editor' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <PencilIcon className="size-4" />} 在 Resume Studio 中编辑
              </button>
            )}
          </div>
        </header>

        {(progress || reportError) && (
          <div className={`mt-5 rounded-md border px-4 py-3 text-sm ${reportError ? 'border-bad-border bg-bad-soft text-bad' : 'border-brand bg-brand-soft text-brand'}`}>
            {progress && !reportError && <LoaderCircleIcon className="mr-2 inline size-4 animate-spin" />}{reportError || progress}
          </div>
        )}

        {batchAnalyses.length > 1 && (
          <div className="mt-6 flex items-stretch overflow-x-auto border border-line bg-white">
            {batchAnalyses.map((item, index) => {
              const active = item.resume_id === data.resume_id;
              const name = item.candidate_name || item.hr_analysis?.candidate_name || `候选人 ${index + 1}`;
              return (
                <button
                  type="button"
                  key={item.resume_id}
                  onClick={() => selectCandidate(item.resume_id)}
                  aria-pressed={active}
                  className={`flex min-w-[195px] shrink-0 items-center gap-[13px] px-[22px] py-[15px] text-left transition-colors ${
                    active ? 'bg-ink text-white' : 'border-l border-line-soft text-ink hover:bg-mist'
                  }`}
                >
                  <span className={`font-mono text-[10.5px] font-medium tracking-[0.14em] ${active ? 'text-white/55' : 'text-sub'}`}>
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  <span className="truncate text-[13.5px]">{name}</span>
                  <span className="ml-auto flex items-baseline gap-1 text-[17px] font-medium tracking-[-0.02em] tabular-nums">
                    {item.hr_analysis?.final_score ?? '--'}
                    {item.hr_analysis && evidenceScoreProvisional(item.hr_analysis) && (
                      <span className="text-[9px] font-normal opacity-55">暂估</span>
                    )}
                  </span>
                </button>
              );
            })}
            <div className="ml-auto hidden items-center px-[18px] font-mono text-[11px] font-medium uppercase tracking-[0.1em] text-sub sm:flex">
              {batchFailures.length ? '部分完成' : '并发分析完成'} · {batchAnalyses.length} / {batchTotal}
            </div>
          </div>
        )}

        {batchFailures.length > 0 && (
          <div className="mt-4 flex gap-3 rounded-md border border-warn-border bg-warn-soft px-4 py-3 text-sm text-warn">
            <AlertTriangleIcon className="mt-0.5 size-4 shrink-0" /> {batchFailures.length} 份简历未完成分析，其余结果已保留。
          </div>
        )}

        {analysis && reportView === 'analysis' ? (
          <>
            <div id="sec-overview" tabIndex={-1} className="mt-6 scroll-mt-[72px] border border-line bg-white">
              <div className="grid lg:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]">
                <div className="border-b border-line px-[30px] pb-6 pt-7 lg:border-b-0 lg:border-r">
                  <p className="font-mono text-[11px] font-medium uppercase tracking-[0.14em] text-sub">综合得分</p>
                  <div className="mt-4 flex items-baseline gap-2.5">
                    <span className="text-[88px] font-normal leading-[0.9] tracking-[-0.055em] tabular-nums text-ink">{analysis.final_score}</span>
                    <span className="font-mono text-[14px] leading-none tracking-[0.06em] text-sub">/ 100</span>
                  </div>

                  <div className="mt-[22px] max-w-[380px] pt-[38px]">
                    <div className="relative h-px bg-line-strong">
                      <span className="absolute left-0 -top-px h-[3px] bg-ink" style={{ width: `${scorePct}%` }} />
                      <span className="absolute left-[60%] -top-[5px] h-[11px] w-px bg-score-tick">
                        <b className="absolute -top-[18px] left-1/2 -translate-x-1/2 font-mono text-[11px] font-medium leading-none tracking-[0.06em] text-body">60</b>
                      </span>
                      <span className="absolute left-[80%] -top-[5px] h-[11px] w-px bg-score-tick">
                        <b className="absolute -top-[18px] left-1/2 -translate-x-1/2 font-mono text-[11px] font-medium leading-none tracking-[0.06em] text-body">80</b>
                      </span>
                      <span className="absolute -top-[9px] h-[19px] w-px bg-ink" style={{ left: `${scorePct}%` }}>
                        <i className="absolute -left-[2px] -top-1 block size-[5px] bg-ink" />
                      </span>
                    </div>
                    <div className="mt-[13px] grid grid-cols-3 sm:grid-cols-[60fr_20fr_20fr]">
                      {[
                        { label: pendingEvidence ? '< 60' : '淘汰 < 60', active: scorePct < 60 },
                        { label: pendingEvidence ? '60–79' : '储备 60–79', active: scorePct >= 60 && scorePct < 80 },
                        { label: pendingEvidence ? '80+' : '优先 80+', active: scorePct >= 80 },
                      ].map(({ label, active }) => (
                        <span
                          key={label}
                          className={`border-t border-line-soft pt-2 font-mono text-[11px] leading-none tracking-[0.06em] sm:pl-2.5 sm:first:pl-0 ${
                            active ? 'font-semibold text-ink' : 'font-medium text-sub'
                          }`}
                        >
                          {label}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>

                <div id="sec-verdict" className="flex min-w-0 scroll-mt-6 flex-col px-[30px] pb-6 pt-7">
                  <div className="mb-[22px] flex flex-wrap items-center gap-3">
                    <span className="inline-flex h-[23px] items-center gap-2 border border-ink bg-ink pl-[9px] pr-[9px] font-mono text-[11px] font-medium tracking-[0.08em] text-white">
                      <i className={`block size-[5px] ${hiringRecommendation === '优先面试' ? 'bg-good-on-ink' : hiringRecommendation === '淘汰' ? 'bg-bad-on-ink' : 'bg-warn-on-ink'}`} />
                      {hiringRecommendation}
                    </span>
                    <span className="font-mono text-[10.5px] font-medium tracking-[0.08em] text-sub">{pendingEvidence ? '证据待核实 · 得分不作为招聘结论' : `适配等级 ${analysis.fit_grade || '—'} · 按综合得分`}</span>
                  </div>

                  <p className="font-mono text-[11px] font-medium uppercase tracking-[0.14em] text-sub">核心判定</p>
                  <p className="mt-[9px] max-w-[50ch] text-[15px] leading-[1.75] text-body [overflow-wrap:anywhere]">{coreVerdict?.summary}</p>
                </div>
              </div>

              <div className="grid border-t border-line sm:grid-cols-2 lg:grid-cols-4">
                <div className="border-b border-line px-[30px] pb-5 pt-[18px] sm:border-r lg:border-b-0">
                  <p className="font-mono text-[11px] font-medium uppercase tracking-[0.14em] text-sub">岗位相关经验</p>
                  <p className="mt-[9px] text-[18px] font-medium leading-[1.2] tracking-[-0.015em] text-ink">{experienceYears}</p>
                  <p className="mt-1 text-[12px] text-sub">职责重合 {analysis.work_history.responsibility_match || '—'}{experienceRange ? ` · ${experienceRange}` : ''}</p>
                </div>
                <div className="border-b border-line px-[30px] pb-5 pt-[18px] lg:border-b-0 lg:border-r">
                  <p className="font-mono text-[11px] font-medium uppercase tracking-[0.14em] text-sub">简历美化程度</p>
                  <p className="mt-[9px] text-[18px] font-medium leading-[1.2] tracking-[-0.015em] text-ink">{analysis.ai_risk_level || '—'}</p>
                  <p className="mt-1 text-[12px] text-sub">{analysis.ai_deduction > 0 ? `扣 ${analysis.ai_deduction} 分` : '仅提示，未扣分'}</p>
                </div>
                <div className="border-b border-line px-[30px] pb-5 pt-[18px] sm:border-r lg:border-b-0">
                  <p className="font-mono text-[11px] font-medium uppercase tracking-[0.14em] text-sub">硬门槛</p>
                  <p className="mt-[9px] text-[18px] font-medium leading-[1.2] tracking-[-0.015em] text-ink">
                    {gateTotal ? (
                      <>
                        {gateMet} / {gateTotal} <span className="text-[12px] font-normal tracking-normal text-sub">项满足</span>
                      </>
                    ) : '—'}
                  </p>
                  <p className="mt-1 text-[12px] text-sub">{gateNote}</p>
                </div>
                <div className="px-[30px] pb-5 pt-[18px]">
                  <p className="font-mono text-[11px] font-medium uppercase tracking-[0.14em] text-sub">招聘风险</p>
                  <p className="mt-[9px] text-[18px] font-medium leading-[1.2] tracking-[-0.015em] text-ink">{riskItems.length} 项</p>
                  <p className="mt-1 truncate text-[12px] text-sub" title={riskItems[0] || ''}>{riskNote}</p>
                </div>
              </div>
            </div>

            <ReportNav items={reportNavItems} reportRef={reportRef} />

            {(coreVerdict?.notice || (analysis.scoring_meta && !evidenceMatch)) && (
              <div className={`mt-4 space-y-1 rounded-md border px-4 py-3 text-xs leading-5 [overflow-wrap:anywhere] ${pendingEvidence || provisionalScore ? 'border-warn-border bg-warn-soft text-warn-ink-deep' : 'border-line bg-mist text-body'}`}>
                {coreVerdict?.notice && <p>{coreVerdict.notice}</p>}
                {analysis.scoring_meta && !evidenceMatch && <p>{evidenceScoreNotice(analysis)}</p>}
              </div>
            )}

            {/* 破格复核提示：能力分高、只被硬门槛卡住 */}
            {!evidenceMatch && !pendingEvidence && analysis.override_review && (
              <div className="mt-4 rounded-md border border-warn-border bg-warn-soft px-4 py-3 text-sm text-warn">
                <div className="flex items-center gap-2 font-medium">
                  <AlertTriangleIcon className="size-4 shrink-0" />
                  建议破格复核：能力匹配 {analysis.capability_score} 分，因硬门槛封顶至 {analysis.final_score} 分
                </div>
                {analysis.gate_blockers && analysis.gate_blockers.length > 0 && (
                  <ul className="mt-2 space-y-1 text-xs leading-5">
                    {analysis.gate_blockers.map((blocker, index) => (
                      <li key={index}>
                        · {blocker.text}
                        {blocker.evidence_label ? `（${blocker.evidence_label}）` : ''}
                        {blocker.basis ? ` ${blocker.basis}` : ''}
                      </li>
                    ))}
                  </ul>
                )}
                <p className="mt-2 text-xs leading-5">
                  系统不会因为经历契合就放宽分数，也不把这条简历归入淘汰——请人工确认是否值得破格进入面试。
                  {analysis.capability_basis === 'ex_gate'
                    ? '（能力分已剔除模型对硬门槛的打分，避免同一缺口被扣两次）'
                    : '（模型未给出分项，能力分退回模型总分）'}
                </p>
              </div>
            )}

            {/* 被"不能由能力弥补"的门槛卡住：说清为什么不建议破格，避免 HR 以为系统漏标 */}
            {!evidenceMatch && !pendingEvidence && !analysis.override_review
              && (analysis.non_mitigable_blockers?.length ?? 0) > 0
              && analysis.final_score < 60 && (
              <div className="mt-4 rounded-md border border-line bg-mist/50 px-4 py-3 text-sm text-body">
                <div className="flex items-center gap-2 font-medium text-ink">
                  <AlertTriangleIcon className="size-4 shrink-0 text-sub" />
                  未达硬门槛：能力匹配 {analysis.capability_score ?? analysis.final_score} 分，但不支持破格复核
                </div>
                <ul className="mt-2 space-y-1 text-xs leading-5">
                  {analysis.non_mitigable_blockers?.map((blocker, index) => (
                    <li key={index}>· {blocker.text}（法规执业资格或事实类要求，能力再高也不能由经历弥补）</li>
                  ))}
                </ul>
                <p className="mt-2 text-xs leading-5">
                  这类门槛通常意味着岗位工作本身要求持证或满足事实条件，破格等于违规上岗——请按制度处理。
                </p>
              </div>
            )}

            {/* 归档提示 */}
            {currentArchiveFeedback?.phase === 'success' && (
              <div className="mt-4 flex items-center gap-3 rounded-md border border-good-border bg-good-soft px-4 py-3 text-sm text-good-deep">
                <CheckCircle2Icon className="size-4 shrink-0" /> {currentArchiveFeedback.message}
              </div>
            )}

            <div className="mt-6 space-y-6">
              {/* 岗位要求逐条判定：让 HR 看到"分是怎么来的"，以及哪些要人工确认 */}
              {((analysis.requirements_checklist?.length ?? 0) > 0
                || (analysis.undetermined_requirements?.length ?? 0) > 0) && (
                <Section key={data.resume_id} id="sec-requirements" eyebrow="逐条核对" title="岗位要求逐条判定" icon={ClipboardCheckIcon} collapsible defaultOpen={false} summary={requirementsSummary}>
                  <RequirementsChecklist analysis={analysis} />
                </Section>
              )}

              {/* 深入分析：次级证据模块平铺（教育·履历·技能·加分） */}
              <div id="sec-highlights" tabIndex={-1} className="scroll-mt-[72px] space-y-6">
              {/* 亮点与风险：决策证据链核心，默认展开（P1-1 distill：与次级区块分离） */}
              <div className="grid gap-6 lg:grid-cols-2">
                <Section eyebrow="优势证据" title="匹配亮点" icon={CheckCircle2Icon}>
                  <InsightList items={analysis.strengths} evidence={analysis.strength_evidence} />
                  <h3 className="mt-6 border-t border-line-soft pt-5 text-sm font-semibold text-ink">项目匹配点</h3>
                  <InsightList items={analysis.skill_match.project_match_points} />
                </Section>
                <Section eyebrow="缺口预警" title="短板与风险" icon={ShieldAlertIcon}>
                  <h3 className="mt-5 text-sm font-semibold text-warn">短板不足</h3>
                  <InsightList items={analysis.weaknesses} />
                  <h3 className="mt-6 border-t border-line-soft pt-5 text-sm font-semibold text-bad">招聘风险预警</h3>
                  <InsightList items={analysis.risk_points} />
                </Section>
              </div>


              <div className="grid gap-6 lg:grid-cols-2">
                <Section eyebrow="能力核对" title="专业技能匹配" icon={BarChart3Icon}>
                  <h3 className="mt-5 text-sm font-semibold text-ink">硬技能与工具</h3>
                  <InsightList items={analysis.skill_match.hard_skills} />
                  <h3 className="mt-6 border-t border-line-soft pt-5 text-sm font-semibold text-ink">软实力</h3>
                  <InsightList items={analysis.skill_match.soft_skills} />
                </Section>
                <Section eyebrow="加分信号" title="竞争力加分项" icon={SparklesIcon}>
                  <InsightList items={analysis.bonus_items} />
                  <h3 className="mt-6 border-t border-line-soft pt-5 text-sm font-semibold text-ink">证书资质</h3>
                  <InsightList items={analysis.certificates} />
                </Section>
              </div>

              <div className="grid gap-6 lg:grid-cols-2">
                <Section eyebrow="岗位适配" title="岗位定制判断" icon={TargetIcon}>
                  <InsightList items={analysis.role_specific_assessment} />
                </Section>
                <Section eyebrow="美化核查" title="美化程度判断依据" icon={ShieldAlertIcon}>
                  {analysis.ai_deduction === 0 && (analysis.ai_deduction_suggested ?? 0) > 0 && (
                    <p className="mb-2 rounded-md border border-warn-border-soft bg-warn-soft px-3 py-2 text-xs leading-5 text-warn-ink-deep">
                      以下是模型给出的「疑似 AI 包装」语言特征。该项当前<strong className="font-semibold">不参与扣分</strong>
                      ——风格印象没有可核对的对象，是否采信请 HR 自行判断（模型建议扣 {analysis.ai_deduction_suggested} 分，未采用）。
                    </p>
                  )}
                  <InsightList items={analysis.deduction_reasons} empty="未发现明显美化痕迹" />
                </Section>
              </div>
              </div>

              <div id="sec-details" tabIndex={-1} className="scroll-mt-[72px] space-y-6">
              <Section eyebrow="基本信息" title="基础信息筛选" icon={GraduationCapIcon}>
                <DetailGrid values={[
                  ['姓名', candidateName],
                  ['性别', analysis.basic_screening.gender],
                  ['年龄', analysis.basic_screening.age],
                  ['籍贯', analysis.basic_screening.native_place],
                  ['工作所在地', analysis.basic_screening.work_location],
                  ['期望薪资', analysis.basic_screening.salary_expectation],
                ]} />
              </Section>

              <Section id="sec-education" eyebrow="教育背景" title="教育经历" icon={GraduationCapIcon}>
                {analysis.education_history && analysis.education_history.length > 0 ? (
                  <>
                    <div className="mt-3 flex items-start gap-2 rounded-md border border-warn-border bg-warn-soft px-3.5 py-2.5 text-xs leading-5 text-warn-ink-deep">
                      <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                      <span>学历信息为简历自述，未经权威渠道核验，请结合学信网或学历/学位证书原件复核（人工待审核）。</span>
                    </div>
                    <div className="mt-3 flex flex-col gap-3">
                      {analysis.education_history.map((edu, i) => (
                        <div key={i} className="rounded-md border border-line-soft bg-mist p-4">
                          <div className="flex items-center gap-2 text-xs font-semibold text-brand">
                            <span className="inline-flex h-5 w-5 items-center justify-center rounded bg-brand-soft text-[10px]">{i + 1}</span>
                            <span>{edu.degree}</span>
                            <span className="ml-auto text-sub font-normal">毕业时间：{edu.graduation_year}</span>
                          </div>
                          <div className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1.5">
                            <div className="flex items-center"><dt className="w-14 shrink-0 text-[11px] text-sub">院校</dt><dd className="text-sm text-ink">{edu.school_name}</dd></div>
                            <div className="flex items-center"><dt className="w-14 shrink-0 text-[11px] text-sub">层次</dt><dd className="text-sm text-ink">{edu.school_tier}</dd></div>
                            <div className="flex items-center"><dt className="w-14 shrink-0 text-[11px] text-sub">专业</dt><dd className="text-sm text-ink">{edu.major}</dd></div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <p className="mt-2 text-sm text-sub">暂无教育经历信息。</p>
                )}
              </Section>

              <Section id="sec-employment" eyebrow="职业履历" title="工作履历" icon={BriefcaseBusinessIcon}>
                <DetailGrid
                  compact
                  values={[
                    ['总工作年限', analysis.work_history.total_years],
                    ['相关岗位年限', analysis.work_history.relevant_years],
                    ['职责重合度', analysis.work_history.responsibility_match],
                    ['行业匹配', analysis.work_history.industry_match],
                    ['公司背景', analysis.work_history.company_background],
                    ['岗位层级', analysis.work_history.seniority],
                    ['带人规模', analysis.work_history.team_size],
                    ['跳槽稳定性', analysis.work_history.stability],
                  ]}
                />
                <EmploymentGapSummary value={analysis.work_history.employment_gaps} />
                <EmploymentOverlapWarning items={analysis.work_history.employment_overlaps} />
                 <EmploymentTimeline records={analysis.work_history.employment_records} />
              </Section>


              </div>

              {/* 真实性核验：只读提示（不进分数），只在后端切 on 时出现 */}
              {analysis.credibility?.enabled && (
                <Section id="sec-credibility" eyebrow="真实性核验" title="表述核验与待核实项" icon={ShieldCheckIcon}>
                  <CredibilityPanel analysis={analysis} />
                </Section>
              )}

              {/* Agent 校验：折叠徽章，始终显示；展开查看五步检测过程与问题详情 */}
              {analysis.agent_validation && analysis.agent_validation.checked_rules > 0 && (
                <section id="sec-validation" tabIndex={-1} className={`scroll-mt-[72px] rounded-md border ${agentValidationNeedsReview(analysis.agent_validation) ? 'border-warn-border-soft bg-warn-soft' : 'border-good-border-soft bg-good-soft'}`}>
                  <button
                    type="button"
                    onClick={() => setShowAgentValidation((visible) => !visible)}
                    className={`flex w-full items-center gap-2 px-4 py-3 text-left text-xs font-medium ${agentValidationNeedsReview(analysis.agent_validation) ? 'text-warn' : 'text-good'}`}
                  >
                    {agentValidationNeedsReview(analysis.agent_validation) ? <ShieldAlertIcon className="size-3.5 shrink-0" /> : <CheckCircle2Icon className="size-3.5 shrink-0" />}
                    <span>
                      Agent 校验：{agentValidationStatus(analysis.agent_validation)}
                    </span>
                    <span className={`ml-auto flex shrink-0 items-center gap-1 ${agentValidationNeedsReview(analysis.agent_validation) ? 'text-warn' : 'text-good'}`}>
                      {showAgentValidation ? '收起' : '查看过程'}
                      {showAgentValidation ? <ChevronUpIcon className="size-3.5" /> : <ChevronDownIcon className="size-3.5" />}
                    </span>
                  </button>
                  {showAgentValidation && (
                    <div className={`border-t px-4 pb-4 pt-3 ${agentValidationNeedsReview(analysis.agent_validation) ? 'border-warn-border-faint' : 'border-good-border-soft'}`}>
                      {analysis.agent_trace?.steps?.length ? (
                        <div>
                          <p className="text-xs font-semibold text-ink">检测过程（共 {analysis.agent_trace.steps.length} 步）</p>
                          <ol className="mt-2 space-y-1.5">
                            {analysis.agent_trace.steps.map((step, i) => (
                              <li key={i} className="flex items-start gap-2 text-xs leading-5">
                                <span className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full bg-mist text-[10px] text-sub">{i + 1}</span>
                                <span className="shrink-0 font-semibold text-ink">{step.step}</span>
                                <span className="shrink-0 text-sub">{step.status}</span>
                                <span className="min-w-0 break-words text-body">{step.detail}</span>
                              </li>
                            ))}
                          </ol>
                        </div>
                      ) : (
                        <p className="text-xs text-sub">本次分析未记录检测过程明细。</p>
                      )}
                      {analysis.agent_validation.issues.length > 0 && (
                        <div className="mt-3 overflow-x-auto rounded-md border border-line-soft">
                          <table className="w-full border-collapse text-sm">
                            <thead>
                              <tr className="bg-mist">
                                <th className="border-b border-line-soft px-4 py-2 text-left text-xs font-semibold text-sub">问题类型</th>
                                <th className="border-b border-line-soft px-4 py-2 text-left text-xs font-semibold text-sub">问题</th>
                                <th className="border-b border-line-soft px-4 py-2 text-left text-xs font-semibold text-sub">修正</th>
                              </tr>
                            </thead>
                            <tbody>
                              {analysis.agent_validation.issues.map((issue, i) => (
                                <tr key={i} className="border-b border-line-soft last:border-b-0">
                                  <td className="px-4 py-2 text-xs text-ink">
                                    <span className="inline-block rounded bg-warn-soft px-1.5 py-0.5 text-[10px] text-warn">{AGENT_RULE_LABELS[issue.rule] || ''}</span>
                                  </td>
                                  <td className="px-4 py-2 text-xs text-bad">{issue.problem}</td>
                                  <td className="px-4 py-2 text-xs text-good">{issue.fix}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  )}
                </section>
              )}
            </div>

            {/* 候选人排名 */}
            {data.comparison && (
              <div className="mt-6">
                <Section id="sec-ranking" eyebrow="全局对比" title={evidenceComparison ? '匹配证据对比' : '候选人排名'} icon={TrophyIcon}>
                  <div className="mt-4">
                    {evidenceComparison && (
                      <p className="mb-3 text-xs leading-5 text-sub">仅比较简历与JD的匹配证据，不代表能力排名；未体现不代表不具备，真实性仍需核验。</p>
                    )}
                    {!evidenceComparison && batchAnalyses.some((item) => item.hr_analysis && evidenceScoreProvisional(item.hr_analysis)) && (
                      <p className="mb-3 text-xs leading-5 text-warn-ink-deep">含暂估分的候选人对比仅供参考，请先补充证据，勿据此确定排名或淘汰。</p>
                    )}
                    <div className="overflow-x-auto rounded-md border border-line-soft">
                      <table className="w-full border-collapse text-sm">
                        <thead>
                          <tr className="bg-mist">
                            <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">{evidenceComparison ? '顺序' : '排名'}</th>
                            <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">得分</th>
                            <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">候选人</th>
                            <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">核心差异点</th>
                          </tr>
                        </thead>
                        <tbody>
                          {data.comparison.ranking.map((item) => {
                            const rankLabel = `${item.rank}`;
                            const target = batchAnalyses.find(
                              (batch) => (batch.candidate_name || batch.hr_analysis?.candidate_name) === item.name,
                            );
                            return (
                              <tr
                                key={item.rank}
                                onClick={() => target && selectCandidate(target.resume_id)}
                                onKeyDown={(e) => {
                                  if (target && (e.key === 'Enter' || e.key === ' ')) {
                                    e.preventDefault();
                                    selectCandidate(target.resume_id);
                                  }
                                }}
                                role={target ? 'button' : undefined}
                                tabIndex={target ? 0 : undefined}
                                aria-label={target ? `切换到候选人 ${item.name}，得分 ${target?.hr_analysis?.final_score ?? item.score}` : undefined}
                                className={`border-b border-line-soft last:border-b-0 ${target ? 'cursor-pointer transition-colors focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-brand hover:bg-soft' : ''}`}
                                title={target ? `点击切换到 ${item.name}` : undefined}
                              >
                                <td className="px-4 py-3 font-semibold text-ink">{rankLabel}</td>
                                <td className="px-4 py-3 font-semibold text-brand">{target?.hr_analysis?.final_score ?? item.score}{target?.hr_analysis && evidenceScoreProvisional(target.hr_analysis) ? ' · 暂估' : ''}</td>
                                <td className="px-4 py-3 text-ink">{item.name}</td>
                                <td className="px-4 py-3 text-sub">{item.difference}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>

                    {data.comparison.recommendation && (
                      <p className="mt-3 text-sm font-medium text-good">{data.comparison.recommendation}</p>
                    )}
                    {data.comparison.pairwise.length > 0 && (
                      <ul className="mt-2 list-none space-y-1">
                        {data.comparison.pairwise.map((p, i) => (
                          <li key={i} className="text-xs text-sub">• {p}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                </Section>
              </div>
            )}


            {/* 统一导出中心：整份报告 × 图片/PDF/Word */}
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-line bg-white px-4 py-2.5">
              <p className="text-xs text-sub">导出整份报告（含 Agent 校验、标记要点）</p>
              <ReportExportCenter
                ref={exportCenterRef}
                key={data.resume_id}
                analysis={analysis}
                candidateName={candidateName}
                resumeId={data.resume_id}
                onRequestExport={openExportCopier}
              />
            </div>

          </>
        ) : (
          <section className="mt-6 rounded-md border border-line bg-white p-5 sm:p-8">
            <MarkdownReport content={data.optimized_markdown || data.analysis_result || '暂无深度优化内容。'} />
          </section>
        )}
      </div>

      <ReportCopier
        open={copierOpen}
        format={exportFormat}
        exporting={exporting}
        error={exportError}
        onClose={closeExportCopier}
        completed={exportComplete}
      />
    </AppShell>
  );
}
