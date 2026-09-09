'use client';

import { useRef, type ReactNode } from 'react';
import { AlertTriangleIcon, LoaderCircleIcon } from 'lucide-react';
import { useDialogBehavior } from './dialog-behavior';

/**
 * 产品级确认弹窗：替代原生 window.confirm。
 * - 危险/中性两种确认语色；后果陈述放标题下；确认按钮聚焦安全（默认聚焦取消）。
 * - 统一键盘行为：Esc 关闭、背景滚动锁定、焦点恢复、Tab 陷阱。
 */
export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = '确认',
  cancelLabel = '取消',
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: ReactNode;
  /** 后果/说明文案（ReactNode 便于插入用户名、理由等） */
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useDialogBehavior({
    open,
    onClose: () => {
      if (!busy) onCancel();
    },
    panelRef,
    initialFocusRef: cancelRef,
  });

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="presentation">
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : '操作确认'}
        className="w-full max-w-md rounded-md border border-line bg-white p-6 shadow-xl"
      >
        <div className="flex items-start gap-3">
          {danger ? (
            <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-bad-soft text-bad">
              <AlertTriangleIcon className="size-4" />
            </span>
          ) : null}
          <div className="min-w-0">
            <h3 className="text-base font-semibold text-ink">{title}</h3>
            <div className="mt-1.5 text-sm leading-6 text-sub">{message}</div>
          </div>
        </div>
        <div className="mt-6 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="rounded-md border border-line-soft bg-white px-4 py-2 text-sm font-medium text-ink hover:bg-mist disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className={`inline-flex h-10 items-center gap-2 rounded-md px-4 text-sm font-medium text-white transition-colors disabled:opacity-50 ${
              danger ? 'bg-bad hover:bg-bad-hover' : 'bg-brand-deep hover:bg-brand-hover'
            }`}
          >
            {busy && <LoaderCircleIcon className="size-4 animate-spin" />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}