'use client';

import { useMemo, useState } from 'react';
import { ChevronDownIcon, ChevronRightIcon, HelpCircleIcon } from 'lucide-react';
import type { HrAnalysis, RequirementChecklistItem } from './analysis-context';

/**
 * 岗位要求逐条核对。
 *
 * 四态口径（2026-11-04 统一）：已满足 / 部分满足 / 未提及 / 不符合。
 * - 已满足：有直接原文，覆盖要求全部条件；
 * - 部分满足：有原文但只覆盖部分条件，或证据不足、需人工核实（原「待人工确认」）；
 * - 未提及：简历没有相关信息，不等于不符合，不扣分；
 * - 不符合：简历写了相反或明确不足的信息（原「不符合」），是唯一有反证的状态。
 *
 * 各档位默认收起。展开分组后显示要求全文，点击条目可展开判定依据。
 */

const STATUS_ORDER = ['not_met', 'manual_review', 'not_mentioned', 'met'] as const;

const STATUS_META: Record<string, { label: string; className: string; dot: string }> = {
  not_met: { label: '不符合', className: 'border-bad-border bg-bad-soft text-bad', dot: 'bg-bad' },
  manual_review: { label: '部分满足', className: 'border-warn-border bg-warn-soft text-warn-ink-deep', dot: 'bg-warn' },
  not_mentioned: { label: '未提及', className: 'border-line bg-mist text-sub', dot: 'bg-sub/60' },
  met: { label: '已满足', className: 'border-good-border bg-good-soft text-good-deep', dot: 'bg-good' },
};

const TIER_ORDER = ['A', 'B', 'C'] as const;
const ROW_LIMIT = 8;
const UNDETERMINED_GROUP = 'undetermined';
const TIER_LABELS: Record<string, string> = {
  A: 'A 学历层级',
  B: 'B 岗位硬门槛',
  C: 'C 生活 / 通用类',
};
const TIER_NOTES: Record<string, string> = {
  A: '学历、院校、专业等准入条件',
  B: '岗位职责与专业技能门槛',
  C: '地点、出差、年龄等通用条件',
};

function metaOf(status: string) {
  return STATUS_META[status] ?? { label: status || '未判定', className: 'border-line bg-mist text-sub', dot: 'bg-sub/60' };
}

function statusOf(item: RequirementChecklistItem) {
  return item.effective_status || item.status;
}

function StatusPill({ status }: { status: string }) {
  const meta = metaOf(status);
  return (
    <span className={`inline-block shrink-0 rounded border px-1.5 py-0.5 text-[11px] font-medium ${meta.className}`}>
      {meta.label}
    </span>
  );
}

function Counts({ items }: { items: RequirementChecklistItem[] }) {
  const counts = useMemo(() => {
    const result: Record<string, number> = { not_met: 0, manual_review: 0, not_mentioned: 0, met: 0 };
    for (const item of items) result[statusOf(item)] = (result[statusOf(item)] || 0) + 1;
    return result;
  }, [items]);
  return (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
      {STATUS_ORDER.map((status) => counts[status] > 0 && (
        <span key={status} className="inline-flex items-center gap-1 text-sub">
          <span className={`size-1.5 rounded-full ${metaOf(status).dot}`} />
          {metaOf(status).label} {counts[status]}
        </span>
      ))}
    </span>
  );
}

function GroupCard({
  tier,
  items,
  open,
  onToggle,
  initiallyExpanded,
}: {
  tier: string;
  items: RequirementChecklistItem[];
  open: boolean;
  onToggle: () => void;
  initiallyExpanded: boolean;
}) {
  const [showAllRows, setShowAllRows] = useState(initiallyExpanded);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const visibleItems = showAllRows ? items : items.slice(0, ROW_LIMIT);

  const toggleRow = (key: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <section className="overflow-hidden rounded-md border border-line-soft bg-white">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-3 bg-mist/60 px-3 py-2.5 text-left hover:bg-mist"
      >
        {open ? <ChevronDownIcon className="size-3.5 shrink-0 text-sub" /> : <ChevronRightIcon className="size-3.5 shrink-0 text-sub" />}
        <span className="shrink-0 text-xs font-semibold text-ink">{TIER_LABELS[tier] || tier}</span>
        <span className="hidden min-w-0 truncate text-[11px] text-sub sm:inline">{TIER_NOTES[tier] || ''}</span>
        <span className="ml-auto flex items-center gap-3">
          <Counts items={items} />
          <span className="text-[11px] text-sub">{open ? '收起' : '展开'}</span>
        </span>
      </button>
      {open && (
        <ul className="divide-y divide-line-soft">
          {visibleItems.map((item, index) => {
            const key = item.id || `${tier}-${index}`;
            const isOpen = expanded.has(key);
            const status = statusOf(item);
            const needsCheck = status === 'manual_review' && item.evidence_state === 'matched';
            return (
              <li key={key}>
                <button
                  type="button"
                  onClick={() => toggleRow(key)}
                  aria-expanded={isOpen}
                  className="flex w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-mist/40"
                >
                  <span className="min-w-0 flex-1 text-xs leading-5 text-ink [overflow-wrap:anywhere]">{item.text}</span>
                  <StatusPill status={status} />
                  <span className="mt-0.5 shrink-0 text-sub">
                    {isOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
                  </span>
                </button>
                {isOpen && (
                  <div className="border-t border-line-soft bg-mist/30 px-3 py-2.5 text-xs leading-5">
                    <p className="text-[11px] font-medium text-sub">判定依据</p>
                    <p className="mt-1 text-body [overflow-wrap:anywhere]">
                      {item.evidence_label || (item.resume_basis ? item.resume_basis : '简历未提供可核验的原文，不等于候选人不具备，建议面试核实。')}
                    </p>
                    {item.resume_basis && item.evidence_label && (
                      <p className="mt-1 text-sub [overflow-wrap:anywhere]">原文：{item.resume_basis}</p>
                    )}
                    {status === 'not_mentioned' && (
                      <p className="mt-1 text-[11px] text-sub">简历未提及，不扣分，建议面试时核实。</p>
                    )}
                    {needsCheck && (
                      <p className="mt-1 text-[11px] text-warn">原文有对应表述，但真实性（证书 / 学历 / 履历）需人工核验。</p>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {open && items.length > ROW_LIMIT && (
        <div className="border-t border-line-soft px-3 py-2 text-center">
          <button
            type="button"
            onClick={() => setShowAllRows((visible) => !visible)}
            className="rounded-md border border-line-soft bg-white px-2.5 py-1 text-[11px] font-medium text-ink hover:bg-soft active:scale-[0.98]"
          >
            {showAllRows ? `收起，只显示前 ${ROW_LIMIT} 条` : `显示全部 ${items.length} 条`}
          </button>
        </div>
      )}
    </section>
  );
}

export function RequirementsChecklist({ analysis }: { analysis: HrAnalysis }) {
  const items = analysis.requirements_checklist;
  const undetermined = analysis.undetermined_requirements ?? [];
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const [bulkAction, setBulkAction] = useState({ revision: 0, expand: false });

  const groups = useMemo(() => {
    const byTier = new Map<string, RequirementChecklistItem[]>();
    for (const item of items ?? []) {
      const tier = item.gate_tier && TIER_LABELS[item.gate_tier] ? item.gate_tier : '其它';
      if (!byTier.has(tier)) byTier.set(tier, []);
      byTier.get(tier)!.push(item);
    }
    return [...byTier.entries()]
      .sort((a, b) => {
        const ai = TIER_ORDER.indexOf(a[0] as (typeof TIER_ORDER)[number]);
        const bi = TIER_ORDER.indexOf(b[0] as (typeof TIER_ORDER)[number]);
        return (ai === -1 ? 9 : ai) - (bi === -1 ? 9 : bi);
      })
      .map(([tier, groupItems]) => ({
        tier,
        items: [...groupItems].sort(
          (a, b) => STATUS_ORDER.indexOf(statusOf(a) as (typeof STATUS_ORDER)[number]) - STATUS_ORDER.indexOf(statusOf(b) as (typeof STATUS_ORDER)[number]),
        ),
      }));
  }, [items]);

  const groupKeys = groups.map(({ tier }) => tier);
  if (undetermined.length > 0) groupKeys.push(UNDETERMINED_GROUP);
  if (groupKeys.length === 0) return null;

  const allOpen = groupKeys.every((key) => openGroups.has(key));
  const undeterminedOpen = openGroups.has(UNDETERMINED_GROUP);
  const toggleGroup = (key: string) => {
    setOpenGroups((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleAll = () => {
    const expand = !allOpen;
    setOpenGroups(new Set(expand ? groupKeys : []));
    // Keep individual row settings on manual toggles, and reset them on a bulk action.
    setBulkAction(({ revision }) => ({ revision: revision + 1, expand }));
  };

  return (
    <div className="mt-4 space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
        <span className="text-sub">共 {items?.length ?? 0} 条要求</span>
        <Counts items={items ?? []} />
        <button
          type="button"
          onClick={toggleAll}
          className="ml-auto rounded-md border border-line-soft bg-white px-2.5 py-1 text-[11px] font-medium text-ink hover:bg-soft active:scale-[0.98]"
        >
          {allOpen ? '全部收起' : '全部展开'}
        </button>
      </div>

      {groups.map((group) => (
        <GroupCard
          key={`${group.tier}-${bulkAction.revision}`}
          tier={group.tier}
          items={group.items}
          open={openGroups.has(group.tier)}
          onToggle={() => toggleGroup(group.tier)}
          initiallyExpanded={bulkAction.expand}
        />
      ))}

      {undetermined.length > 0 && (
        <section className="overflow-hidden rounded-md border border-warn-border-soft bg-warn-soft/40">
          <button
            type="button"
            onClick={() => toggleGroup(UNDETERMINED_GROUP)}
            aria-expanded={undeterminedOpen}
            className="flex w-full items-center gap-2 px-3 py-2.5 text-left"
          >
            {undeterminedOpen ? <ChevronDownIcon className="size-3.5 text-sub" /> : <ChevronRightIcon className="size-3.5 text-sub" />}
            <HelpCircleIcon className="size-3.5 shrink-0 text-warn" />
            <span className="text-xs font-medium text-warn-ink-deep">系统不做判定，请人工确认（{undetermined.length} 项）</span>
            <span className="ml-auto text-[11px] text-sub">{undeterminedOpen ? '收起' : '展开'}</span>
          </button>
          {undeterminedOpen && (
            <div className="border-t border-warn-border-faint px-3 py-2.5">
              <ul className="space-y-1.5">
                {undetermined.map((entry, index) => (
                  <li key={`${entry.type}-${index}`} className="text-xs leading-5 text-body [overflow-wrap:anywhere]">
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
        </section>
      )}
    </div>
  );
}
