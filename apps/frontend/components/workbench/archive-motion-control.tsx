'use client';

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArchiveIcon, ArrowUpRightIcon, LoaderCircleIcon } from 'lucide-react';
import { ARCHIVE_PRESET_CATEGORIES, type ArchiveRecord } from '@/lib/api/archives';
import { useDialogBehavior } from './dialog-behavior';
import { ARCHIVE_ARRIVAL_PARAM } from './archive-arrival';
import { ArchiveMachine, OperationFrame, useAnimationCompletion, useMotionSequence } from './operation-motion';
import styles from './operation-motion.module.css';

type ArchiveReceipt = Pick<ArchiveRecord, 'archive_id' | 'candidate_name' | 'category' | 'job_title' | 'status'>;
type ArchiveResult =
  | { phase: 'pending' }
  | { phase: 'success'; record: ArchiveReceipt }
  | { phase: 'existing'; record: ArchiveReceipt; requestedCategory: string; updating?: boolean; updated?: boolean; error?: string }
  | { phase: 'error'; message: string; inTrash?: true };

export default function ArchiveMotionControl({ candidate, jobTitle, disabled, onArchive, onArchived, onUpdateCategory, archivePath = '/archives' }: {
  candidate: string;
  jobTitle: string;
  disabled?: boolean;
  onArchive: (category: string) => Promise<{ record: ArchiveReceipt; created: boolean }>;
  onArchived?: (record: ArchiveReceipt & { status: 'active' }) => void;
  onUpdateCategory?: (archiveId: string, category: string) => Promise<ArchiveReceipt>;
  archivePath?: string;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [preset, setPreset] = useState('');
  const [custom, setCustom] = useState('');
  const [result, setResult] = useState<ArchiveResult | null>(null);
  const [departure, setDeparture] = useState('');
  const [drawerMilestone, setDrawerMilestone] = useState<'inserting' | 'closed' | 'reopened'>('inserting');
  const saving = useRef(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const previousPanel = useRef<DOMRect | null>(null);
  const departureRef = useRef<SVGGElement>(null);
  const drawerRef = useRef<SVGGElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const firstCategory = useRef<HTMLButtonElement>(null);
  const categoryId = useId();
  const titleId = useId();
  const showingMotion = result !== null;
  const { motionPhase, statusPhase, introRef, finishRef } = useMotionSequence(open && showingMotion, result?.phase === 'existing' ? 'pending' : result?.phase ?? 'pending');
  const existing = result?.phase === 'existing' ? result : null;
  const drawerStage = existing ? 'closed' : departure
    ? (drawerMilestone === 'reopened' ? 'extracting' : 'reopening')
    : drawerMilestone === 'closed' ? 'closed' : statusPhase === 'success' ? 'closing' : 'initial';
  const drawerClosing = drawerStage === 'closing';
  const displayPhase = existing ? (existing.updating ? 'pending' : existing.error ? 'error' : 'success') : drawerClosing ? 'pending' : statusPhase;
  const close = () => { setDeparture(''); setOpen(false); };

  useDialogBehavior({ open, onClose: close, panelRef, autoFocus: false });
  useEffect(() => {
    if (open) (showingMotion ? closeRef : firstCategory).current?.focus();
  }, [open, showingMotion]);
  useLayoutEffect(() => {
    const from = previousPanel.current;
    previousPanel.current = null;
    const panel = panelRef.current;
    if (!open || !from || !panel?.animate || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const to = panel.getBoundingClientRect();
    if (!to.width || !to.height) return;
    const animation = panel.animate([
      { transform: `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${from.width / to.width}, ${from.height / to.height})` },
      { transform: 'none' },
    ], { duration: 330, easing: 'cubic-bezier(.22, .8, .25, 1)' });
    return () => animation.cancel();
  }, [open, showingMotion]);
  const navigateToRecord = useCallback(() => router.push(departure), [departure, router]);
  const markClosed = useCallback(() => setDrawerMilestone('closed'), []);
  const markReopened = useCallback(() => setDrawerMilestone('reopened'), []);
  useAnimationCompletion(open && drawerClosing, drawerRef, markClosed);
  useAnimationCompletion(open && drawerStage === 'reopening', drawerRef, markReopened);
  useAnimationCompletion(open && drawerStage === 'extracting', departureRef, navigateToRecord);

  function rememberPanel() {
    previousPanel.current = panelRef.current?.getBoundingClientRect() ?? null;
  }

  const category = custom.trim() || preset;
  const displayCategory = category || jobTitle || '默认分类';
  const pending = result?.phase === 'pending' || existing?.updating;
  const record = result?.phase === 'success' ? result.record : existing?.record;
  const recordHref = record ? `${archivePath}?${new URLSearchParams({ [ARCHIVE_ARRIVAL_PARAM]: record.archive_id })}` : '';
  const existingCategory = existing?.record.category || existing?.record.job_title || '未分类';
  const canChangeCategory = Boolean(onUpdateCategory && existing?.requestedCategory && !existing.updated && existing.requestedCategory !== existing.record.category);

  const confirm = async () => {
    if (saving.current) return;
    saving.current = true;
    rememberPanel();
    setResult({ phase: 'pending' });
    try {
      const { record: saved, created } = await onArchive(category);
      if (saved.status === 'trashed') {
        setResult({ phase: 'error', inTrash: true, message: '请前往候选人才库的「回收站」恢复这条档案，再查看档案。' });
        return;
      }
      if (saved.status !== 'active') {
        setResult({ phase: 'error', message: '未能确认档案已进入候选人才库，请稍后重试。' });
        return;
      }
      if (!created) {
        setResult({ phase: 'existing', record: saved, requestedCategory: category });
        return;
      }
      setResult({ phase: 'success', record: saved });
      onArchived?.({ ...saved, status: saved.status });
    } catch (caught) {
      setResult({ phase: 'error', message: caught instanceof Error ? caught.message : '请稍后重试。' });
    } finally {
      saving.current = false;
    }
  };

  const changeCategory = async () => {
    if (!existing || !canChangeCategory || !onUpdateCategory || saving.current) return;
    saving.current = true;
    const { record: currentRecord, requestedCategory } = existing;
    setResult({ phase: 'existing', record: currentRecord, requestedCategory, updating: true });
    try {
      const updated = await onUpdateCategory(currentRecord.archive_id, requestedCategory);
      if (updated.status !== 'active' || updated.category !== requestedCategory) throw new Error('未能确认分类已更新。');
      setResult({ phase: 'existing', record: updated, requestedCategory, updated: true });
    } catch (caught) {
      setResult({ phase: 'existing', record: currentRecord, requestedCategory, error: caught instanceof Error ? caught.message : '分类更新失败，请稍后重试。' });
    } finally {
      saving.current = false;
    }
  };

  const status = existing
    ? existing.updating ? '正在更新分类' : existing.error ? '分类更新失败' : existing.updated ? '分类已更新' : '档案已存在'
    : displayPhase === 'success' ? '已归档到人才库' : result?.phase === 'error' ? (result.inTrash ? '档案在回收站' : '归档失败') : drawerClosing ? '正在合上抽屉' : '正在归档';
  const detail = existing
    ? existing.error || `当前分类：${existingCategory}${canChangeCategory ? `；本次选择：${existing.requestedCategory}` : ''}`
    : result?.phase === 'error' ? result.message : `${record?.candidate_name || candidate} · ${record?.category || record?.job_title || displayCategory}`;

  return (
    <>
      <button
        type="button"
        disabled={disabled}
        aria-disabled={pending || undefined}
        onClick={() => {
          if (saving.current) return;
          setPreset('');
          setCustom('');
          setResult(null);
          setDrawerMilestone('inserting');
          setOpen(true);
        }}
        className="inline-flex h-[38px] items-center gap-2 border border-control-border bg-control px-4 text-[13px] font-medium text-ink transition-colors hover:bg-control-hover disabled:opacity-50 aria-disabled:opacity-50"
      >
        {pending ? <LoaderCircleIcon className="size-4 motion-safe:animate-spin" /> : <ArchiveIcon className="size-4" />} 归档到人才库
      </button>
      {open && (
        <div className={styles.overlay} onClick={(event) => { if (event.target === event.currentTarget) close(); }}>
          {result ? (
            <OperationFrame
              panelRef={panelRef}
              closeRef={closeRef}
              id={titleId}
              eyebrow="TALENT LIBRARY / ARCHIVE"
              title="归档到候选人才库"
              morph
              phase={displayPhase}
              status={status}
              detail={detail}
              onClose={close}
              closeLabel={existing?.updating ? '关闭窗口，继续更新分类' : pending ? '关闭窗口，继续归档' : '关闭归档窗口'}
              graphic={<ArchiveMachine candidate={record?.candidate_name || candidate} category={record?.category || record?.job_title || displayCategory} phase={motionPhase} drawerStage={drawerStage} introRef={introRef} finishRef={finishRef} drawerRef={drawerRef} departureRef={departureRef} />}
              action={existing ? (
                canChangeCategory ? (
                  <button type="button" className={styles.primary} onClick={() => void changeCategory()} aria-disabled={existing.updating}>
                    {existing.updating ? '正在更新…' : existing.error ? '重试修改分类' : '修改为所选分类'}
                  </button>
                ) : (
                  <Link href={recordHref} className={styles.button}>查看人才库档案 <ArrowUpRightIcon size={15} aria-hidden="true" /></Link>
                )
              ) : displayPhase === 'success' && record ? (
                <Link
                  href={recordHref}
                  className={styles.primary}
                  aria-disabled={Boolean(departure) || undefined}
                  onNavigate={(event) => {
                    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
                    event.preventDefault();
                    setDeparture(recordHref);
                  }}
                >
                  查看人才库档案 <ArrowUpRightIcon size={15} aria-hidden="true" />
                </Link>
              ) : result.phase === 'error' ? (
                result.inTrash ? (
                  <Link href={archivePath} className={styles.button}>前往人才库 <ArrowUpRightIcon size={15} aria-hidden="true" /></Link>
                ) : (
                  <button type="button" className={styles.button} onClick={() => { rememberPanel(); setResult(null); }}>返回重试</button>
                )
              ) : <span className={styles.actionSpace} aria-hidden="true" />}
            />
          ) : (
            <div ref={panelRef} className={`${styles.panel} w-full max-w-md p-6`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
              <h2 id={titleId} className="text-lg font-semibold text-ink">归档到候选人才库</h2>
              <p className="mt-1.5 text-sm leading-6 text-sub">选择或自定义岗位分类，方便后续按分类筛选排名。</p>
              <fieldset className="mt-4">
                <legend className="mb-2 text-xs font-medium text-sub">预设分类</legend>
                <div className="flex flex-wrap gap-2">
                  {ARCHIVE_PRESET_CATEGORIES.map((item, index) => (
                    <button
                      key={item}
                      type="button"
                      ref={index === 0 ? firstCategory : undefined}
                      aria-pressed={preset === item}
                      onClick={() => { setPreset(item); setCustom(''); }}
                      className={`rounded-full border px-3 py-1 text-sm transition-colors ${preset === item ? 'border-brand bg-brand-soft text-brand' : 'border-line-soft bg-white text-body hover:bg-mist'}`}
                    >
                      {item}
                    </button>
                  ))}
                </div>
              </fieldset>
              <form className="mt-4" onSubmit={(event) => { event.preventDefault(); void confirm(); }}>
                <label className="mb-1.5 block text-xs font-medium text-sub" htmlFor={categoryId}>自定义分类</label>
                <input
                  id={categoryId}
                  type="text"
                  value={custom}
                  onChange={(event) => { setCustom(event.target.value); setPreset(''); }}
                  placeholder="如：AI 算法工程师（留空则使用岗位名）"
                  className="w-full border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand focus:ring-2 focus:ring-brand/20"
                />
                <div className="mt-6 flex justify-end gap-2">
                  <button type="button" onClick={close} className={styles.button}>取消</button>
                  <button type="submit" className={styles.primary}><ArchiveIcon size={16} /> 确认归档</button>
                </div>
              </form>
            </div>
          )}
        </div>
      )}
    </>
  );
}
