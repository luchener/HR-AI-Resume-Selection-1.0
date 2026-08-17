'use client';

import { ChangeEvent, DragEvent, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  ArrowRightIcon,
  BriefcaseBusinessIcon,
  CheckIcon,
  FileTextIcon,
  LoaderCircleIcon,
  ShieldCheckIcon,
  SparklesIcon,
  UploadCloudIcon,
  XIcon,
} from 'lucide-react';
import AppShell from './app-shell';
import { analyzeResumes, uploadJobDescription, uploadResume } from '@/lib/api/screening';
import { useAnalysis } from './analysis-context';
import { AiModelButton, useAiModel } from './ai-model-config';

const MAX_FILES = 3;
const MAX_FILE_SIZE = 30 * 1024 * 1024;
const ACCEPTED_EXTENSIONS = ['pdf', 'docx'];

type Phase = 'idle' | 'uploading' | 'job' | 'analyzing';

const formatSize = (size: number) => `${(size / 1024 / 1024).toFixed(size > 1024 * 1024 * 10 ? 0 : 1)} MB`;

export default function AnalysisWorkbench() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const { setAnalysisResult } = useAnalysis();
  const { config: aiConfig, isConfigured, openConfigurator } = useAiModel();
  const [files, setFiles] = useState<File[]>([]);
  const [jobDescription, setJobDescription] = useState('');
  const [isDragging, setIsDragging] = useState(false);
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState('');

  const busy = phase !== 'idle';
  const canAnalyze = files.length > 0 && jobDescription.trim().length >= 20 && !busy;

  const addFiles = (incoming: File[]) => {
    setError('');
    const combined = [...files];
    for (const file of incoming) {
      const extension = file.name.split('.').pop()?.toLowerCase() || '';
      if (!ACCEPTED_EXTENSIONS.includes(extension)) {
        setError('仅支持 PDF 和 DOCX 格式。');
        continue;
      }
      if (file.size > MAX_FILE_SIZE) {
        setError(`${file.name} 超过 30 MB，无法添加。`);
        continue;
      }
      if (combined.some((item) => item.name === file.name && item.size === file.size)) continue;
      if (combined.length >= MAX_FILES) {
        setError('一次最多分析 3 份简历。');
        break;
      }
      combined.push(file);
    }
    setFiles(combined);
  };

  const handleFileInput = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files || []));
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    if (!busy) addFiles(Array.from(event.dataTransfer.files));
  };

  const handleAnalyze = async () => {
    if (!canAnalyze) return;
    if (!isConfigured) {
      setError('请先配置 AI 模型，再开始分析。');
      openConfigurator();
      return;
    }
    setError('');
    try {
      setPhase('uploading');
      const resumeIds = await Promise.all(files.map(uploadResume));
      setPhase('job');
      const jobId = await uploadJobDescription(jobDescription.trim(), resumeIds[0]);
      setPhase('analyzing');
      const result = await analyzeResumes(resumeIds, jobId, aiConfig);
      setAnalysisResult(result);
      router.push('/dashboard');
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : '';
      setError(
        message === 'Failed to fetch'
          ? '无法连接分析服务，请确认后端服务已启动后重试。'
          : message || '分析未完成，请稍后重试。',
      );
      setPhase('idle');
    }
  };

  const phaseLabel = phase === 'uploading'
    ? '正在读取简历'
    : phase === 'job'
      ? '正在解析岗位要求'
      : phase === 'analyzing'
        ? `正在分析 ${files.length} 位候选人`
        : '开始分析';

  return (
    <AppShell active="home">
      <div className="mx-auto w-full max-w-[1480px] px-5 py-8 sm:px-8 lg:px-10 lg:py-10 xl:px-14">
        <header className="flex flex-col gap-5 border-b border-[#dce2eb] pb-7 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-xs font-semibold uppercase text-[#5d6d86]">HR Screening Workspace · 1.0</p>
            <h1 className="mt-3 text-3xl font-semibold text-[#152137] sm:text-4xl">AI 智能<span className="block sm:inline">全维度量化人才评估。</span></h1>
            <p className="mt-3 max-w-2xl text-sm leading-6 text-[#6d7b91] sm:text-base">自动匹配岗位与简历信息，输出可直接用于招聘决策的标准化分析报告</p>
          </div>
          <AiModelButton />
        </header>

        <div className="mt-7 grid gap-6 xl:grid-cols-[minmax(340px,0.82fr)_minmax(500px,1.18fr)]">
          <section className="flex min-h-[550px] flex-col rounded-md border border-[#dce2eb] bg-white p-5 sm:p-7">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-semibold uppercase text-[#5273c6]">01 · Resume files</p>
                <h2 className="mt-2 text-xl font-semibold text-[#18243a]">添加候选人简历</h2>
              </div>
              <span className="rounded-full bg-[#eef3ff] px-3 py-1 text-xs font-medium text-[#4268c6]">{files.length}/{MAX_FILES}</span>
            </div>

            <div
              role="button"
              tabIndex={busy ? -1 : 0}
              onClick={() => !busy && inputRef.current?.click()}
              onKeyDown={(event) => {
                if (!busy && (event.key === 'Enter' || event.key === ' ')) inputRef.current?.click();
              }}
              onDragEnter={(event) => { event.preventDefault(); if (!busy) setIsDragging(true); }}
              onDragOver={(event) => event.preventDefault()}
              onDragLeave={() => setIsDragging(false)}
              onDrop={handleDrop}
              className={`mt-6 flex min-h-64 cursor-pointer flex-col items-center justify-center rounded-md border border-dashed px-6 text-center transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#789cf2] ${isDragging ? 'border-[#4774db] bg-[#eef3ff]' : 'border-[#cfd7e4] bg-[#f8fafd] hover:border-[#7999df] hover:bg-[#f3f6fc]'} ${busy ? 'pointer-events-none opacity-60' : ''}`}
            >
              <input ref={inputRef} type="file" accept=".pdf,.docx" multiple className="hidden" onChange={handleFileInput} />
              <span className="flex size-14 items-center justify-center rounded-md bg-[#17243b] text-white shadow-[8px_8px_0_#88a8ff]">
                <UploadCloudIcon className="size-6" aria-hidden="true" />
              </span>
              <p className="mt-7 text-lg font-semibold text-[#1a263b]">拖放简历到这里</p>
              <p className="mt-2 text-sm text-[#7a879a]">PDF / DOCX · 每份最大 30 MB</p>
              <button type="button" className="mt-5 rounded-md border border-[#d9e0e9] bg-white px-4 py-2 text-sm font-medium text-[#26344b]">选择文件</button>
            </div>

            <div className="mt-5 space-y-2" aria-live="polite">
              {files.length === 0 ? (
                <div className="flex items-center gap-3 rounded-md border border-[#edf0f4] px-4 py-3 text-sm text-[#7b8798]">
                  <ShieldCheckIcon className="size-4 text-[#2b936b]" /> 文件仅用于本次招聘分析
                </div>
              ) : files.map((file, index) => (
                <div key={`${file.name}-${file.lastModified}`} className="flex items-center gap-3 rounded-md border border-[#e1e6ed] px-3 py-3">
                  <span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-[#eef3ff] text-[#4d72cf]"><FileTextIcon className="size-4" /></span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-[#253249]">{file.name}</p>
                    <p className="mt-0.5 text-xs text-[#8793a4]">候选人 {index + 1} · {formatSize(file.size)}</p>
                  </div>
                  <button type="button" disabled={busy} onClick={(event) => { event.stopPropagation(); setFiles((current) => current.filter((item) => item !== file)); }} className="flex size-8 items-center justify-center rounded-md text-[#8290a3] hover:bg-[#f1f3f6] hover:text-[#253249]" aria-label={`移除 ${file.name}`}>
                    <XIcon className="size-4" />
                  </button>
                </div>
              ))}
            </div>
          </section>

          <section className="flex min-h-[550px] flex-col rounded-md border border-[#dce2eb] bg-white p-5 sm:p-7">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-semibold uppercase text-[#2f8b68]">02 · Job description</p>
                <h2 className="mt-2 text-xl font-semibold text-[#18243a]">输入目标岗位描述</h2>
              </div>
              <BriefcaseBusinessIcon className="size-5 text-[#6f7e92]" />
            </div>

            <label htmlFor="job-description" className="mt-6 text-sm font-medium text-[#344158]">岗位职责与任职要求</label>
            <textarea
              id="job-description"
              value={jobDescription}
              onChange={(event) => setJobDescription(event.target.value)}
              disabled={busy}
              placeholder="粘贴完整 JD，包括岗位职责、经验年限、技能要求、学历与地点等信息..."
              className="mt-2 min-h-56 w-full resize-y rounded-md border border-[#cfd7e2] bg-[#fbfcfe] p-4 text-sm leading-6 text-[#253249] outline-none transition focus:border-[#6488df] focus:ring-2 focus:ring-[#dce7ff] disabled:opacity-60"
            />
            <div className="mt-2 flex items-center justify-between text-xs text-[#8a96a7]">
              <span>{jobDescription.trim().length < 20 ? '至少输入 20 个字符' : '岗位信息已就绪'}</span>
              <span>{jobDescription.length} 字</span>
            </div>

            <div className="mt-6 grid gap-2 sm:grid-cols-3">
              {[
                { number: '01', label: '解析硬性门槛', color: 'bg-[#7da2ff]' },
                { number: '02', label: '比对履历证据', color: 'bg-[#ff9b89]' },
                { number: '03', label: '生成招聘建议', color: 'bg-[#a98cf5]' },
              ].map((item) => (
                <div key={item.number} className="flex items-center gap-3 rounded-md border border-[#e1e6ed] px-3 py-3">
                  <span className={`flex size-8 shrink-0 items-center justify-center rounded-md text-xs font-semibold text-[#17233a] ${item.color}`}>{item.number}</span>
                  <span className="text-xs font-medium text-[#435168]">{item.label}</span>
                </div>
              ))}
            </div>

            <div className="mt-auto border-t border-[#e5e9ef] pt-5">
              {error && <div role="alert" className="mb-4 rounded-md border border-[#efb5ad] bg-[#fff4f2] px-4 py-3 text-sm text-[#9d3e32]">{error}</div>}
              <button
                type="button"
                disabled={!canAnalyze}
                onClick={handleAnalyze}
                className="flex h-12 w-full items-center justify-center gap-2 rounded-md bg-[#1b2a45] px-5 text-sm font-semibold text-white transition-colors hover:bg-[#263a5e] disabled:cursor-not-allowed disabled:bg-[#b8c0cc]"
              >
                {busy ? <LoaderCircleIcon className="size-4 animate-spin" /> : <SparklesIcon className="size-4" />}
                {phaseLabel}
                {!busy && <ArrowRightIcon className="size-4" />}
              </button>
              <p className="mt-3 flex items-center justify-center gap-2 text-xs text-[#7e8a9d]">
                <CheckIcon className="size-3.5 text-[#2b936b]" /> 分析将覆盖履历、技能、项目、稳定性与招聘风险
              </p>
            </div>
          </section>
        </div>
      </div>
    </AppShell>
  );
}
