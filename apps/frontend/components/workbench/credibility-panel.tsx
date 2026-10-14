'use client';

import { useState } from 'react';
import { CheckCircle2Icon, ChevronDownIcon, ChevronUpIcon, ShieldAlertIcon } from 'lucide-react';
import type { Credibility, CredibilityFinding, HrAnalysis } from './analysis-context';

/**
 * 表述核验面板（只读）。
 *
 * 为什么要有这一块：核验层（`deception_check.py`，12 条规则）跑了很久，但只写日志 ——
 * HR 在界面上完全看不到。核验结果不进分数（不扣分、不封顶），它的价值全在"HR 有没有看到"：
 * 一份简历有没有夸大的量化结果、有没有照抄 JD、清单结论和报告表述是否打架，
 * 这些是面试该问的问题清单，而不是系统该替他下的结论。
 *
 * 版面纪律（HR 是扫一眼，不是读文档）：
 *   ① 面板默认**整体收起**：常显的只有一行——已跑多少条规则 + 各级计数（红级加重）+ 「只作提示，不参与评分」；
 *   ② 展开后每条也只有**一行标题 + 一行"该问什么"**（截断）；说明、证据原文、完整问法收进单条的「依据」里；
 *   ③ 完整的 notice 与口径版本只在展开时出现，不占默认版面；
 *   ④ 0 命中时只有一行绿色结论——"跑过且没问题"和"没跑"必须区分得开。
 * 导出件不做收纳：文件离开界面后没有展开按钮，导出仍是全量（见 report-export.tsx）。
 *
 * `credibility` 只在后端 `DECEPTION_CHECK_MODE=on` 时才会出现在 payload 里：
 * 影子模式/关闭时这里渲染 null，界面与之前完全一致（不会突然多出一个空区块）。
 */

const LEVEL_META: Record<string, { label: string; className: string }> = {
  red_flag: { label: '需优先核实', className: 'border-bad-border bg-bad-soft text-bad' },
  review: { label: '待核实', className: 'border-warn-border bg-warn-soft text-warn-ink-deep' },
  info: { label: '提示', className: 'border-line bg-mist text-sub' },
};

const LEVEL_ORDER = ['red_flag', 'review', 'info'];

const SOURCE_LABELS: Record<string, string> = {
  resume: '简历原文',
  report: '报告表述',
};

function levelMeta(level: string) {
  return LEVEL_META[level] ?? { label: level || '提示', className: 'border-line bg-mist text-sub' };
}

/** 单条：默认只占两行（标题 + 截断的问法），依据需要时再展开。 */
function FindingRow({ finding }: { finding: CredibilityFinding }) {
  const [open, setOpen] = useState(false);
  const meta = levelMeta(finding.level);
  const source = SOURCE_LABELS[String(finding.source || 'report')] || '报告表述';
  return (
    <li className="rounded-md border border-line-soft bg-white px-3 py-2">
      <div className="flex items-start gap-2">
        <span className={`mt-0.5 inline-block shrink-0 rounded border px-1.5 py-0.5 text-[11px] font-medium ${meta.className}`}>
          {meta.label}
        </span>
        <span className="mt-0.5 shrink-0 rounded border border-line bg-mist px-1.5 py-0.5 text-[10px] text-sub">{source}</span>
        <span className="min-w-0 flex-1 text-xs font-medium leading-5 text-ink">{finding.title}</span>
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className="shrink-0 rounded px-1.5 py-0.5 text-[11px] text-sub hover:bg-soft hover:text-ink"
        >
          {open ? '收起' : '依据'}
        </button>
      </div>
      {!open && finding.ask && (
        <p className="mt-1 truncate text-[11px] leading-5 text-warn-ink-deep" title={finding.ask}>
          问：{finding.ask}
        </p>
      )}
      {open && (
        <div className="mt-1.5 space-y-1.5">
          {finding.detail && <p className="text-[11px] leading-5 text-body">{finding.detail}</p>}
          {finding.evidence && (
            <p className="rounded border border-line-soft bg-soft px-2 py-1 text-[11px] leading-5 break-words text-sub">
              证据：{finding.evidence}
            </p>
          )}
          {finding.quote && <p className="text-[11px] leading-5 break-words text-sub">原文引用：{finding.quote}</p>}
          {finding.ask && (
            <p className="text-[11px] leading-5 text-warn-ink-deep">
              <span className="font-medium">面试该问：</span>
              {finding.ask}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

export function CredibilityPanel({ analysis }: { analysis: HrAnalysis }) {
  const [open, setOpen] = useState(false);
  const credibility: Credibility | undefined = analysis.credibility;
  if (!credibility || credibility.enabled === false) return null;

  const findings = credibility.findings ?? [];
  const counts = credibility.counts ?? { red_flag: 0, review: 0, info: 0 };
  const bySource = credibility.by_source;
  const sorted = [...findings].sort((a, b) => LEVEL_ORDER.indexOf(a.level) - LEVEL_ORDER.indexOf(b.level));

  // 0 命中：一行绿色结论，不需要收纳（没有明细可收）
  if (findings.length === 0) {
    return (
      <div className="mt-4" data-credibility-panel="empty">
        <p className="flex items-center gap-2 rounded-md border border-good-border-soft bg-good-soft px-3 py-2 text-xs text-good-deep">
          <CheckCircle2Icon className="size-3.5 shrink-0" />
          {credibility.checks_run} 条核验规则全部跑完，未发现需要人工核实的表述问题。
        </p>
        <p className="mt-1.5 text-[10px] leading-4 text-sub">核验口径：{credibility.version}（只读字段，不参与分数与等级）</p>
      </div>
    );
  }

  return (
    <div className="mt-4" data-credibility-panel={open ? 'open' : 'collapsed'}>
      {/* 常显只有这一行：计数 + 一句纪律 + 展开开关 */}
      <div
        className={`flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border px-3 py-2 ${
          counts.red_flag > 0 ? 'border-bad-border bg-bad-soft' : 'border-line-soft bg-soft'
        }`}
      >
        <ShieldAlertIcon className={`size-3.5 shrink-0 ${counts.red_flag > 0 ? 'text-bad' : 'text-sub'}`} />
        <span className="text-xs text-sub">已跑 {credibility.checks_run} 条规则</span>
        {counts.red_flag > 0 && <span className="text-xs font-semibold text-bad">需优先核实 {counts.red_flag}</span>}
        {counts.review > 0 && <span className="text-xs font-medium text-warn-ink-deep">待核实 {counts.review}</span>}
        {counts.info > 0 && <span className="text-xs text-sub">提示 {counts.info}</span>}
        <span className="text-[11px] text-sub">只作提示，不参与评分</span>
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className="ml-auto inline-flex items-center gap-1 rounded-md border border-line-soft bg-white px-2.5 py-1 text-[11px] font-medium text-ink hover:bg-soft active:scale-[0.98]"
        >
          {open ? '收起明细' : `展开明细 ${findings.length} 条`}
          {open ? <ChevronUpIcon className="size-3" /> : <ChevronDownIcon className="size-3" />}
        </button>
      </div>

      {open && (
        <>
          <p className="mt-2 text-[11px] leading-5 text-sub">
            {credibility.notice || '核验结果只用于提示，不参与评分，也不构成对候选人的任何结论。'}
          </p>
          <ul className="mt-2 space-y-1.5">
            {sorted.map((finding, index) => (
              <FindingRow key={`${finding.code}-${index}`} finding={finding} />
            ))}
          </ul>
          <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] leading-4 text-sub">
            <span>核验口径：{credibility.version}（只读字段，不参与分数与等级）</span>
            {bySource && (
              <span>
                来源：简历原文 {bySource.resume ?? 0} · 报告表述 {bySource.report ?? 0}
              </span>
            )}
          </p>
        </>
      )}
    </div>
  );
}
