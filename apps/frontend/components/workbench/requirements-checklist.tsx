'use client';

import { useState } from 'react';
import { HelpCircleIcon } from 'lucide-react';
import type { HrAnalysis, RequirementChecklistItem } from './analysis-context';

/**
 * 岗位要求逐条核对。
 *
 * 为什么要有这一块：后端一直在逐条判定（符合/不符合/未写明/待人工确认），但界面只给了一个总分——
 * HR 无法回答"这个分是怎么来的"。更关键的是"未写明"这一类：按产品铁律「没写 ≠ 没有」，
 * 系统不判定、不扣分，但必须让人看见并去核实，否则这条规则在界面上等于不存在。
 *
 * 默认只列需要人工判断的条目（不符合 / 待人工确认），其余可展开；计数永远显示，
 * 所以"藏起来"不等于"看不到"。
 */

const STATUS_META: Record<string, { label: string; className: string; rank: number }> = {
  not_met: { label: '不符合', className: 'border-bad-border bg-bad-soft text-bad', rank: 0 },
  manual_review: { label: '待人工确认', className: 'border-warn-border bg-warn-soft text-warn-ink-deep', rank: 1 },
  not_mentioned: { label: '未写明', className: 'border-line bg-mist text-sub', rank: 2 },
  met: { label: '符合', className: 'border-good-border bg-good-soft text-good-deep', rank: 3 },
};

const TIER_LABELS: Record<string, string> = {
  A: 'A 学历层级',
  B: 'B 岗位硬门槛',
  C: 'C 生活/通用类',
};

function metaOf(status: string) {
  return STATUS_META[status] ?? { label: status || '未判定', className: 'border-line bg-mist text-sub', rank: 2 };
}

function StatusPill({ item }: { item: RequirementChecklistItem }) {
  const meta = metaOf(item.status);
  // 副标题只补"徽标本身答不了的那个问题"：学历这类条目文字上确实写了，但学信网/证书原件
  // 机器核不了，所以状态才是待人工确认。这里不重复写「已写明符合」——那句话是徽标矛盾的来源，
  // 「写了什么」由旁边「依据」列的证据原文回答。
  const sub =
    item.status === 'manual_review' && item.evidence_state === 'matched'
      ? '真实性需核实'
      : '';
  return (
    <span className="inline-flex flex-col items-start gap-1">
      <span className={`inline-block rounded border px-1.5 py-0.5 text-[11px] font-medium ${meta.className}`}>
        {meta.label}
      </span>
      {sub && <span className="text-[10px] leading-4 text-sub">{sub}</span>}
    </span>
  );
}

export function RequirementsChecklist({ analysis }: { analysis: HrAnalysis }) {
  const [showAll, setShowAll] = useState(false);
  const items = (analysis.requirements_checklist ?? []).map((item) => ({
    ...item,
    status: item.effective_status || item.status,
  }));
  const undetermined = analysis.undetermined_requirements ?? [];

  if (items.length === 0 && undetermined.length === 0) return null;

  const counts = {
    not_met: items.filter((item) => item.status === 'not_met').length,
    manual_review: items.filter((item) => item.status === 'manual_review').length,
    not_mentioned: items.filter((item) => item.status === 'not_mentioned').length,
    met: items.filter((item) => item.status === 'met').length,
  };
  const actionable = items
    .filter((item) => item.status === 'not_met' || item.status === 'manual_review')
    .sort((a, b) => metaOf(a.status).rank - metaOf(b.status).rank);
  const hiddenCount = items.length - actionable.length;
  const visible = showAll ? [...items].sort((a, b) => metaOf(a.status).rank - metaOf(b.status).rank) : actionable;

  return (
    <div className="mt-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
        <span className="text-sub">共 {items.length} 条要求</span>
        {counts.not_met > 0 && <span className="font-medium text-bad">不符合 {counts.not_met}</span>}
        {counts.manual_review > 0 && <span className="font-medium text-warn">待人工确认 {counts.manual_review}</span>}
        {counts.not_mentioned > 0 && <span className="text-sub">未写明 {counts.not_mentioned}</span>}
        {counts.met > 0 && <span className="text-good-deep">符合 {counts.met}</span>}
        {hiddenCount > 0 && (
          <button
            type="button"
            onClick={() => setShowAll((visible) => !visible)}
            className="ml-auto rounded-md border border-line-soft bg-white px-2.5 py-1 text-[11px] font-medium text-ink hover:bg-soft active:scale-[0.98]"
          >
            {showAll ? '只看需要人工判断的' : `显示全部 ${items.length} 条`}
          </button>
        )}
      </div>

      <p className="mt-2 text-xs leading-5 text-sub">
        四态口径：符合＝有对应证据；不符合＝有明确冲突；未写明＝简历未体现，不等于不符合；待人工确认＝证据不足或需核实。相关技术线索只说明有关联，不能直接认定熟练。
      </p>

      {visible.length > 0 ? (
        <div className="mt-3 overflow-x-auto rounded-md border border-line-soft">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="bg-mist">
                <th className="border-b border-line-soft px-3 py-2 text-left text-xs font-semibold text-sub">岗位要求</th>
                <th className="w-28 border-b border-line-soft px-3 py-2 text-left text-xs font-semibold text-sub">判定</th>
                <th className="border-b border-line-soft px-3 py-2 text-left text-xs font-semibold text-sub">依据</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((item, index) => (
                <tr key={item.id || index} className="border-b border-line-soft align-top last:border-b-0">
                  <td className="px-3 py-2 text-xs leading-5 text-ink">
                    {item.gate_tier && TIER_LABELS[item.gate_tier] && (
                      <span className="mr-1.5 inline-block rounded bg-brand-soft px-1.5 py-0.5 text-[10px] text-brand">
                        {TIER_LABELS[item.gate_tier]}
                      </span>
                    )}
                    {item.text}
                  </td>
                  <td className="px-3 py-2">
                    <StatusPill item={item} />
                  </td>
                  <td className="px-3 py-2 text-xs leading-5 text-body">
                    {item.resume_basis ? (
                      <span title={item.resume_basis}>
                        {item.resume_basis.length > 80 ? `${item.resume_basis.slice(0, 80)}…` : item.resume_basis}
                      </span>
                    ) : (
                      <span className="text-sub">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="mt-3 rounded-md border border-good-border-soft bg-good-soft px-3 py-2 text-xs text-good-deep">
          当前筛选下没有「不符合」或「待人工确认」条目；请结合「未写明」计数查看尚未体现的要求。
        </p>
      )}

      {undetermined.length > 0 && (
        <div className="mt-3 rounded-md border border-warn-border-soft bg-warn-soft px-4 py-3">
          <p className="flex items-center gap-2 text-xs font-medium text-warn-ink-deep">
            <HelpCircleIcon className="size-3.5 shrink-0" />
            系统不做判定，请人工确认（{undetermined.length} 项）
          </p>
          <ul className="mt-2 space-y-1.5">
            {undetermined.map((entry, index) => (
              <li key={`${entry.type}-${index}`} className="text-xs leading-5 text-body">
                <span className="mr-1.5 inline-block rounded border border-warn-border bg-white px-1.5 py-0.5 text-[10px] text-warn-ink-deep">
                  {entry.type}
                </span>
                {entry.text}
                {entry.note && <span className="ml-1 text-sub">（{entry.note}）</span>}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[11px] leading-5 text-sub">
            这类要求（学历形式、年龄、出差意愿等）机器无法从简历确认，系统不替 HR 下结论，也不据此扣分。
          </p>
        </div>
      )}

      {counts.not_mentioned > 0 && !showAll && (
        <p className="mt-2 text-[11px] leading-5 text-sub">
          另有 {counts.not_mentioned} 条「未写明」：简历里没有提到，系统按「没写 ≠ 没有」不判不符合、不扣分，建议面试时核实（点上方按钮可查看全部）。
        </p>
      )}
    </div>
  );
}
