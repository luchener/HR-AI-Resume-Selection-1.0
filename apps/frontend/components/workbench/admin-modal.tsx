'use client';

import { useRef, type ReactNode } from 'react';
import { XCircleIcon } from 'lucide-react';
import { useDialogBehavior } from './dialog-behavior';

/**
 * 管理后台统一弹窗：Esc 关闭、打开自动聚焦首个可聚焦元素、背景滚动锁定、
 * 关闭后焦点恢复到触发元素、Tab 焦点陷阱（与 ConfirmDialog/抽屉同一套行为）。
 * 无障碍：role=dialog + aria-modal；点遮罩关闭；标题栏右侧 ✕ 按钮。
 */
export default function AdminModal({
  title,
  onClose,
  children,
  footer,
  maxWidth = 'max-w-2xl',
  autoFocus = true,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  /** 底部操作按钮区（按钮右对其；若为空则不渲染） */
  footer?: ReactNode;
  maxWidth?: string;
  /** 打开后是否自动聚焦首个输入框/按钮，默认 true */
  autoFocus?: boolean;
}) {
  const panelRef = useRef<HTMLDivElement>(null);

  useDialogBehavior({
    open: true,
    onClose,
    panelRef,
    autoFocus,
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose} role="presentation">
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : undefined}
        className={`w-full ${maxWidth} rounded-md border border-line bg-white p-6 shadow-xl`}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3">
          <h3 className="flex items-center gap-2 text-base font-semibold text-ink">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="relative inline-flex size-8 shrink-0 items-center justify-center rounded-md text-sub after:absolute after:-inset-1.5 after:content-[''] transition-colors hover:bg-mist"
          >
            <XCircleIcon className="size-5" />
          </button>
        </div>
        <div className="mt-4">{children}</div>
        {footer && <div className="mt-5 flex justify-end gap-2">{footer}</div>}
      </div>
    </div>
  );
}