'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { ArchiveIcon, ArrowLeftIcon, DownloadIcon, FileTextIcon } from 'lucide-react';
import { ARCHIVE_ARRIVAL_PARAM, ArchiveArrival, archiveRowId } from '@/components/workbench/archive-arrival';
import ArchiveMotionControl from '@/components/workbench/archive-motion-control';
import ReportCopier from '@/components/workbench/report-copier';

const PREVIEW_PATH = '/motion-preview';
const DEMO_RECORD = { archive_id: 'preview-file', candidate_name: '林知远', category: '产品', job_title: '产品经理', status: 'active' as const };

export default function MotionPreview() {
  const inLibrary = useSearchParams().has(ARCHIVE_ARRIVAL_PARAM);
  const [receipt, setReceipt] = useState(DEMO_RECORD);
  const [fail, setFail] = useState(false);
  const [copier, setCopier] = useState<'pending' | 'success' | 'error' | null>(null);
  const printTimer = useRef<number | null>(null);

  const stopPrint = () => {
    if (printTimer.current !== null) window.clearTimeout(printTimer.current);
    printTimer.current = null;
  };
  useEffect(() => stopPrint, []);

  const previewExport = () => {
    stopPrint();
    setCopier('pending');
    printTimer.current = window.setTimeout(() => {
      printTimer.current = null;
      setCopier(fail ? 'error' : 'success');
    }, 1120);
  };

  return (
    <main className="min-h-screen bg-[#f7f7f2] text-ink">
      <header className="border-b border-line-strong bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-5 py-4 sm:px-10">
          <div className="flex items-center gap-3 text-sm font-semibold"><span className="grid size-8 place-items-center bg-ink text-white"><ArchiveIcon size={16} /></span> 简历智选 <span className="font-normal text-sub">/ 动效验收</span></div>
          <label className="flex items-center gap-2 text-xs text-sub"><input type="checkbox" className="accent-[#215b43]" checked={fail} onChange={(event) => setFail(event.target.checked)} /> 模拟操作失败</label>
        </div>
      </header>

      <div className="mx-auto max-w-6xl px-5 py-8 sm:px-10 sm:py-14">
        <div className="flex flex-wrap items-end justify-between gap-5 border-b border-line-strong pb-7">
          <div>
            <p className="font-mono text-[11px] tracking-[.2em] text-sub">{inLibrary ? '03 / TALENT LIBRARY' : '02 / ANALYSIS'}</p>
            <h1 className="mt-3 text-3xl font-semibold tracking-tight sm:text-5xl">{inLibrary ? '候选人才库' : '候选人分析报告'}</h1>
            <p className="mt-3 text-sm text-sub">{inLibrary ? '查看刚归档的候选人及岗位分类。' : '演示数据 · 不写入人才库，不下载真实报告。'}</p>
          </div>
          {inLibrary ? (
            <Link href={PREVIEW_PATH} className="inline-flex h-[38px] items-center gap-2 border border-control-border bg-white px-4 text-sm hover:bg-control-hover"><ArrowLeftIcon size={16} /> 返回分析报告</Link>
          ) : (
            <div className="flex flex-wrap gap-2">
              <button type="button" onClick={previewExport} className="inline-flex h-[38px] items-center gap-2 border border-control-border bg-control px-4 text-[13px] font-medium hover:bg-control-hover"><DownloadIcon size={16} /> 导出报告</button>
              <ArchiveMotionControl
                candidate={DEMO_RECORD.candidate_name}
                jobTitle={DEMO_RECORD.job_title}
                archivePath={PREVIEW_PATH}
                onArchive={async (category) => {
                  await new Promise<void>((resolve) => window.setTimeout(resolve, 590));
                  if (fail) throw new Error('演示：暂时无法写入人才库。');
                  return { record: { ...DEMO_RECORD, category: category || DEMO_RECORD.job_title }, created: true };
                }}
                onArchived={setReceipt}
              />
            </div>
          )}
        </div>

        {inLibrary ? (
          <section className="mt-6 overflow-x-auto border border-line-strong bg-white" aria-label="人才档案">
            <ArchiveArrival ready />
            <table className="w-full min-w-72 text-left text-sm">
              <thead className="border-b border-line-soft bg-mist text-xs font-medium text-sub"><tr><th scope="col" className="p-4">候选人</th><th scope="col" className="p-4">岗位分类</th><th scope="col" className="p-4">状态</th></tr></thead>
              <tbody>
                <tr id={archiveRowId(receipt.archive_id)} tabIndex={-1} className="border-b border-line-soft">
                  <th scope="row" className="p-4 font-medium"><span className="flex items-center gap-3"><span data-archive-file className="grid size-8 place-items-center border border-line-strong bg-[#f4f3e9] text-[#215b43]"><FileTextIcon size={15} /></span>{receipt.candidate_name}</span></th>
                  <td className="p-4">{receipt.category}</td><td className="p-4 text-good-deep">已归档</td>
                </tr>
                <tr><th scope="row" className="p-4 font-medium"><span className="flex items-center gap-3"><span className="grid size-8 place-items-center border border-line-soft bg-mist"><FileTextIcon size={15} /></span>陈予晴</span></th><td className="p-4">运营</td><td className="p-4 text-sub">已归档</td></tr>
              </tbody>
            </table>
          </section>
        ) : (
          <section className="mt-6 border border-line-strong bg-white" aria-label="候选人摘要">
            <div className="flex items-center justify-between gap-4 border-b border-line-soft px-5 py-4 text-sm"><strong>{DEMO_RECORD.candidate_name}</strong><span className="text-sub">{DEMO_RECORD.job_title}</span></div>
            <div className="grid sm:grid-cols-[1fr_2fr]">
              <div className="border-line-soft p-5 sm:border-r sm:p-8"><p className="text-xs text-sub">综合得分</p><p className="mt-2 text-6xl font-semibold tracking-tight">86 <span className="text-base font-normal text-sub">/ 100</span></p></div>
              <div className="border-t border-line-soft p-5 sm:border-t-0 sm:p-8"><p className="text-xs text-sub">候选人摘要</p><h2 className="mt-2 text-xl font-semibold">建议进入面试</h2><p className="mt-2 max-w-lg text-sm leading-6 text-sub">负责过从需求研究到上线复盘的完整产品周期。建议重点核实指标拆解能力和跨团队协作经验。</p></div>
            </div>
          </section>
        )}
      </div>

      <ReportCopier open={copier !== null} format="pdf" exporting={copier === 'pending'} error={copier === 'error' ? '演示：报告未生成。' : ''} completed={copier === 'success'} onClose={() => { stopPrint(); setCopier(null); }} preview />
    </main>
  );
}
