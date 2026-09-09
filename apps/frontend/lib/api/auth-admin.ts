import { getStoredToken } from '@/components/workbench/auth-context';
import { API_URL } from './config';

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) };
  const token = getStoredToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

async function errorDetail(response: Response): Promise<string> {
  const text = await response.text();
  try { return (JSON.parse(text) as { detail?: string }).detail || text; } catch { return text; }
}

function handleUnauthorized(response: Response): void {
  if (response.status === 401) {
    try {
      window.sessionStorage.removeItem('resume-screening-token');
      window.sessionStorage.removeItem('resume-screening-user');
      window.localStorage.removeItem('resume-screening-token');
      window.localStorage.removeItem('resume-screening-user');
    } catch { /* ignore */ }
    if (typeof window !== 'undefined' && window.location.pathname !== '/login') window.location.replace('/login');
  }
}

// ── 注册页配置（invite_required + 联系邮箱 + 邀请码位数）────────────────────
export interface RegisterConfig {
  invite_required: boolean;
  contact_email: string;
  invite_code_length: number;
}

export async function fetchRegisterConfig(): Promise<RegisterConfig> {
  const response = await fetch(`${API_URL}/api/v1/auth/register-config`, { method: 'GET' });
  if (!response.ok) throw new Error((await errorDetail(response)) || '注册配置读取失败。');
  const payload = (await response.json()) as { data?: Partial<RegisterConfig> };
  return {
    invite_required: payload.data?.invite_required ?? true,
    contact_email: payload.data?.contact_email || 'luchenstudio@163.com',
    invite_code_length: payload.data?.invite_code_length ?? 4,
  };
}

// ── 邀请码注册（申请 / 校验 / 发邮箱验证码）────────────────────────────
export async function submitInviteRequest(email: string, note: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/invite-request`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, note }),
  });
  if (!response.ok) throw new Error((await errorDetail(response)) || '申请提交失败。');
  const payload = (await response.json()) as { data?: { message?: string } };
  return payload.data?.message || '申请已提交。';
}

export async function checkInviteCode(inviteCode: string): Promise<void> {
  const response = await fetch(`${API_URL}/api/v1/auth/invite-code/check`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ invite_code: inviteCode }),
  });
  if (!response.ok) throw new Error((await errorDetail(response)) || '邀请码校验失败。');
}

export async function sendRegisterEmailCode(email: string, inviteCode: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/auth/invite-code/send-email-code`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, invite_code: inviteCode }),
  });
  if (!response.ok) throw new Error((await errorDetail(response)) || '验证码发送失败。');
  const payload = (await response.json()) as { detail?: string };
  return payload.detail || '验证码已发送。';
}

// ── 冻结账号自助解冻 ───────────────────────────────────────────────────
export async function sendUnfreezeCode(username: string, email: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/auth/unfreeze/send-code`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, email }),
  });
  if (!response.ok) throw new Error((await errorDetail(response)) || '验证码发送失败。');
  const payload = (await response.json()) as { detail?: string };
  return payload.detail || '解冻验证码已发送。';
}

export interface UnfreezeResult {
  message: string;
  token?: string;
  user_id?: string;
  username?: string;
}

export async function submitUnfreeze(username: string, password: string, email: string, code: string): Promise<UnfreezeResult> {
  const response = await fetch(`${API_URL}/api/v1/auth/unfreeze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, email, code }),
  });
  if (!response.ok) throw new Error((await errorDetail(response)) || '解冻失败。');
  const payload = (await response.json()) as { data?: UnfreezeResult };
  return payload.data || { message: '账号已解冻。' };
}

// ── 管理端：申请单 / 邀请码 / 冻结账号 ─────────────────────────────────
export interface InviteRequestItem {
  request_id: string;
  email: string;
  note: string;
  status: 'pending' | 'approved' | 'rejected';
  created_at: string;
  reviewed_at?: string | null;
  reject_reason?: string;
  code_sent: boolean;
  rejected_count: number;
}

export async function fetchInviteRequests(status: 'pending' | 'approved' | 'rejected'): Promise<InviteRequestItem[]> {
  const response = await fetch(`${API_URL}/api/v1/admin/invite-requests?status=${status}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '申请单读取失败。'); }
  const payload = (await response.json()) as { data?: { items?: InviteRequestItem[] } };
  return payload.data?.items || [];
}

export interface ApproveResult {
  message?: string;
  invite_code?: string;
  code_sent: boolean;
}

export async function approveInviteRequest(requestId: string): Promise<ApproveResult> {
  const response = await fetch(`${API_URL}/api/v1/admin/invite-requests/${encodeURIComponent(requestId)}/approve`, { method: 'POST', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '审批失败。'); }
  const payload = (await response.json()) as { data?: ApproveResult; detail?: string };
  return { ...(payload.data || {}), code_sent: Boolean(payload.data?.code_sent), message: payload.data?.message || payload.detail || '已通过。' };
}

export async function rejectInviteRequest(requestId: string, reason: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/invite-requests/${encodeURIComponent(requestId)}/reject`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ reason }),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '拒绝失败。'); }
  const payload = (await response.json()) as { data?: { message?: string } };
  return payload.data?.message || '已拒绝该申请。';
}

export async function resendInviteRequest(requestId: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/invite-requests/${encodeURIComponent(requestId)}/resend`, { method: 'POST', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '补发失败。'); }
  const payload = (await response.json()) as { data?: { message?: string; invite_code?: string }; detail?: string };
  if (payload.data?.invite_code) {
    return `补发成功，新邀请码：${payload.data.invite_code}（邮件发送失败场景请人工转达）`;
  }
  return payload.data?.message || payload.detail || '补发成功。';
}

export interface InviteCodeItem {
  code_hash: string;
  status: 'active' | 'used' | 'expired';
  bound_email: string;
  created_at: string;
  expires_at: string;
  used_by_username?: string | null;
  note?: string;
  revoked?: boolean;
}

export async function fetchInviteCodes(status: string = 'all'): Promise<InviteCodeItem[]> {
  const response = await fetch(`${API_URL}/api/v1/admin/invite-codes?status=${status}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '邀请码列表读取失败。'); }
  const payload = (await response.json()) as { data?: { items?: InviteCodeItem[] } };
  return payload.data?.items || [];
}

export async function generateInviteCodes(count: number, note: string): Promise<string[]> {
  const response = await fetch(`${API_URL}/api/v1/admin/invite-codes/generate`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ count, note }),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '生成失败。'); }
  const payload = (await response.json()) as { data?: { codes?: string[] } };
  return payload.data?.codes || [];
}

export async function revokeInviteCode(codeOrHash: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/invite-codes/${encodeURIComponent(codeOrHash)}/revoke`, { method: 'POST', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '作废失败。'); }
  const payload = (await response.json()) as { data?: { message?: string } };
  return payload.data?.message || '邀请码已作废。';
}

export interface FrozenUser {
  username: string;
  frozen_at?: string | null;
  frozen_by?: 'auto' | 'admin';
  frozen_reason?: string;
  consecutive_failures: number;
  window_failures: number;
}

export async function fetchFrozenUsers(): Promise<FrozenUser[]> {
  const response = await fetch(`${API_URL}/api/v1/admin/users/frozen`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '冻结列表读取失败。'); }
  const payload = (await response.json()) as { data?: { items?: FrozenUser[] } };
  return payload.data?.items || [];
}

export async function adminFreezeUser(username: string, reason: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/users/${encodeURIComponent(username)}/freeze`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ reason }),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '冻结失败。'); }
  const data = (await response.json()) as { data?: { message?: string } };
  return data.data?.message || '账号已冻结。';
}

export async function adminUnfreezeUser(username: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/users/${encodeURIComponent(username)}/unfreeze`, { method: 'POST', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '解冻失败。'); }
  const payload = (await response.json()) as { data?: { message?: string } };
  return payload.data?.message || '账号已解冻。';
}

// ── 管理端：用户管理（增删改查）────────────────────────────────────────
export interface AdminUserItem {
  user_id: string;
  username: string;
  email: string;
  is_admin: boolean;
  frozen: boolean;
  email_bound: boolean;
  created_at?: string | null;
  password_updated_at?: string | null;
}

export interface AdminUserList {
  items: AdminUserItem[];
  total: number;
  page: number;
  size: number;
}

export async function fetchAdminUsers(keyword: string = '', page: number = 1, size: number = 50): Promise<AdminUserList> {
  const params = new URLSearchParams();
  if (keyword.trim()) params.set('keyword', keyword.trim());
  params.set('page', String(page));
  params.set('size', String(size));
  const response = await fetch(`${API_URL}/api/v1/admin/users?${params.toString()}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '用户列表读取失败。'); }
  const payload = (await response.json()) as { data?: Partial<AdminUserList> };
  return {
    items: payload.data?.items || [],
    total: payload.data?.total ?? 0,
    page: payload.data?.page ?? 1,
    size: payload.data?.size ?? size,
  };
}

export interface AdminCreateUserPayload {
  username: string;
  password: string;
  email?: string;
  is_admin?: boolean;
}

export async function adminCreateUser(payload: AdminCreateUserPayload): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/users`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '创建用户失败。'); }
  const data = (await response.json()) as { data?: { message?: string } };
  return data.data?.message || '用户创建成功。';
}

export interface AdminUpdateUserPayload {
  email?: string;
  is_admin?: boolean;
}

export async function adminUpdateUser(username: string, payload: AdminUpdateUserPayload): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/users/${encodeURIComponent(username)}`, {
    method: 'PATCH',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '更新用户失败。'); }
  const data = (await response.json()) as { data?: { message?: string } };
  return data.data?.message || '用户已更新。';
}

export async function adminResetUserPassword(username: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/users/${encodeURIComponent(username)}/reset-password`, { method: 'POST', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '重置密码失败。'); }
  const data = (await response.json()) as { data?: { message?: string; temp_password?: string } };
  return data.data?.temp_password
    ? `${data.data.message || '密码已重置。'} 临时密码：${data.data.temp_password}（仅显示一次，请立即转达用户）`
    : data.data?.message || '密码已重置。';
}

export async function adminDeleteUser(username: string, adminPassword: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/users/${encodeURIComponent(username)}`, { method: 'DELETE', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ admin_password: adminPassword }) });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '删除用户失败。'); }
  const data = (await response.json()) as { data?: { message?: string } };
  return data.data?.message || '用户已删除。';
}

// ── 登录验证码（四位随机数字，一次性）───────────────────────────────
export interface CaptchaData {
  captcha_id: string;
  code: string;
  ttl_seconds: number;
}

export async function fetchCaptcha(): Promise<CaptchaData> {
  const response = await fetch(`${API_URL}/api/v1/auth/captcha`, { method: 'GET' });
  if (!response.ok) throw new Error((await errorDetail(response)) || '验证码获取失败。');
  const payload = (await response.json()) as { data?: CaptchaData };
  if (!payload.data?.captcha_id) throw new Error('验证码服务未返回有效数据。');
  return payload.data;
}

// ── 管理端：软删除账号恢复（90 天窗口）──────────────────────────────
export interface DeletedUserItem {
  user_id: string;
  username: string;
  email: string;
  deleted_at: string;
  deleted_by: string;
}

export async function fetchDeletedUsers(): Promise<DeletedUserItem[]> {
  const response = await fetch(`${API_URL}/api/v1/admin/users/deleted`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '已删除账号列表读取失败。'); }
  const data = (await response.json()) as { data?: { items?: DeletedUserItem[] } };
  return data.data?.items || [];
}

export async function adminRestoreUser(username: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/users/${encodeURIComponent(username)}/restore`, { method: 'POST', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '账号恢复失败。'); }
  const data = (await response.json()) as { data?: { message?: string } };
  return data.data?.message || '账号已恢复。';
}

// ── 管理端：操作记录（审计）────────────────────────────────────────
export interface AdminOpItem {
  op: string;
  operator_id: string;
  operator_name: string;
  target_username: string;
  detail?: string;
  created_at: string;
}

export interface AdminOpList {
  items: AdminOpItem[];
  total: number;
  page: number;
  size: number;
}

export const ADMIN_OP_LABELS: Record<string, string> = {
  user_create: '创建用户',
  user_delete: '删除用户',
  user_restore: '恢复账号',
  user_freeze: '冻结用户',
  user_unfreeze: '解冻用户',
  admin_grant: '授予管理员',
  admin_revoke: '取消管理员',
  email_update: '修改邮箱',
  pwd_reset: '重置密码',
};

export async function fetchAdminOps(op: string = '', keyword: string = '', page: number = 1, size: number = 50): Promise<AdminOpList> {
  const params = new URLSearchParams();
  if (op) params.set('op', op);
  if (keyword.trim()) params.set('keyword', keyword.trim());
  params.set('page', String(page));
  params.set('size', String(size));
  const response = await fetch(`${API_URL}/api/v1/admin/ops?${params.toString()}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '操作记录读取失败。'); }
  const payload = (await response.json()) as { data?: Partial<AdminOpList> };
  return {
    items: payload.data?.items || [],
    total: payload.data?.total ?? 0,
    page: payload.data?.page ?? 1,
    size: payload.data?.size ?? size,
  };
}

/**
 * 导出操作记录（审计）：csv（Excel 兼容，utf-8-sig）或 json，遵循当前 op/keyword 过滤，导出全部结果。
 * 以浏览器下载方式保存，返回 Promise 在新任务中用 Blob 触发下载。
 */
export async function downloadAdminOpsExport(op: string, keyword: string, format: 'csv' | 'json' = 'csv'): Promise<void> {
  const params = new URLSearchParams();
  if (op) params.set('op', op);
  if (keyword.trim()) params.set('keyword', keyword.trim());
  params.set('format', format);
  const response = await fetch(`${API_URL}/api/v1/admin/ops/export?${params.toString()}`, {
    method: 'GET',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '导出失败。'); }
  const blob = await response.blob();
  // 从 Content-Disposition 提取文件名，失败则按日期回退
  const cd = response.headers.get('Content-Disposition') || '';
  const match = cd.match(/filename="?([^";]+)"?/);
  const filename = match?.[1] || `admin-ops-${new Date().toISOString().slice(0, 10)}.${format}`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = decodeURIComponent(filename);
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ── 管理端：用户使用统计（日 / 月 / 年）────────────────────────────
export type UsageGranularity = 'day' | 'month' | 'year';

export interface UserUsageSummary {
  total: number;
  login_total: number;
  analysis_total: number;
  avg_per_bucket: number;
  peak_label: string;
  peak_total: number;
  active_buckets: number;
  first_usage: string;
  last_usage: string;
}

export interface UserUsageData {
  granularity: UsageGranularity;
  labels: string[];
  series: { login: number[]; analysis: number[]; total: number[] };
  summary?: UserUsageSummary;
}

export async function fetchUserUsage(username: string, granularity: UsageGranularity = 'day', buckets: number = 30): Promise<UserUsageData> {
  const params = new URLSearchParams();
  params.set('granularity', granularity);
  params.set('buckets', String(buckets));
  const response = await fetch(`${API_URL}/api/v1/admin/users/${encodeURIComponent(username)}/usage?${params.toString()}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '使用统计读取失败。'); }
  const payload = (await response.json()) as { data?: Partial<UserUsageData> };
  return {
    granularity: (payload.data?.granularity as UsageGranularity) || granularity,
    labels: payload.data?.labels || [],
    series: {
      login: payload.data?.series?.login || [],
      analysis: payload.data?.series?.analysis || [],
      total: payload.data?.series?.total || [],
    },
    summary: payload.data?.summary,
  };
}

// ── 管理端：全用户使用排行（发现高消耗账号）────────────────────────
export interface UsageRankingItem {
  user_id: string;
  username: string;
  login: number;
  analysis: number;
  total: number;
  last_usage: string;
}

export async function fetchUsageRanking(limit: number = 20): Promise<UsageRankingItem[]> {
  const params = new URLSearchParams();
  params.set('limit', String(limit));
  const response = await fetch(`${API_URL}/api/v1/admin/users/usage-ranking?${params.toString()}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '使用排行读取失败。'); }
  const payload = (await response.json()) as { data?: { items?: UsageRankingItem[] } };
  return payload.data?.items || [];
}
