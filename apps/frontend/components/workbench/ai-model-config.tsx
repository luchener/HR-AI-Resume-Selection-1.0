'use client';

import {
  createContext,
  FormEvent,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import {
  CheckCircle2Icon,
  EyeIcon,
  EyeOffIcon,
  LoaderCircleIcon,
  PlugZapIcon,
  SaveIcon,
  Settings2Icon,
  ShieldCheckIcon,
  Trash2Icon,
  XIcon,
} from 'lucide-react';
import { testAiConnection } from '@/lib/api/screening';

export type AiProvider = 'deepseek' | 'custom';

export interface AiModelConfig {
  provider: AiProvider;
  apiKey: string;
  baseUrl: string;
  model: string;
}

const STORAGE_KEY = 'resume-screening-ai-model-v1';
const DEFAULT_CONFIG: AiModelConfig = {
  provider: 'deepseek',
  apiKey: '',
  baseUrl: 'https://api.deepseek.com',
  model: 'deepseek-v4-flash',
};

interface AiModelContextValue {
  config: AiModelConfig;
  isConfigured: boolean;
  openConfigurator: () => void;
}

const AiModelContext = createContext<AiModelContextValue | null>(null);

function validationError(config: AiModelConfig): string {
  if (!config.apiKey.trim()) return '请填写 API Key。';
  if (!config.baseUrl.trim()) return '请填写接口地址。';
  try {
    const url = new URL(config.baseUrl.trim());
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error();
  } catch {
    return '接口地址必须是有效的 HTTP 或 HTTPS 地址。';
  }
  if (!config.model.trim()) return '请填写模型名称。';
  return '';
}

function normalizeStoredConfig(value: unknown): AiModelConfig | null {
  if (!value || typeof value !== 'object') return null;
  const stored = value as Partial<AiModelConfig>;
  if (stored.provider !== 'deepseek' && stored.provider !== 'custom') return null;
  if (
    typeof stored.apiKey !== 'string'
    || typeof stored.baseUrl !== 'string'
    || typeof stored.model !== 'string'
  ) return null;
  return {
    provider: stored.provider,
    apiKey: stored.apiKey,
    baseUrl: stored.baseUrl,
    model: stored.model,
  };
}

export function AiModelProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<AiModelConfig>(DEFAULT_CONFIG);
  const [draft, setDraft] = useState<AiModelConfig>(DEFAULT_CONFIG);
  const [isOpen, setIsOpen] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const [fieldError, setFieldError] = useState('');
  const [testState, setTestState] = useState<'idle' | 'testing' | 'success' | 'error'>('idle');
  const [testMessage, setTestMessage] = useState('');

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const stored = normalizeStoredConfig(JSON.parse(raw));
      if (stored) {
        const migrated = stored.provider === 'deepseek' && stored.model === 'deepseek-chat'
          ? { ...stored, model: 'deepseek-v4-flash' }
          : stored;
        if (migrated !== stored) {
          window.localStorage.setItem(STORAGE_KEY, JSON.stringify(migrated));
        }
        setConfig(migrated);
        setDraft(migrated);
      }
    } catch {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  }, []);

  const closeConfigurator = useCallback(() => {
    setIsOpen(false);
    setShowKey(false);
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeConfigurator();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [closeConfigurator, isOpen]);

  const openConfigurator = useCallback(() => {
    setDraft(config);
    setFieldError('');
    setTestState('idle');
    setTestMessage('');
    setIsOpen(true);
  }, [config]);

  const updateDraft = <K extends keyof AiModelConfig>(key: K, value: AiModelConfig[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setFieldError('');
    setTestState('idle');
    setTestMessage('');
  };

  const changeProvider = (provider: AiProvider) => {
    setDraft(
      provider === 'deepseek'
        ? DEFAULT_CONFIG
        : { provider: 'custom', apiKey: '', baseUrl: '', model: '' },
    );
    setShowKey(false);
    setFieldError('');
    setTestState('idle');
    setTestMessage('');
  };

  const handleTest = async () => {
    const error = validationError(draft);
    if (error) {
      setFieldError(error);
      return;
    }
    setFieldError('');
    setTestState('testing');
    setTestMessage('正在验证接口与模型...');
    try {
      const result = await testAiConnection({
        ...draft,
        apiKey: draft.apiKey.trim(),
        baseUrl: draft.baseUrl.trim().replace(/\/$/, ''),
        model: draft.model.trim(),
      });
      setTestState('success');
      setTestMessage(`连接成功 · ${result.model}${result.elapsedMs ? ` · ${result.elapsedMs} ms` : ''}`);
    } catch (caught) {
      setTestState('error');
      setTestMessage(caught instanceof Error ? caught.message : '连接测试失败，请检查配置。');
    }
  };

  const handleSave = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const error = validationError(draft);
    if (error) {
      setFieldError(error);
      return;
    }
    const normalized = {
      ...draft,
      apiKey: draft.apiKey.trim(),
      baseUrl: draft.baseUrl.trim().replace(/\/$/, ''),
      model: draft.model.trim(),
    };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
    setConfig(normalized);
    closeConfigurator();
  };

  const handleClear = () => {
    window.localStorage.removeItem(STORAGE_KEY);
    setConfig(DEFAULT_CONFIG);
    setDraft(DEFAULT_CONFIG);
    setShowKey(false);
    setFieldError('');
    setTestState('idle');
    setTestMessage('');
  };

  const isConfigured = Boolean(
    config.apiKey.trim() && config.baseUrl.trim() && config.model.trim(),
  );
  const contextValue = useMemo(
    () => ({ config, isConfigured, openConfigurator }),
    [config, isConfigured, openConfigurator],
  );

  return (
    <AiModelContext.Provider value={contextValue}>
      {children}
      {isOpen && (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-[#10192b]/45 p-0 backdrop-blur-[2px] sm:items-center sm:p-5"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeConfigurator();
          }}
        >
          <form
            role="dialog"
            aria-modal="true"
            aria-labelledby="ai-model-dialog-title"
            onSubmit={handleSave}
            className="max-h-[94vh] w-full overflow-y-auto rounded-t-lg border border-[#d8e0ea] bg-white shadow-[0_24px_80px_rgba(19,31,51,0.24)] sm:max-w-[640px] sm:rounded-lg"
          >
            <div className="flex items-start justify-between border-b border-[#e3e8ef] px-5 py-5 sm:px-7">
              <div className="flex min-w-0 gap-3">
                <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-[#eaf1ff] text-[#426dcc]">
                  <Settings2Icon className="size-5" aria-hidden="true" />
                </span>
                <div className="min-w-0">
                  <h2 id="ai-model-dialog-title" className="text-xl font-semibold text-[#17243a]">配置 AI 模型</h2>
                  <p className="mt-1 text-sm leading-6 text-[#718096]">选择 DeepSeek，或连接任意 OpenAI 兼容接口。</p>
                </div>
              </div>
              <button
                type="button"
                onClick={closeConfigurator}
                className="flex size-9 shrink-0 items-center justify-center rounded-md text-[#78869a] hover:bg-[#f0f3f7] hover:text-[#23314a]"
                aria-label="关闭模型配置"
                title="关闭"
              >
                <XIcon className="size-4" />
              </button>
            </div>

            <div className="space-y-5 px-5 py-6 sm:px-7">
              <div>
                <label htmlFor="ai-provider" className="text-sm font-semibold text-[#3d4a60]">服务商</label>
                <select
                  id="ai-provider"
                  value={draft.provider}
                  onChange={(event) => changeProvider(event.target.value as AiProvider)}
                  className="mt-2 h-12 w-full rounded-md border border-[#cfd8e5] bg-white px-4 text-sm font-medium text-[#1f2d43] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff]"
                >
                  <option value="deepseek">DeepSeek（默认）</option>
                  <option value="custom">其他（OpenAI 兼容接口）</option>
                </select>
                {draft.provider === 'deepseek' && (
                  <p className="mt-2 text-xs leading-5 text-[#8490a2]">
                    API Key 获取地址：{' '}
                    <a
                      href="https://platform.deepseek.com/api_keys"
                      target="_blank"
                      rel="noreferrer"
                      className="font-medium text-[#466fd0] underline underline-offset-2 hover:text-[#274fAD]"
                    >
                      platform.deepseek.com/api_keys
                    </a>
                  </p>
                )}
              </div>

              <div>
                <label htmlFor="ai-api-key" className="text-sm font-semibold text-[#3d4a60]">API Key</label>
                <div className="relative mt-2">
                  <input
                    id="ai-api-key"
                    type={showKey ? 'text' : 'password'}
                    value={draft.apiKey}
                    onChange={(event) => updateDraft('apiKey', event.target.value)}
                    placeholder="sk-..."
                    autoComplete="off"
                    spellCheck={false}
                    className="h-12 w-full rounded-md border border-[#cfd8e5] bg-[#fbfcfe] px-4 pr-12 text-sm text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff]"
                  />
                  <button
                    type="button"
                    onClick={() => setShowKey((current) => !current)}
                    className="absolute right-2 top-1/2 flex size-8 -translate-y-1/2 items-center justify-center rounded-md text-[#7b889a] hover:bg-[#edf1f6] hover:text-[#26354c]"
                    aria-label={showKey ? '隐藏 API Key' : '显示 API Key'}
                    title={showKey ? '隐藏 API Key' : '显示 API Key'}
                  >
                    {showKey ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}
                  </button>
                </div>
              </div>

              <div>
                <label htmlFor="ai-base-url" className="text-sm font-semibold text-[#3d4a60]">接口地址（Base URL）</label>
                <input
                  id="ai-base-url"
                  type="url"
                  value={draft.baseUrl}
                  onChange={(event) => updateDraft('baseUrl', event.target.value)}
                  placeholder="https://api.example.com/v1"
                  spellCheck={false}
                  className="mt-2 h-12 w-full rounded-md border border-[#cfd8e5] bg-[#fbfcfe] px-4 text-sm text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff]"
                />
              </div>

              <div>
                <label htmlFor="ai-model-name" className="text-sm font-semibold text-[#3d4a60]">模型名称</label>
                <input
                  id="ai-model-name"
                  value={draft.model}
                  onChange={(event) => updateDraft('model', event.target.value)}
                  placeholder={draft.provider === 'deepseek' ? 'deepseek-v4-flash' : '例如 gpt-4o-mini'}
                  spellCheck={false}
                  className="mt-2 h-12 w-full rounded-md border border-[#cfd8e5] bg-[#fbfcfe] px-4 text-sm text-[#253249] outline-none transition focus:border-[#6f91e5] focus:ring-2 focus:ring-[#dce7ff]"
                />
              </div>

              <div className="flex gap-3 rounded-md border border-[#dce5f1] bg-[#f5f8fd] px-4 py-3 text-xs leading-5 text-[#5f6f86]">
                <ShieldCheckIcon className="mt-0.5 size-4 shrink-0 text-[#2e8a69]" aria-hidden="true" />
                <p>配置保存在当前浏览器中。分析或测试时会经后端转发给模型服务，后端不持久化 API Key；线上部署请启用 HTTPS。</p>
              </div>

              {fieldError && (
                <div role="alert" className="rounded-md border border-[#efb5ad] bg-[#fff4f2] px-4 py-3 text-sm text-[#963f35]">
                  {fieldError}
                </div>
              )}
              {testState !== 'idle' && (
                <div
                  aria-live="polite"
                  className={`flex items-center gap-2 rounded-md border px-4 py-3 text-sm ${
                    testState === 'success'
                      ? 'border-[#acd9c7] bg-[#effaf5] text-[#207355]'
                      : testState === 'error'
                        ? 'border-[#efb5ad] bg-[#fff4f2] text-[#963f35]'
                        : 'border-[#cbd9f2] bg-[#f2f6ff] text-[#4364a5]'
                  }`}
                >
                  {testState === 'testing' && <LoaderCircleIcon className="size-4 animate-spin" />}
                  {testState === 'success' && <CheckCircle2Icon className="size-4" />}
                  {testMessage}
                </div>
              )}
            </div>

            <div className="grid grid-cols-2 gap-3 border-t border-[#e3e8ef] px-5 py-4 sm:flex sm:items-center sm:px-7">
              <button
                type="button"
                onClick={handleClear}
                className="order-3 col-span-2 inline-flex h-10 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium text-[#6c7789] hover:bg-[#f2f4f7] hover:text-[#9a4035] sm:order-none sm:mr-auto"
              >
                <Trash2Icon className="size-4" /> 清除配置
              </button>
              <button
                type="button"
                onClick={handleTest}
                disabled={testState === 'testing'}
                className="inline-flex h-10 items-center justify-center gap-2 rounded-md border border-[#cfd8e5] bg-white px-4 text-sm font-medium text-[#344158] hover:bg-[#f7f9fc] disabled:opacity-50"
              >
                {testState === 'testing' ? <LoaderCircleIcon className="size-4 animate-spin" /> : <PlugZapIcon className="size-4" />}
                测试连接
              </button>
              <button
                type="submit"
                className="inline-flex h-10 items-center justify-center gap-2 rounded-md bg-[#1b2a45] px-5 text-sm font-semibold text-white hover:bg-[#263a5e]"
              >
                <SaveIcon className="size-4" /> 保存配置
              </button>
            </div>
          </form>
        </div>
      )}
    </AiModelContext.Provider>
  );
}

export function useAiModel() {
  const context = useContext(AiModelContext);
  if (!context) throw new Error('useAiModel must be used inside AiModelProvider');
  return context;
}

export function AiModelButton({ compact = false }: { compact?: boolean }) {
  const { config, isConfigured, openConfigurator } = useAiModel();
  const providerName = config.provider === 'deepseek' ? 'DeepSeek' : 'OpenAI 兼容';

  return (
    <button
      type="button"
      onClick={openConfigurator}
      className="inline-flex h-11 w-fit items-center gap-3 rounded-md border border-[#d5dce7] bg-white px-3.5 text-left text-[#334158] shadow-[0_1px_2px_rgba(18,31,52,0.04)] transition hover:border-[#aebdd5] hover:bg-[#f9fbfe]"
    >
      <Settings2Icon className="size-4 shrink-0 text-[#4f73ca]" aria-hidden="true" />
      <span className="min-w-0">
        <span className="block whitespace-nowrap text-sm font-semibold">配置 AI 模型</span>
        {!compact && (
          <span className="mt-0.5 block max-w-44 truncate text-[11px] text-[#7b889b]">
            {isConfigured ? `${providerName} · ${config.model}` : 'DeepSeek · 待配置'}
          </span>
        )}
      </span>
      <span
        className={`size-2 shrink-0 rounded-full ${isConfigured ? 'bg-[#2b936b]' : 'bg-[#d69a33]'}`}
        aria-label={isConfigured ? '模型已配置' : '模型待配置'}
      />
    </button>
  );
}
