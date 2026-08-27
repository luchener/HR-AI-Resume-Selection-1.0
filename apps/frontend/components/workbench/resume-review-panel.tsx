'use client';

import { useCallback, useState } from 'react';
import {
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

  // ── 导出辅助 ───────────────────────────────────────────────────────

  function escapeHtml(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function escapeXml(s: string): string {
    // 先把换行替换成 <w:br/>，再转义其余 XML 特殊字符
    return s.replace(/\r\n/g, '\n').replace(/\n/g, '<w:br/>')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function categoryBg(cat: ResumeReviewMarker['category']): string {
    return CATEGORY_STYLE[cat].bg === 'bg-[#e6f7ee]' ? '#e6f7ee' :
           CATEGORY_STYLE[cat].bg === 'bg-[#eaf0fb]' ? '#eaf0fb' :
           CATEGORY_STYLE[cat].bg === 'bg-[#fdecec]' ? '#fdecec' :
           CATEGORY_STYLE[cat].bg === 'bg-[#f3f4f6]' ? '#f3f4f6' : '#fef6e6';
  }
  function categoryColor(cat: ResumeReviewMarker['category']): string {
    return CATEGORY_STYLE[cat].text === 'text-[#1d7f5c]' ? '1D7F5C' :
           CATEGORY_STYLE[cat].text === 'text-[#3e6fd3]' ? '3E6FD3' :
           CATEGORY_STYLE[cat].text === 'text-[#b23b4e]' ? 'B23B4E' :
           CATEGORY_STYLE[cat].text === 'text-[#6b7280]' ? '6B7280' : 'B0761A';
  }

  function docxRun(text: string, color?: string, bold?: boolean, sz?: string): string {
    const rPr = (() => {
      let inner = '';
      if (bold) inner += '<w:b/><w:bCs/>';
      if (color) inner += `<w:color w:val="${color}"/>`;
      if (sz) inner += `<w:sz w:val="${sz}"/><w:szCs w:val="${sz}"/>`;
      return inner ? `<w:rPr>${inner}</w:rPr>` : '';
    })();
    return `<w:r>${rPr}<w:t xml:space="preserve">${text}</w:t></w:r>`;
  }
  function docxH(text: string, level: string): string {
    return docxRun(escapeXml(text), '253249', true, level === '1' ? '32' : '28');
  }
  function docxP(...runs: string[]): string {
    return `<w:p>${runs.join('')}</w:p>`;
  }

  function docxDocumentXml(paragraphs: string[]): string {
    return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
            xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
            mc:Ignorable="wps">
<w:body>${paragraphs.join('')}\n<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body>
</w:document>`;
  }

  function docxStylesXml(): string {
    return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:default="1" w:styleId="a">
  <w:name w:val="Normal"/><w:qFormat/>
  <w:rPr><w:rFonts w:ascii="Microsoft YaHei" w:eastAsia="Microsoft YaHei" w:hAnsi="Microsoft YaHei"/><w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr>
</w:style></w:styles>`;
  }

  function docxContentTypesXml(): string {
    return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>`;
  }

  function docxPackageRelsXml(): string {
    return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>`;
  }

  function docxDocumentRelsXml(): string {
    return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>`;
  }

  // 纯 JS 最小 ZIP 实现（仅用于 DOCX 导出；DOCX 是 ZIP 容器）
  class DocxZip {
    private files: Array<{ name: string; data: string }> = [];
    addFile(name: string, content: string): void {
      this.files.push({ name, data: content });
    }
    generate(): Blob {
      // UTF-8 → bytes
      const encoder = new TextEncoder();
      const compressed: Array<{ name: string; raw: Uint8Array; comp: Uint8Array | null; crc: number; rawSize: number }> = [];
      for (const f of this.files) {
        const raw = encoder.encode(f.data);
        const comp = deflateRaw(raw);
        compressed.push({ name: f.name, raw, comp: comp.length < raw.length ? comp : null, crc: crc32(raw), rawSize: raw.length });
      }
      let offset = 0;
      const localHeaders: Array<{ offset: number; extraLen: number }> = [];
      const localParts: Uint8Array[] = [];
      for (const f of compressed) {
        localHeaders.push({ offset, extraLen: 0 });
        const nameBytes = encoder.encode(f.name);
        const hdr = buildLocalHdr(f.name, f.rawSize, f.crc, f.comp ?? f.raw, nameBytes.length, 0);
        localParts.push(hdr);
        localParts.push(f.comp ?? f.raw);
        offset += hdr.length + (f.comp ?? f.raw).length;
      }
      const centralOffset = offset;
      const centralParts: Uint8Array[] = [];
      for (let i = 0; i < compressed.length; i++) {
        const f = compressed[i];
        const nameBytes = encoder.encode(f.name);
        const ch = buildCentralHdr(f.name, f.rawSize, f.crc, f.comp ?? f.raw, nameBytes.length, localHeaders[i].offset, 0);
        centralParts.push(ch);
      }
      const centralBytes = concatUint8(centralParts);
      const eocd = buildEocd(compressed.length, centralBytes.length, centralOffset);
      const all = concatUint8([...localParts, centralBytes, eocd]);
      return new Blob([all], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' });
    }
  }

  // ── 二进制构建工具 ──────────────────────────────────────────────

  function u16(v: number): Uint8Array { const b = new Uint8Array(2); b[0] = v & 0xff; b[1] = (v >>> 8) & 0xff; return b; }
  function u32(v: number): Uint8Array { const b = new Uint8Array(4); b[0] = v & 0xff; b[1] = (v >>> 8) & 0xff; b[2] = (v >>> 16) & 0xff; b[3] = (v >>> 24) & 0xff; return b; }
  function concatUint8(arrays: Uint8Array[]): Uint8Array {
    const total = arrays.reduce((s, a) => s + a.length, 0);
    const out = new Uint8Array(total);
    let o = 0;
    for (const a of arrays) { out.set(a, o); o += a.length; }
    return out;
  }

  function buildLocalHdr(name: string, rawSize: number, crc: number, data: Uint8Array, nameLen: number, extraLen: number): Uint8Array {
    const parts: Uint8Array[] = [
      new Uint8Array([0x50, 0x4b, 0x03, 0x04]), // signature
      u16(20), // version needed
      u16(0),  // flags
      u16(0),  // compression method (0 = stored) — we use stored since deflate may not shrink XML much
      u16(0), u16(0), // mod time/date
      u32(crc), u32(data.length), u32(rawSize),
      u16(nameLen), u16(extraLen),
      new TextEncoder().encode(name)
    ];
    return concatUint8(parts);
  }

  function buildCentralHdr(name: string, rawSize: number, crc: number, data: Uint8Array, nameLen: number, localOffset: number, extraLen: number): Uint8Array {
    const parts: Uint8Array[] = [
      new Uint8Array([0x50, 0x4b, 0x01, 0x02]),
      u16(20), u16(20), // version made / needed
      u16(0), u16(0),
      u16(0), u16(0),
      u32(crc), u32(data.length), u32(rawSize),
      u16(nameLen), u16(extraLen), u16(0), u16(0), u32(0), u32(0),
      u32(localOffset),
      new TextEncoder().encode(name)
    ];
    return concatUint8(parts);
  }

  function buildEocd(count: number, centralSize: number, centralOffset: number): Uint8Array {
    return concatUint8([
      new Uint8Array([0x50, 0x4b, 0x05, 0x06]),
      u16(0), u16(0),
      u16(count), u16(count),
      u32(centralSize), u32(centralOffset),
      u16(0)
    ]);
  }

  function crc32(data: Uint8Array): number {
    let table = crc32.table;
    if (!table) {
      table = crc32.table = new Uint32Array(256);
      for (let i = 0; i < 256; i++) {
        let c = i;
        for (let j = 0; j < 8; j++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
        table[i] = c;
      }
    }
    let crc = 0 ^ -1;
    for (let i = 0; i < data.length; i++) crc = table[(crc ^ data[i]) & 0xff] ^ (crc >>> 8);
    return (crc ^ -1) >>> 0;
  }
  crc32.table = null as unknown as Uint32Array;

  // 极简 deflate stored block（不需要压缩，直接 stored）
  function deflateRaw(_data: Uint8Array): Uint8Array {
    // 使用 stored block（不压缩），因为 XML 体积很小，压缩收益不大，且实现完整 deflate 复杂度高
    const len = _data.length;
    const out = new Uint8Array(5 + len);
    out[0] = 0x00; // BFINAL=0, BTYPE=00 (stored)
    out[1] = len & 0xff; out[2] = (len >>> 8) & 0xff;
    out[3] = (len ^ 0xffff) & 0xff; out[4] = ((len ^ 0xffff) >>> 8) & 0xff;
    out.set(_data, 5);
    return out;
  }

  const loadReview = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [content, markers] = await Promise.all([
        fetchResumeView(resumeId),
        fetchResumeReviewMarkers(resumeId, analysis as unknown as Record<string, unknown>, candidateName),
      ]);
      setRawContent(content || '未提供原简历内容');
      setReview(markers);
    } catch (err) {
      setError(err instanceof Error ? err.message : '简历重点标记加载失败。');
    } finally {
      setLoading(false);
    }
  }, [resumeId, analysis, candidateName]);

  const exportReview = useCallback(
    (format: 'pdf' | 'docx') => {
      if (!review) return;
      const segments = buildHighlightedSegments(rawContent, review.annotations);
      const name = review.candidate_name || candidateName || '候选人';

      if (format === 'pdf') {
        // 打开带样式的 HTML 窗口，触发浏览器打印 → 用户可保存为 PDF
        const rows = review.annotations
          .map((a) => `<tr><td>${CATEGORY_STYLE[a.category].label}</td><td>${escapeHtml(a.quote)}</td><td>${escapeHtml(a.reason)}</td></tr>`)
          .join('');
        const resumeHtml = segments
          .map((seg) =>
            seg.annotation
              ? `<mark style="background:${categoryBg(seg.annotation.category)}">${escapeHtml(seg.text)}</mark>`
              : escapeHtml(seg.text),
          )
          .join('');
        const html = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>简历重点标记·${escapeHtml(name)}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}body{font-family:"Microsoft YaHei",Arial,sans-serif;font-size:13px;line-height:1.7;color:#2c394f;padding:32px}
h1{font-size:20px;color:#253249;margin-bottom:4px}h2{font-size:15px;color:#3e6fd3;margin:18px 0 8px;border-bottom:1px solid #dce2eb;padding-bottom:4px}
.meta{display:flex;gap:24px;margin:8px 0 16px;font-size:13px;color:#65738a}.meta b{color:#1d7f5c;font-size:22px;margin-right:4px}
table{border-collapse:collapse;width:100%;margin:8px 0 16px;font-size:12px}th,td{border:1px solid #dce2eb;padding:5px 8px;text-align:left}th{background:#eaf0fb;color:#3e6fd3}
.resume{white-space:pre-wrap;word-break:break-word;background:#fbfcfe;border:1px solid #e5e9ef;border-radius:6px;padding:12px;margin-top:8px}
mark{border-radius:2px;padding:0 2px}.page-break{page-break-before:always}
@media print{body{padding:16px}.noprint{display:none}}
</style></head><body>
<h1>简历重点标记 · ${escapeHtml(name)}</h1>
<div class="meta"><span>综合得分：<b>${review.summary.final_score}</b></span><span>招聘建议：${escapeHtml(review.summary.recommendation)}</span><span>匹配重点：${review.summary.highlights} 处</span><span>待核实：${review.summary.risks} 处</span></div>
<h2>标记清单</h2>
<table><thead><tr><th>类型</th><th>引用原文</th><th>HR 说明</th></tr></thead><tbody>${rows}</tbody></table>
<h2>原简历内容</h2>
<div class="resume">${resumeHtml}</div>
<p style="margin-top:16px;font-size:11px;color:#8190a4">${escapeHtml(review.notice)}</p>
</body></html>`;
        const printWin = window.open('', '_blank', 'width=900,height=700');
        if (printWin) {
          printWin.document.write(html);
          printWin.document.close();
          printWin.focus();
          setTimeout(() => { printWin.print(); }, 400);
        }
        return;
      }

      // ---- DOCX：构建真实 OOXML 压缩包 ----
      const zip = new DocxZip();
      const paragraphs: string[] = [];
      paragraphs.push(docxP(docxH(name, '1'), docxRun(' 简历重点标记')));
      paragraphs.push(docxP(docxRun(`综合得分：${review.summary.final_score}    招聘建议：${review.summary.recommendation}    匹配重点：${review.summary.highlights} 处    待核实：${review.summary.risks} 处`)));
      paragraphs.push(docxP(docxH('标记清单', '2')));
      for (const a of review.annotations) {
        paragraphs.push(docxP(
          docxRun(`【${CATEGORY_STYLE[a.category].label}】`, '#3e6fd3', true),
          docxRun(` ${escapeXml(a.quote)}`),
          docxRun(`（${escapeXml(a.reason)}）`),
        ));
      }
      paragraphs.push(docxP(docxH('原简历内容', '2')));
      for (const line of rawContent.split('\n')) {
        paragraphs.push(docxP(docxRun(escapeXml(line))));
      }

      // 高亮段落：按段重新渲染
      const highlightPara: string[] = [];
      for (const seg of segments) {
        if (seg.annotation) {
          highlightPara.push(docxRun(escapeXml(seg.text), categoryColor(seg.annotation.category), true));
        } else {
          highlightPara.push(docxRun(escapeXml(seg.text)));
        }
      }
      if (highlightPara.length) paragraphs.push(docxP(...highlightPara));

      zip.addFile('[Content_Types].xml', docxContentTypesXml());
      zip.addFile('_rels/.rels', docxPackageRelsXml());
      zip.addFile('word/document.xml', docxDocumentXml(paragraphs));
      zip.addFile('word/styles.xml', docxStylesXml());
      zip.addFile('word/_rels/document.xml.rels', docxDocumentRelsXml());
      const blob = zip.generate();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `简历重点标记-${name}.docx`;
      anchor.click();
      URL.revokeObjectURL(url);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- DocxZip/docxH 为内部稳定引用
    [review, rawContent, candidateName],
  );

  const segments = review ? buildHighlightedSegments(rawContent, review.annotations) : [];

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
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_260px]">
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
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  onClick={() => exportReview('pdf')}
                  className="inline-flex items-center justify-center gap-1.5 rounded-md bg-[#3e6fd3] px-3 py-2 text-xs font-medium text-white"
                >
                  <DownloadIcon className="size-3.5" />
                  导出 PDF
                </button>
                <button
                  type="button"
                  onClick={() => exportReview('docx')}
                  className="inline-flex items-center justify-center gap-1.5 rounded-md border border-[#d5dde9] bg-white px-3 py-2 text-xs font-medium text-[#2c394f]"
                >
                  <DownloadIcon className="size-3.5" />
                  导出 Word
                </button>
              </div>
            </aside>
          </div>
        </div>
      )}
    </section>
  );
}
