'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  BarChart3Icon,
  BriefcaseBusinessIcon,
  CalendarDaysIcon,
  CheckCircle2Icon,
  Clock3Icon,
  FileSearch2Icon,
  GraduationCapIcon,
  LoaderCircleIcon,
  PencilIcon,
  RefreshCwIcon,
  ShieldAlertIcon,
  SparklesIcon,
  TargetIcon,
  UserRoundIcon,
} from 'lucide-react';
import AppShell from '@/components/workbench/app-shell';
import { useAnalysis, type EmploymentRecord } from '@/components/workbench/analysis-context';
import { AiModelButton, useAiModel } from '@/components/workbench/ai-model-config';
import { API_URL } from '@/lib/api/config';
import { analyzeResumes, improveResumeStream } from '@/lib/api/screening';

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
    <dl className={`mt-5 grid border-l border-t border-[#e2e7ee] ${compact ? 'grid-cols-2 xl:grid-cols-4' : 'sm:grid-cols-2 xl:grid-cols-3'}`}>
      {values.map(([label, value]) => (
        <div key={label} className="min-w-0 border-b border-r border-[#e2e7ee] px-4 py-3.5">
          <dt className="text-xs text-[#8290a3]">{label}</dt>
          <dd className="mt-1 break-words text-sm font-medium leading-6 text-[#2c394f]">{value || EMPTY_VALUE}</dd>
        </div>
      ))}
    </dl>
  );
}

function EmploymentGapSummary({ value }: { value?: string }) {
  return (
    <div className="mt-5 flex items-start gap-3 border-y border-[#e2e7ee] bg-[#f8fafc] px-4 py-3.5">
      <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-[#eaf0fb] text-[#4d6fae]">
        <Clock3Icon className="size-4" />
      </span>
      <div className="min-w-0">
        <p className="text-xs font-medium text-[#66758b]">空窗期核算</p>
        <p className="mt-1 break-words text-sm font-medium leading-6 text-[#2c394f]">{value || EMPTY_VALUE}</p>
      </div>
    </div>
  );
}

function EmploymentTimeline({ records }: { records?: EmploymentRecord[] }) {
  const rows = records || [];
  return (
    <div className="mt-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <h3 className="text-sm font-semibold text-[#29364c]">工作经历明细</h3>
        {rows.length > 0 && <p className="text-xs text-[#8290a3]">{rows.length} 段经历 · 按开始时间倒序</p>}
      </div>
      <ol className="mt-3 divide-y divide-[#e5e9ef] border-y border-[#e2e7ee]">
        {rows.length ? rows.map((record, index) => (
          <li
            key={`${record.company_name}-${record.start_date}-${index}`}
            className="grid min-w-0 gap-3 py-4 sm:grid-cols-[minmax(0,1fr)_minmax(210px,auto)] sm:items-center"
          >
            <div className="min-w-0">
              <p className="break-words text-sm font-semibold leading-6 text-[#243249]">{record.company_name || EMPTY_VALUE}</p>
              <p className="mt-0.5 break-words text-sm leading-6 text-[#65738a]">{record.job_title || EMPTY_VALUE}</p>
            </div>
            <div className="min-w-0 text-sm text-[#526178] sm:text-right">
              <p className="inline-flex max-w-full items-center gap-2 leading-6">
                <CalendarDaysIcon className="size-4 shrink-0 text-[#6c86bd]" />
                <span className="break-words">{record.start_date || EMPTY_VALUE} 至 {record.end_date || EMPTY_VALUE}</span>
              </p>
              {record.duration && record.duration !== '未提供' && (
                <p className="mt-0.5 text-xs text-[#8290a3]">任职 {record.duration}</p>
              )}
            </div>
          </li>
        )) : (
          <li className="py-4 text-sm leading-6 text-[#8290a3]">{EMPTY_VALUE}</li>
        )}
      </ol>
    </div>
  );
}

function InsightList({ items, empty = '未发现明确证据' }: { items?: string[]; empty?: string }) {
  const rows = items?.length ? items : [empty];
  return (
    <ul className="mt-4 divide-y divide-[#e7ebf1]">
      {rows.map((item, index) => (
        <li key={`${item}-${index}`} className="flex gap-3 py-3 text-sm leading-6 text-[#48566d] first:pt-0 last:pb-0">
          <span className="mt-2 size-1.5 shrink-0 rounded-full bg-[#6f91e4]" />
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
  children,
}: {
  eyebrow: string;
  title: string;
  icon: typeof FileSearch2Icon;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-md border border-[#dce2eb] bg-white p-5 sm:p-6">
      <div className="flex items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-[#edf3ff] text-[#496fc9]">
          <Icon className="size-4.5" />
        </span>
        <div>
          <p className="text-[11px] font-semibold uppercase text-[#8290a3]">{eyebrow}</p>
          <h2 className="mt-1 text-lg font-semibold text-[#1b273d]">{title}</h2>
        </div>
      </div>
      {children}
    </section>
  );
}

function MarkdownReport({ content }: { content: string }) {
  return (
    <div className="space-y-3 text-sm leading-7 text-[#45536a]">
      {content.split(/\r?\n/).filter(Boolean).map((line, index) => {
        const value = line.trim();
        if (value.startsWith('### ')) return <h3 key={index} className="pt-4 text-base font-semibold text-[#1c2940]">{value.slice(4)}</h3>;
        if (value.startsWith('## ')) return <h2 key={index} className="border-b border-[#e4e9ef] pb-3 pt-5 text-xl font-semibold text-[#17243a]">{value.slice(3)}</h2>;
        if (value.startsWith('# ')) return <h1 key={index} className="border-b border-[#e4e9ef] pb-4 text-2xl font-semibold text-[#17243a]">{value.slice(2)}</h1>;
        if (/^[-*+]\s/.test(value)) return <div key={index} className="flex gap-3"><span className="mt-3 size-1.5 shrink-0 rounded-full bg-[#6f91e4]" /><span>{value.slice(2)}</span></div>;
        if (/^\d+[.)]\s/.test(value)) return <p key={index}>{value}</p>;
        return <p key={index}>{value.replace(/\*\*/g, '')}</p>;
      })}
    </div>
  );
}

export default function DashboardPage() {
  const router = useRouter();
  const { analysisResult, setAnalysisResult, isHydrated } = useAnalysis();
  const { config: aiConfig, isConfigured, openConfigurator } = useAiModel();
  const [action, setAction] = useState<Action>(null);
  const [progress, setProgress] = useState('');
  const [error, setError] = useState('');

  if (!isHydrated) {
    return (
      <AppShell active="report">
        <div className="flex min-h-screen items-center justify-center text-sm text-[#6f7d91]">
          <LoaderCircleIcon className="mr-2 size-4 animate-spin" /> 正在载入分析报告
        </div>
      </AppShell>
    );
  }

  if (!analysisResult) {
    return (
      <AppShell active="report">
        <div className="flex min-h-screen items-center justify-center px-5">
          <div className="max-w-md rounded-md border border-[#dce2eb] bg-white p-8 text-center">
            <FileSearch2Icon className="mx-auto size-9 text-[#6688d8]" />
            <h1 className="mt-5 text-xl font-semibold text-[#1b273d]">暂无可展示的报告</h1>
            <p className="mt-2 text-sm leading-6 text-[#738096]">请先添加简历和岗位描述，完成一次招聘分析。</p>
            <button type="button" onClick={() => router.push('/')} className="mt-6 inline-flex h-10 items-center gap-2 rounded-md bg-[#1b2a45] px-5 text-sm font-medium text-white">
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
    setAnalysisResult({ data: { ...selected, batch_analyses: batchAnalyses, batch_failures: batchFailures } });
  };

  const handleReanalyze = async () => {
    if (!isConfigured) {
      setError('请先配置 AI 模型，再重新分析。');
      openConfigurator();
      return;
    }
    setAction('reanalyze');
    setError('');
    setProgress('正在重新生成招聘报告');
    try {
      const result = await analyzeResumes(data.resume_id, data.job_id, aiConfig);
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
    if (!isConfigured) {
      setError('请先配置 AI 模型，再进行深度优化。');
      openConfigurator();
      return;
    }
    setAction('improve');
    setError('');
    setProgress('正在准备深度优化');
    try {
      const result = await improveResumeStream(
        data.resume_id,
        data.job_id,
        aiConfig,
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
        const response = await fetch(`${API_URL}/api/v1/resumes/improved-markdown`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resume_id: data.resume_id, job_id: data.job_id, analysis_result: data.analysis_result || '' }),
        });
        const text = await response.text();
        if (!response.ok) throw new Error(text || `编辑器内容生成失败（HTTP ${response.status}）`);
        markdown = (JSON.parse(text) as { data?: { markdown?: string } }).data?.markdown;
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

  const recommendationClass = analysis?.recruitment_recommendation === '优先面试'
    ? 'bg-[#e8f7f0] text-[#177453]'
    : analysis?.recruitment_recommendation === '储备观察'
      ? 'bg-[#fff5dc] text-[#8b6514]'
      : 'bg-[#fff0ed] text-[#9a4035]';

  return (
    <AppShell active="report">
      <div className="mx-auto w-full max-w-[1500px] px-5 py-8 sm:px-8 lg:px-10 lg:py-10 xl:px-14">
        <header className="flex flex-col gap-5 border-b border-[#dce2eb] pb-7 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <button type="button" onClick={() => router.push('/')} className="mb-4 inline-flex items-center gap-2 text-sm font-medium text-[#5d6d84] hover:text-[#1e2d47]">
              <ArrowLeftIcon className="size-4" /> 新建分析
            </button>
            <p className="text-xs font-semibold uppercase text-[#5e7190]">Candidate screening report</p>
            <h1 className="mt-2 text-3xl font-semibold text-[#152137] sm:text-4xl">{analysis ? '候选人分析报告' : '深度优化简历'}</h1>
            <p className="mt-3 text-sm text-[#6f7d91]">{candidateName} · 基于目标岗位要求生成</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <AiModelButton compact />
            {analysis ? (
              <>
                <button type="button" disabled={busy} onClick={handleReanalyze} className="inline-flex h-10 items-center gap-2 rounded-md border border-[#d3dae5] bg-white px-4 text-sm font-medium text-[#334158] hover:bg-[#f8f9fb] disabled:opacity-50">
                  {action === 'reanalyze' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <RefreshCwIcon className="size-4" />} 重新分析
                </button>
                <button type="button" disabled={busy} onClick={handleImprove} className="inline-flex h-10 items-center gap-2 rounded-md bg-[#1b2a45] px-4 text-sm font-medium text-white hover:bg-[#263a5e] disabled:opacity-50">
                  {action === 'improve' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SparklesIcon className="size-4" />} 深度优化简历
                </button>
              </>
            ) : (
              <button type="button" disabled={busy} onClick={handleOpenEditor} className="inline-flex h-10 items-center gap-2 rounded-md bg-[#1b2a45] px-4 text-sm font-medium text-white hover:bg-[#263a5e] disabled:opacity-50">
                {action === 'editor' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <PencilIcon className="size-4" />} 在 Resume Studio 中编辑
              </button>
            )}
          </div>
        </header>

        {(progress || error) && (
          <div className={`mt-5 rounded-md border px-4 py-3 text-sm ${error ? 'border-[#efb5ad] bg-[#fff4f2] text-[#9d3e32]' : 'border-[#cdd9f2] bg-[#f1f5ff] text-[#3d5f9f]'}`}>
            {progress && !error && <LoaderCircleIcon className="mr-2 inline size-4 animate-spin" />}{error || progress}
          </div>
        )}

        {batchAnalyses.length > 1 && (
          <div className="mt-6 overflow-x-auto rounded-md border border-[#dce2eb] bg-white p-2">
            <div className="flex min-w-max gap-2">
              {batchAnalyses.map((item, index) => {
                const active = item.resume_id === data.resume_id;
                const name = item.candidate_name || item.hr_analysis?.candidate_name || `候选人 ${index + 1}`;
                return (
                  <button type="button" key={item.resume_id} onClick={() => selectCandidate(item.resume_id)} className={`flex min-w-40 items-center justify-between gap-4 rounded-md px-4 py-3 text-left ${active ? 'bg-[#1b2a45] text-white' : 'text-[#4b596f] hover:bg-[#f3f6fa]'}`}>
                    <span><span className="block text-xs opacity-60">0{index + 1}</span><span className="mt-0.5 block text-sm font-medium">{name}</span></span>
                    <span className="text-lg font-semibold">{item.hr_analysis?.final_score ?? '--'}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {batchFailures.length > 0 && (
          <div className="mt-4 flex gap-3 rounded-md border border-[#efcf8a] bg-[#fff9e9] px-4 py-3 text-sm text-[#805f18]">
            <AlertTriangleIcon className="mt-0.5 size-4 shrink-0" /> {batchFailures.length} 份简历未完成分析，其余结果已保留。
          </div>
        )}

        {analysis ? (
          <>
            <div className="mt-6 grid overflow-hidden rounded-md border border-[#dce2eb] bg-white sm:grid-cols-2 xl:grid-cols-4">
              {[
                { label: '综合得分', value: String(analysis.final_score), note: analysis.fit_grade, color: 'text-[#1d7f5c]' },
                { label: '岗位契合度', value: `${analysis.job_fit_percentage}%`, note: `基础分 ${analysis.job_fit_score}/100`, color: 'text-[#3e6fd3]' },
                { label: '简历美化程度', value: analysis.ai_risk_level, note: `${analysis.ai_risk_label} · 扣 ${analysis.ai_deduction} 分`, color: 'text-[#995c87]' },
                { label: '招聘建议', value: analysis.recruitment_recommendation, note: analysis.fit_tag, color: 'text-[#17243a]' },
              ].map((metric) => (
                <div key={metric.label} className="border-b border-[#e1e6ed] p-5 last:border-b-0 sm:[&:nth-child(odd)]:border-r xl:border-b-0 xl:border-r xl:last:border-r-0">
                  <p className="text-xs text-[#8190a4]">{metric.label}</p>
                  <p className={`mt-2 text-2xl font-semibold ${metric.color}`}>{metric.value}</p>
                  <p className="mt-1 text-xs text-[#65738a]">{metric.note}</p>
                </div>
              ))}
            </div>

            <div className="mt-6 grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_310px]">
              <div className="space-y-6">
                <Section eyebrow="Decision summary" title="核心判定" icon={TargetIcon}>
                  <p className="mt-5 text-sm leading-7 text-[#435168]">{analysis.summary}</p>
                </Section>

                <Section eyebrow="Basic screening" title="基础信息筛选" icon={GraduationCapIcon}>
                  <DetailGrid values={[
                    ['最高学历', analysis.basic_screening.highest_education],
                    [
                      '院校及层次',
                      [analysis.basic_screening.school_name, analysis.basic_screening.school_tier]
                        .filter((value) => value && value !== '未提供' && value !== EMPTY_VALUE)
                        .join(' · ') || EMPTY_VALUE,
                    ],
                    ['学历类型', analysis.basic_screening.education_type],
                    ['专业匹配', analysis.basic_screening.major_match],
                    ['毕业时间', analysis.basic_screening.graduation_year],
                    ['应届状态', analysis.basic_screening.fresh_graduate],
                    ['年龄', analysis.basic_screening.age],
                    ['性别', analysis.basic_screening.gender],
                    ['工作所在地', analysis.basic_screening.work_location],
                    ['期望薪资', analysis.basic_screening.salary_expectation],
                  ]} />
                </Section>

                <Section eyebrow="Career evidence" title="工作履历" icon={BriefcaseBusinessIcon}>
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

                <div className="grid gap-6 lg:grid-cols-2">
                  <Section eyebrow="Match evidence" title="匹配亮点" icon={CheckCircle2Icon}>
                    <InsightList items={analysis.strengths} />
                    <h3 className="mt-6 border-t border-[#e6eaf0] pt-5 text-sm font-semibold text-[#29364c]">项目匹配点</h3>
                    <InsightList items={analysis.skill_match.project_match_points} />
                  </Section>
                  <Section eyebrow="Gap analysis" title="短板与风险" icon={ShieldAlertIcon}>
                    <h3 className="mt-5 text-sm font-semibold text-[#9a6b18]">短板不足</h3>
                    <InsightList items={analysis.weaknesses} />
                    <h3 className="mt-6 border-t border-[#e6eaf0] pt-5 text-sm font-semibold text-[#9a4338]">招聘风险预警</h3>
                    <InsightList items={analysis.risk_points} />
                  </Section>
                </div>

                <div className="grid gap-6 lg:grid-cols-2">
                  <Section eyebrow="Capability match" title="专业技能匹配" icon={BarChart3Icon}>
                    <h3 className="mt-5 text-sm font-semibold text-[#29364c]">硬技能与工具</h3>
                    <InsightList items={analysis.skill_match.hard_skills} />
                    <h3 className="mt-6 border-t border-[#e6eaf0] pt-5 text-sm font-semibold text-[#29364c]">软实力</h3>
                    <InsightList items={analysis.skill_match.soft_skills} />
                  </Section>
                  <Section eyebrow="Additional signals" title="竞争力加分项" icon={SparklesIcon}>
                    <InsightList items={analysis.bonus_items} />
                    <h3 className="mt-6 border-t border-[#e6eaf0] pt-5 text-sm font-semibold text-[#29364c]">证书资质</h3>
                    <InsightList items={analysis.certificates} />
                  </Section>
                </div>
              </div>

              <aside className="space-y-5 xl:sticky xl:top-6">
                <section className="rounded-md border border-[#dce2eb] bg-white p-5">
                  <p className="text-xs font-semibold uppercase text-[#8390a2]">Final decision</p>
                  <div className={`mt-4 inline-flex rounded-full px-3 py-1.5 text-sm font-semibold ${recommendationClass}`}>{analysis.recruitment_recommendation}</div>
                  <p className="mt-4 text-3xl font-semibold text-[#17243a]">{analysis.final_score}<span className="ml-1 text-sm font-normal text-[#8b96a7]">/ 100</span></p>
                  <div className="mt-4 h-2 overflow-hidden rounded-full bg-[#edf0f4]"><div className="h-full rounded-full bg-[#668de8]" style={{ width: `${Math.max(0, Math.min(100, analysis.final_score))}%` }} /></div>
                  <dl className="mt-5 divide-y divide-[#e7ebf0] text-sm">
                    <div className="flex justify-between gap-3 py-3"><dt className="text-[#7e8b9e]">适配标签</dt><dd className="font-medium text-[#2b384e]">{analysis.fit_tag}</dd></div>
                    <div className="flex justify-between gap-3 py-3"><dt className="text-[#7e8b9e]">美化程度</dt><dd className="font-medium text-[#2b384e]">{analysis.ai_risk_level}</dd></div>
                    <div className="flex justify-between gap-3 py-3"><dt className="text-[#7e8b9e]">候选人</dt><dd className="max-w-36 truncate font-medium text-[#2b384e]">{candidateName}</dd></div>
                  </dl>
                </section>

                <section className="rounded-md border border-[#dce2eb] bg-white p-5">
                  <div className="flex items-center gap-2"><UserRoundIcon className="size-4 text-[#5f80ce]" /><h2 className="text-sm font-semibold text-[#253249]">岗位定制判断</h2></div>
                  <InsightList items={analysis.role_specific_assessment} />
                </section>

                {analysis.deduction_reasons.length > 0 && (
                  <section className="rounded-md border border-[#e6d8dc] bg-[#fffafb] p-5">
                    <h2 className="text-sm font-semibold text-[#7c4e59]">美化程度判断依据</h2>
                    <InsightList items={analysis.deduction_reasons} />
                  </section>
                )}
              </aside>
            </div>
          </>
        ) : (
          <section className="mt-6 rounded-md border border-[#dce2eb] bg-white p-5 sm:p-8">
            <MarkdownReport content={data.analysis_result || '暂无深度优化内容。'} />
          </section>
        )}
      </div>
    </AppShell>
  );
}
