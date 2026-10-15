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

export function evidenceRecommendation(analysis: HrAnalysis): HrAnalysis['recruitment_recommendation'] {
  return evidenceDecisionPending(analysis) ? '待补充材料' : analysis.recruitment_recommendation;
}

export function evidenceNormalizationSummary(analysis: HrAnalysis): string {
  const meta = analysis.scoring_meta;
  if (!meta || typeof meta.applicable_max !== 'number' || typeof meta.raw_points !== 'number') return '';
  if (meta.applicable_max === 0) return 'JD 无适用计分要求，相关维度为 0（不适用），基础分暂不作匹配结论。';
  return `适用维度原始分合计 ${meta.raw_points} / ${meta.applicable_max} → 归一基础分 ${meta.base_score} / 100。五维分项保留原预算分；JD 无该维要求时为 0（不适用），分项合计不直接等于综合得分。`;
}

export function evidenceScoreSummary(analysis: HrAnalysis): string {
  const meta = analysis.scoring_meta;
  if (!meta) return '';
  const percentage = (ratio: number | undefined) => typeof ratio === 'number' && Number.isFinite(ratio) ? `${Math.round(ratio * 100)}%` : '未提供';
  if (isResumeJdEvidenceMatch(analysis)) {
    return `证据覆盖 ${percentage(meta.evidence_coverage)} · 待补充项 ${percentage(meta.unknown_ratio)}`;
  }
  const range = meta.score_range && Number.isFinite(meta.score_range.min) && Number.isFinite(meta.score_range.max)
    ? `证据基础分区间 ${meta.score_range.min}–${meta.score_range.max} / 100`
    : typeof meta.base_score === 'number' ? `基础分 ${meta.base_score} / 100` : '基础分待确认';
  return `${range} · 证据覆盖 ${percentage(meta.evidence_coverage)} · 未知占比 ${percentage(meta.unknown_ratio)}`;
}

export function evidenceScoreBasis(analysis: HrAnalysis): string {
  const meta = analysis.scoring_meta;
  if (!meta) return `${analysis.job_fit_percentage}% 基础契合度`;
  if (isResumeJdEvidenceMatch(analysis)) return `仅计已支持匹配点 · 基础分 ${meta.base_score}`;
  return `基础分 ${meta.base_score} · 暂估部分 ${meta.provisional_points} 分`;
}

export function evidenceScoreNotice(analysis: HrAnalysis): string {
  const meta = analysis.scoring_meta;
  if (!meta) return '';
  if (isResumeJdEvidenceMatch(analysis)) {
    return `${evidenceNormalizationSummary(analysis)} 简历与JD证据匹配分仅计已支持匹配点。未体现不代表不具备，真实性仍需核验。${evidenceDecisionPending(analysis) ? '匹配依据不足，需补充材料后复核。' : '待补充项不计分，请结合逐条证据复核。'}`;
  }
  const normalization = evidenceNormalizationSummary(analysis);
  const basis = `${normalization ? `${normalization} ` : ''}服务端按证据四态评分：${normalization && meta.applicable_max !== 0 ? '归一' : ''}基础分 ${meta.base_score} / 100，已获证据支持 ${meta.supported_score} 分，暂估部分 ${meta.provisional_points} 分。`;
  if (evidenceDecisionPending(analysis)) return `${basis}材料或评分区间尚未支持确定结论，请补充材料后复核，勿凭暂估分直接淘汰或确定排名。`;
  return `${basis}${evidenceScoreProvisional(analysis) ? '仍含待确认部分；区间处于同一决策档位，建议结合证据复核。' : '请结合逐条证据与岗位要求复核。'}`;
}
