"""Release-image quota smoke through the formal Gunicorn entry (no fake model)."""
import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import zipfile

BASE = 'http://127.0.0.1:18050'
ORIGIN = 'http://127.0.0.1:13050'
PG = 'resume-release-pg-20261102'
BE = 'release-sql-backend-env'
ROOT = os.environ.get('RESUME_QUOTA_RELEASE_DIR', os.path.expanduser('~/.resume-workbench/release'))
DB = 'resume_sql_preview_release_20261102'
ADMIN_USER = os.environ['RELEASE_ADMIN_USER']
ADMIN_PASS = os.environ['RELEASE_ADMIN_PASS']
BE_IMAGE = 'resume-release-sql-backend:691fc7d12060'


def run(a, **kw):
    return subprocess.run(a, capture_output=True, text=True, timeout=kw.get('timeout', 180))


def http(path, payload=None, token=None, raw=None, ctype=None, method=None):
    h = {'Origin': ORIGIN}
    if token:
        h['Authorization'] = 'Bearer ' + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode(); h['Content-Type'] = 'application/json'
    if raw is not None:
        data = raw; h['Content-Type'] = ctype
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=h, method=method), timeout=120) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'{}')
        except Exception:
            return e.code, {}


def psql(sql):
    r = run(['docker', 'exec', '-i', PG, 'sh', '-c',
             'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -X -h 127.0.0.1 '
             '-U resume_bootstrap_admin -d "%s" -At -c "$1"' % DB, 'x', sql], input='\n')
    return r.stdout.strip() or r.stderr.strip()[-120:]


def valid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def category(status):
    return 'http_%dxx' % (status // 100) if isinstance(status, int) else 'no_response'


def stream(token, resume_id, job_id, key):
    request = urllib.request.Request(BASE + '/api/v1/resumes/hr-analysis?stream=true',
                                     data=json.dumps({'resume_ids': [resume_id], 'job_id': job_id}).encode(),
                                     headers={'Origin': ORIGIN, 'Authorization': 'Bearer ' + token,
                                              'Content-Type': 'application/json', 'Idempotency-Key': key})
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            text = response.read().decode('utf-8', 'replace')
            status = response.status
    except urllib.error.HTTPError as error:
        text = error.read().decode('utf-8', 'replace'); status = error.code
    match = re.search(r'"task_id"\s*:\s*"([0-9a-f-]{36})"', text)
    return status, (match.group(1) if match else None)


def main():
    admin = ((http('/api/v1/auth/login', {'username': ADMIN_USER, 'password': ADMIN_PASS})[1].get('data') or {}).get('token'))
    if not admin:
        print(json.dumps({'error': 'admin_login'})); return 2
    user = 'release_quota_' + uuid.uuid4().hex[:6]
    password = 'ReleaseQ9x'
    seed = ('import sys\nfrom db_deployment import wsgi\nfrom db_auth import AuthService\n'
            'u,p=sys.argv[1],sys.argv[2]\napp=wsgi(); owned=app.extensions["db_application"]\n'
            'try:\n svc=AuthService(owned.sessions)\n if not svc.authenticate_user(u,p): svc.create_user(u,p,u+"@example.invalid")\n print("ok")\nfinally:\n owned.close()\n')
    created = run(['docker', 'run', '--rm', '--network', 'resume-release-net-20261102',
                   '-e', 'SQL_DEPLOYMENT_FILE=/run/secrets/sql-deployment.json',
                   '-v', ROOT + '/deployment.json:/run/secrets/sql-deployment.json:ro',
                   '-v', ROOT + '/originals:/app/sql-originals',
                   '--entrypoint', 'python', BE_IMAGE, '-c', seed, user, password])
    if created.returncode:
        print(json.dumps({'error': 'user_create'})); return 2
    settings_status = http('/api/v1/admin/quota/settings', {'quota_enabled': True, 'default_daily': 5,
                                                            'email_notify_enabled': False}, token=admin, method='PUT')[0]
    policy_status = http('/api/v1/admin/quota/users/' + user, {'daily_limit': 1, 'analysis_enabled': True,
                                                               'email_notify': False}, token=admin, method='PUT')[0]
    if settings_status != 200 or policy_status != 200:
        print(json.dumps({'stage': 'policy', 'settings': settings_status, 'user': policy_status})); return 2
    user_id = psql("SELECT id::text FROM users WHERE username='%s';" % user)
    usage_before = int(psql("SELECT coalesce(sum(used),0) FROM quota_daily_usage WHERE user_id='%s'::uuid;" % user_id) or 0)
    token = ((http('/api/v1/auth/login', {'username': user, 'password': password})[1].get('data') or {}).get('token'))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   '<w:body><w:p><w:r><w:t>Release quota smoke</w:t></w:r></w:p></w:body></w:document>')
    boundary = 'b' + uuid.uuid4().hex
    body = (('--%s\r\nContent-Disposition: form-data; name="file"; filename="q.docx"\r\n'
             'Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n') % boundary).encode() \
        + buffer.getvalue() + ('\r\n--%s--\r\n' % boundary).encode()
    status, uploaded = http('/api/v1/resumes/upload', token=token, raw=body,
                            ctype='multipart/form-data; boundary=' + boundary)
    resume_id = (uploaded.get('data') or {}).get('resume_id') or uploaded.get('resume_id')
    if status != 200 or not valid(resume_id):
        print(json.dumps({'stage': 'upload', 'category': category(status), 'valid_uuid': valid(resume_id)})); return 2
    status, job = http('/api/v1/jobs/upload', {'resume_id': resume_id,
                                               'job_descriptions': ['Synthetic backend role']}, token=token)
    job_ids = (job.get('data') or {}).get('job_id') or job.get('job_id')
    job_id = job_ids[0] if isinstance(job_ids, list) and job_ids else None
    if status != 200 or not valid(job_id):
        print(json.dumps({'stage': 'job', 'category': category(status), 'valid_uuid': valid(job_id)})); return 2
    success_status, task_id = stream(token, resume_id, job_id, str(uuid.uuid4()))
    if success_status != 200 or not valid(task_id):
        print(json.dumps({'stage': 'first_submit', 'category': category(success_status), 'task_id': valid(task_id)})); return 2
    deadline = time.time() + 300
    task_state = reservation = None
    while time.time() < deadline:
        task_state = psql("SELECT status FROM analysis_tasks WHERE id='%s'::uuid;" % task_id)
        reservation = psql("SELECT status||'|'||(settled_at IS NOT NULL) FROM quota_reservations WHERE task_id='%s'::uuid;" % task_id)
        if task_state in ('completed', 'failed', 'partial_success', 'cancelled'):
            break
        time.sleep(1)
    usage_after = int(psql("SELECT coalesce(sum(used),0) FROM quota_daily_usage WHERE user_id='%s'::uuid;" % user_id) or 0)
    tasks_before = int(psql("SELECT count(*) FROM analysis_tasks WHERE owner_user_id='%s'::uuid;" % user_id) or 0)
    reservations_before = int(psql("SELECT count(*) FROM quota_reservations WHERE user_id='%s'::uuid;" % user_id) or 0)
    denied_status, _ = stream(token, resume_id, job_id, str(uuid.uuid4()))
    tasks_after = int(psql("SELECT count(*) FROM analysis_tasks WHERE owner_user_id='%s'::uuid;" % user_id) or 0)
    reservations_after = int(psql("SELECT count(*) FROM quota_reservations WHERE user_id='%s'::uuid;" % user_id) or 0)
    ledger = {'open_tasks': int(psql("SELECT count(*) FROM analysis_tasks WHERE status IN ('pending','running');") or 0),
              'reserved': int(psql('SELECT coalesce(sum(reserved),0) FROM quota_daily_usage;') or 0),
              'unsettled': int(psql('SELECT count(*) FROM quota_reservations WHERE settled_at IS NULL;') or 0)}
    result = {'user': user, 'task_id': task_id, 'task_status': task_state, 'reservation': reservation,
              'usage_before': usage_before, 'usage_after': usage_after,
              'denied_status': denied_status, 'tasks_before': tasks_before, 'tasks_after': tasks_after,
              'reservations_before': reservations_before, 'reservations_after': reservations_after,
              'ledger': ledger}
    ok = (task_state == 'completed' and reservation in ('succeeded|true', 'succeeded|t') and usage_before == 0 and usage_after == 1
          and denied_status == 429 and tasks_before == tasks_after and reservations_before == reservations_after
          and all(ledger[k] == 0 for k in ('open_tasks', 'reserved', 'unsettled')))
    result['passed'] = ok
    print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == '__main__':
    raise SystemExit(main())
