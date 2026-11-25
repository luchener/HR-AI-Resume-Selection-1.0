'use client';

import { useRef, useState, type ReactNode } from 'react';
import Link from 'next/link';
import {
  ArchiveIcon,
  BarChart3Icon,
  KeyRoundIcon,
  LayoutDashboardIcon,
  LogOutIcon,
  ShieldCheckIcon,
  XIcon,
} from 'lucide-react';
import BrandMark from './brand-mark';
import { useAuth } from './auth-context';
import { changePassword } from '@/lib/api/screening';
import { useDialogBehavior } from './dialog-behavior';

type AppShellProps = {
  active: 'home' | 'report' | 'archives' | 'admin';
  children: ReactNode;
};

const workspaceNav = [
  { id: 'home' as const, label: '筛选工作台', href: '/', icon: LayoutDashboardIcon, code: '01' },
  { id: 'report' as const, label: '分析报告', href: '/dashboard', icon: BarChart3Icon, code: '02' },
  { id: 'archives' as const, label: '候选人才库', href: '/archives', icon: ArchiveIcon, code: '03' },
];

const adminNav = { id: 'admin' as const, label: '账号管理', href: '/admin', icon: ShieldCheckIcon, code: '04' };

export default function AppShell({ active, children }: AppShellProps) {
  const { user, logout, isAdmin } = useAuth();

  const [showPwdDialog, setShowPwdDialog] = useState(false);
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [pwdError, setPwdError] = useState('');
  const [pwdLoading, setPwdLoading] = useState(false);

  const pwdDialogRef = useRef<HTMLDivElement>(null);
  const oldPasswordRef = useRef<HTMLInputElement>(null);

  const closePwdDialog = () => {
    setShowPwdDialog(false);
    setOldPassword('');
    setNewPassword('');
    setConfirmPassword('');
    setPwdError('');
  };

  useDialogBehavior({
    open: showPwdDialog,
    onClose: closePwdDialog,
    panelRef: pwdDialogRef,
    initialFocusRef: oldPasswordRef,
  });

  const handleChangePassword = async () => {
    setPwdError('');
    if (!oldPassword || !newPassword) {
      setPwdError('请填写旧密码和新密码。');
      return;
    }
    if (newPassword.length < 8) {
      setPwdError('新密码长度至少 8 个字符。');
      return;
    }
    if (newPassword !== confirmPassword) {
      setPwdError('两次输入的新密码不一致。');
      return;
    }
    if (oldPassword === newPassword) {
      setPwdError('新密码不能与旧密码相同。');
      return;
    }
    setPwdLoading(true);
    try {
      await changePassword(oldPassword, newPassword);
      closePwdDialog();
      logout();
    } catch (err) {
      setPwdError(err instanceof Error ? err.message : '密码修改失败。');
    } finally {
      setPwdLoading(false);
    }
  };

  const renderNavItem = (item: (typeof workspaceNav)[number] | typeof adminNav, mobile = false) => {
    const Icon = item.icon;
    const isActive = item.id === active;
    return (
      <Link
        key={item.id}
        href={item.href}
        aria-current={isActive ? 'page' : undefined}
        className={`flex min-w-0 items-center gap-3 border px-3 py-2.5 text-[13px] transition-colors focus-visible:outline-none ${
          isActive
            ? 'border-ink bg-ink font-semibold text-white'
            : 'border-transparent text-sub hover:bg-soft hover:text-ink'
        } ${mobile ? 'justify-start' : ''}`}
      >
        <Icon className="size-4 shrink-0" strokeWidth={1.6} aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate">{item.label}</span>
        <span className={`hidden font-mono text-[10px] tracking-[0.08em] lg:block ${isActive ? 'text-white/60' : 'text-sub/70'}`}>
          {item.code}
        </span>
      </Link>
    );
  };

  return (
    <div className="min-h-screen bg-soft lg:grid lg:grid-cols-[264px_minmax(0,1fr)]">
      <aside className="border-b border-line bg-white px-4 py-4 lg:sticky lg:top-0 lg:flex lg:h-screen lg:flex-col lg:border-b-0 lg:border-r lg:px-4 lg:py-6">
        <div className="flex items-center justify-between gap-3">
          <Link href="/" className="flex min-w-0 items-center gap-3">
            <BrandMark className="size-8" />
            <span className="min-w-0">
              <span className="block truncate text-[14.5px] font-semibold leading-tight tracking-[-0.01em] text-ink">
                AI 简历智选
              </span>
              <span className="mt-1 block font-mono text-[10.5px] uppercase leading-none tracking-[0.22em] text-sub">
                Screening workspace
              </span>
            </span>
          </Link>
          <button
            type="button"
            onClick={logout}
            aria-label="退出登录"
            title="退出登录"
            className="flex size-8 items-center justify-center text-sub transition-colors hover:bg-soft hover:text-ink lg:hidden"
          >
            <LogOutIcon className="size-4" strokeWidth={1.5} />
          </button>
        </div>

        <nav className="mt-5 grid grid-cols-2 gap-1 lg:mt-7 lg:grid-cols-1" aria-label="页面导航">
          <p className="col-span-2 hidden px-3 pb-2 font-mono lg:col-span-1 text-[10px] uppercase tracking-[0.16em] text-sub lg:block">
            Workspace
          </p>
          {workspaceNav.map((item) => renderNavItem(item, true))}

          {isAdmin && (
            <>
              <p className="col-span-2 hidden px-3 pb-2 pt-5 font-mono lg:col-span-1 text-[10px] uppercase tracking-[0.16em] text-sub lg:block">
                Admin
              </p>
              {renderNavItem(adminNav, true)}
            </>
          )}
        </nav>

        <div className="mt-4 border-t border-line-soft pt-4 lg:mt-auto">
          <div className="hidden gap-4 px-3 pb-3 text-[10.5px] text-sub lg:flex">
            <Link href="/privacy" className="transition-colors hover:text-ink">
              隐私政策
            </Link>
            <Link href="/terms" className="transition-colors hover:text-ink">
              用户协议
            </Link>
          </div>

          <div className="flex items-center gap-2.5 px-2 py-1">
            <span className="flex size-8 shrink-0 items-center justify-center border border-line-strong text-[11px] font-semibold text-ink">
              {(user?.username || '?').slice(0, 1).toUpperCase()}
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-[12.5px] font-medium leading-tight text-ink">{user?.username || '未登录'}</p>
              <p className="mt-1 font-mono text-[8.5px] uppercase leading-none tracking-[0.12em] text-sub">
                {isAdmin ? 'Admin' : 'Recruiter'} · 已登录
              </p>
            </div>
            <button
              type="button"
              onClick={() => setShowPwdDialog(true)}
              title="修改密码"
              aria-label="修改密码"
              className="relative flex size-8 shrink-0 items-center justify-center text-sub transition-colors hover:bg-soft hover:text-ink after:absolute after:-inset-1 after:content-['']"
            >
              <KeyRoundIcon className="size-4" strokeWidth={1.5} />
            </button>
            <button
              type="button"
              onClick={logout}
              title="退出登录"
              aria-label="退出登录"
              className="relative flex size-8 shrink-0 items-center justify-center text-sub transition-colors hover:bg-soft hover:text-ink after:absolute after:-inset-1 after:content-['']"
            >
              <LogOutIcon className="size-4" strokeWidth={1.5} />
            </button>
          </div>

          <p className="hidden px-2 pt-3 font-mono text-[8.5px] uppercase tracking-[0.12em] text-sub lg:block">
            Version 1.0 · WickLu
          </p>
        </div>
      </aside>

      <main className="min-w-0">{children}</main>

      {showPwdDialog && (
        <div
          ref={pwdDialogRef}
          role="dialog"
          aria-modal="true"
          aria-label="修改密码"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          onClick={(event) => {
            if (event.target === event.currentTarget) closePwdDialog();
          }}
        >
          <div className="w-full max-w-sm border border-line bg-white p-6">
            <div className="flex items-center justify-between">
              <h2 className="text-[18px] font-medium tracking-[-0.01em] text-ink">修改密码</h2>
              <button
                type="button"
                onClick={closePwdDialog}
                aria-label="关闭"
                className="flex size-8 items-center justify-center text-sub transition-colors hover:bg-soft hover:text-ink"
              >
                <XIcon className="size-4" strokeWidth={1.5} />
              </button>
            </div>

            <div className="mt-5 space-y-4">
              <div>
                <label htmlFor="old-password" className="mb-1.5 block text-[11.5px] font-medium text-body">
                  旧密码
                </label>
                <input
                  ref={oldPasswordRef}
                  id="old-password"
                  type="password"
                  value={oldPassword}
                  onChange={(event) => setOldPassword(event.target.value)}
                  className="h-10 w-full border border-line-strong bg-white px-3 text-[13px] text-ink outline-none focus:border-ink"
                  placeholder="请输入当前密码"
                />
              </div>
              <div>
                <label htmlFor="new-password" className="mb-1.5 block text-[11.5px] font-medium text-body">
                  新密码（至少 8 位，含字母和数字）
                </label>
                <input
                  id="new-password"
                  type="password"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                  className="h-10 w-full border border-line-strong bg-white px-3 text-[13px] text-ink outline-none focus:border-ink"
                  placeholder="请输入新密码"
                />
              </div>
              <div>
                <label htmlFor="confirm-password" className="mb-1.5 block text-[11.5px] font-medium text-body">
                  确认新密码
                </label>
                <input
                  id="confirm-password"
                  type="password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  className="h-10 w-full border border-line-strong bg-white px-3 text-[13px] text-ink outline-none focus:border-ink"
                  placeholder="再次输入新密码"
                />
              </div>

              {pwdError && <p className="border border-bad-border bg-bad-soft px-3 py-2 text-[12px] text-bad">{pwdError}</p>}

              <button
                type="button"
                disabled={pwdLoading}
                onClick={handleChangePassword}
                className="h-11 w-full bg-ink text-[13px] font-medium text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-50"
              >
                {pwdLoading ? '提交中…' : '确认修改'}
              </button>
              <p className="text-center text-[10.5px] text-sub">修改成功后需重新登录</p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
