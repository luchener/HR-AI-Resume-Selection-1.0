'use client';

import { FormEvent, useState } from 'react';
import Link from 'next/link';
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  CheckCircleIcon,
  KeyRoundIcon,
  LoaderCircleIcon,
  LockKeyholeIcon,
  MailIcon,
  ShieldCheckIcon,
} from 'lucide-react';
import { API_URL } from '@/lib/api/config';

type Step = 'email' | 'confirm' | 'done';

export default function ResetPasswordPage() {
  const [step, setStep] = useState<Step>('email');
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const handleRequest = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    setMessage('');
    if (!email.trim()) {
      setError('请输入注册邮箱。');
      return;
    }
    setBusy(true);
    try {
      const response = await fetch(`${API_URL}/api/v1/auth/reset-password/request`, {
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
        throw new Error(payload.detail || (text || `请求失败（HTTP ${response.status}）`));
      }
      setMessage(payload.detail || '如果该邮箱已注册，重置验证码已发送，请查收邮件。');
      setStep('confirm');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '请求失败，请稍后重试。');
    } finally {
      setBusy(false);
    }
  };

  const handleConfirm = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    setMessage('');
    if (!code.trim()) {
      setError('请输入邮件中的验证码。');
      return;
    }
    if (newPassword.length < 8) {
      setError('新密码长度至少 8 个字符。');
      return;
    }
    if (newPassword !== confirmPassword) {
      setError('两次输入的新密码不一致。');
      return;
    }
    setBusy(true);
    try {
      const response = await fetch(`${API_URL}/api/v1/auth/reset-password/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim(), code: code.trim(), new_password: newPassword }),
      });
      const text = await response.text();
      let payload: { detail?: string; data?: { message?: string } } = {};
      try {
        payload = JSON.parse(text) as typeof payload;
      } catch {
        // ignore
      }
      if (!response.ok) {
        throw new Error(payload.detail || (text || `重置失败（HTTP ${response.status}）`));
      }
      setMessage(payload.data?.message || '密码重置成功。');
      setStep('done');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '重置失败，请稍后重试。');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-[#f3f6fa] px-5 py-10">
      <div className="w-full max-w-md">
        <div className="rounded-lg border border-[#dce2eb] bg-white p-8 shadow-[0_24px_80px_rgba(19,31,51,0.1)] sm:p-10">
          <div className="flex size-12 items-center justify-center rounded-lg bg-[#17243b] text-white shadow-[6px_6px_0_#88a8ff]">
            <ShieldCheckIcon className="size-6" />
          </div>
          <h1 className="mt-6 text-2xl font-semibold text-[#152137]">重置密码</h1>
          <p className="mt-2 text-sm leading-6 text-[#6d7b91]">
            {step === 'email' && '输入注册邮箱，我们将发送 6 位重置验证码到你的邮箱'}
            {step === 'confirm' && '输入邮件中的验证码并设置新密码'}
            {step === 'done' && '密码已重置，请使用新密码登录'}
          </p>

          {step === 'email' && (
            <form onSubmit={handleRequest} className="mt-6 space-y-4">
              <div>
                <label htmlFor="reset-email" className="text-sm font-semibold text-[#3d4a60]">注册邮箱</label>
                <div className="relative mt-2">
                  <MailIcon className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-[#8a96a7]" />
                  <input
                    id="reset-email"
                    type="email"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    placeholder="you@example.com"
                    autoComplete="email"
                    spellCheck={false}
                    className="h-12 w-full rounded-md border border-[#cfd8e5] bg-[#fbfcfe] pl-10 pr-4 text-sm text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff]"
                  />
                </div>
              </div>

              {error && (
                <div role="alert" className="rounded-md border border-[#efb5ad] bg-[#fff4f2] px-4 py-3 text-sm text-[#963f35]">
                  {error}
                </div>
              )}

              <button
                type="submit"
                disabled={busy}
                className="flex h-12 w-full items-center justify-center gap-2 rounded-md bg-[#1b2a45] px-5 text-sm font-semibold text-white transition-colors hover:bg-[#263a5e] disabled:opacity-60"
              >
                {busy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <ArrowRightIcon className="size-4" />}
                发送重置邮件
              </button>
            </form>
          )}

          {step === 'confirm' && (
            <>
              {message && (
                <div role="status" className="mt-6 rounded-md border border-[#b7d6c5] bg-[#f0faf4] px-4 py-3 text-sm text-[#2f6b4a]">
                  {message}
                </div>
              )}
              <form onSubmit={handleConfirm} className="mt-4 space-y-4">
                <div className="flex items-center gap-2 rounded-lg border border-[#e0e7f1] bg-[#f7f9fd] px-3 py-2.5">
                  <MailIcon className="size-4 shrink-0 text-[#6d7b91]" />
                  <span className="truncate text-xs text-[#6d7b91]">
                    已发往 <span className="font-semibold text-[#253249]">{email || '你的邮箱'}</span>
                  </span>
                </div>
                <div>
                  <label htmlFor="reset-code" className="text-sm font-semibold text-[#3d4a60]">邮箱验证码</label>
                  <div className="relative mt-2">
                    <KeyRoundIcon className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-[#8a96a7]" />
                    <input
                      id="reset-code"
                      type="text"
                      maxLength={6}
                      value={code}
                      onChange={(event) => setCode(event.target.value.toUpperCase())}
                      placeholder="邮件中的 6 位验证码"
                      autoComplete="one-time-code"
                      spellCheck={false}
                      className="h-12 w-full rounded-xl border border-[#cfd8e5] bg-[#fbfcfe] pl-10 pr-4 text-sm tracking-widest text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff] focus:bg-white"
                    />
                  </div>
                </div>
                <div>
                  <label htmlFor="reset-new-password" className="text-sm font-semibold text-[#3d4a60]">新密码</label>
                  <div className="relative mt-2">
                    <LockKeyholeIcon className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-[#8a96a7]" />
                    <input
                      id="reset-new-password"
                      type="password"
                      value={newPassword}
                      onChange={(event) => setNewPassword(event.target.value)}
                      placeholder="至少 8 个字符，含字母和数字"
                      autoComplete="new-password"
                      className="h-12 w-full rounded-xl border border-[#cfd8e5] bg-[#fbfcfe] pl-10 pr-4 text-sm text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff] focus:bg-white"
                    />
                  </div>
                </div>
                <div>
                  <label htmlFor="reset-confirm-password" className="text-sm font-semibold text-[#3d4a60]">确认新密码</label>
                  <div className="relative mt-2">
                    <LockKeyholeIcon className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-[#8a96a7]" />
                    <input
                      id="reset-confirm-password"
                      type="password"
                      value={confirmPassword}
                      onChange={(event) => setConfirmPassword(event.target.value)}
                      placeholder="再次输入新密码"
                      autoComplete="new-password"
                      className="h-12 w-full rounded-xl border border-[#cfd8e5] bg-[#fbfcfe] pl-10 pr-4 text-sm text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff] focus:bg-white"
                    />
                  </div>
                </div>

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
                  重置密码
                </button>
              </form>
            </>
          )}

          {step === 'done' && (
            <div className="mt-6 rounded-md border border-[#b7d6c5] bg-[#f0faf4] px-4 py-3 text-sm text-[#2f6b4a]">
              {message || '密码重置成功。'}
            </div>
          )}

          <p className="mt-6 text-center text-xs text-[#8a96a7]">
            <Link href="/login" className="inline-flex items-center gap-1 font-medium text-[#466fd0] hover:underline">
              <ArrowLeftIcon className="size-3" /> 返回登录
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}