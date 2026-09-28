'use client';

/* eslint-disable @next/next/no-img-element -- 邮件图片缩略图为 data URI，无法使用 next/image 优化，故保留原生 <img> */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CheckCircle2Icon,
  ImagePlusIcon,
  LoaderCircleIcon,
  MailIcon,
  SendIcon,
  UsersIcon,
  XCircleIcon,
} from 'lucide-react';
import ConfirmDialog from './confirm-dialog';
import AdminModal from './admin-modal';
import {
  deleteEmailImage,
  EMAIL_PRESET_THEMES,
  EMAIL_TEMPLATES,
  EMAIL_TEMPLATE_SAMPLES,
  fetchEmailImageData,
  fetchEmailImages,
  fetchEmailLogDetail,
  fetchEmailLogs,
  fetchEmailRecipients,
  previewEmail,
  sendEmail,
  uploadEmailImage,
  type EmailImageItem,
  type EmailLogItem,
  type EmailTemplateId,
  type RecipientUser,
} from '@/lib/api/notifications';

const DRAFT_KEY = 'email-broadcast-draft-v1';
const PREVIEW_DEBOUNCE_MS = 300;

interface EditorImage extends EmailImageItem {
  width: number;
  thumb?: string;
}

const PRESET_KEYS = Object.keys(EMAIL_PRESET_THEMES) as (keyof typeof EMAIL_PRESET_THEMES)[];

export default function AdminEmailBroadcast() {
  // ── 编辑器状态 ──────────────────────────────────────────────────
  const [templateId, setTemplateId] = useState<EmailTemplateId>('system_update');
  const [preset, setPreset] = useState<keyof typeof EMAIL_PRESET_THEMES>('brand');
  const [customHex, setCustomHex] = useState('');
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [items, setItems] = useState<string[]>(['']);
  const [images, setImages] = useState<EditorImage[]>([]);
  const [accentEnabled, setAccentEnabled] = useState(true);
  const [accentText, setAccentText] = useState('登录后请留意「更新公告」弹窗并确认，即可开始体验新功能。');
  const [buttonEnabled, setButtonEnabled] = useState(true);
  const [buttonUrl, setButtonUrl] = useState('');
  const [hasItems, setHasItems] = useState(true);

  // ── 收件人 ─────────────────────────────────────────────────────
  const [target, setTarget] = useState<'all' | 'selected'>('all');
  const [recipients, setRecipients] = useState<{ total_users: number; with_email: number; items: RecipientUser[] } | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pickerOpen, setPickerOpen] = useState(false);
  const [search, setSearch] = useState('');

  // ── 预览 / 发送 / 历史 ─────────────────────────────────────────
  const [previewHtml, setPreviewHtml] = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [logs, setLogs] = useState<EmailLogItem[]>([]);
  const [logsTotal, setLogsTotal] = useState(0);
  const [logsPage, setLogsPage] = useState(1);
  const [notice, setNotice] = useState<{ text: string; kind: 'ok' | 'err' } | null>(null);
  const [sendToast, setSendToast] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const [viewLog, setViewLog] = useState<EmailLogItem | null>(null);
  const [viewHtml, setViewHtml] = useState('');
  const [viewLoading, setViewLoading] = useState(false);

  const [pendingImageDelete, setPendingImageDelete] = useState<EditorImage | null>(null);
  const [pendingTemplate, setPendingTemplate] = useState<EmailTemplateId | null>(null);
  const [deletingImage, setDeletingImage] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const theme = useMemo(() => {
    const t = { ...EMAIL_PRESET_THEMES[preset] };
    if (/^#[0-9a-fA-F]{6}$/.test(customHex)) t.header_bg = customHex;
    return t;
  }, [preset, customHex]);

  const themeText = useMemo(() => (customHex && /^#[0-9a-fA-F]{6}$/.test(customHex) ? `自定义 ${customHex}` : (preset === 'brand' ? '品牌深蓝' : preset === 'green' ? '墨绿' : preset === 'purple' ? '宫廷紫' : '石墨黑')), [preset, customHex]);

  // ── 初始化：草稿 / 历史 / 收件人 / 图片 ───────────────────────
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(DRAFT_KEY);
      if (raw) {
        const d = JSON.parse(raw) as Record<string, unknown>;
        if (d.subject) setSubject(String(d.subject));
        if (d.body) setBody(String(d.body));
        if (d.template_id) setTemplateId(d.template_id as EmailTemplateId);
        if (d.items && Array.isArray(d.items)) setItems(d.items as string[]);
        if (typeof d.accent_enabled === 'boolean') setAccentEnabled(d.accent_enabled);
        if (d.accent_text) setAccentText(String(d.accent_text));
        if (typeof d.button_enabled === 'boolean') setButtonEnabled(d.button_enabled);
        if (d.button_url) setButtonUrl(String(d.button_url));
      }
    } catch { /* ignore */ }
    void refreshMeta();
    void loadLogs(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refreshMeta = useCallback(async () => {
    try {
      const [rec, imgList] = await Promise.all([fetchEmailRecipients(), fetchEmailImages()]);
      setRecipients(rec);
      const withThumbs: EditorImage[] = await Promise.all(
        imgList.map(async (img) => {
          let thumb = '';
          try { thumb = await fetchEmailImageData(img.image_id); } catch { /* ignore */ }
          return { ...img, width: 360, thumb };
        }),
      );
      setImages(withThumbs);
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '初始化失败。', kind: 'err' });
    }
  }, []);

  const LOGS_PAGE_SIZE = 5;
  const logsPageCount = Math.max(1, Math.ceil(logsTotal / LOGS_PAGE_SIZE));

  const loadLogs = useCallback(async (p: number) => {
    try {
      const res = await fetchEmailLogs(p, LOGS_PAGE_SIZE);
      setLogs(res.items);
      setLogsTotal(res.total);
      if (res.items.length === 0 && res.total > 0 && p > 1) {
        const last = Math.ceil(res.total / LOGS_PAGE_SIZE);
        setLogsPage(last);
        const res2 = await fetchEmailLogs(last, LOGS_PAGE_SIZE);
        setLogs(res2.items);
        return;
      }
      setLogsPage(p);
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '发送历史读取失败。', kind: 'err' });
    }
  }, []);

  // ── 模板切换：示例文案自动替换正文（提示将覆盖）──────────────
  const applyTemplate = useCallback(async (id: EmailTemplateId) => {
    const sample = EMAIL_TEMPLATE_SAMPLES[id];
    setTemplateId(id);
    setSubject(sample.subject);
    setBody(sample.body);
    setItems(sample.items.length ? sample.items : ['']);
    setHasItems(sample.items.length > 0);
    const t = EMAIL_TEMPLATES.find((x) => x.id === id);
    if (t && (t.id === 'system_update' || t.id === 'feature_launch')) setHasItems(true);
  }, []);

  // ── 实时预览（300ms debounce）─────────────────────────────────
  useEffect(() => {
    const timer = setTimeout(() => {
      if (!subject.trim() && !body.trim()) { setPreviewHtml(''); return; }
      setPreviewLoading(true);
      previewEmail({
        subject,
        template_id: templateId,
        body,
        images: images.map((im) => ({ image_id: im.image_id, width: im.width })),
        items: items.map((s) => s.trim()).filter(Boolean),
        accent: { enabled: accentEnabled, text: accentText },
        button: { enabled: buttonEnabled, url: buttonUrl, text: '前往工作台' },
        theme,
      })
        .then(setPreviewHtml)
        .catch(() => setPreviewHtml((prev) => prev))
        .finally(() => setPreviewLoading(false));
    }, PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [subject, body, templateId, images, items, accentEnabled, accentText, buttonEnabled, buttonUrl, theme]);

  // ── 图片操作 ──────────────────────────────────────────────────
  const handleUpload = async (file: File) => {
    if (!file) return;
    // 客户端预检（后端仍会兜底校验）
    if (file.size > 2 * 1024 * 1024) { setNotice({ text: '图片不能超过 2MB。', kind: 'err' }); return; }
    setUploading(true);
    try {
      const meta = await uploadEmailImage(file);
      const objectUrl = URL.createObjectURL(file);
      setImages((prev) => [...prev, { ...meta, width: 360 }]);
      try {
        const uri = await fetchEmailImageData(meta.image_id);
        setImages((prev) => prev.map((im) => (im.image_id === meta.image_id ? { ...im, thumb: uri } : im)));
      } catch { setImages((prev) => prev.map((im) => (im.image_id === meta.image_id ? { ...im, thumb: objectUrl } : im))); }
      setNotice({ text: '图片已上传，可在正文中调整大小。', kind: 'ok' });
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '上传失败。', kind: 'err' });
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleImageDelete = async () => {
    if (!pendingImageDelete) return;
    setDeletingImage(true);
    try {
      await deleteEmailImage(pendingImageDelete.image_id);
      setImages((prev) => prev.filter((im) => im.image_id !== pendingImageDelete.image_id));
      setPendingImageDelete(null);
      setNotice({ text: '图片已删除。', kind: 'ok' });
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '删除失败。', kind: 'err' });
    } finally {
      setDeletingImage(false);
    }
  };

  // 拖拽等比缩放：pointer 事件调整宽度（height 自动等比）
  const resizeState = useRef<{ id: string; startX: number; startW: number } | null>(null);
  const beginResize = (id: string, startW: number) => (e: React.PointerEvent) => {
    e.preventDefault();
    resizeState.current = { id, startX: e.clientX, startW };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    const onMove = (ev: PointerEvent) => {
      if (!resizeState.current) return;
      const w = Math.max(60, Math.min(800, resizeState.current.startW + (ev.clientX - resizeState.current.startX)));
      setImages((prev) => prev.map((im) => (im.image_id === resizeState.current!.id ? { ...im, width: Math.round(w) } : im)));
    };
    const onUp = () => {
      resizeState.current = null;
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };

  // ── 草稿 ──────────────────────────────────────────────────────
  const saveDraft = () => {
    window.localStorage.setItem(DRAFT_KEY, JSON.stringify({
      template_id: templateId,
      subject, body, items: items.filter((s) => s.trim()),
      accent_enabled: accentEnabled, accent_text: accentText,
      button_enabled: buttonEnabled, button_url: buttonUrl,
    }));
    setNotice({ text: '草稿已保存到本地浏览器。', kind: 'ok' });
  };

  // ── 发送 ──────────────────────────────────────────────────────
  const handleSend = async () => {
    if (!subject.trim()) { setNotice({ text: '请填写邮件主题。', kind: 'err' }); return; }
    if (!body.trim()) { setNotice({ text: '请填写邮件正文。', kind: 'err' }); return; }
    if (target === 'selected' && selected.size === 0) { setNotice({ text: '请至少选择一位收件人。', kind: 'err' }); return; }
    setSending(true);
    try {
      const summary = await sendEmail({
        subject,
        template_id: templateId,
        body,
        images: images.map((im) => ({ image_id: im.image_id, width: im.width })),
        items: items.map((s) => s.trim()).filter(Boolean),
        accent: { enabled: accentEnabled, text: accentText },
        button: { enabled: buttonEnabled, url: buttonUrl, text: '前往工作台' },
        theme,
        target,
        user_ids: target === 'selected' ? Array.from(selected) : undefined,
      });
      const toastText = `发送完成：目标 ${summary.target_count} 人，成功 ${summary.sent} 封，跳过（无邮箱）${summary.skipped}，失败 ${summary.failed}。`;
      setNotice({ text: toastText, kind: summary.failed ? 'err' : 'ok' });
      setSendToast({ kind: summary.failed ? 'err' : 'ok', text: toastText });
      window.setTimeout(() => setSendToast(null), 5000);
      await loadLogs(1);
      if (summary.failed > 0 && summary.errors?.length) {
        const detail = `发送完成：成功 ${summary.sent}，失败 ${summary.failed}（${summary.errors[0].email}：${summary.errors[0].error}）`;
        setNotice({ text: detail, kind: 'err' });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : '发送失败。';
      setNotice({ text: msg, kind: 'err' });
      setSendToast({ kind: 'err', text: msg });
      window.setTimeout(() => setSendToast(null), 5000);
    } finally {
      setSending(false);
    }
  };

  // 历史邮件查看：拉取详情（记录 + 重新渲染 HTML）
  const openLogDetail = async (log: EmailLogItem) => {
    setViewLog(log);
    setViewHtml('');
    setViewLoading(true);
    try {
      const detail = await fetchEmailLogDetail(log.email_log_id);
      setViewHtml(detail.html);
    } catch (err) {
      setViewHtml('');
      setNotice({ text: err instanceof Error ? err.message : '发送记录读取失败。', kind: 'err' });
    } finally {
      setViewLoading(false);
    }
  };

  const realItems = items.map((s) => s.trim()).filter(Boolean);

  // ── 渲染 ──────────────────────────────────────────────────────
  const inputCls = 'w-full rounded-md border border-line-soft bg-white px-3 py-2 text-sm text-ink outline-none transition-shadow focus:border-brand focus:ring-2 focus:ring-brand/20';
  const labelCls = 'mb-1.5 block text-xs font-semibold text-body';
  const segOn = 'border-line-soft bg-brand-soft text-brand font-semibold border-r border-line-soft';
  const segOff = 'text-sub';

  const dismissNotice = () => setNotice(null);

  return (
    <div className="mt-6">
      {notice && (
        <div className={`mb-5 flex items-center justify-between rounded-md border px-4 py-2.5 text-sm ${
          notice.kind === 'ok' ? 'border-good-border bg-good-soft text-good-deep' : 'border-bad-border bg-bad-soft text-bad'
        }`}>
          <span>{notice.text}</span>
          <button type="button" onClick={dismissNotice} aria-label="关闭提示" className="ml-3 opacity-60 hover:opacity-100">✕</button>
        </div>
      )}
      <div className="grid gap-5 xl:grid-cols-[440px_1fr]">
      {/* ═══ 左：清爽编辑表单 ═══ */}
      <div className="space-y-5">
        {/* 模板 + 主题 */}
        <div className="rounded-md border border-line bg-white p-6">
          <h2 className="text-base font-semibold text-ink">编写通知</h2>
          <div className="mt-4">
            <label className={labelCls}>模板排版 <span className="text-[10px] font-normal text-sub">切模板自动替换正文（提示覆盖）</span></label>
            <div className="grid grid-cols-2 gap-2">
              {EMAIL_TEMPLATES.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => {
                    if (subject.trim() || body.trim() || realItems.length) setPendingTemplate(t.id);
                    else void applyTemplate(t.id);
                  }}
                  className={`rounded-md border px-3 py-2 text-left transition-colors ${
                    templateId === t.id ? 'border-brand bg-brand-soft' : 'border-line-soft bg-white hover:bg-mist'
                  }`}
                >
                  <div className={`text-[13px] font-semibold ${templateId === t.id ? 'text-brand' : 'text-ink'}`}>{t.name}</div>
                  <div className="mt-0.5 text-[11px] text-sub">{t.desc}</div>
                </button>
              ))}
            </div>
          </div>
          <div className="mt-4">
            <label className={labelCls}>主题颜色</label>
            <div className="flex flex-wrap items-center gap-2">
              {PRESET_KEYS.map((k) => (
                <button
                  key={k}
                  type="button"
                  title={k}
                  onClick={() => { setPreset(k); setCustomHex(''); }}
                  className={`relative size-7 rounded-full border-2 border-white shadow-[0_0_0_1.5px_var(--color-line)] transition-shadow ${
                    preset === k && !customHex ? 'shadow-[0_0_0_2px_var(--color-brand)]' : ''
                  }`}
                  style={{ background: EMAIL_PRESET_THEMES[k].header_bg }}
                >
                  {preset === k && !customHex ? <span className="absolute inset-0 flex items-center justify-center text-[11px] font-bold text-white">✓</span> : null}
                </button>
              ))}
              <div className="flex items-center gap-1.5">
                <span className="text-xs text-sub">自定义</span>
                <input
                  type="text"
                  value={customHex}
                  onChange={(e) => setCustomHex(e.target.value.trim())}
                  placeholder="#17243B"
                  maxLength={7}
                  className="w-24 rounded-md border border-line-soft px-2 py-1 text-xs text-ink outline-none focus:border-brand"
                />
                <span className="size-5 rounded border border-line-soft" style={{ background: /^#[0-9a-fA-F]{6}$/.test(customHex) ? customHex : '#fff' }} />
              </div>
            </div>
          </div>
        </div>

        {/* 内容 */}
        <div className="rounded-md border border-line bg-white p-6">
          <div className="">
            <label className={labelCls}>邮件主题</label>
            <input className={inputCls} value={subject} maxLength={200} onChange={(e) => setSubject(e.target.value)} placeholder="【系统更新】…" />
          </div>
          <div className="mt-4">
            <label className={labelCls}>邮件正文 <span className="text-[10px] font-normal text-sub">纯文本/段落 · 空行分段</span></label>
            <textarea className={inputCls + ' min-h-40 leading-7'} value={body} onChange={(e) => setBody(e.target.value)} placeholder={'您好：\n\n请输入正文内容……'} />
          </div>

          {hasItems && (
            <div className="mt-4">
              <label className={labelCls}>要点列表 <span className="text-[10px] font-normal text-sub">模板排版自带 · 渲染在正文之后、图片上方</span></label>
              <div className="space-y-2">
                {items.map((it, idx) => (
                  <div key={idx} className="flex items-center gap-2">
                    <span className="flex size-4 shrink-0 items-center justify-center rounded bg-good text-[10px] text-white">✓</span>
                    <input className={inputCls} value={it} maxLength={300} onChange={(e) => setItems((prev) => prev.map((v, i) => (i === idx ? e.target.value : v)))} placeholder="要点内容…" />
                    <button type="button" aria-label="删除" className="flex size-6 shrink-0 items-center justify-center rounded border border-line-soft text-sub hover:bg-mist" onClick={() => setItems((prev) => (prev.length === 1 ? [''] : prev.filter((_, i) => i !== idx)))}>✕</button>
                  </div>
                ))}
              </div>
              <button type="button" onClick={() => setItems((prev) => [...prev, ''])} className="mt-2 text-xs font-medium text-brand">＋ 添加一条要点</button>
            </div>
          )}

          {/* 已插入图片 */}
          <div className="mt-4">
            <label className={labelCls}>邮件图片 <span className="text-[10px] font-normal text-sub">PNG/JPG/WebP ≤2MB · 最多3张 · 拖拽手柄等比缩放 · 删除后正文一并移除</span></label>
            {images.length === 0 ? (
              <button type="button" onClick={() => fileInputRef.current?.click()} disabled={uploading} className="flex w-full items-center justify-center gap-2 rounded-md border border-dashed border-line py-6 text-sm text-sub transition-colors hover:bg-mist disabled:opacity-50">
                {uploading ? <LoaderCircleIcon className="size-4 animate-spin" /> : <ImagePlusIcon className="size-4" />} 上传图片
              </button>
            ) : (
              <div className="space-y-2">
                {images.map((img) => (
                  <div key={img.image_id} className="flex items-center gap-3 rounded-md border border-line-soft p-2">
                    <div className="h-10 w-14 shrink-0 overflow-hidden rounded-md border border-line-soft bg-mist">
                      {img.thumb ? <img src={img.thumb} alt="" className="h-full w-full object-cover" /> : null}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-xs font-medium text-ink">{img.name}</div>
                      <div className="mt-0.5 text-[11px] text-sub">
                        宽度 <span className="font-semibold text-brand">{img.width}px</span>（等比） · 拖右侧手柄调整
                      </div>
                    </div>
                    <div
                      role="slider"
                      aria-label="调整宽度"
                      aria-valuemin={60}
                      aria-valuemax={800}
                      aria-valuenow={img.width}
                      onPointerDown={beginResize(img.image_id, img.width)}
                      className="cursor-ew-resize select-none rounded-md border border-brand/40 bg-brand-soft px-2 py-3 text-brand"
                      title="拖拽调整宽度（等比缩放）"
                    >
                      ⇔
                    </div>
                    <button type="button" onClick={() => setPendingImageDelete(img)} className="rounded border border-bad-border/60 px-2 py-1 text-xs text-bad hover:bg-bad-soft">删除</button>
                  </div>
                ))}
                {images.length < 3 && (
                  <button type="button" onClick={() => fileInputRef.current?.click()} disabled={uploading} className="flex w-full items-center justify-center gap-2 rounded-md border border-dashed border-line py-3 text-xs text-sub transition-colors hover:bg-mist disabled:opacity-50">
                    {uploading ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <ImagePlusIcon className="size-3.5" />} 再上传一张
                  </button>
                )}
              </div>
            )}
            <input ref={fileInputRef} type="file" accept="image/png,image/jpeg,image/webp" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) void handleUpload(f); }} />
          </div>
        </div>

        {/* 提示块 + 按钮 + 收件人 */}
        <div className="rounded-md border border-line bg-white p-6">
          <div className="flex items-center gap-3">
            <span className="w-20 shrink-0 text-xs font-semibold text-body">蓝色提示块</span>
            <div className="flex overflow-hidden rounded-md border border-line-soft">
              <button type="button" onClick={() => setAccentEnabled(true)} className={`px-3 py-1 text-xs ${accentEnabled ? segOn : segOff}`}>显示</button>
              <button type="button" onClick={() => setAccentEnabled(false)} className={`px-3 py-1 text-xs ${!accentEnabled ? segOn : segOff}`}>隐藏</button>
            </div>
          </div>
          {accentEnabled && (
            <input className={inputCls + ' mt-2'} value={accentText} maxLength={500} onChange={(e) => setAccentText(e.target.value)} placeholder="提示文案…" />
          )}
          <div className="mt-4 flex items-center gap-3">
            <span className="w-20 shrink-0 text-xs font-semibold text-body">工作台按钮</span>
            <div className="flex overflow-hidden rounded-md border border-line-soft">
              <button type="button" onClick={() => setButtonEnabled(true)} className={`px-3 py-1 text-xs ${buttonEnabled ? segOn : segOff}`}>显示</button>
              <button type="button" onClick={() => setButtonEnabled(false)} className={`px-3 py-1 text-xs ${!buttonEnabled ? segOn : segOff}`}>隐藏</button>
            </div>
          </div>
          {buttonEnabled && (
            <input className={inputCls + ' mt-2'} value={buttonUrl} maxLength={500} onChange={(e) => setButtonUrl(e.target.value)} placeholder="https://… 或 / 相对路径" />
          )}
          <div className="mt-4 flex items-center gap-3">
            <span className="w-20 shrink-0 text-xs font-semibold text-body">收件人</span>
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-1.5 text-xs text-body">
                <input type="radio" checked={target === 'all'} onChange={() => setTarget('all')} />
                全部用户<span className="text-sub">（{recipients?.with_email ?? '--'}人有邮箱）</span>
              </label>
              <label className="flex items-center gap-1.5 text-xs text-body">
                <input type="radio" checked={target === 'selected'} onChange={() => setTarget('selected')} />
                指定用户
              </label>
              {target === 'selected' && (
                <button type="button" onClick={() => setPickerOpen(true)} className="inline-flex items-center gap-1 rounded-md border border-line-soft px-2.5 py-1 text-xs font-medium text-ink hover:bg-mist">
                  <UsersIcon className="size-3.5" /> 选择（{selected.size}）
                </button>
              )}
            </div>
          </div>

          <div className="mt-5 flex items-center gap-2 border-t border-line-soft pt-4">
            <button
              type="button"
              onClick={() => void handleSend()}
              disabled={sending}
              className="inline-flex h-10 flex-1 items-center justify-center gap-2 rounded-md bg-brand-deep px-5 text-sm font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-60"
            >
              {sending ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SendIcon className="size-4" />} 发送通知
            </button>
            <button type="button" onClick={saveDraft} className="inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist">存为草稿</button>
          </div>
        </div>
      </div>

      {/* ═══ 右：实时预览 + 发送历史 ═══ */}
      <div className="space-y-5">
        <div className="rounded-md border border-line bg-white">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line-soft px-5 py-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-ink">
              <span className="relative flex size-2">
                <span className="absolute inline-flex size-full animate-ping rounded-full bg-good opacity-50" />
                <span className="relative inline-flex size-2 rounded-full bg-good" />
              </span>
              实时预览
              <span className="text-[11px] font-normal text-sub">与收件人看到的一致</span>
            </div>
            <div className="flex flex-wrap gap-1.5 text-[11px] text-sub">
              <span className="rounded-full border border-line-soft bg-mist px-2 py-0.5">模板：{EMAIL_TEMPLATES.find((t) => t.id === templateId)?.name}</span>
              <span className="rounded-full border border-line-soft bg-mist px-2 py-0.5">主题：{themeText}</span>
              <span className="rounded-full border border-line-soft bg-mist px-2 py-0.5">图片 ×{images.length}</span>
              <span className="rounded-full border border-line-soft bg-mist px-2 py-0.5">提示块：{accentEnabled ? '显示' : '隐藏'}</span>
            </div>
          </div>
          <div className="relative bg-[#e9edf4] p-4">
            {previewLoading && (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/40">
                <span className="flex items-center gap-2 rounded-md border border-line bg-white px-3 py-1.5 text-xs text-sub shadow-sm"><LoaderCircleIcon className="size-3.5 animate-spin" /> 刷新中</span>
              </div>
            )}
            {previewHtml ? (
              <iframe title="邮件实时预览" sandbox="" srcDoc={previewHtml} className="mx-auto block h-[640px] w-full max-w-[560px] rounded-md border border-line bg-white" />
            ) : (
              <div className="mx-auto flex h-[300px] w-full max-w-[560px] items-center justify-center rounded-md border border-line bg-white text-sm text-sub">
                填写主题与正文后，此处实时展示邮件效果
              </div>
            )}
          </div>
        </div>

        {/* 发送历史（页面滚动时固定；固定窗口分页） */}
        <div className="sticky top-5 rounded-md border border-line bg-white">
          <div className="flex items-center justify-between border-b border-line-soft px-5 py-3">
            <h3 className="flex items-center gap-2 text-sm font-semibold text-ink"><MailIcon className="size-4 text-brand" /> 发送历史</h3>
            <button
              type="button"
              onClick={async () => { setRefreshing(true); try { await loadLogs(logsPage); } finally { setRefreshing(false); } }}
              disabled={refreshing}
              className="rounded border border-line-soft px-2 py-1 text-xs text-sub hover:bg-mist disabled:opacity-50"
            >{refreshing ? '刷新中…' : '刷新'}</button>
          </div>
          {logs.length === 0 ? (
            <div className="py-10 text-center text-sm text-sub">暂无发送记录，发送后在此查看结果。</div>
          ) : (
            <div className="max-h-80 divide-y divide-line-soft overflow-y-auto">
              {logs.map((log) => (
                <div key={log.email_log_id} className="px-5 py-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{log.subject}</span>
                    <button
                      type="button"
                      onClick={() => void openLogDetail(log)}
                      className="inline-flex items-center gap-1 rounded border border-brand/40 bg-brand-soft px-2 py-0.5 text-[11px] font-medium text-brand hover:bg-brand-soft/70"
                    >
                      查看内容
                    </button>
                    <Badge cls="border-line-soft bg-mist text-sub">{log.target === 'all' ? '全员' : '指定'}</Badge>
                    <Badge cls="border-good-border bg-good-soft text-good-deep">成功 {log.sent}</Badge>
                    {log.skipped > 0 && <Badge cls="border-warn-border bg-warn-soft text-warn">跳过 {log.skipped}</Badge>}
                    {log.failed > 0 && <Badge cls="border-bad-border bg-bad-soft text-bad">失败 {log.failed}</Badge>}
                  </div>
                  <div className="mt-1 text-xs text-sub">
                    {new Date(log.created_at || Date.now()).toLocaleString('zh-CN')} · 目标 {log.target_count} 人
                    {log.failed > 0 && log.errors?.length ? <span className="ml-2 text-bad cursor-help" title={log.errors.map((e) => `${e.email}：${e.error}`).join('\n')}>详情</span> : null}
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="flex items-center justify-center gap-1 border-t border-line-soft px-5 py-3">
              <button
                type="button"
                disabled={logsPage <= 1 || refreshing}
                onClick={() => { if (logsPage > 1) void loadLogs(logsPage - 1); }}
                className="rounded-md border border-line-soft bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-mist disabled:opacity-40"
              >
                ‹ 上一页
              </button>
              {Array.from({ length: logsPageCount }, (_, i) => i + 1).map((pnum) => (
                <button
                  key={pnum}
                  type="button"
                  onClick={() => { if (pnum !== logsPage) void loadLogs(pnum); }}
                  className={`min-w-8 rounded-md border px-2 py-1.5 text-xs transition-colors ${
                    pnum === logsPage ? 'border-brand bg-brand-deep text-white font-semibold' : 'border-line-soft bg-white text-ink hover:bg-mist'
                  }`}
                >
                  {pnum}
                </button>
              ))}
              <button
                type="button"
                disabled={logsPage >= logsPageCount || refreshing}
                onClick={() => { if (logsPage < logsPageCount) void loadLogs(logsPage + 1); }}
                className="rounded-md border border-line-soft bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-mist disabled:opacity-40"
              >
                下一页 ›
              </button>
              <span className="ml-2 text-xs text-sub">共 {logsTotal} 条 · 第 {logsPage}/{logsPageCount} 页</span>
            </div>
        </div>
      </div>

      {/* 选人弹窗 */}
      {pickerOpen && (
      <AdminModal
        title={<>选择收件人</>}
        onClose={() => setPickerOpen(false)}
        maxWidth="max-w-lg"
      >
        <input
          value={search}
          onChange={async (e) => {
            setSearch(e.target.value);
            try { setRecipients(await fetchEmailRecipients(e.target.value)); } catch { /* ignore */ }
          }}
          placeholder="搜索用户名 / 邮箱…"
          className="w-full rounded-md border border-line-soft px-3 py-2 text-sm text-ink outline-none focus:border-brand"
        />
        <div className="mt-3 max-h-72 space-y-1 overflow-y-auto">
          {(recipients?.items || []).map((u) => (
            <label key={u.user_id} className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-body hover:bg-mist">
              <input
                type="checkbox"
                checked={selected.has(u.user_id)}
                onChange={(e) => {
                  const next = new Set(selected);
                  if (e.target.checked) next.add(u.user_id); else next.delete(u.user_id);
                  setSelected(next);
                }}
              />
              <span className="font-medium text-ink">{u.username}</span>
              <span className="text-xs text-sub">{u.email}</span>
            </label>
          ))}
          {recipients && recipients.items.length === 0 && <div className="py-8 text-center text-sm text-sub">没有匹配的用户。</div>}
        </div>
      </AdminModal>
      )}

      <ConfirmDialog
        open={Boolean(pendingImageDelete)}
        title="删除该图片？"
        message={<>删除后<strong>正文中对该图片的引用将一并移除</strong>（功能设计定稿 §8.1）。</>}
        confirmLabel="删除"
        danger
        busy={deletingImage}
        onConfirm={() => void handleImageDelete()}
        onCancel={() => { if (!deletingImage) setPendingImageDelete(null); }}
      />
      <ConfirmDialog
        open={Boolean(pendingTemplate)}
        title="切换模板将覆盖当前正文？"
        message={<>切换模板会用内置示例文案<strong>替换当前已编辑的主题/正文/要点</strong>，此操作不可撤销。</>}
        confirmLabel="覆盖并切换"
        onConfirm={() => { if (pendingTemplate) void applyTemplate(pendingTemplate); setPendingTemplate(null); }}
        onCancel={() => setPendingTemplate(null)}
      />

      {/* 历史邮件内容查看 */}
      {viewLog && (
        <AdminModal
          title={<>历史邮件内容 · {viewLog.subject}</>}
          onClose={() => setViewLog(null)}
          maxWidth="max-w-2xl"
        >
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-sub">
            <span>发送时间：{new Date(viewLog.created_at || Date.now()).toLocaleString('zh-CN')}</span>
            <span>对象：{viewLog.target === 'all' ? '全员' : '指定'}（{viewLog.target_count} 人）</span>
            <span>成功 {viewLog.sent}</span>
            {viewLog.skipped > 0 && <span>跳过 {viewLog.skipped}</span>}
            {viewLog.failed > 0 && <span className="text-bad">失败 {viewLog.failed}</span>}
            <span>操作人：{viewLog.created_by_name || '--'}</span>
          </div>
          {viewLog.failed > 0 && viewLog.errors?.length ? (
            <div className="mt-2 max-h-28 overflow-y-auto rounded-md border border-bad-border/60 bg-bad-soft px-3 py-2 text-xs text-bad">
              {viewLog.errors.map((e, i) => <div key={i}>{e.email}：{e.error}</div>)}
            </div>
          ) : null}
          <div className="mt-3 overflow-hidden rounded-md border border-line bg-[#e9edf4] p-3">
            {viewLoading ? (
              <div className="flex h-72 items-center justify-center text-xs text-sub"><LoaderCircleIcon className="mr-2 size-4 animate-spin" /> 加载中</div>
            ) : viewHtml ? (
              <iframe title="历史邮件内容" sandbox="" srcDoc={viewHtml} className="mx-auto block h-[560px] w-full max-w-[520px] rounded-md border border-line bg-white" />
            ) : (
              <div className="flex h-40 items-center justify-center text-xs text-sub">该记录无法渲染（内容可能已清理）。</div>
            )}
          </div>
        </AdminModal>
      )}

      {/* 发送结果浮层（固定在视口右下角，自动消失） */}
      {sendToast && (
        <div
          role="status"
          className={`fixed bottom-6 right-6 z-50 flex max-w-md items-start gap-2.5 rounded-md border bg-white px-4 py-3 text-sm shadow-2xl ${
            sendToast.kind === 'ok' ? 'border-good-border text-good-deep' : 'border-bad-border text-bad'
          }`}
        >
          {sendToast.kind === 'ok' ? <CheckCircle2Icon className="mt-0.5 size-4 shrink-0" /> : <XCircleIcon className="mt-0.5 size-4 shrink-0" />}
          <span className="min-w-0 flex-1 leading-6">{sendToast.text}</span>
          <button type="button" onClick={() => setSendToast(null)} aria-label="关闭提示" className="shrink-0 opacity-50 hover:opacity-100">✕</button>
        </div>
      )}
      </div>
    </div>
  );
}

function Badge({ children, cls }: { children: React.ReactNode; cls: string }) {
  return <span className={`inline-flex items-center rounded border px-2 py-0.5 text-[11px] font-medium ${cls}`}>{children}</span>;
}
