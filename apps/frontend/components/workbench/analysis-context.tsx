'use client';

import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

export interface EmploymentRecord {
  company_name: string;
  job_title: string;
  start_date: string;
  end_date: string;
  duration: string;
}

export interface HrAnalysis {
  candidate_name: string;
  final_score: number;
  fit_grade: string;
  job_fit_score: number;
  job_fit_percentage: number;
  ai_risk: 'none' | 'light' | 'medium' | 'high';
  ai_risk_level: string;
  ai_risk_label: string;
  ai_deduction: number;
  summary: string;
  basic_screening: {
    highest_education: string;
    school_name: string;
    school_tier: string;
    education_type: string;
    major_match: string;
    graduation_year: string;
    fresh_graduate: string;
    age: string;
    gender: string;
    work_location: string;
    salary_expectation: string;
  };
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
  recruitment_recommendation: '优先面试' | '储备观察' | '淘汰';
  fit_tag: '高匹配' | '部分匹配' | '不匹配';
}

export interface AnalysisData {
  request_id?: string;
  resume_id: string;
  job_id: string;
  candidate_name?: string;
  analysis_result?: string;
  hr_analysis?: HrAnalysis;
  studio_markdown?: string;
  batch_analyses?: AnalysisData[];
  batch_failures?: Array<{ resume_id: string; detail: string }>;
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

  useEffect(() => {
    try {
      const saved = sessionStorage.getItem(STORAGE_KEY);
      if (saved) setAnalysisResultState(JSON.parse(saved) as AnalysisResult);
    } catch {
      sessionStorage.removeItem(STORAGE_KEY);
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
