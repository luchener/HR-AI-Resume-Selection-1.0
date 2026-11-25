import { getStoredToken } from '@/components/workbench/auth-context';

export async function downloadReportPdf(
  html: string,
  name: string,
  kind: 'analysis' | 'review',
  options?: { signal?: AbortSignal },
) {
  const response = await fetch(`/report-pdf?kind=${kind}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${getStoredToken()}`,
      'Content-Type': 'text/html; charset=utf-8',
    },
    body: html,
    signal: options?.signal,
  });

  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.error || 'PDF 导出失败，请稍后重试。');
  }
  if (response.headers.get('Content-Type') !== 'application/pdf') {
    throw new Error('未收到 PDF 文件，请稍后重试。');
  }

  const blob = await response.blob();
  options?.signal?.throwIfAborted();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 160) + '.pdf';
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
