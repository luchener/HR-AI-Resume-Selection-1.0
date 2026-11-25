/* Real browser protocol/session tests, synthetic loopback HTTP only. */
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const cp = require('node:child_process');
const root = path.resolve(__dirname, '..');
const frontend = path.join(root, 'apps/frontend');
const ts = require(path.join(frontend, 'node_modules/typescript'));
const { chromium } = require(path.join(frontend, 'node_modules/playwright'));
const files = ['apps/frontend/lib/api/sql-analysis-stream.ts', 'apps/frontend/lib/api/screening.ts',
  'apps/frontend/lib/api/config.ts', 'apps/frontend/lib/analysis-session.ts',
  'apps/frontend/components/workbench/auth-context.tsx', 'apps/frontend/components/workbench/analysis-context.tsx',
  'apps/frontend/components/workbench/analysis-workbench.tsx', 'apps/frontend/app/(default)/dashboard/page.tsx',
  'scripts/test-sql-analysis-browser.cjs'];
const digest = name => crypto.createHash('sha256').update(fs.readFileSync(path.join(root, name), 'utf8').replace(/\r\n?/g, '\n')).digest('hex');
const hashes = () => Object.fromEntries(files.map(name => [name, digest(name)]));
const git = (...args) => cp.execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
const before = hashes();
const commit = git('rev-parse', 'HEAD'), tree = git('rev-parse', 'HEAD^{tree}');
const R = '11111111-1111-4111-8111-111111111111', R2 = '22222222-2222-4222-8222-222222222222';
const J = '33333333-3333-4333-8333-333333333333', T = '44444444-4444-4444-8444-444444444444';
const normalizedReport = JSON.parse(cp.execFileSync('python', ['-c',
  "import json; from hr_normalization import normalize_hr_analysis,NormalizationPolicy; print(json.dumps(normalize_hr_analysis({'candidate_name':'合成候选人','job_fit_score':82,'agent_validation':{'passed':True,'issues':[]}},policy=NormalizationPolicy(DECEPTION_CHECK_MODE='off'))))"],
{ cwd: path.join(root, 'apps/backend'), encoding: 'utf8' }));
const snapshot = { task_id: T, status: 'completed', total_candidates: 1, completed_candidates: 1, failed_candidates: 0,
  analyses: [{ resume_id: R, job_id: J, status: 'completed', hr_analysis: normalizedReport }] };
const result = { data: snapshot };
const stage = (id, status) => ({ id, status, task_id: T, resume_id: R, message: '合成进度' });
const frame = event => 'data: ' + JSON.stringify(event) + '\n\n';
const final = () => frame({ status: 'completed', result });
const I = '55555555-5555-4555-8555-555555555555';
const optimized = '# Synthetic Optimized Candidate\n## Experience\nPython API delivery';
const improvement = { data: { task_id: I, kind: 'improvement', status: 'completed', total_candidates: 1,
  completed_candidates: 1, failed_candidates: 0, analyses: [],
  improvement: { resume_id: R, job_id: J, analysis_result: optimized, error_code: null } } };
const improved = () => frame({ status: 'completed', result: improvement });
const requests = [], states = new Map();
const compile = (filename, rewrites = {}) => {
  let source = ts.transpileModule(fs.readFileSync(path.join(frontend, filename), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 },
  }).outputText;
  for (const [from, to] of Object.entries(rewrites)) source = source.split(from).join(to);
  return source;
};
const modules = {
  '/transport.js': compile('lib/api/sql-analysis-stream.ts'),
  '/session.js': compile('lib/analysis-session.ts', { "'react'": "'/react.js'", "'@/lib/api/screening'": "'/screening-stub.js'",
    "'@/components/workbench/auth-context'": "'/auth-stub.js'", "'@/lib/api/config'": "'/sql-config.js'" }),
  '/actual-screening.js': compile('lib/api/screening.ts', { "'@/components/workbench/auth-context'": "'/auth-stub.js'",
    "'./config'": "'/legacy-config.js'", "'./sql-analysis-stream'": "'/transport.js'" }),
  '/actual-sql-screening.js': compile('lib/api/screening.ts', { "'@/components/workbench/auth-context'": "'/auth-stub.js'",
    "'./config'": "'/sql-api-config.js'", "'./sql-analysis-stream'": "'/transport.js'" }),
  '/react.js': 'export const useSyncExternalStore=(_subscribe,get)=>get();',
  '/auth-stub.js': "export const getStoredToken=()=>window.fixtureToken;",
  '/sql-config.js': "export const SQL_ANALYSIS_ENABLED=true; export const API_URL='';",
  '/sql-api-config.js': "export const SQL_ANALYSIS_ENABLED=true; export const API_URL='/case/sql-api';",
  '/legacy-config.js': "export const SQL_ANALYSIS_ENABLED=false; export const API_URL='/case/legacy';",
  '/screening-stub.js': `export {SqlAnalysisConnectionError} from '/transport.js';
    export const resetSqlImprovementRequests=()=>{};
    export const uploadResume=(...args)=>window.sessionAPI.upload(...args);
    export const uploadJobDescription=(...args)=>window.sessionAPI.job(...args);
    export const analyzeResumesStream=(...args)=>window.sessionAPI.analyze(...args);`,
};
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><title>SQL stream rehearsal</title>'); return; }
    if (modules[url.pathname]) { res.setHeader('Content-Type', 'text/javascript'); res.end(modules[url.pathname]); return; }
    if (url.pathname === '/frames') {
      res.setHeader('Content-Type', 'text/event-stream');
      const bytes = Buffer.from('\ufeff: comment\r\nid: 7\r\ndata: {"message":\r\ndata: "中文"}\r\n\r\n');
      for (let i = 0; i < bytes.length; i += 2) res.write(bytes.subarray(i, i + 2));
      res.end(); return;
    }
    const match = /^\/case\/([^/]+)\/(.*)$/.exec(url.pathname);
    if (!match) { res.writeHead(404).end(); return; }
    const name = match[1];
    const state = states.get(name) || { posts: 0, gets: 0, resumed: false, closed: 0 };
    states.set(name, state);
    let body = ''; for await (const chunk of req) body += chunk;
    requests.push({ name, method: req.method, key: req.headers['idempotency-key'], cursor: req.headers['last-event-id'],
      auth: req.headers.authorization, body, path: match[2] });
    if (req.method === 'POST') state.posts++; else state.gets++;
    res.on('close', () => state.closed++);
    if (name === 'improve-source-missing' || name === 'improve-task-missing') {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ detail: name === 'improve-source-missing'
        ? 'Resume or job not found or unavailable' : 'Task not found' }));
      return;
    }
    if (['improve', 'improve-lost', 'sql-api'].includes(name)) {
      res.setHeader('Content-Type', 'text/event-stream');
      if (name === 'improve-lost' && state.posts === 1) { res.destroy(); return; }
      if (name === 'sql-api' && req.method === 'POST') {
        res.end(frame({ status: 'starting', task_id: I }) + frame({ ...stage(1, 'generating'), task_id: I })); return;
      }
      if (name === 'sql-api' && !state.resumed) { res.writeHead(503).end(); return; }
      res.end(frame({ status: 'starting', task_id: I }) + improved()); return;
    }
    if (name === 'ui') {
      const jsonReply = data => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); };
      if (match[2] === 'api/v1/auth/me') { jsonReply({ data: { user_id: R, username: 'synthetic-browser', is_admin: false } }); return; }
      if (match[2] === 'api/v1/quota/me') { jsonReply({ data: { analysis_enabled: true, unlimited: true } }); return; }
      if (match[2] === 'api/v1/resumes/upload') { jsonReply({ resume_id: R }); return; }
      if (match[2] === 'api/v1/jobs/upload') { jsonReply({ job_id: [J] }); return; }
      if (match[2] === 'api/v1/resumes/review-markers') { jsonReply({ data: { candidate_name: '合成候选人', annotations: [],
        summary: { final_score: 82, recommendation: '合成报告', highlights: 0, risks: 0 }, notice: '合成标记' } }); return; }
      if (match[2] === 'api/v1/resumes/improve') {
        res.setHeader('Content-Type', 'text/event-stream'); res.end(frame({ status: 'starting', task_id: I }) + improved()); return;
      }
      if (match[2] === 'api/v1/resumes/hr-analysis') {
        res.setHeader('Content-Type', 'text/event-stream'); res.end(frame({ status: 'starting', task_id: T }) + frame(stage(1, 'planning'))); return;
      }
      if (!match[2].endsWith('/events')) { jsonReply({ data: [] }); return; }
      if (!state.resumed) { res.writeHead(503).end(); return; }
    }
    if (name === 'unauthorized') { res.writeHead(401).end(); return; }
    if (name === 'changed') { setTimeout(() => res.writeHead(401).end(), 100); return; }
    if (name === 'lost' && state.posts === 1) { res.destroy(); return; }
    if ((name === 'outage' && state.gets > 0 && !state.resumed) || name === 'backoff') { res.writeHead(503).end(); return; }
    res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
    if (name === 'bad') { res.end('data: {invalid}\n\n'); return; }
    if (name === 'legacy') { res.end(frame({ status: 'planning', message: '旧服务' }) + frame({ status: 'completed',
      result: { data: { resume_id: R, job_id: J, hr_analysis: { final_score: 82 } } } })); return; }
    if (name === 'cancel') { res.write(frame({ status: 'starting', task_id: T })); return; }
    if ((name === 'reconnect' || name === 'outage') && req.method === 'POST') {
      res.end(frame({ status: 'starting', task_id: T }) + frame(stage(1, 'planning'))); return;
    }
    if (req.method === 'GET') {
      res.end('id: 1\n' + frame(stage(1, 'planning')) + 'id: 2\n' + frame(stage(2, 'candidate_completed')) + final()); return;
    }
    res.end(frame({ status: 'starting', task_id: T }) + frame(stage(1, 'candidate_completed')) + final());
  } catch { if (!res.destroyed) res.destroy(); }
});

(async () => {
  let browser, next;
  let uiPort;
  const checks = [];
  const outputIndex = process.argv.indexOf('--evidence');
  const target = outputIndex >= 0 ? path.resolve(process.argv[outputIndex + 1] || '') : null;
  if (target && (fs.existsSync(target) || !target.startsWith(root + path.sep))) throw new Error('Evidence must use a new workspace path');
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  try {
    const edge = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
    browser = await chromium.launch({ headless: true, ...(fs.existsSync(edge) ? { executablePath: edge } : {}) });
    async function check(name, fn) {
      const context = await browser.newContext();
      const page = await context.newPage();
      await page.goto('http://127.0.0.1:' + port);
      await page.evaluate(() => { window.fixtureToken = 'synthetic-browser-token'; });
      try { await fn(page); checks.push(name); console.log('PASS ' + name); }
      finally { await context.close(); }
    }
    const options = name => ({ baseUrl: '/case/' + name, resumeIds: [R], jobId: J, token: 'synthetic-browser-token' });
    await check('fragmented_utf8_crlf_multiline_frames', async page => {
      const got = await page.evaluate(async () => {
        const { readSSE } = await import('/transport.js'); const frames = [];
        for await (const event of readSSE(await fetch('/frames'))) frames.push(event);
        return frames;
      });
      assert.equal(got[0].id, '7'); assert.equal(JSON.parse(got[0].data).message, '中文');
    });
    await check('disconnect_resumes_get_and_deduplicates', async page => {
      const got = await page.evaluate(async opt => {
        const { runSqlAnalysis } = await import('/transport.js'); const stages = [];
        const result = await runSqlAnalysis({ ...opt, getToken: () => window.fixtureToken, onUnauthorized: () => {},
          onProgress: status => stages.push(status) });
        return { result, stages };
      }, options('reconnect'));
      assert.equal(got.result.data.hr_analysis.final_score, 82);
      assert.equal(got.stages.filter(value => value === 'planning').length, 1);
      const seen = requests.filter(req => req.name === 'reconnect');
      assert.deepEqual(seen.map(req => req.method), ['POST', 'GET']); assert.equal(seen[1].cursor, '1');
      assert(seen.every(req => req.auth === 'Bearer synthetic-browser-token'));
    });
    await check('ambiguous_submit_reuses_idempotency_key', async page => {
      await page.evaluate(async opt => { const { runSqlAnalysis } = await import('/transport.js');
        await runSqlAnalysis({ ...opt, getToken: () => window.fixtureToken, onUnauthorized: () => {} }); }, options('lost'));
      const seen = requests.filter(req => req.name === 'lost');
      assert.equal(seen.length, 2); assert.equal(seen[0].key, seen[1].key); assert.equal(seen[0].body, seen[1].body);
      assert(seen[0].key && seen[0].key.length === 36);
    });
    await check('manual_recovery_uses_saved_task_and_cursor', async page => {
      const checkpoint = await page.evaluate(async opt => {
        const { runSqlAnalysis, SqlAnalysisConnectionError } = await import('/transport.js'); let saved;
        try { await runSqlAnalysis({ ...opt, getToken: () => window.fixtureToken, onUnauthorized: () => {}, onCheckpoint: c => saved = c }); }
        catch (error) { if (!(error instanceof SqlAnalysisConnectionError)) throw error; }
        return saved;
      }, options('outage'));
      assert.equal(checkpoint.cursor, 1); states.get('outage').resumed = true;
      await page.evaluate(async ({ opt, checkpoint }) => { const { runSqlAnalysis } = await import('/transport.js');
        await runSqlAnalysis({ ...opt, checkpoint, getToken: () => window.fixtureToken, onUnauthorized: () => {} }); },
      { opt: options('outage'), checkpoint });
      assert.equal(states.get('outage').posts, 1);
      assert.equal(requests.filter(req => req.name === 'outage').at(-1).cursor, '1');
    });
    await check('malformed_frame_stops_without_retry', async page => {
      const message = await page.evaluate(async opt => { const { runSqlAnalysis } = await import('/transport.js');
        try { await runSqlAnalysis({ ...opt, getToken: () => window.fixtureToken, onUnauthorized: () => {} }); }
        catch (e) { return e.message; } }, options('bad'));
      assert.match(message, /解析/); assert.equal(states.get('bad').posts, 1);
    });
    await check('unauthorized_stops_and_expires_same_session', async page => {
      const count = await page.evaluate(async opt => { const { runSqlAnalysis } = await import('/transport.js'); let count = 0;
        try { await runSqlAnalysis({ ...opt, getToken: () => window.fixtureToken, onUnauthorized: () => count++ }); } catch {}
        return count; }, options('unauthorized'));
      assert.equal(count, 1); assert.equal(states.get('unauthorized').posts, 1);
    });
    await check('old_401_cannot_clear_new_account', async page => {
      const got = await page.evaluate(async opt => { const { runSqlAnalysis } = await import('/transport.js'); let expired = 0;
        setTimeout(() => window.fixtureToken = 'new-account-token', 10);
        try { await runSqlAnalysis({ ...opt, getToken: () => window.fixtureToken, onUnauthorized: () => expired++ }); }
        catch (error) { return { expired, name: error.name }; } }, options('changed'));
      assert.deepEqual(got, { expired: 0, name: 'AbortError' });
    });
    for (const name of ['cancel', 'backoff']) await check('abort_' + name, async page => {
      const result = await page.evaluate(async opt => { const { runSqlAnalysis } = await import('/transport.js'); const controller = new AbortController();
        setTimeout(() => controller.abort(), 100);
        try { await runSqlAnalysis({ ...opt, signal: controller.signal, getToken: () => window.fixtureToken, onUnauthorized: () => {} }); }
        catch (error) { return error.name; } }, options(name));
      assert.equal(result, 'AbortError'); assert.equal(states.get(name).posts, 1);
    });
    await check('partial_result_mapping_rejects_foreign_or_inconsistent_rows', async page => {
      const got = await page.evaluate(async ({ result, R, R2, J, T }) => {
        const { sqlResult } = await import('/transport.js');
        const partial = structuredClone(result); Object.assign(partial.data, { status: 'partial_success', total_candidates: 2, failed_candidates: 1 });
        const mapped = sqlResult(partial, [R, R2], J, T); let rejected = 0;
        try { sqlResult(result, [R2], J, T); } catch { rejected++; }
        const wrong = structuredClone(result); wrong.data.completed_candidates = 3;
        try { sqlResult(wrong, [R], J, T); } catch { rejected++; }
        return { mapped, rejected };
      }, { result, R, R2, J, T });
      assert.equal(got.mapped.data.batch_analyses.length, 1); assert.equal(got.mapped.data.batch_failures[0].resume_id, R2);
      assert.equal(got.rejected, 2);
    });
    await check('session_reset_cancels_upload_and_ignores_late_result', async page => {
      const got = await page.evaluate(async () => {
        let resolveUpload, uploads = 0, jobs = 0, signal;
        window.sessionAPI = { upload: (_file, s) => { uploads++; signal = s; return new Promise(r => resolveUpload = r); },
          job: () => jobs++, analyze: () => { throw Error('Unexpected analysis'); } };
        const session = await import('/session.js'); const completed = [];
        session.setCompletionHandler(value => completed.push(value));
        session.startAnalysisTask({ files: [{ name: 'synthetic.docx', size: 1, status: 'pending' }],
          fileObjects: [new File(['x'], 'synthetic.docx')], jobDescription: 'synthetic', webSearch: false });
        session.resetSession(); resolveUpload('id'); await new Promise(r => setTimeout(r, 10));
        return { state: session.useAnalysisSession(), uploads, jobs, aborted: signal.aborted, completed };
      });
      assert.equal(got.jobs, 0); assert.equal(got.aborted, true); assert.equal(got.state.running, false);
      assert.equal(got.state.result, null); assert.deepEqual(got.completed, [null]);
    });
    await check('session_recovery_does_not_upload_again', async page => {
      const got = await page.evaluate(async ({ R, J, T }) => {
        const { SqlAnalysisConnectionError } = await import('/transport.js'); let uploads = 0, jobs = 0, analyses = 0, resumed;
        const saved = { requestId: crypto.randomUUID(), taskId: T, cursor: 1 };
        window.sessionAPI = { upload: async () => { uploads++; return R; }, job: async () => { jobs++; return J; },
          analyze: async (...args) => { analyses++; if (analyses === 1) { args[5].onCheckpoint(saved); throw new SqlAnalysisConnectionError('synthetic disconnect'); }
            resumed = args[5].checkpoint; return { data: { resume_id: R, job_id: J, hr_analysis: { final_score: 82 } } }; } };
        const session = await import('/session.js');
        const input = { files: [{ name: 'synthetic.docx', size: 1, status: 'pending' }], fileObjects: [new File(['x'], 'synthetic.docx')], jobDescription: 'synthetic', webSearch: false };
        session.startAnalysisTask(input); await new Promise(r => setTimeout(r, 20));
        const recoverable = session.useAnalysisSession().recoverable;
        session.startAnalysisTask(input); await new Promise(r => setTimeout(r, 20));
        return { uploads, jobs, analyses, resumed, saved, recoverable, state: session.useAnalysisSession() };
      }, { R, J, T });
      assert(got.recoverable); assert.equal(got.uploads, 1); assert.equal(got.jobs, 1); assert.equal(got.analyses, 2);
      assert.deepEqual(got.resumed, got.saved); assert.equal(got.state.result.data.hr_analysis.final_score, 82);
    });
    await check('legacy_default_stays_on_old_protocol', async page => {
      const got = await page.evaluate(async ({ R, J }) => {
        const module = await import('/actual-screening.js'); return module.analyzeResumesStream([R], J, undefined);
      }, { R, J });
      assert.equal(got.data.hr_analysis.final_score, 82);
      const seen = requests.filter(req => req.name === 'legacy'); assert.equal(seen.length, 1); assert.equal(seen[0].key, undefined);
    });
    await check('improvement_markdown_mapping_and_stable_submit_key', async page => {
      const got = await page.evaluate(async opt => {
        const { runSqlAnalysis } = await import('/transport.js');
        return runSqlAnalysis({ ...opt, kind: 'improvement', getToken: () => window.fixtureToken, onUnauthorized: () => {} });
      }, options('improve-lost'));
      assert.equal(got.data.analysis_result, optimized); assert.equal(got.data.hr_analysis, undefined);
      const seen = requests.filter(req => req.name === 'improve-lost');
      assert.equal(seen.length, 2); assert.equal(seen[0].key, seen[1].key);
      assert.deepEqual(JSON.parse(seen[0].body), { resume_id: R, job_id: J });
    });
    await check('improvement_404_distinguishes_deleted_source_from_missing_task', async page => {
      for (const [name, message] of [
        ['improve-source-missing', /简历原文或岗位已不可用.*重新上传简历/],
        ['improve-task-missing', /原优化任务已无法恢复.*发起新任务/],
      ]) {
        const error = await page.evaluate(async ({ opt, resume, task }) => {
          const { runSqlAnalysis } = await import('/transport.js');
          try {
            await runSqlAnalysis({ ...opt, kind: 'improvement', getToken: () => window.fixtureToken, onUnauthorized: () => {},
              ...(resume ? { checkpoint: { requestId: crypto.randomUUID(), taskId: task, cursor: 0 } } : {}) });
          } catch (caught) { return caught.message; }
          return '';
        }, { opt: options(name), resume: name === 'improve-task-missing', task: I });
        assert.match(error, message);
        const seen = requests.filter(req => req.name === name);
        assert.equal(seen.length, 1);
        assert.equal(seen[0].method, name === 'improve-task-missing' ? 'GET' : 'POST');
      }
    });
    await check('actual_improvement_wrapper_recovers_without_new_post', async page => {
      const failed = await page.evaluate(async ({ R, J }) => {
        const api = await import('/actual-sql-screening.js');
        try { await api.improveResumeStream(R, J); return false; }
        catch (error) { return error instanceof api.SqlAnalysisConnectionError; }
      }, { R, J });
      assert(failed); states.get('sql-api').resumed = true;
      const got = await page.evaluate(async ({ R, J }) => (await import('/actual-sql-screening.js')).improveResumeStream(R, J), { R, J });
      assert.equal(got.data.analysis_result, optimized);
      assert.equal(requests.filter(req => req.name === 'sql-api' && req.method === 'POST').length, 1);
    });
    await check('screening_search_choice_is_in_submit_body', async page => {
      await page.evaluate(async opt => {
        const { runSqlAnalysis } = await import('/transport.js');
        await runSqlAnalysis({ ...opt, webSearch: true, getToken: () => window.fixtureToken, onUnauthorized: () => {} });
      }, options('search-on'));
      const seen = requests.find(req => req.name === 'search-on');
      assert.equal(JSON.parse(seen.body).agent_config.web_search, true);
    });
    if (process.argv.includes('--ui')) {
      const net = require('node:net');
      const probe = net.createServer();
      await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
      uiPort = probe.address().port;
      await new Promise(resolve => probe.close(resolve));
      const log = fs.openSync(path.join(root, '.test-tmp/frontend-sql-ui.log'), 'w');
      next = cp.spawn(process.execPath, [path.join(frontend, 'node_modules/next/dist/bin/next'), 'dev', '--hostname', '127.0.0.1', '--port', String(uiPort)],
        { cwd: frontend, windowsHide: true, stdio: ['ignore', log, log], env: { ...process.env,
          NEXT_TELEMETRY_DISABLED: '1', NEXT_PUBLIC_API_URL: '', NEXT_PUBLIC_ANALYSIS_BACKEND: 'sql',
          BACKEND_INTERNAL_URL: 'http://127.0.0.1:' + port + '/case/ui' } });
      fs.closeSync(log);
      const base = 'http://127.0.0.1:' + uiPort;
      const deadline = Date.now() + 90000;
      while (true) {
        if (next.exitCode !== null) throw new Error('Isolated Next server failed; inspect local synthetic UI log');
        try { const response = await fetch(base, { signal: AbortSignal.timeout(3000) });
          await response.body?.cancel(); if (response.ok) break; } catch {}
        if (Date.now() >= deadline) throw new Error('Isolated Next server startup timed out');
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      const context = await browser.newContext();
      await context.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
      await context.addInitScript(({ R }) => {
        localStorage.setItem('resume-screening-token', 'synthetic-browser-token');
        localStorage.setItem('resume-screening-user', JSON.stringify({ user_id: R, username: 'synthetic-browser' }));
      }, { R });
      const page = await context.newPage(), errors = [];
      page.on('pageerror', error => errors.push(error.message));
      try {
        await page.goto(base);
        await page.getByRole('checkbox').first().waitFor();
        assert(await page.getByRole('checkbox').first().isEnabled());
        await page.getByRole('checkbox').first().check();
        await page.locator('input[type=file]').setInputFiles({ name: 'synthetic.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('synthetic') });
        await page.locator('#job-description').fill('Synthetic Python engineer with API and project experience required.');
        await page.getByRole('button', { name: '开始分析', exact: true }).click();
        const recover = page.getByRole('button', { name: '恢复分析进度', exact: true });
        await recover.waitFor({ timeout: 25000 });
        states.get('ui').resumed = true;
        await recover.click();
        await page.waitForURL('**/dashboard', { timeout: 30000 });
        await page.getByText('合成候选人', { exact: true }).first().waitFor();
        assert(await page.getByRole('button', { name: '深度优化简历', exact: true }).isEnabled());
        await page.getByRole('button', { name: '深度优化简历', exact: true }).click();
        await page.getByRole('heading', { name: '深度优化简历', exact: true }).waitFor();
        assert.deepEqual(errors, []);
        const seen = requests.filter(req => req.name === 'ui');
        assert.equal(seen.filter(req => req.path === 'api/v1/resumes/upload').length, 1);
        assert.equal(seen.filter(req => req.path === 'api/v1/jobs/upload').length, 1);
        assert.equal(seen.filter(req => req.path === 'api/v1/resumes/hr-analysis').length, 1);
        const saved = await page.evaluate(() => JSON.parse(sessionStorage.getItem('resume-screening-result')));
        assert.equal(saved.data.task_id, T); assert.equal(saved.data.hr_analysis.final_score, 82);
        assert.equal(saved.data.optimized_markdown, optimized);
        assert.equal(saved.data.improvement_task_id, I);
        assert.equal(seen.filter(req => req.path === 'api/v1/resumes/improve').length, 1);
        assert.equal(JSON.parse(seen.find(req => req.path === 'api/v1/resumes/hr-analysis').body).agent_config.web_search, true);
        checks.push('actual_workbench_outage_recovery_and_report_navigation');
        console.log('PASS actual_workbench_outage_recovery_and_report_navigation');
      } catch (error) {
        fs.writeFileSync(path.join(root, '.test-tmp/sql-ui-failure.json'), JSON.stringify({ url: page.url(), errors,
          text: (await page.locator('body').innerText()).slice(0, 2200),
          requests: requests.filter(request => request.name === 'ui').map(request => ({ method: request.method, path: request.path })) }, null, 2));
        await page.screenshot({ path: path.join(root, '.test-tmp/sql-ui-failure.png') });
        throw error;
      } finally { await context.close(); }
    }
    assert.deepEqual(hashes(), before, 'Source changed during browser acceptance');
    assert.equal(git('rev-parse', 'HEAD'), commit);
    const dirty = git('status', '--porcelain=v1', '--', ...files);
    const evidence = { scope: 'isolated-real-browser-synthetic-http-and-session-state', passed: true,
      tests: { run: checks.length, failures: 0 }, checks, browser: 'Chromium / isolated context',
      code_commit: commit, code_tree: tree, source_sha256: before, source_unchanged: true, source_clean: !dirty,
      workbench_ui_tested: process.argv.includes('--ui'), production_acceptance: false, real_vendor_calls: false };
    if (target) fs.writeFileSync(target, JSON.stringify(evidence, null, 2) + '\n', { flag: 'wx' });
    console.log(JSON.stringify({ passed: true, run: checks.length, source_clean: !dirty }));
  } finally {
    if (next && next.exitCode === null) {
      if (process.platform === 'win32') cp.spawnSync('taskkill.exe', ['/PID', String(next.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      else next.kill('SIGTERM');
      await new Promise(resolve => { if (next.exitCode !== null) resolve(); else { next.once('exit', resolve); setTimeout(resolve, 5000); } });
    }
    if (browser) await browser.close();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
