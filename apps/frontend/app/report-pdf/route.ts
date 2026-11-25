import { setTimeout as delay } from 'node:timers/promises';
import { chromium } from 'playwright-core';
import { REPORT_EXPORT_STYLE, REVIEW_EXPORT_STYLE } from '@/lib/report-export-styles';

export const runtime = 'nodejs';

const MAX_HTML_BYTES = 256 * 1024;
const MAX_PDF_BYTES = 8 * 1024 * 1024;
let rendering: { signal: AbortSignal; done: Promise<void> } | null = null;

function error(message: string, status: number) {
  return Response.json({ error: message }, { status, headers: { 'Cache-Control': 'no-store' } });
}

async function readHtml(request: Request) {
  const reader = request.body?.getReader();
  if (!reader) return '';
  const signal = AbortSignal.any([request.signal, AbortSignal.timeout(5000)]);
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', cancel, { once: true });
  const decoder = new TextDecoder();
  let bytes = 0;
  let html = '';
  try {
    while (true) {
      signal.throwIfAborted();
      const { value, done } = await reader.read();
      signal.throwIfAborted();
      if (done) return html + decoder.decode();
      bytes += value.byteLength;
      if (bytes > MAX_HTML_BYTES) return null;
      html += decoder.decode(value, { stream: true });
    }
  } finally {
    signal.removeEventListener('abort', cancel);
    cancel();
  }
}

async function renderPdf(html: string, style: string, signal: AbortSignal) {
  signal.throwIfAborted();
  const browser = await chromium.launch({
    executablePath: process.env.REPORT_PDF_CHROMIUM_PATH ||
      (process.platform === 'linux' ? '/usr/bin/chromium' : undefined),
    timeout: 8000,
    // 容器默认 /dev/shm 只有 64 MB，Chromium 在内存紧张时会启动失败；改用 /tmp。
    args: ['--disable-dev-shm-usage'],
  });
  const close = () => { void browser.close().catch(() => {}); };
  const deadline = setTimeout(close, 12000);
  signal.addEventListener('abort', close, { once: true });
  try {
    signal.throwIfAborted();
    const page = await browser.newPage({ javaScriptEnabled: false, serviceWorkers: 'block' });
    await page.route('**/*', (route) => route.abort());
    await page.setContent(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
      <style>${style}
      h1,h2,h3{break-after:avoid} thead{display:table-header-group}
      tr{break-inside:avoid} body{overflow-wrap:anywhere}
      </style></head><body></body></html>`);

    // Parse into an inert fragment and keep only elements used by the two report templates.
    await page.evaluate((markup) => {
      const template = document.createElement('template');
      template.innerHTML = markup;
      const allowed = new Set('B BR DIV H1 H2 H3 HEADER LI OL P SECTION SPAN STRONG TABLE TBODY TD TH THEAD TR UL'.split(' '));
      const inlineStyles = ['width', 'font-weight', 'font-size', 'color', 'background-color', 'border-radius', 'padding'];
      for (const node of template.content.querySelectorAll('*')) {
        if (!allowed.has(node.tagName)) {
          if (node.tagName === 'TITLE') document.title = node.textContent || '';
          node.remove();
          continue;
        }
        const element = node as HTMLElement;
        for (const attr of Array.from(element.attributes)) {
          if (!['class', 'style', 'colspan', 'rowspan'].includes(attr.name)) element.removeAttribute(attr.name);
        }
        const declarations = inlineStyles.map((name) => [name, element.style.getPropertyValue(name)]);
        element.removeAttribute('style');
        for (const [name, value] of declarations) {
          if (value) element.style.setProperty(name, value);
        }
      }
      document.body.replaceChildren(template.content);
    }, html);

    return await page.pdf({
      format: 'A4',
      printBackground: true,
      margin: { top: '10mm', bottom: '10mm', left: '10mm', right: '10mm' },
    });
  } finally {
    clearTimeout(deadline);
    signal.removeEventListener('abort', close);
    await browser.close();
  }
}

export async function POST(request: Request) {
  const signal = request.signal;
  const authorization = request.headers.get('Authorization');
  if (!authorization?.startsWith('Bearer ')) return error('请登录后导出 PDF。', 401);
  const backend = process.env.BACKEND_INTERNAL_URL || 'http://127.0.0.1:9001';
  try {
    const session = await fetch(`${backend}/api/v1/auth/me`, {
      headers: { Authorization: authorization },
      cache: 'no-store',
      redirect: 'error',
      signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),
    });
    if (!session.ok) {
      return session.status === 401 || session.status === 403
        ? error('登录状态已失效，请重新登录。', session.status)
        : error('暂时无法验证登录状态，请稍后重试。', 503);
    }
  } catch {
    if (signal.aborted) return error('已取消 PDF 导出。', 499);
    return error('暂时无法验证登录状态，请稍后重试。', 503);
  }

  const kind = new URL(request.url).searchParams.get('kind');
  if (kind !== 'analysis' && kind !== 'review') return error('报告类型无效。', 400);
  if (Number(request.headers.get('Content-Length')) > MAX_HTML_BYTES) {
    return error('报告内容过长，无法导出 PDF。', 413);
  }
  if (rendering?.signal.aborted) await rendering.done;
  if (signal.aborted) return error('已取消 PDF 导出。', 499);
  if (rendering) return error('正在生成另一份 PDF，请稍后重试。', 429);
  let release!: () => void;
  const done = new Promise<void>((resolve) => { release = resolve; });
  rendering = { signal, done };
  try {
    const html = await readHtml(request);
    if (html === null) return error('报告内容过长，无法导出 PDF。', 413);
    if (!html.trim()) return error('报告内容为空。', 400);
    let pdf: Buffer;
    try {
      pdf = await renderPdf(html, kind === 'review' ? REVIEW_EXPORT_STYLE : REPORT_EXPORT_STYLE, signal);
    } catch {
      signal.throwIfAborted();
      // 首次启动 Chromium 可能因瞬时资源紧张失败；退避后重试一次。
      await delay(600, undefined, { signal });
      pdf = await renderPdf(html, kind === 'review' ? REVIEW_EXPORT_STYLE : REPORT_EXPORT_STYLE, signal);
    }
    signal.throwIfAborted();
    if (pdf.byteLength > MAX_PDF_BYTES) return error('生成的 PDF 超过 8 MB。', 413);
    return new Response(new Uint8Array(pdf), {
      headers: {
        'Content-Type': 'application/pdf',
        'Content-Disposition': 'attachment; filename="report.pdf"',
        'Cache-Control': 'no-store',
      },
    });
  } catch {
    if (signal.aborted) return error('已取消 PDF 导出。', 499);
    return error('PDF 生成失败或超时，请稍后重试。', 503);
  } finally {
    rendering = null;
    release();
  }
}
