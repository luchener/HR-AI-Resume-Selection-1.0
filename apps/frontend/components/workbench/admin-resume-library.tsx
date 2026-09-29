'use client';

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangleIcon, EyeIcon, FileTextIcon, LoaderCircleIcon, RefreshCwIcon, SearchIcon, Trash2Icon } from 'lucide-react';
import AdminModal from './admin-modal';
import ConfirmDialog from './confirm-dialog';
import {
  deleteAdminResume,
  fetchAdminResumeDetail,
  fetchAdminResumes,
  type AdminResumeDetail,
  type AdminResumeItem,
} from '@/lib/api/auth-admin';

const PAGE_SIZES = [10, 20, 50, 100];
const DEFAULT_SIZE = 50;

/** 时间格式化：兼容 epoch 秒 / 毫秒与 ISO 字符串 */
function fmtTime(iso?: string | null): string {
  if (!iso) return '--';
  let num = NaN;
  if (/^\d+(\.\d+)?$/.test(iso.trim())) num = Number(iso.trim());
  const d = Number.isNaN(num) ? new Date(iso) : new Date(num > 1e12 ? num : num * 1000);
  if (Number.isNaN(d.getTime())) return '--';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** 千分位数字（手写而非 toLocaleString，避免 SSR/CSR 展示差异） */
function fmtNum(n: number): string {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/**
 * 用户简历库（超级管理员只读视图）。
 * 硬性约束：本组件只做展示，不提供任何下载 / 导出 / 另存 / 打印 / 复制全文入口。
 */
export default function AdminResumeLibrary() {
  const [keyword, setKeyword] = useState('');
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(DEFAULT_SIZE);
  const [items, setItems] = useState<AdminResumeItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState<{ text: string; kind: 'ok' | 'err' } | null>(null);

  const [detailTarget, setDetailTarget] = useState<AdminResumeItem | null>(null);
  const [detail, setDetail] = useState<AdminResumeDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');

  const [pendingDelete, setPendingDelete] = useState<AdminResumeItem | null>(null);
  const [deleting, setDeleting] = useState(false);

  /** 拉取列表：分页参数显式传入，避免闭包读到旧的分页状态 */
  const load = useCallback(async (nextKeyword: string, nextPage: number, nextSize: number) => {
    setLoading(true);
    setError('');
    try {
      const data = await fetchAdminResumes(nextKeyword, '', nextPage, nextSize);
      setItems(data.items);
      setTotal(data.total);
      setPage(data.page);
      setSize(data.size);
    } catch (err) {
      setItems([]);
      setTotal(0);
      setError(err instanceof Error ? err.message : '简历列表读取失败。');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load('', 1, DEFAULT_SIZE); }, [load]);

  const pageCount = Math.max(1, Math.ceil(total / (size || DEFAULT_SIZE)));

  function handleSearch() { void load(keyword, 1, size); }
  function handleRefresh() { void load(keyword, page, size); }
  function handleSizeChange(nextSize: number) { void load(keyword, 1, nextSize); }
  function goPrev() { if (page > 1) void load(keyword, page - 1, size); }
  function goNext() { if (page < pageCount) void load(keyword, page + 1, size); }

  /** 打开只读原文面板：详情接口才返回 content 与归档数 */
  async function openDetail(item: AdminResumeItem) {
    setDetailTarget(item);
    setDetail(null);
    setDetailError('');
    setDetailLoading(true);
    try {
      setDetail(await fetchAdminResumeDetail(item.resume_id));
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : '简历详情读取失败。');
    } finally {
      setDetailLoading(false);
    }
  }

  function closeDetail() {
    setDetailTarget(null);
    setDetail(null);
    setDetailError('');
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    const target = pendingDelete;
    setDeleting(true);
    try {
      const message = await deleteAdminResume(target.resume_id);
      setPendingDelete(null);
      setNotice({ text: message, kind: 'ok' });
      // 当前页被删空时回退一页，避免停在空白页
      const nextPage = items.length <= 1 && page > 1 ? page - 1 : page;
      await load(keyword, nextPage, size);
    } catch (err) {
      setNotice({ text: err instanceof Error ? err.message : '删除简历失败。', kind: 'err' });
    } finally {
      setDeleting(false);
    }
  }

  const btnSecondary = 'inline-flex h-9 items-center gap-2 rounded-md border border-line-soft bg-white px-4 text-sm font-medium text-ink hover:bg-mist disabled:opacity-50';
  const thCls = 'border-b border-line-soft px-4 py-3 text-left text-xs font-semibold text-sub';

  return (
    <div className="mt-6">
      {notice && (
        <div className={`mb-4 flex items-center justify-between rounded-md border px-4 py-2.5 text-sm ${notice.kind === 'ok' ? 'border-good-border bg-good-soft text-good-deep' : 'border-bad-border bg-bad-soft text-bad'}`}>
          <span>{notice.text}</span>
          <button type="button" onClick={() => setNotice(null)} aria-label="关闭提示" className="ml-3 opacity-60 hover:opacity-100">✕</button>
        </div>
      )}

      {/* 工具栏：关键词 / 每页条数 / 刷新 */}
      <div className="flex flex-wrap items-center gap-3 rounded-md border border-line bg-white p-4">
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-sub" />
          <input
            type="text"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') handleSearch(); }}
            placeholder="搜索候选人姓名或简历内容…"
            className="w-72 rounded-md border border-line-soft bg-white py-2 pl-9 pr-3 text-sm text-ink outline-none focus:border-brand"
          />
        </div>
        <button type="button" onClick={handleSearch} className={btnSecondary}>
          <SearchIcon className="size-4" /> 搜索
        </button>
        <button type="button" onClick={handleRefresh} disabled={loading} className={btnSecondary}>
          <RefreshCwIcon className={`size-4 ${loading ? 'animate-spin' : ''}`} /> 刷新
        </button>
        <label className="flex items-center gap-2 text-sm text-sub">
          每页
          <select
            value={size}
            onChange={(event) => handleSizeChange(Number(event.target.value))}
            className="h-9 rounded-md border border-line-soft bg-white px-2 text-sm text-ink outline-none focus:border-brand"
          >
            {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          条
        </label>
        <p className="ml-auto text-sm text-sub">共 {fmtNum(total)} 份简历</p>
      </div>

      {/* 列表 */}
      <div className="mt-4 overflow-x-auto rounded-md border border-line bg-white">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="bg-mist">
              <th className={thCls}>候选人</th>
              <th className={thCls}>所属用户</th>
              <th className={thCls}>字符数</th>
              <th className={thCls}>上传时间</th>
              <th className="border-b border-line-soft px-4 py-3 text-right text-xs font-semibold text-sub">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={5} className="px-4 py-16 text-center text-sub">
                  <LoaderCircleIcon className="mx-auto size-5 animate-spin" />
                  <span className="mt-2 block text-sm">简历加载中…</span>
                </td>
              </tr>
            ) : error ? (
              <tr>
                <td colSpan={5} className="px-4 py-14 text-center">
                  <AlertTriangleIcon className="mx-auto size-6 text-bad" />
                  <p className="mt-2 text-sm text-bad">{error}</p>
                  <button type="button" onClick={handleRefresh} className="mt-3 inline-flex h-8 items-center gap-1.5 rounded-md border border-line-soft bg-white px-3 text-xs font-medium text-ink hover:bg-mist">
                    <RefreshCwIcon className="size-3.5" /> 重试
                  </button>
                </td>
              </tr>
            ) : items.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-4 py-16 text-center text-sub">
                  <FileTextIcon className="mx-auto size-6 text-sub/60" />
                  <p className="mt-2 text-sm">{keyword.trim() ? '没有匹配的简历，换个关键词试试。' : '当前还没有任何用户简历。'}</p>
                </td>
              </tr>
            ) : items.map((item) => (
              <tr key={item.resume_id} className="border-b border-line-soft last:border-b-0 hover:bg-soft/60">
                <td className="px-4 py-3">
                  <span className="inline-flex items-center gap-1.5 font-medium text-ink">
                    <FileTextIcon className="size-4 text-sub" /> {item.candidate_name || '未命名候选人'}
                  </span>
                  <span className="mt-0.5 block text-xs text-sub">{item.content_type || '未知格式'}</span>
                  {item.content_suspect && (
                    <span className="mt-1 inline-flex items-center gap-1 rounded border border-warn-border bg-warn-panel px-1.5 py-0.5 text-[11px] leading-4 text-warn-ink-deep">
                      <AlertTriangleIcon className="size-3" /> 解析异常
                    </span>
                  )}
                </td>
                <td className="px-4 py-3">
                  <span className="block font-medium text-ink">{item.owner_username || '--'}</span>
                  <span className="mt-0.5 block text-xs text-sub">{item.owner_email || '未绑定邮箱'}</span>
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sub">{fmtNum(item.chars || 0)}</td>
                <td className="px-4 py-3 whitespace-nowrap text-sub">{fmtTime(item.created_at)}</td>
                <td className="px-4 py-3">
                  <div className="flex justify-end gap-1.5">
                    <button type="button" onClick={() => void openDetail(item)} className="relative inline-flex h-8 items-center gap-1.5 rounded border border-line-soft px-2.5 text-xs font-medium text-ink after:absolute after:-inset-1.5 after:content-[''] hover:bg-mist">
                      <EyeIcon className="size-3.5" /> 查看
                    </button>
                    <button type="button" onClick={() => setPendingDelete(item)} className="relative inline-flex h-8 items-center gap-1.5 rounded border border-bad-border/60 px-2.5 text-xs font-medium text-bad after:absolute after:-inset-1.5 after:content-[''] hover:bg-bad-soft">
                      <Trash2Icon className="size-3.5" /> 删除
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 客户端分页控件（上一页 / 下一页，按接口返回的 total 计算总页数） */}
      <div className="mt-3 flex flex-wrap items-center justify-center gap-3">
        <button type="button" disabled={loading || page <= 1} onClick={goPrev} className="rounded-md border border-line-soft bg-white px-3 py-1.5 text-xs text-ink hover:bg-mist disabled:opacity-40">‹ 上一页</button>
        <span className="text-xs text-sub">第 {page} / {pageCount} 页 · 共 {fmtNum(total)} 条</span>
        <button type="button" disabled={loading || page >= pageCount} onClick={goNext} className="rounded-md border border-line-soft bg-white px-3 py-1.5 text-xs text-ink hover:bg-mist disabled:opacity-40">下一页 ›</button>
      </div>

      {/* 只读原文面板：无任何下载 / 导出 / 另存 / 打印入口 */}
      {detailTarget && (
        <AdminModal
          title={<><EyeIcon className="size-4" /> 简历原文</>}
          onClose={closeDetail}
          maxWidth="max-w-3xl"
          autoFocus={false}
        >
          <div className="flex items-start gap-2 rounded-md border border-warn-border bg-warn-panel px-3 py-2.5 text-xs leading-5 text-warn-ink-deep">
            <AlertTriangleIcon className="mt-0.5 size-4 shrink-0" />
            <div>
              <p className="font-semibold">仅可查看，不可下载</p>
              <p className="mt-0.5">下方为后端解析后的原文文本，仅供审核查阅；本页面不提供下载、导出、另存或打印入口。</p>
            </div>
          </div>

          {detail?.content_suspect && (
            <div className="mt-2 flex items-start gap-2 rounded-md border border-warn-border bg-warn-panel px-3 py-2.5 text-xs leading-5 text-warn-ink-deep">
              <AlertTriangleIcon className="mt-0.5 size-4 shrink-0" />
              <div>
                <p className="font-semibold">原文含 PDF 解析残留</p>
                <p className="mt-0.5">该 PDF 的字体缺少 Unicode 映射，解析结果里混入了字形码与内容流操作符（如 Rj、G q n P）。下方正文已自动过滤 {detail.residue_lines_removed} 行解析残留，真文字完整保留；原始文本仍原样留存于存储中，本页不再展示。</p>
              </div>
            </div>
          )}

          <div className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1.5 text-xs text-sub sm:grid-cols-3">
            <p>候选人：<span className="text-ink">{detailTarget.candidate_name || '未命名候选人'}</span></p>
            <p>所属用户：<span className="text-ink">{detailTarget.owner_username || '--'}</span></p>
            <p>内容类型：<span className="text-ink">{detailTarget.content_type || '未知格式'}</span></p>
            <p>字符数：<span className="text-ink">{fmtNum(detail?.chars ?? detailTarget.chars ?? 0)}</span></p>
            <p>上传时间：<span className="text-ink">{fmtTime(detail?.created_at || detailTarget.created_at)}</span></p>
            <p>关联归档数：<span className="text-ink">{detail ? fmtNum(detail.archived_count) : '—'}</span></p>
          </div>

          {detailLoading ? (
            <div className="mt-3 flex items-center justify-center rounded-md border border-line-soft bg-mist py-16 text-sm text-sub">
              <LoaderCircleIcon className="mr-2 size-4 animate-spin" /> 原文加载中…
            </div>
          ) : detailError ? (
            <div className="mt-3 rounded-md border border-bad-border bg-bad-soft px-4 py-3 text-sm text-bad">{detailError}</div>
          ) : (
            <pre className="mt-3 max-h-[55vh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-line-soft bg-mist p-4 text-sm leading-6 text-body">{detail?.content || '（该简历没有可展示的文本内容）'}</pre>
          )}
        </AdminModal>
      )}

      <ConfirmDialog
        open={Boolean(pendingDelete)}
        title="删除这份简历？"
        message={<>将永久删除候选人 <span className="font-medium text-ink">{pendingDelete?.candidate_name || '（未命名）'}</span> 的简历（所属用户 {pendingDelete?.owner_username || '--'}）。删除后无法从本页面恢复。</>}
        confirmLabel="确认删除"
        danger
        busy={deleting}
        onConfirm={() => void confirmDelete()}
        onCancel={() => { if (!deleting) setPendingDelete(null); }}
      />
    </div>
  );
}
