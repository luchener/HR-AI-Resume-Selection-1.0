'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  CheckCircle2Icon,
  DownloadIcon,
  FileSearch2Icon,
  LoaderCircleIcon,
  SearchIcon,
  Trash2Icon,
  XIcon,
} from 'lucide-react';
import AppShell from '@/components/workbench/app-shell';
import ConfirmDialog from '@/components/workbench/confirm-dialog';
import { useDialogBehavior } from '@/components/workbench/dialog-behavior';
import {
  ARCHIVE_PRESET_CATEGORIES,
  emptyTrash,
  fetchArchives,
  fetchArchiveDetail,
  fetchTrash,
  moveToTrash,
  permanentDeleteArchive,
  restoreArchive,
  updateArchiveCategory,
  updateArchiveTags,
  type ArchiveRecord,
} from '@/lib/api/archives';
import { downloadReportImage } from '@/components/workbench/report-export';

const FIT_TAG_STYLE: Record<string, string> = {
  '高匹配': 'bg-good-soft text-good',
  '部分匹配': 'bg-warn-soft text-warn',
  '不匹配': 'bg-bad-soft text-bad',
};

const REC_STYLE: Record<string, string> = {
  '优先面试': 'bg-good-soft text-good',
  '储备观察': 'bg-warn-soft text-warn',
  '淘汰': 'bg-bad-soft text-bad',
};

function ScoreBadge({ score }: { score: number }) {
  const color = score >= 80 ? 'text-good' : score >= 60 ? 'text-warn' : 'text-bad';
  return <span className={`text-lg font-semibold ${color}`}>{score > 0 ? score : '--'}</span>;
}

function rankMedal(rank: number): string {
  return String(rank);
}

function timeLabel(iso?: string | null): string {
  if (!iso) return '--';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '--';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export default function ArchivesPage() {
  const [tab, setTab] = useState<'active' | 'trash'>('active');
  const [archives, setArchives] = useState<ArchiveRecord[]>([]);
  const [trash, setTrash] = useState<ArchiveRecord[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [nameQuery, setNameQuery] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [tagFilter, setTagFilter] = useState('');
  const [sort, setSort] = useState<'score' | 'created'>('score');
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ArchiveRecord | null>(null);
  const [tagDraft, setTagDraft] = useState('');
  const [categoryDraft, setCategoryDraft] = useState('');
  const [regenerating, setRegenerating] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  /** 待确认的高风险操作（P1-2：替代原生 window.confirm） */
  const [pendingAction, setPendingAction] = useState<{ kind: 'trash' | 'delete' | 'empty'; record?: ArchiveRecord } | null>(null);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 详情抽屉：统一弹窗行为（Esc 关闭 / 焦点恢复 / Tab 陷阱）——P1-3
  const drawerPanelRef = useRef<HTMLDivElement>(null);
  useDialogBehavior({
    open: detail !== null,
    onClose: () => setDetail(null),
    panelRef: drawerPanelRef,
  });

  const loadActive = useCallback(async (query = nameQuery) => {
    setLoading(true);
    try {
      const data = await fetchArchives({
        name: query || undefined,
        category: categoryFilter || undefined,
        tag: tagFilter || undefined,
        sort,
      });
      setArchives(data.archives);
      setCategories(data.meta.categories || []);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '归档列表读取失败。');
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [categoryFilter, tagFilter, sort]);

  const loadTrash = useCallback(async () => {
    try {
      const data = await fetchTrash();
      setTrash(data.archives);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '回收站读取失败。');
    }
  }, []);

  useEffect(() => {
    if (tab === 'active') loadActive();
    else loadTrash();
  }, [tab, loadActive, loadTrash]);

  // 姓名查询防抖
  useEffect(() => {
    if (tab !== 'active') return;
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => loadActive(nameQuery), 300);
    return () => {
      if (searchTimer.current) clearTimeout(searchTimer.current);
    };
  }, [nameQuery, tab, loadActive]);

  const ranked = useMemo(() => {
    // 动态排名：对当前筛选结果按 final_score 降序，同分并列取相同名次
    let prevScore = Number.POSITIVE_INFINITY;
    let prevRank = 0;
    return archives.map((a, index) => {
      const score = a.final_score || 0;
      const rank = score === prevScore ? prevRank : index + 1;
      prevScore = score;
      prevRank = rank;
      return { ...a, rank };
    });
  }, [archives]);

  const availableTags = useMemo(() => {
    const set = new Set<string>();
    archives.forEach((a) => (a.custom_tags || []).forEach((t) => set.add(t)));
    return Array.from(set).sort();
  }, [archives]);

  const showNotice = (message: string) => {
    setNotice(message);
    setTimeout(() => setNotice(''), 3000);
  };

  const handleMoveToTrash = async (record: ArchiveRecord) => {
    setBusyId(record.archive_id);
    try {
      await moveToTrash(record.archive_id);
      setArchives((prev) => prev.filter((a) => a.archive_id !== record.archive_id));
      await loadTrash();
      showNotice(`「${record.candidate_name}」已移入回收站`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '操作失败。');
    } finally {
      setBusyId(null);
    }
  };

  const handleRestore = async (record: ArchiveRecord) => {
    setBusyId(record.archive_id);
    try {
      await restoreArchive(record.archive_id);
      setTrash((prev) => prev.filter((a) => a.archive_id !== record.archive_id));
      await loadActive();
      showNotice(`「${record.candidate_name}」已恢复到人才库`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '恢复失败。');
    } finally {
      setBusyId(null);
    }
  };

  const handlePermanentDelete = async (record: ArchiveRecord) => {
    setBusyId(record.archive_id);
    try {
      await permanentDeleteArchive(record.archive_id);
      setTrash((prev) => prev.filter((a) => a.archive_id !== record.archive_id));
      showNotice(`「${record.candidate_name}」已彻底删除`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '删除失败。');
    } finally {
      setBusyId(null);
    }
  };

  const handleEmptyTrash = async () => {
    setBusyId('__all__');
    try {
      await emptyTrash();
      setTrash([]);
      showNotice('回收站已清空');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '清空失败。');
    } finally {
      setBusyId(null);
    }
  };

  const openDetail = async (record: ArchiveRecord) => {
    try {
      const full = await fetchArchiveDetail(record.archive_id);
      setDetail(full);
      setTagDraft((full.custom_tags || []).join('，'));
      setCategoryDraft(full.category || full.job_title || '');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '详情读取失败。');
    }
  };

  const saveTags = async () => {
    if (!detail) return;
    const tags = tagDraft.split(/[，,]/).map((t) => t.trim()).filter(Boolean);
    try {
      const updated = await updateArchiveTags(detail.archive_id, tags);
      setDetail(updated);
      setArchives((prev) => prev.map((a) => (a.archive_id === updated.archive_id ? updated : a)));
      showNotice('标签已更新');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '标签更新失败。');
    }
  };

  const saveCategory = async () => {
    if (!detail) return;
    const category = categoryDraft.trim();
    if (!category) return;
    try {
      const updated = await updateArchiveCategory(detail.archive_id, category);
      setDetail(updated);
      setArchives((prev) => prev.map((a) => (a.archive_id === updated.archive_id ? updated : a)));
      await loadActive();
      showNotice('岗位分类已更新');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '分类更新失败。');
    }
  };

  const exportReportImage = async () => {
    if (!detail || !detail.analysis || regenerating) return;
    setRegenerating(true);
    setError('');
    try {
      const ok = await downloadReportImage({
        analysis: detail.analysis as unknown as Parameters<typeof downloadReportImage>[0]['analysis'],
        candidateName: detail.candidate_name || '候选人',
        resumeId: detail.resume_id,
      });
      if (!ok) throw new Error('报告图片生成失败，请稍后重试。');
      showNotice('报告图片已导出');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '报告图片导出失败。');
    } finally {
      setRegenerating(false);
    }
  };

  return (
    <AppShell active="archives">
      <div className="mx-auto w-full max-w-[1500px] px-5 py-8 sm:px-8 lg:px-10 lg:py-10 xl:px-14">
        {/* 头部 */}
        <header className="flex flex-col gap-4 border-b border-line pb-7 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <p className="text-xs font-semibold uppercase text-sub">Archive Pool</p>
            <h1 className="mt-2 text-3xl font-semibold text-ink sm:text-4xl">候选人才库</h1>
            <p className="mt-3 text-sm text-sub">归档已通过 AI 筛选的候选人，支持按姓名查询、岗位分类与分数排名</p>
          </div>
          <div className="flex items-center gap-1 rounded-md border border-line bg-white p-1">
            <button
              type="button"
              onClick={() => setTab('active')}
              className={`inline-flex h-9 items-center gap-2 rounded px-4 text-sm font-medium transition-colors ${tab === 'active' ? 'bg-brand-deep text-white' : 'text-body hover:bg-mist'}`}
            >
              <ArchiveIcon className="size-4" /> 在库人才
              <span className="rounded bg-black/10 px-1.5 text-xs">{archives.length}</span>
            </button>
            <button
              type="button"
              onClick={() => setTab('trash')}
              className={`inline-flex h-9 items-center gap-2 rounded px-4 text-sm font-medium transition-colors ${tab === 'trash' ? 'bg-brand-deep text-white' : 'text-body hover:bg-mist'}`}
            >
              <Trash2Icon className="size-4" /> 回收站
              <span className="rounded bg-black/10 px-1.5 text-xs">{trash.length}</span>
            </button>
          </div>
        </header>

        {notice && (
          <div className="mt-4 flex items-center gap-3 rounded-md border border-good-border bg-good-soft px-4 py-3 text-sm text-good-deep">
            <CheckCircle2Icon className="size-4 shrink-0" /> {notice}
          </div>
        )}
        {error && (
          <div className="mt-4 flex items-center gap-3 rounded-md border border-bad-border bg-bad-soft px-4 py-3 text-sm text-bad">
            <XIcon className="size-4 shrink-0" /> {error}
            <button type="button" onClick={() => setError('')} className="ml-auto text-xs underline">关闭</button>
          </div>
        )}

        {tab === 'active' ? (
          <>
            {/* 筛选区 */}
            <div className="mt-6 flex flex-wrap items-center gap-3 rounded-md border border-line bg-white p-4">
              <div className="relative min-w-56 flex-1">
                <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-sub" />
                <input
                  type="text"
                  value={nameQuery}
                  onChange={(e) => setNameQuery(e.target.value)}
                  placeholder="按候选人姓名查询…"
                  className="w-full rounded-md border border-line-soft py-2 pl-9 pr-3 text-sm outline-none focus:border-brand focus:ring-2 focus:ring-brand/20"
                />
              </div>
              <select
                value={categoryFilter}
                onChange={(e) => setCategoryFilter(e.target.value)}
                className="rounded-md border border-line-soft bg-white px-3 py-2 text-sm text-ink outline-none focus:border-brand"
              >
                <option value="">全部分类</option>
                {[...new Set([...ARCHIVE_PRESET_CATEGORIES, ...categories])].map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <select
                value={tagFilter}
                onChange={(e) => setTagFilter(e.target.value)}
                className="rounded-md border border-line-soft bg-white px-3 py-2 text-sm text-ink outline-none focus:border-brand"
              >
                <option value="">全部标签</option>
                {availableTags.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value as 'score' | 'created')}
                className="rounded-md border border-line-soft bg-white px-3 py-2 text-sm text-ink outline-none focus:border-brand"
              >
                <option value="score">按得分排名</option>
                <option value="created">按归档时间</option>
              </select>
            </div>

            {/* 列表 */}
            <div className="mt-4 overflow-x-auto rounded-md border border-line bg-white">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-mist">
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">排名</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">候选人</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">岗位分类</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">最终得分</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">适配等级</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">招聘建议</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">标签</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">归档时间</th>
                    <th className="border-b border-line-soft px-4 py-3 text-right text-xs font-semibold text-sub">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {loading ? (
                    <tr><td colSpan={9} className="px-4 py-10 text-center text-sub"><LoaderCircleIcon className="mx-auto size-5 animate-spin" /></td></tr>
                  ) : ranked.length === 0 ? (
                    <tr><td colSpan={9} className="px-4 py-14 text-center">
                      <FileSearch2Icon className="mx-auto size-8 text-sub/60" />
                      <p className="mt-3 text-sm text-sub">暂无符合条件的归档。去分析报告页点击「归档到人才库」。</p>
                    </td></tr>
                  ) : ranked.map((a) => (
                    <tr key={a.archive_id} className="border-b border-line-soft last:border-b-0 hover:bg-soft/60">
                      <td className="px-4 py-3 font-semibold text-ink">{rankMedal(a.rank)}</td>
                      <td className="px-4 py-3">
                        <button type="button" onClick={() => openDetail(a)} className="flex items-center gap-2 text-left font-medium text-ink hover:text-brand">
                          <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-brand-soft text-xs font-bold text-brand">
                            {(a.candidate_name || '?').slice(0, 1)}
                          </span>
                          <span className="break-words">{a.candidate_name || '未提供'}</span>
                        </button>
                      </td>
                      <td className="px-4 py-3 text-body">{a.category || a.job_title || '--'}</td>
                      <td className="px-4 py-3"><ScoreBadge score={a.final_score} /></td>
                      <td className="px-4 py-3"><span className={`rounded px-2 py-0.5 text-xs font-medium ${FIT_TAG_STYLE[a.fit_tag] || 'bg-mist text-sub'}`}>{a.fit_tag || '--'}</span></td>
                      <td className="px-4 py-3"><span className={`rounded px-2 py-0.5 text-xs font-medium ${REC_STYLE[a.recruitment_recommendation] || 'bg-mist text-sub'}`}>{a.recruitment_recommendation || '--'}</span></td>
                      <td className="px-4 py-3">
                        <div className="flex max-w-44 flex-wrap gap-1">
                          {(a.custom_tags || []).map((t) => <span key={t} className="rounded bg-brand-soft px-1.5 py-0.5 text-[11px] text-brand-deep">{t}</span>)}
                          {(a.custom_tags || []).length === 0 && <span className="text-xs text-sub/70">--</span>}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-sub">{timeLabel(a.created_at)}</td>
                      <td className="px-4 py-3">
                        <div className="flex justify-end gap-1.5">
                          <button
                            type="button"
                            disabled={busyId === a.archive_id}
                            onClick={() => openDetail(a)}
                            className="relative rounded border border-line-soft px-2.5 py-1 text-xs font-medium text-ink after:absolute after:-inset-1.5 after:content-[''] hover:bg-mist disabled:opacity-50"
                          >详情</button>
                          <button
                            type="button"
                            disabled={busyId === a.archive_id}
                            onClick={() => setPendingAction({ kind: 'trash', record: a })}
                            className="relative rounded border border-line-soft px-2.5 py-1 text-xs font-medium text-bad after:absolute after:-inset-1.5 after:content-[''] hover:bg-bad-soft disabled:opacity-50"
                          >
                            {busyId === a.archive_id ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : '移入回收站'}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <>
            <div className="mt-6 flex items-center justify-between rounded-md border border-line bg-white p-4">
              <p className="text-sm text-sub">回收站中的归档可恢复，或彻底删除（不可恢复）。</p>
              <button
                type="button"
                disabled={busyId !== null || trash.length === 0}
                onClick={() => setPendingAction({ kind: 'empty' })}
                className="inline-flex h-9 items-center gap-2 rounded-md border border-bad/30 bg-bad-soft px-4 text-sm font-medium text-bad hover:bg-bad-hover-soft disabled:opacity-50"
              >
                <Trash2Icon className="size-4" /> 清空回收站
              </button>
            </div>
            <div className="mt-4 overflow-x-auto rounded-md border border-line bg-white">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-mist">
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">候选人</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">岗位分类</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">最终得分</th>
                    <th className="border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub">回收时间</th>
                    <th className="border-b border-line-soft px-4 py-3 text-right text-xs font-semibold text-sub">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {trash.length === 0 ? (
                    <tr><td colSpan={5} className="px-4 py-14 text-center">
                      <Trash2Icon className="mx-auto size-8 text-sub/60" />
                      <p className="mt-3 text-sm text-sub">回收站是空的。</p>
                    </td></tr>
                  ) : trash.map((a) => (
                    <tr key={a.archive_id} className="border-b border-line-soft last:border-b-0 hover:bg-soft/60">
                      <td className="px-4 py-3">
                        <span className="flex items-center gap-2 font-medium text-ink">
                          <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-mist text-xs font-bold text-sub">{(a.candidate_name || '?').slice(0, 1)}</span>
                          <span className="break-words">{a.candidate_name || '未提供'}</span>
                        </span>
                      </td>
                      <td className="px-4 py-3 text-body">{a.category || a.job_title || '--'}</td>
                      <td className="px-4 py-3"><ScoreBadge score={a.final_score} /></td>
                      <td className="px-4 py-3 text-sub">{timeLabel(a.trashed_at)}</td>
                      <td className="px-4 py-3">
                        <div className="flex justify-end gap-1.5">
                          <button
                            type="button"
                            disabled={busyId === a.archive_id}
                            onClick={() => handleRestore(a)}
                            className="relative inline-flex items-center gap-1 rounded border border-line-soft px-2.5 py-1 text-xs font-medium text-good after:absolute after:-inset-1.5 after:content-[''] hover:bg-good-soft disabled:opacity-50"
                          >
                            {busyId === a.archive_id ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <ArchiveRestoreIcon className="size-3.5" />} 恢复
                          </button>
                          <button
                            type="button"
                            disabled={busyId === a.archive_id}
                            onClick={() => setPendingAction({ kind: 'delete', record: a })}
                            className="relative inline-flex items-center gap-1 rounded border border-line-soft px-2.5 py-1 text-xs font-medium text-bad after:absolute after:-inset-1.5 after:content-[''] hover:bg-bad-soft disabled:opacity-50"
                          >
                            <Trash2Icon className="size-3.5" /> 彻底删除
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      {/* 详情抽屉 */}
      {detail && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="归档详情"
          className="fixed inset-0 z-50 flex justify-end bg-black/40"
          onClick={(e) => { if (e.target === e.currentTarget) setDetail(null); }}
        >
          <div ref={drawerPanelRef} className="h-full w-full max-w-xl overflow-y-auto bg-white shadow-2xl">
            <div className="sticky top-0 flex items-center justify-between border-b border-line bg-white px-6 py-4">
              <div className="flex items-center gap-3">
                <span className="flex size-10 items-center justify-center rounded-full bg-brand-soft text-base font-bold text-brand">
                  {(detail.candidate_name || '?').slice(0, 1)}
                </span>
                <div>
                  <h2 className="text-lg font-semibold text-ink">{detail.candidate_name || '未提供'}</h2>
                  <p className="text-xs text-sub">{detail.category || detail.job_title || '未关联岗位'}</p>
                </div>
              </div>
              <button type="button" onClick={() => setDetail(null)} aria-label="关闭" className="relative flex size-8 items-center justify-center rounded-md text-sub after:absolute after:-inset-1.5 after:content-[''] hover:bg-mist">
                <XIcon className="size-4" />
              </button>
            </div>

            <div className="space-y-6 px-6 py-6">
              {/* 关键信息 */}
              <div className="grid grid-cols-3 gap-3">
                <div className="rounded-md border border-line-soft bg-mist/50 p-3 text-center">
                  <p className="text-xs text-sub">最终得分</p>
                  <p className="mt-1 text-2xl font-semibold text-brand">{detail.final_score || '--'}</p>
                </div>
                <div className="rounded-md border border-line-soft bg-mist/50 p-3 text-center">
                  <p className="text-xs text-sub">适配等级</p>
                  <p className={`mt-2 text-sm font-semibold ${FIT_TAG_STYLE[detail.fit_tag] || 'text-ink'}`}>{detail.fit_tag || '--'}</p>
                </div>
                <div className="rounded-md border border-line-soft bg-mist/50 p-3 text-center">
                  <p className="text-xs text-sub">招聘建议</p>
                  <p className={`mt-2 text-sm font-semibold ${REC_STYLE[detail.recruitment_recommendation] || 'text-ink'}`}>{detail.recruitment_recommendation || '--'}</p>
                </div>
              </div>

              {/* 岗位分类 */}
              <div>
                <label className="mb-1.5 block text-xs font-medium text-sub">岗位分类</label>
                <div className="flex flex-wrap gap-2">
                  {ARCHIVE_PRESET_CATEGORIES.map((c) => (
                    <button
                      key={c}
                      type="button"
                      onClick={() => setCategoryDraft(c)}
                      className={`relative rounded-full border px-3 py-1 text-sm transition-colors after:absolute after:-inset-1.5 after:content-[''] ${categoryDraft === c ? 'border-brand bg-brand-soft text-brand' : 'border-line-soft bg-white text-body hover:bg-mist'}`}
                    >
                      {c}
                    </button>
                  ))}
                </div>
                <div className="mt-2 flex gap-2">
                  <input
                    type="text"
                    value={categoryDraft}
                    onChange={(e) => setCategoryDraft(e.target.value)}
                    placeholder="或输入自定义分类"
                    className="flex-1 rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand focus:ring-2 focus:ring-brand/20"
                  />
                  <button type="button" onClick={saveCategory} className="rounded-md bg-brand-deep px-4 text-sm font-medium text-white hover:bg-brand-hover">保存</button>
                </div>
              </div>

              {/* 自定义标签 */}
              <div>
                <label className="mb-1.5 block text-xs font-medium text-sub">自定义标签（逗号分隔）</label>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={tagDraft}
                    onChange={(e) => setTagDraft(e.target.value)}
                    placeholder="如：AI，社招，急聘"
                    className="flex-1 rounded-md border border-line-soft px-3 py-2 text-sm outline-none focus:border-brand focus:ring-2 focus:ring-brand/20"
                  />
                  <button type="button" onClick={saveTags} className="rounded-md bg-brand-deep px-4 text-sm font-medium text-white hover:bg-brand-hover">保存</button>
                </div>
              </div>

              {/* 分析摘要 */}
              {(() => {
                const snap = detail.analysis_snapshot || {};
                const str = (v: unknown) => (v == null || v === '' ? '--' : String(v));
                return (
                  <div>
                    <h3 className="text-sm font-semibold text-ink">分析摘要</h3>
                    <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 border-t border-line-soft pt-3 text-sm">
                      <div><dt className="text-xs text-sub">岗位契合度</dt><dd className="mt-0.5 font-medium text-ink">{snap.final_score != null ? `${String(snap.final_score)} / 100` : '--'}</dd></div>
                      <div><dt className="text-xs text-sub">相关经验年限</dt><dd className="mt-0.5 font-medium text-ink">{str(snap.relevant_years)}</dd></div>
                      <div><dt className="text-xs text-sub">简历美化程度</dt><dd className="mt-0.5 font-medium text-ink">{str(snap.ai_risk_label)}</dd></div>
                    </dl>
                    {snap.summary ? <p className="mt-3 rounded-md bg-mist/50 p-3 text-sm leading-6 text-body">{str(snap.summary)}</p> : null}
                  </div>
                );
              })()}

              {/* 报告图片快照 */}
              <div>
                <h3 className="text-sm font-semibold text-ink">分析报告图片</h3>
                <p className="mt-1 text-xs leading-5 text-sub">
                  报告图片不占存储，点击按需生成并直接下载（JPEG）。
                </p>
                <button
                  type="button"
                  onClick={exportReportImage}
                  disabled={regenerating || !detail.analysis}
                  title={detail.analysis ? '实时生成并导出报告图片' : '该归档无完整分析数据，无法导出，请重新归档'}
                  className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-brand px-3 py-2 text-xs font-medium text-white hover:bg-brand-hover-soft disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {regenerating ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <DownloadIcon className="size-3.5" />}
                  导出报告图片
                </button>
                {!detail.analysis && (
                  <p className="mt-2 text-[11px] text-warn">旧归档缺少完整分析数据，无法导出报告图片，请重新归档该候选人。</p>
                )}
              </div>

              {/* 元信息 */}
              <div className="rounded-md bg-mist/40 p-3 text-xs leading-6 text-sub">
                <p>归档时间：{timeLabel(detail.created_at)}</p>
                <p>关联简历 ID：{detail.resume_id}</p>
                <p>关联岗位 ID：{detail.job_id}</p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 高风险操作确认（P1-2：替代原生 window.confirm） */}
      <ConfirmDialog
        open={pendingAction !== null}
        danger={pendingAction?.kind !== 'trash'}
        title={pendingAction?.kind === 'trash' ? '移入回收站' : pendingAction?.kind === 'delete' ? '彻底删除' : '清空回收站'}
        message={
          pendingAction?.kind === 'trash' ? (
            <>将「<span className="font-medium text-ink">{pendingAction?.record?.candidate_name}</span>」移入回收站。可在回收站中恢复，不影响人才库统计。</>
          ) : pendingAction?.kind === 'delete' ? (
            <>将彻底删除「<span className="font-medium text-ink">{pendingAction?.record?.candidate_name}</span>」的归档记录与关联数据，<span className="font-medium text-bad">此操作不可恢复</span>。</>
          ) : (
            <>回收站内所有归档将被彻底删除，<span className="font-medium text-bad">此操作不可恢复</span>。</>
          )
        }
        confirmLabel={pendingAction?.kind === 'trash' ? '移入回收站' : '确认删除'}
        busy={busyId !== null}
        onConfirm={() => {
          const action = pendingAction;
          setPendingAction(null);
          if (action?.kind === 'trash' && action.record) handleMoveToTrash(action.record);
          else if (action?.kind === 'delete' && action.record) handlePermanentDelete(action.record);
          else if (action?.kind === 'empty') handleEmptyTrash();
        }}
        onCancel={() => setPendingAction(null)}
      />
    </AppShell>
  );
}
