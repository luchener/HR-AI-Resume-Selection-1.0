'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { GaugeIcon, LoaderCircleIcon, PlusIcon, SearchIcon, Settings2Icon, ShieldCheckIcon, UsersIcon } from 'lucide-react';
import ConfirmDialog from './confirm-dialog';
import AdminModal from './admin-modal';
import { fetchQuotaSettings, resetUserQuota, updateQuotaSettings, updateUserQuota, type QuotaSettings, type QuotaUserRow } from '@/lib/api/quota';
import { fetchAdminUsers, type AdminUserItem } from '@/lib/api/auth-admin';

const PAGE_SIZE = 10;

function remainingPill(u: QuotaUserRow) {
  const unlimited = u.daily_limit == null || u.daily_limit < 0;
  if (!u.analysis_enabled) return { label: '已禁用', cls: 'border-bad-border bg-bad-soft text-bad' };
  if (unlimited) return { label: u.is_admin ? '不限 · 管理员' : '不限', cls: 'border-[#cddbf6] bg-brand-soft text-brand' };
  if (u.remaining <= 0) return { label: '已用完', cls: 'border-bad-border bg-bad-soft text-bad' };
  if (u.remaining <= 2) return { label: '剩 ' + u.remaining, cls: 'border-warn-border bg-warn-soft text-warn' };
  return { label: '剩 ' + u.remaining, cls: 'border-good-border bg-good-soft text-good-deep' };
}

function ToggleSwitch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label?: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={'group inline-flex items-center gap-2 rounded-md px-1 py-1 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ' + (disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer hover:bg-mist')}
    >
      {/* 轨道：本体即实体开关；滑块 absolute 定位于轨道内部（relative），不会逃逸到页面 */}
      <span
        aria-hidden="true"
        className={'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors ' + (checked ? 'border-brand-deep bg-brand-deep' : 'border-line bg-soft')}
      >
        <span
          className={'absolute top-0.5 size-4 rounded-full bg-white shadow-sm ring-1 ring-black/5 transition-all duration-150 ' + (checked ? 'left-[18px]' : 'left-0.5')}
        />
      </span>
      {label && <span className={'w-6 text-xs font-medium ' + (disabled ? 'text-disabled-fg' : checked ? 'text-good-deep' : 'text-sub')}>{label}</span>}
    </button>
  );
}
function StatCard({ icon, iconCls, num, label }: { icon: React.ReactNode; iconCls: string; num: number; label: string }) {
  return (
    <div className="flex items-center gap-3 rounded-md border border-line bg-white px-4 py-3">
      <span className={"flex size-9 shrink-0 items-center justify-center rounded-md " + iconCls}>{icon}</span>
      <div>
        <div className="text-lg font-bold leading-6 text-ink">{num}</div>
        <div className="text-xs text-sub">{label}</div>
      </div>
    </div>
  );
}


export default function AdminQuota() {
  const [settings, setSettings] = useState<QuotaSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<{ text: string; kind: 'ok' | 'err' } | null>(null);
  const [saving, setSaving] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [defaultDaily, setDefaultDaily] = useState('');
  const [emailNotify, setEmailNotify] = useState(false);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState<QuotaUserRow | null>(null);
  const [editingLimit, setEditingLimit] = useState('');
  const [editingEnabled, setEditingEnabled] = useState(true);
  const [editingMail, setEditingMail] = useState(true);
  const [editingSaving, setEditingSaving] = useState(false);
  const [adding, setAdding] = useState(false);
  const [candidates, setCandidates] = useState<AdminUserItem[]>([]);
  const [addUser, setAddUser] = useState('');
  const [addLimit, setAddLimit] = useState('');
  const [addEnabled, setAddEnabled] = useState(true);
  const [addMail, setAddMail] = useState(true);
  const [addSaving, setAddSaving] = useState(false);
  const [pendingReset, setPendingReset] = useState<QuotaUserRow | null>(null);
  const [resetting, setResetting] = useState(false);

  /**
   * 读取配额设置。
   * syncGlobal=true 时同时把服务端值回填到全局表单；默认 false —— 账号表格的
   * 任何操作（改限额/开关/重置）都只该刷新列表，不应把管理员还没保存的全局改动冲掉。
   */
  const load = useCallback(async (syncGlobal = false) => {
    try {
      const s = await fetchQuotaSettings();
      setSettings(s);
      if (syncGlobal) {
        setEnabled(s.quota_enabled);
        setDefaultDaily(s.default_daily == null ? '' : String(s.default_daily));
        setEmailNotify(s.email_notify_enabled);
      }
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '配额设置读取失败。', kind: 'err' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(true); }, [load]);

  const filtered = useMemo(() => {
    const q2 = search.trim().toLowerCase();
    const all = settings?.users || [];
    if (!q2) return all;
    return all.filter((u) => u.username.toLowerCase().includes(q2));
  }, [settings, search]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageItems = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [page, pageCount]);

  const stats = useMemo(() => {
    const users = settings?.users || [];
    const todayUsed = users.reduce((a, u) => a + (u.used || 0), 0);
    const restricted = users.filter((u) => u.daily_limit != null && u.daily_limit >= 0).length;
    const exhausted = users.filter((u) => u.daily_limit != null && u.daily_limit >= 0 && u.remaining === 0).length;
    return { count: users.length, todayUsed, restricted, exhausted };
  }, [settings]);

  const inputCls = 'w-full rounded-md border border-line-soft bg-white px-3 py-2 text-sm text-ink outline-none transition-shadow focus:border-brand focus:ring-2 focus:ring-brand/20';
  const labelCls = 'mb-1.5 block text-xs font-semibold text-body';

  async function saveGlobal(overrideEnabled?: boolean, overrideEmail?: boolean) {
    setSaving(true);
    try {
      const useEnabled = overrideEnabled === undefined ? enabled : overrideEnabled;
      const useEmail = overrideEmail === undefined ? emailNotify : overrideEmail;
      const val = defaultDaily.trim() === '' ? null : Number(defaultDaily);
      if (val !== null && (Number.isNaN(val) || val < -1 || val > 1000)) { setNotice({ text: '每日限额需在 -1 ~ 1000 之间（-1 或留空 = 不限）。', kind: 'err' }); return; }
      const s = await updateQuotaSettings({ quota_enabled: useEnabled, default_daily: val, email_notify_enabled: useEmail });
      setSettings(s);
      setEnabled(s.quota_enabled);
      setDefaultDaily(s.default_daily == null ? '' : String(s.default_daily));
      setEmailNotify(s.email_notify_enabled);
      setNotice({ text: useEnabled ? '全局设置已保存（配额限制已启用）。' : '全局设置已保存。', kind: 'ok' });
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '保存失败。', kind: 'err' });
    } finally {
      setSaving(false);
    }
  }

  function openEdit(u: QuotaUserRow) {
    setEditing(u);
    setEditingLimit(u.raw_daily_limit == null ? '' : String(u.raw_daily_limit));
    setEditingEnabled(u.analysis_enabled);
    setEditingMail(u.email_notify);
  }

  async function saveEdit() {
    if (!editing) return;
    setEditingSaving(true);
    try {
      const val = editingLimit.trim() === '' ? null : Number(editingLimit);
      if (val !== null && (Number.isNaN(val) || val < -1 || val > 1000)) { setNotice({ text: '每日限额需在 -1 ~ 1000 之间。', kind: 'err' }); return; }
      await updateUserQuota(editing.username, { daily_limit: val, analysis_enabled: editingEnabled, email_notify: editingMail });
      setEditing(null);
      await load();
      setNotice({ text: '已更新 ' + editing.username + ' 的配额设置。', kind: 'ok' });
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '保存失败。', kind: 'err' });
    } finally {
      setEditingSaving(false);
    }
  }

  async function openAdd() {
    setAdding(true);
    setAddUser('');
    setAddLimit('');
    setAddEnabled(true);
    setAddMail(true);
    try {
      const list = await fetchAdminUsers('', 1, 200);
      setCandidates(list.items);
    } catch {
      setCandidates([]);
    }
  }

  async function saveAdd() {
    if (!addUser.trim()) { setNotice({ text: '请选择用户。', kind: 'err' }); return; }
    setAddSaving(true);
    try {
      const val = addLimit.trim() === '' ? null : Number(addLimit);
      if (val !== null && (Number.isNaN(val) || val < -1 || val > 1000)) { setNotice({ text: '每日限额需在 -1 ~ 1000 之间。', kind: 'err' }); return; }
      await updateUserQuota(addUser.trim(), { daily_limit: val, analysis_enabled: addEnabled, email_notify: addMail });
      setAdding(false);
      await load();
      setNotice({ text: '已为 ' + addUser.trim() + ' 配置配额。', kind: 'ok' });
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '保存失败。', kind: 'err' });
    } finally {
      setAddSaving(false);
    }
  }

  async function confirmReset() {
    if (!pendingReset) return;
    setResetting(true);
    try {
      await resetUserQuota(pendingReset.username);
      setPendingReset(null);
      await load();
      setNotice({ text: '已恢复 ' + pendingReset.username + ' 为全局默认。', kind: 'ok' });
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '重置失败。', kind: 'err' });
    } finally {
      setResetting(false);
    }
  }

  async function quickUpdate(u: QuotaUserRow, payload: { analysis_enabled?: boolean; email_notify?: boolean }) {
    try {
      await updateUserQuota(u.username, payload);
      await load();
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '更新失败。', kind: 'err' });
    }
  }

  if (loading) {
    return (
      <div className="mt-6 flex items-center justify-center py-20 text-sm text-sub"><LoaderCircleIcon className="mr-2 size-4 animate-spin" /> 加载中…</div>
    );
  }


  return (
    <div className="mt-6">
      {notice && (
        <div className={"mb-4 flex items-center justify-between rounded-md border px-4 py-2.5 text-sm " + (notice.kind === 'ok' ? 'border-good-border bg-good-soft text-good-deep' : 'border-bad-border bg-bad-soft text-bad')}>
          <span>{notice.text}</span>
          <button type="button" onClick={() => setNotice(null)} aria-label="关闭提示" className="ml-3 opacity-60 hover:opacity-100">✕</button>
        </div>
      )}

      {/* 统计条 */}
      <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard icon={<UsersIcon className="size-4" />} iconCls="bg-brand-soft text-brand" num={stats.count} label="已配置账号" />
        <StatCard icon={<GaugeIcon className="size-4" />} iconCls="bg-good-soft text-good-deep" num={stats.todayUsed} label="今日总消耗（次）" />
        <StatCard icon={<Settings2Icon className="size-4" />} iconCls="bg-warn-soft text-warn" num={stats.restricted} label="受限账号" />
        <StatCard icon={<ShieldCheckIcon className="size-4" />} iconCls="bg-bad-soft text-bad" num={stats.exhausted} label="今日已用完" />
      </div>

      {/* ① 全局设置 */}
      <div className="rounded-md border border-line bg-white">
        <div className="flex items-center gap-2.5 border-b border-line-soft px-5 py-3">
          <span className="h-4 w-1 rounded-sm bg-brand" />
          <h2 className="text-sm font-semibold text-ink">全局设置</h2>
          <span className="ml-auto text-xs text-sub">开关即时生效；「每日默认限额」需点保存。未单独配置的账号按默认限额执行</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3 px-5 py-4">
          <div className="flex items-center gap-2.5">
            <span className="text-sm text-body">启用配额限制</span>
            <ToggleSwitch checked={enabled} onChange={(v) => { setEnabled(v); void saveGlobal(v); }} label={enabled ? '开' : '关'} />
          </div>
          <div className="flex items-center gap-2.5">
            <span className="text-sm text-body">每日默认限额</span>
            <input type="number" min={-1} max={1000} value={defaultDaily} onChange={(e) => setDefaultDaily(e.target.value)} className="w-24 rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand" placeholder="不限" />
            <span className="text-xs text-sub">次（-1 或留空 = 不限，0 = 禁用）</span>
          </div>
          <div className="flex items-center gap-2.5">
            <span className="text-sm text-body">次数用尽邮件提醒</span>
            <ToggleSwitch checked={emailNotify} onChange={(v) => { setEmailNotify(v); void saveGlobal(undefined, v); }} label={emailNotify ? '开' : '关'} />
          </div>
          <button type="button" onClick={() => void saveGlobal()} disabled={saving} className="rounded-md bg-brand-deep px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50">{saving ? '保存中…' : '保存默认限额'}</button>
        </div>
      </div>

      {/* ② 账号额度 */}
      <div className="mt-5 rounded-md border border-line bg-white">
        <div className="flex items-center gap-2.5 border-b border-line-soft px-5 py-3">
          <span className="h-4 w-1 rounded-sm bg-brand" />
          <h2 className="text-sm font-semibold text-ink">账号额度</h2>
          {!enabled && <span className="rounded border border-warn-border bg-warn-soft px-2 py-0.5 text-[11px] font-medium text-warn">每日限额未生效</span>}
          <span className="ml-auto text-xs text-sub">共 {filtered.length} 个账号</span>
        </div>
        {!enabled && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line-soft bg-warn-soft/60 px-5 py-3 text-xs text-warn">
            <span className="font-medium">全局「启用配额限制」未开启，下方账号的「每日限额」不会生效（「分析功能」开关独立生效）。</span>
            <button
              type="button"
              onClick={() => { setEnabled(true); void saveGlobal(true); }}
              className="rounded border border-warn-border bg-white px-2.5 py-1 font-medium text-warn transition-colors hover:bg-warn-soft"
            >
              开启配额限制
            </button>
          </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-3">
          <div className="relative">
            <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-sub" />
            <input value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} placeholder="搜索用户名…" className="w-56 rounded-md border border-line-soft py-2 pl-9 pr-3 text-sm outline-none focus:border-brand" />
          </div>
          <button type="button" onClick={() => void openAdd()} className="inline-flex items-center gap-1.5 rounded-md border border-brand-line bg-brand-soft px-3 py-2 text-sm font-medium text-brand transition-colors hover:bg-brand-soft/70"><PlusIcon className="size-4" /> 添加账号</button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left">
            <thead>
              <tr className="border-b border-line-soft text-xs text-sub">
                <th className="px-5 py-2.5 font-medium">用户</th>
                <th className="px-4 py-2.5 font-medium">分析功能</th>
                <th className="px-4 py-2.5 font-medium">每日限额</th>
                <th className="px-4 py-2.5 font-medium">今日已用</th>
                <th className="px-4 py-2.5 font-medium">剩余</th>
                <th className="px-4 py-2.5 font-medium">邮件提醒</th>
                <th className="px-5 py-2.5 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line-soft">
              {pageItems.length === 0 ? (
                <tr><td colSpan={7} className="px-5 py-14 text-center text-sm text-sub">暂无账号配置{search ? '（无匹配）' : ''}，可点击「添加账号」设置。</td></tr>
              ) : pageItems.map((u) => {
                const pill = remainingPill(u);
                return (
                  <tr key={u.username} className="hover:bg-mist/50">
                    <td className="px-5 py-3">
                      <span className="font-medium text-ink">{u.username}</span>
                      {u.is_admin && <span className="ml-2 rounded border border-line-soft bg-mist px-1.5 py-0.5 text-[10px] text-sub">管理员</span>}
                    </td>
                    <td className="px-4 py-3"><ToggleSwitch checked={u.analysis_enabled} onChange={(v) => void quickUpdate(u, { analysis_enabled: v })} label={u.analysis_enabled ? '开' : '关'} /></td>

                    <td className="px-4 py-3 text-sm">
                      {u.daily_limit == null || u.daily_limit < 0 ? <span className="text-sub">不限</span> : <span className="font-medium text-ink">{u.daily_limit}</span>}
                      <span className={"ml-1.5 text-[11px] " + (u.custom ? 'text-brand' : 'text-sub')}>{u.custom ? '自定义' : '继承默认'}</span>
                    </td>
                    <td className="px-4 py-3 text-sm text-body">{u.is_admin ? '—' : u.used + ' / ' + (u.daily_limit == null || u.daily_limit < 0 ? '∞' : u.daily_limit)}</td>
                    <td className="px-4 py-3"><span className={"inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs " + pill.cls}>{pill.label}</span></td>
                    <td className="px-4 py-3">
                      {u.has_email ? <ToggleSwitch checked={u.email_notify} onChange={(v) => void quickUpdate(u, { email_notify: v })} label={u.email_notify ? '开' : '关'} /> : <span className="rounded border border-line-soft bg-mist px-2 py-0.5 text-xs text-sub">未绑定邮箱</span>}
                    </td>
                    <td className="px-5 py-3 text-right">
                      <button type="button" onClick={() => openEdit(u)} className="rounded border border-brand-line bg-brand-soft px-2.5 py-1 text-xs font-medium text-brand transition-colors hover:bg-brand-soft/70">编辑</button>
                      {u.custom && <button type="button" onClick={() => setPendingReset(u)} className="ml-2 rounded border border-line-soft px-2.5 py-1 text-xs text-sub transition-colors hover:bg-mist hover:text-bad">重置</button>}
                    </td>
                  </tr>
                );
              }) }
            </tbody>
          </table>
        </div>
        {/* 固定窗口分页栏（常驻） */}
        <div className="flex items-center justify-center gap-1 border-t border-line-soft px-5 py-3">
          <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)} className="rounded-md border border-line-soft bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-mist disabled:opacity-40">‹ 上一页</button>
          {Array.from({ length: pageCount }, (_, i) => i + 1).map((pnum) => (
            <button key={pnum} type="button" onClick={() => setPage(pnum)} className={"min-w-8 rounded-md border px-2 py-1.5 text-xs transition-colors " + (pnum === page ? 'border-brand bg-brand-deep text-white font-semibold' : 'border-line-soft bg-white text-ink hover:bg-mist')}>{pnum}</button>
          ))}
          <button type="button" disabled={page >= pageCount} onClick={() => setPage(page + 1)} className="rounded-md border border-line-soft bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-mist disabled:opacity-40">下一页 ›</button>
          <span className="ml-2 text-xs text-sub">共 {filtered.length} 条 · 第 {page}/{pageCount} 页</span>
        </div>
        <div className="border-t border-line-soft bg-mist/40 px-5 py-2.5 text-xs text-sub">
          <div>· 已用/剩余按今日 0 点自然重置；修改限额立即生效，无需重启服务。</div>
          <div>· 未绑定邮箱的账号，次数用尽时仅弹窗提示，不发送邮件；「分析功能」关闭 = 该账号整体禁用（管理员同样生效）。</div>
        </div>
      </div>

      {/* 编辑弹窗 */}
      {editing && (
        <AdminModal title={<>编辑配额 · {editing.username}</>} onClose={() => setEditing(null)} maxWidth="max-w-md">
          <div className="space-y-4">
            <div>
              <label className={labelCls}>每日限额</label>
              <input type="number" min={-1} max={1000} value={editingLimit} onChange={(e) => setEditingLimit(e.target.value)} className={inputCls} placeholder="留空 = 继承全局默认" />
              <p className="mt-1 text-xs leading-5 text-sub">
                留空 = 继承全局默认（当前 {settings?.default_daily == null || settings.default_daily < 0 ? '不限' : settings.default_daily + ' 次'}）；<br />
                -1 = 该账号不限次；0 = 禁用分析；1~1000 = 每日具体次数。
              </p>
            </div>
            {editing?.is_admin && (
              <p className="rounded-md border border-line-soft bg-mist px-3 py-2 text-xs leading-5 text-sub">
                该账号是管理员：不填写每日限额时默认不限次；一旦填写具体次数，管理员同样受该限额约束。
              </p>
            )}
            <div className="flex items-center justify-between rounded-md border border-line-soft px-3 py-2.5">
              <span className="text-sm text-body">允许使用分析功能</span>
              <ToggleSwitch checked={editingEnabled} onChange={setEditingEnabled} label={editingEnabled ? '开' : '关'} />
            </div>
            <div className="flex items-center justify-between rounded-md border border-line-soft px-3 py-2.5">
              <div><span className="text-sm text-body">次数用尽邮件提醒</span><p className="text-xs text-sub">{editing && !editing.has_email ? '该账号未绑定邮箱，邮件提醒自动跳过。' : '每账号每天最多一封'}</p></div>
              <ToggleSwitch checked={editingMail} onChange={setEditingMail} label={editingMail ? '开' : '关'} disabled={editing ? !editing.has_email : false} />
            </div>
            <div className="flex justify-end gap-2.5 pt-1">
              <button type="button" onClick={() => setEditing(null)} className="rounded-md border border-line px-4 py-2 text-sm text-body hover:bg-mist">取消</button>
              <button type="button" onClick={() => void saveEdit()} disabled={editingSaving} className="rounded-md bg-brand-deep px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50">{editingSaving ? '保存中…' : '保存'}</button>
            </div>
          </div>
        </AdminModal>
      )}

      {/* 添加账号弹窗 */}
      {adding && (
        <AdminModal title={<>添加配额账号</>} onClose={() => setAdding(false)} maxWidth="max-w-md">
          <div className="space-y-4">
            <div>
              <label className={labelCls}>选择用户</label>
              <select value={addUser} onChange={(e) => setAddUser(e.target.value)} className={inputCls}>
                <option value="">— 请选择用户 —</option>
                {candidates.map((c) => <option key={c.username} value={c.username}>{c.username}{c.is_admin ? '（管理员）' : ''}</option>)}
              </select>
            </div>
            <div>
              <label className={labelCls}>每日限额</label>
              <input type="number" min={-1} max={1000} value={addLimit} onChange={(e) => setAddLimit(e.target.value)} className={inputCls} placeholder="留空 = 继承全局默认" />
              <p className="mt-1 text-xs leading-5 text-sub">留空 = 继承全局默认；-1 = 不限；0 = 禁用分析；1~1000 = 每日具体次数。</p>
            </div>
            <div className="flex items-center justify-between rounded-md border border-line-soft px-3 py-2.5">
              <span className="text-sm text-body">允许使用分析功能</span>
              <ToggleSwitch checked={addEnabled} onChange={setAddEnabled} label={addEnabled ? '开' : '关'} />
            </div>
            <div className="flex items-center justify-between rounded-md border border-line-soft px-3 py-2.5">
              <span className="text-sm text-body">次数用尽邮件提醒</span>
              <ToggleSwitch checked={addMail} onChange={setAddMail} label={addMail ? '开' : '关'} />
            </div>
            <div className="flex justify-end gap-2.5 pt-1">
              <button type="button" onClick={() => setAdding(false)} className="rounded-md border border-line px-4 py-2 text-sm text-body hover:bg-mist">取消</button>
              <button type="button" onClick={() => void saveAdd()} disabled={addSaving} className="rounded-md bg-brand-deep px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50">{addSaving ? '保存中…' : '添加并保存'}</button>
            </div>
          </div>
        </AdminModal>
      )}

      <ConfirmDialog
        open={Boolean(pendingReset)}
        title="恢复该账号为全局默认？"
        message={<>将移除 {pendingReset?.username} 的自定义配额设置，之后按全局默认限额执行。此操作不可撤销。</>}
        confirmLabel="恢复默认"
        busy={resetting}
        onConfirm={() => void confirmReset()}
        onCancel={() => { if (!resetting) setPendingReset(null); }}
      />
    </div>
  );
}

