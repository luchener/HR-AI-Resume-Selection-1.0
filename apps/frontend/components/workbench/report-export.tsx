'use client';

import { useEffect, useRef, useState } from 'react';
import html2canvas from 'html2canvas';
import { CameraIcon, DownloadIcon, FileTextIcon, LoaderCircleIcon } from 'lucide-react';
import type { HrAnalysis } from './analysis-context';
import { evidenceDecisionPending, evidenceRecommendation, evidenceScoreBasis, evidenceScoreLabel, evidenceScoreNotice, evidenceScoreSummary, isResumeJdEvidenceMatch } from './evidence-score-status';
import { agentValidationNeedsReview, agentValidationStatus } from './agent-validation-status';
import { fetchResumeReviewMarkers, type ResumeReviewData } from '@/lib/api/screening';

export const AGENT_RULE_LABELS = ['', '论断超出简历依据', '评分依据不可追溯', '加分项与岗位无关', '缺失项标注"未提供"', '风险与未体现混淆'];

const MARKER_LABELS: Record<string, string> = {
  strength: '匹配亮点',
  match: '岗位匹配',
  risk: '待核实',
  missing: '未体现',
  verify: '学历待核实',
};

/** 硬门槛档位（与后端 _GATE_TIER_POLICY 对应）：A 学历层级 / B 岗位硬门槛 / C 生活类 */
const TIER_LABELS: Record<string, string> = {
  A: '学历层级',
  B: '岗位硬门槛',
  C: '生活类要求',
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
.empty{color:#8190a4}
.notice{margin-top:18px;font-size:10px;color:#8190a4}
@media print{.page{width:auto;padding:10px 12px}}
`;

function buildReportInner(opts: {
  analysis: HrAnalysis;
  candidateName: string;
  markers: ResumeReviewData | null;
}): string {
  const { analysis: a, markers } = opts;
  const name = opts.candidateName || a.candidate_name || '候选人';
  const out: string[] = [];
  const scoreLabel = evidenceScoreLabel(a);
  const evidenceMatch = isResumeJdEvidenceMatch(a);
  const pending = evidenceDecisionPending(a);
  const recommendation = evidenceRecommendation(a);
  const fitTag = pending ? '待确认' : a.fit_tag;
  const fitGrade = pending ? '待确认' : a.fit_grade;

  out.push(`
<h1>候选人分析报告 · ${esc(name)}</h1>
<div class="meta">
  <span>生成日期：${new Date().toISOString().slice(0, 10)}</span>
  <span>${esc(scoreLabel)}：<b>${esc(a.final_score)}</b>/100（${esc(fitGrade)}）</span>
  <span>招聘建议：${esc(recommendation)} · ${esc(fitTag)}</span>
</div>`);

  if (a.scoring_meta) {
    out.push(`<p class="para">${esc(evidenceScoreSummary(a))}</p><p class="para">${esc(evidenceScoreNotice(a))}</p>`);
  }

  out.push(`
<h2>评估总览</h2>
<table>
  <tr>
    <td class="lab">${esc(scoreLabel)}</td><td class="lab">${a.scoring_meta ? '证据支持分' : '能力匹配度'}</td><td class="lab">简历美化程度</td><td class="lab">招聘建议</td>
  </tr>
  <tr>
    <td class="val"><b class="green">${esc(a.final_score)}</b><br><span class="sub">${esc(fitGrade)}</span></td>
    <td class="val"><b class="blue">${esc(a.scoring_meta?.supported_score ?? a.capability_score ?? a.final_score)} / 100</b><br><span class="sub">${esc(evidenceScoreBasis(a))}${a.ai_deduction > 0 ? ` · AI 美化扣 ${esc(a.ai_deduction)}` : ''}${a.hard_gate_deduction ? ` · 硬门槛扣 ${esc(a.hard_gate_deduction)}` : ''}${a.gate_cap_applied ? ` · 封顶 ${esc(a.gate_cap_applied)}` : ''}</span></td>
    <td class="val"><b class="purple">${esc(a.ai_risk_level)}</b><br><span class="sub">${esc(a.ai_risk_label)}${
      a.ai_deduction > 0
        ? ` · 扣 ${esc(a.ai_deduction)} 分`
        : a.ai_deduction_suggested
          ? ' · 仅风险提示，未扣分'
          : ''
    }</span></td>
    <td class="val"><b>${esc(recommendation)}</b><br><span class="sub">${esc(fitTag)}</span></td>
  </tr>
</table>`);

  if (!evidenceMatch && !pending && a.override_review) {
    out.push(`
<h2>建议破格复核</h2>
<p class="para">能力匹配 <b>${esc(a.capability_score)}</b> 分，只被硬门槛卡住，最终得分封顶至 <b>${esc(a.final_score)}</b> 分。系统不据此放宽分数，也不将其归入淘汰——请人工确认是否值得破格进入面试。</p>
<p class="sub">能力分口径：${a.capability_basis === 'total' ? '模型未给出分项，退回模型总分' : '已剔除模型对硬门槛的打分，避免同一缺口被扣两次'}</p>
${(a.gate_blockers || []).length > 0 ? `<table>
  <thead><tr><th style="width:140px">门槛档位</th><th>岗位要求</th><th>简历证据</th></tr></thead>
  <tbody>
    ${(a.gate_blockers || []).map((b) => `
    <tr>
      <td><span class="tag">${esc(TIER_LABELS[b.tier] || b.tier || '硬门槛')}</span></td>
      <td>${esc(b.text)}</td>
      <td class="red">${esc(b.evidence_label)}${b.basis ? `<br><span class="sub">${esc(b.basis)}</span>` : ''}</td>
    </tr>`).join('')}
  </tbody>
</table>` : ''}`);
  } else if (!evidenceMatch && !pending && (a.non_mitigable_blockers || []).length > 0 && a.final_score < 60) {
    out.push(`
<h2>不支持破格复核</h2>
<p class="para">能力匹配 <b>${esc(a.capability_score ?? a.final_score)}</b> 分，但被<b>不能由能力弥补</b>的门槛卡住，因此不给"破格复核"结论：</p>
${bullets((a.non_mitigable_blockers || []).map((b) => b.text))}
<p class="sub">这类门槛通常意味着岗位工作本身要求持证或满足事实条件（法规执业资格、驾照、届别等），破格等于违规上岗——请按制度处理。</p>`);
  }

  out.push(`<h2>核心判定</h2><p class="para">${esc(a.summary)}</p>`);

  // 岗位要求逐条判定：导出**只逐条列出「符合」的条目**。
  // 不符合 / 未写明 / 待人工确认一次能到二三十条，逐条铺开太占版面；而且不符合已经通过
  // 硬门槛扣分与封顶体现在「评估总览」的分数里，不在这里再重复一遍。
  // 但"缺哪几条"不能丢：不符合的条目名合并成一行列出（没有判定/依据，只留要求本身），
  // 报告被转发给用人部门时还看得出缺什么。
  // 与界面上那份是互补分工：屏幕按"需要处理的优先"（默认只显示不符合 + 待人工确认，可展开全部），
  // 导出按"够格在哪"（表格只列符合，其余压缩成几行）。
  const checklist = a.requirements_checklist || [];
  const undetermined = a.undetermined_requirements || [];
  if (checklist.length > 0) {
      const statusOf = (item: (typeof checklist)[number]) => item.effective_status || item.status;
    const countOf = (status: string) => checklist.filter((item) => statusOf(item) === status).length;
    const metItems = checklist.filter((item) => statusOf(item) === 'met');
    const notMetItems = checklist.filter((item) => statusOf(item) === 'not_met');
    const restParts = [
      countOf('not_met') > 0 ? `不符合 ${countOf('not_met')}` : '',
      countOf('manual_review') > 0 ? `待人工确认 ${countOf('manual_review')}` : '',
      countOf('not_mentioned') > 0 ? `未写明 ${countOf('not_mentioned')}` : '',
    ].filter(Boolean);
    out.push(`
<h2>岗位要求逐条判定</h2>
<p class="sub">共 ${checklist.length} 条：符合 ${metItems.length}${restParts.length > 0 ? ` · ${restParts.join(' · ')}` : ''}。
下表只逐条列出「符合」的条目${restParts.length > 0 ? '；其余条目以计数和摘要保留。' : ''}</p>
<p class="sub">四态口径：符合＝有对应证据；不符合＝有明确冲突；未写明＝简历未体现，不等于不符合；待人工确认＝证据不足或需核实。相关技术线索只说明有关联，不能直接认定熟练。</p>
${
  notMetItems.length > 0
    ? `<p class="sub"><b class="red">不符合（${notMetItems.length} 条）：</b>${notMetItems.map((item) => esc(item.text)).join('、')}</p>`
    : ''
}
${
  metItems.length > 0
    ? `<table>
  <thead><tr><th style="width:96px">档位</th><th>岗位要求</th><th>依据</th></tr></thead>
  <tbody>
    ${metItems.map((item) => `
    <tr>
      <td><span class="tag">${esc(TIER_LABELS[item.gate_tier || ''] || '—')}</span></td>
      <td>${esc(item.text)}</td>
      <td class="sub">${item.resume_basis ? esc(item.resume_basis) : '—'}</td>
    </tr>`).join('')}
  </tbody>
</table>`
    : '<p class="sub">本次没有判定为「符合」的条目。</p>'
}`);
  }
  if (undetermined.length > 0) {
    out.push(`
<h2>系统不判定，请人工确认</h2>
${bullets(undetermined.map((entry) => `${entry.type}：${entry.text}${entry.note ? `（${entry.note}）` : ''}`))}
<p class="sub">这类要求（学历形式、年龄、出差意愿等）机器无法从简历确认，系统不替 HR 下结论，也不据此扣分。</p>`);
  }

  // 真实性核验（只读）：文件里没有折叠按钮，所以这里全量导出，并保留 notice 的定性说明。
  // 标题不带"（只读，不参与评分）"——只读这件事由下方口径行说明，标题保持干净。
  // 0 命中时整段不输出：文件里没有"展开"按钮，「核验规则全部跑完，未发现…」这种空结论
  // 对读者只有噪音（界面上那行绿字仍然保留，信息不丢）。
  const credibility = a.credibility;
  const credibilityFindings = credibility?.findings || [];
  if (credibility?.enabled && credibilityFindings.length > 0) {
    const levelLabels: Record<string, string> = { red_flag: '需优先核实', review: '待核实', info: '提示' };
    const srcLabels: Record<string, string> = { resume: '简历原文', report: '报告表述' };
    const items = credibilityFindings;
    const counts = credibility.counts || { red_flag: 0, review: 0, info: 0 };
    out.push(`
<h2>真实性核验</h2>
<p class="sub">${esc(credibility.notice || '核验结果只用于提示，不构成对候选人的任何结论。')}
已跑 ${esc(String(credibility.checks_run))} 条规则：需优先核实 ${counts.red_flag} · 待核实 ${counts.review} · 提示 ${counts.info}。</p>
${
  items
    .map(
      (item) => `
<p><span class="tag">${esc(levelLabels[item.level] || item.level)}</span> <b>${esc(item.title)}</b>
<span class="sub">（${esc(srcLabels[String(item.source || 'report')] || '报告表述')}）</span><br>
${esc(item.detail)}<br>
<span class="sub">证据：${esc(item.evidence)}</span>${
        item.quote ? `<br><span class="sub">原文引用：${esc(item.quote)}</span>` : ''
      }<br>
<span class="sub">面试该问：${esc(item.ask)}</span></p>`,
    )
    .join('')
}
<p class="sub">核验口径：${esc(credibility.version)}（只读字段，不参与分数与等级）</p>`);
  }

  // 无问题时不再输出"已核查 N 项要求，全部通过"这种空结论：导出的报告是给 HR 与用人部门看的，
  // 没有检出问题就不占版面（界面上那行绿字仍然保留）。
  const av = a.agent_validation;
  if (av && agentValidationNeedsReview(av)) {
    out.push(`
<h2>Agent 校验</h2>
<p class="sub">${esc(agentValidationStatus(av))}</p>
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
    out.push(
      `<h2>美化程度判断依据</h2>` +
        (a.ai_deduction > 0
          ? ''
          : `<p class="sub">以下是模型给出的“疑似 AI 包装”语言特征。该项当前<strong>不参与扣分</strong>——风格印象没有可核对的对象，是否采信请 HR 自行判断。</p>`) +
        bullets(a.deduction_reasons),
    );
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
  const captureHtml = `<style>${DOC_STYLE}</style><div class="page">${inner}</div>`;

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

export default function ReportExportCenter({
  analysis,
  candidateName,
  resumeId,
}: {
  analysis: HrAnalysis;
  candidateName: string;
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
      const inner = buildReportInner({ analysis, candidateName, markers: markerData });
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
      .then((canvas) => canvas.toBlob((img) => {
        if (!img) return;
        const url = URL.createObjectURL(img);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `候选人分析报告-${name}.jpg`;
        anchor.click();
        URL.revokeObjectURL(url);
      }, 'image/jpeg', 0.9))
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
