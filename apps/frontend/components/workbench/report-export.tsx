'use client';

import { forwardRef, useImperativeHandle, useRef, useState } from 'react';
import html2canvas from 'html2canvas';
import { downloadReportPdf } from '@/lib/api/report-pdf';
import { REPORT_EXPORT_STYLE } from '@/lib/report-export-styles';
import { CameraIcon, DownloadIcon, FileTextIcon, LoaderCircleIcon } from 'lucide-react';
import type { HrAnalysis } from './analysis-context';
import { HIRING_BAND_NOTE, evidenceDecisionPending, evidenceRecommendation, relevantExperienceSummary, reportCoreVerdict } from './evidence-score-status';
import { agentValidationNeedsReview } from './agent-validation-status';
import { fetchResumeReviewMarkers, type ResumeReviewData } from '@/lib/api/screening';

export const AGENT_RULE_LABELS = ['', '论断超出简历依据', '评分依据不可追溯', '加分项与岗位无关', '缺失项标注"未提供"', '风险与未体现混淆'];

export type ExportFormat = 'png' | 'pdf' | 'doc';

function esc(value: unknown): string {
  // 引号一并转义：这份 HTML 会进 innerHTML 与 .doc 导出，任何属性上下文都不能被打断。
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function clip(value: unknown, max = 46): string {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function uniqText(items: unknown[], limit: number): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of items) {
    const text = String(item ?? '').trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
    if (out.length >= limit) break;
  }
  return out;
}

/** 从履历、项目、待核实要求和核验提示中生成 3–5 个 HR 必问问题（确定性、不额外调用模型）。 */
function buildInterviewQuestions(a: HrAnalysis): string[] {
  const questions: string[] = [];
  const push = (value: string) => {
    const text = value.trim();
    if (text && !questions.includes(text) && questions.length < 5) questions.push(text);
  };
  const records = a.work_history?.employment_records || [];
  const recent = records.find((record) => record.job_title && !/未提供|未知/.test(record.job_title));
  if (recent) {
    const company = /未提供|未知/.test(recent.company_name || '') ? '' : `在「${clip(recent.company_name, 20)}」`;
    push(`请介绍你${company}担任「${clip(recent.job_title, 24)}」期间的主要职责和一个代表性项目，说明你本人做了什么、结果如何。`);
  }
  const project = (a.skill_match?.project_match_points || []).find((item) => !/未提供/.test(item));
  if (project) {
    push(`简历提到「${clip(project, 40)}」，请说明你负责的具体部分、技术难点和可量化结果。`);
  }
  const pending = (a.requirements_checklist || []).filter((item) =>
    ['not_met', 'manual_review', 'not_mentioned'].includes(item.effective_status || item.status));
  for (const item of pending.slice(0, 2)) {
    const status = item.effective_status || item.status;
    push(`岗位要求「${clip(item.text, 42)}」，简历${status === 'not_met' ? '显示不满足' : '未明确体现'}，请举例说明你的实际经历或补充证据。`);
  }
  if (records.length >= 3 || /频繁|较低|一般/.test(a.work_history?.stability || '')) {
    push(`过去${clip(a.work_history?.total_years || '', 8)}有 ${records.length} 段工作经历，请说明每次离职原因和选择下一份工作的标准。`);
  }
  if (a.education_history?.length) {
    push('学历信息为简历自述，请提供毕业证、学位证或学信网备案等可核验材料。');
  }
  if (a.ai_deduction > 0 || /美化|包装/.test(a.ai_risk_level || '')) {
    push('简历中部分表述较概括，请用具体项目、数据和你的实际贡献说明。');
  }
  if (!questions.length) {
    push('请用 3 分钟介绍最近一段工作：负责什么、最拿得出手的成果、为什么考虑新机会。');
  }
  return questions.slice(0, 5);
}

function buildReportInner(opts: {
  analysis: HrAnalysis;
  candidateName: string;
  markers: ResumeReviewData | null;
}): string {
  const { analysis: a } = opts;
  const name = opts.candidateName || a.candidate_name || '候选人';
  const out: string[] = [];
  const pending = evidenceDecisionPending(a);
  const recommendation = evidenceRecommendation(a);
  const fitTag = pending ? '' : a.fit_tag;
  const fitGrade = pending ? '' : a.fit_grade;
  const checklist = a.requirements_checklist || [];
  const statusOf = (item: (typeof checklist)[number]) => item.effective_status || item.status;
  const countOf = (status: string) => checklist.filter((item) => statusOf(item) === status).length;
  const records = a.work_history?.employment_records || [];
  const experience = relevantExperienceSummary(records, a.work_history?.total_years);
  const verdict = reportCoreVerdict(a);
  const questions = buildInterviewQuestions(a);
  const weaknesses = uniqText([...(a.weaknesses || []), ...(a.risk_points || [])], 3);
  const strengths = uniqText([...(a.strengths || []), ...(a.skill_match?.project_match_points || [])], 3);
  const notMet = checklist.filter((item) => statusOf(item) === 'not_met');
  const pendingItems = checklist.filter((item) => ['manual_review', 'not_mentioned'].includes(statusOf(item)));

  out.push(`
<header class="hero">
  <div class="hero-main">
    <div class="kicker">候选人分析报告</div>
    <h1>${esc(name)}</h1>
    <div class="meta">生成日期 ${new Date().toISOString().slice(0, 10)} · 岗位相关经验 ${esc(experience)}</div>
  </div>
  <div class="hero-score">
    <div class="num">${esc(a.final_score)}<span class="den"> / 100</span></div>
    <div class="rec">${esc(recommendation)}${fitTag ? ` · ${esc(fitTag)}` : ''}</div>
    ${fitGrade ? `<div class="grade">${esc(fitGrade)}</div>` : ''}
  </div>
</header>`);

  if (verdict.summary) {
    out.push(`<div class="verdict"><div class="label">核心判定</div><p>${esc(verdict.summary)}</p>${verdict.notice ? `<p class="verdict-notice">${esc(verdict.notice)}</p>` : ''}</div>`);
  }

  out.push(`
<div class="cards">
  <div class="card"><div class="k">综合得分</div><div class="v green">${esc(a.final_score)}<span class="den"> / 100</span></div><div class="s">${esc(fitGrade || '按简历原文证据计分')}</div></div>
  <div class="card"><div class="k">招聘建议</div><div class="v">${esc(recommendation)}</div><div class="s">${esc(pending ? '证据待核实' : fitTag || '按综合得分分档')}</div></div>
  <div class="card"><div class="k">岗位相关经验</div><div class="v small">${esc(experience)}</div><div class="s">职责重合 ${esc(a.work_history?.responsibility_match || '未提供')}</div></div>
  <div class="card"><div class="k">简历美化程度</div><div class="v small purple">${esc(a.ai_risk_level || '未评估')}</div><div class="s">${esc(a.ai_risk_label || '未发现明显美化痕迹')}</div></div>
</div>
<p class="band">${pending ? '当前得分仅反映已提供证据，不作为招聘结论。' : `招聘建议按综合得分：${esc(HIRING_BAND_NOTE)}`}</p>`);

  out.push(`
<section class="qa">
  <h2>面试建议提问</h2>
  <ol class="questions">${questions.map((question, index) => `<li><span class="qnum">${index + 1}</span>${esc(question)}</li>`).join('')}</ol>
  <p class="sub">根据简历中的履历、项目与待核实要求生成，供面试官按顺序提问。</p>
</section>`);



  const bs = a.basic_screening;
  const education = (a.education_history || [])[0];
  out.push(`
<section>
  <h2>基础信息与教育</h2>
  <table class="compact">
    <tr><td class="lab">姓名</td><td>${esc(name)}</td><td class="lab">性别</td><td>${esc(bs?.gender || '未提供')}</td><td class="lab">年龄</td><td>${esc(bs?.age || '未提供')}</td></tr>
    <tr><td class="lab">所在地</td><td>${esc(bs?.work_location || '未提供')}</td><td class="lab">期望薪资</td><td>${esc(bs?.salary_expectation || '未提供')}</td><td class="lab">学历</td><td>${esc(education?.degree || '未提供')}</td></tr>
    ${education ? `<tr><td class="lab">院校</td><td>${esc(education.school_name)}</td><td class="lab">专业</td><td>${esc(education.major)}</td><td class="lab">毕业</td><td>${esc(education.graduation_year)}</td></tr>` : ''}
  </table>
</section>`);

  out.push(`
<section>
  <h2>工作履历</h2>
  <div class="facts">
    <span><b>总工作年限</b>${esc(a.work_history?.total_years || '未提供')}</span>
    <span><b>岗位相关经验</b>${esc(experience)}</span>
    <span><b>职责重合</b>${esc(a.work_history?.responsibility_match || '未提供')}</span>
    <span><b>跳槽稳定性</b>${esc(a.work_history?.stability || '未提供')}</span>
  </div>
  ${records.length > 0 ? `<table class="compact">
    <thead><tr><th>公司</th><th>岗位</th><th style="width:110px">起止</th><th style="width:64px">时长</th></tr></thead>
    <tbody>${records.slice(0, 6).map((record) => `
      <tr><td>${esc(record.company_name)}</td><td>${esc(record.job_title)}</td><td>${esc(record.start_date)} ~ ${esc(record.end_date)}</td><td>${esc(record.duration)}</td></tr>`).join('')}
    </tbody>
  </table>` : ''}
  ${a.work_history?.employment_gaps && !/^未提供|无空窗|无明显/.test(a.work_history.employment_gaps) ? `<p class="sub">空窗期：${esc(a.work_history.employment_gaps)}</p>` : ''}
</section>`);

  if (weaknesses.length > 0) {
    out.push(`<section><h2>关键短板</h2><ul class="tight">${weaknesses.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></section>`);
  }

  const blockers = (a.gate_blockers || []).slice(0, 3);
  if (blockers.length > 0) {
    out.push(`<section><h2>硬门槛提示</h2><ul class="tight">${blockers.map((blocker) => `<li>${esc(blocker.text)}：<span class="red">${esc(blocker.evidence_label)}</span></li>`).join('')}</ul></section>`);
  }

  if (strengths.length > 0) {
    out.push(`<section><h2>匹配亮点</h2><ul class="tight">${strengths.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></section>`);
  }

  if (checklist.length > 0) {
    out.push(`
<section>
  <h2>岗位要求核对</h2>
  <p class="chips">
    <span>共 ${checklist.length} 条</span>
    <span class="ok">已满足 ${countOf('met')}</span>
    <span class="warn">部分满足 ${countOf('manual_review')}</span>
    <span class="muted">未提及 ${countOf('not_mentioned')}</span>
    ${countOf('not_met') > 0 ? `<span class="bad">不符合 ${countOf('not_met')}</span>` : ''}
  </p>
  ${notMet.length > 0 ? `<p class="sub"><b class="red">不符合：</b>${notMet.slice(0, 4).map((item) => esc(item.text)).join('；')}</p>` : ''}
  ${pendingItems.length > 0 ? `<p class="sub"><b>需重点核实：</b>${pendingItems.slice(0, 4).map((item) => esc(item.text)).join('；')}${pendingItems.length > 4 ? ' 等' : ''}</p>` : ''}
  ${(a.undetermined_requirements || []).length > 0 ? `<p class="sub"><b>待人工确认：</b>${(a.undetermined_requirements || []).slice(0, 4).map((entry) => esc(entry.text)).join('；')}</p>` : ''}
  <p class="sub">未提及不等于不符合；面试时按下发要求逐条确认。</p>
</section>`);
  }


  const findings = (a.credibility?.findings || []).filter((item) => item.level === 'red_flag' || item.level === 'review');
  const agentIssues = a.agent_validation && agentValidationNeedsReview(a.agent_validation) ? a.agent_validation.issues.slice(0, 3) : [];
  if (findings.length > 0 || agentIssues.length > 0) {
    out.push(`
<section class="appendix">
  <h2>核验提示（不影响分数）</h2>
  ${findings.slice(0, 3).map((item) => `<p><b>${esc(item.title)}</b>：${esc(clip(item.detail, 90))}${item.ask ? ` <span class="sub">建议问：${esc(clip(item.ask, 60))}</span>` : ''}</p>`).join('')}
  ${agentIssues.length > 0 ? `<ul class="tight">${agentIssues.map((issue) => `<li>${esc(issue.problem)}</li>`).join('')}</ul>` : ''}
</section>`);
  }

  out.push(`<p class="notice">本报告由 AI 简历筛选系统生成，结论仅供招聘决策参考；标记与核验信息不改变原始简历。</p>`);
  return out.join('\n');
}

function wrapDocument(inner: string, title: string): string {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>${esc(title)}</title><style>${REPORT_EXPORT_STYLE}</style></head><body><div class="page">${inner}</div></body></html>`;
}

function downloadWord(html: string, name: string) {
  const blob = new Blob(['\ufeff', html], { type: 'application/msword;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `候选人分析报告-${name}.doc`;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const BUTTON_CLASS = 'inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium disabled:opacity-50';

/** 归档等场景复用：生成报告图片 blob（JPEG，不触发浏览器下载）。失败返回 null。 */
export async function captureReportPng(opts: {
  analysis: HrAnalysis;
  candidateName: string;
  resumeId: string;
}): Promise<Blob | null> {
  let markers: ResumeReviewData | null = null;
  try {
    markers = await fetchResumeReviewMarkers(
      opts.resumeId,
      opts.analysis as unknown as Record<string, unknown>,
      opts.candidateName,
    );
  } catch {
    markers = null;
  }
  const inner = buildReportInner({ analysis: opts.analysis, candidateName: opts.candidateName, markers });
  const captureHtml = `<style>${REPORT_EXPORT_STYLE}</style><div class="page">${inner}</div>`;

  const holder = document.createElement('div');
  // 与 ReportExportCenter 的隐藏捕获容器保持一致的离屏方式：
  // position:fixed + left 负偏移，不加 z-index:-1（负 z-index 会让 html2canvas 渲染空白）。
  holder.style.cssText = 'position:fixed;left:-99999px;top:0;width:800px;background:#ffffff;';
  holder.innerHTML = captureHtml;
  document.body.appendChild(holder);
  try {
    // 等一帧让离屏容器完成布局与字体加载，避免 html2canvas 截到空白
    await new Promise((r) => requestAnimationFrame(() => r(null)));
    const canvas = await html2canvas(holder, { scale: 2, backgroundColor: '#ffffff', useCORS: true, logging: false });
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9));
    return blob;
  } catch {
    return null;
  } finally {
    holder.remove();
  }
}

/** 生成并直接下载报告图片（JPEG）。成功返回 true，失败返回 false。 */
export async function downloadReportImage(opts: {
  analysis: HrAnalysis;
  candidateName: string;
  resumeId: string;
}): Promise<boolean> {
  const name = opts.candidateName || opts.analysis.candidate_name || '候选人';
  const blob = await captureReportPng(opts);
  if (!blob) return false;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `候选人分析报告-${name}.jpg`;
  anchor.click();
  URL.revokeObjectURL(url);
  return true;
}

export type ReportExportHandle = {
  exportReport: (format: ExportFormat, signal?: AbortSignal) => Promise<void>;
};

type ReportExportCenterProps = {
  analysis: HrAnalysis;
  candidateName: string;
  resumeId: string;
  /** 外部弹层通过 ref.exportReport(format) 启动导出。 */
  onRequestExport?: (format: ExportFormat) => void;
};

const ReportExportCenter = forwardRef<ReportExportHandle, ReportExportCenterProps>(function ReportExportCenter(
  { analysis, candidateName, resumeId, onRequestExport },
  ref,
) {
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const activeExport = useRef<object | null>(null);
  const [exportError, setExportError] = useState('');
  const [markers, setMarkers] = useState<ResumeReviewData | null>(null);

  // 标记数据懒加载：首次导出才请求，失败不阻断报告导出
  async function ensureMarkers(signal?: AbortSignal): Promise<ResumeReviewData | null> {
    if (markers) return markers;
    try {
      const data = await fetchResumeReviewMarkers(resumeId, analysis as unknown as Record<string, unknown>, candidateName, signal);
      signal?.throwIfAborted();
      setMarkers(data);
      return data;
    } catch {
      signal?.throwIfAborted();
      return null;
    }
  }

  function captureImage(inner: string, signal?: AbortSignal) {
    const throwIfAborted = () => {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    };
    const name = candidateName || analysis.candidate_name || '候选人';
    const html = `<style>${REPORT_EXPORT_STYLE}</style><div class="page">${inner}</div>`;
    const holder = document.createElement('div');
    holder.style.cssText = 'position:fixed;left:-99999px;top:0;width:800px;background:#ffffff;';
    holder.innerHTML = html;
    document.body.appendChild(holder);

    return html2canvas(holder, { scale: 2, backgroundColor: '#ffffff', useCORS: true, logging: false })
      .then((canvas) => {
        throwIfAborted();
        return new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9));
      })
      .then((blob) => {
        throwIfAborted();
        if (!blob) throw new Error('图片生成失败，请稍后重试。');
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `候选人分析报告-${name}.jpg`;
        anchor.click();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      })
      .catch((error) => {
        if (error instanceof DOMException && error.name === 'AbortError') throw error;
        throw error instanceof Error ? error : new Error('图片生成失败，请稍后重试。');
      })
      .finally(() => {
        holder.remove();
      });
  }

  async function handleExport(format: ExportFormat, signal?: AbortSignal) {
    signal?.throwIfAborted();
    if (activeExport.current !== null) {
      const message = '导出正在进行中，请稍后再试。';
      setExportError(message);
      throw new Error(message);
    }
    const attempt = {};
    activeExport.current = attempt;
    const release = () => {
      if (activeExport.current !== attempt) return;
      activeExport.current = null;
      setBusy(null);
    };
    signal?.addEventListener('abort', release, { once: true });
    setBusy(format);
    setExportError('');
    try {
      const markerData = await ensureMarkers(signal);
      signal?.throwIfAborted();
      const name = candidateName || analysis.candidate_name || '候选人';
      const inner = buildReportInner({ analysis, candidateName, markers: markerData });
      if (format === 'png') {
        await captureImage(inner, signal);
      } else if (format === 'pdf') {
        const title = `候选人分析报告-${name}`;
        await downloadReportPdf(wrapDocument(inner, title), title, 'analysis', { signal });
      } else {
        downloadWord(wrapDocument(inner, `候选人分析报告-${name}`), name);
      }
    } catch (error) {
      signal?.throwIfAborted();
      if (error instanceof DOMException && error.name === 'AbortError') throw error;
      const message = error instanceof Error ? error.message : '导出失败，请稍后重试。';
      setExportError(message);
      throw error;
    } finally {
      signal?.removeEventListener('abort', release);
      release();
    }
  }

  useImperativeHandle(ref, () => ({ exportReport: handleExport }));

  const requestExport = (format: ExportFormat) => {
    if (onRequestExport) {
      onRequestExport(format);
      return;
    }
    void handleExport(format).catch(() => {
      /* 错误已写入 exportError */
    });
  };

  return (
    <>
      <div className="flex flex-wrap gap-1.5">
        <button
          type="button"
          onClick={() => requestExport('pdf')}
          disabled={busy !== null}
          className={`${BUTTON_CLASS} bg-ink text-white hover:bg-brand-hover`}
        >
          {busy === 'pdf' ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <FileTextIcon className="size-3.5" />}
          {busy === 'pdf' ? '正在导出 PDF…' : '导出 PDF'}
        </button>
        <button
          type="button"
          onClick={() => requestExport('png')}
          disabled={busy !== null}
          className={`${BUTTON_CLASS} border border-line-strong bg-white text-ink hover:bg-mist`}
        >
          {busy === 'png' ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <CameraIcon className="size-3.5" />}
          {busy === 'png' ? '正在导出图片…' : '导出图片'}
        </button>
        <button
          type="button"
          onClick={() => requestExport('doc')}
          disabled={busy !== null}
          className={`${BUTTON_CLASS} border border-line-strong bg-white text-ink hover:bg-mist`}
        >
          {busy === 'doc' ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <DownloadIcon className="size-3.5" />}
          {busy === 'doc' ? '正在导出 Word…' : '导出 Word'}
        </button>
      </div>
      {exportError && <p role="alert" className="mt-2 text-xs text-bad">{exportError}</p>}
    </>
  );
});

export default ReportExportCenter;
