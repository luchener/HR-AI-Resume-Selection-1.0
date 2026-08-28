'use client';

import { useEffect, useRef, useState } from 'react';
import html2canvas from 'html2canvas';
import { CameraIcon, DownloadIcon, FileTextIcon, LoaderCircleIcon } from 'lucide-react';
import type { CandidateComparison, HrAnalysis } from './analysis-context';
import { fetchResumeReviewMarkers, type ResumeReviewData } from '@/lib/api/screening';

export const AGENT_RULE_LABELS = ['', '论断超出简历依据', '评分依据不可追溯', '加分项与岗位无关', '缺失项标注"未提供"', '风险与未体现混淆'];

const MARKER_LABELS: Record<string, string> = {
  strength: '匹配亮点',
  match: '岗位匹配',
  risk: '待核实',
  missing: '未体现',
  verify: '学历待核实',
};

type ExportFormat = 'png' | 'pdf' | 'doc';

function esc(value: unknown): string {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function bullets(items: unknown): string {
  const clean = Array.isArray(items) ? items.map((item) => String(item || '').trim()).filter(Boolean) : [];
  if (!clean.length) return '<p class="empty">未提供</p>';
  return `<ul>${clean.map((item) => `<li>${esc(item)}</li>`).join('')}</ul>`;
}

// 布局只用 table/h*/p/ul，保证 Word（.doc HTML）渲染不塌陷
const DOC_STYLE = `
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;color:#2c394f;font-size:13px;line-height:1.7;background:#ffffff}
.page{width:800px;margin:0 auto;padding:32px 36px;background:#ffffff}
h1{font-size:20px;color:#253249;margin-bottom:6px}
h2{font-size:14px;color:#3e6fd3;margin:20px 0 8px;border-bottom:1px solid #dce2eb;padding-bottom:4px}
h3{font-size:13px;color:#29364c;margin:12px 0 4px}
.meta{margin:6px 0 4px;font-size:12px;color:#65738a}
.meta span{margin-right:16px}
.meta b{color:#1d7f5c;font-size:18px}
table{border-collapse:collapse;width:100%;margin:6px 0 10px;font-size:12px}
th,td{border:1px solid #dce2eb;padding:6px 9px;text-align:left;vertical-align:top}
th{background:#eaf0fb;color:#3e6fd3;font-weight:600;font-size:11px}
ul{margin:4px 0 8px 18px}
li{margin-bottom:3px}
p.para{margin:4px 0;font-size:13px;line-height:1.8}
td.lab{background:#f7f8fa;color:#66758b;font-size:11px;width:90px}
td.val{text-align:center;width:25%}
b.green,.green{color:#1d7f5c}
.blue{color:#3e6fd3}
.purple{color:#995c87}
.red{color:#b23b4e}
.sub{color:#65738a;font-size:11px}
.tag{display:inline-block;background:#fef6e6;color:#b0761a;border-radius:2px;padding:1px 6px;font-size:10px}
.callout{margin:6px 0;font-size:13px;font-weight:600;color:#1d7f5c}
.empty{color:#8190a4}
.notice{margin-top:18px;font-size:10px;color:#8190a4}
@media print{.page{width:auto;padding:10px 12px}}
`;

function buildReportInner(opts: {
  analysis: HrAnalysis;
  candidateName: string;
  comparison?: CandidateComparison | null;
  markers: ResumeReviewData | null;
}): string {
  const { analysis: a, comparison, markers } = opts;
  const name = opts.candidateName || a.candidate_name || '候选人';
  const out: string[] = [];

  out.push(`
<h1>候选人分析报告 · ${esc(name)}</h1>
<div class="meta">
  <span>生成日期：${new Date().toISOString().slice(0, 10)}</span>
  <span>综合得分：<b>${esc(a.final_score)}</b>/100（${esc(a.fit_grade)}）</span>
  <span>招聘建议：${esc(a.recruitment_recommendation)} · ${esc(a.fit_tag)}</span>
</div>`);

  out.push(`
<h2>评估总览</h2>
<table>
  <tr>
    <td class="lab">综合得分</td><td class="lab">岗位契合度</td><td class="lab">简历美化程度</td><td class="lab">招聘建议</td>
  </tr>
  <tr>
    <td class="val"><b class="green">${esc(a.final_score)}</b><br><span class="sub">${esc(a.fit_grade)}</span></td>
    <td class="val"><b class="blue">${esc(a.job_fit_percentage)}%</b><br><span class="sub">基础分 ${esc(a.job_fit_score)}/100</span></td>
    <td class="val"><b class="purple">${esc(a.ai_risk_level)}</b><br><span class="sub">${esc(a.ai_risk_label)} · 扣 ${esc(a.ai_deduction)} 分</span></td>
    <td class="val"><b>${esc(a.recruitment_recommendation)}</b><br><span class="sub">${esc(a.fit_tag)}</span></td>
  </tr>
</table>`);

  if (comparison && Array.isArray(comparison.ranking) && comparison.ranking.length > 0) {
    const medals = ['🥇', '🥈', '🥉'];
    out.push(`
<h2>候选人排名</h2>
<table>
  <thead><tr><th style="width:56px">排名</th><th style="width:64px">得分</th><th style="width:120px">候选人</th><th>核心差异点</th></tr></thead>
  <tbody>
    ${comparison.ranking.map((item) => `
    <tr>
      <td><b>${item.rank <= 3 ? medals[item.rank - 1] : esc(item.rank)}</b></td>
      <td><b class="blue">${esc(item.score)}</b></td>
      <td>${esc(item.name)}</td>
      <td class="sub">${esc(item.difference)}</td>
    </tr>`).join('')}
  </tbody>
</table>
${comparison.recommendation ? `<p class="callout">${esc(comparison.recommendation)}</p>` : ''}
${comparison.pairwise?.length ? `<ul>${comparison.pairwise.map((item) => `<li class="sub">${esc(item)}</li>`).join('')}</ul>` : ''}`);
  }

  out.push(`<h2>核心判定</h2><p class="para">${esc(a.summary)}</p>`);

  const av = a.agent_validation;
  if (av && av.issues.length > 0) {
    out.push(`
<h2>Agent 校验</h2>
<p class="sub">已校验 ${esc(av.checked_rules)} 项要求，检出 ${av.issues.length} 个问题${av.revised ? '并已修正' : ''}</p>
<table>
  <thead><tr><th style="width:140px">问题类型</th><th>问题</th><th>修正</th></tr></thead>
  <tbody>
    ${av.issues.map((issue) => `
    <tr>
      <td><span class="tag">${esc(AGENT_RULE_LABELS[issue.rule] || '')}</span></td>
      <td class="red">${esc(issue.problem)}</td>
      <td class="green">${esc(issue.fix)}</td>
    </tr>`).join('')}
  </tbody>
</table>`);
  }

  const bs = a.basic_screening;
  out.push(`
<h2>基础信息筛选</h2>
<table>
  <tr><td class="lab">姓名</td><td>${esc(name)}</td><td class="lab">性别</td><td>${esc(bs.gender)}</td><td class="lab">年龄</td><td>${esc(bs.age)}</td></tr>
  <tr><td class="lab">籍贯</td><td>${esc(bs.native_place)}</td><td class="lab">工作所在地</td><td>${esc(bs.work_location)}</td><td class="lab">期望薪资</td><td>${esc(bs.salary_expectation)}</td></tr>
</table>`);

  if (a.education_history?.length > 0) {
    out.push(`
<h2>教育经历</h2>
<table>
  <thead><tr><th style="width:64px">学历</th><th>院校</th><th style="width:100px">层次</th><th>专业</th><th style="width:80px">毕业时间</th></tr></thead>
  <tbody>
    ${a.education_history.map((edu) => `
    <tr><td><b>${esc(edu.degree)}</b></td><td>${esc(edu.school_name)}</td><td>${esc(edu.school_tier)}</td><td>${esc(edu.major)}</td><td>${esc(edu.graduation_year)}</td></tr>`).join('')}
  </tbody>
</table>`);
  }

  const wh = a.work_history;
  out.push(`
<h2>工作履历</h2>
<table>
  <tr><td class="lab">总工作年限</td><td>${esc(wh.total_years)}</td><td class="lab">相关岗位年限</td><td>${esc(wh.relevant_years)}</td></tr>
  <tr><td class="lab">职责重合度</td><td>${esc(wh.responsibility_match)}</td><td class="lab">行业匹配</td><td>${esc(wh.industry_match)}</td></tr>
  <tr><td class="lab">公司背景</td><td>${esc(wh.company_background)}</td><td class="lab">岗位层级</td><td>${esc(wh.seniority)}</td></tr>
  <tr><td class="lab">带人规模</td><td>${esc(wh.team_size)}</td><td class="lab">跳槽稳定性</td><td>${esc(wh.stability)}</td></tr>
  <tr><td class="lab">空窗期核算</td><td colspan="3">${esc(wh.employment_gaps)}</td></tr>
</table>`);

  const records = wh.employment_records || [];
  if (records.length > 0) {
    out.push(`
<table>
  <thead><tr><th>公司</th><th>岗位</th><th style="width:130px">起止</th><th style="width:80px">时长</th></tr></thead>
  <tbody>
    ${records.map((record) => `
    <tr><td>${esc(record.company_name)}</td><td>${esc(record.job_title)}</td><td>${esc(record.start_date)} ~ ${esc(record.end_date)}</td><td>${esc(record.duration)}</td></tr>`).join('')}
  </tbody>
</table>`);
  }

  out.push(`<h2>匹配亮点</h2>${bullets(a.strengths)}`);
  if (a.skill_match.project_match_points?.length > 0) {
    out.push(`<h3>项目匹配点</h3>${bullets(a.skill_match.project_match_points)}`);
  }
  out.push(`<h2>短板与风险</h2><h3>短板不足</h3>${bullets(a.weaknesses)}<h3>招聘风险预警</h3>${bullets(a.risk_points)}`);
  out.push(`<h2>专业技能匹配</h2><h3>硬技能与工具</h3>${bullets(a.skill_match.hard_skills)}<h3>软实力</h3>${bullets(a.skill_match.soft_skills)}`);
  out.push(`<h2>竞争力加分项</h2>${bullets(a.bonus_items)}<h3>证书资质</h3>${bullets(a.certificates)}`);
  if (a.role_specific_assessment?.length > 0) {
    out.push(`<h2>岗位定制判断</h2>${bullets(a.role_specific_assessment)}`);
  }
  if (a.deduction_reasons?.length > 0) {
    out.push(`<h2>美化程度判断依据</h2>${bullets(a.deduction_reasons)}`);
  }

  if (markers && markers.annotations?.length > 0) {
    out.push(`
<h2>简历标记要点</h2>
<p class="sub">匹配重点 ${esc(markers.summary.highlights)} 处 · 待核实 ${esc(markers.summary.risks)} 处${markers.summary.verify_count ? ` · 学历待核实 ${esc(markers.summary.verify_count)} 处` : ''}</p>
<table>
  <thead><tr><th style="width:90px">类型</th><th>引用原文</th><th style="width:32%">HR 说明</th></tr></thead>
  <tbody>
    ${markers.annotations.map((marker) => `
    <tr><td><b class="blue">${esc(MARKER_LABELS[marker.category] || marker.category)}</b></td><td>${esc(marker.quote)}</td><td class="sub">${esc(marker.reason)}</td></tr>`).join('')}
  </tbody>
</table>`);
  }

  out.push(`<p class="notice">本报告由 AI 简历筛选系统生成，Agent 已按 5 项规则自检；结论仅供招聘决策参考。</p>`);
  return out.join('\n');
}

function wrapDocument(inner: string, title: string): string {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>${esc(title)}</title><style>${DOC_STYLE}</style></head><body><div class="page">${inner}</div></body></html>`;
}

function printDocument(html: string) {
  const iframe = document.createElement('iframe');
  iframe.style.position = 'fixed';
  iframe.style.right = '0';
  iframe.style.bottom = '0';
  iframe.style.width = '0';
  iframe.style.height = '0';
  iframe.style.border = '0';
  document.body.appendChild(iframe);
  const doc = iframe.contentWindow?.document;
  if (!doc) {
    iframe.remove();
    return;
  }
  doc.open();
  doc.write(html);
  doc.close();
  const win = iframe.contentWindow;
  if (win) {
    win.focus();
    setTimeout(() => {
      win.print();
      setTimeout(() => iframe.remove(), 1000);
    }, 250);
  } else {
    iframe.remove();
  }
}

function downloadWord(html: string, name: string) {
  const blob = new Blob(['\ufeff', html], { type: 'application/msword;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `候选人分析报告-${name}.doc`;
  anchor.click();
  URL.revokeObjectURL(url);
}

const BUTTON_CLASS = 'inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium disabled:opacity-50';

export default function ReportExportCenter({
  analysis,
  candidateName,
  comparison,
  resumeId,
}: {
  analysis: HrAnalysis;
  candidateName: string;
  comparison?: CandidateComparison | null;
  resumeId: string;
}) {
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const [markers, setMarkers] = useState<ResumeReviewData | null>(null);
  const [captureHtml, setCaptureHtml] = useState('');
  const captureRef = useRef<HTMLDivElement>(null);

  // 标记数据懒加载：首次导出才请求，失败不阻断报告导出
  async function ensureMarkers(): Promise<ResumeReviewData | null> {
    if (markers) return markers;
    try {
      const data = await fetchResumeReviewMarkers(resumeId, analysis as unknown as Record<string, unknown>, candidateName);
      setMarkers(data);
      return data;
    } catch {
      return null;
    }
  }

  async function handleExport(format: ExportFormat) {
    if (busy) return;
    setBusy(format);
    try {
      const markerData = await ensureMarkers();
      const name = candidateName || analysis.candidate_name || '候选人';
      const inner = buildReportInner({ analysis, candidateName, comparison, markers: markerData });
      if (format === 'png') {
        setCaptureHtml(`<style>${DOC_STYLE}</style><div class="page">${inner}</div>`);
        return; // busy 在截图完成后释放
      }
      if (format === 'pdf') {
        printDocument(wrapDocument(inner, `候选人分析报告-${name}`));
      } else {
        downloadWord(wrapDocument(inner, `候选人分析报告-${name}`), name);
      }
    } finally {
      if (format !== 'png') setBusy(null);
    }
  }

  useEffect(() => {
    if (!captureHtml || !captureRef.current) return;
    const name = candidateName || analysis.candidate_name || '候选人';
    html2canvas(captureRef.current, { scale: 2, backgroundColor: '#ffffff', useCORS: true, logging: false })
      .then((canvas) => canvas.toBlob((png) => {
        if (!png) return;
        const url = URL.createObjectURL(png);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `候选人分析报告-${name}.png`;
        anchor.click();
        URL.revokeObjectURL(url);
      }, 'image/png'))
      .catch(() => printDocument(captureHtml)) // 截图失败回退打印对话框
      .finally(() => {
        setCaptureHtml(''); // 清空以卸载隐藏容器，并保证下次导出能重新触发
        setBusy(null);
      });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [captureHtml]);

  return (
    <>
      <div className="flex flex-wrap gap-1.5">
        <button
          type="button"
          onClick={() => handleExport('png')}
          disabled={busy !== null}
          className={`${BUTTON_CLASS} bg-[#3e6fd3] text-white hover:bg-[#2f5cb8]`}
        >
          {busy === 'png' ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <CameraIcon className="size-3.5" />}
          导出图片
        </button>
        <button
          type="button"
          onClick={() => handleExport('pdf')}
          disabled={busy !== null}
          className={`${BUTTON_CLASS} border border-[#d5dde9] bg-white text-[#2c394f] hover:bg-[#f3f6fa]`}
        >
          {busy === 'pdf' ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <FileTextIcon className="size-3.5" />}
          打印 PDF
        </button>
        <button
          type="button"
          onClick={() => handleExport('doc')}
          disabled={busy !== null}
          className={`${BUTTON_CLASS} border border-[#d5dde9] bg-white text-[#2c394f] hover:bg-[#f3f6fa]`}
        >
          {busy === 'doc' ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <DownloadIcon className="size-3.5" />}
          导出 Word
        </button>
      </div>
      {captureHtml && (
        <div
          ref={captureRef}
          style={{ position: 'fixed', left: -99999, top: 0, width: 800, backgroundColor: '#ffffff' }}
          dangerouslySetInnerHTML={{ __html: captureHtml }}
        />
      )}
    </>
  );
}
