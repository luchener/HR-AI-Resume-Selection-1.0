'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  BarChart3Icon,
  BriefcaseBusinessIcon,
  CalendarDaysIcon,
  CheckCircle2Icon,
  Clock3Icon,
  CompassIcon,
  FileSearch2Icon,
  GraduationCapIcon,
  HighlighterIcon,
  LoaderCircleIcon,
  PencilIcon,
  RefreshCwIcon,
  ShieldAlertIcon,
  SparklesIcon,
  TargetIcon,
  TrophyIcon,
  ArchiveIcon,
} from 'lucide-react';
import AppShell from '@/components/workbench/app-shell';
import { useAnalysis, type EmploymentRecord } from '@/components/workbench/analysis-context';
import ResumeReviewPanel from '@/components/workbench/resume-review-panel';
import ReportExportCenter, { AGENT_RULE_LABELS } from '@/components/workbench/report-export';
import { analyzeResumes, fetchImprovedMarkdown, improveResumeStream } from '@/lib/api/screening';
import { createArchive, ARCHIVE_PRESET_CATEGORIES } from '@/lib/api/archives';

type Action = 'reanalyze' | 'improve' | 'editor' | null;

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

function InsightList({ items, empty = '未发现明确证据' }: { items?: string[]; empty?: string }) {
  const rows = items?.length ? items : [empty];
  return (
    <ul className="mt-4 divide-y divide-line-soft">
      {rows.map((item, index) => (
        <li key={`${item}-${index}`} className="flex gap-3 py-3 text-sm leading-6 text-body first:pt-0 last:pb-0">
          <span className="mt-2 size-1.5 shrink-0 rounded-full bg-brand" />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

function Section({
  eyebrow,
  title,
  icon: Icon,
  id,
  children,
}: {
  eyebrow: string;
  title: string;
  icon: typeof FileSearch2Icon;
  id?: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-6 rounded-md border border-line bg-white p-5 sm:p-6">
      <div className="flex items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-brand-soft text-brand">
          <Icon className="size-4.5" />
        </span>
        <div>
          <p className="text-[11px] font-semibold uppercase text-sub">{eyebrow}</p>
          <h2 className="mt-1 text-lg font-semibold text-ink">{title}</h2>
        </div>
      </div>
      {children}
    </section>
  );
}

const NAV_SECTION_ICONS: Record<string, typeof FileSearch2Icon> = {
  'sec-overview': BarChart3Icon,
  'sec-ranking': TrophyIcon,
  'sec-validation': ShieldAlertIcon,
  'sec-education': GraduationCapIcon,
  'sec-highlights': CheckCircle2Icon,
  'resume-review-panel': HighlighterIcon,
};

function ReportNav({ items }: { items: Array<{ id: string; label: string }> }) {
  const [activeId, setActiveId] = useState(items[0]?.id ?? '');

  const itemsKey = items.map((item) => item.id).join(',');

  useEffect(() => {
    setActiveId((current) => (items.some((item) => item.id === current) ? current : items[0]?.id ?? ''));
    const sections = itemsKey
      .split(',')
      .map((id) => document.getElementById(id))
      .filter((node): node is HTMLElement => node !== null);
    if (sections.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActiveId(visible[0].target.id);
      },
      { rootMargin: '-20% 0px -65% 0px' },
    );
    sections.forEach((node) => observer.observe(node));
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemsKey]);

  return (
    <section className="rounded-md border border-line bg-white p-4">
      <div className="flex items-center gap-2">
        <CompassIcon className="size-4 text-brand" />
        <p className="text-sm font-semibold text-ink">报告导航</p>
      </div>
      <nav className="mt-3 grid gap-1" aria-label="报告页内导航">
        {items.map((item) => {
          const Icon = NAV_SECTION_ICONS[item.id] ?? FileSearch2Icon;
          const active = item.id === activeId;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => {
                setActiveId(item.id);
                document.getElementById(item.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }}
              aria-current={active ? 'true' : undefined}
              className={`flex items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px] transition-colors ${
                active ? 'bg-brand-soft font-medium text-brand' : 'text-body hover:bg-soft'
              }`}
            >
              <Icon className={`size-3.5 shrink-0 ${active ? 'text-brand' : 'text-sub'}`} />
              {item.label}
            </button>
          );
        })}
      </nav>
    </section>
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
  const [progress, setProgress] = useState('');
  const [error, setError] = useState('');
  const [showAgentValidation, setShowAgentValidation] = useState(false);
  const [archiveNote, setArchiveNote] = useState('');
  const [archiving, setArchiving] = useState(false);
  const [archiveModalOpen, setArchiveModalOpen] = useState(false);
  const [archiveCategory, setArchiveCategory] = useState('');
  const [archiveCategoryCustom, setArchiveCategoryCustom] = useState('');

  const reportNavItems = useMemo(() => {
    const current = analysisResult?.data;
    if (!current?.hr_analysis) return [];
    return [
      { id: 'sec-overview', label: '评估总览' },
      ...(current.comparison ? [{ id: 'sec-ranking', label: '候选人排名' }] : []),
      ...(current.hr_analysis.agent_validation ? [{ id: 'sec-validation', label: 'Agent 校验' }] : []),
      { id: 'sec-education', label: '教育与履历' },
      { id: 'sec-highlights', label: '亮点与风险' },
      { id: 'resume-review-panel', label: '简历原文标记' },
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
  const batchAnalyses = data.batch_analyses || [];
  const batchFailures = data.batch_failures || [];
  const candidateName = data.candidate_name || analysis?.candidate_name || '候选人';
  const busy = action !== null;

  const selectCandidate = (selectedResumeId: string) => {
    const selected = batchAnalyses.find((item) => item.resume_id === selectedResumeId);
    if (!selected) return;
    setAnalysisResult({ data: { ...selected, batch_analyses: batchAnalyses, batch_failures: batchFailures, comparison: data.comparison } });
  };

  const handleReanalyze = async () => {
    setAction('reanalyze');
    setError('');
    setProgress('正在重新生成招聘报告');
    try {
      const result = await analyzeResumes(data.resume_id, data.job_id);
      if (batchAnalyses.length > 1) {
        const refreshed = result.data;
        const nextBatch = batchAnalyses.map((item) => item.resume_id === refreshed.resume_id ? refreshed : item);
        setAnalysisResult({ data: { ...refreshed, batch_analyses: nextBatch, batch_failures: batchFailures } });
      } else {
        setAnalysisResult(result);
      }
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
      const result = await improveResumeStream(
        data.resume_id,
        data.job_id,
        (_status, message) => setProgress(message),
      );
      setAnalysisResult(result);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '深度优化失败，请稍后重试。');
    } finally {
      setAction(null);
      setProgress('');
    }
  };

  const handleOpenEditor = async () => {
    setAction('editor');
    setError('');
    try {
      let markdown = data.studio_markdown;
      if (!markdown) {
        markdown = await fetchImprovedMarkdown(
          data.resume_id,
          data.job_id,
          data.analysis_result || '',
        );
      }
      if (!markdown) throw new Error('未获取到可编辑的简历内容。');
      sessionStorage.setItem('pendingResumeMD', markdown);
      sessionStorage.setItem('pendingResumeMeta', JSON.stringify({ resumeId: data.resume_id, jobId: data.job_id }));
      window.open('/a4cv/index.html?pickup=session', '_blank', 'noopener,noreferrer');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '编辑器打开失败。');
    } finally {
      setAction(null);
    }
  };

  // ── 归档到候选人才库 ─────────────────────────────────────────────
  const openArchiveModal = () => {
    setError('');
    setArchiveNote('');
    // 默认分类预填 JD 岗位名（若有），HR 可改成预设分类或自定义
    setArchiveCategory('');
    setArchiveCategoryCustom('');
    setArchiveModalOpen(true);
  };

  const resolveArchiveCategory = () => {
    const custom = archiveCategoryCustom.trim();
    if (custom) return custom;
    if (archiveCategory) return archiveCategory;
    return '';
  };

  const handleArchive = async () => {
    if (!analysis || !data.resume_id || !data.job_id || archiving) return;
    setArchiving(true);
    setError('');
    setArchiveNote('');
    const category = resolveArchiveCategory();
    setArchiveModalOpen(false);
    try {
      const record = await createArchive(data.resume_id, data.job_id, {
        candidateName,
        finalScore: analysis.final_score,
        category: category || undefined,
        analysis: analysis as unknown as Record<string, unknown>,
        analysisResult: data.analysis_result,
      });
      setArchiveNote(`已归档「${record.candidate_name}」到候选人才库`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '归档失败，请稍后重试。');
    } finally {
      setArchiving(false);
    }
  };

  const recommendationClass = analysis?.recruitment_recommendation === '优先面试'
    ? 'bg-good-soft text-good'
    : analysis?.recruitment_recommendation === '储备观察'
      ? 'bg-warn-soft text-warn'
      : 'bg-bad-soft text-bad';

  return (
    <AppShell active="report">
      <div className="mx-auto w-full max-w-[1500px] px-5 py-8 sm:px-8 lg:px-10 lg:py-10 xl:px-14">
        <header className="flex flex-col gap-5 border-b border-line pb-7 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <button type="button" onClick={() => router.push('/')} className="mb-4 inline-flex items-center gap-2 text-sm font-medium text-body hover:text-ink">
              <ArrowLeftIcon className="size-4" /> 新建分析
            </button>
            <p className="text-xs font-semibold uppercase text-sub">候选人筛选报告</p>
            <h1 className="mt-2 text-3xl font-semibold text-ink sm:text-4xl">{analysis ? '候选人分析报告' : '深度优化简历'}</h1>
            <p className="mt-3 text-sm text-sub">{candidateName} · 基于目标岗位要求生成</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {analysis ? (
              <>
                <button type="button" disabled={busy} onClick={handleReanalyze} className="inline-flex h-10 items-center gap-2 rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist disabled:opacity-50">
                  {action === 'reanalyze' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <RefreshCwIcon className="size-4" />} 重新分析
                </button>
                <button type="button" disabled={busy || archiving} onClick={openArchiveModal} className="inline-flex h-10 items-center gap-2 rounded-md border border-[#cfe3d8] bg-[#e8f5ee] px-4 text-sm font-medium text-[#1d7f5c] hover:bg-[#d9efe4] disabled:opacity-50">
                  {archiving ? <LoaderCircleIcon className="size-4 animate-spin" /> : <ArchiveIcon className="size-4" />} 归档到人才库
                </button>
                <button type="button" disabled={busy} onClick={handleImprove} className="inline-flex h-10 items-center gap-2 rounded-md bg-brand-deep px-4 text-sm font-medium text-white hover:bg-[#263a5e] disabled:opacity-50">
                  {action === 'improve' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SparklesIcon className="size-4" />} 深度优化简历
                </button>
              </>
            ) : (
              <button type="button" disabled={busy} onClick={handleOpenEditor} className="inline-flex h-10 items-center gap-2 rounded-md bg-brand-deep px-4 text-sm font-medium text-white hover:bg-[#263a5e] disabled:opacity-50">
                {action === 'editor' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <PencilIcon className="size-4" />} 在 Resume Studio 中编辑
              </button>
            )}
          </div>
        </header>

        {(progress || error) && (
          <div className={`mt-5 rounded-md border px-4 py-3 text-sm ${error ? 'border-[#efb5ad] bg-bad-soft text-bad' : 'border-brand bg-brand-soft text-brand'}`}>
            {progress && !error && <LoaderCircleIcon className="mr-2 inline size-4 animate-spin" />}{error || progress}
          </div>
        )}

        {batchAnalyses.length > 1 && (
          <div className="mt-6 overflow-x-auto rounded-md border border-line bg-white p-2">
            <div className="flex min-w-max gap-2">
              {batchAnalyses.map((item, index) => {
                const active = item.resume_id === data.resume_id;
                const name = item.candidate_name || item.hr_analysis?.candidate_name || `候选人 ${index + 1}`;
                return (
                  <button type="button" key={item.resume_id} onClick={() => selectCandidate(item.resume_id)} className={`flex min-w-40 items-center justify-between gap-4 rounded-md px-4 py-3 text-left ${active ? 'bg-brand-deep text-white' : 'text-body hover:bg-soft'}`}>
                    <span><span className="block text-xs opacity-60">0{index + 1}</span><span className="mt-0.5 block text-sm font-medium">{name}</span></span>
                    <span className="text-lg font-semibold">{item.hr_analysis?.final_score ?? '--'}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {batchFailures.length > 0 && (
          <div className="mt-4 flex gap-3 rounded-md border border-[#efcf8a] bg-warn-soft px-4 py-3 text-sm text-warn">
            <AlertTriangleIcon className="mt-0.5 size-4 shrink-0" /> {batchFailures.length} 份简历未完成分析，其余结果已保留。
          </div>
        )}

        {analysis ? (
          <>
            <div id="sec-overview" className="mt-6 grid scroll-mt-6 overflow-hidden rounded-md border border-line bg-white sm:grid-cols-2 xl:grid-cols-4">
              {[
                { label: '岗位契合度', value: `${analysis.job_fit_percentage}%`, note: `基础分 ${analysis.job_fit_score}/100`, color: 'text-brand' },
                { label: '简历美化程度', value: analysis.ai_risk_level, note: `${analysis.ai_risk_label} · 扣 ${analysis.ai_deduction} 分`, color: 'text-violet' },
                { label: '相关经验年限', value: analysis.work_history.relevant_years, note: `职责重合 ${analysis.work_history.responsibility_match}`, color: 'text-ink' },
                { label: '跳槽稳定性', value: analysis.work_history.stability, note: `公司背景 ${analysis.work_history.company_background}`, color: 'text-ink' },
              ].map((metric) => (
                <div key={metric.label} className="border-b border-line-soft p-5 last:border-b-0 sm:[&:nth-child(odd)]:border-r xl:border-b-0 xl:border-r xl:last:border-r-0">
                  <p className="text-xs text-sub">{metric.label}</p>
                  <p className={`mt-2 text-2xl font-semibold ${metric.color}`}>{metric.value}</p>
                  <p className="mt-1 text-xs text-sub">{metric.note}</p>
                </div>
              ))}
            </div>

            {/* 统一导出中心：整份报告 × 图片/PDF/Word */}
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-line bg-white px-4 py-2.5">
              <p className="text-xs text-sub">导出整份报告（含 Agent 校验、标记要点）</p>
              <ReportExportCenter key={data.resume_id} analysis={analysis} candidateName={candidateName} resumeId={data.resume_id} />
            </div>

            {/* 归档提示 */}
            {archiveNote && (
              <div className="mt-4 flex items-center gap-3 rounded-md border border-[#bfe3d0] bg-[#e8f5ee] px-4 py-3 text-sm text-[#1d7f5c]">
                <CheckCircle2Icon className="size-4 shrink-0" /> {archiveNote}
              </div>
            )}

            {/* 候选人排名 */}
            {data.comparison && (
              <Section id="sec-ranking" eyebrow="全局对比" title="候选人排名" icon={TrophyIcon}>
                <div className="mt-4">
                  <div className="overflow-x-auto rounded-md border border-line-soft">
                    <table className="w-full border-collapse text-sm">
                      <thead>
                        <tr className="bg-mist">
                          <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">排名</th>
                          <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">得分</th>
                          <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">候选人</th>
                          <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">核心差异点</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.comparison.ranking.map((item) => {
                          const medals = ['🥇', '🥈', '🥉'];
                          const medal = item.rank <= 3 ? medals[item.rank - 1] : `${item.rank}`;
                          const target = batchAnalyses.find(
                            (batch) => (batch.candidate_name || batch.hr_analysis?.candidate_name) === item.name,
                          );
                          return (
                            <tr
                              key={item.rank}
                              onClick={() => target && selectCandidate(target.resume_id)}
                              className={`border-b border-line-soft last:border-b-0 ${target ? 'cursor-pointer transition-colors hover:bg-soft' : ''}`}
                              title={target ? `点击切换到 ${item.name}` : undefined}
                            >
                              <td className="px-4 py-3 font-semibold text-ink">{medal}</td>
                              <td className="px-4 py-3 font-semibold text-brand">{item.score}</td>
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
            )}

            <div className="mt-6 grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_310px]">
              <div className="space-y-6">
                <Section eyebrow="综合结论" title="核心判定" icon={TargetIcon}>
                  <p className="mt-5 text-sm leading-7 text-body">{analysis.summary}</p>
                </Section>

                {/* Agent 校验：折叠徽章，始终显示；有问题时可展开详情 */}
                {analysis.agent_validation && analysis.agent_validation.issues.length > 0 && (
                  <section id="sec-validation" className="scroll-mt-6 rounded-md border border-[#e8dfd0] bg-warn-soft">
                    <button
                      type="button"
                      onClick={() => setShowAgentValidation((visible) => !visible)}
                      className="flex w-full items-center gap-2 px-4 py-3 text-left text-xs font-medium text-warn"
                    >
                      <ShieldAlertIcon className="size-3.5 shrink-0" />
                      <span>
                        Agent 校验：已核查 {analysis.agent_validation.checked_rules} 项要求，检出 {analysis.agent_validation.issues.length} 个问题
                        {analysis.agent_validation.revised ? '并已修正' : ''}
                      </span>
                      <span className="ml-auto shrink-0 text-warn">{showAgentValidation ? '收起 ▴' : '详情 ▾'}</span>
                    </button>
                    {showAgentValidation && (
                      <div className="border-t border-[#eee3cd] px-4 pb-4 pt-3">
                        <div className="overflow-x-auto rounded-md border border-line-soft">
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
                      </div>
                    )}
                  </section>
                )}

                {analysis.agent_validation && analysis.agent_validation.issues.length === 0 && (
                  <section id="sec-validation" className="scroll-mt-6 flex items-center gap-2 rounded-md border border-[#c9e5d6] bg-good-soft px-4 py-3 text-xs font-medium text-good">
                    <CheckCircle2Icon className="size-3.5 shrink-0" />
                    <span>Agent 校验：已核查 {analysis.agent_validation.checked_rules} 项要求，全部通过</span>
                  </section>
                )}

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
                    <div className="mt-2 flex flex-col gap-3">
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
                  ) : (
                    <p className="mt-2 text-sm text-sub">暂无教育经历信息。</p>
                  )}
                </Section>

                <Section eyebrow="职业履历" title="工作履历" icon={BriefcaseBusinessIcon}>
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
                  <EmploymentTimeline records={analysis.work_history.employment_records} />
                </Section>

                <div id="sec-highlights" className="grid scroll-mt-6 gap-6 lg:grid-cols-2">
                  <Section eyebrow="优势证据" title="匹配亮点" icon={CheckCircle2Icon}>
                    <InsightList items={analysis.strengths} />
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
                    <InsightList items={analysis.deduction_reasons} empty="未发现明显美化痕迹" />
                  </Section>
                </div>
              </div>

              <aside className="space-y-5 xl:sticky xl:top-6">
                <section className="rounded-md border border-line bg-white p-5">
                  <p className="text-xs font-semibold uppercase text-sub">招聘决策</p>
                  <div className={`mt-4 inline-flex rounded-full px-3 py-1.5 text-sm font-semibold ${recommendationClass}`}>{analysis.recruitment_recommendation}</div>
                  <p className="mt-4 text-3xl font-semibold text-ink">{analysis.final_score}<span className="ml-1 text-sm font-normal text-sub">/ 100</span></p>
                  <div className="mt-4 h-2 overflow-hidden rounded-full bg-mist"><div className="h-full rounded-full bg-brand" style={{ width: `${Math.max(0, Math.min(100, analysis.final_score))}%` }} /></div>
                  <dl className="mt-5 divide-y divide-line-soft text-sm">
                    <div className="flex justify-between gap-3 py-3"><dt className="text-sub">适配标签</dt><dd className="font-medium text-ink">{analysis.fit_tag}</dd></div>
                    <div className="flex justify-between gap-3 py-3"><dt className="text-sub">候选人</dt><dd className="max-w-36 truncate font-medium text-ink">{candidateName}</dd></div>
                  </dl>
                  <button
                    type="button"
                    onClick={() => document.getElementById('resume-review-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
                    className="mt-4 inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-line-soft bg-white px-3 py-2 text-xs font-medium text-ink hover:bg-soft"
                  >
                    <HighlighterIcon className="size-3.5 text-brand" />
                    简历原文标记 ↓
                  </button>
                </section>

                <ReportNav items={reportNavItems} />
              </aside>
            </div>

            <div id="resume-review-panel" className="scroll-mt-6">
              <ResumeReviewPanel
                key={data.resume_id}
                resumeId={data.resume_id}
                analysis={analysis}
                candidateName={data.candidate_name}
              />
            </div>
          </>
        ) : (
          <section className="mt-6 rounded-md border border-line bg-white p-5 sm:p-8">
            <MarkdownReport content={data.analysis_result || '暂无深度优化内容。'} />
          </section>
        )}
      </div>

      {/* 归档分类弹窗 */}
      {archiveModalOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="归档到候选人才库"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={(e) => { if (e.target === e.currentTarget) setArchiveModalOpen(false); }}
        >
          <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-2xl">
            <h3 className="text-lg font-semibold text-ink">归档到候选人才库</h3>
            <p className="mt-1 text-sm text-sub">选择或自定义岗位分类，方便后续按分类筛选排名。</p>

            <div className="mt-4">
              <p className="mb-2 text-xs font-medium text-sub">预设分类</p>
              <div className="flex flex-wrap gap-2">
                {ARCHIVE_PRESET_CATEGORIES.map((c) => (
                  <button
                    key={c}
                    type="button"
                    onClick={() => { setArchiveCategory(c); setArchiveCategoryCustom(''); }}
                    className={`rounded-full border px-3 py-1 text-sm transition-colors ${archiveCategory === c && !archiveCategoryCustom ? 'border-brand bg-brand-soft text-brand' : 'border-line-soft bg-white text-body hover:bg-mist'}`}
                  >
                    {c}
                  </button>
                ))}
              </div>
            </div>

            <div className="mt-4">
              <label className="mb-1.5 block text-xs font-medium text-sub">自定义分类</label>
              <input
                type="text"
                value={archiveCategoryCustom}
                onChange={(e) => { setArchiveCategoryCustom(e.target.value); setArchiveCategory(''); }}
                placeholder="如：AI 算法工程师、跨境电商运营…（留空则使用岗位名）"
                className="w-full rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand focus:ring-2 focus:ring-brand/20"
              />
            </div>

            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setArchiveModalOpen(false)}
                className="rounded-md border border-line-soft bg-white px-4 py-2 text-sm font-medium text-ink hover:bg-mist"
              >
                取消
              </button>
              <button
                type="button"
                disabled={archiving}
                onClick={handleArchive}
                className="inline-flex items-center gap-2 rounded-md bg-[#1d7f5c] px-4 py-2 text-sm font-medium text-white hover:bg-[#18694d] disabled:opacity-50"
              >
                {archiving ? <LoaderCircleIcon className="size-4 animate-spin" /> : <ArchiveIcon className="size-4" />} 确认归档
              </button>
            </div>
          </div>
        </div>
      )}
    </AppShell>
  );
}
