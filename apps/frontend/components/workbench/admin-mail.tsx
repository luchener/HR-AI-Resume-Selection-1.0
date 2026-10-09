'use client';

import { useCallback, useEffect, useState } from 'react';
import { AlertCircleIcon, CheckCircle2Icon, LoaderCircleIcon, SendIcon } from 'lucide-react';
import ConfirmDialog from './confirm-dialog';
import {
  clearAdminMailConfig,
  fetchAdminMailConfig,
  saveAdminMailConfig,
  testAdminMailConfig,
  type AdminMailConfig,
  type AdminMailSlot,
  type AdminMailSlotKey,
  type AdminMailSecurity,
} from '@/lib/api/system-mail';

const SLOTS: AdminMailSlotKey[] = ['transactional', 'notification'];

const SLOT_META: Record<AdminMailSlotKey, { title: string; usage: string }> = {
  transactional: { title: '注册与验证码', usage: '注册邮箱验证码 / 找回密码 / 邀请码' },
  notification: { title: '通知与群发', usage: '公告通知 / 群发邮件' },
};

const SECURITIES: { value: AdminMailSecurity; label: string }[] = [
  { value: 'ssl', label: 'SSL（465）' },
  { value: 'starttls', label: 'STARTTLS（587）' },
  { value: 'plain', label: '明文（25，仅内网）' },
];

type MailForm = {
  host: string;
  port: string;
  security: AdminMailSecurity;
  username: string;
  password: string;
  from_address: string;
  from_name: string;
  enabled: boolean;
};

type Notice = { kind: 'ok' | 'err'; text: string } | null;
type TestState = { kind: 'ok' | 'err'; text: string } | undefined;

function emptyForm(): MailForm {
  return { host: '', port: '465', security: 'ssl', username: '', password: '', from_address: '', from_name: 'AI 简历智选', enabled: true };
}

function toForm(slot: AdminMailSlot): MailForm {
  return {
    host: slot.host,
    port: String(slot.port || 465),
    security: slot.security,
    username: slot.username,
    password: '',
    from_address: slot.from_address,
    from_name: slot.from_name,
    enabled: slot.enabled,
  };
}

/** 当前生效来源徽标：让管理员一眼看出改的是哪一层配置。 */
function sourceBadge(slot: AdminMailSlot): { label: string; cls: string } {
  if (slot.source === 'ui') return { label: '界面配置', cls: 'border-[#cddbf6] bg-brand-soft text-brand' };
  if (slot.source === 'ui.transactional') return { label: '跟随注册配置', cls: 'border-line bg-soft text-sub' };
  if (slot.source === 'env') return { label: '.env', cls: 'border-warn-border bg-warn-soft text-warn' };
  return { label: '未配置', cls: 'border-bad-border bg-bad-soft text-bad' };
}

function ToggleSwitch({ checked, onChange, disabled }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={'inline-flex items-center gap-2 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ' + (disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer')}
    >
      <span
        aria-hidden="true"
        className={'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors ' + (checked ? 'border-brand-deep bg-brand-deep' : 'border-line bg-soft')}
      >
        <span className={'absolute top-0.5 size-4 rounded-full bg-white shadow-sm ring-1 ring-black/5 transition-all duration-150 ' + (checked ? 'left-[18px]' : 'left-0.5')} />
      </span>
    </button>
  );
}

const inputCls = 'h-9 w-full rounded-md border border-line bg-white px-2.5 text-sm text-ink outline-none placeholder:text-disabled-fg focus:border-brand focus:ring-2 focus:ring-brand/15 disabled:bg-mist disabled:text-sub';

export default function AdminMail() {
  const [config, setConfig] = useState<AdminMailConfig | null>(null);
  const [forms, setForms] = useState<Record<AdminMailSlotKey, MailForm>>({
    transactional: emptyForm(),
    notification: emptyForm(),
  });
  const [ownNotification, setOwnNotification] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<Notice>(null);
  const [tests, setTests] = useState<Partial<Record<AdminMailSlotKey, TestState>>>({});
  const [confirmClear, setConfirmClear] = useState(false);

  const apply = useCallback((next: AdminMailConfig) => {
    setConfig(next);
    setForms({
      transactional: toForm(next.slots.transactional),
      notification: toForm(next.slots.notification),
    });
    // 只有"通知槽位确实有自己的一套配置"才算单独配置。
    // 后端 enable 默认就是 true，用它判断会让卡片一打开就显示"单独配置"，
    // 而徽标却按 source 显示"跟随注册配置"，自相矛盾。
    setOwnNotification(next.slots.notification.source === 'ui');
  }, []);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const next = await fetchAdminMailConfig();
        if (alive) apply(next);
      } catch (error) {
        if (alive) setNotice({ kind: 'err', text: (error as Error).message || '读取邮件服务配置失败。' });
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [apply]);

  const patch = (slot: AdminMailSlotKey, key: keyof MailForm, value: string | boolean) => {
    setForms((prev) => ({ ...prev, [slot]: { ...prev[slot], [key]: value } }));
  };

  const save = async (slot: AdminMailSlotKey) => {
    const form = forms[slot];
    const port = Number(form.port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setNotice({ kind: 'err', text: '端口必须是 1-65535 的整数。' });
      return;
    }
    setBusy('save:' + slot);
    setNotice(null);
    try {
      const next = await saveAdminMailConfig({
        slots: {
          [slot]: {
            host: form.host.trim(),
            port,
            security: form.security,
            username: form.username.trim(),
            from_address: form.from_address.trim(),
            from_name: form.from_name.trim(),
            enabled: slot === 'notification' ? ownNotification : form.enabled,
            // 留空 = 不修改：不要用空串覆盖已保存的授权码
            ...(form.password ? { password: form.password } : {}),
          },
        },
      });
      apply(next);
      setNotice({ kind: 'ok', text: '「' + SLOT_META[slot].title + '」已保存，全站新邮件立即生效（无需重启服务）。' });
    } catch (error) {
      setNotice({ kind: 'err', text: (error as Error).message || '保存失败。' });
    } finally {
      setBusy('');
    }
  };

  const runTest = async (slot: AdminMailSlotKey, send: boolean) => {
    setBusy((send ? 'testmail:' : 'test:') + slot);
    setNotice(null);
    setTests((prev) => ({ ...prev, [slot]: undefined }));
    try {
      const result = await testAdminMailConfig(slot, send);
      const text = result.message + '（' + result.seconds + 's · ' + result.host + ':' + result.port + '）';
      setTests((prev) => ({ ...prev, [slot]: { kind: 'ok', text } }));
    } catch (error) {
      setTests((prev) => ({ ...prev, [slot]: { kind: 'err', text: (error as Error).message || '测试失败。' } }));
    } finally {
      setBusy('');
    }
  };

  const doClear = async () => {
    setBusy('clear');
    setNotice(null);
    try {
      const next = await clearAdminMailConfig();
      apply(next);
      setConfirmClear(false);
      setNotice({ kind: 'ok', text: '已清空界面配置，发件邮箱改用服务器 .env 里的 SMTP_* 设置。' });
    } catch (error) {
      setNotice({ kind: 'err', text: (error as Error).message || '清空失败。' });
      setConfirmClear(false);
    } finally {
      setBusy('');
    }
  };

  if (loading) {
    return (
      <div className="mt-6 flex items-center gap-3 rounded-md border border-line bg-white px-5 py-8 text-sm text-sub">
        <LoaderCircleIcon className="size-4 animate-spin" /> 正在读取邮件服务配置…
      </div>
    );
  }

  if (!config) {
    return (
      <div className="mt-6 flex items-center gap-3 rounded-md border border-bad-border bg-bad-soft px-5 py-4 text-sm text-bad">
        <AlertCircleIcon className="size-4" /> {notice?.text || '邮件服务配置读取失败，请刷新重试。'}
      </div>
    );
  }

  return (
    <div className="mt-6">
      <div className="rounded-md border border-line bg-white px-5 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <SendIcon className="size-4 text-brand" />
          <h2 className="text-sm font-semibold text-ink">邮件服务</h2>
          {config.updated_at && (
            <span className="text-xs text-sub">最近更新：{config.updated_at}{config.updated_by ? ' · ' + config.updated_by : ''}</span>
          )}
        </div>
        <p className="mt-2 text-sm leading-6 text-sub">
          配置用于注册验证码、找回密码与通知群发的发件邮箱。保存后立即生效，无需重启服务。
          {!config.env_available && <span className="text-warn">当前 .env 未配置发件邮箱，界面上未填写的槽位将无法发信。</span>}
        </p>
      </div>

      {notice && (
        <div
          role={notice.kind === 'ok' ? 'status' : 'alert'}
          className={'mt-4 flex items-center gap-3 rounded-md border px-4 py-3 text-sm ' + (notice.kind === 'ok' ? 'border-good-border bg-good-soft text-good-deep' : 'border-bad-border bg-bad-soft text-bad')}
        >
          {notice.kind === 'ok' ? <CheckCircle2Icon className="size-4" /> : <AlertCircleIcon className="size-4" />}
          {notice.text}
        </div>
      )}

      {SLOTS.map((slot) => {
        const view = config.slots[slot];
        const form = forms[slot];
        const badge = sourceBadge(view);
        const following = slot === 'notification' && !ownNotification;
        const disabled = following;
        const test = tests[slot];
        return (
          <section key={slot} className="mt-5 rounded-md border border-line bg-white">
            <header className="flex flex-wrap items-center gap-2 border-b border-line-soft px-5 py-3.5">
              <h3 className="text-sm font-semibold text-ink">{SLOT_META[slot].title}</h3>
              <span className={'rounded-full border px-2.5 py-0.5 text-[11px] ' + badge.cls}>{badge.label}</span>
              {view.ready && (
                <span className="rounded-full border border-good-border bg-good-soft px-2.5 py-0.5 text-[11px] text-good-deep">可发信</span>
              )}
              <span className="ml-auto text-[11px] text-sub">用于：{SLOT_META[slot].usage}</span>
            </header>

            <div className="px-5 py-4">
              {slot === 'notification' && (
                <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border border-line-soft bg-mist px-3 py-2.5">
                  <ToggleSwitch checked={ownNotification} onChange={setOwnNotification} />
                  <span className="text-sm text-ink">单独配置通知发件邮箱</span>
                  <span className="text-xs text-sub">关闭时跟随「注册与验证码」，无需重复填写</span>
                </div>
              )}

              <div className={'grid gap-4 sm:grid-cols-2 ' + (disabled ? 'opacity-60' : '')}>
                <div>
                  <label htmlFor={'host-' + slot} className="mb-1 block text-xs text-sub">SMTP 服务器</label>
                  <input id={'host-' + slot} type="text" className={inputCls} value={form.host} disabled={disabled}
                    placeholder="smtp.qq.com" onChange={(e) => patch(slot, 'host', e.target.value)} />
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label htmlFor={'port-' + slot} className="mb-1 block text-xs text-sub">端口</label>
                    <input id={'port-' + slot} type="number" min={1} max={65535} className={inputCls} value={form.port} disabled={disabled}
                      onChange={(e) => patch(slot, 'port', e.target.value)} />
                  </div>
                  <div>
                    <label htmlFor={'security-' + slot} className="mb-1 block text-xs text-sub">加密方式</label>
                    <select id={'security-' + slot} className={inputCls} value={form.security} disabled={disabled}
                      onChange={(e) => patch(slot, 'security', e.target.value)}>
                      {SECURITIES.map((item) => (<option key={item.value} value={item.value}>{item.label}</option>))}
                    </select>
                  </div>
                </div>
                <div>
                  <label htmlFor={'username-' + slot} className="mb-1 block text-xs text-sub">发件邮箱（用户名）</label>
                  <input id={'username-' + slot} type="text" className={inputCls} value={form.username} disabled={disabled}
                    placeholder="noreply@example.com" onChange={(e) => patch(slot, 'username', e.target.value)} />
                </div>
                <div>
                  <label htmlFor={'password-' + slot} className="mb-1 block text-xs text-sub">密码 / 授权码</label>
                  <input id={'password-' + slot} type="password" className={inputCls} value={form.password} disabled={disabled}
                    placeholder={view.password_set ? '已设置 · 留空表示不修改' : '邮箱授权码（不是登录密码）'}
                    autoComplete="new-password"
                    onChange={(e) => patch(slot, 'password', e.target.value)} />
                </div>
                <div>
                  <label htmlFor={'from-' + slot} className="mb-1 block text-xs text-sub">发件人地址</label>
                  <input id={'from-' + slot} type="text" className={inputCls} value={form.from_address} disabled={disabled}
                    placeholder="留空默认与用户名相同" onChange={(e) => patch(slot, 'from_address', e.target.value)} />
                </div>
                <div>
                  <label htmlFor={'fromname-' + slot} className="mb-1 block text-xs text-sub">发件人显示名</label>
                  <input id={'fromname-' + slot} type="text" className={inputCls} value={form.from_name} disabled={disabled}
                    placeholder="AI 简历智选" onChange={(e) => patch(slot, 'from_name', e.target.value)} />
                </div>
                {slot === 'transactional' && (
                  <div className="flex items-center gap-2">
                    <ToggleSwitch checked={form.enabled} onChange={(v) => patch(slot, 'enabled', v)} />
                    <span className="text-sm text-ink">启用这套发件配置</span>
                  </div>
                )}
              </div>

              <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-dashed border-line pt-4">
                <button type="button" disabled={disabled || busy !== ''} onClick={() => save(slot)}
                  className="inline-flex h-9 items-center gap-2 rounded-md bg-brand-deep px-4 text-sm font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-50">
                  {busy === 'save:' + slot && <LoaderCircleIcon className="size-4 animate-spin" />} 保存
                </button>
                <button type="button" disabled={disabled || busy !== ''} onClick={() => runTest(slot, false)}
                  className="inline-flex h-9 items-center gap-2 rounded-md border border-line bg-white px-4 text-sm font-medium text-ink transition-colors hover:bg-mist disabled:opacity-50">
                  {busy === 'test:' + slot && <LoaderCircleIcon className="size-4 animate-spin" />} 测试连接
                </button>
                <button type="button" disabled={disabled || busy !== ''} onClick={() => runTest(slot, true)}
                  className="inline-flex h-9 items-center gap-2 rounded-md border border-line bg-white px-4 text-sm font-medium text-ink transition-colors hover:bg-mist disabled:opacity-50">
                  {busy === 'testmail:' + slot && <LoaderCircleIcon className="size-4 animate-spin" />} 发测试邮件给我
                </button>
                {slot === 'transactional' && view.source === 'ui' && (
                  <button type="button" disabled={busy !== ''} onClick={() => setConfirmClear(true)}
                    className="inline-flex h-9 items-center gap-2 rounded-md border border-line bg-white px-4 text-sm font-medium text-sub transition-colors hover:bg-mist disabled:opacity-50">
                    清空界面配置
                  </button>
                )}
                {test && (
                  <span className={'ml-auto inline-flex items-center gap-1.5 text-xs ' + (test.kind === 'ok' ? 'text-good-deep' : 'text-bad')}>
                    {test.kind === 'ok' ? <CheckCircle2Icon className="size-3.5" /> : <AlertCircleIcon className="size-3.5" />}
                    {test.text}
                  </span>
                )}
                {!test && following && <span className="ml-auto text-xs text-sub">开启「单独配置」后可独立保存与测试</span>}
              </div>
            </div>
          </section>
        );
      })}

      <ConfirmDialog
        open={confirmClear}
        danger
        title="清空邮件服务界面配置？"
        message="清空后发件邮箱立即回退到服务器 .env 的 SMTP_* 配置。此操作不影响用户数据，但若 .env 也未配置，注册验证码与找回密码邮件将无法发送。"
        confirmLabel="清空并回退 .env"
        busy={busy === 'clear'}
        onConfirm={doClear}
        onCancel={() => setConfirmClear(false)}
      />
    </div>
  );
}
