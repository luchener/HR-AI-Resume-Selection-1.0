import type { HrAnalysis } from './analysis-context';
import type { EmploymentRecord } from './analysis-context';

export function isResumeJdEvidenceMatch(analysis: HrAnalysis): boolean {
  return analysis.scoring_meta?.score_semantics === 'resume_jd_evidence_match';
}

export function evidenceDecisionPending(analysis: HrAnalysis): boolean {
  const meta = analysis.scoring_meta;
  if (analysis.analysis_incomplete || analysis.recruitment_recommendation === '待补充材料') return true;
  if (isResumeJdEvidenceMatch(analysis)) {
    return !!(meta?.analysis_incomplete || meta?.incomplete || meta?.provisional || meta?.decision_ready === false);
  }
  return !!meta && (meta.incomplete || !meta.decision_ready);
}

export function evidenceScoreProvisional(analysis: HrAnalysis): boolean {
  if (isResumeJdEvidenceMatch(analysis)) return false;
  return !!analysis.scoring_meta && (analysis.scoring_meta.provisional || evidenceDecisionPending(analysis));
}

export function evidenceScoreLabel(analysis: HrAnalysis, legacyLabel = '综合得分'): string {
  if (isResumeJdEvidenceMatch(analysis)) return '简历与JD证据匹配分';
  return `${legacyLabel}${evidenceScoreProvisional(analysis) ? ' · 暂估' : ''}`;
}

export function evidenceRecommendation(analysis: HrAnalysis): string {
  return evidenceDecisionPending(analysis)
    ? '待补充材料'
    : analysis.hiring_recommendation || analysis.recruitment_recommendation || hiringRecommendationForScore(analysis.final_score);
}

const HIRING_OUTCOME_PHRASES = [
  /优先\s*面试|储备\s*观察|不(?:建议|推荐)(?:安排)?(?:面试|复试|录用|继续推进|进入下一轮)|(?:不予|暂缓|直接)(?:录用|淘汰)|不(?:进入下一轮|通过(?:初筛|筛选))/u,
  /(?:建议|推荐|应当?|可以?|宜)\s*(?:(?:将|对)(?:该)?候选人\s*)?(?:安排(?:面试|复试)|进入(?:面试|复试|下一轮)|继续推进|(?:予以)?(?:录用|淘汰|面试|复试)(?=$|[\s。！？.!?；;，,”」】（(*]|(?:该)?候选人|出局))/u,
  /(?:^|[：:“「【*\s])(?:淘汰|录用)(?=$|[\s。！？.!?；;，,”」】（(*])/u,
  /\b(?:do not proceed|not recommended for (?:an? )?(?:interview|hire)|(?:should|must) be (?:rejected|hired)|(?:recommend(?:ed)?\s+)(?:rejection|rejecting|hiring|an? interview|interviewing)|(?:advance|proceed) (?:to|with) (?:an? |the )?(?:interview|next round))\b/i,
];

function evidenceOnlySummary(summary: string): string {
  // Keep factual clauses when a model adds an outcome before or after them.
  const clauses = summary.split(/(?<=[。！？!?；;，,\n]|\.(?=\s|$))/u);
  const retained = clauses.filter((clause) => !HIRING_OUTCOME_PHRASES.some((pattern) => pattern.test(clause)));
  if (retained.length === clauses.length) return summary;
  const ending = summary.match(/[。！？.!?]$/u)?.[0] ?? '';
  return retained.join('').trim().replace(/[，,；;]$/u, ending);
}

export function reportCoreVerdict(analysis: HrAnalysis): { summary: string; notice: string } {
  const pending = evidenceDecisionPending(analysis);
  const summary = [analysis.core_verdict, analysis.summary]
    .map((value) => value?.trim() || '')
    .filter((value) => value && !value.startsWith('评分仅衡量'))
    .map((value) => pending ? evidenceOnlySummary(value) : value)
    .find(Boolean);
  const requirements = analysis.requirements_checklist ?? [];
  const matched = requirements.find((item) => (item.effective_status || item.status) === 'met' && item.text?.trim());
  const conflict = requirements.find((item) => (item.effective_status || item.status) === 'not_met' && item.text?.trim());
  const missing = requirements.find((item) => ['manual_review', 'not_mentioned'].includes(item.effective_status || item.status) && item.text?.trim());
  const evidenceSummary = [
    matched && `已核对符合：${matched.text.trim()}`,
    conflict && `已核对不符合：${conflict.text.trim()}`,
    missing && `待核实：${missing.text.trim()}`,
  ].filter(Boolean).join('；');
  const verdict = summary || evidenceSummary;
  const notice = pending
    ? analysis.analysis_incomplete_reason?.trim() || '现有证据不足，请补充材料后复核。'
    : '';
  return { summary: verdict || notice || analysis.summary || '', notice: verdict && verdict !== notice ? notice : '' };
}

export function evidenceNormalizationSummary(analysis: HrAnalysis): string {
  const meta = analysis.scoring_meta;
  if (!meta || typeof meta.applicable_max !== 'number' || typeof meta.raw_points !== 'number') return '';
  if (meta.applicable_max === 0) return '岗位要求中没有可从简历原文核对的计分项，当前不能形成匹配结论。';
  return `当前得分按已核对的岗位要求计算，共核对到 ${meta.raw_points} / ${meta.applicable_max} 的匹配证据；未写明的要求不计入得分，也不代表候选人不具备。`;
}

export function evidenceScoreSummary(analysis: HrAnalysis): string {
  const meta = analysis.scoring_meta;
  if (!meta) return '';
  const percentage = (ratio: number | undefined) => typeof ratio === 'number' && Number.isFinite(ratio) ? `${Math.round(ratio * 100)}%` : '未提供';
  if (isResumeJdEvidenceMatch(analysis)) {
    return `已核对 ${percentage(meta.evidence_coverage)} · 尚需补充 ${percentage(meta.unknown_ratio)}`;
  }
  const range = meta.score_range && Number.isFinite(meta.score_range.min) && Number.isFinite(meta.score_range.max)
    ? `证据基础分区间 ${meta.score_range.min}–${meta.score_range.max} / 100`
    : typeof meta.base_score === 'number' ? `基础分 ${meta.base_score} / 100` : '基础分待确认';
  return `${range} · 已核对 ${percentage(meta.evidence_coverage)} · 尚需补充 ${percentage(meta.unknown_ratio)}`;
}

/** 顶部指标只展示候选人事实；简历未写明相关经验时直接给结论，不展示模型的括号解释。 */
export function relevantExperienceLabel(value?: string | null): string {
  const text = (value || '').trim();
  if (!text) return '无相关行业工作经验';
  if (/未提供|未体现|未提及|无相关|没有相关/.test(text)) return '无相关行业工作经验';
  return text;
}

/** 招聘建议三档的分数口径（与后端 hr_normalization 的 score_recommendation 一致）。 */
export const HIRING_BAND_NOTE = '≥80 优先面试 · 60–79 储备观察 · <60 淘汰';

export function hiringRecommendationForScore(score: number | undefined | null): '优先面试' | '储备观察' | '淘汰' {
  const value = typeof score === 'number' && Number.isFinite(score) ? score : 0;
  return value >= 80 ? '优先面试' : value >= 60 ? '储备观察' : '淘汰';
}

/** 岗位相关经验：按岗位名称汇总任职年限，例如「8年后端开发，5年运维」。
 *  没有可计算经历时回退到总工作年限；总年限也缺失时才显示无相关经验。 */
export function relevantExperienceSummary(
  records?: EmploymentRecord[] | null,
  totalYears?: string | null,
): string {
  const cleanTitle = (value: string) => value
    .replace(/[（(][^）)]*[）)]/g, '')
    .replace(/[，,。;；].*$/, '')
    .trim();
  const yearsOf = (record: EmploymentRecord) => {
    const duration = String(record.duration || '');
    if (!/未提供|未体现|未知/.test(duration)) {
      const years = duration.match(/(\d+(?:\.\d+)?)\s*年/);
      const months = duration.match(/(\d+)\s*个月/);
      if (years) return Number(years[1]) + (months ? Number(months[1]) / 12 : 0);
    }
    const start = parseInt(String(record.start_date || ''), 10);
    const end = parseInt(String(record.end_date || ''), 10);
    if (Number.isFinite(start) && Number.isFinite(end) && end > start) return end - start;
    return 0;
  };
  const totals = new Map<string, number>();
  for (const record of records || []) {
    const title = cleanTitle(String(record.job_title || ''));
    const years = yearsOf(record);
    if (!title || years <= 0) continue;
    totals.set(title, (totals.get(title) || 0) + years);
  }
  const rows = [...totals.entries()]
    .map(([title, years]) => ({ title, years: Math.round(years) }))
    .filter((row) => row.years > 0)
    .sort((a, b) => b.years - a.years)
    .slice(0, 3);
  if (rows.length > 0) return rows.map((row) => `${row.years}年${row.title}`).join('，');
  const fallback = String(totalYears || '').trim();
  if (fallback && !/未提供|未体现|未提及|未知|无相关/.test(fallback)) {
    return `总工作年限 ${fallback}`;
  }
  return '无相关行业工作经验';
}

export function evidenceScoreBasis(analysis: HrAnalysis): string {
  const meta = analysis.scoring_meta;
  if (!meta) return `${analysis.job_fit_percentage}% 基础契合度`;
  if (isResumeJdEvidenceMatch(analysis)) return `只按简历中已明确写出的匹配内容计分 · 当前 ${meta.base_score} 分`;
  return `基础分 ${meta.base_score} · 暂估部分 ${meta.provisional_points} 分`;
}

export function evidenceScoreNotice(analysis: HrAnalysis): string {
  const meta = analysis.scoring_meta;
  if (!meta) return '';
  if (isResumeJdEvidenceMatch(analysis)) {
    return `${evidenceNormalizationSummary(analysis)} 这不是能力百分比，只表示简历中明确写出的岗位匹配内容。未写明不等于不会，但当前不能据此确认；请补充材料后再决定。`;
  }
  const normalization = evidenceNormalizationSummary(analysis);
  const basis = `${normalization ? `${normalization} ` : ''}服务端按证据四态评分：${normalization && meta.applicable_max !== 0 ? '归一' : ''}基础分 ${meta.base_score} / 100，已获证据支持 ${meta.supported_score} 分，暂估部分 ${meta.provisional_points} 分。`;
  if (evidenceDecisionPending(analysis)) return `${basis}目前证据不足，不能直接录用或淘汰；请补充材料后复核。`;
  return `${basis}${evidenceScoreProvisional(analysis) ? '仍含待确认部分；区间处于同一决策档位，建议结合证据复核。' : '请结合逐条证据与岗位要求复核。'}`;
}
