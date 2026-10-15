'use client';

import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { setCompletionHandler } from '@/lib/analysis-session';

export interface EmploymentRecord {
  company_name: string;
  job_title: string;
  start_date: string;
  end_date: string;
  duration: string;
}

export interface RequirementChecklistItem {
  id: string;
  text: string;
  category: string;
  logic: string;
  status: 'met' | 'not_met' | 'not_mentioned' | 'manual_review';
  effective_status?: 'met' | 'not_met' | 'not_mentioned' | 'manual_review';
  resume_basis: string;
  /** 硬门槛档位：A 学历层级 / B 岗位硬门槛 / C 生活类与通用类 */
  gate_tier?: string;
  /** 门槛维度：degree 学历层级 / tier 院校层次 / years 年限 / major 专业 / certificate 证书 … */
  gate_dimension?: string;
  /** 证据三态：matched 已写明符合 / missing 未写明或无法核对 / conflict 已写明不符合 */
  evidence_state?: 'matched' | 'missing' | 'conflict' | string;
  /** 证据态的中文说明（如"未写明或无法核对，需人工确认"） */
  evidence_label?: string;
  /** JD 自己写了"可放宽" → 只扣分不封顶 */
  cap_exempt?: boolean;
  /** 服务端按原文兜底补的条目（gate_backstop）/ 被纠正过档位（gate_backstop_upgrade） */
  source?: string;
}

export interface EvidenceScoreRange {
  min: number;
  max: number;
}

export interface EvidenceScoreDimension {
  maximum: number;
  score: number;
  supported_score: number;
  confirmable_max: number;
  score_range: EvidenceScoreRange;
  applicable?: boolean;
  unknown_ratio?: number;
  unassessed_lanes?: string[];
  [key: string]: unknown;
}

export interface EvidenceScoreItem {
  id: string;
  status?: RequirementChecklistItem['status'];
  effective_status?: RequirementChecklistItem['status'];
  dimension?: string;
  dimensions?: string[];
  score?: number;
  confirmable_max?: number;
  [key: string]: unknown;
}

/** 服务端以证据四态计算；覆盖率与未知占比均为 0–1，比率不代表熟练程度。 */
export interface ScoringMeta {
  score_source: 'server_deterministic_evidence';
  /** 适用维度的原始预算上限；旧结果可能未提供。 */
  applicable_max?: number;
  /** 适用维度的原始分合计，五维分项仍保持原预算。 */
  raw_points?: number;
  /** 归一后的各维贡献，仅用于解释总分，不用于原预算分项 meter。 */
  normalized_breakdown?: Partial<NonNullable<HrAnalysis['score_breakdown']>>;
  /** 新语义仅计已支持匹配点；旧语义可能含暂估部分。 */
  score_semantics?: string;
  analysis_incomplete?: boolean;
  base_score?: number;
  supported_score?: number;
  provisional_points?: number;
  score_range?: EvidenceScoreRange;
  provisional?: boolean;
  incomplete?: boolean;
  decision_ready?: boolean;
  evidence_coverage?: number;
  unknown_ratio?: number;
  dimensions?: Record<string, EvidenceScoreDimension>;
  items?: EvidenceScoreItem[];
  version?: string;
  decision_note?: string;
  [key: string]: unknown;
}

/** 系统不做确定性判定的要求（学历形式/年龄/出差意愿…）：只展示，请人工确认。 */
export interface UndeterminedRequirement {
  type: string;
  text: string;
  note: string;
}

export interface EmploymentOverlap {
  company_name: string;
  other_company_name: string;
  start_date: string;
  end_date: string;
  months: number;
  message: string;
}

export interface AgentTraceStep {
  step: string;
  status: string;
  detail: string;
}

export interface AgentTrace {
  steps?: AgentTraceStep[];
  requirements?: string[];
  experiences?: string[];
  web_search?: boolean;
}

/** 简历文案核验：只读提示，不参与评分。level 仅用于排序与配色。
 *  source：resume=直接对账简历原文（不依赖模型）；report=对账模型报告的声明。 */
export interface CredibilityFinding {
  code: string;
  level: 'red_flag' | 'review' | 'info';
  title: string;
  detail: string;
  evidence: string;
  ask: string;
  where?: string;
  quote?: string;
  source?: 'resume' | 'report';
}

export interface Credibility {
  version: string;
  enabled: boolean;
  checks_run: number;
  findings: CredibilityFinding[];
  counts: { red_flag: number; review: number; info: number };
  by_source?: { resume: number; report: number };
  notice: string;
}

export interface GateBlocker {
  text: string;
  category: string;
  /** A 学历层级 / B 岗位硬门槛 / C 生活类与通用类 */
  tier: string;
  evidence_label: string;
  basis: string;
  /** JD 自己写了"可放宽" → 只扣分不封顶 */
  cap_exempt?: boolean;
}

export interface HrAnalysis {
  candidate_name: string;
  final_score: number;
  scoring_meta?: ScoringMeta;
  analysis_incomplete?: boolean;
  analysis_incomplete_reason?: string;
  fit_grade: string;
  job_fit_score: number;
  job_fit_percentage: number;
  hard_gate_deduction?: number;
  /** 模型原始匹配度：剔除硬门槛分项后归一到 0-100（与 job_fit_score 不同口径） */
  capability_score?: number;
  /** `ex_gate` = 已剔除硬门槛分项；`total` = 模型没给分项，退回模型总分 */
  capability_basis?: 'ex_gate' | 'total';
  /** 门槛阻塞项：能力分高却被封顶时，HR 要能一眼看到卡在哪 */
  gate_blockers?: GateBlocker[];
  /** 不能由能力弥补的门槛（法规执业资格/驾照/届别）→ 即使能力分高也不建议破格 */
  non_mitigable_blockers?: Array<{ text: string; category: string; tier: string }>;
  gate_tier_counts?: { A: number; B: number; C: number };
  /** 实际生效的封顶分（59/69/79），没封顶为 null */
  gate_cap_applied?: number | null;
  /** 能力分 ≥80 且被封顶到 60 以下 → 建议人工破格复核 */
  override_review?: boolean;
  score_breakdown?: {
    hard_requirements: number;
    responsibility_overlap: number;
    skills_projects: number;
    industry_background: number;
    evidence_bonus: number;
  };
  requirements_checklist?: RequirementChecklistItem[];
  /** 系统不做确定性判定的要求（全日制/年龄/地点…）：后端已产出，必须让 HR 看见 */
  undetermined_requirements?: UndeterminedRequirement[];
  ai_risk: 'none' | 'light' | 'medium' | 'high';
  ai_risk_level: string;
  ai_risk_label: string;
  /** 实际扣掉的分。默认 readonly 模式下恒为 0（该项只作风险提示） */
  ai_deduction: number;
  /** 模型建议扣的分（readonly 下未采用，仅供 HR 参考） */
  ai_deduction_suggested?: number;
  /** 后端给的处置说明（如"仅作风险提示，未扣分"） */
  ai_risk_note?: string;
  summary: string;
  basic_screening: {
    native_place: string;
    age: string;
    gender: string;
    work_location: string;
    salary_expectation: string;
  };
  education_history: Array<{
    degree: string;
    school_name: string;
    school_tier: string;
    major: string;
    graduation_year: string;
  }>;
  work_history: {
    total_years: string;
    relevant_years: string;
    industry_match: string;
    company_background: string;
    seniority: string;
    team_size: string;
    stability: string;
    employment_gaps: string;
    employment_records?: EmploymentRecord[];
    employment_overlaps?: EmploymentOverlap[];
    responsibility_match: string;
  };
  skill_match: {
    hard_skills: string[];
    project_match_points: string[];
    soft_skills: string[];
  };
  certificates: string[];
  bonus_items: string[];
  strengths: string[];
  weaknesses: string[];
  risk_points: string[];
  role_specific_assessment: string[];
  deduction_reasons: string[];
  /** 「破格复核」由服务端在"能力分≥80 但被硬门槛封顶"时给出，模型不能自己选 */
  recruitment_recommendation: '优先面试' | '储备观察' | '淘汰' | '破格复核' | '待补充材料';
  fit_tag: '高匹配' | '部分匹配' | '不匹配' | '待确认';
  agent_validation?: {
    checked_rules: number;
    issues: Array<{ rule: number; problem: string; fix: string }>;
    passed: boolean;
    mode?: string;
    revised?: boolean;
    recheck_required?: boolean;
    original_issues?: Array<{ rule: number; problem: string; fix: string }>;
  };
  agent_meta?: {
    validation_passed?: boolean;
    validation_issues?: string[];
    llm_calls?: number;
  };
  agent_trace?: AgentTrace;
  credibility?: Credibility;
}

export interface CandidateComparison {
  ranking: Array<{ rank: number; name: string; score: number; difference: string }>;
  pairwise: string[];
  recommendation: string;
}

export interface AnalysisData {
  request_id?: string;
  resume_id: string;
  job_id: string;
  candidate_name?: string;
  analysis_result?: string;
  optimized_markdown?: string;
  hr_analysis?: HrAnalysis;
  requirements_checklist?: RequirementChecklistItem[];
  credibility?: Credibility;
  studio_markdown?: string;
  batch_analyses?: AnalysisData[];
  batch_failures?: Array<{ resume_id: string; detail: string }>;
  comparison?: CandidateComparison;
}

export interface AnalysisResult {
  data: AnalysisData;
}

interface AnalysisContextValue {
  analysisResult: AnalysisResult | null;
  setAnalysisResult: (result: AnalysisResult) => void;
  isHydrated: boolean;
}

const STORAGE_KEY = 'resume-screening-result';
const AnalysisContext = createContext<AnalysisContextValue | undefined>(undefined);

export function AnalysisProvider({ children }: { children: ReactNode }) {
  const [analysisResult, setAnalysisResultState] = useState<AnalysisResult | null>(null);
  const [isHydrated, setIsHydrated] = useState(false);

  // 分析任务可能在任何页面后台完成（用户切走工作台时任务仍继续）：
  // 注册全局完成回调，把结果写入 context + sessionStorage，避免结果"丢失"。
  useEffect(() => {
    setCompletionHandler((result) => {
      setAnalysisResultState(result);
      try {
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify(result));
      } catch {
        // Keep the current session usable when browser storage is unavailable.
      }
    });
    return () => setCompletionHandler(null);
  }, []);

  useEffect(() => {
    try {
      const saved = sessionStorage.getItem(STORAGE_KEY);
      if (saved) setAnalysisResultState(JSON.parse(saved) as AnalysisResult);
    } catch {
      try {
        sessionStorage.removeItem(STORAGE_KEY);
      } catch {
        // Storage can throw again when the browser blocks the API entirely.
      }
    } finally {
      setIsHydrated(true);
    }
  }, []);

  const setAnalysisResult = (result: AnalysisResult) => {
    setAnalysisResultState(result);
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(result));
    } catch {
      // Keep the current session usable when browser storage is unavailable.
    }
  };

  return (
    <AnalysisContext.Provider value={{ analysisResult, setAnalysisResult, isHydrated }}>
      {children}
    </AnalysisContext.Provider>
  );
}

export function useAnalysis(): AnalysisContextValue {
  const context = useContext(AnalysisContext);
  if (!context) throw new Error('useAnalysis must be used within AnalysisProvider');
  return context;
}
