'use client';

import { useEffect, useRef, type RefObject } from 'react';

/** 可聚焦元素选择器（排除 disabled 与隐藏元素由调用处负责） */
const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

/**
 * 统一弹窗/抽屉键盘交互：Esc 关闭、背景滚动锁定、打开聚焦目标元素、
 * 关闭后焦点恢复到触发元素、Tab 焦点陷阱（首尾循环）。
 * 供 AdminModal/ConfirmDialog/归档弹窗/详情抽屉复用，保证全站行为一致。
 */
export function useDialogBehavior({
  open,
  onClose,
  panelRef,
  autoFocus = true,
  initialFocusRef,
  closeOnEscape = true,
  lockScroll = true,
}: {
  open: boolean;
  onClose: () => void;
  panelRef: RefObject<HTMLElement | null>;
  /** 打开后是否自动聚焦（默认聚焦第一个可聚焦元素，或 initialFocusRef 指定元素） */
  autoFocus?: boolean;
  initialFocusRef?: RefObject<HTMLElement | null>;
  closeOnEscape?: boolean;
  lockScroll?: boolean;
}) {
  const triggerRef = useRef<HTMLElement | null>(null);

  // 打开：记录触发元素；关闭：焦点恢复
  useEffect(() => {
    if (!open) return;
    triggerRef.current = document.activeElement as HTMLElement | null;
    if (autoFocus) {
      const target = initialFocusRef?.current ?? panelRef.current?.querySelector<HTMLElement>(FOCUSABLE);
      target?.focus();
    }
    return () => {
      triggerRef.current?.focus();
    };
  }, [open, autoFocus, initialFocusRef, panelRef]);

  // Esc 关闭
  useEffect(() => {
    if (!open || !closeOnEscape) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, closeOnEscape, onClose]);

  // 背景滚动锁定
  useEffect(() => {
    if (!open || !lockScroll) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open, lockScroll]);

  // Tab 焦点陷阱：焦点移出面板时循环回面板内
  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    if (!panel) return;
    const onTab = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const focusables = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      const inside = panel.contains(active);
      if (event.shiftKey && (!inside || active === first)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (!inside || active === last)) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onTab);
    return () => window.removeEventListener('keydown', onTab);
  }, [open, panelRef]);
}