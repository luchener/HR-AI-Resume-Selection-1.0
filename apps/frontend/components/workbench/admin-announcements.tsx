'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  BanIcon,
  CalendarDaysIcon,
  CheckCircle2Icon,
  ClockIcon,
  LoaderCircleIcon,
  MegaphoneIcon,
  SendIcon,
} from 'lucide-react';
import ConfirmDialog from './confirm-dialog';
import {
  cancelAnnouncement,
  createAnnouncement,
  deleteAnnouncement,
  fetchAnnouncements,
  type AnnouncementItem,
} from '@/lib/api/notifications';

type Unit = 'minute' | 'hour' | 'day';
const UNIT_LABELS: Record<Unit, string> = { minute: '分钟', hour: '小时', day: '天' };
const START_LABELS = { now: '立即', at: '指定时间', delay: '延迟' } as const;

const STATUS_META: Record<string, { label: string; cls: string }> = {
  active: { label: '投放中', cls: 'border-good-border bg-good-soft text-good-deep' },
  scheduled: { label: '待投放', cls: 'border-[#cddbf6] bg-brand-soft text-brand' },
  expired: { label: '已过期', cls: 'border-line-soft bg-mist text-sub' },
  cancelled: { label: '已撤回', cls: 'border-bad-border bg-bad-soft text-bad' },
};

function fmtTime(iso?: string): string {
  if (!iso) return '--';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '--';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function AdminAnnouncements() {
  const [items, setItems] = useState<AnnouncementItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState<{ text: string; kind: 'ok' | 'err' } | null>(null);

  // 表单
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [startMode, setStartMode] = useState<'now' | 'at' | 'delay'>('now');
  const [delayValue, setDelayValue] = useState('30');
  const [delayUnit, setDelayUnit] = useState<Unit>('minute');
  const [atLocal, setAtLocal] = useState('');
  const [durationValue, setDurationValue] = useState('1');
  const [durationUnit, setDurationUnit] = useState<Unit>('day');

  const [pendingCancel, setPendingCancel] = useState<AnnouncementItem | null>(null);
  const [pendingDelete, setPendingDelete] = useState<AnnouncementItem | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const PAGE_SIZE = 5;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const load = useCallback(async (targetPage?: number) => {
    const p = targetPage ?? 1;
    try {
      const res = await fetchAnnouncements(p, PAGE_SIZE);
      setItems(res.items);
      setTotal(res.total);
      // 删除后当前页可能变空：回退到最后一页
      if (res.items.length === 0 && res.total > 0 && p > 1) {
        const last = Math.ceil(res.total / PAGE_SIZE);
        setPage(last);
        await load(last);
        return;
      }
      setPage(p);
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '公告列表读取失败。', kind: 'err' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const fullError = async (err: unknown): Promise<string> => (err instanceof Error ? err.message : '操作失败。');
  const flash = (text: string, kind: 'ok' | 'err') => setNotice({ text, kind });

  async function handleCreate() {
    if (!title.trim()) { flash('请填写公告标题。', 'err'); return; }
    if (!content.trim()) { flash('请填写公告内容。', 'err'); return; }
    const duration = Number(durationValue);
    if (!Number.isFinite(duration) || duration < 1) { flash('请填写有效的有效期（≥1）。', 'err'); return; }
    setSaving(true);
    try {
      const start_at = startMode === 'at' && atLocal ? new Date(atLocal).toISOString() : '';
      await createAnnouncement({
        title: title.trim(),
        content: content.trim(),
        start_mode: startMode,
        delay_value: Number(delayValue) || 0,
        delay_unit: delayUnit,
        duration_value: duration,
        duration_unit: durationUnit,
        start_at,
      });
      flash('公告已发布，全员将在投放期收到弹窗。', 'ok');
      setTitle(''); setContent(''); setStartMode('now');
      await load();
    } catch (err) {
      flash(await fullError(err), 'err');
    } finally {
      setSaving(false);
    }
  }

  async function handleCancel() {
    if (!pendingCancel) return;
    setCancelling(true);
    try {
      await cancelAnnouncement(pendingCancel.announcement_id);
      flash('公告已撤回，将不再弹出。', 'ok');
      setPendingCancel(null);
      await load(page);
    } catch (err) {
      flash(await fullError(err), 'err');
    } finally {
      setCancelling(false);
    }
  }

  async function handleDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await deleteAnnouncement(pendingDelete.announcement_id);
      flash('公告已删除（删除记录已保留，可在操作记录中追溯）。', 'ok');
      setPendingDelete(null);
      await load(page);
    } catch (err) {
      flash(await fullError(err), 'err');
    } finally {
      setDeleting(false);
    }
  }

  const inputCls = 'w-full rounded-md border border-line-soft bg-white px-3 py-2 text-sm text-ink outline-none transition-shadow focus:border-brand focus:ring-2 focus:ring-brand/20';
  const labelCls = 'mb-1.5 block text-xs font-semibold text-body';

  const dismissNotice = () => setNotice(null);

  return (
    <div className="mt-6">
      {notice && (
        <div className={`mb-4 flex items-center justify-between rounded-md border px-4 py-2.5 text-sm ${
          notice.kind === 'ok' ? 'border-good-border bg-good-soft text-good-deep' : 'border-bad-border bg-bad-soft text-bad'
        }`}>
          <span>{notice.text}</span>
          <button type="button" onClick={dismissNotice} aria-label="关闭提示" className="ml-3 text-current/60 hover:text-current">✕</button>
        </div>
      )}
      <div className="grid gap-5 xl:grid-cols-[420px_1fr]">
      {/* 左：发布表单 */}
      <div className="rounded-md border border-line bg-white p-6">
        <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
          <MegaphoneIcon className="size-4 text-brand" /> 发布公告
        </h2>
        <p className="mt-1 text-xs text-sub">公告将以弹窗形式推送给全部用户（本期仅全员）</p>

        <div className="mt-5">
          <label className={labelCls}>公告标题</label>
          <input className={inputCls} value={title} maxLength={100} onChange={(e) => setTitle(e.target.value)} placeholder="例如：系统将于本周日维护" />
        </div>

        <div className="mt-4">
          <label className={labelCls}>公告内容</label>
          <textarea className={inputCls + ' min-h-32 leading-7'} value={content} maxLength={5000} onChange={(e) => setContent(e.target.value)} placeholder={'输入公告正文，支持多行：\n\n本周日 02:00-04:00 进行例行维护……'} />
        </div>

        <div className="mt-4">
          <label className={labelCls}>投放时间（开始时间 + 有效期）</label>
          <div className="flex flex-wrap items-center gap-2">
            {(['now', 'at', 'delay'] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setStartMode(m)}
                className={`rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
                  startMode === m ? 'border-brand bg-brand-soft text-brand' : 'border-line-soft bg-white text-body hover:bg-mist'
                }`}
              >
                {START_LABELS[m]}
              </button>
            ))}
          </div>
          {startMode === 'at' && (
            <input type="datetime-local" value={atLocal} onChange={(e) => setAtLocal(e.target.value)} className={inputCls + ' mt-2'} />
          )}
          {startMode === 'delay' && (
            <div className="mt-2 flex items-center gap-2 text-sm text-body">
              <span>延迟</span>
              <input type="number" min={0} value={delayValue} onChange={(e) => setDelayValue(e.target.value)} className={inputCls + ' w-24'} />
              <select value={delayUnit} onChange={(e) => setDelayUnit(e.target.value as Unit)} className={inputCls + ' w-24'}>
                {(['minute', 'hour', 'day'] as Unit[]).map((u) => <option key={u} value={u}>{UNIT_LABELS[u]}</option>)}
              </select>
              <span>后开始</span>
            </div>
          )}
          <div className="mt-2 flex items-center gap-2 text-sm text-body">
            <span>有效期</span>
            <input type="number" min={1} value={durationValue} onChange={(e) => setDurationValue(e.target.value)} className={inputCls + ' w-24'} />
            <select value={durationUnit} onChange={(e) => setDurationUnit(e.target.value as Unit)} className={inputCls + ' w-24'}>
              {(['minute', 'hour', 'day'] as Unit[]).map((u) => <option key={u} value={u}>{UNIT_LABELS[u]}</option>)}
            </select>
            <span className="text-sub">（到期自动不再弹出）</span>
          </div>
        </div>

        <button
          type="button"
          onClick={handleCreate}
          disabled={saving}
          className="mt-5 inline-flex h-10 w-full items-center justify-center gap-2 rounded-md bg-brand-deep px-5 text-sm font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-60"
        >
          {saving ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SendIcon className="size-4" />}
          发布公告
        </button>
      </div>

      {/* 右：公告列表 */}
      <div className="rounded-md border border-line bg-white">
        <div className="border-b border-line-soft px-6 py-4">
          <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
            <CalendarDaysIcon className="size-4 text-brand" /> 公告列表
          </h2>
        </div>
        <div className="divide-y divide-line-soft">
          {loading ? (
            <div className="flex items-center justify-center py-16 text-sub"><LoaderCircleIcon className="mr-2 size-5 animate-spin" /> 正在载入</div>
          ) : items.length === 0 ? (
            <div className="py-16 text-center text-sm text-sub">暂无公告（或本页无数据），发布后在此展示投放状态。</div>
          ) : items.map((item) => {
            const meta = STATUS_META[item.status] || STATUS_META.expired;
            return (
              <div key={item.announcement_id} className="px-6 py-4">
                <div className="flex flex-wrap items-center gap-3">
                  <h3 className="min-w-0 flex-1 truncate text-sm font-semibold text-ink">{item.title}</h3>
                  <span className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] font-medium ${meta.cls}`}>
                    {item.status === 'cancelled' ? <BanIcon className="size-3" /> : item.status === 'active' ? <CheckCircle2Icon className="size-3" /> : <ClockIcon className="size-3" />}
                    {meta.label}
                  </span>
                  {item.status === 'active' && (
                    <button
                      type="button"
                      onClick={() => setPendingCancel(item)}
                      className="inline-flex items-center gap-1 rounded border border-bad-border/60 px-2 py-1 text-xs font-medium text-bad hover:bg-bad-soft"
                    >
                      撤回
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setPendingDelete(item)}
                    className="inline-flex items-center gap-1 rounded border border-line-soft px-2 py-1 text-xs font-medium text-sub hover:bg-mist hover:text-bad"
                  >
                    删除
                  </button>
                </div>
                <p className="mt-1 line-clamp-2 whitespace-pre-line text-sm leading-6 text-body">{item.content}</p>
                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-sub">
                  <span>投放：{fmtTime(item.start_at)}</span>
                  <span>截止：{fmtTime(item.end_at)}</span>
                  <span>创建：{item.created_by_name || '--'}</span>
                  {item.status === 'cancelled' && <span>撤回：{fmtTime(item.cancelled_at)}</span>}
                </div>
              </div>
            );
          })}
        </div>
        {/* 固定窗口分页栏（常驻，数据不足一页时上一页/下一页禁用）——仅属于公告列表 */}
        <div className="flex items-center justify-center gap-1 border-t border-line-soft px-5 py-3">
          <button
            type="button"
            disabled={page <= 1 || loading}
            onClick={() => { if (page > 1) load(page - 1); }}
            className="rounded-md border border-line-soft bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-mist disabled:opacity-40"
          >
            ‹ 上一页
          </button>
          {Array.from({ length: pageCount }, (_, i) => i + 1).map((pnum) => (
            <button
              key={pnum}
              type="button"
              onClick={() => { if (pnum !== page) load(pnum); }}
              className={`min-w-8 rounded-md border px-2 py-1.5 text-xs transition-colors ${
                pnum === page ? 'border-brand bg-brand-deep text-white font-semibold' : 'border-line-soft bg-white text-ink hover:bg-mist'
              }`}
            >
              {pnum}
            </button>
          ))}
          <button
            type="button"
            disabled={page >= pageCount || loading}
            onClick={() => { if (page < pageCount) load(page + 1); }}
            className="rounded-md border border-line-soft bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-mist disabled:opacity-40"
          >
            下一页 ›
          </button>
          <span className="ml-2 text-xs text-sub">共 {total} 条 · 第 {page}/{pageCount} 页</span>
        </div>
      </div>

      <ConfirmDialog
        open={Boolean(pendingDelete)}
        title="删除该公告？"
        message={<>删除后公告<strong>立即停止弹出且从列表移除</strong>；删除记录会保留，可在「操作记录」中追溯。此操作不可撤销。</>}
        confirmLabel="删除"
        danger
        busy={deleting}
        onConfirm={() => void handleDelete()}
        onCancel={() => { if (!deleting) setPendingDelete(null); }}
      />
      <ConfirmDialog
        open={Boolean(pendingCancel)}
        title="撤回该公告？"
        message={<>撤回后公告将<strong>立即停止弹出</strong>，已阅读的用户关闭记录不受影响。此操作可随时再次发布新公告。</>}
        confirmLabel="撤回"
        danger
        busy={cancelling}
        onConfirm={handleCancel}
        onCancel={() => { if (!cancelling) setPendingCancel(null); }}
      />
      </div>
    </div>
  );
}
