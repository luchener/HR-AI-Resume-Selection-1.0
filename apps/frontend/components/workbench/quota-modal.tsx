'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangleIcon } from 'lucide-react';
import { useDialogBehavior } from './dialog-behavior';

const ANIM_OUT = 140;

/**
 * 次数用尽弹窗（受控组件，复用公告弹窗动效）。
 * - 点遮罩不关闭（需显式操作）；Esc / ✕ / 我知道了 关闭；200ms/140ms 动画。
 * - 会话内去重由调用方（analysis-workbench）按「触发类型 + 日期」管理。
 */
export interface QuotaModalData {
  variant: 'exhausted' | 'insufficient' | 'disabled';
  title: string;
  detail?: string;
  limit?: number;
  used?: number;
  resets_at?: string;
  need?: number;
  remaining?: number;
}

export function QuotaModal({
  open,
  data,
  onClose,
  onRefresh,
}: {
  open: boolean;
  data: QuotaModalData | null;
  onClose: () => void;
  /** 重新拉取配额状态（管理员开启后无需刷新页面） */
  onRefresh?: () => void;
}) {
  const [leaving, setLeaving] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const close = useCallback(() => {
    if (!open || leaving) return;
    setLeaving(true);
    timerRef.current = setTimeout(() => {
      setLeaving(false);
      onClose();
    }, ANIM_OUT);
  }, [open, leaving, onClose]);

  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);

  // 外部变更 data 后重置退出态
  useEffect(() => { if (open) setLeaving(false); }, [open, data]);

  useDialogBehavior({
    open: open && !leaving,
    onClose: close,
    panelRef,
    autoFocus: true,
  });

  if (!open || !data) return null;

  const isDanger = data.variant === 'exhausted' || data.variant === 'disabled';

  return (
    <div className="announcement-overlay fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="presentation">
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={data.title}
        className={`announcement-panel w-full max-w-md rounded-md border border-line bg-white p-6 shadow-2xl ${leaving ? 'announcement-leave' : 'announcement-enter'}`}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className={`flex size-10 shrink-0 items-center justify-center rounded-md ${isDanger ? 'bg-bad-soft text-bad' : 'bg-warn-soft text-warn'}`}>
              <AlertTriangleIcon className="size-5" />
            </span>
            <div>
              <h3 className="text-base font-semibold text-ink">{data.title}</h3>
              <p className="mt-0.5 text-xs text-sub">{data.variant === 'disabled' ? '该限制与配额开关无关，需管理员开启' : data.resets_at ? `恢复：${data.resets_at}` : '联系管理员可调整配额'}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={close}
            aria-label="关闭"
            className="relative inline-flex size-8 shrink-0 items-center justify-center rounded-md text-sub after:absolute after:-inset-1.5 after:content-[''] transition-colors hover:bg-mist"
          >
            ✕
          </button>
        </div>

        <div className="mt-4 space-y-2 text-sm text-body">
          {data.limit !== undefined && data.used !== undefined && (
            <div className="flex gap-2">
              <span className="w-20 shrink-0 text-sub">今日限额</span>
              <span className="font-medium text-ink">{data.limit} 次</span>
            </div>
          )}
          {data.used !== undefined && (
            <div className="flex gap-2">
              <span className="w-20 shrink-0 text-sub">已用次数</span>
              <span className="font-medium text-ink">{data.used} 次</span>
            </div>
          )}
          {data.variant === 'insufficient' && data.need !== undefined && data.remaining !== undefined && (
            <div className="flex gap-2">
              <span className="w-20 shrink-0 text-sub">本次需要</span>
              <span className="font-medium text-bad">{data.need} 次（今日剩余 {data.remaining} 次）</span>
            </div>
          )}
          {data.detail && <p className="pt-1 text-sub">{data.detail}</p>}
        </div>

        <div className="mt-6 flex justify-end gap-2.5">
          {onRefresh && (
            <button
              type="button"
              onClick={onRefresh}
              className="rounded-md border border-line px-4 py-2 text-sm text-body transition-colors hover:bg-mist"
            >
              刷新状态
            </button>
          )}
          <button
            type="button"
            onClick={close}
            className={`rounded-md px-4 py-2 text-sm font-medium text-white ${isDanger ? 'bg-bad hover:bg-bad/90' : 'bg-brand-deep hover:bg-brand-deep/90'}`}
          >
            我知道了
          </button>
        </div>
      </div>
    </div>
  );
}
