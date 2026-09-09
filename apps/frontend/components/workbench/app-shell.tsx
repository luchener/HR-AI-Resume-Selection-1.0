'use client';

import { useRef, useState, type ReactNode } from 'react';
import Image from 'next/image';
import Link from 'next/link';
import { ArchiveIcon, BarChart3Icon, KeyRoundIcon, LayoutDashboardIcon, LogOutIcon, ShieldCheckIcon, XIcon } from 'lucide-react';
import { useAuth } from './auth-context';
import { changePassword } from '@/lib/api/screening';
import { useDialogBehavior } from './dialog-behavior';

type AppShellProps = {
  active: 'home' | 'report' | 'archives' | 'admin';
  children: ReactNode;
};

const navigation = [
  { id: 'home' as const, label: '首页控制台', href: '/', icon: LayoutDashboardIcon },
  { id: 'report' as const, label: '分析报告页', href: '/dashboard', icon: BarChart3Icon },
  { id: 'archives' as const, label: '候选人才库', href: '/archives', icon: ArchiveIcon },
  { id: 'admin' as const, label: '账号管理', href: '/admin', icon: ShieldCheckIcon, adminOnly: true },
];

export default function AppShell({ active, children }: AppShellProps) {
  const { user, logout, isAdmin } = useAuth();

  // 修改密码弹窗状态
  const [showPwdDialog, setShowPwdDialog] = useState(false);
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [pwdError, setPwdError] = useState('');
  const [pwdLoading, setPwdLoading] = useState(false);

  // P3 弹窗行为统一：Esc/滚动锁定/焦点恢复/Tab 陷阱 → 共享 useDialogBehavior
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
      logout(); // 密码版本已变更，旧 token 失效，跳回登录页
    } catch (err) {
      setPwdError(err instanceof Error ? err.message : '密码修改失败。');
    } finally {
      setPwdLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-soft lg:grid lg:grid-cols-[272px_minmax(0,1fr)]">
      <aside className="relative overflow-hidden bg-navy px-5 py-5 text-white lg:sticky lg:top-0 lg:flex lg:h-screen lg:flex-col lg:px-7 lg:py-8">
        <div className="flex items-center gap-3">
          <Image
            src="/brand/resume-screening-logo.svg"
            alt=""
            width={44}
            height={44}
            className="size-11 shrink-0"
            aria-hidden="true"
          />
          <div>
            <p className="text-lg font-semibold">AI 简历智选</p>
            <p className="text-xs text-slate-400">Recruiting workspace</p>
          </div>
        </div>

        <nav className="mt-8 grid grid-cols-2 gap-2 lg:grid-cols-1" aria-label="页面导航">
          {navigation
            .filter((item) => !('adminOnly' in item && item.adminOnly) || isAdmin)
            .map((item, index) => {
            const Icon = item.icon;
            const isActive = item.id === active;
            return (
              <Link
                key={item.id}
                href={item.href}
                aria-current={isActive ? 'page' : undefined}
                className={`flex min-w-0 flex-col items-center gap-1 rounded-md border px-1 py-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nav-accent lg:flex-row lg:gap-3 lg:px-3 lg:py-3 ${
                  isActive
                    ? 'border-nav-active-border bg-nav-active-bg'
                    : 'border-transparent text-slate-400 hover:border-white/10 hover:bg-white/5 hover:text-slate-200'
                }`}
              >
                <span className={`flex size-8 shrink-0 items-center justify-center rounded-md ${isActive ? 'bg-nav-accent text-nav-accent-fg' : 'bg-white/5'}`}>
                  <Icon className="size-4" aria-hidden="true" />
                </span>
                <span className="min-w-0 text-center lg:text-left">
                  <span className="hidden text-[11px] text-slate-300 lg:block">0{index + 1}</span>
                  <span className={`block truncate text-xs font-medium lg:text-sm ${isActive ? 'text-white' : ''}`}>{item.label}</span>
                </span>
              </Link>
            );
          })}
        </nav>

        <div className="mt-auto space-y-4">
          <div className="hidden rounded-md border border-white/10 bg-white/5 p-4 lg:block">
            <div className="flex items-center gap-2.5">
              <Image src="/brand/email-avatar.svg" alt="" width={32} height={32} className="size-8 shrink-0 rounded-full" aria-hidden="true" />
              <div className="min-w-0">
                <p className="text-xs font-medium text-slate-200">AI 简历智选 1.0</p>
                <p className="mt-0.5 text-[11px] leading-5 text-slate-500">Develop By WickLu</p>
              </div>
            </div>
            <p className="mt-2 truncate text-[11px] leading-5 text-slate-500">luchenstudio@163.com</p>
          </div>
          <div className="flex items-center gap-3 rounded-md border border-white/10 bg-white/5 px-3 py-3">
            <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-nav-accent text-xs font-bold text-nav-accent-fg">
              {(user?.username || '?').slice(0, 1).toUpperCase()}
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-white">{user?.username || '未登录'}</p>
              <p className="text-[11px] text-slate-400">已登录</p>
            </div>
            <button
              type="button"
              onClick={() => setShowPwdDialog(true)}
              title="修改密码"
              aria-label="修改密码"
              className="relative -m-1 flex size-8 shrink-0 items-center justify-center rounded-md p-1 text-slate-400 transition-colors hover:bg-white/10 hover:text-white after:absolute after:-inset-1 after:content-['']"
            >
              <KeyRoundIcon className="size-4" />
            </button>
            <button
              type="button"
              onClick={logout}
              title="退出登录"
              aria-label="退出登录"
              className="relative -m-1 flex size-8 shrink-0 items-center justify-center rounded-md p-1 text-slate-400 transition-colors hover:bg-white/10 hover:text-white after:absolute after:-inset-1 after:content-['']"
            >
              <LogOutIcon className="size-4" />
            </button>
          </div>
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
          onClick={(e) => {
            if (e.target === e.currentTarget) closePwdDialog();
          }}
        >
          <div className="w-full max-w-sm rounded-xl bg-white p-6 shadow-2xl">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-slate-900">修改密码</h2>
              <button
                type="button"
                onClick={closePwdDialog}
                aria-label="关闭"
                className="flex size-8 items-center justify-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              >
                <XIcon className="size-4" />
              </button>
            </div>

            <div className="mt-5 space-y-4">
              <div>
                <label htmlFor="old-password" className="mb-1 block text-xs font-medium text-slate-600">
                  旧密码
                </label>
                <input
                  ref={oldPasswordRef}
                  id="old-password"
                  type="password"
                  value={oldPassword}
                  onChange={(e) => setOldPassword(e.target.value)}
                  className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-nav-accent focus:ring-2 focus:ring-nav-ring"
                  placeholder="请输入当前密码"
                />
              </div>
              <div>
                <label htmlFor="new-password" className="mb-1 block text-xs font-medium text-slate-600">
                  新密码（至少 8 位，含字母和数字）
                </label>
                <input
                  id="new-password"
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-nav-accent focus:ring-2 focus:ring-nav-ring"
                  placeholder="请输入新密码"
                />
              </div>
              <div>
                <label htmlFor="confirm-password" className="mb-1 block text-xs font-medium text-slate-600">
                  确认新密码
                </label>
                <input
                  id="confirm-password"
                  type="password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-nav-accent focus:ring-2 focus:ring-nav-ring"
                  placeholder="再次输入新密码"
                />
              </div>

              {pwdError && (
                <p className="rounded-md bg-red-50 px-3 py-2 text-xs text-red-600">{pwdError}</p>
              )}

              <button
                type="button"
                disabled={pwdLoading}
                onClick={handleChangePassword}
                className="w-full rounded-md bg-navy py-2.5 text-sm font-medium text-white transition-colors hover:bg-navy-hover disabled:cursor-not-allowed disabled:opacity-60"
              >
                {pwdLoading ? '提交中…' : '确认修改'}
              </button>
              <p className="text-center text-[11px] text-slate-400">修改成功后需重新登录</p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
