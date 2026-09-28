'use client';

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { usePathname } from 'next/navigation';
import { BellRingIcon, LoaderCircleIcon } from 'lucide-react';
import { useAuth } from './auth-context';
import { useDialogBehavior } from './dialog-behavior';
import { dismissAnnouncement, fetchUserAnnouncements, type UserAnnouncement } from '@/lib/api/notifications';

const POLL_INTERVAL_MS = 5 * 60 * 1000; // 5 分钟轮询
const ANIM_OUT = 140;

/**
 * 公告弹窗（全员）：登录后自动弹出，多条排队逐个展示。
 * - 投放期由后端过滤（start_at/end_at 惰性判定）+ 用户已关闭记录。
 * - 点遮罩不关闭（需显式操作）；Esc / ✕ / 我知道了 关闭并持久化。
 * - 入场 200ms / 出场 140ms；prefers-reduced-motion 降级为无动画。
 */
export function AnnouncementProvider({ children }: { children: ReactNode }) {
  const { isHydrated, isAuthenticated } = useAuth();
  const pathname = usePathname();
  const [queue, setQueue] = useState<UserAnnouncement[]>([]);
  const [leaving, setLeaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const current = queue[0] ?? null;
  const visible = Boolean(current) && !leaving;

  const refresh = useCallback(async () => {
    if (!isHydrated || !isAuthenticated) return;
    try {
      const items = await fetchUserAnnouncements();
      setQueue((prev) => {
        const ids = new Set(prev.map((a) => a.announcement_id));
        const fresh = items.filter((a) => !ids.has(a.announcement_id));
        // 已在展示中的公告不被刷新覆盖；新公告追加到队列尾
        return [...prev, ...fresh];
      });
    } catch { /* 网络/未登录等静默，下次轮询重试 */ }
  }, [isHydrated, isAuthenticated]);

  // 挂载 / 路由切换 / 窗口聚焦 触发刷新；每 5 分钟轮询
  // 路由切换时也重新拉取（避免在公告投放期内切换页面错过弹窗）
  useEffect(() => {
    refresh();
  }, [refresh, pathname]);

  useEffect(() => {
    const onFocus = () => refresh();
    window.addEventListener('focus', onFocus);
    const interval = window.setInterval(refresh, POLL_INTERVAL_MS);
    return () => {
      window.removeEventListener('focus', onFocus);
      window.clearInterval(interval);
    };
  }, [refresh]);

  // 登出后清空队列
  useEffect(() => {
    if (isHydrated && !isAuthenticated) setQueue([]);
  }, [isHydrated, isAuthenticated]);

  const closeCurrent = useCallback(() => {
    if (!current || leaving || busy) return;
    setLeaving(true);
    timerRef.current = setTimeout(async () => {
      const id = current.announcement_id;
      setBusy(true);
      try {
        await dismissAnnouncement(id);
      } catch { /* 关闭失败不阻断展示后续公告 */ }
      setQueue((prev) => prev.filter((a) => a.announcement_id !== id));
      setBusy(false);
      setLeaving(false);
    }, ANIM_OUT);
  }, [current, leaving, busy]);

  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);

  useDialogBehavior({
    open: visible,
    onClose: closeCurrent,
    panelRef,
    autoFocus: true,
  });

  // 退出动画完成前保持展示
  if (!visible || !current) return <>{children}</>;

  return (
    <>
      {children}
      <div className="announcement-overlay fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="presentation">
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-label={current.title}
          className={`announcement-panel w-full max-w-md rounded-md border border-line bg-white p-6 shadow-2xl ${leaving ? 'announcement-leave' : 'announcement-enter'}`}
        >
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-3">
              <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-brand-deep text-white">
                <BellRingIcon className="size-5" />
              </span>
              <div>
                <h3 className="text-base font-semibold text-ink">{current.title}</h3>
                <p className="mt-0.5 text-xs text-sub">系统公告 · {new Date(current.created_at || Date.now()).toLocaleDateString('zh-CN')}</p>
              </div>
            </div>
            <button
              type="button"
              onClick={closeCurrent}
              aria-label="关闭"
              disabled={busy}
              className="relative inline-flex size-8 shrink-0 items-center justify-center rounded-md text-sub after:absolute after:-inset-1.5 after:content-[''] transition-colors hover:bg-mist disabled:opacity-50"
            >
              ✕
            </button>
          </div>
          <div className="mt-4 max-h-[52vh] overflow-y-auto whitespace-pre-line rounded-md border border-line-soft bg-soft/60 p-4 text-sm leading-7 text-body">
            {current.content}
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <button
              type="button"
              onClick={closeCurrent}
              disabled={busy}
              className="inline-flex h-10 items-center gap-2 rounded-md bg-brand-deep px-6 text-sm font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-60"
            >
              {busy ? <LoaderCircleIcon className="size-4 animate-spin" /> : null}
              我知道了
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
