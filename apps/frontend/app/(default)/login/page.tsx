'use client';

import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import Image from 'next/image';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  BanIcon,
  CheckIcon,
  CheckCircleIcon,
  KeyRoundIcon,
  LoaderCircleIcon,
  LockKeyholeIcon,
  MailIcon,
  MessageSquareTextIcon,
  RefreshCwIcon,
  SendIcon,
  TicketIcon,
  UserRoundIcon,
} from 'lucide-react';
import { useAuth } from '@/components/workbench/auth-context';
import BrandMark from '@/components/workbench/brand-mark';
import EyeCube from '@/components/workbench/eye-cube';
import { useDialogBehavior } from '@/components/workbench/dialog-behavior';
import { API_URL } from '@/lib/api/config';
import {
  checkInviteCode,
  fetchCaptcha,
  fetchRegisterConfig,
  sendRegisterEmailCode,
  sendUnfreezeCode,
  submitInviteRequest,
  submitUnfreeze,
  type CaptchaData,
  type RegisterConfig,
} from '@/lib/api/auth-admin';

type Mode = 'login' | 'register';
type Panel = 'main' | 'apply' | 'unfreeze';

export default function LoginPage() {
  const router = useRouter();
  const { login, isAuthenticated, isHydrated } = useAuth();
  const [mode, setMode] = useState<Mode>('login');
  // 面板：主登录/注册 / 申请邀请码 / 自助解冻
  const [panel, setPanel] = useState<Panel>('main');

  // 注册状态
  const [username, setUsername] = useState('');          // 注册用用户名
  const [agreed, setAgreed] = useState(false);           // 是否已同意《用户协议》与《隐私政策》
  const [loginUsername, setLoginUsername] = useState(''); // 登录用用户名（与注册隔离）
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [inviteCode, setInviteCode] = useState('');
  const [inviteChecked, setInviteChecked] = useState(false);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [codeSent, setCodeSent] = useState(false);
  const [codeBusy, setCodeBusy] = useState(false);
  const [codeCountdown, setCodeCountdown] = useState(0);
  const [password, setPassword] = useState('');          // 注册用密码
  const [loginPassword, setLoginPassword] = useState(''); // 登录用密码（与注册隔离）
  const [confirm, setConfirm] = useState('');
  const [regConfig, setRegConfig] = useState<RegisterConfig | null>(null);

  // 申请邀请码
  const [applyEmail, setApplyEmail] = useState('');
  const [applyNote, setApplyNote] = useState('');
  const [applyMsg, setApplyMsg] = useState('');
  const [applyError, setApplyError] = useState('');
  const [applyBusy, setApplyBusy] = useState(false);

  // 自助解冻
  const [ufUsername, setUfUsername] = useState('');
  const [ufPassword, setUfPassword] = useState('');
  const [ufEmail, setUfEmail] = useState('');
  const [ufCode, setUfCode] = useState('');
  const [ufMsg, setUfMsg] = useState('');
  const [ufError, setUfError] = useState('');
  const [ufBusy, setUfBusy] = useState(false);
  const [ufCodeSent, setUfCodeSent] = useState(false);
  const [ufCodeBusy, setUfCodeBusy] = useState(false);
  const [ufCountdown, setUfCountdown] = useState(0);

  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  // 邀请码验证错误（显示在邀请码输入框下方，比表单底部错误更醒目）
  const [inviteError, setInviteError] = useState('');
  // 登录密码错误次数（提示用，前端累计；后端以服务端计数为准）
  const [loginFailCount, setLoginFailCount] = useState(0);
  // 管理员冻结提示弹窗（frozen_by=admin 的 423 响应）
  const [frozenNotice, setFrozenNotice] = useState<{ username: string; reason: string; adminEmail: string } | null>(null);
  const frozenPanelRef = useRef<HTMLDivElement>(null);

  useDialogBehavior({
    open: frozenNotice !== null,
    onClose: () => setFrozenNotice(null),
    panelRef: frozenPanelRef,
  });

  // 登录验证码（四位随机数字，一次性）：进入登录态时获取，失败后刷新
  const [captcha, setCaptcha] = useState<CaptchaData | null>(null);
  const [captchaInput, setCaptchaInput] = useState('');
  // 服务端未启用验证码时（GET 404）跳过前端校验，直接登录
  const [captchaUnavailable, setCaptchaUnavailable] = useState(false);
  const [captchaLoadError, setCaptchaLoadError] = useState(false);
  const captchaRequestRef = useRef(0);
  const captchaLoading = !captcha && !captchaUnavailable && !captchaLoadError;

  const loadCaptcha = useCallback(async () => {
    const request = ++captchaRequestRef.current;
    setCaptcha(null);
    setCaptchaInput('');
    setCaptchaUnavailable(false);
    setCaptchaLoadError(false);
    try {
      const data = await fetchCaptcha();
      if (request !== captchaRequestRef.current) return;
      setCaptcha(data);
      setCaptchaUnavailable(data === null);
    } catch {
      if (request === captchaRequestRef.current) setCaptchaLoadError(true);
    }
  }, []);

  useEffect(() => {
    if (mode === 'login' && panel === 'main') loadCaptcha();
  }, [mode, panel, loadCaptcha]);

  // 已登录则直接回首页
  useEffect(() => {
    if (isHydrated && isAuthenticated) router.replace('/');
  }, [isHydrated, isAuthenticated, router]);

  // 注册页配置（邀请码是否必填 + 联系邮箱）
  useEffect(() => {
    (async () => {
      try {
        setRegConfig(await fetchRegisterConfig());
      } catch {
        setRegConfig(null);
      }
    })();
  }, []);

  const switchMode = (next: Mode) => {
    setMode(next);
    setPanel('main');
    setError('');
    setInviteError('');
    setLoginFailCount(0);
    setConfirm('');
    setEmail('');
    setCode('');
    setCodeSent(false);
    setInviteChecked(false);
    setInviteCode('');
    setAgreed(false);
    if (next === 'login') {
      // 登录模式下注册字段归零；登录字段保留用户已输入内容
      setUsername('');
      setPassword('');
    } else {
      // 注册模式下登录字段归零；注册字段保留
      setLoginUsername('');
      setLoginPassword('');
    }
  };

  // 发送验证码倒计时
  useEffect(() => {
    if (codeCountdown <= 0) return;
    const timer = setTimeout(() => setCodeCountdown((c) => c - 1), 1000);
    return () => clearTimeout(timer);
  }, [codeCountdown]);

  // 解冻验证码倒计时
  useEffect(() => {
    if (ufCountdown <= 0) return;
    const timer = setTimeout(() => setUfCountdown((c) => c - 1), 1000);
    return () => clearTimeout(timer);
  }, [ufCountdown]);

  // ── 邀请码校验（注册 Tab）────────────────────────────────────
  const handleCheckInvite = async () => {
    setInviteError('');
    if (!inviteCode.trim()) {
      setInviteError('请填写邀请码。');
      return;
    }
    setInviteBusy(true);
    try {
      await checkInviteCode(inviteCode.trim());
      setInviteChecked(true);
      setInviteError('');
    } catch (caught) {
      setInviteChecked(false);
      setInviteError(caught instanceof Error ? caught.message : '邀请码校验失败，请检查后重试。');
    } finally {
      setInviteBusy(false);
    }
  };

  // ── 发送注册邮箱验证码（须先通过邀请码校验）────────────────────
  const handleSendCode = async () => {
    setError('');
    if (!inviteChecked) {
      setInviteError('请先通过邀请码校验。');
      return;
    }
    if (!email.trim()) {
      setError('请先填写邮箱。');
      return;
    }
    setCodeBusy(true);
    try {
      const message = await sendRegisterEmailCode(email.trim(), inviteCode.trim());
      setCodeSent(true);
      setCodeCountdown(60);
      setError('');
      if (message) {
        // 验证码已发送的提示放底部 codeSent 区块
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '验证码发送失败，请稍后重试。');
    } finally {
      setCodeBusy(false);
    }
  };

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    if (mode === 'register') {
      if (!agreed) {
        setError('请先阅读并同意《用户协议》与《隐私政策》。');
        return;
      }
      if (!inviteChecked) {
        setInviteError('请先验证邀请码。');
        return;
      }
      if (password !== confirm) {
        setError('两次输入的密码不一致。');
        return;
      }
      if (password.length < 8) {
        setError('密码长度至少 8 个字符。');
        return;
      }
      if (!email.trim()) {
        setError('请填写邮箱。');
        return;
      }
      if (!code.trim()) {
        setError('请输入邮箱验证码。');
        return;
      }
    } else if (mode === 'login') {
      if (!loginUsername.trim() || !loginPassword) {
        setError('请输入用户名和密码。');
        return;
      }
      if (!captcha && !captchaUnavailable) {
        setError(captchaLoading ? '验证码正在加载，请稍候。' : '验证码加载失败，请刷新后重试。');
        return;
      }
      if (captcha && !captchaInput.trim()) {
        setError('请输入右侧四位数字验证码。');
        return;
      }
    }
    setBusy(true);
    try {
      const submitUsername = mode === 'login' ? loginUsername.trim() : username.trim();
      const submitPassword = mode === 'login' ? loginPassword : password;
      const body: Record<string, string> = { username: submitUsername, password: submitPassword };
      if (mode === 'register') {
        body.email = email.trim();
        body.code = code.trim();
        body.invite_code = inviteCode.trim();
      } else if (captcha) {
        body.captcha_id = captcha.captcha_id;
        body.captcha_code = captchaInput.trim();
      }
      const response = await fetch(`${API_URL}/api/v1/auth/${mode}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const text = await response.text();
      let payload: {
        detail?: string;
        frozen_by?: 'admin' | 'auto';
        frozen_reason?: string;
        admin_email?: string;
        data?: { user_id?: string; username?: string; token?: string };
      } = {};
      try {
        payload = JSON.parse(text) as typeof payload;
      } catch {
        // ignore
      }
      if (!response.ok) {
        // 登录失败 → 累计次数提示（前端展示用；后端以服务端防爆破计数为准）
        if (mode === 'login' && response.status !== 423 && !payload.detail?.includes('验证码')) {
          setLoginFailCount((count) => count + 1);
        }
        // 423 = 账号被冻结：管理员冻结 → 弹窗提示联系管理员；系统自动冻结 → 引导自助解冻
        if (response.status === 423) {
          if (payload.frozen_by === 'admin') {
            setError('');
            setFrozenNotice({
              username: submitUsername,
              reason: payload.frozen_reason || '',
              adminEmail: payload.admin_email || 'luchenstudio@163.com',
            });
            return;
          }
          setPanel('unfreeze');
          setUfUsername(submitUsername);
          setUfPassword('');
          setUfEmail('');
          setUfCode('');
          setUfCodeSent(false);
          setError('');
          setUfError(payload.detail || '账号已冻结，请使用正确密码与邮箱验证码自助解冻。');
          return;
        }
        throw new Error(payload.detail || (text || `认证失败（HTTP ${response.status}）`));
      }
      if (!payload.data?.token || !payload.data?.user_id) {
        throw new Error('服务未返回有效的登录凭证。');
      }
      login(payload.data.token, { user_id: payload.data.user_id, username: payload.data.username || submitUsername });
      setLoginFailCount(0);
      router.replace('/');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '认证失败，请稍后重试。');
      // 验证码一次性：登录失败后必须换新验证码
      loadCaptcha();
    } finally {
      setBusy(false);
    }
  };

  // ── 申请邀请码提交 ───────────────────────────────────────────
  const handleApply = async (event: FormEvent) => {
    event.preventDefault();
    setApplyError('');
    setApplyMsg('');
    if (!applyEmail.trim()) {
      setApplyError('请填写邮箱。');
      return;
    }
    if (applyNote.trim().length < 2) {
      setApplyError('请填写申请理由（至少 2 个字符）。');
      return;
    }
    setApplyBusy(true);
    try {
      const message = await submitInviteRequest(applyEmail.trim(), applyNote.trim());
      setApplyMsg(message || '申请已提交，管理员审批通过后邀请码将发送至你的邮箱。');
    } catch (caught) {
      setApplyError(caught instanceof Error ? caught.message : '申请提交失败，请稍后重试。');
    } finally {
      setApplyBusy(false);
    }
  };

  // ── 自助解冻 ─────────────────────────────────────────────────
  const handleSendUnfreezeCode = async () => {
    setUfError('');
    setUfMsg('');
    if (!ufUsername.trim() || !ufEmail.trim()) {
      setUfError('请填写用户名和绑定邮箱。');
      return;
    }
    setUfCodeBusy(true);
    try {
      await sendUnfreezeCode(ufUsername.trim(), ufEmail.trim());
      setUfCodeSent(true);
      setUfCountdown(60);
      setUfMsg('解冻验证码已发送，请查收邮件。');
    } catch (caught) {
      setUfError(caught instanceof Error ? caught.message : '验证码发送失败，请稍后重试。');
    } finally {
      setUfCodeBusy(false);
    }
  };

  const handleUnfreeze = async (event: FormEvent) => {
    event.preventDefault();
    setUfError('');
    setUfMsg('');
    if (!ufUsername.trim() || !ufPassword || !ufEmail.trim() || !ufCode.trim()) {
      setUfError('请填写用户名、密码、绑定邮箱和邮箱验证码。');
      return;
    }
    setUfBusy(true);
    try {
      const result = await submitUnfreeze(ufUsername.trim(), ufPassword, ufEmail.trim(), ufCode.trim());
      if (result.token && result.user_id) {
        login(result.token, { user_id: result.user_id, username: result.username || ufUsername.trim() });
        router.replace('/');
      } else {
        setUfMsg(result.message || '账号已解冻。');
        setPanel('main');
        setMode('login');
        setLoginUsername(ufUsername.trim());
        setLoginPassword(ufPassword);
        setError('');
      }
    } catch (caught) {
      setUfError(caught instanceof Error ? caught.message : '解冻失败，请稍后重试。');
    } finally {
      setUfBusy(false);
    }
  };

  if (!isHydrated) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-soft text-sm text-sub">
        <LoaderCircleIcon className="mr-2 size-4 animate-spin" /> 正在载入
      </div>
    );
  }

  const lockIcon =
    'absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-sub';
  const inputCls =
    'h-[42px] w-full border border-line-strong bg-white pl-9 pr-3 text-[12.5px] text-ink outline-none transition focus:border-ink';
  const disabledInputCls =
    'h-[42px] w-full cursor-not-allowed border border-line bg-mist pl-9 pr-3 text-[12.5px] text-sub outline-none';
  const errorInputCls =
    'h-[42px] w-full border border-bad-border bg-bad-soft pl-9 pr-3 text-[12.5px] text-ink outline-none transition focus:border-bad';
  const captchaError = mode === 'login' && error.includes('验证码') ? error : '';
  const btnPrimary =
    'flex h-[46px] w-full items-center justify-center gap-2 bg-ink px-5 text-[14px] font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-50';

  return (
    <div className="flex min-h-screen flex-col bg-soft">
      <header className="flex h-[68px] shrink-0 items-center justify-between border-b border-line px-5 lg:px-11">
        <Link href="/" className="flex min-w-0 items-center gap-3">
          <BrandMark className="size-[30px]" />
          <span className="min-w-0">
            <span className="block text-[14.5px] font-semibold leading-tight tracking-[-0.01em] text-ink">AI 简历智选</span>
            <span className="mt-0.5 block font-mono text-[10.5px] uppercase leading-none tracking-[0.18em] text-sub">
              Screening workspace
            </span>
          </span>
        </Link>
      </header>

      <main className="flex-1 lg:grid lg:grid-cols-[minmax(0,1.12fr)_minmax(0,0.88fr)]">
        <section className="hidden flex-col justify-between border-r border-line bg-soft px-10 py-12 lg:flex lg:px-14 lg:pb-12 lg:pt-16">
          <div>

            <div className="flex items-start gap-9 xl:gap-10">
              <div className="min-w-0">
                <h1 className="text-[76px] font-medium leading-none tracking-[-0.045em] text-ink">AI 简历智选</h1>
                <p className="mt-[18px] max-w-[34ch] text-[14px] leading-[1.7] text-sub">
                  把简历、岗位描述与招聘结论放进同一张对照表。
                </p>
              </div>
              <EyeCube className="mt-1 shrink-0" />
            </div>

            <div className="mt-11 max-w-[480px]">
              <div className="relative h-px bg-line-strong">
                <span className="absolute left-0 top-[-1px] h-[3px] bg-ink" style={{ width: '88%' }} />
                <span className="absolute left-[60%] top-[-4px] h-[9px] w-px bg-line-strong" />
                <span className="absolute left-[60%] -top-5 -translate-x-1/2 font-mono text-[10px] tracking-[0.06em] text-sub">60</span>
                <span className="absolute left-[80%] top-[-4px] h-[9px] w-px bg-line-strong" />
                <span className="absolute left-[80%] -top-5 -translate-x-1/2 font-mono text-[10px] tracking-[0.06em] text-sub">80</span>
                <span className="absolute left-[88%] top-[-7px] h-[15px] w-px bg-ink" />
              </div>
              <div className="mt-3 grid grid-cols-[60fr_20fr_20fr] border-t border-line-soft pt-2 font-mono text-[10px] tracking-[0.06em] text-sub">
                <span>淘汰 &lt; 60</span>
                <span>储备 60–79</span>
                <span className="text-ink">优先 80+</span>
              </div>
              <div className="mt-3 flex items-center justify-between font-mono text-[9px] tracking-[0.12em] text-sub">
                <span>匹配刻度 · 0–100</span>
                <span>示例 88</span>
              </div>
            </div>

            <div className="mt-10 max-w-[460px] border-t border-line">
              {[
                ['01', '解析岗位门槛', '从 JD 抽取硬性条件'],
                ['02', '比对履历证据', '逐条回到简历原文'],
                ['03', '生成招聘建议', '分数与结论保持同源'],
              ].map(([code, title, desc]) => (
                <div key={code} className="flex items-baseline gap-3 border-b border-line-soft py-[14px] text-[13px]">
                  <span className="w-7 font-mono text-[11px] tracking-[0.16em] text-sub">{code}</span>
                  <span className="text-ink">{title}</span>
                  <span className="ml-auto text-[11.5px] text-sub">{desc}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="mt-auto flex max-w-[480px] pt-11">
            {[
              ['邀请制', '仅内部账号可用'],
              ['HTTPS', '生产环境强制加密'],
            ].map(([value, label], index) => (
              <div key={value} className={`flex-1 ${index === 0 ? '' : 'border-l border-line pl-5'}`}>
                <p className="text-[16px] font-medium leading-tight text-ink">{value}</p>
                <p className="mt-1.5 font-mono text-[11px] tracking-[0.1em] text-sub">{label}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="flex min-h-[calc(100dvh-126px)] items-center justify-center bg-white px-5 py-10 lg:justify-start lg:pl-[72px] lg:pr-11">
          <div className="w-full max-w-[380px]">
            <h1 className="sr-only lg:hidden">AI 简历智选登录</h1>
            <div className="mb-8 flex items-center gap-3 lg:hidden">
              <BrandMark className="size-8" />
              <span className="text-[14px] font-semibold text-ink">AI 简历智选</span>
            </div>

          {panel === 'main' && (
            <>
              <p className="font-mono text-[10.5px] font-medium uppercase tracking-[0.18em] text-sub">
                {mode === 'login' ? 'Sign in' : 'Create account'}
              </p>
              <h2 className="mt-5 text-[30px] font-medium leading-[1.1] tracking-[-0.02em] text-ink">
                {mode === 'login' ? '登录' : '注册'}
              </h2>
              <p className="mb-[30px] mt-[9px] text-[13.5px] text-sub">
                {mode === 'login' ? '继续使用简历筛选工作台' : '创建账号，开始使用筛选工作台'}
              </p>

              <form onSubmit={handleSubmit} className="space-y-[18px]">
                {mode === 'register' && (
                  <div className="rounded-md border border-brand-soft bg-brand-soft p-3">
                    <p className="text-xs leading-5 text-brand-deep">
                      本平台采用邀请制，需要邀请码才能注册。还没有邀请码？
                      <button type="button" onClick={() => setPanel('apply')} className="ml-1 font-semibold underline">
                        点此申请
                      </button>
                    </p>
                  </div>
                )}

                {mode === 'register' && (
                  <div>
                    <label htmlFor="login-invite" className="text-[12.5px] font-medium text-body">邀请码</label>
                    <div className="mt-2 flex gap-2">
                      <div className="relative flex-1">
                        <TicketIcon className={lockIcon} />
                        <input
                          id="login-invite"
                          type="text"
                          value={inviteCode}
                          maxLength={regConfig?.invite_code_length ?? 4}
                          onChange={(event) => {
                            setInviteCode(event.target.value.toUpperCase().replace(/\s/g, ''));
                            setInviteChecked(false);
                            setInviteError('');
                          }}
                          placeholder={`${regConfig?.invite_code_length ?? 4} 位邀请码`}
                          autoComplete="off"
                          spellCheck={false}
                          disabled={inviteChecked}
                          className={inviteChecked ? disabledInputCls : inviteError ? errorInputCls : inputCls}
                        />
                      </div>
                      <button
                        type="button"
                        onClick={handleCheckInvite}
                        disabled={inviteBusy || inviteChecked}
                        className={`h-[42px] shrink-0 rounded-md px-4 text-sm font-semibold transition-all duration-200 ${
                          inviteChecked
                            ? 'bg-good-soft text-good-ink'
                            : 'bg-brand-deep text-white shadow-sm hover:bg-brand-hover'
                        }`}
                      >
                        {inviteBusy ? (
                          <LoaderCircleIcon className="size-4 animate-spin" />
                        ) : inviteChecked ? (
                          <span className="flex items-center gap-1"><CheckIcon className="size-4" /> 已通过</span>
                        ) : (
                          '验证'
                        )}
                      </button>
                    </div>
                    {inviteError && (
                      <div role="alert" className="animate-shake mt-2 flex items-start gap-2 rounded-lg border border-bad-border bg-bad-soft px-3 py-2.5 text-sm text-bad">
                        <BanIcon className="mt-0.5 size-4 shrink-0" />
                        <span>{inviteError}</span>
                      </div>
                    )}
                    {inviteChecked && (
                      <div role="status" className="mt-2 flex items-center gap-2 rounded-lg border border-good-border bg-good-soft px-3 py-2.5 text-sm text-good-ink">
                        <CheckCircleIcon className="size-4 shrink-0 text-good" />
                        <span>邀请码校验通过，可继续填写邮箱注册。</span>
                      </div>
                    )}
                  </div>
                )}

                <div>
                  <label htmlFor="login-username" className="text-[12.5px] font-medium text-body">用户名</label>
                  <div className="relative mt-2">
                    <UserRoundIcon className={lockIcon} />
                    <input
                      id="login-username"
                      type="text"
                      value={mode === 'login' ? loginUsername : username}
                      onChange={(event) => (mode === 'login' ? setLoginUsername(event.target.value) : setUsername(event.target.value))}
                      placeholder="字母、数字或中文"
                      autoComplete={mode === 'login' ? 'username' : 'username'}
                      spellCheck={false}
                      className={inputCls}
                    />
                  </div>
                </div>

                <div>
                  <label htmlFor="login-password" className="text-[12.5px] font-medium text-body">密码</label>
                  <div className="relative mt-2">
                    <LockKeyholeIcon className={lockIcon} />
                    <input
                      id="login-password"
                      type="password"
                      value={mode === 'login' ? loginPassword : password}
                      onChange={(event) => (mode === 'login' ? setLoginPassword(event.target.value) : setPassword(event.target.value))}
                      placeholder={mode === 'register' ? '至少 8 个字符' : '输入密码'}
                      autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                      className={inputCls}
                    />
                  </div>
                </div>

                {mode === 'register' && (
                  <div>
                    <label htmlFor="login-confirm" className="text-[12.5px] font-medium text-body">确认密码</label>
                    <div className="relative mt-2">
                      <LockKeyholeIcon className={lockIcon} />
                      <input
                        id="login-confirm"
                        type="password"
                        value={confirm}
                        onChange={(event) => setConfirm(event.target.value)}
                        placeholder="再次输入密码"
                        autoComplete="new-password"
                        className={inputCls}
                      />
                    </div>
                  </div>
                )}

                {mode === 'register' && (
                  <div>
                    <label htmlFor="login-email" className="text-[12.5px] font-medium text-body">邮箱</label>
                    <div className="relative mt-2">
                      <MailIcon className={inviteChecked ? lockIcon : 'absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-disabled-fg'} />
                      <input
                        id="login-email"
                        type="email"
                        value={email}
                        onChange={(event) => setEmail(event.target.value)}
                        placeholder="与申请邀请码时一致"
                        autoComplete="email"
                        spellCheck={false}
                        disabled={!inviteChecked}
                        className={inviteChecked ? inputCls : disabledInputCls}
                      />
                    </div>
                    {!inviteChecked && (
                      <p className="mt-1.5 text-xs text-sub">请先通过邀请码校验后填写邮箱。</p>
                    )}
                  </div>
                )}

                {mode === 'register' && (
                  <div>
                    <label htmlFor="login-code" className="text-[12.5px] font-medium text-body">邮箱验证码</label>
                    <div className="mt-2 flex gap-2">
                      <div className={`relative flex-1 transition-all ${codeSent ? 'text-good-ink' : ''}`}>
                        <KeyRoundIcon
                          className={`absolute left-3.5 top-1/2 size-4 -translate-y-1/2 transition-colors ${
                            codeSent ? 'text-good' : 'text-sub'
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
                          disabled={!inviteChecked}
                          className={
                            inviteChecked
                              ? 'h-[42px] w-full rounded-md border border-line-soft bg-white pl-10 pr-4 text-sm tracking-widest text-ink outline-none transition focus:border-brand focus:ring-2 focus:ring-brand/20 focus:bg-white'
                              : disabledInputCls
                          }
                        />
                      </div>
                      <button
                        type="button"
                        onClick={handleSendCode}
                        disabled={codeBusy || codeCountdown > 0 || !inviteChecked}
                        className={`h-[42px] shrink-0 rounded-md px-4 text-sm font-semibold transition-all duration-200 ${
                          !inviteChecked
                            ? 'cursor-not-allowed bg-soft text-disabled-fg'
                            : codeCountdown > 0
                              ? 'cursor-not-allowed bg-soft text-sub'
                              : codeSent
                                ? 'border border-good-border bg-good-soft text-good-ink hover:bg-good-border-soft'
                                : 'bg-brand-deep text-white shadow-sm hover:bg-brand-hover hover:shadow'
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
                            <SendIcon className="size-4" /> 发送验证码
                          </span>
                        )}
                      </button>
                    </div>
                    {codeSent && (
                      <div className="mt-2 flex items-start gap-1.5 rounded-lg border border-good-border bg-good-soft px-3 py-2 text-xs text-good-ink">
                        <CheckCircleIcon className="mt-0.5 size-3.5 shrink-0 text-good" />
                        <span>验证码已发送到 {email.trim() || '你的邮箱'}，请在 30 分钟内填写。</span>
                      </div>
                    )}
                  </div>
                )}

                {mode === 'login' && (
                  <div>
                    <label htmlFor="login-captcha" className="text-[12.5px] font-medium text-body">验证码</label>
                    <div className="mt-2 flex gap-2">
                      <div className="relative min-w-0 flex-1">
                        <KeyRoundIcon className={lockIcon} />
                        <input
                          id="login-captcha"
                          type="text"
                          inputMode="numeric"
                          maxLength={4}
                          value={captchaInput}
                          onChange={(event) => setCaptchaInput(event.target.value.replace(/\D/g, ''))}
                          placeholder="请输入右侧四位数字"
                          autoComplete="off"
                          spellCheck={false}
                          aria-describedby={captchaError ? 'captcha-hint captcha-error' : 'captcha-hint'}
                          aria-invalid={captchaError ? true : undefined}
                          className={captchaError ? errorInputCls : inputCls}
                        />
                      </div>
                      <div className="flex h-[42px] w-20 shrink-0 items-center justify-center overflow-hidden border border-line-strong bg-white text-xs text-sub sm:w-24">
                        {captcha ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={captcha.image} alt="验证码图片" className="size-full object-contain" />
                        ) : captchaUnavailable ? '未启用' : captchaLoadError ? '加载失败' : '加载中'}
                      </div>
                      <button
                        type="button"
                        onClick={() => { if (!captchaLoading) void loadCaptcha(); }}
                        aria-disabled={captchaLoading}
                        aria-label="刷新验证码"
                        className="flex size-[42px] shrink-0 items-center justify-center border border-control-border bg-control text-ink hover:bg-control-hover aria-disabled:cursor-wait aria-disabled:opacity-60"
                      >
                        <RefreshCwIcon className="size-4" aria-hidden="true" />
                      </button>
                    </div>
                    <p id="captcha-hint" className="mt-1.5 flex items-center gap-1 text-xs text-sub">
                      看不清？使用右侧按钮刷新验证码。
                    </p>
                    {captchaError && <p id="captcha-error" role="alert" className="mt-2 text-xs text-bad">{captchaError}</p>}
                  </div>
                )}

                {error && !captchaError && (
                  <div role="alert" className="flex items-start gap-2 rounded-lg border border-bad-border bg-bad-soft px-3.5 py-2.5 text-sm text-bad">
                    <BanIcon className="mt-0.5 size-4 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <span>{error}</span>
                      {mode === 'login' && loginFailCount > 0 && (
                        <p className="mt-1 text-xs leading-5 text-bad">
                          密码错误 {loginFailCount} 次。连续失败 3 次将暂停登录 5 分钟，6 次将冻结账号，请核对密码或使用「忘记密码」。
                        </p>
                      )}
                    </div>
                  </div>
                )}

                {mode === 'register' && (
                  <label className="flex items-start gap-2 rounded-md border border-line-soft bg-mist px-3 py-2.5 text-xs leading-5 text-sub">
                    <input
                      type="checkbox"
                      checked={agreed}
                      onChange={(event) => setAgreed(event.target.checked)}
                      className="mt-0.5 size-4 shrink-0 accent-check-accent"
                    />
                    <span>
                      我已阅读并同意
                      <Link href="/terms" className="mx-1 font-medium text-brand hover:underline">《用户协议》</Link>
                      与
                      <Link href="/privacy" className="mx-1 font-medium text-brand hover:underline">《隐私政策》</Link>
                      ，并承诺上传的简历均已取得候选人授权。
                    </span>
                  </label>
                )}

                <button
                  type="submit"
                  disabled={busy || (mode === 'register' && !agreed)}
                  className={btnPrimary}
                >
                  {busy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <ArrowRightIcon className="size-4" />}
                  {mode === 'login' ? '登录' : '注册并登录'}
                </button>
              </form>

              <div className="mt-5 space-y-2 text-center text-[12.5px] text-sub">
                <div role="group" aria-label="开通账号" className="flex flex-wrap justify-center gap-x-3">
                  {mode === 'login' ? (
                    <button type="button" onClick={() => switchMode('register')} className="hover:text-ink">注册账号</button>
                  ) : (
                    <button type="button" onClick={() => switchMode('login')} className="hover:text-ink">返回登录</button>
                  )}
                  <span aria-hidden="true">·</span>
                  <button type="button" onClick={() => setPanel('apply')} className="hover:text-ink">申请邀请码</button>
                </div>
                <div role="group" aria-label="找回访问" className="flex flex-wrap justify-center gap-x-3">
                  <Link href="/reset-password" className="hover:text-ink">忘记密码</Link>
                  <span aria-hidden="true">·</span>
                  <button type="button" onClick={() => setPanel('unfreeze')} className="hover:text-ink">账号自助解冻</button>
                </div>
              </div>
              <p className="mt-7 border-t border-line-soft pt-4 text-[12px] leading-[1.7] text-sub">
                {mode === 'login' ? (
                  <>
                    登录即表示你已阅读并同意
                    <Link href="/terms" className="mx-1 text-ink hover:underline">《用户协议》</Link>
                    与
                    <Link href="/privacy" className="mx-1 text-ink hover:underline">《隐私政策》</Link>
                  </>
                ) : (
                  '平台采用邀请制，注册需要邀请码。忘记密码可通过绑定邮箱重置；账号被冻结时请联系系统管理员。'
                )}
              </p>
            </>
          )}

          {panel === 'apply' && (
            <>
              <h2 className="text-[28px] font-medium leading-tight tracking-[-0.02em] text-ink">申请邀请码</h2>
              <p className="mt-2 text-sm leading-6 text-body">
                填写申请邮箱与使用理由，管理员审批通过后，邀请码将发送至你的邮箱。
              </p>
              <form onSubmit={handleApply} className="mt-6 space-y-4">
                <div>
                  <label htmlFor="apply-email" className="text-[12.5px] font-medium text-body">申请邮箱</label>
                  <div className="relative mt-2">
                    <MailIcon className={lockIcon} />
                    <input
                      id="apply-email"
                      type="email"
                      value={applyEmail}
                      onChange={(event) => setApplyEmail(event.target.value)}
                      placeholder="you@example.com"
                      autoComplete="email"
                      spellCheck={false}
                      className={inputCls}
                    />
                  </div>
                </div>
                <div>
                  <label htmlFor="apply-note" className="text-[12.5px] font-medium text-body">申请理由</label>
                  <div className="relative mt-2">
                    <MessageSquareTextIcon className="absolute left-3.5 top-4 size-4 text-sub" />
                    <textarea
                      id="apply-note"
                      value={applyNote}
                      onChange={(event) => setApplyNote(event.target.value)}
                      placeholder="例如：我是 HR 岗位，需要试用 AI 简历筛选工具（2–200 字）"
                      maxLength={200}
                      rows={4}
                      className="w-full rounded-md border border-line-soft bg-white py-3 pl-10 pr-4 text-sm leading-6 text-ink outline-none transition focus:border-brand focus:ring-2 focus:ring-brand/20"
                    />
                  </div>
                  <p className="mt-1 text-right text-[11px] text-sub">{applyNote.length}/200</p>
                </div>

                {applyError && (
                  <div role="alert" className="flex items-start gap-2 rounded-lg border border-bad-border bg-bad-soft px-3.5 py-2.5 text-sm text-bad">
                    <BanIcon className="mt-0.5 size-4 shrink-0" />
                    <span>{applyError}</span>
                  </div>
                )}
                {applyMsg && (
                  <div role="status" className="flex items-start gap-2 rounded-lg border border-good-border bg-good-soft px-3.5 py-2.5 text-sm text-good-ink">
                    <CheckCircleIcon className="mt-0.5 size-4 shrink-0 text-good" />
                    <span>{applyMsg}</span>
                  </div>
                )}

                <button type="submit" disabled={applyBusy} className={btnPrimary}>
                  {applyBusy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SendIcon className="size-4" />}
                  提交申请
                </button>
              </form>

              <div className="mt-4 border-t border-line-soft pt-4">
                <button
                  type="button"
                  onClick={() => setPanel('main')}
                  className="flex h-[42px] w-full items-center justify-center gap-2 rounded-md border border-line-soft bg-white text-sm font-medium text-body transition-colors hover:bg-soft hover:border-line-soft"
                >
                  <ArrowLeftIcon className="size-4" />
                  返回登录/注册
                </button>
              </div>
            </>
          )}

          {panel === 'unfreeze' && (
            <>
              <h2 className="text-[28px] font-medium leading-tight tracking-[-0.02em] text-ink">账号自助解冻</h2>
              <p className="mt-2 text-sm leading-6 text-body">
                账号因多次登录失败被临时冻结。输入正确密码并通过绑定邮箱验证即可解冻登录。
                {regConfig?.contact_email && (
                  <span className="block text-xs text-sub">如账号未绑定邮箱，请联系管理员 {regConfig.contact_email}。</span>
                )}
              </p>
              <form onSubmit={handleUnfreeze} className="mt-6 space-y-4">
                <div>
                  <label htmlFor="uf-username" className="text-[12.5px] font-medium text-body">用户名</label>
                  <div className="relative mt-2">
                    <UserRoundIcon className={lockIcon} />
                    <input
                      id="uf-username"
                      type="text"
                      value={ufUsername}
                      onChange={(event) => setUfUsername(event.target.value)}
                      placeholder="被冻结的账号"
                      autoComplete="username"
                      className={inputCls}
                    />
                  </div>
                </div>
                <div>
                  <label htmlFor="uf-password" className="text-[12.5px] font-medium text-body">密码</label>
                  <div className="relative mt-2">
                    <LockKeyholeIcon className={lockIcon} />
                    <input
                      id="uf-password"
                      type="password"
                      value={ufPassword}
                      onChange={(event) => setUfPassword(event.target.value)}
                      placeholder="账号的正确密码"
                      autoComplete="current-password"
                      className={inputCls}
                    />
                  </div>
                </div>
                <div>
                  <label htmlFor="uf-email" className="text-[12.5px] font-medium text-body">绑定邮箱</label>
                  <div className="relative mt-2">
                    <MailIcon className={lockIcon} />
                    <input
                      id="uf-email"
                      type="email"
                      value={ufEmail}
                      onChange={(event) => setUfEmail(event.target.value)}
                      placeholder="注册时绑定的邮箱"
                      autoComplete="email"
                      spellCheck={false}
                      className={inputCls}
                    />
                  </div>
                </div>
                <div>
                  <label htmlFor="uf-code" className="text-[12.5px] font-medium text-body">邮箱验证码</label>
                  <div className="mt-2 flex gap-2">
                    <div className="relative flex-1">
                      <KeyRoundIcon className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-sub" />
                      <input
                        id="uf-code"
                        type="text"
                        maxLength={6}
                        value={ufCode}
                        onChange={(event) => setUfCode(event.target.value.toUpperCase())}
                        placeholder="6 位验证码"
                        autoComplete="one-time-code"
                        spellCheck={false}
                        className="h-[42px] w-full rounded-md border border-line-soft bg-white pl-10 pr-4 text-sm tracking-widest text-ink outline-none transition focus:border-brand focus:ring-2 focus:ring-brand/20"
                      />
                    </div>
                    <button
                      type="button"
                      onClick={handleSendUnfreezeCode}
                      disabled={ufCodeBusy || ufCountdown > 0}
                      className={`h-[42px] shrink-0 rounded-md px-4 text-sm font-semibold transition-all duration-200 ${
                        ufCountdown > 0
                          ? 'cursor-not-allowed bg-soft text-sub'
                          : ufCodeSent
                            ? 'border border-good-border bg-good-soft text-good-ink hover:bg-good-border-soft'
                            : 'bg-brand-deep text-white shadow-sm hover:bg-brand-hover'
                      }`}
                    >
                      {ufCodeBusy ? (
                        <LoaderCircleIcon className="size-4 animate-spin" />
                      ) : ufCountdown > 0 ? (
                        <span className="whitespace-nowrap">{ufCountdown}s</span>
                      ) : (
                        <span className="flex items-center gap-1.5"><SendIcon className="size-4" /> 发送验证码</span>
                      )}
                    </button>
                  </div>
                </div>

                {ufError && (
                  <div role="alert" className="flex items-start gap-2 rounded-lg border border-bad-border bg-bad-soft px-3.5 py-2.5 text-sm text-bad">
                    <BanIcon className="mt-0.5 size-4 shrink-0" />
                    <span>{ufError}</span>
                  </div>
                )}
                {ufMsg && (
                  <div role="status" className="flex items-start gap-2 rounded-lg border border-good-border bg-good-soft px-3.5 py-2.5 text-sm text-good-ink">
                    <CheckCircleIcon className="mt-0.5 size-4 shrink-0 text-good" />
                    <span>{ufMsg}</span>
                  </div>
                )}

                <button type="submit" disabled={ufBusy} className={btnPrimary}>
                  {ufBusy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <RefreshCwIcon className="size-4" />}
                  解冻并登录
                </button>
              </form>

              <div className="mt-4 border-t border-line-soft pt-4">
                <button
                  type="button"
                  onClick={() => { setPanel('main'); setUfError(''); setUfMsg(''); }}
                  className="flex h-[42px] w-full items-center justify-center gap-2 rounded-md border border-line-soft bg-white text-sm font-medium text-body transition-colors hover:bg-soft hover:border-line-soft"
                >
                  <ArrowLeftIcon className="size-4" />
                  返回登录
                </button>
              </div>
            </>
          )}

          <p className="mt-6 text-center text-[10.5px] leading-5 text-sub">
            每位用户的简历与岗位数据相互隔离，请妥善保管账号。
          </p>
          </div>
        </section>
      </main>

      <footer className="flex h-[58px] shrink-0 items-center gap-5 border-t border-line px-5 text-[12px] text-sub lg:px-11">
        <Link href="/terms" className="transition-colors hover:text-ink">用户协议</Link>
        <Link href="/privacy" className="transition-colors hover:text-ink">隐私政策</Link>
        <span className="flex-1" />
        <span>公司内部招聘筛选</span>
        <span className="font-mono text-[9px] tracking-[0.12em]">v1.0</span>
      </footer>

      {/* 管理员冻结提示弹窗（frozen_by=admin 的 423 响应） */}
      {frozenNotice && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          role="presentation"
        >
          <div
            ref={frozenPanelRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="frozen-notice-title"
            className="w-full max-w-md rounded-md border border-line bg-white p-6 shadow-2xl"
          >
            <div className="flex items-start gap-4">
              <div className="flex size-11 shrink-0 items-center justify-center rounded-full bg-bad-soft">
                <BanIcon className="size-5 text-bad" />
              </div>
              <div className="min-w-0 flex-1">
                <h2 id="frozen-notice-title" className="text-lg font-semibold text-ink">
                  账号异常请联系系统管理员处理！
                </h2>
                {frozenNotice.reason && (
                  <p className="mt-2 text-sm text-body">
                    冻结原因：{frozenNotice.reason}
                  </p>
                )}
                <p className="mt-3 flex items-center gap-2.5 rounded-lg bg-soft px-3.5 py-2.5 text-sm text-body">
                  <Image src="/brand/email-avatar.svg" alt="" width={24} height={24} className="size-6 shrink-0 rounded-full" aria-hidden="true" />
                  <span>管理员email：<span className="font-semibold text-brand">{frozenNotice.adminEmail}</span></span>
                </p>
                <p className="mt-2 text-xs text-sub">
                  你的账号（{frozenNotice.username}）已被系统管理员冻结，请通过上述邮箱联系管理员核实处理。
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => setFrozenNotice(null)}
              className="mt-5 flex h-[42px] w-full items-center justify-center rounded-lg bg-brand-deep text-sm font-semibold text-white transition hover:bg-brand-hover"
            >
              我知道了
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
