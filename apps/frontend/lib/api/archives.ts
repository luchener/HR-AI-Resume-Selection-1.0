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
  status: 'active' | 'trashed';
  trashed_at: string | null;
  created_at: string;
}

export interface ArchiveListData {
  archives: ArchiveRecord[];
  meta: { job_titles: string[]; categories: string[] };
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
