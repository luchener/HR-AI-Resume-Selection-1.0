import type { AnalysisResult } from '@/components/workbench/analysis-context';
import type { AiModelConfig } from '@/components/workbench/ai-model-config';
import { API_URL } from './config';

function aiConfigPayload(config: AiModelConfig) {
  return {
    provider: config.provider,
    api_key: config.apiKey,
    base_url: config.baseUrl,
    model: config.model,
  };
}

async function errorDetail(response: Response): Promise<string> {
  const text = await response.text();
  try {
    return (JSON.parse(text) as { detail?: string }).detail || text;
  } catch {
    return text;
  }
}

export async function uploadResume(file: File): Promise<string> {
  const formData = new FormData();
  formData.append('file', file);
  const response = await fetch(`${API_URL}/api/v1/resumes/upload`, {
    method: 'POST',
    body: formData,
  });
  if (!response.ok) {
    throw new Error((await errorDetail(response)) || `简历上传失败（HTTP ${response.status}）`);
  }
  const payload = (await response.json()) as { resume_id?: string };
  if (!payload.resume_id) throw new Error('上传成功，但服务未返回简历编号。');
  return payload.resume_id;
}

export async function uploadJobDescription(description: string, resumeId: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/jobs/upload`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_descriptions: [description], resume_id: resumeId }),
  });
  if (!response.ok) {
    throw new Error((await errorDetail(response)) || `岗位描述上传失败（HTTP ${response.status}）`);
  }
  const payload = (await response.json()) as { job_id?: string[] };
  const jobId = payload.job_id?.[0];
  if (!jobId) throw new Error('上传成功，但服务未返回岗位编号。');
  return jobId;
}

export async function analyzeResumes(
  resumeIds: string | string[],
  jobId: string,
  aiConfig: AiModelConfig,
  signal?: AbortSignal,
): Promise<AnalysisResult> {
  const response = await fetch(`${API_URL}/api/v1/resumes/hr-analysis`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(
      Array.isArray(resumeIds)
        ? { resume_ids: resumeIds, job_id: jobId, ai_config: aiConfigPayload(aiConfig) }
        : { resume_id: resumeIds, job_id: jobId, ai_config: aiConfigPayload(aiConfig) },
    ),
    signal,
  });
  if (!response.ok) {
    throw new Error((await errorDetail(response)) || `招聘分析失败（HTTP ${response.status}），请稍后重试。`);
  }
  return (await response.json()) as AnalysisResult;
}

export async function improveResumeStream(
  resumeId: string,
  jobId: string,
  aiConfig: AiModelConfig,
  onProgress?: (status: string, message: string) => void,
  signal?: AbortSignal,
): Promise<AnalysisResult> {
  const response = await fetch(`${API_URL}/api/v1/resumes/improve?stream=true`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({
      resume_id: resumeId,
      job_id: jobId,
      ai_config: aiConfigPayload(aiConfig),
    }),
    signal,
  });
  if (!response.ok) {
    throw new Error((await errorDetail(response)) || `深度优化失败（HTTP ${response.status}）`);
  }
  if (!response.body) throw new Error('深度优化服务未返回数据流。');

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let finalResult: AnalysisResult | null = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let separator: number;
    while ((separator = buffer.indexOf('\n\n')) !== -1) {
      const rawEvent = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      const dataLine = rawEvent
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trim())
        .join('');
      if (!dataLine) continue;

      let event: { status: string; message?: string; result?: AnalysisResult };
      try {
        event = JSON.parse(dataLine) as typeof event;
      } catch {
        continue;
      }
      if (event.status === 'completed' && event.result) {
        finalResult = event.result;
      } else if (event.status === 'error') {
        throw new Error(event.message || '深度优化失败。');
      } else {
        onProgress?.(event.status, event.message ?? '');
      }
    }
  }

  if (!finalResult) throw new Error('深度优化数据流提前结束。');
  return finalResult;
}

export async function testAiConnection(
  aiConfig: AiModelConfig,
  signal?: AbortSignal,
): Promise<{ model: string; elapsedMs: number }> {
  const response = await fetch(`${API_URL}/api/v1/ai/test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ai_config: aiConfigPayload(aiConfig) }),
    signal,
  });
  if (!response.ok) {
    throw new Error((await errorDetail(response)) || `连接测试失败（HTTP ${response.status}）`);
  }
  const payload = (await response.json()) as {
    data?: { model?: string; elapsed_ms?: number };
  };
  return {
    model: payload.data?.model || aiConfig.model,
    elapsedMs: payload.data?.elapsed_ms || 0,
  };
}
