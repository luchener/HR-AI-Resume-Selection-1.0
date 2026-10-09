'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertCircleIcon, CheckCircle2Icon, LoaderCircleIcon } from 'lucide-react';
import ConfirmDialog from './confirm-dialog';
import {
  clearAdminLlmConfig,
  fetchAdminLlmConfig,
  fetchAdminLlmModels,
  saveAdminLlmConfig,
  testAdminLlmConnection,
  type AdminLlmConfig,
  type AdminLlmSource,
} from '@/lib/api/system-llm';

/** 拉不到模型列表时的常用取值，直接手填也能跑 */
const COMMON_MODELS = ['deepseek-chat', 'deepseek-reasoner', 'deepseek-v4-flash'];
const CUSTOM_VALUE = '__custom__';

type Feedback = { kind: 'ok' | 'err'; text: string };

function sourceBadge(source: AdminLlmSource, ready: boolean): { label: string; cls: string } {
  if (source === 'ui') {
    return ready
      ? { label: '界面配置', cls: 'border-brand-soft bg-brand-soft text-brand-deep' }
      : { label: '界面配置 · 未启用', cls: 'border-line bg-soft text-sub' };
  }
  if (source === 'env') return { label: '.env', cls: 'border-warn-border bg-warn-soft text-warn' };
  return { label: '未配置', cls: 'border-line bg-soft text-sub' };
}

function smallButton(extra = '') {
  return [
    'inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border border-line bg-white px-3 text-[13px] text-ink',
    'transition-colors hover:bg-mist disabled:cursor-not-allowed disabled:text-disabled-fg',
    extra,
  ].join(' ');
}

/** 与原型一致的品牌色次要按钮（获取模型列表） */
function brandButton(extra = '') {
  return [
    'inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border border-brand-soft bg-brand-soft px-3 text-[13px] font-medium text-brand-deep',
    'transition-colors hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60',
    extra,
  ].join(' ');
}

function ToggleSwitch({ checked, onChange, disabled }: {
  checked: boolean; onChange: (value: boolean) => void; disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${checked ? 'bg-brand-deep' : 'bg-line'} ${disabled ? 'opacity-50' : ''}`}
    >
      <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${checked ? 'left-[18px]' : 'left-0.5'}`} />
    </button>
  );
}

export default function AdminLlm() {
  const [config, setConfig] = useState<AdminLlmConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<Feedback | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [busy, setBusy] = useState<'' | 'save' | 'test' | 'models' | 'clear'>('');
  const [confirmClear, setConfirmClear] = useState(false);

  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [timeout, setTimeoutValue] = useState(300);
  const [enabled, setEnabled] = useState(true);
  const [models, setModels] = useState<string[]>([]);
  const [customModel, setCustomModel] = useState(false);

  const apply = useCallback((data: AdminLlmConfig) => {
    setConfig(data);
    setBaseUrl(data.base_url || '');
    setModel(data.model || '');
    setTimeoutValue(data.timeout || 300);
    setEnabled(data.enabled);
    setModels(data.models || []);
    setApiKey('');
    setCustomModel(Boolean(data.model) && (data.models || []).length > 0 && !(data.models || []).includes(data.model));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      apply(await fetchAdminLlmConfig());
      setNotice(null);
    } catch (error) {
      setNotice({ kind: 'err', text: error instanceof Error ? error.message : '读取模型配置失败。' });
    } finally {
      setLoading(false);
    }
  }, [apply]);

  useEffect(() => { void load(); }, [load]);

  const options = useMemo(() => {
    const list = [...models];
    for (const item of COMMON_MODELS) if (!list.includes(item)) list.push(item);
    if (model && !list.includes(model)) list.push(model);
    return list;
  }, [models, model]);

  const patch = useCallback((data: AdminLlmConfig, text: string) => {
    apply(data);
    setNotice({ kind: 'ok', text });
  }, [apply]);

  const onSave = useCallback(async () => {
    setBusy('save');
    setFeedback(null);
    try {
      const payload: Record<string, unknown> = {
        base_url: baseUrl.trim(),
        model: model.trim(),
        timeout: Number(timeout) || 300,
        enabled,
      };
      if (apiKey.trim()) payload.api_key = apiKey.trim();
      const data = await saveAdminLlmConfig(payload);
      patch(data, '已保存。新配置立即生效，无需重启服务。');
    } catch (error) {
      setNotice({ kind: 'err', text: error instanceof Error ? error.message : '保存失败。' });
    } finally {
      setBusy('');
    }
  }, [apiKey, baseUrl, enabled, model, patch, timeout]);

  const onTest = useCallback(async () => {
    setBusy('test');
    setFeedback(null);
    try {
      const body: Record<string, string> = {};
      if (apiKey.trim()) body.api_key = apiKey.trim();
      if (baseUrl.trim()) body.base_url = baseUrl.trim();
      if (model.trim()) body.model = model.trim();
      const result = await testAdminLlmConnection(body);
      setFeedback({ kind: 'ok', text: `${result.message}（${result.model} · ${result.base_url}）` });
    } catch (error) {
      setFeedback({ kind: 'err', text: error instanceof Error ? error.message : '测试失败。' });
    } finally {
      setBusy('');
    }
  }, [apiKey, baseUrl, model]);

  const onFetchModels = useCallback(async () => {
    setBusy('models');
    setFeedback(null);
    try {
      const body: Record<string, string> = {};
      if (apiKey.trim()) body.api_key = apiKey.trim();
      if (baseUrl.trim()) body.base_url = baseUrl.trim();
      const result = await fetchAdminLlmModels(body);
      apply(result.config);
      if (result.count > 0 && !model.trim()) setModel(result.models[0]);
      setFeedback({
        kind: 'ok',
        text: `已获取 ${result.count} 个模型（${result.seconds}s），已填入下拉框。`,
      });
    } catch (error) {
      setFeedback({ kind: 'err', text: error instanceof Error ? error.message : '获取模型列表失败。' });
    } finally {
      setBusy('');
    }
  }, [apiKey, apply, baseUrl, model]);

  const onClear = useCallback(async () => {
    setBusy('clear');
    setConfirmClear(false);
    setFeedback(null);
    try {
      const data = await clearAdminLlmConfig();
      patch(data, data.source === 'env'
        ? '已清空界面配置，当前使用服务器 .env 里的模型配置。'
        : '已清空界面配置，当前没有可用的服务端模型配置。');
    } catch (error) {
      setNotice({ kind: 'err', text: error instanceof Error ? error.message : '清空失败。' });
    } finally {
      setBusy('');
    }
  }, [patch]);

  const badge = config ? sourceBadge(config.source, config.ready) : null;
  const active = config?.source ?? 'none';
  const disabled = busy !== '' || loading;

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-ink">模型配置</h2>
        <p className="mt-1 text-[13px] text-sub">
          配置服务器这一层的模型密钥与接口地址。保存在这里后立即生效，无需登录服务器、无需重启。
        </p>
      </div>

      <div className="flex items-start gap-2 rounded-md border border-brand-soft bg-brand-soft px-3 py-2.5 text-[13px] text-ink">
        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand-deep" />
        <div>
          <span className="font-medium">
            当前生效：{badge ? badge.label : '读取中'}
            {config?.base_url ? `（${config.model} · ${config.base_url.replace(/^https?:\/\//, '')}）` : ''}
          </span>
          <span className="text-sub">
            。用户在自己浏览器里填写过 Key 的请求，仍然走用户自己的 Key 与地址——这里的配置只服务于「用户没填」的情况。
          </span>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 text-[12px] text-sub">
        <span>优先级：</span>
        {[
          { key: 'user', label: '① 用户自带 Key', on: false },
          { key: 'ui', label: '② 界面配置', on: active === 'ui' },
          { key: 'env', label: '③ .env', on: active === 'env' },
        ].map((item) => (
          <span
            key={item.key}
            className={`rounded-full border px-2.5 py-0.5 ${item.on
              ? 'border-brand-soft bg-brand-soft font-medium text-brand-deep'
              : 'border-line bg-white text-sub opacity-70'}`}
          >
            {item.label}{item.on ? '（当前生效）' : ''}
          </span>
        ))}
      </div>

      {notice && (
        <div className={`flex items-start gap-2 rounded-md border px-3 py-2 text-[13px] ${notice.kind === 'ok'
          ? 'border-brand-soft bg-brand-soft text-brand-deep'
          : 'border-warn-border bg-warn-soft text-warn'}`}>
          {notice.kind === 'ok'
            ? <CheckCircle2Icon className="mt-0.5 h-4 w-4 shrink-0" />
            : <AlertCircleIcon className="mt-0.5 h-4 w-4 shrink-0" />}
          <span>{notice.text}</span>
        </div>
      )}

      <section className="rounded-lg border border-line bg-white">
        <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
          <h3 className="text-[14px] font-semibold text-ink">服务器端配置</h3>
          {badge && <span className={`rounded-full border px-2 py-0.5 text-[11px] ${badge.cls}`}>{badge.label}</span>}
          {config?.ready && (
            <span className="rounded-full border border-good-soft bg-good-soft px-2 py-0.5 text-[11px] text-good-deep">
              可调用
            </span>
          )}
          <span className="ml-auto text-[11px] text-sub">仅超级管理员可见 · 落盘权限 0600</span>
        </header>

        {loading ? (
          <div className="flex items-center gap-2 px-4 py-8 text-[13px] text-sub">
            <LoaderCircleIcon className="h-4 w-4 animate-spin" />读取中…
          </div>
        ) : (
          <div className="space-y-4 px-4 py-4">
            <div>
              <label className="mb-1 block text-[12px] text-sub" htmlFor="llm-base-url">接口地址（base_url）</label>
              <div className="flex items-center gap-2">
                <input
                  id="llm-base-url"
                  type="text"
                  value={baseUrl}
                  disabled={disabled}
                  onChange={(event) => setBaseUrl(event.target.value)}
                  placeholder="https://api.deepseek.com"
                  className="h-8 min-w-0 flex-1 rounded-md border border-line px-2.5 font-mono text-[13px] text-ink outline-none focus:border-brand-deep disabled:bg-soft"
                />
                <button type="button" className={brandButton()} disabled={disabled} onClick={() => void onFetchModels()}>
                  {busy === 'models' && <LoaderCircleIcon className="h-3.5 w-3.5 animate-spin" />}
                  获取模型列表
                </button>
              </div>
              <p className="mt-1 text-[11.5px] text-sub">
                必须以 https:// 开头；内网、回环、云元数据地址会被拒绝。部分服务商需要带 /v1
              </p>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label className="mb-1 block text-[12px] text-sub" htmlFor="llm-api-key">API Key</label>
                <input
                  id="llm-api-key"
                  type="password"
                  value={apiKey}
                  disabled={disabled}
                  onChange={(event) => setApiKey(event.target.value)}
                  autoComplete="off"
                  placeholder={config?.api_key_set ? `已设置（${config.api_key_mask}）· 留空表示不修改` : '尚未设置，请填写'}
                  className="h-8 w-full rounded-md border border-line px-2.5 text-[13px] text-ink outline-none focus:border-brand-deep disabled:bg-soft"
                />
                <p className="mt-1 text-[11.5px] text-sub">服务商控制台生成的 Key，仅服务端使用，页面永不回显</p>
              </div>

              <div>
                <label className="mb-1 block text-[12px] text-sub" htmlFor="llm-model">默认模型</label>
                {models.length > 0 && !customModel ? (
                  <select
                    id="llm-model"
                    value={model}
                    disabled={disabled}
                    onChange={(event) => {
                      if (event.target.value === CUSTOM_VALUE) { setCustomModel(true); return; }
                      setModel(event.target.value);
                    }}
                    className="h-8 w-full rounded-md border border-line bg-white px-2 text-[13px] text-ink outline-none focus:border-brand-deep disabled:bg-soft"
                  >
                    {options.map((item) => <option key={item} value={item}>{item}</option>)}
                    <option value={CUSTOM_VALUE}>＋ 自定义（手动填写）</option>
                  </select>
                ) : (
                  <div className="flex items-center gap-2">
                    <input
                      id="llm-model"
                      type="text"
                      value={model}
                      disabled={disabled}
                      onChange={(event) => setModel(event.target.value)}
                      placeholder="deepseek-chat"
                      className="h-8 min-w-0 flex-1 rounded-md border border-line px-2.5 font-mono text-[13px] text-ink outline-none focus:border-brand-deep disabled:bg-soft"
                    />
                    {models.length > 0 && (
                      <button type="button" className={smallButton()} disabled={disabled} onClick={() => setCustomModel(false)}>
                        选列表
                      </button>
                    )}
                  </div>
                )}
                <p className="mt-1 text-[11.5px] text-sub">
                  {config?.models_fetched_at
                    ? `已获取 ${models.length} 个模型 · ${new Date(config.models_fetched_at).toLocaleString('zh-CN')}`
                    : '尚未获取模型列表，可直接手动填写；用户请求没带模型时使用'}
                </p>
              </div>

              <div>
                <label className="mb-1 block text-[12px] text-sub" htmlFor="llm-timeout">超时（秒）</label>
                <input
                  id="llm-timeout"
                  type="number"
                  min={1}
                  max={3600}
                  value={timeout}
                  disabled={disabled}
                  onChange={(event) => setTimeoutValue(Number(event.target.value))}
                  className="h-8 w-full rounded-md border border-line px-2.5 text-[13px] text-ink outline-none focus:border-brand-deep disabled:bg-soft"
                />
                <p className="mt-1 text-[11.5px] text-sub">长简历分析耗时较长，一般不用改</p>
              </div>

              <div>
                <label className="mb-1 block text-[12px] text-sub">启用这套配置</label>
                <div className="flex h-8 items-center gap-2 text-[13px] text-ink">
                  <ToggleSwitch checked={enabled} disabled={disabled} onChange={setEnabled} />
                  <span>{enabled ? '已启用' : '已关闭（将使用 .env 里的配置）'}</span>
                </div>
                {config?.updated_at && (
                  <p className="mt-1 text-[11.5px] text-sub">
                    最近更新：{new Date(config.updated_at).toLocaleString('zh-CN')}
                    {config.updated_by ? ` · ${config.updated_by}` : ''}
                  </p>
                )}
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2 border-t border-dashed border-line pt-3">
              <button
                type="button"
                disabled={disabled}
                onClick={() => void onSave()}
                className="inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md bg-brand-deep px-3.5 text-[13px] font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-60"
              >
                {busy === 'save' && <LoaderCircleIcon className="h-3.5 w-3.5 animate-spin" />}
                保存
              </button>
              <button type="button" className={smallButton()} disabled={disabled} onClick={() => void onTest()}>
                {busy === 'test' && <LoaderCircleIcon className="h-3.5 w-3.5 animate-spin" />}
                测试API连接
              </button>
              <button type="button" className={brandButton()} disabled={disabled} onClick={() => void onFetchModels()}>
                获取模型列表
              </button>
              {config?.source === 'ui' && (
                <button
                  type="button"
                  className={smallButton('text-warn')}
                  disabled={disabled}
                  onClick={() => setConfirmClear(true)}
                >
                  清空界面配置
                </button>
              )}
              {feedback && (
                <span className={`ml-auto flex items-center gap-1.5 text-[12px] ${feedback.kind === 'ok' ? 'text-good-deep' : 'text-warn'}`}>
                  {feedback.kind === 'ok'
                    ? <CheckCircle2Icon className="h-3.5 w-3.5" />
                    : <AlertCircleIcon className="h-3.5 w-3.5" />}
                  {feedback.text}
                </span>
              )}
            </div>
          </div>
        )}
      </section>

      <ConfirmDialog
        open={confirmClear}
        danger
        busy={busy === 'clear'}
        title="清空界面配置？"
        message="清空后立即回退到服务器 .env 里的模型配置（若 .env 也未配置，则服务端没有可用的模型密钥）。"
        confirmLabel="清空"
        onCancel={() => setConfirmClear(false)}
        onConfirm={() => void onClear()}
      />
    </div>
  );
}
