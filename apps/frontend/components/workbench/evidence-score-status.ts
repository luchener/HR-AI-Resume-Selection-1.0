import type { HrAnalysis } from './analysis-context';

export function isResumeJdEvidenceMatch(analysis: HrAnalysis): boolean {
  return analysis.scoring_meta?.score_semantics === 'resume_jd_evidence_match';
}

export function evidenceDecisionPending(analysis: HrAnalysis): boolean {
  const meta = analysis.scoring_meta;
  if (isResumeJdEvidenceMatch(analysis)) {
    return !!(analysis.analysis_incomplete || meta?.analysis_incomplete || meta?.incomplete || meta?.provisional || meta?.decision_ready === false);
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
  return evidenceDecisionPending(analysis) ? '证据不足，待核实' : analysis.recruitment_recommendation;
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
