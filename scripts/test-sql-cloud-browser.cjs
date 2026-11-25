/* Read-only cloud browser acceptance: replay one already completed synthetic task. */
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const net = require('node:net');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const frontend = path.join(root, 'apps/frontend');
const { chromium } = require(path.join(frontend, 'node_modules/playwright'));
const ts = require(path.join(frontend, 'node_modules/typescript'));
const commit = '9201f3c96d506b4a317bd6b5ae55c28842eea390';
const sshTarget = process.env.RESUME_QA_SSH_TARGET?.trim();
const privateDir = process.env.SQL_PREVIEW_PRIVATE_DIR?.trim();
if (!sshTarget || !privateDir) throw new Error('Set RESUME_QA_SSH_TARGET and SQL_PREVIEW_PRIVATE_DIR');
const ssh = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '--', sshTarget];
let phase = 'private-fixture', tunnel, browser;
(async () => {
  const target = process.argv[2];
  if (!target || fs.existsSync(target)) throw new Error('Use a new evidence path');
  const encodedPrivateDir = Buffer.from(privateDir, 'utf8').toString('base64');
  const remote = `import json,pathlib;p=pathlib.Path(__import__('base64').b64decode('${encodedPrivateDir}').decode('utf-8'));print(json.dumps({'owner':json.loads((p/'preview-owner.json').read_text()),'request':json.loads((p/'real-analysis-request.json').read_text())}))`;
  const fetched = cp.spawnSync('ssh', [...ssh, 'sudo', '-n', 'python3', '-'],
    { input: remote, encoding: 'utf8', windowsHide: true, timeout: 20000 });
  if (fetched.status !== 0) throw new Error('Private synthetic fixture unavailable');
  const fixture = JSON.parse(fetched.stdout);
  const source = cp.execFileSync('git', ['show', commit + ':apps/frontend/lib/api/sql-analysis-stream.ts'], { cwd: root, encoding: 'utf8' });
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 } }).outputText;
  const probe = net.createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  tunnel = cp.spawn('ssh', ['-N', '-o', 'ExitOnForwardFailure=yes', '-L', `127.0.0.1:${port}:127.0.0.1:13010`, ...ssh],
    { windowsHide: true, stdio: 'ignore' });
  const base = `http://127.0.0.1:${port}`;
  phase = 'frontend-tunnel';
  for (let attempt = 0; ; attempt++) {
    if (tunnel.exitCode !== null || attempt > 40) throw new Error('Frontend tunnel unavailable');
    try { const response = await fetch(base + '/login', { signal: AbortSignal.timeout(2000) });
      await response.body?.cancel(); if (response.ok) break; } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
  const context = await browser.newContext();
  await context.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
  await context.route('**/__sql_preview_transport.js', route => route.fulfill({ contentType: 'text/javascript', body: compiled }));
  const page = await context.newPage(), errors = [];
  let analysisPosts = 0;
  page.on('pageerror', error => errors.push(error.name));
  page.on('request', request => { if (request.method() === 'POST' && request.url().includes('/resumes/hr-analysis')) analysisPosts++; });
  phase = 'login';
  await page.goto(base + '/login');
  await page.locator('#login-username').fill(fixture.owner.username);
  await page.locator('#login-password').fill(fixture.owner.password);
  await page.locator('button[type=submit]').first().click();
  await page.waitForURL(base + '/', { timeout: 30000 });
  assert(await page.getByRole('checkbox').first().isDisabled());
  phase = 'persisted-task-replay';
  const replay = await page.evaluate(async saved => {
    const module = await import('/__sql_preview_transport.js');
    const getToken = () => localStorage.getItem('resume-screening-token');
    const token = getToken();
    const result = await module.runSqlAnalysis({ baseUrl: '', resumeIds: [saved.resume_id], jobId: saved.job_id,
      token, getToken, onUnauthorized: () => {}, checkpoint: { requestId: saved.request_id, taskId: saved.task_id, cursor: 0 } });
    sessionStorage.setItem('resume-screening-result', JSON.stringify(result));
    return { score: result.data.hr_analysis.final_score, task: result.data.task_id,
      binding: !!result.data.model_binding, candidate: result.data.hr_analysis.candidate_name };
  }, fixture.request);
  assert.equal(replay.task, fixture.request.task_id);
  assert.equal(replay.binding, true);
  assert.equal(typeof replay.score, 'number');
  phase = 'report-render';
  await page.goto(base + '/dashboard');
  await page.getByRole('heading', { name: '候选人分析报告', exact: true }).waitFor();
  if (replay.candidate) await page.getByText(replay.candidate, { exact: true }).first().waitFor();
  assert(await page.getByRole('button', { name: '深度优化暂未开放', exact: true }).isDisabled());
  assert.equal(analysisPosts, 0);
  assert.deepEqual(errors, []);
  const evidence = { passed: true, server: 'configured QA target', code_commit: commit,
    scope: 'cloud-login-persisted-synthetic-task-sse-replay-and-report-rendering',
    real_cloud_http_and_postgres: true, real_model_report_replayed: true, new_inference_submissions: analysisPosts,
    replay_result_seeded_in_session_storage: true, fresh_upload_submission_ui_tested: false,
    report_score_valid: true, model_binding_present: true, page_errors: errors.length,
    helper_sha256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex'),
    transport_sha256: crypto.createHash('sha256').update(source).digest('hex'), production_cutover: false };
  fs.writeFileSync(target, JSON.stringify(evidence, null, 2) + '\n', { flag: 'wx' });
  console.log(JSON.stringify(evidence));
})().catch(error => { console.error(JSON.stringify({ passed: false, phase, error_type: error.name })); process.exitCode = 1; })
  .finally(async () => {
    if (browser) await browser.close();
    if (tunnel && tunnel.exitCode === null) {
      tunnel.kill();
      await new Promise(resolve => { tunnel.once('exit', resolve); setTimeout(resolve, 3000); });
    }
  });
