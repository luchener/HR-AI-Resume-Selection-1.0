const { chromium } = require('../apps/frontend/node_modules/playwright');
const fs = require('fs');

const BASE = process.env.UI_BASE ? process.env.UI_BASE : 'http://127.0.0.1:13030';
const USER = process.env.UI_USER || '';
const PASS = process.env.UI_PASS || '';
const OUT = process.env.UI_OUT ? process.env.UI_OUT : '.test-tmp/ui-acceptance-result.json';
const DOCX = process.env.UI_DOCX ? process.env.UI_DOCX : '.test-tmp/candidate-sample.docx';
const SEARCH_ONLY = process.env.UI_SCENARIO === 'search';
if (!USER || !PASS) {
  console.error('UI_USER and UI_PASS are required');
  process.exit(2);
}

async function armScreeningCapture(page, webSearch) {
  await page.evaluate(({ webSearch }) => {
    const original = window.fetch;
    window.fetch = async function (input, options) {
      const response = await original.call(this, input, options);
      const url = new URL(response.url || (input instanceof Request ? input.url : input), location.href);
      const method = options?.method || (input instanceof Request ? input.method : 'GET');
      if (url.origin !== location.origin || url.pathname !== '/api/v1/resumes/hr-analysis' ||
          method.toUpperCase() !== 'POST') return response;
      window.fetch = original;
      const requestBody = typeof options?.body === 'string' ? JSON.parse(options.body) : {};
      const sentWebSearch = requestBody.agent_config?.web_search === true;
      if (!response.ok || !response.headers.get('content-type')?.includes('text/event-stream') ||
          sentWebSearch !== webSearch) {
        void window.acceptanceScreeningStarted({ status: response.status, sentWebSearch, requested: true,
          contentType: response.headers.get('content-type') || '' });
        return response;
      }
      const reader = response.clone().body.getReader();
      void (async () => {
        const decoder = new TextDecoder();
        let buffer = '';
        let firstFrame = null;
        let bytes = 0;
        let chunks = 0;
        let sawSeparator = false;
        let parseErrors = 0;
        let readResult = 'pending';
        const timer = setTimeout(() => {
          if (readResult === 'pending') {
            readResult = 'timeout';
            void window.acceptanceScreeningStarted({ status: response.status, sentWebSearch, firstFrame,
              bytes, chunks, sawSeparator, parseErrors, readResult, requested: true,
              contentType: response.headers.get('content-type') || '' });
          }
        }, 30000);
        try {
          while (true) {
            const { value, done } = await reader.read();
            if (done) { readResult = 'eof'; break; }
            chunks += 1;
            bytes += value ? value.byteLength : 0;
            buffer += decoder.decode(value || new Uint8Array(), { stream: true });
            let separator;
            while ((separator = /\r?\n\r?\n/.exec(buffer))) {
              sawSeparator = true;
              const frame = buffer.slice(0, separator.index);
              buffer = buffer.slice(separator.index + separator[0].length);
              const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:'))
                .map(line => line.slice(5).replace(/^ /, '')).join('\n');
              if (!data) continue;
              let event;
              try { event = JSON.parse(data); } catch { parseErrors += 1; continue; }
              if (!firstFrame) firstFrame = event.status || event.type || null;
              if (event.status === 'starting' && /^[\da-f-]{36}$/i.test(event.task_id || '')) {
                readResult = 'starting';
                clearTimeout(timer);
                await window.acceptanceScreeningStarted({ taskId: event.task_id, sentWebSearch,
                  status: response.status, contentType: response.headers.get('content-type') || '',
                  firstFrame: 'starting', bytes, chunks, sawSeparator, parseErrors, readResult });
                return;
              }
            }
          }
        } catch {
          readResult = 'error';
        } finally {
          clearTimeout(timer);
          void reader.cancel().catch(() => {});
        }
        if (readResult !== 'timeout') {
          await window.acceptanceScreeningStarted({ status: response.status, sentWebSearch, firstFrame,
            bytes, chunks, sawSeparator, parseErrors, readResult, requested: true,
            contentType: response.headers.get('content-type') || '' });
        }
      })();
      return response;
    };
  }, { webSearch });
}

async function runScreening(page, token, webSearch) {
  let capture;
  const started = new Promise(resolve => { capture = resolve; });
  // The test runs one screening per workspace document.
  await page.exposeFunction('acceptanceScreeningStarted', capture);
  await armScreeningCapture(page, webSearch);
  await page.getByRole('button', { name: '开始分析' }).click();
  let timer;
  let start;
  try {
    start = await Promise.race([started, new Promise(resolve => { timer = setTimeout(resolve, 60000); })]);
  } finally {
    clearTimeout(timer);
  }
  if (!start?.taskId) throw new Error('Screening SSE starting event missing: ' + JSON.stringify(start || { timeout: true }));

  const deadline = Date.now() + 300000;
  let task;
  while (Date.now() < deadline) {
    const response = await page.request.get(BASE + '/api/v1/analysis-tasks/' + start.taskId, {
      headers: { Authorization: 'Bearer ' + token },
      timeout: Math.min(10000, Math.max(1, deadline - Date.now())),
    });
    if (!response.ok()) throw new Error('Screening task lookup returned HTTP ' + response.status());
    task = (await response.json()).data;
    if (['completed', 'failed', 'partial_success', 'cancelled'].includes(task?.status)) break;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  if (task?.task_id !== start.taskId || task.web_search !== webSearch || task.status !== 'completed' ||
      !task.analyses?.some(item => item.status === 'completed' && item.hr_analysis)) {
    throw new Error(JSON.stringify({ taskId: start.taskId, status: task?.status, webSearch: task?.web_search,
      candidateErrors: task?.analyses?.map(item => item.error_code).filter(Boolean) }));
  }
  return { taskId: start.taskId, webSearch: task.web_search };
}

(async () => {
  const result = { checks: [], failures: [] };
  const record = (name, ok, detail) => {
    result.checks.push({ name, ok, detail });
    if (!ok) result.failures.push({ name, detail });
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail === undefined ? '' : ' :: ' + JSON.stringify(detail)}`);
  };
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const context = await browser.newContext({ locale: 'zh-CN', acceptDownloads: true });
    const page = await context.newPage();
    const pageErrors = [];
    page.on('pageerror', e => pageErrors.push(String(e).slice(0, 150)));

    await page.goto(BASE + '/login', { waitUntil: 'networkidle', timeout: 60000 });
    await page.fill('#login-username', USER);
    await page.fill('#login-password', PASS);
    const loginWait = page.waitForResponse(r => r.url().includes('/api/v1/auth/login') && r.request().method() === 'POST', { timeout: 60000 });
    await page.click('button[type=submit]');
    let loginStatus = null;
    let loginBody = {};
    try {
      const loginResponse = await loginWait;
      loginStatus = loginResponse.status();
      try { loginBody = await loginResponse.json(); } catch {}
    } catch {}
    const token = (loginBody.data || {}).token || '';
    record('login_response', loginStatus === 200 && Boolean(token), {
      status: loginStatus,
      error: loginBody.detail ? String(loginBody.detail).slice(0, 80) : null,
      token_present: Boolean(token),
    });
    await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 30000 });
    record('login_redirects_to_workspace', true, page.url());

    await page.setInputFiles('input[type=file]', DOCX);
    if (SEARCH_ONLY) {
      await page.fill('#job-description', '合成岗位：Python 后端工程师，要求 PostgreSQL、Docker 与检索核验。');
      await page.check('input[type=checkbox]');
      record('search_task_completed_with_report', true, await runScreening(page, token, true));
      await page.waitForSelector('text=候选人分析报告', { timeout: 30000 });
      record('search_report_rendered', true);
      record('no_page_errors', pageErrors.length === 0, pageErrors);
      result.passed = result.failures.length === 0;
      return;
    }
    await page.fill('#job-description', '合成岗位：Python 后端工程师，要求 PostgreSQL 与 Docker 实践，3 年以上经验。');
    record('screening_task_completed_with_report', true, await runScreening(page, token, false));
    await page.waitForFunction(() => location.pathname.includes('/dashboard'), null, { timeout: 180000 });
    await page.waitForSelector('text=候选人分析报告', { timeout: 30000 });
    const reportText = await page.locator('body').innerText();
    record('report_rendered', reportText.includes('候选人分析报告'));
    record('report_has_score', /\d+\s*\/\s*100/.test(reportText));

    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    const afterReload = await page.locator('body').innerText();
    record('report_survives_reload', afterReload.includes('候选人分析报告'), page.url());

    const improveResponse = page.waitForResponse(r => r.url().includes('/api/v1/resumes/improve') && r.request().method() === 'POST', { timeout: 120000 });
    await page.getByRole('button', { name: /深度优化简历/ }).click();
    const improve = await improveResponse;
    record('improvement_request_ok', improve.status() === 200, improve.status());

    let markdownRendered = false;
    try {
      await page.waitForFunction(() => document.body.innerText.includes('Step 5'), null, { timeout: 300000 });
      markdownRendered = true;
    } catch {}
    const optimizedText = await page.locator('body').innerText();
    record('improvement_markdown_rendered', markdownRendered, optimizedText.slice(-160));

    let taskId = null;
    for (let i = 0; i < 60 && !taskId; i++) {
      taskId = await page.evaluate(() => {
        const keys = [...Object.keys(sessionStorage), ...Object.keys(localStorage)];
        for (const key of keys) {
          try {
            const value = JSON.parse(localStorage.getItem(key) || sessionStorage.getItem(key) || '');
            const id = value && value.data && value.data.improvement_task_id;
            if (id) return id;
          } catch {}
        }
        return null;
      });
      if (!taskId) await page.waitForTimeout(1000);
    }
    record('improvement_task_id_captured', Boolean(taskId), taskId);

    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(1500);
    const persisted = taskId ? await page.evaluate(async (id) => {
      const token = localStorage.getItem('resume-screening-token');
      const response = await fetch('/api/v1/analysis-tasks/' + id, { headers: { Authorization: 'Bearer ' + token } });
      const body = await response.json();
      const data = body.data || body;
      return { status: data.status, chars: ((data.improvement || {}).analysis_result || '').length,
               text: ((data.improvement || {}).analysis_result || '').slice(0, 40) };
    }, taskId) : { status: 'no-task-id', chars: 0 };
    record('improvement_persisted_api', persisted.status === 'completed' && persisted.chars > 0, persisted);
    // UI re-view: toggle to optimized if the page still offers it after reload
    let uiRevisited = false;
    const toggle = page.getByRole('button', { name: '优化简历' });
    if (await toggle.count()) {
      await toggle.first().click();
      await page.waitForTimeout(800);
      uiRevisited = (await page.locator('body').innerText()).includes('Step 5');
    }
    record('improvement_ui_revisit_after_reload', uiRevisited, { toggle: await toggle.count() });

    const analysisTab = page.getByRole('button', { name: '分析报告' });
    if (await analysisTab.count()) {
      await analysisTab.first().click();
      await page.waitForTimeout(600);
    }
    await page.getByRole('button', { name: /归档到人才库/ }).click();
    await page.waitForSelector('[role=dialog]', { timeout: 15000 });
    await page.getByRole('button', { name: '确认归档' }).click();
    await page.waitForSelector('text=已归档', { timeout: 60000 });
    record('archive_saved', true);

    await page.goto(BASE + '/archives', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1200);
    const archiveText = await page.locator('body').innerText();
    record('archive_list_visible', archiveText.includes('合成候选人'), archiveText.slice(0, 80));
    await page.getByRole('button', { name: '详情' }).first().click();
    await page.waitForTimeout(1200);
    const detailText = await page.locator('body').innerText();
    record('archive_detail_readable', detailText.includes('合成候选人') && detailText.length > 200, detailText.slice(0, 80));

    record('no_page_errors', pageErrors.length === 0, pageErrors);
    result.passed = result.failures.length === 0;
  } catch (error) {
    record('harness_error', false, String((error && error.message) || error).slice(0, 300));
    result.passed = false;
  } finally {
    await browser.close().catch(() => {});
    fs.writeFileSync(OUT, JSON.stringify(result, null, 2));
    console.log('\nRESULT ' + JSON.stringify({ passed: result.passed, failures: result.failures.length }));
    process.exitCode = result.passed ? 0 : 1;
  }
})();
