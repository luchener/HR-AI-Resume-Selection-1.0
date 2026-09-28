import { getStoredToken } from '@/components/workbench/auth-context';
import { API_URL } from './config';

/**
 * 统一请求头。
 * 注意：fetch 传字符串 body 时不会自动带 application/json，浏览器会按规范发
 * text/plain;charset=UTF-8 —— Flask 的 request.get_json(silent=True) 会解析失败返回
 * None，接口于是"200 但什么都没写"。所有带 body 的写操作必须显式声明 JSON。
 */
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

// ── 类型 ────────────────────────────────────────────────────────────
export interface QuotaUserRow {
  username: string;
  is_admin: boolean;
  has_email: boolean;
  daily_limit: number | null;      // 生效限额（含继承全局默认）；null = 不限
  raw_daily_limit: number | null;  // 该账号显式配置值；null = 未配置（继承全局默认）
  custom: boolean;                 // 是否单独配置（raw_daily_limit 非空）
  analysis_enabled: boolean;
  email_notify: boolean;
  used: number;
  remaining: number;               // -1 = 不限
  updated_at: string;
}

export interface QuotaSettings {
  quota_enabled: boolean;
  default_daily: number | null;    // null = 不限
  email_notify_enabled: boolean;
  users: QuotaUserRow[];
  updated_at: string;
}

export interface MyQuota {
  analysis_enabled: boolean;
  unlimited: boolean;
  is_admin?: boolean;
  quota_enabled?: boolean;
  daily_limit?: number;
  used?: number;
  remaining?: number;
  resets_at?: string;
}

// ── 管理端 ──────────────────────────────────────────────────────────
export async function fetchQuotaSettings(): Promise<QuotaSettings> {
  const response = await fetch(`${API_URL}/api/v1/admin/quota/settings`, {
    method: 'GET',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '配额设置读取失败。'); }
  const payload = (await response.json()) as { data?: QuotaSettings };
  return payload.data as QuotaSettings;
}

export async function updateQuotaSettings(payload: {
  quota_enabled?: boolean;
  default_daily?: number | null;
  email_notify_enabled?: boolean;
}): Promise<QuotaSettings> {
  const response = await fetch(`${API_URL}/api/v1/admin/quota/settings`, {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '保存失败。'); }
  const body = (await response.json()) as { data?: QuotaSettings };
  return body.data as QuotaSettings;
}

export async function updateUserQuota(username: string, payload: {
  daily_limit?: number | null;
  analysis_enabled?: boolean;
  email_notify?: boolean;
}): Promise<QuotaUserRow> {
  const response = await fetch(`${API_URL}/api/v1/admin/quota/users/${encodeURIComponent(username)}`, {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '保存失败。'); }
  const body = (await response.json()) as { data?: QuotaUserRow };
  return body.data as QuotaUserRow;
}

export async function resetUserQuota(username: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/quota/users/${encodeURIComponent(username)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '重置失败。'); }
  const body = (await response.json()) as { data?: { message?: string } };
  return body.data?.message || '已恢复全局默认。';
}

// ── 用户端 ──────────────────────────────────────────────────────────
export async function fetchMyQuota(): Promise<MyQuota> {
  const response = await fetch(`${API_URL}/api/v1/quota/me`, {
    method: 'GET',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '配额读取失败。'); }
  const payload = (await response.json()) as { data?: MyQuota };
  return payload.data as MyQuota;
}
