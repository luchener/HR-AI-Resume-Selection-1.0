"""Root-only isolated preview setup on the existing server. No production writes.

Secrets are generated and used on the server, never printed or passed as argv.
Run from a reviewed release directory; existing private state is never replaced.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
from uuid import uuid4

PRIVATE = Path(os.environ.get('SQL_PREVIEW_PRIVATE_DIR', Path.home() / '.resume-workbench-sql-preview'))
DATABASE = 'resume_sql_preview_20261030'
ROOT = Path(__file__).resolve().parents[1]
# 复用运行时的严格 Origin 规则，避免预检与后端判定出现两套标准。
sys.path.insert(0, str(ROOT / 'apps' / 'backend'))
from prompt_safety import normalize_origin  # noqa: E402


def validated_trusted_origins(raw):
    """严格校验并规范化可信浏览器源；任何不合格输入都直接拒绝。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('SQL_PREVIEW_TRUSTED_ORIGINS is required (comma-separated absolute origins)')
    entries = [item.strip() for item in raw.split(',') if item.strip()]
    if not entries:
        raise ValueError('SQL_PREVIEW_TRUSTED_ORIGINS contains no usable origin')
    origins = []
    for item in entries:
        origin = normalize_origin(item)
        if origin is None:
            raise ValueError('Invalid SQL_PREVIEW_TRUSTED_ORIGINS entry: ' + item[:120])
        origins.append(origin)
    return list(dict.fromkeys(origins))


def check_ports():
    for port in (15442, 18010, 13010):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))


def guard_existing_state():
    """任何持久写入之前完成：拒绝已有状态与端口/卷/容器冲突。"""
    if PRIVATE.exists():
        raise ValueError('Existing preview state refused; inspect and reuse it explicitly')
    check_ports()
    volumes = run(['docker', 'volume', 'ls', '--format', '{{.Name}}']).splitlines()
    if any(name.startswith('resume-sql-preview_') for name in volumes):
        raise ValueError('Existing preview volumes refused')
    if run(['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=resume-sql-preview',
            '--format', '{{.ID}}']).strip():
        raise ValueError('Existing preview containers refused')


def run(command, *, payload=None, timeout=90, env=None):
    result = subprocess.run(command, input=payload, capture_output=True, text=True,
                            timeout=timeout, env=env)
    if result.returncode:
        raise RuntimeError('Captured preview operation failed')
    return result.stdout


def save(path, value, *, owner=0):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(value if isinstance(value, str) else json.dumps(value, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.chown(path, owner, owner)


def configuration():
    manifest = json.loads((ROOT / 'sql-preview-manifest.json').read_text())
    for name, expected in manifest['files'].items():
        path = ROOT / name
        path.resolve().relative_to(ROOT.resolve())
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Preview source manifest mismatch')
    release = manifest['commit'][:12]
    if not all(char in '0123456789abcdef' for char in release) or len(release) != 12:
        raise ValueError('Invalid release identity')
    return release, dict(os.environ, SQL_PREVIEW_RELEASE=release, SQL_PREVIEW_PRIVATE_DIR=str(PRIVATE))


def compose(*args, payload=None, timeout=90):
    _, environment = configuration()
    return run(['docker', 'compose', '-f', str(ROOT / 'docker-compose.sql-preview.yml'), *args],
               payload=payload, timeout=timeout, env=environment)


def prepare():
    # ── 纯配置/源清单校验：必须在任何持久写入之前完成 ──────────────────
    # 这样漏配或配置非法时不会留下半套私有状态，补齐后可以直接重试。
    origins = validated_trusted_origins(os.environ.get('SQL_PREVIEW_TRUSTED_ORIGINS', ''))
    release, _ = configuration()
    guard_existing_state()

    created = False
    try:
        PRIVATE.mkdir(mode=0o700)
        created = True
        originals = PRIVATE / 'originals'
        originals.mkdir(mode=0o700)
        os.chown(originals, 1000, 1000)
        identity = str(uuid4())
        save(originals / '.original-store-id', identity + '\n', owner=1000)
        passwords = {name: secrets.token_urlsafe(36) for name in ('POSTGRES_PASSWORD', 'MIGRATOR_PASSWORD', 'APP_PASSWORD', 'READONLY_PASSWORD')}
        postgres = dict(POSTGRES_USER='resume_bootstrap_admin', POSTGRES_DB=DATABASE,
                        POSTGRES_INITDB_ARGS='--auth-host=scram-sha-256', **passwords)
        save(PRIVATE / 'postgres.env', ''.join(k + '=' + v + '\n' for k, v in postgres.items()))
        def key():
            return base64.urlsafe_b64encode(os.urandom(32)).decode()
        config = dict(scope='isolated-sql-preview',
            database_url='postgresql+psycopg://resume_app:' + passwords['APP_PASSWORD'] + '@postgres:5432/' + DATABASE,
            jwt_secret=secrets.token_urlsafe(48), auth_secret=secrets.token_urlsafe(48),
            config_key=key(), config_key_id='sql-preview-' + release, invite_key=key(),
            admin_emails=['preview-owner@example.invalid'], originals_root='/app/sql-originals', storage_id=identity,
            captcha_enabled=False, max_workers=1, max_pending=4, environment={},
            trusted_origins=origins)
        save(PRIVATE / 'deployment.json', config, owner=1000)
        save(PRIVATE / 'migration.env', 'DATABASE_URL=postgresql+psycopg://resume_migrator:' + passwords['MIGRATOR_PASSWORD'] + '@postgres:5432/' + DATABASE + '\n')
        save(PRIVATE / 'preview-owner.json', {'username': 'preview_owner', 'password': secrets.token_urlsafe(24),
                                             'email': config['admin_emails'][0]})
        save(PRIVATE / 'identity.json', {'release': release, 'database': DATABASE, 'production_cutover': False})
    except BaseException:
        # 只清理本次调用创建出来的目录；绝不触碰已存在的私有状态。
        if created:
            shutil.rmtree(PRIVATE, ignore_errors=True)
        raise
    print(json.dumps({'prepared': True, 'database': DATABASE, 'release': release, 'production_cutover': False}))


def initialize():
    identity = json.loads((PRIVATE / 'identity.json').read_text())
    if identity.get('database') != DATABASE or identity.get('production_cutover') is not False:
        raise ValueError('Unexpected preview identity')
    if (PRIVATE / 'initialized.json').exists():
        raise ValueError('Preview already initialized')
    release, _ = configuration()
    manifest = json.loads((ROOT / 'sql-preview-manifest.json').read_text())
    image = json.loads(run(['docker', 'image', 'inspect', 'resume-sql-preview-backend:' + release]))[0]
    if image['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != manifest['commit']:
        raise ValueError('Preview image revision mismatch')
    compose('up', '-d', '--no-build', '--wait', 'postgres', timeout=120)
    compose('run', '--rm', '--no-deps', 'migrate', timeout=120)
    # SCRAM authentication is enabled for local sockets too: use the container's
    # existing bootstrap secret internally, without printing or argv exposure.
    compose('exec', '-T', 'postgres', 'sh', '-c',
            'export PGPASSWORD="$POSTGRES_PASSWORD"; sh /docker-entrypoint-initdb.d/10-database-init.sh grants')
    owner = json.loads((PRIVATE / 'preview-owner.json').read_text())
    code = '''import json,sys
from db_deployment import wsgi
from db_auth import AuthService
credentials=json.load(sys.stdin)
app=wsgi(); owned=app.extensions['db_application']
try:
 user,error=AuthService(owned.sessions).create_user(**credentials)
 if error:raise RuntimeError('Preview owner creation failed')
finally:
 if not owned.close()['complete']:raise RuntimeError('Preview initialization did not drain')
'''
    compose('run', '--rm', '--no-deps', '-T', 'backend', 'python', '-c', code, payload=json.dumps(owner))
    save(PRIVATE / 'initialized.json', {'database': DATABASE, 'release': identity['release']})
    print(json.dumps({'initialized': True, 'database': DATABASE, 'production_cutover': False}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'initialize'])
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run on the reviewed server as root')
    try:
        (prepare if args.action == 'prepare' else initialize)()
    except Exception:
        print('Isolated preview operation failed; private state retained for inspection')
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
