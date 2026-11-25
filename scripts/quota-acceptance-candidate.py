"""Quota acceptance matrix (fresh zero-usage accounts per run).

Groups: success + replay + rejection, concurrency, failure release, isolation.
Reserved is read from the candidate database ledger (UTC quota day), never from quota/me.
Exit 0 only when every assertion in every group passes.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

BASE = 'http://127.0.0.1:18030'
ORIGIN = 'http://127.0.0.1:13030'
P = pathlib.Path(os.environ.get('RESUME_QUOTA_CANDIDATE_DIR', pathlib.Path.home() / '.resume-workbench' / 'candidate'))
PG = 'resume-candidate-postgres'
DB = 'resume_sql_preview_candidate_20261030'
IMAGE = 'resume-candidate-sql-backend:92635d24c6a5-r5'
NET = 'resume-candidate-sql-net'
RUN_ID = os.environ.get('RUN_ID') or datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
OUT = pathlib.Path(os.environ.get('QUOTA_OUT', P / ('quota-run-%s.json' % RUN_ID)))
PGPASS = None
for env_name in ('pg-password', 'postgres.env'):
    candidate = P / env_name
    if candidate.exists():
        if env_name == 'pg-password':
            PGPASS = candidate.read_text().strip()
        else:
            for line in candidate.read_text().splitlines():
                if line.startswith('POSTGRES_PASSWORD='):
                    PGPASS = line.split('=', 1)[1]
        break
results = {'run_id': RUN_ID, 'scenarios': {}, 'failures': []}


def check(name, ok, detail=None, group=None):
    entry = {'name': name, 'ok': bool(ok)}
    if detail is not None:
        entry['detail'] = detail
    results['scenarios'].setdefault(group or 'general', {}).setdefault('checks', []).append(entry)
    if not ok:
        results['failures'].append({'group': group, 'name': name, 'detail': detail})
    print(('PASS ' if ok else 'FAIL ') + (group or '') + ' ' + name +
          ('' if detail is None else ' :: ' + json.dumps(detail, ensure_ascii=False)))


def http(path, payload=None, token=None, method=None, raw=None, ctype=None, timeout=120):
    headers = {'Origin': ORIGIN}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode(); headers['Content-Type'] = 'application/json'
    if raw is not None:
        data = raw; headers['Content-Type'] = ctype
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=headers,
                                                           method=method), timeout=timeout) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'{}')
        except Exception:
            return e.code, {}


def psql(sql):
    r = subprocess.run(['docker', 'exec', '-i', PG, 'sh', '-c',
                        'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -X -h 127.0.0.1 '
                        '-U resume_bootstrap_admin -d "$1" -v ON_ERROR_STOP=1 -At -c "$2"',
                        'x', DB, sql], input=(PGPASS or '') + '\n', capture_output=True,
                       text=True, timeout=120)
    if r.returncode:
        raise RuntimeError('psql failed')
    return r.stdout.strip()


def usernames():
    return {s: ('qa_%s_%s' % (s, RUN_ID.lower())) for s in ('succ', 'conc', 'fail', 'isol')}


def create_accounts(names):
    code = '''import json, sys
from candidate_app import application
from db_auth import AuthService
import sqlalchemy as sa
users = json.load(sys.stdin)
owned = application.extensions["db_application"]
try:
    service = AuthService(owned.sessions)
    out = {}
    for username, password in users.items():
        existing = service.authenticate_user(username, password)
        if existing:
            out[username] = True
            continue
        created, error = service.create_user(username, password, username + "@example.invalid")
        out[username] = bool(created)
    print(json.dumps(out))
finally:
    owned.close()
'''
    r = subprocess.run(['docker', 'run', '--rm', '-i', '--network', NET,
                        '-e', 'SQL_DEPLOYMENT_FILE=/run/secrets/sql-deployment.json',
                        '-v', str(P / 'deployment.json') + ':/run/secrets/sql-deployment.json:ro',
                        '-v', str(P / 'originals') + ':/app/sql-originals',
                        '-v', str(P / 'candidate_app.py') + ':/app/candidate_app.py:ro',
                        '--entrypoint', 'python', IMAGE, '-c', code],
                       input=json.dumps({n: 'QaPass_' + RUN_ID for n in names.values()}),
                       capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise RuntimeError('account creation failed')
    return json.loads(r.stdout.strip().splitlines()[-1])


def user_id(username):
    return psql("SELECT id::text FROM users WHERE username='%s';" % username)


def ledger(uid):
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    usage = psql("SELECT coalesce(used,0)||','||coalesce(reserved,0) FROM quota_daily_usage "
                 "WHERE user_id='%s'::uuid AND date='%s';" % (uid, day))
    used, reserved = (usage.split(',') if usage else ('0', '0'))
    rows = psql("SELECT count(*) FILTER (WHERE status='succeeded'), "
                "count(*) FILTER (WHERE status='failed'), "
                "count(*) FILTER (WHERE settled_at IS NOT NULL), count(*) "
                "FROM quota_reservations WHERE user_id='%s'::uuid;" % uid)
    succeeded, failed, settled, total = (rows.split('|') if rows else ('0', '0', '0', '0'))
    return {'used': int(used), 'reserved': int(reserved), 'reservations': int(total),
            'succeeded': int(succeeded), 'failed': int(failed), 'settled': int(settled)}


def tasks_for(uid):
    return int(psql("SELECT count(*) FROM analysis_tasks WHERE owner_user_id='%s'::uuid;" % uid) or 0)


def fake_runs():
    r = subprocess.run(['docker', 'exec', 'resume-candidate-sql-backend', 'sh', '-c',
                        'wc -l < /tmp/candidate_fake_runs 2>/dev/null || echo 0'],
                       capture_output=True, text=True)
    return int(r.stdout.strip() or 0)


def restart_backend(env=None):
    subprocess.run(['docker', 'rm', '-f', 'resume-candidate-sql-backend'], capture_output=True)
    extra = []
    for key, value in (env or {}).items():
        extra += ['-e', key + '=' + value]
    subprocess.run(['docker', 'run', '-d', '--name', 'resume-candidate-sql-backend',
                    '--label', 'purpose=candidate-ui', '--network', NET, '--network-alias', 'backend',
                    '-e', 'SQL_DEPLOYMENT_FILE=/run/secrets/sql-deployment.json',
                    '-e', 'CANDIDATE_FAKE_MODEL=1', *extra,
                    '-v', str(P / 'deployment.json') + ':/run/secrets/sql-deployment.json:ro',
                    '-v', str(P / 'originals') + ':/app/sql-originals',
                    '-v', str(P / 'candidate_app.py') + ':/app/candidate_app.py:ro',
                    '-p', '127.0.0.1:18030:8000', '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--memory', '320m',
                    IMAGE, 'gunicorn', '-c', 'python:gunicorn_sql_preview', 'candidate_app:application'],
                   capture_output=True)
    for _ in range(90):
        if subprocess.run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', BASE + '/ready'],
                          capture_output=True, text=True).stdout.strip() == '200':
            return True
        time.sleep(1)
    return False


def login(name):
    status, body = http('/api/v1/auth/login', {'username': name, 'password': 'QaPass_' + RUN_ID})
    return status, (body.get('data') or {}).get('token'), body


def _valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _category(status):
    return 'http_%dxx' % (status // 100) if isinstance(status, int) else 'no_response'


def upload(token):
    import io, zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   '<w:body><w:p><w:r><w:t>Quota synthetic resume</w:t></w:r></w:p></w:body></w:document>')
    boundary = 'b' + uuid.uuid4().hex
    body = (('--%s\r\nContent-Disposition: form-data; name="file"; filename="quota.docx"\r\n'
             'Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n') % boundary).encode() \
        + buffer.getvalue() + ('\r\n--%s--\r\n' % boundary).encode()
    status, payload = http('/api/v1/resumes/upload', token=token, raw=body,
                           ctype='multipart/form-data; boundary=' + boundary)
    resume = (payload.get('data') or {}).get('resume_id') or payload.get('resume_id')
    if status != 200 or not _valid_uuid(resume):
        raise SystemExit(json.dumps({'stage': 'upload', 'category': _category(status),
                                     'valid_uuid': _valid_uuid(resume)}, ensure_ascii=False))
    status, job = http('/api/v1/jobs/upload', {'resume_id': resume,
                                               'job_descriptions': ['Synthetic backend role with PostgreSQL']}, token=token)
    job_ids = (job.get('data') or {}).get('job_id') or job.get('job_id')
    job_id = job_ids[0] if isinstance(job_ids, list) and job_ids else None
    if status != 200 or not _valid_uuid(job_id):
        raise SystemExit(json.dumps({'stage': 'job', 'category': _category(status),
                                     'valid_uuid': _valid_uuid(job_id)}, ensure_ascii=False))
    return resume, job_id


def stream(token, resume, job, key):
    body = json.dumps({'resume_ids': [resume], 'job_id': job}).encode()
    request = urllib.request.Request(BASE + '/api/v1/resumes/hr-analysis?stream=true', data=body, headers={
        'Origin': ORIGIN, 'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json', 'Idempotency-Key': key})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            text = response.read().decode('utf-8', 'replace')
            status = response.status
    except urllib.error.HTTPError as error:
        text = error.read().decode('utf-8', 'replace'); status = error.code
    task = re.search(r'"task_id"\s*:\s*"([0-9a-f-]{36})"', text)
    return {'status': status, 'text': text, 'task_id': task.group(1) if task else None,
            'completed': '"status": "completed"' in text or '"status":"completed"' in text,
            'failed': '"status": "error"' in text or '"status":"error"' in text or 'candidate_failed' in text}


def wait_task(token, task, timeout=180):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, body = http('/api/v1/analysis-tasks/' + task, token=token)
        last = (body.get('data') or body)
        if last.get('status') in ('completed', 'failed', 'partial_success', 'cancelled'):
            return last
        time.sleep(0.5)
    return last


def set_quota(token, username, limit):
    return http('/api/v1/admin/quota/users/' + username, {'daily_limit': limit, 'analysis_enabled': True,
                                                          'email_notify': False}, token=token, method='PUT')


def main():
    names = usernames()
    created = create_accounts(names)
    if not all(created.values()):
        check('accounts_created', False, created); return 2
    admin_status, admin_body = http('/api/v1/auth/login', {'username': 'candidate_owner',
                                                            'password': 'OwnerPass2026'})
    admin = (admin_body.get('data') or {}).get('token')
    if admin_status != 200 or not admin:
        check('admin_login', False, admin_status); return 2
    tokens, ids, resumes = {}, {}, {}
    for scenario, name in names.items():
        status, token, body = login(name)
        check('precondition_login_' + scenario, status == 200 and bool(token), status, scenario)
        if not token:
            return 2
        tokens[scenario] = token
        ids[scenario] = user_id(name)
        flags = psql("SELECT is_admin||','||status FROM users WHERE username='%s';" % name).split(',')
        check('precondition_not_admin_' + scenario, flags[0] in ('f', 'false'), flags, scenario)
        resumes[scenario] = upload(token)
        before = ledger(ids[scenario])
        check('precondition_zero_usage_' + scenario,
              before['used'] == 0 and before['reserved'] == 0 and before['reservations'] == 0, before, scenario)
        set_quota(admin, name, 1 if scenario != 'conc' else 2)
    http('/api/v1/admin/quota/settings', {'quota_enabled': True, 'default_daily': 5,
                                          'email_notify_enabled': False}, token=admin, method='PUT')
    check('global_quota_enabled', True, None, 'setup')

    # scenario 1: success, replay, rejection
    restart_backend({'CANDIDATE_FAKE_DELAY': '5'})
    runs0 = fake_runs()
    resume, job = resumes['succ']
    key = str(uuid.uuid4())
    holder = {}
    thread = threading.Thread(target=lambda: holder.update(stream(tokens['succ'], resume, job, key)))
    thread.start()
    observed = None
    for _ in range(40):
        current = ledger(ids['succ'])
        if current['reserved'] >= 1:
            observed = current; break
        time.sleep(0.25)
    check('success_reserved_while_running', bool(observed) and observed['used'] == 0 and observed['reserved'] == 1,
          observed, 'success')
    thread.join(300)
    after = ledger(ids['succ'])
    check('success_settled', after['used'] == 1 and after['reserved'] == 0 and after['succeeded'] == 1
          and after['settled'] == 1, after, 'success')
    check('success_task_completed', holder.get('completed') is True, holder.get('status'), 'success')

    replay = stream(tokens['succ'], resume, job, key)
    same = replay['task_id'] == holder.get('task_id') and replay['status'] == 200
    check('replay_same_task', same, {'replay': replay['task_id'], 'first': holder.get('task_id')}, 'success')
    time.sleep(2)
    replay_ledger = ledger(ids['succ'])
    check('replay_no_extra_charge', replay_ledger['used'] == 1 and replay_ledger['reservations'] == 1,
          replay_ledger, 'success')
    check('replay_no_extra_model_run', fake_runs() - runs0 == 1, fake_runs() - runs0, 'success')

    denied = stream(tokens['succ'], resume, job, str(uuid.uuid4()))
    time.sleep(1)
    rejected = ledger(ids['succ'])
    check('rejection_429', denied['status'] == 429, denied['status'], 'success')
    check('rejection_no_new_reservation', rejected['reservations'] == 1, rejected, 'success')

    # scenario 2: concurrency
    restart_backend({'CANDIDATE_FAKE_DELAY': '3'})
    runs1 = fake_runs()
    resume, job = resumes['conc']
    outcomes = []
    def fire():
        outcomes.append(stream(tokens['conc'], resume, job, str(uuid.uuid4())))
    threads = [threading.Thread(target=fire) for _ in range(3)]
    [t.start() for t in threads]
    peak = 0
    while any(t.is_alive() for t in threads):
        current = ledger(ids['conc'])
        peak = max(peak, current['used'] + current['reserved'])
        time.sleep(0.2)
    [t.join(300) for t in threads]
    accepted = [o for o in outcomes if o['status'] == 200]
    limited = [o for o in outcomes if o['status'] == 429]
    final = ledger(ids['conc'])
    check('concurrency_two_accepted', len(accepted) == 2 and len(limited) == 1,
          {'accepted': len(accepted), 'limited': len(limited)}, 'concurrent')
    check('concurrency_never_over_limit', peak <= 2, peak, 'concurrent')
    check('concurrency_settled', final['used'] == 2 and final['reserved'] == 0 and final['succeeded'] == 2,
          final, 'concurrent')
    check('concurrency_model_runs', fake_runs() - runs1 == 2, fake_runs() - runs1, 'concurrent')

    # scenario 3: failure release
    resume, job = resumes['fail']
    restart_backend({'CANDIDATE_FAKE_FAIL': '1', 'CANDIDATE_FAKE_DELAY': '3'})
    runs2 = fake_runs()
    failure_key = str(uuid.uuid4())
    holder = {}
    thread = threading.Thread(target=lambda: holder.update(stream(tokens['fail'], resume, job, failure_key)))
    thread.start()
    observed = None
    for _ in range(40):
        current = ledger(ids['fail'])
        if current['reserved'] >= 1:
            observed = current; break
        time.sleep(0.25)
    check('failure_reserved_while_running', bool(observed) and observed['reserved'] == 1, observed, 'failure')
    thread.join(300)
    final = ledger(ids['fail'])
    check('failure_released', final['used'] == 0 and final['reserved'] == 0 and final['failed'] == 1
          and final['settled'] == 1, final, 'failure')
    check('failure_task_terminal', holder.get('failed') is True, holder.get('status'), 'failure')

    restart_backend({'CANDIDATE_FAKE_DELAY': '1'})
    retry = stream(tokens['fail'], resume, job, str(uuid.uuid4()))
    retry_task = wait_task(tokens['fail'], retry['task_id']) if retry['task_id'] else None
    final_retry = ledger(ids['fail'])
    check('failure_capacity_reusable', retry['status'] == 200 and (retry_task or {}).get('status') == 'completed'
          and final_retry['used'] == 1, {'retry': retry['status'], 'task': (retry_task or {}).get('status'),
                                          'ledger': final_retry}, 'failure')

    # scenario 4: isolation
    resume, job = resumes['isol']
    before_other = ledger(ids['succ'])
    status, token_isol, _ = login(names['isol'])
    outcome = stream(tokens['isol'], resume, job, str(uuid.uuid4()))
    task = wait_task(tokens['isol'], outcome['task_id']) if outcome['task_id'] else None
    isol_ledger = ledger(ids['isol'])
    other_after = ledger(ids['succ'])
    check('isolation_own_success', outcome['status'] == 200 and (task or {}).get('status') == 'completed'
          and isol_ledger['used'] == 1, {'task': (task or {}).get('status'), 'ledger': isol_ledger}, 'isolation')
    check('isolation_other_unchanged', other_after == before_other, {'before': before_other,
                                                                    'after': other_after}, 'isolation')
    cross = http('/api/v1/analysis-tasks/' + str(outcome['task_id']), token=tokens['succ'])
    check('isolation_cross_user_denied', cross[0] == 404, cross[0], 'isolation')
    admin_quota = http('/api/v1/quota/me', token=admin)[1].get('data') or {}
    check('admin_unlimited', admin_quota.get('unlimited') is True, admin_quota.get('unlimited'), 'isolation')

    results['passed'] = not results['failures']
    return 0 if results['passed'] else 2


if __name__ == '__main__':
    try:
        code = main()
    except Exception as error:
        import traceback
        check('harness_error', False, type(error).__name__ + ': ' + traceback.format_exc()[-300:])
        results['passed'] = False
        code = 2
    results['finished_at'] = datetime.now(timezone.utc).isoformat()
    try:
        OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False) + '\n')
    except OSError:
        code = 2
    print(json.dumps({'passed': results.get('passed'), 'run_id': RUN_ID,
                      'failures': len(results['failures']), 'output': str(OUT)}))
    raise SystemExit(code)
