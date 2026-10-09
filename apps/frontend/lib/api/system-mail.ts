import { getStoredToken } from '@/components/workbench/auth-context';
import { API_URL } from './config';

/**
 * 统一请求头。
 * 注意：fetch 传字符串 body 时不会自动带 application/json，浏览器会按规范发
 * text/plain;charset=UTF-8 —— Flask 的 request.get_json(silent=True) 解析失败返回
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

const BASE = '/api/v1/admin/system/mail';

export type AdminMailSlotKey = 'transactional' | 'notification';
export type AdminMailSecurity = 'ssl' | 'starttls' | 'plain';
/** ui=界面配置；ui.transactional=跟随注册配置；env=服务器 .env；none=未配置 */
export type AdminMailSource = 'ui' | 'ui.transactional' | 'env' | 'none';

export interface AdminMailSlot {
  slot: AdminMailSlotKey;
  host: string;
  port: number;
  security: AdminMailSecurity;
  username: string;
  /** 是否已设置密码；服务端永不返回明文 */
  password_set: boolean;
  password_mask: string;
  from_address: string;
  from_name: string;
  enabled: boolean;
  source: AdminMailSource;
  ready: boolean;
}

export interface AdminMailConfig {
  slots: Record<AdminMailSlotKey, AdminMailSlot>;
  env_available: boolean;
  updated_at: string;
  updated_by: string;
}

export interface AdminMailTestResult {
  slot: AdminMailSlotKey;
  source: AdminMailSource;
  host: string;
  port: number;
  security: string;
  from_address: string;
  test_email_sent: boolean;
  seconds: number;
  message: string;
}

export interface AdminMailSavePayload {
  slots: Partial<Record<AdminMailSlotKey, {
    host?: string;
    port?: number;
    security?: AdminMailSecurity;
    username?: string;
    /** 空串或不传 = 保持原密码 */
    password?: string;
    from_address?: string;
    from_name?: string;
    enabled?: boolean;
  }>>;
}

export async function fetchAdminMailConfig(): Promise<AdminMailConfig> {
  const response = await fetch(`${API_URL}${BASE}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) {
    handleUnauthorized(response);
    throw new Error((await errorDetail(response)) || '读取邮件服务配置失败。');
  }
  const payload = (await response.json()) as { data: AdminMailConfig };
  return payload.data;
}

export async function saveAdminMailConfig(body: AdminMailSavePayload): Promise<AdminMailConfig> {
  const response = await fetch(`${API_URL}${BASE}`, {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    handleUnauthorized(response);
    throw new Error((await errorDetail(response)) || '保存邮件服务配置失败。');
  }
  const payload = (await response.json()) as { data: AdminMailConfig };
  return payload.data;
}

export async function testAdminMailConfig(slot: AdminMailSlotKey, send: boolean): Promise<AdminMailTestResult> {
  const response = await fetch(`${API_URL}${BASE}/test`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ slot, send }),
  });
  if (!response.ok) {
    handleUnauthorized(response);
    throw new Error((await errorDetail(response)) || '测试失败。');
  }
  const payload = (await response.json()) as { data: AdminMailTestResult };
  return payload.data;
}

export async function clearAdminMailConfig(): Promise<AdminMailConfig> {
  const response = await fetch(`${API_URL}${BASE}`, { method: 'DELETE', headers: authHeaders() });
  if (!response.ok) {
    handleUnauthorized(response);
    throw new Error((await errorDetail(response)) || '清空配置失败。');
  }
  const payload = (await response.json()) as { data: AdminMailConfig };
  return payload.data;
}
