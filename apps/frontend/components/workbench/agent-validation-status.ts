import type { HrAnalysis } from './analysis-context';

type Validation = NonNullable<HrAnalysis['agent_validation']>;

/** Describe only the checks actually completed; a regenerated report is not automatically verified. */
export function agentValidationStatus(validation: Validation): string {
  if (validation.recheck_required) {
    return validation.revised ? '已生成修正稿，尚待复核' : '校验未完成，需人工复核';
  }
  if (validation.issues.length > 0) return `检出 ${validation.issues.length} 个问题，需人工复核`;
  if (!validation.passed) return '校验未通过，需人工复核';
  if (validation.mode === '预检') return '预检未发现疑点';
  return validation.revised ? '修正稿已复核，未发现问题' : '本次校验未发现问题';
}

export function agentValidationNeedsReview(validation: Validation): boolean {
  return !validation.passed || Boolean(validation.recheck_required) || validation.issues.length > 0;
}
