'use client';

import { FormEvent, useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import {
  ArrowRightIcon,
  CheckIcon,
  CheckCircleIcon,
  KeyRoundIcon,
  LoaderCircleIcon,
  LockKeyholeIcon,
  MailIcon,
  ShieldCheckIcon,
  UserRoundIcon,
} from 'lucide-react';
import { useAuth } from '@/components/workbench/auth-context';
import { API_URL } from '@/lib/api/config';

type Mode = 'login' | 'register';

export default function LoginPage() {
  const router = useRouter();
  const { login, isAuthenticated, isHydrated } = useAuth();
  const [mode, setMode] = useState<Mode>('login');
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [codeSent, setCodeSent] = useState(false);
  const [codeBusy, setCodeBusy] = useState(false);
  const [codeCountdown, setCodeCountdown] = useState(0);
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  // 已登录则直接回首页
  useEffect(() => {
    if (isHydrated && isAuthenticated) router.replace('/');
  }, [isHydrated, isAuthenticated, router]);

  const switchMode = (next: Mode) => {
    setMode(next);
    setError('');
    setConfirm('');
    setEmail('');
    setCode('');
    setCodeSent(false);
  };

  // 发送验证码倒计时
  useEffect(() => {
    if (codeCountdown <= 0) return;
    const timer = setTimeout(() => setCodeCountdown((c) => c - 1), 1000);
    return () => clearTimeout(timer);
  }, [codeCountdown]);

  const handleSendCode = async () => {
    setError('');
    if (!email.trim()) {
      setError('请先填写邮箱。');
      return;
    }
    setCodeBusy(true);
    try {
      const response = await fetch(`${API_URL}/api/v1/auth/email-code/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim() }),
      });
      const text = await response.text();
      let payload: { detail?: string } = {};
      try {
        payload = JSON.parse(text) as typeof payload;
      } catch {
        // ignore
      }
      if (!response.ok) {
        throw new Error(payload.detail || (text || '发送失败'));
      }
      setCodeSent(true);
      setCodeCountdown(60);
      setError('');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '验证码发送失败，请稍后重试。');
    } finally {
      setCodeBusy(false);
    }
  };

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    if (!username.trim() || !password) {
      setError('请输入用户名和密码。');
      return;
    }
    if (mode === 'register' && password !== confirm) {
      setError('两次输入的密码不一致。');
      return;
    }
    if (mode === 'register' && password.length < 8) {
      setError('密码长度至少 8 个字符。');
      return;
    }
    if (mode === 'register' && !email.trim()) {
      setError('请填写邮箱。');
      return;
    }
    if (mode === 'register' && !code.trim()) {
      setError('请输入邮箱验证码。');
      return;
    }
    setBusy(true);
    try {
      const body: Record<string, string> = { username: username.trim(), password };
      if (mode === 'register') {
        body.email = email.trim();
        body.code = code.trim();
      }
      const response = await fetch(`${API_URL}/api/v1/auth/${mode}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const text = await response.text();
      let payload: { detail?: string; data?: { user_id?: string; username?: string; token?: string } } = {};
      try {
        payload = JSON.parse(text) as typeof payload;
      } catch {
        // ignore
      }
      if (!response.ok) {
        throw new Error(payload.detail || (text || `认证失败（HTTP ${response.status}）`));
      }
      if (!payload.data?.token || !payload.data?.user_id) {
        throw new Error('服务未返回有效的登录凭证。');
      }
      login(payload.data.token, { user_id: payload.data.user_id, username: payload.data.username || username.trim() });
      router.replace('/');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '认证失败，请稍后重试。');
    } finally {
      setBusy(false);
    }
  };

  if (!isHydrated) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#f3f6fa] text-sm text-[#6f7d91]">
        <LoaderCircleIcon className="mr-2 size-4 animate-spin" /> 正在载入
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-[#f3f6fa] px-5 py-10">
      <div className="w-full max-w-md">
        <div className="rounded-lg border border-[#dce2eb] bg-white p-8 shadow-[0_24px_80px_rgba(19,31,51,0.1)] sm:p-10">
          <div className="flex size-12 items-center justify-center rounded-lg bg-[#17243b] text-white shadow-[6px_6px_0_#88a8ff]">
            <ShieldCheckIcon className="size-6" />
          </div>
          <h1 className="mt-6 text-2xl font-semibold text-[#152137]">AI 简历智选</h1>
          <p className="mt-2 text-sm leading-6 text-[#6d7b91]">
            {mode === 'login' ? '登录后继续使用简历筛选工作台' : '注册一个新账号开始使用'}
          </p>

          <div className="mt-6 grid grid-cols-2 gap-1 rounded-md bg-[#eef1f6] p-1">
            {(['login', 'register'] as const).map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => switchMode(item)}
                className={`h-9 rounded-md text-sm font-medium transition-colors ${
                  mode === item ? 'bg-white text-[#1b273d] shadow-sm' : 'text-[#6f7d91] hover:text-[#334158]'
                }`}
              >
                {item === 'login' ? '登录' : '注册'}
              </button>
            ))}
          </div>

          <form onSubmit={handleSubmit} className="mt-6 space-y-4">
            <div>
              <label htmlFor="login-username" className="text-sm font-semibold text-[#3d4a60]">用户名</label>
              <div className="relative mt-2">
                <UserRoundIcon className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-[#8a96a7]" />
                <input
                  id="login-username"
                  type="text"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  placeholder="字母、数字或中文"
                  autoComplete="username"
                  spellCheck={false}
                  className="h-12 w-full rounded-md border border-[#cfd8e5] bg-[#fbfcfe] pl-10 pr-4 text-sm text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff]"
                />
              </div>
            </div>

            <div>
              <label htmlFor="login-password" className="text-sm font-semibold text-[#3d4a60]">密码</label>
              <div className="relative mt-2">
                <LockKeyholeIcon className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-[#8a96a7]" />
                <input
                  id="login-password"
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder={mode === 'register' ? '至少 8 个字符' : '输入密码'}
                  autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                  className="h-12 w-full rounded-md border border-[#cfd8e5] bg-[#fbfcfe] pl-10 pr-4 text-sm text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff]"
                />
              </div>
            </div>

            {mode === 'register' && (
              <div>
                <label htmlFor="login-confirm" className="text-sm font-semibold text-[#3d4a60]">确认密码</label>
                <div className="relative mt-2">
                  <LockKeyholeIcon className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-[#8a96a7]" />
                  <input
                    id="login-confirm"
                    type="password"
                    value={confirm}
                    onChange={(event) => setConfirm(event.target.value)}
                    placeholder="再次输入密码"
                    autoComplete="new-password"
                    className="h-12 w-full rounded-md border border-[#cfd8e5] bg-[#fbfcfe] pl-10 pr-4 text-sm text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff]"
                  />
                </div>
              </div>
            )}

            {mode === 'register' && (
              <div>
                <label htmlFor="login-email" className="text-sm font-semibold text-[#3d4a60]">邮箱</label>
                <div className="relative mt-2">
                  <MailIcon className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-[#8a96a7]" />
                  <input
                    id="login-email"
                    type="email"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    placeholder="用于找回密码"
                    autoComplete="email"
                    spellCheck={false}
                    className="h-12 w-full rounded-md border border-[#cfd8e5] bg-[#fbfcfe] pl-10 pr-4 text-sm text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff]"
                  />
                </div>
              </div>
            )}

            {mode === 'register' && (
              <div>
                <label htmlFor="login-code" className="text-sm font-semibold text-[#3d4a60]">邮箱验证码</label>
                <div className="mt-2 flex gap-2">
                  <div className={`relative flex-1 transition-all ${codeSent ? 'text-[#2f6b4a]' : ''}`}>
                    <KeyRoundIcon
                      className={`absolute left-3.5 top-1/2 size-4 -translate-y-1/2 transition-colors ${
                        codeSent ? 'text-[#5aa87c]' : 'text-[#8a96a7]'
                      }`}
                    />
                    <input
                      id="login-code"
                      type="text"
                      inputMode="text"
                      maxLength={6}
                      value={code}
                      onChange={(event) => setCode(event.target.value.toUpperCase())}
                      placeholder="6 位验证码"
                      autoComplete="one-time-code"
                      spellCheck={false}
                      className="h-12 w-full rounded-xl border border-[#cfd8e5] bg-[#fbfcfe] pl-10 pr-4 text-sm tracking-widest text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff] focus:bg-white"
                    />
                  </div>
                  <button
                    type="button"
                    onClick={handleSendCode}
                    disabled={codeBusy || codeCountdown > 0}
                    className={`h-12 shrink-0 rounded-xl px-4 text-sm font-semibold transition-all duration-200 ${
                      codeCountdown > 0
                        ? 'cursor-not-allowed bg-[#eef1f6] text-[#8a96a7]'
                        : codeSent
                          ? 'border border-[#b7d6c5] bg-[#f0faf4] text-[#2f6b4a] hover:bg-[#e3f5ea]'
                          : 'bg-[#1b2a45] text-white shadow-sm hover:bg-[#263a5e] hover:shadow'
                    }`}
                  >
                    {codeBusy ? (
                      <span className="flex items-center gap-1.5">
                        <LoaderCircleIcon className="size-4 animate-spin" /> 发送中
                      </span>
                    ) : codeCountdown > 0 ? (
                      <span className="flex items-center gap-1">{codeCountdown}s 后重发</span>
                    ) : codeSent ? (
                      <span className="flex items-center gap-1.5">
                        <CheckIcon className="size-4" /> 重新发送
                      </span>
                    ) : (
                      <span className="flex items-center gap-1.5">
                        <MailIcon className="size-4" /> 发送验证码
                      </span>
                    )}
                  </button>
                </div>
                {codeSent && (
                  <div className="mt-2 flex items-start gap-1.5 rounded-lg border border-[#b7d6c5] bg-[#f0faf4] px-3 py-2 text-xs text-[#2f6b4a]">
                    <CheckCircleIcon className="mt-0.5 size-3.5 shrink-0 text-[#5aa87c]" />
                    <span>验证码已发送到 {email.trim() || '你的邮箱'}，请在 30 分钟内填写。</span>
                  </div>
                )}
              </div>
            )}

            {error && (
              <div role="alert" className="flex items-start gap-2 rounded-lg border border-[#efb5ad] bg-[#fff4f2] px-3.5 py-2.5 text-sm text-[#963f35]">
                <CheckCircleIcon className="mt-0.5 size-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <button
              type="submit"
              disabled={busy}
              className="flex h-12 w-full items-center justify-center gap-2 rounded-md bg-[#1b2a45] px-5 text-sm font-semibold text-white transition-colors hover:bg-[#263a5e] disabled:opacity-60"
            >
              {busy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <ArrowRightIcon className="size-4" />}
              {mode === 'login' ? '登 录' : '注册并登录'}
            </button>
          </form>

          <p className="mt-6 text-center text-xs leading-5 text-[#8a96a7]">
            {mode === 'login' ? (
              <>
                <Link href="/reset-password" className="font-medium text-[#466fd0] hover:underline">忘记密码？</Link>
                <span className="mx-2 text-[#c8d0dc]">|</span>
                还没有账号？<button type="button" onClick={() => switchMode('register')} className="font-medium text-[#466fd0] hover:underline">立即注册</button>
              </>
            ) : (
              <>已有账号？<button type="button" onClick={() => switchMode('login')} className="font-medium text-[#466fd0] hover:underline">返回登录</button></>
            )}
          </p>
        </div>
        <p className="mt-5 text-center text-xs text-[#9aa5b5]">
          每位用户的简历与岗位数据相互隔离，请妥善保管账号。
        </p>
      </div>
    </div>
  );
}
