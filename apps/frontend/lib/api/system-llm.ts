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

const BASE = '/api/v1/admin/system/llm';

/** ui=界面配置（当前生效）；env=服务器 .env；none=未配置 */
export type AdminLlmSource = 'ui' | 'env' | 'none';

export interface AdminLlmConfig {
  /** 是否已设置 API Key；服务端永不返回明文 */
  api_key_set: boolean;
  api_key_mask: string;
  base_url: string;
  model: string;
  timeout: number;
  enabled: boolean;
  source: AdminLlmSource;
  ready: boolean;
  env_available: boolean;
  /** 上次「获取模型列表」的结果，落盘缓存，刷新页面仍在 */
  models: string[];
  models_fetched_at: string;
  updated_at: string;
  updated_by: string;
}

export interface AdminLlmTestResult {
  model: string;
  base_url: string;
  seconds: number;
  reply_chars: number;
  message: string;
}

export interface AdminLlmModelsResult {
  models: string[];
  count: number;
  models_fetched_at: string;
  seconds: number;
  config: AdminLlmConfig;
}

export interface AdminLlmSavePayload {
  /** 空串或不传 = 保持原密钥 */
  api_key?: string;
  base_url?: string;
  model?: string;
  timeout?: number;
  enabled?: boolean;
}

/** 测试 / 取模型列表时可以传当前表单里的值，支持"先测通再保存"。 */
export interface AdminLlmProbePayload {
  api_key?: string;
  base_url?: string;
  model?: string;
}

export async function fetchAdminLlmConfig(): Promise<AdminLlmConfig> {
  const response = await fetch(`${API_URL}${BASE}`, { method: 'GET', headers: authHeaders() });
  if (!response.ok) {
    handleUnauthorized(response);
    throw new Error((await errorDetail(response)) || '读取模型配置失败。');
  }
  const payload = (await response.json()) as { data: AdminLlmConfig };
  return payload.data;
}

export async function saveAdminLlmConfig(body: AdminLlmSavePayload): Promise<AdminLlmConfig> {
  const response = await fetch(`${API_URL}${BASE}`, {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    handleUnauthorized(response);
    throw new Error((await errorDetail(response)) || '保存模型配置失败。');
  }
  const payload = (await response.json()) as { data: AdminLlmConfig };
  return payload.data;
}

export async function testAdminLlmConnection(body: AdminLlmProbePayload): Promise<AdminLlmTestResult> {
  const response = await fetch(`${API_URL}${BASE}/test`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    handleUnauthorized(response);
    throw new Error((await errorDetail(response)) || '测试失败。');
  }
  const payload = (await response.json()) as { data: AdminLlmTestResult };
  return payload.data;
}

export async function fetchAdminLlmModels(body: AdminLlmProbePayload): Promise<AdminLlmModelsResult> {
  const response = await fetch(`${API_URL}${BASE}/models`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    handleUnauthorized(response);
    throw new Error((await errorDetail(response)) || '获取模型列表失败。');
  }
  const payload = (await response.json()) as { data: AdminLlmModelsResult };
  return payload.data;
}

export async function clearAdminLlmConfig(): Promise<AdminLlmConfig> {
  const response = await fetch(`${API_URL}${BASE}`, { method: 'DELETE', headers: authHeaders() });
  if (!response.ok) {
    handleUnauthorized(response);
    throw new Error((await errorDetail(response)) || '清空配置失败。');
  }
  const payload = (await response.json()) as { data: AdminLlmConfig };
  return payload.data;
}
