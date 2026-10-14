import { getStoredToken } from '@/components/workbench/auth-context';
import { API_URL } from './config';

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) };
  const token = getStoredToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

function handleUnauthorized(response: Response): void {
  if (response.status === 401) {
    try {
      window.sessionStorage.removeItem('resume-screening-token');
      window.sessionStorage.removeItem('resume-screening-user');
      window.localStorage.removeItem('resume-screening-token');
      window.localStorage.removeItem('resume-screening-user');
      window.sessionStorage.removeItem('resume-screening-result');
    } catch { /* ignore */ }
    if (typeof window !== 'undefined' && window.location.pathname !== '/login') window.location.replace('/login');
  }
}

async function errorDetail(response: Response): Promise<string> {
  const text = await response.text();
  try { return (JSON.parse(text) as { detail?: string }).detail || text; } catch { return text; }
}

/** 预设岗位分类（归档弹窗 + 人才库筛选共用）。 */
export const ARCHIVE_PRESET_CATEGORIES = [
  '行政', '销售', 'IT', '营销', '财务', '人力', '产品', '设计', '运营', '法务', '客服', '其他',
];

/** 归档记录（后端 _archive_payload 结构）。 */
export interface ArchiveRecord {
  archive_id: string;
  resume_id: string;
  job_id: string;
  candidate_name: string;
  final_score: number;
  fit_tag: string;
  recruitment_recommendation: string;
  job_title: string;
  category: string;
  custom_tags: string[];
  analysis_snapshot: Record<string, unknown>;
  /** 完整 hr_analysis，仅详情接口返回（用于重新生成报告快照）。 */
  analysis?: Record<string, unknown>;
  /** 原文是否已过保留期自动清除（归档结果仍在，仅原文不可查看）。 */
  source_purged?: boolean;
  status: 'active' | 'trashed';
  trashed_at: string | null;
  created_at: string;
}

export interface ArchiveListData {
  archives: ArchiveRecord[];
  meta: { job_titles: string[]; categories: string[] };
}

/** 门槛阻塞项（后端 gate_blockers 的裁剪版，随归档快照存储）。 */
export interface ArchiveGateBlocker {
  text: string;
  /** A 学历层级 / B 岗位硬门槛 / C 生活类与通用类 */
  tier: string;
  evidence_label: string;
}

export interface ArchiveGateSummary {
  /** 模型原始匹配度中剔除硬门槛分项后归一（不受门槛封顶影响）；老归档没有这个字段时为 null */
  capabilityScore: number | null;
  /** `ex_gate` = 已剔除硬门槛分项；`total` = 无分项，退回模型总分 */
  capabilityBasis: string | null;
  /** 能力分够高、却被门槛压到 60 以下 → 建议人工破格复核 */
  overrideReview: boolean;
  /** 实际生效的封顶分（59/69/79），没封顶为 null */
  capApplied: number | null;
  blockers: ArchiveGateBlocker[];
  /** 不能由能力弥补的门槛（法规执业资格/驾照/届别）：即使能力分高也不建议破格 */
  nonMitigableBlockers: string[];
}

/** 从归档快照里读出"能力分 / 破格复核 / 阻塞项"，兼容没有这些字段的历史归档。 */
export function readGateSummary(record: ArchiveRecord): ArchiveGateSummary {
  const snapshot = (record.analysis_snapshot || {}) as Record<string, unknown>;
  const rawCapability = snapshot.capability_score;
  const capabilityScore = typeof rawCapability === 'number' ? rawCapability : null;
  const rawCap = snapshot.gate_cap_applied;
  const capApplied = typeof rawCap === 'number' ? rawCap : null;
  const rawBlockers = snapshot.gate_blockers;
  const blockers: ArchiveGateBlocker[] = Array.isArray(rawBlockers)
    ? rawBlockers
        .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
        .map((item) => ({
          text: String(item.text ?? ''),
          tier: String(item.tier ?? ''),
          evidence_label: String(item.evidence_label ?? ''),
        }))
    : [];
  const rawNonMitigable = snapshot.non_mitigable_blockers;
  const nonMitigableBlockers: string[] = Array.isArray(rawNonMitigable)
    ? rawNonMitigable.map((item) => String(item ?? '')).filter(Boolean)
    : [];
  const rawBasis = snapshot.capability_basis;
  return {
    capabilityScore,
    capabilityBasis: typeof rawBasis === 'string' && rawBasis ? rawBasis : null,
    overrideReview: Boolean(snapshot.override_review),
    capApplied,
    blockers,
    nonMitigableBlockers,
  };
}

/** 创建归档（幂等：同一 resume_id + job_id 重复归档返回已有记录）。 */
export async function createArchive(
  resumeId: string,
  jobId: string,
  options?: {
    candidateName?: string;
    finalScore?: number;
    customTags?: string[];
    category?: string;
    analysis?: Record<string, unknown>;
    analysisResult?: string;
  },
): Promise<ArchiveRecord> {
  const response = await fetch(`${API_URL}/api/v1/archives`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      resume_id: resumeId,
      job_id: jobId,
      candidate_name: options?.candidateName,
      final_score: options?.finalScore,
      custom_tags: options?.customTags,
      category: options?.category,
      analysis: options?.analysis,
      analysis_result: options?.analysisResult,
    }),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `归档失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: ArchiveRecord };
  if (!payload.data) throw new Error('服务未返回归档记录。');
  return payload.data;
}

/** 人才库列表（仅 active），支持姓名/岗位/分类/标签筛选与排序。 */
export async function fetchArchives(options?: {
  name?: string;
  jobTitle?: string;
  category?: string;
  tag?: string;
  sort?: 'score' | 'created';
}): Promise<ArchiveListData> {
  const params = new URLSearchParams();
  if (options?.name) params.set('name', options.name);
  if (options?.jobTitle) params.set('job_title', options.jobTitle);
  if (options?.category) params.set('category', options.category);
  if (options?.tag) params.set('tag', options.tag);
  if (options?.sort) params.set('sort', options.sort);
  const qs = params.toString();
  const response = await fetch(`${API_URL}/api/v1/archives${qs ? `?${qs}` : ''}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `归档列表读取失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: ArchiveListData };
  if (!payload.data) throw new Error('服务未返回归档列表。');
  return payload.data;
}

/** 回收站列表。 */
export async function fetchTrash(): Promise<ArchiveListData> {
  const response = await fetch(`${API_URL}/api/v1/archives/trash`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `回收站读取失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: ArchiveListData };
  if (!payload.data) throw new Error('服务未返回回收站数据。');
  return payload.data;
}

/** 归档详情（仅 active 可读）。 */
export async function fetchArchiveDetail(archiveId: string): Promise<ArchiveRecord> {
  const response = await fetch(`${API_URL}/api/v1/archives/${encodeURIComponent(archiveId)}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `归档详情读取失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: ArchiveRecord };
  if (!payload.data) throw new Error('服务未返回归档详情。');
  return payload.data;
}

/** 更新自定义标签。 */
export async function updateArchiveTags(archiveId: string, customTags: string[]): Promise<ArchiveRecord> {
  const response = await fetch(`${API_URL}/api/v1/archives/${encodeURIComponent(archiveId)}/tags`, {
    method: 'PATCH',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ custom_tags: customTags }),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `标签更新失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: ArchiveRecord };
  if (!payload.data) throw new Error('服务未返回归档记录。');
  return payload.data;
}

/** 更新岗位分类。 */
export async function updateArchiveCategory(archiveId: string, category: string): Promise<ArchiveRecord> {
  const response = await fetch(`${API_URL}/api/v1/archives/${encodeURIComponent(archiveId)}/category`, {
    method: 'PATCH',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ category }),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `分类更新失败（HTTP ${response.status}）`); }
  const payload = (await response.json()) as { data?: ArchiveRecord };
  if (!payload.data) throw new Error('服务未返回归档记录。');
  return payload.data;
}

/** 移入回收站（软删除）。 */
export async function moveToTrash(archiveId: string): Promise<void> {
  const response = await fetch(`${API_URL}/api/v1/archives/${encodeURIComponent(archiveId)}`, { method: 'DELETE', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `移入回收站失败（HTTP ${response.status}）`); }
}

/** 从回收站恢复。 */
export async function restoreArchive(archiveId: string): Promise<void> {
  const response = await fetch(`${API_URL}/api/v1/archives/${encodeURIComponent(archiveId)}/restore`, { method: 'POST', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `恢复归档失败（HTTP ${response.status}）`); }
}

/** 彻底删除回收站单条。 */
export async function permanentDeleteArchive(archiveId: string): Promise<void> {
  const response = await fetch(`${API_URL}/api/v1/archives/trash/${encodeURIComponent(archiveId)}`, { method: 'DELETE', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `彻底删除失败（HTTP ${response.status}）`); }
}

/** 清空回收站。 */
export async function emptyTrash(): Promise<void> {
  const response = await fetch(`${API_URL}/api/v1/archives/trash`, { method: 'DELETE', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || `清空回收站失败（HTTP ${response.status}）`); }
}
