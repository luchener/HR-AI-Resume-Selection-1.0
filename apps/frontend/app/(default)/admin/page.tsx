'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  ArrowDownToLineIcon,
  BanIcon,
  BarChart3Icon,
  CalendarDaysIcon,
  CheckCircle2Icon,
  CopyIcon,
  FileJsonIcon,
  HistoryIcon,
  InboxIcon,
  KeyRoundIcon,
  LoaderCircleIcon,
  LockKeyholeIcon,
  MailIcon,
  MoreHorizontalIcon,
  RefreshCwIcon,
  SaveIcon,
  SendIcon,
  ShieldCheckIcon,
  SnowflakeIcon,
  TicketIcon,
  UserRoundIcon,
  XCircleIcon,
} from 'lucide-react';
import AppShell from '@/components/workbench/app-shell';
import { useAuth } from '@/components/workbench/auth-context';
import AdminModal from '@/components/workbench/admin-modal';
import ConfirmDialog from '@/components/workbench/confirm-dialog';
import {
  adminCreateUser,
  adminDeleteUser,
  adminFreezeUser,
  adminResetUserPassword,
  adminRestoreUser,
  adminUnfreezeUser,
  adminUpdateUser,
  approveInviteRequest,
  downloadAdminOpsExport,
  fetchAdminOps,
  fetchAdminUsers,
  fetchDeletedUsers,
  fetchFrozenUsers,
  fetchInviteCodes,
  fetchInviteRequests,
  fetchUsageRanking,
  fetchUserUsage,
  generateInviteCodes,
  rejectInviteRequest,
  resendInviteRequest,
  revokeInviteCode,
  type AdminOpItem,
  type AdminUserItem,
  type DeletedUserItem,
  type FrozenUser,
  type InviteCodeItem,
  type InviteRequestItem,
  type UsageGranularity,
  type UsageRankingItem,
  type UserUsageData,
} from '@/lib/api/auth-admin';

type Tab = 'requests' | 'codes' | 'frozen' | 'users' | 'ops';

const TAB_KEYS: Tab[] = ['requests', 'codes', 'frozen', 'users', 'ops'];

/** 从 URL query 解析 Tab（非法值回落 'requests'）；仅在浏览器端可用 */
function tabFromQuery(): Tab {
  if (typeof window === 'undefined') return 'requests';
  const raw = new URLSearchParams(window.location.search).get('tab') as Tab | null;
  return raw && (TAB_KEYS as string[]).includes(raw) ? raw : 'requests';
}

function fmtTime(iso?: string | number | null): string {
  if (iso === null || iso === undefined || iso === '') return '--';
  // 兼容 epoch 秒 / 毫秒数字（旧数据）：number 直接换算，字符串数字也换算，ISO 字符串直接解析
  let num: number = NaN;
  if (typeof iso === 'number') {
    num = iso;
  } else if (typeof iso === 'string' && /^\d+(\.\d+)?$/.test(iso.trim())) {
    num = Number(iso.trim());
  }
  const d = Number.isNaN(num) ? new Date(iso) : new Date(num > 1e12 ? num : num * 1000);
  if (Number.isNaN(d.getTime())) return '--';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function shortHash(hash: string): string {
  if (!hash) return '--';
  return hash.length > 12 ? `${hash.slice(0, 8)}…${hash.slice(-4)}` : hash;
}

/** 使用统计横轴标签格式化：day → MM-DD，month → YYYY-MM，year → YYYY（后端已返回字符串） */
function fmtUsageLabel(label: string, granularity: 'day' | 'month' | 'year'): string {
  if (!label) return '--';
  if (granularity === 'day') {
    // "2026-09-04" → "09-04"
    return label.length >= 10 ? label.slice(5) : label;
  }
  if (granularity === 'month') {
    // "2026-09" → "26-09"? 保持 YYYY-MM
    return label;
  }
  return label; // year: "2026"
}

export default function AdminPage() {
  const router = useRouter();
  const { isHydrated, isAdmin, isSuperAdmin } = useAuth();
  const [tab, setTab] = useState<Tab>('requests');
  const [notice, setNotice] = useState<{ text: string; kind: 'ok' | 'err' }>({ text: '', kind: 'ok' });

  /** 待确认的高风险操作（P1-2：替代原生 window.confirm） */
  const [pendingConfirm, setPendingConfirm] = useState<{
    title: string;
    message: React.ReactNode;
    danger: boolean;
    confirmLabel: string;
    run: () => void;
  } | null>(null);

  /** 发起一次产品级确认（P1-2） */
  const askConfirm = (opts: { title: string; message: React.ReactNode; danger?: boolean; confirmLabel?: string; run: () => void }) => {
    setPendingConfirm({ title: opts.title, message: opts.message, danger: opts.danger ?? false, confirmLabel: opts.confirmLabel ?? '确认', run: opts.run });
  };

  // Tab 状态记忆：挂载时从 URL 恢复；切换时用 history.replaceState 写入 URL（不触发整页跳转）
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const fromUrl = tabFromQuery();
    if (fromUrl !== 'requests') setTab(fromUrl);
  }, []);
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const url = new URL(window.location.href);
    const prev = url.searchParams.get('tab');
    if (prev !== tab) {
      url.searchParams.set('tab', tab);
      window.history.replaceState(null, '', url.toString());
    }
  }, [tab]);

  // 待审批 / 历史
  const [reqTab, setReqTab] = useState<'pending' | 'approved' | 'rejected'>('pending');
  const [requests, setRequests] = useState<InviteRequestItem[]>([]);
  const [reqLoading, setReqLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState<Record<string, string>>({});

  // 邀请码总览
  const [codeStatus, setCodeStatus] = useState('all');
  const [codes, setCodes] = useState<InviteCodeItem[]>([]);
  const [codeLoading, setCodeLoading] = useState(false);
  const [genCount, setGenCount] = useState(1);
  const [genNote, setGenNote] = useState('');
  const [genBusy, setGenBusy] = useState(false);
  const [genCodes, setGenCodes] = useState<string[]>([]);

  // 冻结账号
  const [frozen, setFrozen] = useState<FrozenUser[]>([]);
  const [frozenLoading, setFrozenLoading] = useState(false);
  const [unfreezeBusy, setUnfreezeBusy] = useState<string | null>(null);

  // 用户管理
  const [users, setUsers] = useState<AdminUserItem[]>([]);
  const [userTotal, setUserTotal] = useState(0);
  const [userLoading, setUserLoading] = useState(false);
  const [userKeyword, setUserKeyword] = useState('');
  const [userBusy, setUserBusy] = useState<string | null>(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [createForm, setCreateForm] = useState({ username: '', password: '', email: '', is_admin: false });
  const [createBusy, setCreateBusy] = useState(false);

  // 操作记录（审计）
  const [ops, setOps] = useState<AdminOpItem[]>([]);
  const [opsTotal, setOpsTotal] = useState(0);
  const [opsLoading, setOpsLoading] = useState(false);
  const [opsFilter, setOpsFilter] = useState('');
  const [opsKeyword, setOpsKeyword] = useState('');

  // 使用统计弹窗
  const [usageUser, setUsageUser] = useState<AdminUserItem | null>(null);
  const [usageGran, setUsageGran] = useState<UsageGranularity>('day');
  const [usageData, setUsageData] = useState<UserUsageData | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);

  // 冻结弹窗
  const [freezeTarget, setFreezeTarget] = useState<AdminUserItem | null>(null);
  const [freezeReason, setFreezeReason] = useState('');
  const [freezeBusy, setFreezeBusy] = useState(false);

  // 改邮箱弹窗
  const [emailTarget, setEmailTarget] = useState<AdminUserItem | null>(null);
  const [newEmail, setNewEmail] = useState('');
  const [emailBusy, setEmailBusy] = useState(false);

  // 使用排行弹窗
  const [rankOpen, setRankOpen] = useState(false);
  const [rankData, setRankData] = useState<UsageRankingItem[]>([]);
  const [rankLoading, setRankLoading] = useState(false);

  // 操作记录分页
  const [opsPage, setOpsPage] = useState(1);
  const OPS_PAGE_SIZE = 20;

  // 行内「…」菜单（fixed 定位，不受表格 overflow 裁剪）：记录目标用户名与按钮坐标
  const [rowMenu, setRowMenu] = useState<{ username: string; x: number; y: number } | null>(null);
  const rowMenuRef = useRef<HTMLDivElement>(null);
  const rowMenuTriggerRef = useRef<HTMLButtonElement | null>(null);

  // 菜单打开 → 聚焦首个菜单项（键盘可达）
  useEffect(() => {
    if (!rowMenu) return;
    rowMenuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
  }, [rowMenu]);

  // 审计导出
  const [exportBusy, setExportBusy] = useState(false);

  // 点击行菜单外部 → 收起
  useEffect(() => {
    if (!rowMenu) return;
    const onDown = (event: PointerEvent) => {
      if (rowMenuRef.current && !rowMenuRef.current.contains(event.target as Node)) setRowMenu(null);
    };
    document.addEventListener('pointerdown', onDown);
    return () => document.removeEventListener('pointerdown', onDown);
  }, [rowMenu]);

  const flash = (text: string, kind: 'ok' | 'err' = 'ok') => {
    setNotice({ text, kind });
    window.setTimeout(() => setNotice({ text: '', kind: 'ok' }), 5000);
  };

  const loadRequests = useCallback(async () => {
    setReqLoading(true);
    try {
      const items = await fetchInviteRequests(reqTab);
      setRequests(items);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '申请单读取失败。', 'err');
    } finally {
      setReqLoading(false);
    }
  }, [reqTab]);

  const loadCodes = useCallback(async () => {
    setCodeLoading(true);
    try {
      const items = await fetchInviteCodes(codeStatus);
      setCodes(items);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '邀请码列表读取失败。', 'err');
    } finally {
      setCodeLoading(false);
    }
  }, [codeStatus]);

  const loadFrozen = useCallback(async () => {
    setFrozenLoading(true);
    try {
      const items = await fetchFrozenUsers();
      setFrozen(items);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '冻结列表读取失败。', 'err');
    } finally {
      setFrozenLoading(false);
    }
  }, []);

  const loadUsers = useCallback(async () => {
    setUserLoading(true);
    try {
      const data = await fetchAdminUsers(userKeyword, 1, 100);
      setUsers(data.items);
      setUserTotal(data.total);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '用户列表读取失败。', 'err');
    } finally {
      setUserLoading(false);
    }
  }, [userKeyword]);

  const loadDeletedUsers = useCallback(async () => {
    setDeletedLoading(true);
    try {
      setDeletedUsers(await fetchDeletedUsers());
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '已删除账号读取失败。', 'err');
    } finally {
      setDeletedLoading(false);
    }
  }, []);

  const loadOps = useCallback(async (page: number) => {
    setOpsLoading(true);
    try {
      const data = await fetchAdminOps(opsFilter, opsKeyword, page, OPS_PAGE_SIZE);
      setOps(data.items);
      setOpsTotal(data.total);
      setOpsPage(data.page);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '操作记录读取失败。', 'err');
    } finally {
      setOpsLoading(false);
    }
  }, [opsFilter, opsKeyword]);

  // 过滤条件变化时回到第 1 页重新加载
  useEffect(() => {
    if (!isHydrated || !isAdmin || tab !== 'ops') return;
    setOpsPage(1);
    loadOps(1);
  }, [tab, opsFilter, opsKeyword, isHydrated, isAdmin, loadOps]);

  useEffect(() => {
    if (!isHydrated || !isAdmin) return;
    if (tab === 'requests') loadRequests();
    else if (tab === 'codes') loadCodes();
    else if (tab === 'frozen') loadFrozen();
    else if (tab === 'users') {
      loadUsers();
      loadDeletedUsers();
    } else loadUsers();
  }, [isHydrated, isAdmin, tab, loadRequests, loadCodes, loadFrozen, loadUsers, loadDeletedUsers]);

  const handleApprove = async (item: InviteRequestItem) => {
    setBusyId(item.request_id);
    try {
      const result = await approveInviteRequest(item.request_id);
      if (result.invite_code) {
        // 明文仅此一次出现：提示管理员可复制人工转达
        await navigator.clipboard?.writeText(result.invite_code).catch(() => {});
        flash(`已通过，邀请码已复制：${result.invite_code}（邮件发送失败时可人工转达）`);
      } else {
        flash(result.message || '审批已通过，邀请码已发送。');
      }
      await loadRequests();
      await loadCodes();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '审批失败。', 'err');
    } finally {
      setBusyId(null);
    }
  };

  const handleReject = async (item: InviteRequestItem) => {
    const reason = (rejectReason[item.request_id] || '').trim();
    setBusyId(item.request_id);
    try {
      await rejectInviteRequest(item.request_id, reason);
      flash('已拒绝该申请。');
      await loadRequests();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '拒绝失败。', 'err');
    } finally {
      setBusyId(null);
    }
  };

  const handleResend = async (requestId: string) => {
    setBusyId(requestId);
    try {
      const msg = await resendInviteRequest(requestId);
      flash(msg);
      await loadRequests();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '补发失败。', 'err');
    } finally {
      setBusyId(null);
    }
  };

  const handleRevoke = async (item: InviteCodeItem) => {
    setBusyId(item.code_hash);
    try {
      const msg = await revokeInviteCode(item.code_hash);
      flash(msg);
      await loadCodes();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '作废失败。', 'err');
    } finally {
      setBusyId(null);
    }
  };

  const handleGenerate = async () => {
    setGenBusy(true);
    try {
      const codesArr = await generateInviteCodes(Math.max(1, Math.min(genCount || 1, 20)), genNote.trim());
      setGenCodes(codesArr);
      flash(`已生成 ${codesArr.length} 个邀请码，请立即复制（服务器不保存明文）。`);
      await loadCodes();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '生成失败。', 'err');
    } finally {
      setGenBusy(false);
    }
  };

  const handleUnfreeze = async (username: string) => {
    setUnfreezeBusy(username);
    try {
      const msg = await adminUnfreezeUser(username);
      flash(msg);
      await loadFrozen();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '解冻失败。', 'err');
    } finally {
      setUnfreezeBusy(null);
    }
  };

  // ── 用户管理操作 ────────────────────────────────────────────────
  const handleToggleAdmin = async (item: AdminUserItem) => {
    const target = !item.is_admin;
    setUserBusy(item.username);
    try {
      const msg = await adminUpdateUser(item.username, { is_admin: target });
      flash(msg);
      await loadUsers();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '更新权限失败。', 'err');
    } finally {
      setUserBusy(null);
    }
  };

  const handleResetUserPassword = async (item: AdminUserItem) => {
    setUserBusy(item.username);
    try {
      const msg = await adminResetUserPassword(item.username);
      flash(msg);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '重置密码失败。', 'err');
    } finally {
      setUserBusy(null);
    }
  };

  // 删除账号弹窗（仅超级管理员，需输入管理员密码确认）
  const [deleteTarget, setDeleteTarget] = useState<AdminUserItem | null>(null);
  const [deletePassword, setDeletePassword] = useState('');
  const [deleteBusy, setDeleteBusy] = useState(false);

  // 已删除账号（90 天内可恢复）
  const [deletedUsers, setDeletedUsers] = useState<DeletedUserItem[]>([]);
  const [deletedLoading, setDeletedLoading] = useState(false);
  const [restoreBusy, setRestoreBusy] = useState<string | null>(null);

  const handleDeleteUser = async (item: AdminUserItem) => {
    setDeleteTarget(item);
    setDeletePassword('');
  };

  const confirmDeleteUser = async () => {
    if (!deleteTarget) return;
    if (!deletePassword) {
      flash('请输入管理员密码。', 'err');
      return;
    }
    setDeleteBusy(true);
    try {
      const msg = await adminDeleteUser(deleteTarget.username, deletePassword);
      flash(msg);
      setDeleteTarget(null);
      setDeletePassword('');
      await Promise.all([loadUsers(), loadDeletedUsers()]);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '删除失败。', 'err');
    } finally {
      setDeleteBusy(false);
    }
  };

  const handleRestoreUser = async (username: string) => {
    setRestoreBusy(username);
    try {
      const msg = await adminRestoreUser(username);
      flash(msg);
      await Promise.all([loadUsers(), loadDeletedUsers()]);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '恢复失败。', 'err');
    } finally {
      setRestoreBusy(null);
    }
  };

  const handleCreateUser = async () => {
    const name = createForm.username.trim();
    if (!name) {
      flash('请填写用户名。', 'err');
      return;
    }
    if (createForm.password.length < 8) {
      flash('密码至少 8 个字符。', 'err');
      return;
    }
    setCreateBusy(true);
    try {
      const msg = await adminCreateUser({
        username: name,
        password: createForm.password,
        email: createForm.email.trim() || undefined,
        is_admin: createForm.is_admin,
      });
      flash(msg);
      setShowCreateForm(false);
      setCreateForm({ username: '', password: '', email: '', is_admin: false });
      await loadUsers();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '创建失败。', 'err');
    } finally {
      setCreateBusy(false);
    }
  };

  // ── 使用统计弹窗 ────────────────────────────────────────────────
  const openUsage = async (item: AdminUserItem) => {
    setUsageUser(item);
    setUsageGran('day');
    setUsageData(null);
    setUsageLoading(true);
    try {
      const data = await fetchUserUsage(item.username, 'day', 30);
      setUsageData(data);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '使用统计读取失败。', 'err');
    } finally {
      setUsageLoading(false);
    }
  };

  const switchUsageGran = async (gran: UsageGranularity) => {
    if (!usageUser) return;
    setUsageGran(gran);
    setUsageLoading(true);
    try {
      const data = await fetchUserUsage(usageUser.username, gran, gran === 'day' ? 30 : gran === 'month' ? 12 : 5);
      setUsageData(data);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '使用统计读取失败。', 'err');
    } finally {
      setUsageLoading(false);
    }
  };

  // ── 使用统计导出 CSV（前端组装，Excel 兼容 utf-8-sig）────────────
  const handleExportUsageCsv = () => {
    if (!usageUser || !usageData) return;
    const d = usageData;
    const rows: string[][] = [
      [`用户:${usageUser.username}`, '', '', '', ''],
      ['日期/区间', '登录', '分析', '合计', ''],
      ...d.labels.map((label, i) => [
        String(label),
        String(d.series.login[i] ?? 0),
        String(d.series.analysis[i] ?? 0),
        String(d.series.total[i] ?? 0),
        '',
      ]),
      ['合计', String(d.summary?.login_total ?? 0), String(d.summary?.analysis_total ?? 0), String(d.summary?.total ?? 0), ''],
      ['日均/月均/年均', '', '', String(d.summary?.avg_per_bucket ?? 0), ''],
      ['峰值', '', '', `${d.summary?.peak_label ?? ''}:${d.summary?.peak_total ?? 0}`, ''],
      ['首次使用', '', '', d.summary?.first_usage ?? '', ''],
      ['最近使用', '', '', d.summary?.last_usage ?? '', ''],
      ['活跃期数', '', '', String(d.summary?.active_buckets ?? 0), ''],
    ];
    const csv = rows
      .map((r) => r.map((cell) => `"${String(cell ?? '').replace(/"/g, '""')}"`).join(','))
      .join('\r\n');
    const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `usage-${usageUser.username}-${d.granularity}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    flash(`已导出使用统计 CSV（${d.labels.length} 个${d.granularity === 'day' ? '天' : d.granularity === 'month' ? '月' : '年'}）。`);
  };

  const copyCode = async (code: string) => {
    await navigator.clipboard?.writeText(code).catch(() => {});
    flash(`已复制：${code}`);
  };

  // ── 冻结 / 解冻 ────────────────────────────────────────────────
  const openFreeze = (item: AdminUserItem) => {
    setFreezeTarget(item);
    setFreezeReason('');
  };

  const handleFreeze = async () => {
    if (!freezeTarget) return;
    const reason = freezeReason.trim();
    if (!reason) {
      flash('请填写冻结原因（便于用户核对与审计留痕）。', 'err');
      return;
    }
    setFreezeBusy(true);
    try {
      const msg = await adminFreezeUser(freezeTarget.username, reason);
      flash(msg);
      setFreezeTarget(null);
      await loadUsers();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '冻结失败。', 'err');
    } finally {
      setFreezeBusy(false);
    }
  };

  const handleUnfreezeUser = async (item: AdminUserItem) => {
    setUserBusy(item.username);
    try {
      const msg = await adminUnfreezeUser(item.username);
      flash(msg);
      await loadUsers();
      await loadFrozen();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '解冻失败。', 'err');
    } finally {
      setUserBusy(null);
    }
  };

  // ── 改邮箱 ─────────────────────────────────────────────────────
  const openEmail = (item: AdminUserItem) => {
    setEmailTarget(item);
    setNewEmail('');
  };

  const handleChangeEmail = async () => {
    if (!emailTarget) return;
    const email = newEmail.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      flash('请填写有效的邮箱地址。', 'err');
      return;
    }
    setEmailBusy(true);
    try {
      const msg = await adminUpdateUser(emailTarget.username, { email });
      flash(msg);
      setEmailTarget(null);
      await loadUsers();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '修改邮箱失败。', 'err');
    } finally {
      setEmailBusy(false);
    }
  };

  // ── 使用排行 ───────────────────────────────────────────────────
  const openRanking = async () => {
    setRankOpen(true);
    setRankLoading(true);
    try {
      const items = await fetchUsageRanking(50);
      setRankData(items);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '使用排行读取失败。', 'err');
    } finally {
      setRankLoading(false);
    }
  };

  // ── 操作记录导出 ────────────────────────────────────────────────
  const handleExportOps = async (format: 'csv' | 'json') => {
    setExportBusy(true);
    try {
      await downloadAdminOpsExport(opsFilter, opsKeyword, format);
      if (format === 'csv') flash('已导出 CSV（可用 Excel 直接打开）。');
      else flash('已导出 JSON。');
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : '导出失败。', 'err');
    } finally {
      setExportBusy(false);
    }
  };

  // ── 行内「…」菜单 ───────────────────────────────────────────────
  const toggleRowMenu = (event: React.MouseEvent<HTMLButtonElement>, username: string) => {
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    rowMenuTriggerRef.current = event.currentTarget;
    setRowMenu((prev) => (prev && prev.username === username ? null : { username, x: rect.right, y: rect.bottom + 4 }));
  };

  const onRowMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(rowMenuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
    if (items.length === 0) return;
    const idx = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      items[(idx + 1) % items.length].focus();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      items[(idx - 1 + items.length) % items.length].focus();
    } else if (event.key === 'Escape' || event.key === 'Tab') {
      setRowMenu(null);
      rowMenuTriggerRef.current?.focus();
    }
  };

  // 守卫
  if (!isHydrated) {
    return (
      <AppShell active="admin">
        <div className="flex min-h-screen items-center justify-center text-sm text-sub">
          <LoaderCircleIcon className="mr-2 size-4 animate-spin" /> 正在载入
        </div>
      </AppShell>
    );
  }
  if (!isAdmin) {
    return (
      <AppShell active="admin">
        <div className="flex min-h-[60vh] items-center justify-center px-5">
          <div className="max-w-md rounded-md border border-line bg-white p-8 text-center">
            <LockKeyholeIcon className="mx-auto size-9 text-sub" />
            <h1 className="mt-5 text-xl font-semibold text-ink">无权限访问</h1>
            <p className="mt-2 text-sm leading-6 text-sub">账号管理仅对管理员开放。</p>
            <button type="button" onClick={() => router.replace('/')} className="mt-6 inline-flex h-10 items-center gap-2 rounded-md bg-brand-deep px-5 text-sm font-medium text-white">
              返回首页
            </button>
          </div>
        </div>
      </AppShell>
    );
  }

  const tabCls = (active: boolean) =>
    `inline-flex h-9 items-center gap-2 rounded px-4 text-sm font-medium transition-colors ${active ? 'bg-brand-deep text-white' : 'text-body hover:bg-mist'}`;

  const rowAction = (label: string, onClick: () => void, tone: 'ok' | 'bad' = 'ok', disabled = false, busy = false) => (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || busy}
      className={`relative inline-flex items-center gap-1 rounded border px-2.5 py-1 text-xs font-medium after:absolute after:-inset-1.5 after:content-[''] disabled:opacity-50 ${
        tone === 'ok'
          ? 'border-line-soft text-ink hover:bg-mist'
          : 'border-bad-border/60 text-bad hover:bg-bad-soft'
      }`}
    >
      {busy ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : null}
      {label}
    </button>
  );

  return (
    <AppShell active="admin">
      <div className="mx-auto w-full max-w-[1500px] px-5 py-8 sm:px-8 lg:px-10 lg:py-10 xl:px-14">
        <header className="flex flex-col gap-4 border-b border-line pb-7 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <p className="text-xs font-semibold uppercase text-sub">Account Admin</p>
            <h1 className="mt-2 text-3xl font-semibold text-ink sm:text-4xl">账号管理</h1>
            <p className="mt-3 text-sm text-sub">审批邀请码申请、管理邀请码与冻结账号（仅管理员可见）</p>
          </div>
          <div className="flex flex-wrap items-center gap-1 rounded-md border border-line bg-white p-1">
            <button type="button" onClick={() => setTab('requests')} className={tabCls(tab === 'requests')}>
              <InboxIcon className="size-4" /> 申请审批
            </button>
            <button type="button" onClick={() => setTab('codes')} className={tabCls(tab === 'codes')}>
              <TicketIcon className="size-4" /> 邀请码总览
            </button>
            <button type="button" onClick={() => setTab('frozen')} className={tabCls(tab === 'frozen')}>
              <ShieldCheckIcon className="size-4" /> 冻结账号
            </button>
            <button type="button" onClick={() => setTab('users')} className={tabCls(tab === 'users')}>
              <UserRoundIcon className="size-4" /> 用户管理
            </button>
            <button type="button" onClick={() => setTab('ops')} className={tabCls(tab === 'ops')}>
              <HistoryIcon className="size-4" /> 操作记录
            </button>
          </div>
        </header>

        {notice.text && (
          <div role={notice.kind === 'ok' ? 'status' : 'alert'} className={`mt-4 flex items-center gap-3 rounded-md border px-4 py-3 text-sm ${notice.kind === 'ok' ? 'border-good-border bg-good-soft text-good-deep' : 'border-bad-border bg-bad-soft text-bad'}`}>
            {notice.kind === 'ok' ? <CheckCircle2Icon className="size-4 shrink-0" /> : <XCircleIcon className="size-4 shrink-0" />}
            <span className="break-all">{notice.text}</span>
            <button type="button" onClick={() => setNotice({ text: '', kind: 'ok' })} className="ml-auto text-xs underline opacity-70">关闭</button>
          </div>
        )}

        {tab === 'requests' && (
          <div className="mt-6">
            <div className="flex flex-wrap gap-1 rounded-md border border-line bg-white p-1">
              {(['pending', 'approved', 'rejected'] as const).map((s) => (
                <button key={s} type="button" onClick={() => setReqTab(s)} className={tabCls(reqTab === s)}>
                  {s === 'pending' ? '待审批' : s === 'approved' ? '已通过' : '已拒绝'}
                </button>
              ))}
            </div>

            <div className="mt-4 overflow-x-auto rounded-md border border-line bg-white">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-mist">
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">申请邮箱</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">申请理由</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">提交时间</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">被拒历史</th>
                    {reqTab === 'rejected' && <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">拒绝理由</th>}
                    {reqTab === 'approved' && <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">发码状态</th>}
                    <th className="border-b border-line-soft px-4 py-3 text-right text-xs font-semibold text-sub">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {reqLoading ? (
                    <tr><td colSpan={7} className="px-4 py-12 text-center text-sub"><LoaderCircleIcon className="mx-auto size-5 animate-spin" /></td></tr>
                  ) : requests.length === 0 ? (
                    <tr><td colSpan={7} className="px-4 py-14 text-center text-sub">暂无{reqTab === 'pending' ? '待审批' : reqTab === 'approved' ? '已通过' : '已拒绝'}的申请。</td></tr>
                  ) : requests.map((item) => (
                    <tr key={item.request_id} className="border-b border-line-soft last:border-b-0 hover:bg-soft/60">
                      <td className="px-4 py-3">
                        <span className="flex items-center gap-1.5 font-medium text-ink">
                          <MailIcon className="size-3.5 text-sub" /> {item.email}
                        </span>
                        {item.rejected_count >= 3 && (
                          <span className="mt-1 inline-flex items-center gap-1 rounded bg-bad-soft px-1.5 py-0.5 text-[10px] text-bad">
                            <BanIcon className="size-3" /> 已达拒收上限
                          </span>
                        )}
                      </td>
                      <td className="max-w-xs px-4 py-3">
                        <p className="break-words text-body">{item.note || '--'}</p>
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap text-sub">{fmtTime(item.created_at)}</td>
                      <td className="px-4 py-3 text-sub">{item.rejected_count} 次</td>
                      {reqTab === 'rejected' && (
                        <td className="max-w-xs px-4 py-3"><p className="break-words text-body">{item.reject_reason || '未填理由'}</p></td>
                      )}
                      {reqTab === 'approved' && (
                        <td className="px-4 py-3">
                          {item.code_sent ? (
                            <span className="inline-flex items-center gap-1 rounded bg-good-soft px-2 py-0.5 text-xs text-good"><SendIcon className="size-3" /> 已发送</span>
                          ) : (
                            <span className="inline-flex items-center gap-1 rounded bg-warn-soft px-2 py-0.5 text-xs text-warn">未送达</span>
                          )}
                        </td>
                      )}
                      <td className="px-4 py-3">
                        <div className="flex justify-end gap-1.5">
                          {reqTab === 'pending' && (
                            <>
                              {rowAction('通过', () => askConfirm({
                                title: '通过申请',
                                message: <>确认通过 <span className="font-medium text-ink">{item.email}</span> 的申请？将生成邀请码并发往该邮箱。</>,
                                confirmLabel: '通过并生成邀请码',
                                run: () => handleApprove(item),
                              }), 'ok', busyId === item.request_id, busyId === item.request_id)}
                              <span className="relative inline-flex">
                                <input
                                  type="text"
                                  value={rejectReason[item.request_id] || ''}
                                  onChange={(e) => setRejectReason((prev) => ({ ...prev, [item.request_id]: e.target.value }))}
                                  placeholder="拒绝理由"
                                  maxLength={200}
                                  className="w-52 rounded border border-line-soft px-2 py-1.5 text-xs outline-none focus:border-brand"
                                />
                                {rowAction('拒绝', () => askConfirm({
                                  title: '拒绝申请',
                                  message: <>确认拒绝 <span className="font-medium text-ink">{item.email}</span> 的申请？{rejectReason[item.request_id]?.trim() ? `拒绝理由：${rejectReason[item.request_id]?.trim()}` : '未填理由（不发拒信）'}</>,
                                  danger: true,
                                  confirmLabel: '确认拒绝',
                                  run: () => handleReject(item),
                                }), 'bad', busyId === item.request_id, busyId === item.request_id)}
                              </span>
                            </>
                          )}
                          {reqTab === 'approved' && (
                            <>
                              {rowAction('补发码邮件', () => handleResend(item.request_id), 'ok', busyId === item.request_id, busyId === item.request_id)}
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {tab === 'codes' && (
          <div className="mt-6 space-y-6">
            <div className="flex flex-wrap items-center gap-3 rounded-md border border-line bg-white p-4">
              <select
                value={codeStatus}
                onChange={(e) => setCodeStatus(e.target.value)}
                className="rounded-md border border-line-soft bg-white px-3 py-2 text-sm text-ink outline-none focus:border-brand"
              >
                <option value="all">全部状态</option>
                <option value="active">活跃</option>
                <option value="used">已使用</option>
                <option value="expired">已过期</option>
              </select>
              <button type="button" onClick={() => { setCodeStatus('active'); loadCodes(); }} className="ml-auto inline-flex h-9 items-center gap-2 rounded-md bg-brand-deep px-4 text-sm font-medium text-white hover:bg-brand-hover">
                <RefreshCwIcon className="size-4" /> 刷新
              </button>
            </div>

            {/* 手动生成 */}
            <div className="rounded-md border border-line bg-white p-4">
              <h3 className="text-sm font-semibold text-ink">手动生成邀请码（应急）</h3>
              <p className="mt-1 text-xs text-sub">生成后明文仅展示一次，请立即复制保存。注意：未绑定邮箱的邀请码不校验申请邮箱，仅应急首号注册使用。</p>
              <div className="mt-3 flex flex-wrap items-end gap-3">
                <label className="block">
                  <span className="text-xs text-sub">数量（1–20）</span>
                  <input type="number" min={1} max={20} value={genCount} onChange={(e) => setGenCount(Number(e.target.value))} className="mt-1 w-24 rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand" />
                </label>
                <label className="min-w-56 flex-1">
                  <span className="text-xs text-sub">备注（可选）</span>
                  <input type="text" value={genNote} onChange={(e) => setGenNote(e.target.value)} placeholder="如：2026 春招活动" maxLength={200} className="mt-1 w-full rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand" />
                </label>
                <button type="button" onClick={handleGenerate} disabled={genBusy} className="inline-flex h-9 items-center gap-2 rounded-md bg-brand-deep px-4 text-sm font-medium text-white hover:bg-brand-hover disabled:opacity-50">
                  {genBusy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <TicketIcon className="size-4" />} 生成
                </button>
              </div>
              {genCodes.length > 0 && (
                <div className="mt-3 flex flex-wrap items-center gap-2 rounded-md border border-brand bg-brand-soft px-3 py-2.5">
                  {genCodes.map((c) => (
                    <button key={c} type="button" onClick={() => copyCode(c)} title="点击复制" className="relative inline-flex items-center gap-1 rounded bg-white px-2.5 py-1 font-mono text-sm font-semibold text-brand shadow-sm after:absolute after:-inset-1.5 after:content-[''] hover:shadow">
                      {c} <CopyIcon className="size-3.5" />
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div className="overflow-x-auto rounded-md border border-line bg-white">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-mist">
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">码标识（哈希）</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">状态</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">绑定邮箱</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">生成时间</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">过期时间</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">使用人</th>
                    <th className="border-b border-line-soft px-4 py-3 text-right text-xs font-semibold text-sub">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {codeLoading ? (
                    <tr><td colSpan={7} className="px-4 py-12 text-center text-sub"><LoaderCircleIcon className="mx-auto size-5 animate-spin" /></td></tr>
                  ) : codes.length === 0 ? (
                    <tr><td colSpan={7} className="px-4 py-14 text-center text-sub">暂无邀请码记录。</td></tr>
                  ) : codes.map((item) => (
                    <tr key={item.code_hash} className="border-b border-line-soft last:border-b-0 hover:bg-soft/60">
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center gap-1.5 font-mono text-sm text-ink" title={item.code_hash}>
                          <KeyRoundIcon className="size-3.5 text-sub" /> {shortHash(item.code_hash)}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${
                          item.revoked
                            ? 'bg-mist text-sub'
                            : item.status === 'active'
                              ? 'bg-good-soft text-good'
                              : item.status === 'used'
                                ? 'bg-brand-soft text-brand'
                                : 'bg-warn-soft text-warn'
                        }`}>
                          {item.revoked ? '已作废' : item.status === 'active' ? '活跃' : item.status === 'used' ? '已使用' : '已过期'}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-body">{item.bound_email || '未绑定'}</td>
                      <td className="px-4 py-3 whitespace-nowrap text-sub"><span className="inline-flex items-center gap-1"><CalendarDaysIcon className="size-3" />{fmtTime(item.created_at)}</span></td>
                      <td className="px-4 py-3 whitespace-nowrap text-sub">{fmtTime(item.expires_at)}</td>
                      <td className="px-4 py-3 text-body">{item.used_by_username || '--'}</td>
                      <td className="px-4 py-3">
                        <div className="flex justify-end gap-1.5">
                          {item.status === 'active' && !item.revoked && rowAction('作废', () => askConfirm({
                                title: '作废邀请码',
                                message: <>确认作废邀请码 <span className="font-mono font-medium text-ink">{shortHash(item.code_hash)}</span>？作废后该码立即失效，无法再用于注册。</>,
                                danger: true,
                                confirmLabel: '确认作废',
                                run: () => handleRevoke(item),
                              }), 'bad', busyId === item.code_hash, busyId === item.code_hash)}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {tab === 'frozen' && (
          <div className="mt-6">
            <div className="flex items-center justify-between rounded-md border border-line bg-white p-4">
              <p className="text-sm text-sub">冻结账号无法通过常规登录，可在管理员侧兜底解冻。</p>
              <button type="button" onClick={loadFrozen} className="inline-flex h-9 items-center gap-2 rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist">
                <RefreshCwIcon className="size-4" /> 刷新
              </button>
            </div>
            <div className="mt-4 overflow-x-auto rounded-md border border-line bg-white">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-mist">
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">用户名</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">冻结时间</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">来源</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">原因 / 失败次数</th>
                    <th className="border-b border-line-soft px-4 py-3 text-right text-xs font-semibold text-sub">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {frozenLoading ? (
                    <tr><td colSpan={5} className="px-4 py-12 text-center text-sub"><LoaderCircleIcon className="mx-auto size-5 animate-spin" /></td></tr>
                  ) : frozen.length === 0 ? (
                    <tr><td colSpan={5} className="px-4 py-14 text-center text-sub">当前没有冻结账号。</td></tr>
                  ) : frozen.map((item) => (
                    <tr key={item.username} className="border-b border-line-soft last:border-b-0 hover:bg-soft/60">
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center gap-1.5 font-medium text-ink">
                          <UserRoundIcon className="size-4 text-sub" /> {item.username}
                        </span>
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap text-sub">{fmtTime(item.frozen_at)}</td>
                      <td className="px-4 py-3">
                        {item.frozen_by === 'admin' ? (
                          <span className="inline-flex items-center gap-1 rounded-full border border-bad-border bg-bad-soft px-2 py-0.5 text-xs font-medium text-bad">
                            <SnowflakeIcon className="size-3" /> 管理员冻结
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 rounded-full border border-line-soft bg-mist px-2 py-0.5 text-xs font-medium text-sub">
                            自动冻结
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sub">
                        {item.frozen_reason || (item.window_failures > 0 ? `连续失败 ${item.consecutive_failures} 次 / 窗口 ${item.window_failures} 次` : '--')}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex justify-end">
                          {rowAction('解冻', () => askConfirm({
                                title: '解冻账号',
                                message: <>确认解冻账号 <span className="font-medium text-ink">{item.username}</span>？解冻后可正常登录与使用平台。</>,
                                confirmLabel: '确认解冻',
                                run: () => handleUnfreeze(item.username),
                              }), 'ok', unfreezeBusy === item.username, unfreezeBusy === item.username)}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {tab === 'users' && (
          <div className="mt-6 space-y-4">
            <div className="flex flex-wrap items-center gap-3 rounded-md border border-line bg-white p-4">
              <input
                type="text"
                value={userKeyword}
                onChange={(event) => setUserKeyword(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') loadUsers(); }}
                placeholder="搜索用户名或邮箱…"
                className="w-64 rounded-md border border-line-soft bg-white px-3 py-2 text-sm text-ink outline-none focus:border-brand"
              />
              <button type="button" onClick={loadUsers} className="inline-flex h-9 items-center gap-2 rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist">
                <RefreshCwIcon className="size-4" /> 搜索 / 刷新
              </button>
              <p className="ml-auto text-sm text-sub">共 {userTotal} 个用户</p>
              <button type="button" onClick={openRanking} className="inline-flex h-9 items-center gap-2 rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist">
                <BarChart3Icon className="size-4" /> 使用排行
              </button>
              <button type="button" onClick={() => setShowCreateForm(true)} className="inline-flex h-9 items-center gap-2 rounded-md bg-brand-deep px-4 text-sm font-medium text-white hover:bg-brand-hover">
                <UserRoundIcon className="size-4" /> 新建用户
              </button>
            </div>

            {showCreateForm && (
              <div className="rounded-md border border-line bg-white p-4">
                <h3 className="text-sm font-semibold text-ink">新建用户（管理员代建，绕过邀请码）</h3>
                <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <label className="block">
                    <span className="text-xs text-sub">用户名（必填）</span>
                    <input
                      type="text"
                      value={createForm.username}
                      onChange={(event) => setCreateForm({ ...createForm, username: event.target.value })}
                      placeholder="字母、数字或中文"
                      maxLength={64}
                      className="mt-1 w-full rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand"
                    />
                  </label>
                  <label className="block">
                    <span className="text-xs text-sub">初始密码（至少 8 位）</span>
                    <input
                      type="text"
                      value={createForm.password}
                      onChange={(event) => setCreateForm({ ...createForm, password: event.target.value })}
                      placeholder="含字母和数字"
                      className="mt-1 w-full rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand"
                    />
                  </label>
                  <label className="block">
                    <span className="text-xs text-sub">邮箱（可选，用于密码重置）</span>
                    <input
                      type="email"
                      value={createForm.email}
                      onChange={(event) => setCreateForm({ ...createForm, email: event.target.value })}
                      placeholder="user@example.com"
                      className="mt-1 w-full rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand"
                    />
                  </label>
                  {isSuperAdmin ? (
                    <label className="flex items-end gap-2 pb-2.5">
                      <input
                        type="checkbox"
                        checked={createForm.is_admin}
                        onChange={(event) => setCreateForm({ ...createForm, is_admin: event.target.checked })}
                        className="size-4 accent-check-accent"
                      />
                      <span className="text-sm text-ink">设为管理员</span>
                    </label>
                  ) : (
                    <p className="self-end pb-2 text-xs text-sub">仅超级管理员（admin）可创建管理员账号。</p>
                  )}
                </div>
                <div className="mt-3 flex gap-2">
                  <button type="button" onClick={handleCreateUser} disabled={createBusy} className="inline-flex h-9 items-center gap-2 rounded-md bg-brand-deep px-4 text-sm font-medium text-white hover:bg-brand-hover disabled:opacity-50">
                    {createBusy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <CheckCircle2Icon className="size-4" />} 创建
                  </button>
                  <button type="button" onClick={() => { setShowCreateForm(false); setCreateForm({ username: '', password: '', email: '', is_admin: false }); }} className="inline-flex h-9 items-center rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist">
                    取消
                  </button>
                </div>
              </div>
            )}

            <div className="overflow-x-auto rounded-md border border-line bg-white">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-mist">
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">用户名</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">邮箱</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">角色</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">状态</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">注册时间</th>
                    <th className="border-b border-line-soft px-4 py-3 text-right text-xs font-semibold text-sub">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {userLoading ? (
                    <tr><td colSpan={6} className="px-4 py-12 text-center text-sub"><LoaderCircleIcon className="mx-auto size-5 animate-spin" /></td></tr>
                  ) : users.length === 0 ? (
                    <tr><td colSpan={6} className="px-4 py-14 text-center text-sub">没有匹配的用户。</td></tr>
                  ) : users.map((item) => (
                    <tr key={item.user_id} className="border-b border-line-soft last:border-b-0 hover:bg-soft/60">
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center gap-1.5 font-medium text-ink">
                          <UserRoundIcon className="size-4 text-sub" /> {item.username}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sub">{item.email || <span className="text-sub">未绑定</span>}</td>
                      <td className="px-4 py-3">
                        {item.is_admin ? (
                          <span className="inline-flex items-center gap-1 rounded-full border border-good-border bg-good-soft px-2 py-0.5 text-xs font-medium text-good-ink">
                            <ShieldCheckIcon className="size-3.5" /> 管理员
                          </span>
                        ) : (
                          <span className="inline-flex items-center rounded-full border border-line-soft bg-mist px-2 py-0.5 text-xs font-medium text-sub">
                            普通用户
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {item.frozen ? (
                          <span className="inline-flex items-center gap-1 rounded-full border border-bad-border bg-bad-soft px-2 py-0.5 text-xs font-medium text-bad">
                            <BanIcon className="size-3.5" /> 已冻结
                          </span>
                        ) : (
                          <span className="text-xs text-good-ink">正常</span>
                        )}
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap text-sub">{fmtTime(item.created_at)}</td>
                      <td className="px-4 py-3">
                        <div className="flex justify-end gap-1.5">
                          {rowAction('使用统计', () => openUsage(item), 'ok', userBusy === item.username, userBusy === item.username)}
                          {isSuperAdmin && rowAction('删除', () => handleDeleteUser(item), 'bad')}
                          <button
                            type="button"
                            aria-label={`${item.username} 更多操作`}
                            onClick={(event) => toggleRowMenu(event, item.username)}
                            onKeyDown={(e) => {
                              if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                                e.preventDefault();
                                toggleRowMenu(e as unknown as React.MouseEvent<HTMLButtonElement>, item.username);
                              }
                            }}
                            className="relative inline-flex size-6 items-center justify-center rounded border border-line-soft text-sub after:absolute after:-inset-1.5 after:content-[''] hover:bg-mist"
                          >
                            <MoreHorizontalIcon className="size-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {isSuperAdmin && (
              <div className="rounded-md border border-warn-border bg-warn-panel p-4">
                <div className="flex items-center gap-2">
                  <RefreshCwIcon className="size-4 text-warn-ink" />
                  <h3 className="text-sm font-semibold text-warn-ink-deep">已删除账号（90 天内可恢复）</h3>
                  {deletedLoading && <LoaderCircleIcon className="size-4 animate-spin text-warn-ink" />}
                  <button type="button" onClick={loadDeletedUsers} className="ml-auto text-xs font-medium text-brand hover:underline">刷新</button>
                </div>
                {deletedUsers.length === 0 ? (
                  <p className="mt-2 text-xs text-warn-ink">暂无待恢复的已删除账号。</p>
                ) : (
                  <ul className="mt-3 divide-y divide-warn-divider">
                    {deletedUsers.map((item) => (
                      <li key={item.user_id} className="flex flex-wrap items-center gap-3 py-2.5">
                        <div className="min-w-0 flex-1">
                          <p className="text-sm font-medium text-warn-strong">{item.username}</p>
                          <p className="mt-0.5 text-xs text-warn-ink">
                            邮箱：{item.email || '未绑定'} · 删除时间：{fmtTime(item.deleted_at)} · 操作者：{item.deleted_by || 'admin'}
                          </p>
                        </div>
                        <button
                          type="button"
                          disabled={restoreBusy === item.username}
                          onClick={() => handleRestoreUser(item.username)}
                          className="relative inline-flex h-8 items-center gap-1.5 rounded-md border border-warn-btn-border bg-white px-3 text-xs font-medium text-warn-ink-deep after:absolute after:-inset-1.5 after:content-[''] hover:bg-warn-btn-hover disabled:opacity-50"
                        >
                          {restoreBusy === item.username ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <RefreshCwIcon className="size-3.5" />} 恢复账号
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        )}

        {tab === 'ops' && (
          <div className="mt-6 space-y-4">
            <div className="flex flex-wrap items-center gap-3 rounded-md border border-line bg-white p-4">
              <input
                type="text"
                value={opsKeyword}
                onChange={(event) => setOpsKeyword(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') loadOps(1); }}
                placeholder="搜索操作者或目标用户名…"
                className="w-64 rounded-md border border-line-soft bg-white px-3 py-2 text-sm text-ink outline-none focus:border-brand"
              />
              <select
                value={opsFilter}
                onChange={(event) => setOpsFilter(event.target.value)}
                className="h-9 rounded-md border border-line-soft bg-white px-2 text-sm text-ink outline-none focus:border-brand"
              >
                <option value="">全部类型</option>
                {Object.entries({
                  user_create: '创建用户',
                  user_delete: '删除用户',
                  user_restore: '恢复账号',
                  user_freeze: '冻结用户',
                  user_unfreeze: '解冻用户',
                  admin_grant: '授予管理员',
                  admin_revoke: '取消管理员',
                  email_update: '修改邮箱',
                  pwd_reset: '重置密码',
                }).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
              <button type="button" onClick={() => loadOps(1)} className="inline-flex h-9 items-center gap-2 rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist">
                <RefreshCwIcon className="size-4" /> 搜索 / 刷新
              </button>
              <div className="ml-auto flex flex-wrap items-center gap-2">
                <p className="text-sm text-sub">共 {opsTotal} 条操作记录</p>
                <button
                  type="button"
                  disabled={exportBusy}
                  onClick={() => handleExportOps('csv')}
                  title="导出当前筛选结果为 CSV（Excel 可直接打开）"
                  className="inline-flex h-9 items-center gap-1.5 rounded-md border border-line-soft bg-white px-3 text-sm font-medium text-ink hover:bg-mist disabled:opacity-50"
                >
                  {exportBusy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <ArrowDownToLineIcon className="size-4" />} CSV
                </button>
                <button
                  type="button"
                  disabled={exportBusy}
                  onClick={() => handleExportOps('json')}
                  title="导出当前筛选结果为 JSON"
                  className="inline-flex h-9 items-center gap-1.5 rounded-md border border-line-soft bg-white px-3 text-sm font-medium text-ink hover:bg-mist disabled:opacity-50"
                >
                  {exportBusy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <FileJsonIcon className="size-4" />} JSON
                </button>
              </div>
            </div>
            <p className="text-xs text-sub">操作记录仅保留最近 180 天，使用统计保留 365 天（自动清理，无需手动维护）。</p>

            <div className="overflow-x-auto rounded-md border border-line bg-white">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-mist">
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">时间</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">操作类型</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">操作者</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">目标用户</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">详情</th>
                  </tr>
                </thead>
                <tbody>
                  {opsLoading ? (
                    <tr><td colSpan={5} className="px-4 py-12 text-center text-sub"><LoaderCircleIcon className="mx-auto size-5 animate-spin" /></td></tr>
                  ) : ops.length === 0 ? (
                    <tr><td colSpan={5} className="px-4 py-14 text-center text-sub">暂无操作记录。</td></tr>
                  ) : ops.map((item, index) => (                    <tr key={`${item.created_at}-${index}`} className="border-b border-line-soft last:border-b-0 hover:bg-soft/60">
                      <td className="px-4 py-3 whitespace-nowrap text-sub">{fmtTime(item.created_at)}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-medium ${
                          item.op === 'user_delete' || item.op === 'user_freeze'
                            ? 'bg-bad-soft text-bad border border-bad-border/60'
                            : item.op === 'user_unfreeze'
                              ? 'bg-good-soft text-good-ink border border-good-border/60'
                              : item.op === 'admin_grant' || item.op === 'admin_revoke'
                                ? 'bg-brand-soft text-brand border border-brand-soft'
                                : 'bg-mist text-body border border-line-soft'
                        }`}>
                          {item.op === 'user_delete' ? <BanIcon className="size-3" />
                            : item.op === 'user_freeze' ? <SnowflakeIcon className="size-3" />
                            : item.op === 'user_unfreeze' ? <RefreshCwIcon className="size-3" />
                            : item.op === 'admin_grant' ? <ShieldCheckIcon className="size-3" />
                            : item.op === 'admin_revoke' ? <ShieldCheckIcon className="size-3" />
                            : item.op === 'pwd_reset' ? <KeyRoundIcon className="size-3" />
                            : item.op === 'email_update' ? <MailIcon className="size-3" />
                            : <HistoryIcon className="size-3" />}
                          {{ user_create: '创建用户', user_delete: '删除用户', user_restore: '恢复账号', user_freeze: '冻结用户', user_unfreeze: '解冻用户', admin_grant: '授予管理员', admin_revoke: '取消管理员', email_update: '修改邮箱', pwd_reset: '重置密码' }[item.op] || item.op}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-body">{item.operator_name || '--'}</td>
                      <td className="px-4 py-3 text-body">{item.target_username || '--'}</td>
                      <td className="px-4 py-3 text-sub">{item.detail || '--'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {/* 操作记录分页 */}
              {opsTotal > OPS_PAGE_SIZE && (
                <div className="flex items-center justify-between border-t border-line-soft bg-mist/40 px-4 py-2.5 text-sm text-sub">
                  <span className="text-xs">第 {opsPage} 页 · 共 {Math.max(1, Math.ceil(opsTotal / OPS_PAGE_SIZE))} 页 / {opsTotal} 条</span>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={opsPage <= 1 || opsLoading}
                      onClick={() => loadOps(opsPage - 1)}
                      className="relative inline-flex h-8 items-center rounded border border-line-soft bg-white px-3 text-xs font-medium text-ink after:absolute after:-inset-1.5 after:content-[''] hover:bg-mist disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      上一页
                    </button>
                    <button
                      type="button"
                      disabled={opsPage >= Math.ceil(opsTotal / OPS_PAGE_SIZE) || opsLoading}
                      onClick={() => loadOps(opsPage + 1)}
                      className="relative inline-flex h-8 items-center rounded border border-line-soft bg-white px-3 text-xs font-medium text-ink after:absolute after:-inset-1.5 after:content-[''] hover:bg-mist disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      下一页
                    </button>
                    <button
                      type="button"
                      disabled={opsLoading}
                      onClick={() => loadOps(1)}
                      className="relative inline-flex h-8 items-center rounded border border-line-soft bg-white px-3 text-xs font-medium text-ink after:absolute after:-inset-1.5 after:content-[''] hover:bg-mist disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      回到首页
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* 行内「…」菜单浮层 */}
        {rowMenu && (
          <div
            ref={rowMenuRef}
            role="menu"
            aria-label="用户更多操作"
            onKeyDown={onRowMenuKeyDown}
            className="fixed z-[60] flex min-w-36 flex-col gap-0.5 rounded-md border border-line bg-white p-1.5 shadow-xl"
            style={{ right: Math.max(8, window.innerWidth - rowMenu.x), top: Math.min(rowMenu.y, window.innerHeight - 220) }}
          >
            {(() => {
              const item = users.find((u) => u.username === rowMenu.username);
              if (!item) return null;
              const close = () => { setRowMenu(null); rowMenuTriggerRef.current?.focus(); };
              const menuAction = (fn: () => void) => () => { close(); fn(); };
              return (
                <>
                  {item.frozen && (
                    <button type="button" role="menuitem" onClick={menuAction(() => askConfirm({
                      title: '解冻账号',
                      message: <>确认解冻账号 <span className="font-medium text-ink">{item.username}</span>？解冻后可正常登录与使用平台。</>,
                      confirmLabel: '确认解冻',
                      run: () => handleUnfreezeUser(item),
                    }))} className="flex items-center gap-2 rounded px-3 py-2 text-left text-sm font-medium text-ink hover:bg-mist">
                      <RefreshCwIcon className="size-4 text-good-ink" /> 解冻账号
                    </button>
                  )}
                  {!item.frozen && isSuperAdmin && (
                    <button type="button" role="menuitem" onClick={menuAction(() => openFreeze(item))} className="flex items-center gap-2 rounded px-3 py-2 text-left text-sm font-medium text-bad hover:bg-bad-soft">
                      <SnowflakeIcon className="size-4" /> 冻结账号
                    </button>
                  )}
                  {isSuperAdmin && (
                    <button type="button" role="menuitem" onClick={menuAction(() => askConfirm({
                      title: item.is_admin ? '取消管理员权限' : '设为管理员',
                      message: <>确认{item.is_admin ? '取消' : '授予'} <span className="font-medium text-ink">{item.username}</span> 的管理员权限？{item.is_admin ? '取消后其将无法访问管理后台。' : '授予后可访问邀请审批、用户管理等后台功能。'}</>,
                      danger: item.is_admin,
                      confirmLabel: item.is_admin ? '取消管理员' : '设为管理员',
                      run: () => handleToggleAdmin(item),
                    }))} className="flex items-center gap-2 rounded px-3 py-2 text-left text-sm font-medium text-ink hover:bg-mist">
                      <ShieldCheckIcon className="size-4 text-sub" /> {item.is_admin ? '取消管理员' : '设为管理员'}
                    </button>
                  )}
                  <button type="button" role="menuitem" onClick={menuAction(() => openEmail(item))} className="flex items-center gap-2 rounded px-3 py-2 text-left text-sm font-medium text-ink hover:bg-mist">
                    <MailIcon className="size-4 text-sub" /> 修改邮箱
                  </button>
                  <button type="button" role="menuitem" onClick={menuAction(() => askConfirm({
                      title: '重置密码',
                      message: <>确认为 <span className="font-medium text-ink">{item.username}</span> 重置密码？重置后该用户旧会话将全部失效，需使用新密码重新登录。</>,
                      danger: true,
                      confirmLabel: '确认重置',
                      run: () => handleResetUserPassword(item),
                    }))} className="flex items-center gap-2 rounded px-3 py-2 text-left text-sm font-medium text-ink hover:bg-mist">
                    <KeyRoundIcon className="size-4 text-sub" /> 重置密码
                  </button>
                </>
              );
            })()}
          </div>
        )}

        {/* 使用统计弹窗 */}
        {usageUser && (
          <AdminModal
            title={<><UserRoundIcon className="size-5 text-sub" /> 使用统计：{usageUser.username}</>}
            onClose={() => setUsageUser(null)}
            maxWidth="max-w-2xl"
          >
            <p className="text-xs text-sub">记录登录与 HR 筛选分析的使用次数（埋点自本版本生效，历史数据从零累计）</p>

            <div className="mt-4 flex flex-wrap items-center gap-2">
              {(['day', 'month', 'year'] as const).map((g) => (
                <button
                  key={g}
                  type="button"
                  onClick={() => switchUsageGran(g)}
                  className={`relative inline-flex h-8 items-center gap-1.5 rounded px-3 text-sm font-medium after:absolute after:-inset-1.5 after:content-[''] transition-colors ${
                    usageGran === g ? 'bg-brand-deep text-white' : 'border border-line-soft bg-white text-body hover:bg-mist'
                  }`}
                >
                  {g === 'day' ? '按日' : g === 'month' ? '按月' : '按年'}
                </button>
              ))}
              <div className="ml-auto flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleExportUsageCsv}
                  disabled={!usageData}
                  title="导出当前区间使用统计为 CSV（Excel 可直接打开）"
                  className="relative inline-flex h-8 items-center gap-1.5 rounded-md border border-line-soft bg-white px-3 text-xs font-medium text-ink after:absolute after:-inset-1.5 after:content-[''] hover:bg-mist disabled:opacity-40"
                >
                  <ArrowDownToLineIcon className="size-3.5" /> 导出 CSV
                </button>
                <span className="inline-flex items-center gap-1"><span className="inline-block size-2.5 rounded-sm bg-brand" /> 登录</span>
                <span className="inline-flex items-center gap-1"><span className="inline-block size-2.5 rounded-sm bg-good-ink" /> 分析</span>
              </div>
            </div>

            <div className="mt-4">
              {usageLoading || !usageData ? (
                <div className="flex h-64 items-center justify-center text-sm text-sub">
                  <LoaderCircleIcon className="mr-2 size-5 animate-spin" /> 正在加载
                </div>
              ) : usageData.labels.length === 0 ? (
                <div className="flex h-64 items-center justify-center text-sm text-sub">暂无数据。</div>
              ) : (
                <UsageBarChart data={usageData} granularity={usageGran} />
              )}
            </div>

            {/* 汇总卡片（summary 字段） */}
            {usageData?.summary && (
              <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                <div className="rounded-md border border-line-soft bg-white p-3">
                  <p className="text-xs text-sub">区间总使用</p>
                  <p className="mt-1 text-lg font-semibold text-ink">{usageData.summary.total} 次</p>
                </div>
                <div className="rounded-md border border-line-soft bg-white p-3">
                  <p className="text-xs text-sub">登录</p>
                  <p className="mt-1 text-lg font-semibold text-ink">{usageData.summary.login_total} 次</p>
                </div>
                <div className="rounded-md border border-line-soft bg-white p-3">
                  <p className="text-xs text-sub">分析</p>
                  <p className="mt-1 text-lg font-semibold text-ink">{usageData.summary.analysis_total} 次</p>
                </div>
                <div className="rounded-md border border-line-soft bg-white p-3">
                  <p className="text-xs text-sub">日均 / 月均 / 年均</p>
                  <p className="mt-1 text-lg font-semibold text-ink">{usageData.summary.avg_per_bucket}</p>
                </div>
                <div className="rounded-md border border-line-soft bg-white p-3">
                  <p className="text-xs text-sub">峰值（{usageData.summary.peak_label}）</p>
                  <p className="mt-1 text-lg font-semibold text-ink">{usageData.summary.peak_total} 次</p>
                </div>
                <div className="rounded-md border border-line-soft bg-white p-3">
                  <p className="text-xs text-sub">活跃 {usageGran === 'day' ? '天' : usageGran === 'month' ? '月' : '年'}</p>
                  <p className="mt-1 text-lg font-semibold text-ink">{usageData.summary.active_buckets}</p>
                </div>
              </div>
            )}

            <div className="mt-4 rounded-md border border-line-soft bg-mist p-3 text-xs text-sub">
              {(() => {
                const total = usageData?.series?.total?.reduce((a, b) => a + b, 0) ?? 0;
                const login = usageData?.series?.login?.reduce((a, b) => a + b, 0) ?? 0;
                const analysis = usageData?.series?.analysis?.reduce((a, b) => a + b, 0) ?? 0;
                return `统计区间合计：使用 ${total} 次（登录 ${login} 次，分析 ${analysis} 次）。${usageGran === 'day' ? '近 30 天' : usageGran === 'month' ? '近 12 个月' : '近 5 年'}`;
              })()}
            </div>
          </AdminModal>
        )}

        {/* 冻结弹窗 */}
        {freezeTarget && (
          <AdminModal
            title={<><SnowflakeIcon className="size-5 text-bad" /> 冻结账号：{freezeTarget.username}</>}
            onClose={() => setFreezeTarget(null)}
            maxWidth="max-w-md"
            footer={
              <>
                <button type="button" onClick={handleFreeze} disabled={freezeBusy} className="inline-flex h-9 items-center gap-2 rounded-md bg-bad px-4 text-sm font-medium text-white hover:bg-bad-hover disabled:opacity-50">
                  {freezeBusy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SnowflakeIcon className="size-4" />} 确认冻结
                </button>
                <button type="button" onClick={() => setFreezeTarget(null)} className="inline-flex h-9 items-center rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist">
                  取消
                </button>
              </>
            }
          >
            <p className="text-sm text-sub">冻结后该用户将无法登录、无法重置密码，也无法自助解冻，需联系管理员处理。请填写冻结原因（必填，将展示给用户并记录审计）。</p>
            <textarea
              value={freezeReason}
              onChange={(event) => setFreezeReason(event.target.value)}
              placeholder="例如：频繁调用分析接口，疑似异常消耗 token"
              rows={3}
              className="mt-3 w-full rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand"
            />
          </AdminModal>
        )}

        {/* 改邮箱弹窗 */}
        {emailTarget && (
          <AdminModal
            title={<><MailIcon className="size-5 text-sub" /> 修改邮箱：{emailTarget.username}</>}
            onClose={() => setEmailTarget(null)}
            maxWidth="max-w-md"
            footer={
              <>
                <button type="button" onClick={handleChangeEmail} disabled={emailBusy} className="inline-flex h-9 items-center gap-2 rounded-md bg-brand-deep px-4 text-sm font-medium text-white hover:bg-brand-hover disabled:opacity-50">
                  {emailBusy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SaveIcon className="size-4" />} 保存
                </button>
                <button type="button" onClick={() => setEmailTarget(null)} className="inline-flex h-9 items-center rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist">
                  取消
                </button>
              </>
            }
          >
            <p className="text-sm text-sub">
              当前绑定邮箱：<span className="text-ink">{emailTarget.email || '未绑定'}</span>（用于密码重置与自助解冻）
            </p>
            <input
              type="email"
              value={newEmail}
              onChange={(event) => setNewEmail(event.target.value)}
              placeholder="新邮箱 user@example.com"
              className="mt-3 w-full rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand"
            />
          </AdminModal>
        )}

        {/* 删除账号弹窗（仅超级管理员，需输入当前管理员密码确认） */}
        {deleteTarget && (
          <AdminModal
            title={<><XCircleIcon className="size-5 text-bad" /> 删除账号：{deleteTarget.username}</>}
            onClose={() => setDeleteTarget(null)}
            maxWidth="max-w-md"
            footer={
              <>
                <button type="button" onClick={confirmDeleteUser} disabled={deleteBusy} className="inline-flex h-9 items-center gap-2 rounded-md bg-bad px-4 text-sm font-medium text-white hover:bg-bad-hover disabled:opacity-50">
                  {deleteBusy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <XCircleIcon className="size-4" />} 确认删除
                </button>
                <button type="button" onClick={() => setDeleteTarget(null)} className="inline-flex h-9 items-center rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist">
                  取消
                </button>
              </>
            }
          >
            <p className="text-sm text-sub">
              将删除用户 <span className="font-semibold text-ink">{deleteTarget.username}</span>（{deleteTarget.email || '未绑定邮箱'}）。
              删除后 90 天内可在「已删除账号」中恢复；超过期限无法恢复。该操作仅超级管理员可执行。
            </p>
            <label className="mt-4 block">
              <span className="text-xs font-medium text-sub">管理员密码（验证身份）</span>
              <input
                type="password"
                value={deletePassword}
                onChange={(event) => setDeletePassword(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') confirmDeleteUser(); }}
                placeholder="输入你的登录密码以确认删除"
                autoComplete="current-password"
                className="mt-1.5 w-full rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand"
              />
            </label>
          </AdminModal>
        )}

        {/* 使用排行弹窗 */}
        {rankOpen && (
          <AdminModal
            title={<><BarChart3Icon className="size-5 text-sub" /> 用户使用排行（累计）</>}
            onClose={() => setRankOpen(false)}
            maxWidth="max-w-2xl"
          >
            <p className="text-xs text-sub">按登录 + 分析总使用量降序排列，帮助发现异常高消耗账号。埋点自本版本生效，历史数据从零累计。</p>
              <div className="mt-4 overflow-x-auto rounded-md border border-line">
                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="bg-mist">
                      <th className="border-b border-line-soft px-4 py-2.5 text-left text-xs font-semibold text-sub">排名</th>
                      <th className="border-b border-line-soft px-4 py-2.5 text-left text-xs font-semibold text-sub">用户名</th>
                      <th className="border-b border-line-soft px-4 py-2.5 text-right text-xs font-semibold text-sub">登录</th>
                      <th className="border-b border-line-soft px-4 py-2.5 text-right text-xs font-semibold text-sub">分析</th>
                      <th className="border-b border-line-soft px-4 py-2.5 text-right text-xs font-semibold text-sub">合计</th>
                      <th className="border-b border-line-soft px-4 py-2.5 text-left text-xs font-semibold text-sub">最近使用</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rankLoading ? (
                      <tr><td colSpan={6} className="px-4 py-10 text-center text-sub"><LoaderCircleIcon className="mx-auto size-5 animate-spin" /></td></tr>
                    ) : rankData.length === 0 ? (
                      <tr><td colSpan={6} className="px-4 py-10 text-center text-sub">暂无使用数据。</td></tr>
                    ) : rankData.map((item, index) => (
                      <tr key={item.user_id} className="border-b border-line-soft last:border-b-0 hover:bg-soft/60">
                        <td className="px-4 py-2.5 text-sub">{index + 1}</td>
                        <td className="px-4 py-2.5 font-medium text-ink">{item.username || item.user_id.slice(0, 8)}</td>
                        <td className="px-4 py-2.5 text-right text-sub">{item.login}</td>
                        <td className="px-4 py-2.5 text-right text-sub">{item.analysis}</td>
                        <td className="px-4 py-2.5 text-right font-semibold text-ink">{item.total}</td>
                        <td className="px-4 py-2.5 text-sub">{item.last_usage || '--'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
          </AdminModal>
        )}
      </div>

      {/* 高风险操作确认（P1-2：替代原生 window.confirm） */}
      <ConfirmDialog
        open={pendingConfirm !== null}
        danger={pendingConfirm?.danger}
        title={pendingConfirm?.title}
        message={pendingConfirm?.message}
        confirmLabel={pendingConfirm?.confirmLabel}
        busy={busyId !== null || userBusy !== null || unfreezeBusy !== null}
        onConfirm={() => {
          const action = pendingConfirm;
          setPendingConfirm(null);
          action?.run();
        }}
        onCancel={() => setPendingConfirm(null)}
      />
    </AppShell>
  );
}

/** 轻量 SVG 柱状图：双系列（登录/分析），无第三方图表依赖 */
function UsageBarChart({ data, granularity }: { data: UserUsageData; granularity: UsageGranularity }) {
  const labels = data.labels;
  const login = data.series.login;
  const analysis = data.series.analysis;
  const max = Math.max(1, ...login, ...analysis);
  const chartWidth = Math.max(560, labels.length * 36);
  const chartHeight = 220;
  const padBottom = 28;
  const plotHeight = chartHeight - padBottom - 24;
  const band = chartWidth / labels.length;
  const barW = Math.max(4, band * 0.24);
  const gridLines = 4;

  return (
    <div className="overflow-x-auto">
      <svg width={chartWidth} height={chartHeight} viewBox={`0 0 ${chartWidth} ${chartHeight}`} role="img" aria-label="使用次数柱状图">
        {/* 网格线 + Y 轴刻度 */}
        {Array.from({ length: gridLines + 1 }).map((_, i) => {
          const y = 12 + (plotHeight / gridLines) * i;
          const val = Math.round(max * (1 - i / gridLines));
          return (
            <g key={i}>
              <line x1={0} x2={chartWidth} y1={y} y2={y} stroke="var(--color-line-soft)" strokeWidth={1} />
              <text x={0} y={y - 4} fontSize={10} fill="var(--color-sub)">{val}</text>
            </g>
          );
        })}
        {/* 柱体：登录（品牌色）+ 分析（绿） */}
        {labels.map((label, i) => {
          const cx = band * i + band / 2;
          const hLogin = (login[i] / max) * plotHeight;
          const hAnalysis = (analysis[i] / max) * plotHeight;
          const baseY = 12 + plotHeight;
          return (
            <g key={String(label)}>
              <title>{`${String(label)}：登录 ${login[i]} 次 / 分析 ${analysis[i]} 次`}</title>
              <rect x={cx - barW - 1.5} y={baseY - hLogin} width={barW} height={Math.max(0, hLogin)} rx={2} fill="var(--color-brand-deep)" />
              <rect x={cx + 1.5} y={baseY - hAnalysis} width={barW} height={Math.max(0, hAnalysis)} rx={2} fill="var(--color-good-ink)" />
              <text x={cx} y={chartHeight - 8} fontSize={9} fill="var(--color-sub)" textAnchor="middle">{fmtUsageLabel(String(label), granularity)}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
