#!/usr/bin/env node
/**
 * SQL 组合：真实 Next.js（SQL 模式）rewrite → 真实 SQL 预览 Flask 应用。
 *
 * 侧重验证复核报告里的 P1：Next 的 changeOrigin 把上游 Host 改成后端地址后，
 * 浏览器写请求不能因此被误拦；同时外部 Origin 仍必须被拒绝。
 *
 * 说明：本机未运行 Docker，无法起临时 PostgreSQL，因此 SQL 应用使用与单测相同的
 * 注入式服务桩（auth/store），只验证 HTTP + 代理 + 跨站判定链路本身。数据库层面的
 * 任务提交与 SSE 由既有 SQL 套件覆盖，并需在云端预运行验收时重跑。
 */
const { chromium } = require('../apps/frontend/node_modules/playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const net = require('net');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const BACKEND_SRC = path.join(ROOT, 'apps', 'backend');
const FRONTEND_DIR = path.join(ROOT, 'apps', 'frontend');
const RUN_ID = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
const WORK = path.join(ROOT, '.test-tmp', `proxy-integration-sql-${RUN_ID}`);

const results = { run_id: RUN_ID, combo: 'sql-frontend/sql-backend', checks: [], failures: [] };
function record(name, ok, detail) {
  results.checks.push({ name, ok, detail });
  if (!ok) results.failures.push({ name, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail === undefined ? '' : ' :: ' + JSON.stringify(detail)}`);
}
function pickPort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => { const p = srv.address().port; srv.close(() => resolve(p)); });
  });
}
async function waitFor(url, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try { const r = await fetch(url, { redirect: 'manual' }); if (r.status < 500) return true; } catch {}
    await new Promise(r => setTimeout(r, 500));
  }
  throw new Error(`timeout waiting for ${label || url}`);
}

const LAUNCHER = String.raw`
import json, sys
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

port = int(sys.argv[1])
origins = [item for item in sys.argv[2].split(',') if item]

from db_auth import InternalUser
from db_http_preview import create_preview_app

user_id = uuid4()
stamp = datetime.now(timezone.utc)
user = InternalUser(user_id=user_id, username='previewprobe', email='previewprobe@example.invalid',
                    password_hash='x', password_salt='y', password_version=1,
                    is_admin=False, is_super_admin=False, status='active',
                    deleted_at=None, created_at=stamp, updated_at=stamp)

auth = Mock()
auth.authenticate_user.return_value = {'user_id': str(user_id), 'password_version': 1}
auth.find_user_by_id.return_value = user
store = Mock()
store.get_resume_view.return_value = None
store.get_job_view.return_value = None

app = create_preview_app(store, auth, 'sql-proxy-integration-jwt-secret-48-bytes-minimum',
                         trusted_origins=tuple(origins))

log_path = sys.argv[3] + '/requests.jsonl'
@app.after_request
def _record(response):
    from flask import request
    try:
        with open(log_path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'method': request.method, 'path': request.path,
                'origin': request.headers.get('Origin'), 'host': request.headers.get('Host'),
                'xfh': request.headers.get('X-Forwarded-Host'),
                'status': response.status_code}) + '\n')
    except Exception:
        pass
    return response

print(json.dumps({'ready': True}), flush=True)
app.run(host='127.0.0.1', port=port, threaded=True, use_reloader=False)
`;

function spawnBackend(port, origins, tag) {
  const child = spawn(process.env.PYTHON || 'python', [path.join(WORK, 'launcher.py'), String(port), origins, WORK], {
    cwd: path.join(WORK, 'backend'),
    env: { ...process.env, PYTHONPATH: path.join(WORK, 'backend'), PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', d => fs.appendFileSync(path.join(WORK, `${tag}.out.log`), d));
  child.stderr.on('data', d => fs.appendFileSync(path.join(WORK, `${tag}.err.log`), d));
  return child;
}

function spawnFrontend(port, backendUrl) {
  const child = spawn(process.execPath, [path.join(FRONTEND_DIR, 'node_modules', 'next', 'dist', 'bin', 'next'), 'dev', '-p', String(port), '-H', '127.0.0.1'], {
    cwd: FRONTEND_DIR,
    env: { ...process.env, BACKEND_INTERNAL_URL: backendUrl, NEXT_PUBLIC_API_URL: '',
           NEXT_PUBLIC_ANALYSIS_BACKEND: 'sql', NEXT_TELEMETRY_DISABLED: '1', NODE_ENV: 'development' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', d => fs.appendFileSync(path.join(WORK, 'next.out.log'), d));
  child.stderr.on('data', d => fs.appendFileSync(path.join(WORK, 'next.err.log'), d));
  return child;
}

async function stop(child) {
  if (!child || child.killed) return;
  child.kill('SIGTERM');
  await new Promise(resolve => {
    const timer = setTimeout(() => { try { child.kill('SIGKILL'); } catch {} resolve(); }, 5000);
    child.once('exit', () => { clearTimeout(timer); resolve(); });
  });
}

async function main() {
  fs.mkdirSync(WORK, { recursive: true });
  const backendDir = path.join(WORK, 'backend');
  const skip = new Set(['data', 'logs', '__pycache__', '.env']);
  fs.cpSync(BACKEND_SRC, backendDir, {
    recursive: true,
    filter: src => {
      const rel = path.relative(BACKEND_SRC, src);
      if (!rel) return true;
      return !rel.split(path.sep).some(part => skip.has(part));
    },
  });
  fs.writeFileSync(path.join(WORK, 'launcher.py'), LAUNCHER, 'utf8');

  const frontPort = await pickPort();
  const backPort = await pickPort();
  const trusted = `http://127.0.0.1:${frontPort}`;
  results.ports = { front: frontPort, back: backPort };
  results.trusted_origin = trusted;

  const backend = spawnBackend(backPort, trusted, 'backend');
  let nextProc, browser;
  try {
    await waitFor(`http://127.0.0.1:${backPort}/ping`, 60000, 'sql backend');
    record('sql_backend_ready', true, { port: backPort });

    nextProc = spawnFrontend(frontPort, `http://127.0.0.1:${backPort}`);
    await waitFor(`http://127.0.0.1:${frontPort}/login`, 180000, 'next dev (sql)');
    record('sql_next_ready', true, { port: frontPort });

    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const context = await browser.newContext({ locale: 'zh-CN' });
    const page = await context.newPage();
    const pageErrors = [];
    page.on('pageerror', e => pageErrors.push(String(e).slice(0, 200)));
    await page.goto(`http://127.0.0.1:${frontPort}/login`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
    record('sql_login_page_rendered', page.url().includes('/login'), { title: await page.title() });

    const flow = await page.evaluate(async () => {
      const r = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'previewprobe', password: 'SyntheticPass123' }),
      });
      const body = await r.json().catch(() => ({}));
      return { status: r.status, token: Boolean(body?.data?.token), detail: body?.detail || null };
    });
    results.sql_flow = flow;
    record('sql_same_origin_login_through_proxy', flow.status === 200 && flow.token, flow);
    record('sql_no_page_errors', pageErrors.length === 0, pageErrors);

    const back = `http://127.0.0.1:${backPort}`;
    const payload = JSON.stringify({ username: 'previewprobe', password: 'SyntheticPass123' });
    const post = async (origin) => {
      const headers = { 'Content-Type': 'application/json' };
      if (origin !== null) headers.Origin = origin;
      const r = await fetch(`${back}/api/v1/auth/login`, { method: 'POST', headers, body: payload });
      return r.status;
    };
    record('sql_foreign_origin_rejected', await post('https://evil.example') === 403);
    record('sql_lookalike_rejected', await post(`http://127.0.0.1:${frontPort}.evil.example`) === 403);
    record('sql_wrong_port_rejected', await post(`http://127.0.0.1:${frontPort + 1}`) === 403);
    record('sql_null_origin_rejected', await post('null') === 403);
    record('sql_trusted_origin_accepted', await post(trusted) === 200);

    const logPath = path.join(WORK, 'requests.jsonl');
    const rows = fs.existsSync(logPath)
      ? fs.readFileSync(logPath, 'utf8').trim().split('\n').filter(Boolean).map(l => JSON.parse(l))
      : [];
    const hit = rows.find(r => r.path === '/api/v1/auth/login' && r.origin === trusted && r.status === 200);
    results.proxy_evidence = hit || null;
    record('sql_upstream_host_rewritten',
      Boolean(hit) && hit.host === `127.0.0.1:${backPort}` && hit.origin === trusted,
      hit ? { host: hit.host, origin: hit.origin, xfh: hit.xfh } : 'no matching request');

    results.passed = results.failures.length === 0;
  } catch (error) {
    record('harness_error', false, String((error && error.message) || error));
    results.passed = false;
  } finally {
    if (browser) await browser.close().catch(() => {});
    await stop(nextProc);
    await stop(backend);
    fs.writeFileSync(path.join(WORK, 'result.json'), JSON.stringify(results, null, 2), 'utf8');
    console.log('\nRESULT ' + JSON.stringify({ passed: results.passed, failures: results.failures.length, work: WORK }));
    process.exitCode = results.passed ? 0 : 1;
  }
}

main().catch(error => { console.error(error); process.exitCode = 2; });
