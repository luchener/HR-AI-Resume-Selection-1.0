'use client';

import { useState } from 'react';
import {
  CameraIcon,
  DownloadIcon,
  FileTextIcon,
  HighlighterIcon,
  LoaderCircleIcon,
  ShieldAlertIcon,
} from 'lucide-react';
import type { HrAnalysis } from './analysis-context';
import {
  fetchResumeReviewMarkers,
  fetchResumeView,
  type ResumeReviewData,
  type ResumeReviewMarker,
} from '@/lib/api/screening';

const CATEGORY_STYLE: Record<
  ResumeReviewMarker['category'],
  { bg: string; text: string; label: string }
> = {
  strength: { bg: 'bg-[#e6f7ee]', text: 'text-[#1d7f5c]', label: '匹配亮点' },
  match: { bg: 'bg-[#eaf0fb]', text: 'text-[#3e6fd3]', label: '岗位匹配' },
  risk: { bg: 'bg-[#fdecec]', text: 'text-[#b23b4e]', label: '待核实' },
  missing: { bg: 'bg-[#f3f4f6]', text: 'text-[#6b7280]', label: '未体现' },
  verify: { bg: 'bg-[#fef6e6]', text: 'text-[#b0761a]', label: '学历待核实' },
};

function buildHighlightedSegments(
  content: string,
  annotations: ResumeReviewMarker[],
): Array<{ text: string; annotation?: ResumeReviewMarker }> {
  const sorted = [...annotations].sort((a, b) => a.start - b.start);
  const segments: Array<{ text: string; annotation?: ResumeReviewMarker }> = [];
  let cursor = 0;
  for (const annotation of sorted) {
    const start = Math.max(cursor, annotation.start);
    const end = Math.min(content.length, annotation.end);
    if (start > cursor) segments.push({ text: content.slice(cursor, start) });
    if (start < end) segments.push({ text: content.slice(start, end), annotation });
    cursor = Math.max(cursor, end);
  }
  if (cursor < content.length) segments.push({ text: content.slice(cursor) });
  return segments;
}

// ── 导出辅助：纯 HTML 块（foreignObject 嵌入 / Word .doc / PDF 打印共用） ──

function buildExportHtml(review: ResumeReviewData, rawContent: string, name: string): string {
  const escapeHtml = (s: string) =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const catBg = (c: ResumeReviewMarker['category']) =>
    CATEGORY_STYLE[c].bg === 'bg-[#e6f7ee]' ? '#e6f7ee' :
    CATEGORY_STYLE[c].bg === 'bg-[#eaf0fb]' ? '#eaf0fb' :
    CATEGORY_STYLE[c].bg === 'bg-[#fdecec]' ? '#fdecec' :
    CATEGORY_STYLE[c].bg === 'bg-[#f3f4f6]' ? '#f3f4f6' : '#fef6e6';
  const catColor = (c: ResumeReviewMarker['category']) =>
    CATEGORY_STYLE[c].text === 'text-[#1d7f5c]' ? '#1d7f5c' :
    CATEGORY_STYLE[c].text === 'text-[#3e6fd3]' ? '#3e6fd3' :
    CATEGORY_STYLE[c].text === 'text-[#b23b4e]' ? '#b23b4e' :
    CATEGORY_STYLE[c].text === 'text-[#6b7280]' ? '#6b7280' : '#b0761a';

  const segments = buildHighlightedSegments(rawContent, review.annotations);

  const rows = review.annotations
    .map((a) => `<tr>
      <td style="width:110px;font-weight:600;color:${catColor(a.category)}">${CATEGORY_STYLE[a.category].label}</td>
      <td>${escapeHtml(a.quote)}</td>
      <td style="width:210px;color:#65738a;font-size:12px">${escapeHtml(a.reason)}</td>
    </tr>`)
    .join('');

  const resumeLines = segments.map((seg) => {
    if (seg.annotation) {
      return `<span style="background:${catBg(seg.annotation.category)};border-radius:2px;padding:0 3px;font-weight:600">${escapeHtml(seg.text)}</span>`;
    }
    return escapeHtml(seg.text);
  }).join('<br>');

  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>简历重点标记·${escapeHtml(name)}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}body{font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;font-size:14px;line-height:1.8;color:#2c394f;padding:32px 36px;background:#fff;width:780px;margin:0 auto}
h1{font-size:20px;color:#253249;margin-bottom:4px}h2{font-size:15px;color:#3e6fd3;margin:18px 0 6px;border-bottom:1px solid #dce2eb;padding-bottom:4px}
.meta{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0 16px;font-size:13px;color:#65738a}.meta span b{color:#1d7f5c;font-size:22px;margin-right:3px}
table{border-collapse:collapse;width:100%;margin:6px 0 16px;font-size:13px}th,td{border:1px solid #dce2eb;padding:6px 10px;text-align:left;vertical-align:top}th{background:#eaf0fb;color:#3e6fd3;font-weight:600;font-size:12px}
.resume{white-space:pre-wrap;word-break:break-word;background:#fbfcfe;border:1px solid #e5e9ef;border-radius:6px;padding:14px;margin-top:6px;font-size:13px;line-height:1.9}
.notice{margin-top:16px;font-size:11px;color:#8190a4}
@media print{body{padding:12px 16px;width:auto}}
</style></head><body>
<h1>简历重点标记 · ${escapeHtml(name)}</h1>
<div class="meta">
  <span>综合得分：<b>${review.summary.final_score}</b></span>
  <span>招聘建议：${escapeHtml(review.summary.recommendation)}</span>
  <span>匹配重点：${review.summary.highlights} 处</span>
  <span>待核实：${review.summary.risks} 处</span>
  <span>学历待核实：${review.summary.verify_count || 0} 处</span>
</div>
<h2>标记清单</h2>
<table><thead><tr><th style="width:110px">类型</th><th>引用原文</th><th style="width:210px">HR 说明</th></tr></thead><tbody>${rows}</tbody></table>
<h2>原简历内容</h2>
<div class="resume">${resumeLines}</div>
<p class="notice">${escapeHtml(review.notice)}</p>
</body></html>`;
}

// ── SVG（foreignObject 嵌入 HTML） ──────────────────────────────────────

function buildExportSvg(review: ResumeReviewData, rawContent: string, name: string): string {
  const W = 780;
  const html = buildExportHtml(review, rawContent, name);
  // 取出 <body> 内容
  const bodyMatch = html.match(/<body>([\s\S]*)<\/body>/);
  const bodyContent = bodyMatch ? bodyMatch[1] : html;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="2400" font-family="Microsoft YaHei, PingFang SC, Arial, sans-serif">
<rect width="${W}" height="2400" fill="#ffffff"/>
<foreignObject x="0" y="0" width="${W}" height="2400">
<html xmlns="http://www.w3.org/1999/xhtml"><head><style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;font-size:14px;line-height:1.8;color:#2c394f;padding:28px 32px;width:${W - 8}px}
h1{font-size:20px;color:#253249;margin-bottom:4px}h2{font-size:15px;color:#3e6fd3;margin:16px 0 6px;border-bottom:1px solid #dce2eb;padding-bottom:4px}
.meta{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0 14px;font-size:13px;color:#65738a}
.meta span b{color:#1d7f5c;font-size:22px;margin-right:3px}
table{border-collapse:collapse;width:100%;margin:6px 0 14px;font-size:13px}th,td{border:1px solid #dce2eb;padding:6px 10px;text-align:left;vertical-align:top}th{background:#eaf0fb;color:#3e6fd3;font-weight:600;font-size:12px}
.resume{white-space:pre-wrap;word-break:break-word;background:#fbfcfe;border:1px solid #e5e9ef;border-radius:6px;padding:14px;margin-top:6px;font-size:13px;line-height:1.9}
.notice{margin-top:14px;font-size:11px;color:#8190a4}
</style></head><body>${bodyContent}</body></html>
</foreignObject>
</svg>`;
}

export default function ResumeReviewPanel({
  resumeId,
  analysis,
  candidateName,
}: {
  resumeId: string;
  analysis: HrAnalysis;
  candidateName?: string;
}) {
  const [loading, setLoading] = useState(false);
  const [rawContent, setRawContent] = useState('');
  const [review, setReview] = useState<ResumeReviewData | null>(null);
  const [error, setError] = useState('');

  function loadReview() {
    setLoading(true);
    setError('');
    Promise.all([
      fetchResumeView(resumeId),
      fetchResumeReviewMarkers(resumeId, analysis as unknown as Record<string, unknown>, candidateName),
    ])
      .then(([content, markers]) => {
        setRawContent(content || '未提供原简历内容');
        setReview(markers);
      })
      .catch((err) => setError(err instanceof Error ? err.message : '简历重点标记加载失败。'))
      .finally(() => setLoading(false));
  }

  const segments = review ? buildHighlightedSegments(rawContent, review.annotations) : [];
  const name = review ? (review.candidate_name || candidateName || '候选人') : '候选人';
  const agentTrace = analysis.agent_trace;

  // ── 导出 ───────────────────────────────────────────────────────────────

  function exportPng() {
    if (!review) return;
    const svg = buildExportSvg(review, rawContent, name);
    const blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const scale = 2;
      const canvas = document.createElement('canvas');
      canvas.width = img.naturalWidth * scale;
      canvas.height = img.naturalHeight * scale;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.scale(scale, scale);
      ctx.drawImage(img, 0, 0);
      canvas.toBlob((png) => {
        if (!png) return;
        const d = URL.createObjectURL(png);
        const a = document.createElement('a');
        a.href = d;
        a.download = `简历重点标记-${name}.png`;
        a.click();
        URL.revokeObjectURL(d);
      }, 'image/png');
    };
    img.onerror = () => URL.revokeObjectURL(url);
    img.src = url;
  }

  function exportPdf() {
    if (!review) return;
    const html = buildExportHtml(review, rawContent, name);
    const win = window.open('', '_blank', 'width=900,height=720');
    if (win) {
      win.document.write(html);
      win.document.close();
      win.focus();
      setTimeout(() => win.print(), 600);
    }
  }

  function exportWord() {
    if (!review) return;
    // Word 兼容 HTML：标准 HTML 用 .doc 扩展名，Word 直接可打开
    const html = buildExportHtml(review, rawContent, name)
      .replace('<!DOCTYPE html>', '<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word">');
    const blob = new Blob(['\ufeff', html], { type: 'application/msword;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `简历重点标记-${name}.doc`;
    a.click();
    URL.revokeObjectURL(url);
  }

  // ── Agent 分析过程 ───────────────────────────────────────────────────

  function renderAgentTrace() {
    if (!agentTrace || !agentTrace.steps) return null;
    return (
      <div className="mt-3 rounded-md border border-[#e2d6f0] bg-[#fbf8ff] p-3">
        <div className="flex items-center gap-2 text-xs font-semibold text-[#7c5da0]">
          <span>🤖</span>
          <span>Agent 分析过程</span>
        </div>
        <div className="mt-2 flex flex-col gap-1.5">
          {agentTrace.steps.map((s, i) => (
            <div key={i} className="flex items-start gap-2 text-xs">
              <span className={`mt-0.5 inline-flex size-4 items-center justify-center rounded-full shrink-0 ${
                s.status === '通过' || s.status === '完成' ? 'bg-[#e6f7ee] text-[#1d7f5c]' :
                s.status === '不通过' || s.status === '失败' ? 'bg-[#fdecec] text-[#b23b4e]' :
                'bg-[#fef6e6] text-[#b0761a]'
              }`}>
                {s.status === '通过' || s.status === '完成' ? '✓' : s.status === '不通过' || s.status === '失败' ? '✗' : '⟳'}
              </span>
              <span className="font-medium text-[#4a3a6b]">{s.step}：</span>
              <span className="text-[#6b5d87]">{s.detail}</span>
            </div>
          ))}
        </div>
        {agentTrace.requirements && agentTrace.requirements.length > 0 && (
          <div className="mt-2 pt-2 border-t border-[#e2d6f0] text-xs">
            <span className="text-[#7c5da0] font-medium">提取的岗位要求（前 5 项）：</span>
            <div className="mt-1 flex flex-wrap gap-1">
              {agentTrace.requirements.slice(0, 5).map((r: string, i: number) => (
                <span key={i} className="rounded px-1.5 py-0.5 bg-[#f0ebfa] text-[#7c5da0] text-[11px]">
                  {r.length > 28 ? r.slice(0, 28) + '…' : r}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <section className="mt-6 rounded-md border border-[#dce2eb] bg-white p-5 sm:p-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold text-[#253249]">
            <HighlighterIcon className="size-4 text-[#3e6fd3]" />
            简历重点标记
          </h2>
          <p className="mt-1 text-xs text-[#8190a4]">
            根据已有分析结果在原简历上标记岗位匹配重点与待核实信息，原简历内容未被修改。
          </p>
        </div>
        {!review && (
          <button
            type="button"
            onClick={loadReview}
            disabled={loading}
            className="inline-flex items-center gap-2 rounded-md border border-[#3e6fd3] bg-[#eaf0fb] px-3 py-2 text-xs font-medium text-[#3e6fd3] disabled:opacity-60"
          >
            {loading ? <LoaderCircleIcon className="size-4 animate-spin" /> : <FileTextIcon className="size-4" />}
            {loading ? '正在生成标记' : '查看简历重点'}
          </button>
        )}
      </div>

      {error && <p className="mt-4 rounded-md border border-[#f0c9c9] bg-[#fff5f5] p-3 text-xs text-[#b23b4e]">{error}</p>}

      {renderAgentTrace()}

      {review && (
        <div className="mt-5">
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_290px]">
            <div className="rounded-md border border-[#e5e9ef] bg-[#fbfcfe] p-4">
              <div className="mb-2 flex flex-wrap gap-2">
                {Object.entries(CATEGORY_STYLE).map(([key, style]) => (
                  <span key={key} className={`rounded px-2 py-0.5 text-[11px] font-medium ${style.bg} ${style.text}`}>
                    {style.label}
                  </span>
                ))}
              </div>
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap font-sans text-sm leading-6 text-[#2c394f]">
                {segments.map((segment, index) =>
                  segment.annotation ? (
                    <mark key={index} className={`rounded px-0.5 ${CATEGORY_STYLE[segment.annotation.category].bg}`}>
                      {segment.text}
                    </mark>
                  ) : (
                    <span key={index}>{segment.text}</span>
                  ),
                )}
              </pre>
            </div>

            <aside className="space-y-3">
              <div className="rounded-md border border-[#e5e9ef] p-4">
                <p className="text-xs font-medium text-[#66758b]">综合得分</p>
                <p className="mt-1 text-2xl font-semibold text-[#1d7f5c]">{review.summary.final_score}</p>
                <p className="mt-1 text-xs text-[#65738a]">{review.summary.recommendation}</p>
              </div>
              <div className="rounded-md border border-[#e5e9ef] p-4">
                <p className="text-xs font-medium text-[#66758b]">匹配重点</p>
                <p className="mt-1 text-lg font-semibold text-[#3e6fd3]">{review.summary.highlights} 处</p>
                <p className="text-xs text-[#65738a]">待核实 {review.summary.risks} 处 · 学历待核实 {review.summary.verify_count || 0} 处</p>
              </div>
              <div className="rounded-md border border-[#e5e9ef] p-4">
                <p className="flex items-center gap-1.5 text-xs font-medium text-[#66758b]">
                  <ShieldAlertIcon className="size-3.5 text-[#995c87]" />
                  说明
                </p>
                <p className="mt-1 text-xs leading-5 text-[#65738a]">{review.notice}</p>
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                <button
                  type="button"
                  onClick={exportPng}
                  className="inline-flex items-center justify-center gap-1 rounded-md bg-[#3e6fd3] px-2 py-1.5 text-[11px] font-medium text-white hover:bg-[#2f5cb8]"
                >
                  <CameraIcon className="size-3" />
                  导出图片
                </button>
                <button
                  type="button"
                  onClick={exportPdf}
                  className="inline-flex items-center justify-center gap-1 rounded-md border border-[#d5dde9] bg-white px-2 py-1.5 text-[11px] font-medium text-[#2c394f] hover:bg-[#f3f4f6]"
                >
                  <DownloadIcon className="size-3" />
                  打印 PDF
                </button>
                <button
                  type="button"
                  onClick={exportWord}
                  className="inline-flex items-center justify-center gap-1 rounded-md border border-[#d5dde9] bg-white px-2 py-1.5 text-[11px] font-medium text-[#2c394f] hover:bg-[#f3f4f6]"
                >
                  <DownloadIcon className="size-3" />
                  导出 Word
                </button>
              </div>
              <p className="text-[10px] text-[#8190a4] leading-4">
                PDF 通过浏览器打印对话框导出，请选择「另存为 PDF」。
              </p>
            </aside>
          </div>
        </div>
      )}
    </section>
  );
}
