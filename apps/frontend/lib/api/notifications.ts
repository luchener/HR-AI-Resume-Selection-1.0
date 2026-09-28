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

// ── 公告（管理端）────────────────────────────────────────────────────
export interface AnnouncementItem {
  announcement_id: string;
  title: string;
  content: string;
  start_mode: string;
  start_at?: string;
  end_at?: string;
  status: 'active' | 'scheduled' | 'expired' | 'cancelled';
  created_by_name?: string;
  created_at?: string;
  cancelled_at?: string;
}

export async function fetchAnnouncements(page = 1, size = 10): Promise<{ total: number; items: AnnouncementItem[] }> {
  const query = `?page=${page}&size=${size}`;
  const response = await fetch(`${API_URL}/api/v1/admin/announcements${query}`, {
    method: 'GET',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '公告列表读取失败。'); }
  const payload = (await response.json()) as { data?: { total?: number; items?: AnnouncementItem[] } };
  return { total: payload.data?.total ?? 0, items: payload.data?.items || [] };
}

export interface CreateAnnouncementPayload {
  title: string;
  content: string;
  start_mode: 'now' | 'at' | 'delay';
  delay_value?: number;
  delay_unit?: 'minute' | 'hour' | 'day';
  duration_value: number;
  duration_unit: 'minute' | 'hour' | 'day';
  start_at?: string;
}

export async function createAnnouncement(payload: CreateAnnouncementPayload): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/announcements`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '创建公告失败。'); }
  const body = (await response.json()) as { data?: { announcement_id?: string } };
  return body.data?.announcement_id || '';
}

export async function cancelAnnouncement(announcementId: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/announcements/${announcementId}/cancel`, {
    method: 'POST',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '撤回失败。'); }
  const body = (await response.json()) as { data?: { message?: string } };
  return body.data?.message || '公告已撤回。';
}

// ── 公告（用户端弹窗）───────────────────────────────────────────────
export interface UserAnnouncement {
  announcement_id: string;
  title: string;
  content: string;
  created_at?: string;
}

export async function deleteAnnouncement(announcementId: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/announcements/${announcementId}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '删除失败。'); }
  const body = (await response.json()) as { data?: { message?: string } };
  return body.data?.message || '公告已删除。';
}

export async function fetchUserAnnouncements(): Promise<UserAnnouncement[]> {
  const response = await fetch(`${API_URL}/api/v1/notifications/announcements`, {
    method: 'GET',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '公告读取失败。'); }
  const payload = (await response.json()) as { data?: { items?: UserAnnouncement[] } };
  return payload.data?.items || [];
}

export async function dismissAnnouncement(announcementId: string): Promise<void> {
  const response = await fetch(`${API_URL}/api/v1/notifications/announcements/${announcementId}/dismiss`, {
    method: 'POST',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '关闭公告失败。'); }
}

// ── 邮件通知：主题/模板 ──────────────────────────────────────────────
export interface EmailTheme {
  header_bg: string;
  header_accent: string;
  accent_bg: string;
  accent_border: string;
  accent_text: string;
  button_bg: string;
  body_bg: string;
}

export const EMAIL_PRESET_THEMES: Record<string, EmailTheme> = {
  brand: {
    header_bg: '#17243b', header_accent: '#8fa3c7', accent_bg: '#eef2fb',
    accent_border: '#b9cbf2', accent_text: '#253249', button_bg: '#263a5e', body_bg: '#f3f6fa',
  },
  green: {
    header_bg: '#1e3a2f', header_accent: '#9fc3b2', accent_bg: '#e6f7ee',
    accent_border: '#bfe3d0', accent_text: '#2f6b4a', button_bg: '#2e5c49', body_bg: '#f3f6fa',
  },
  purple: {
    header_bg: '#3b2a4d', header_accent: '#b7a4cc', accent_bg: '#f0eaf7',
    accent_border: '#d2c2e6', accent_text: '#4a3560', button_bg: '#5a4080', body_bg: '#f3f6fa',
  },
  graphite: {
    header_bg: '#2f3644', header_accent: '#9aa3b3', accent_bg: '#eef1f6',
    accent_border: '#c3cad6', accent_text: '#39424f', button_bg: '#3d4757', body_bg: '#f3f6fa',
  },
};

export const EMAIL_TEMPLATES = [
  { id: 'system_update', name: '系统更新', desc: '版本更新排版 · 含要点列表' },
  { id: 'notice', name: '通知公告', desc: '段落式通知' },
  { id: 'feature_launch', name: '新功能上线', desc: '亮点式排版 · 含要点列表' },
  { id: 'maintenance', name: '维护通知', desc: '时间+影响排版' },
] as const;

export type EmailTemplateId = (typeof EMAIL_TEMPLATES)[number]['id'];

// 模板示例文案（切模板自动替换正文，前端提示将覆盖）
export const EMAIL_TEMPLATE_SAMPLES: Record<EmailTemplateId, { subject: string; body: string; items: string[] }> = {
  system_update: {
    subject: '【系统更新】简历智选 v1.1 已发布',
    body: '您好：\n\n简历智选 v1.1 已正式发布，本次更新为您带来以下改进：',
    items: ['新增「更新公告」：系统公告登录后自动弹出，重要更新不错过', '新增「邮件通知」：本邮件即通过该功能发送', '修复：批量分析超时场景下的报告生成异常'],
  },
  notice: {
    subject: '【通知】关于近期使用的重要提醒',
    body: '各位用户：\n\n近期平台对部分功能进行了调整，请登录工作台查看详情。如有疑问，可回复本邮件联系管理员。',
    items: [],
  },
  feature_launch: {
    subject: '【新功能】简历智选 v1.1 新功能上线',
    body: '您好：\n\n本次为您带来以下新功能，欢迎体验：',
    items: ['智能简历筛选分析一键归档', '批量对比候选人能力画像', '人才库自定义标签分类'],
  },
  maintenance: {
    subject: '【维护通知】系统维护安排',
    body: '各位用户：\n\n系统将于本周日 02:00-04:00 进行例行维护，期间分析服务不可用，请合理安排使用时间。',
    items: [],
  },
};

// ── 邮件通知：图片 ───────────────────────────────────────────────────
export interface EmailImageItem {
  image_id: string;
  name: string;
  ext: string;
  size: number;
}

export async function fetchEmailImages(): Promise<EmailImageItem[]> {
  const response = await fetch(`${API_URL}/api/v1/admin/notifications/email/images`, {
    method: 'GET',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '图片列表读取失败。'); }
  const payload = (await response.json()) as { data?: { items?: EmailImageItem[] } };
  return payload.data?.items || [];
}

export async function uploadEmailImage(file: File): Promise<EmailImageItem> {
  const form = new FormData();
  form.append('file', file);
  const response = await fetch(`${API_URL}/api/v1/admin/notifications/email/image`, {
    method: 'POST',
    headers: authHeaders(),
    body: form,
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '图片上传失败。'); }
  const payload = (await response.json()) as { data?: EmailImageItem };
  return payload.data!;
}

export async function fetchEmailImageData(imageId: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/notifications/email/image/${imageId}/data`, {
    method: 'GET',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '图片读取失败。'); }
  const payload = (await response.json()) as { data?: { data_uri?: string } };
  return payload.data?.data_uri || '';
}

export async function deleteEmailImage(imageId: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/notifications/email/image/${imageId}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '图片删除失败。'); }
  const body = (await response.json()) as { data?: { message?: string } };
  return body.data?.message || '图片已删除。';
}

// ── 邮件通知：预览 / 发送 / 历史 ────────────────────────────────────
export interface EmailComposePayload {
  subject: string;
  template_id: string;
  body: string;
  images?: { image_id: string; width: number }[];
  items?: string[];
  accent?: { enabled: boolean; text: string };
  button?: { enabled: boolean; url: string; text: string };
  theme?: Partial<EmailTheme>;
}

export interface EmailSendPayload extends EmailComposePayload {
  target: 'all' | 'selected';
  user_ids?: string[];
}

export async function previewEmail(payload: EmailComposePayload): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/admin/notifications/email/preview`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '预览渲染失败。'); }
  const body = (await response.json()) as { data?: { html?: string } };
  return body.data?.html || '';
}

export interface EmailSendSummary {
  email_log_id: string;
  subject: string;
  target: string;
  target_count: number;
  sent: number;
  skipped: number;
  failed: number;
  errors?: { email: string; error: string }[];
  created_at?: string;
}

export async function sendEmail(payload: EmailSendPayload): Promise<EmailSendSummary> {
  const response = await fetch(`${API_URL}/api/v1/admin/notifications/email`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '发送失败。'); }
  const body = (await response.json()) as { data?: EmailSendSummary };
  return body.data!;
}

export interface EmailLogItem extends EmailSendSummary {
  template_id: string;
  created_by_name?: string;
}

export async function fetchEmailLogs(page = 1, size = 5): Promise<{ total: number; items: EmailLogItem[] }> {
  const query = `?page=${page}&size=${size}`;
  const response = await fetch(`${API_URL}/api/v1/admin/notifications/email/logs${query}`, {
    method: 'GET',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '发送历史读取失败。'); }
  const payload = (await response.json()) as { data?: { total?: number; items?: EmailLogItem[] } };
  return { total: payload.data?.total ?? 0, items: payload.data?.items || [] };
}

export async function fetchEmailLogDetail(logId: string): Promise<{ record: EmailLogItem; html: string }> {
  const response = await fetch(`${API_URL}/api/v1/admin/notifications/email/logs/${logId}`, {
    method: 'GET',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '发送记录读取失败。'); }
  const payload = (await response.json()) as { data?: { record?: EmailLogItem; html?: string } };
  return { record: payload.data?.record as EmailLogItem, html: payload.data?.html || '' };
}

// ── 收件人信息 ───────────────────────────────────────────────────────
export interface RecipientUser {
  user_id: string;
  username: string;
  email: string;
}

export async function fetchEmailRecipients(keyword = ''): Promise<{ total_users: number; with_email: number; items: RecipientUser[] }> {
  const query = keyword ? `?keyword=${encodeURIComponent(keyword)}` : '';
  const response = await fetch(`${API_URL}/api/v1/admin/notifications/email/recipients${query}`, {
    method: 'GET',
    headers: authHeaders(),
  });
  if (!response.ok) { handleUnauthorized(response); throw new Error((await errorDetail(response)) || '收件人信息读取失败。'); }
  const payload = (await response.json()) as { data?: { total_users?: number; with_email?: number; items?: RecipientUser[] } };
  return {
    total_users: payload.data?.total_users ?? 0,
    with_email: payload.data?.with_email ?? 0,
    items: payload.data?.items || [],
  };
}
