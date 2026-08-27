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
  verify: { bg: 'bg-[#fef6e6]', text: 'text-[#b0761a]', label: '待核实' },
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

  // ── 构建通用 HTML 内容（打印用） ──────────────────────────────────────

  function buildExportHtml(): string {
    if (!review) return '';
    const escapeHtml = (s: string) =>
      s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const catBg = (c: ResumeReviewMarker['category']) =>
      CATEGORY_STYLE[c].bg === 'bg-[#e6f7ee]' ? '#e6f7ee' :
      CATEGORY_STYLE[c].bg === 'bg-[#eaf0fb]' ? '#eaf0fb' :
      CATEGORY_STYLE[c].bg === 'bg-[#fdecec]' ? '#fdecec' :
      CATEGORY_STYLE[c].bg === 'bg-[#f3f4f6]' ? '#f3f4f6' : '#fef6e6';

    const rows = review.annotations
      .map((a) => `<tr><td>${CATEGORY_STYLE[a.category].label}</td><td>${escapeHtml(a.quote)}</td><td>${escapeHtml(a.reason)}</td></tr>`)
      .join('');
    const resumeHtml = segments
      .map((seg) =>
        seg.annotation
          ? `<mark style="background:${catBg(seg.annotation.category)}">${escapeHtml(seg.text)}</mark>`
          : escapeHtml(seg.text),
      )
      .join('');
    return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>简历重点标记·${escapeHtml(name)}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}body{font-family:"Microsoft YaHei",Arial,sans-serif;font-size:14px;line-height:1.8;color:#2c394f;padding:40px 48px;background:#fff;width:750px;margin:0 auto}
h1{font-size:22px;color:#253249;margin-bottom:4px}h2{font-size:16px;color:#3e6fd3;margin:20px 0 8px;border-bottom:1px solid #dce2eb;padding-bottom:4px}
.meta{display:flex;gap:20px;flex-wrap:wrap;margin:10px 0 18px;font-size:13px;color:#65738a}.meta b{color:#1d7f5c;font-size:24px;margin-right:4px}
table{border-collapse:collapse;width:100%;margin:8px 0 18px;font-size:13px}th,td{border:1px solid #dce2eb;padding:6px 10px;text-align:left}th{background:#eaf0fb;color:#3e6fd3}
.resume{white-space:pre-wrap;word-break:break-word;background:#fbfcfe;border:1px solid #e5e9ef;border-radius:6px;padding:14px;margin-top:8px;font-size:13px;line-height:1.9}
mark{border-radius:2px;padding:0 3px}.notice{margin-top:18px;font-size:11px;color:#8190a4}
@media print{body{padding:16px 20px;width:auto}}
</style></head><body>
<h1>简历重点标记 · ${escapeHtml(name)}</h1>
<div class="meta">
  <span>综合得分：<b>${review.summary.final_score}</b></span>
  <span>招聘建议：${escapeHtml(review.summary.recommendation)}</span>
  <span>匹配重点：${review.summary.highlights} 处</span>
  <span>待核实：${review.summary.risks} 处</span>
</div>
<h2>标记清单</h2>
<table><thead><tr><th style="width:120px">类型</th><th>引用原文</th><th style="width:200px">HR 说明</th></tr></thead><tbody>${rows}</tbody></table>
<h2>原简历内容</h2>
<div class="resume">${resumeHtml}</div>
<p class="notice">${escapeHtml(review.notice)}</p>
</body></html>`;
  }

  // ── 构建 SVG 字符串（用于图片导出） ────────────────────────────────────

  function buildExportSvg(): string {
    if (!review) return '';
    const W = 750, PAD = 36;
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
    const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    const lines: string[] = [];
    let y = PAD;
    const textX = PAD;
    const fontSize = 13;
    const lineH = fontSize * 1.6;

    function add(text: string, opts?: { size?: number; color?: string; weight?: string; gap?: number }) {
      const sz = opts?.size ?? fontSize;
      const c = opts?.color ?? '#2c394f';
      const w = opts?.weight ?? 'normal';
      y += (opts?.gap ?? 0) + (sz === 22 ? 4 : 0);
      lines.push(`<text x="${textX}" y="${y}" font-size="${sz}" fill="${c}" font-weight="${w}">${esc(text)}</text>`);
      y += sz * 1.6;
    }
    function addSegmented(segs: Array<{ text: string; annotation?: ResumeReviewMarker }>, opts?: { gap?: number }) {
      y += opts?.gap ?? 0;
      let x = textX;
      for (const seg of segs) {
        if (!seg.annotation) {
          lines.push(`<text x="${x}" y="${y}" font-size="${fontSize}" fill="#2c394f">${esc(seg.text)}</text>`);
          x += Math.max(0.5 * fontSize * seg.text.length, fontSize);
        } else {
          const bg = catBg(seg.annotation.category);
          const pad = 3;
          const tw = Math.max(0.5 * fontSize * seg.text.length, fontSize);
          lines.push(`<rect x="${x - pad}" y="${y - fontSize - 1}" width="${tw + pad * 2}" height="${fontSize + 2}" rx="2" fill="${bg}"/>`);
          lines.push(`<text x="${x}" y="${y}" font-size="${fontSize}" fill="${catColor(seg.annotation.category)}" font-weight="600">${esc(seg.text)}</text>`);
          x += tw;
        }
      }
      y += lineH;
    }
    function addTableHeader() {
      const cy = y - 2;
      const cols = [
        { x: textX, w: 100, label: '类型' },
        { x: textX + 108, w: 340, label: '引用原文' },
        { x: textX + 456, w: W - PAD * 2 - 456, label: 'HR 说明' },
      ];
      for (const col of cols) {
        lines.push(`<rect x="${col.x}" y="${cy - 16}" width="${col.w}" height="20" fill="#eaf0fb" stroke="#dce2eb" stroke-width="1"/>`);
        lines.push(`<text x="${col.x + 6}" y="${cy}" font-size="12" fill="#3e6fd3" font-weight="600">${esc(col.label)}</text>`);
      }
      y += 20;
    }
    function addTableRow(a: ResumeReviewMarker) {
      const cy = y - 4;
      const cols = [
        { x: textX, w: 100, text: CATEGORY_STYLE[a.category].label, color: catColor(a.category), weight: '600' },
        { x: textX + 108, w: 340, text: a.quote },
        { x: textX + 456, w: W - PAD * 2 - 456, text: a.reason },
      ];
      for (const col of cols) {
        lines.push(`<rect x="${col.x}" y="${cy - 12}" width="${col.w}" height="18" stroke="#e5e9ef" stroke-width="1" fill="none"/>`);
        const c = col.color ?? '#2c394f';
        const w = col.weight ?? 'normal';
        lines.push(`<text x="${col.x + 4}" y="${cy}" font-size="11" fill="${c}" font-weight="${w}">${esc(col.text)}</text>`);
      }
      y += 20;
    }

    add(`简历重点标记 · ${name}`, { size: 22, color: '#253249', weight: '700', gap: 4 });
    add(`综合得分：${review.summary.final_score}  招聘建议：${review.summary.recommendation}  匹配重点：${review.summary.highlights} 处  待核实：${review.summary.risks} 处`, { gap: 2 });

    add('标记清单', { size: 15, color: '#3e6fd3', weight: '600', gap: 16 });
    addTableHeader();
    for (const a of review.annotations) addTableRow(a);

    add('原简历内容', { size: 15, color: '#3e6fd3', weight: '600', gap: 16 });
    addSegmented(segments, { gap: 2 });

    y += 8;
    lines.push(`<text x="${textX}" y="${y}" font-size="10" fill="#8190a4">${esc(review.notice)}</text>`);

    const totalH = y + 20;
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${totalH}" viewBox="0 0 ${W} ${totalH}" font-family="Microsoft YaHei, Arial, sans-serif">
<rect width="${W}" height="${totalH}" fill="#ffffff"/>
${lines.join('\n')}
</svg>`;
  }

  // ── 导出主入口 ─────────────────────────────────────────────────────────

  function exportAs(format: 'png' | 'pdf' | 'doc') {
    if (!review) return;

    if (format === 'png') {
      exportPng();
    } else if (format === 'pdf') {
      exportPdf();
    } else {
      exportWord();
    }
  }

  function exportPng() {
    const svg = buildExportSvg();
    if (!svg) return;
    const svgBlob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(svgBlob);
    const img = new Image();
    img.onload = () => {
      const scale = 2;
      const canvas = document.createElement('canvas');
      canvas.width = img.naturalWidth * scale;
      canvas.height = img.naturalHeight * scale;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.scale(scale, scale);
      ctx.drawImage(img, 0, 0);
      URL.revokeObjectURL(url);
      canvas.toBlob((blob) => {
        if (!blob) return;
        const d = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = d;
        a.download = `简历重点标记-${name}.png`;
        a.click();
        URL.revokeObjectURL(d);
      }, 'image/png');
    };
    img.src = url;
  }

  function exportPdf() {
    const html = buildExportHtml();
    const win = window.open('', '_blank', 'width=900,height=700');
    if (win) {
      win.document.write(html);
      win.document.close();
      win.focus();
      setTimeout(() => win.print(), 500);
    }
  }

  function exportWord() {
    // Word 兼容 HTML：生成标准 HTML 文件，用 .doc 扩展名保存
    const html = buildExportHtml();
    // 替换 DOCTYPE 让 Word 识别为 HTML
    const wordHtml = html.replace('<!DOCTYPE html>', '<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word">');
    const blob = new Blob(['\ufeff', wordHtml], { type: 'application/msword;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `简历重点标记-${name}.doc`;
    a.click();
    URL.revokeObjectURL(url);
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

      {review && (
        <div className="mt-5">
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_280px]">
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
                <p className="text-xs text-[#65738a]">待核实 {review.summary.risks} 处</p>
              </div>
              <div className="rounded-md border border-[#e5e9ef] p-4">
                <p className="flex items-center gap-1.5 text-xs font-medium text-[#66758b]">
                  <ShieldAlertIcon className="size-3.5 text-[#995c87]" />
                  说明
                </p>
                <p className="mt-1 text-xs leading-5 text-[#65738a]">{review.notice}</p>
              </div>
              <div className="grid grid-cols-3 gap-2">
                <button
                  type="button"
                  onClick={() => exportAs('png')}
                  className="inline-flex items-center justify-center gap-1 rounded-md bg-[#3e6fd3] px-2 py-1.5 text-[11px] font-medium text-white"
                >
                  <CameraIcon className="size-3" />
                  图片
                </button>
                <button
                  type="button"
                  onClick={() => exportAs('pdf')}
                  className="inline-flex items-center justify-center gap-1 rounded-md border border-[#d5dde9] bg-white px-2 py-1.5 text-[11px] font-medium text-[#2c394f]"
                >
                  <DownloadIcon className="size-3" />
                  PDF
                </button>
                <button
                  type="button"
                  onClick={() => exportAs('doc')}
                  className="inline-flex items-center justify-center gap-1 rounded-md border border-[#d5dde9] bg-white px-2 py-1.5 text-[11px] font-medium text-[#2c394f]"
                >
                  <DownloadIcon className="size-3" />
                  Word
                </button>
              </div>
            </aside>
          </div>
        </div>
      )}
    </section>
  );
}
