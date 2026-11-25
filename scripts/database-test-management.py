"""Run synthetic management acceptance in one disposable, guarded PostgreSQL.

Uses the existing review host's Docker over SSH, or explicitly --local Docker;
no production volumes, data,
provider calls or environment reads. Requires --execute-synthetic explicitly.
Outputs counts and fixed statuses, with synthetic test diagnostics in .test-tmp.
"""
import argparse
import base64
from datetime import datetime, timezone
import json
import hashlib
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import shutil
from time import monotonic, sleep
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'apps/backend'
HOST = os.environ.get('RESUME_QA_SSH_TARGET', 'resume-qa.invalid')
NAME = 'resume-db-review-test'
LABEL = 'resume-account-adapter-review'
PORT = 15439
SOURCE_MANIFEST_PATH = ROOT / 'scripts' / 'database-source-manifest.json'


def load_source_manifest():
    try:
        value = json.loads(SOURCE_MANIFEST_PATH.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'Source manifest unavailable: {type(exc).__name__}') from None
    if not isinstance(value, dict) or set(value) != {'format', 'source_hash_mode', 'files'}:
        raise ValueError('Source manifest has an unsupported shape')
    if type(value['format']) is not int or value['format'] != 1:
        raise ValueError('Source manifest format is unsupported')
    if value['source_hash_mode'] != 'lf-normalized-sha256-v1':
        raise ValueError('Source manifest hash mode is unsupported')
    files = value['files']
    if not isinstance(files, list) or not files:
        raise ValueError('Source manifest files must be a non-empty list')
    if any(not isinstance(path, str) or not path or '\\' in path for path in files):
        raise ValueError('Source manifest contains an invalid path')
    if len(set(files)) != len(files):
        raise ValueError('Source manifest contains duplicate paths')
    for relative in files:
        path = Path(relative)
        if (path.is_absolute() or path.as_posix() != relative
                or any(part in {'', '.', '..'} for part in path.parts)):
            raise ValueError('Source manifest path escapes the workspace')
        try:
            (ROOT / path).resolve().relative_to(ROOT)
        except (OSError, ValueError):
            raise ValueError('Source manifest path escapes the workspace') from None
    if 'scripts/database-source-manifest.json' not in files:
        raise ValueError('Source manifest must bind its own file')
    return tuple(files), value['source_hash_mode']


SOURCE_FILES, SOURCE_HASH_MODE = load_source_manifest()


def canonical_source_bytes(data):
    if not isinstance(data, bytes):
        raise TypeError('source content must be bytes')
    return data.replace(b'\r\n', b'\n').replace(b'\r', b'\n')


def source_digest(path):
    return hashlib.sha256(canonical_source_bytes(path.read_bytes())).hexdigest()


def source_hashes():
    return {path: source_digest(ROOT / path) for path in SOURCE_FILES}


def review_source_binding(review_package):
    spec = importlib.util.spec_from_file_location(
        'synthetic_review_preflight', ROOT / 'scripts/database-production-release-preflight.py')
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    commit, tree = preflight._load_review_subject(Path(review_package))
    return preflight._git_binding(commit, tree)


def loopback_port_is_free(port=PORT, host='127.0.0.1', attempts=10):
    for attempt in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                if probe.connect_ex((host, port)) == 0:
                    return False
            except OSError:
                return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError:
                if attempt + 1 == attempts:
                    return False
                sleep(0.1)
                continue
        return True
    return False


def remote(mode, identity, *, local=False, container_id=''):
    init = base64.b64encode((ROOT / 'scripts/database-init.sh').read_bytes()).decode()
    script = '''import base64,json,subprocess,sys,socket
mode,identity,expected_container_id=sys.argv[1:4]
name='resume-db-review-test'
def run(args,payload=None):
 r=subprocess.run(args,input=payload,capture_output=True,timeout=45)
 if r.returncode:raise RuntimeError('Synthetic remote command failed')
 return r.stdout
def port_free():
 for attempt in range(10):
  with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:
   probe.settimeout(1)
   try:
    if probe.connect_ex(('127.0.0.1',15439))==0:return False
   except OSError:return False
  with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:
   probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
   try:probe.bind(('127.0.0.1',15439))
   except OSError:
    if attempt+1==10:return False
    import time;time.sleep(0.1);continue
  return True
 return False
def checked_port_bindings(x):
 host_config=x['HostConfig']
 if host_config.get('PublishAllPorts') is not False:raise RuntimeError('PublishAllPorts must be disabled')
 host_bindings=host_config.get('PortBindings')
 network_ports=x.get('NetworkSettings',{}).get('Ports')
 if not isinstance(host_bindings,dict) or set(host_bindings)!={'5432/tcp'}:raise RuntimeError('Unexpected host port mappings')
 if not isinstance(network_ports,dict) or set(network_ports)!={'5432/tcp'}:raise RuntimeError('Unexpected published container ports')
 host_rows=host_bindings['5432/tcp']
 network_rows=network_ports['5432/tcp']
 def binding_ok(rows):
  return isinstance(rows,list) and len(rows)==1 and isinstance(rows[0],dict) and rows[0].get('HostIp')=='127.0.0.1' and rows[0].get('HostPort')=='15439'
 host_ok=binding_ok(host_rows)
 network_ok=binding_ok(network_rows)
 if not host_ok or not network_ok:raise RuntimeError('Port binding mismatch')
 return {'host_ip':host_rows[0].get('HostIp'),'host_port':host_rows[0].get('HostPort'),'binding_count':sum(len(rows) for rows in host_bindings.values() if isinstance(rows,list)),'host_config_matches':host_ok,'network_settings_matches':network_ok}
def guard():
 rows=json.loads(run(['docker','inspect',name]))
 x=rows[0]
 if x['Name']!='/'+name or x['Config'].get('Labels',{}).get('purpose')!='resume-account-adapter-review' or x['Config'].get('Labels',{}).get('review-session')!=identity:
  raise RuntimeError('Container identity mismatch')
 if x.get('Mounts') and any(m.get('Type') in ('bind','volume') for m in x['Mounts']):
  raise RuntimeError('Persistent mount forbidden')
 if x['HostConfig'].get('NetworkMode')=='host' or x['HostConfig'].get('Privileged'):
  raise RuntimeError('Unsafe container configuration')
 checked_port_bindings(x)
 return x
if mode=='create':
 if subprocess.run(['docker','inspect',name],capture_output=True).returncode==0:raise RuntimeError('Existing container refused')
 run(['docker','run','-d','--name',name,'--label','purpose=resume-account-adapter-review','--label','review-session='+identity,
      '--memory=256m','--memory-swap=256m','--cpus=0.5','--pids-limit=128','--tmpfs','/var/lib/postgresql/data:rw,size=128m',
      '-e','POSTGRES_USER=review_bootstrap','-e','POSTGRES_DB=review_test','-e','POSTGRES_PASSWORD=synthetic-local-review-only',
      '-e','POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256','-p','127.0.0.1:15439:5432','postgres:16-alpine',
      '-c','shared_buffers=16MB','-c','max_connections=20'])
elif mode=='ready':
 guard()
 r=subprocess.run(['docker','exec',name,'pg_isready','-U','review_bootstrap','-d','review_test'],capture_output=True)
 print(json.dumps({'ready':r.returncode==0}));sys.exit(0)
elif mode=='identity':
 x=guard();labels=x['Config'].get('Labels',{})
 mounts=x.get('Mounts') or []
 persistent=[m for m in mounts if m.get('Type') in ('bind','volume')]
 tmpfs_config=x['HostConfig'].get('Tmpfs') or {}
 volume_ids=sorted(str(m.get('Name')) for m in persistent if m.get('Type')=='volume' and m.get('Name'))
 target='/var/lib/postgresql/data'
 if persistent or volume_ids:raise RuntimeError('Persistent mount forbidden')
 if set(tmpfs_config)!={target} or tmpfs_config.get(target)!='rw,size=128m':raise RuntimeError('Tmpfs identity mismatch')
 host_config=x['HostConfig']
 port_observed=checked_port_bindings(x)
 print(json.dumps({'container_id':x['Id'],'container_name':x['Name'].lstrip('/'),'review_session':labels.get('review-session'),'purpose_label':labels.get('purpose'),'labels':labels,'image_reference':x['Config'].get('Image'),'image_id':x.get('Image'),'network_mode':host_config.get('NetworkMode'),'storage':{'tmpfs_target':target,'tmpfs_options':tmpfs_config.get(target),'persistent_volume_count':len(persistent),'volume_ids':volume_ids,'persistent_volume_absent':not persistent and not volume_ids},'port':{'container_port':'5432/tcp','host_ip':port_observed['host_ip'],'host_port':int(port_observed['host_port']),'binding_count':port_observed['binding_count'],'host_config_matches':port_observed['host_config_matches'],'network_settings_matches':port_observed['network_settings_matches']}}));sys.exit(0)
elif mode in ('init','grants'):
 guard()
 if mode=='init':
  run(['docker','exec','-i',name,'psql','-X','-U','review_bootstrap','-d','review_test','-v','ON_ERROR_STOP=1'],
      b'CREATE ROLE resume_bootstrap_admin LOGIN SUPERUSER;')
 prefix=b'export POSTGRES_USER=resume_bootstrap_admin POSTGRES_DB=review_test MIGRATOR_PASSWORD=review-migrator-only APP_PASSWORD=review-app-only READONLY_PASSWORD=review-reader-only\\n'
 script=base64.b64decode(''' + repr(init) + ''')
 run(['docker','exec','-i',name,'sh','-s','--',mode],prefix+script)
elif mode=='port-free':
 if subprocess.run(['docker','inspect',name],capture_output=True).returncode==0:
  print(json.dumps({'host':'127.0.0.1','port':15439,'released':False,'method':'connect-and-bind-v1'}));sys.exit(0)
 print(json.dumps({'host':'127.0.0.1','port':15439,'released':port_free(),'method':'connect-and-bind-v1'}));sys.exit(0)
elif mode=='remove':
 x=guard();cid=x.get('Id')
 if expected_container_id and cid!=expected_container_id:raise RuntimeError('Container id mismatch')
 run(['docker','rm','-f',name])
 print(json.dumps({'removed':True,'container_id':cid,'mode':mode}));sys.exit(0)
elif mode=='absence':
 name_present=subprocess.run(['docker','inspect',name],capture_output=True).returncode==0
 id_present=bool(expected_container_id) and subprocess.run(['docker','inspect',expected_container_id],capture_output=True).returncode==0
 print(json.dumps({'container_name':name,'container_id':expected_container_id,'inspect_absent':not name_present,'container_id_absent':not id_present and bool(expected_container_id),'port_released':port_free(),'method':'docker-inspect-absence-and-connect-bind-v1'}));sys.exit(0)
else:raise RuntimeError('Unknown synthetic mode')
print(json.dumps({'passed':True,'mode':mode}))
'''
    command = ([sys.executable, '-', mode, identity, container_id] if local else
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '--', HOST, 'python3', '-',
         mode, identity, container_id])
    result = subprocess.run(command, input=script.encode(), capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Synthetic remote phase failed: ' + mode)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-synthetic', action='store_true', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--review-package', type=Path, required=True,
                        help='Fixed review subject descriptor containing commit/tree')
    parser.add_argument('--content-only', action='store_true', help='Run content and its immediate HTTP/security dependencies')
    parser.add_argument('--auth-only', action='store_true', help='Run public auth and its immediate migration/security dependencies')
    parser.add_argument('--invite-only', action='store_true', help='Run invitation workflow and immediate auth/security dependencies')
    parser.add_argument('--retention-only', action='store_true', help='Run SQL retention cleanup and its immediate content/security dependencies')
    parser.add_argument('--task-only', action='store_true', help='Run analysis result, task lifecycle, scope, recovery and quota transaction acceptance')
    parser.add_argument('--admin-ai-only', action='store_true', help='Run administrator and AI PostgreSQL adapter acceptance')
    parser.add_argument('--drain-only', action='store_true', help='Run synthetic long-request/SSE/worker drain rehearsal')
    parser.add_argument('--history-only', action='store_true', help='Run read-only history HTTP/SQL contracts and role checks')
    parser.add_argument('--announcements-only', action='store_true', help='Run announcement migration, HTTP/SQL and concurrent dismissal acceptance')
    parser.add_argument('--email-only', action='store_true', help='Run email notification SQL/HTTP and delivery idempotency acceptance')
    parser.add_argument('--local', action='store_true', help='Use disposable local Docker, without SSH or any remote host')
    parser.add_argument('--application-only', action='store_true', help='Run composed SQL application and real loopback HTTP acceptance')
    parser.add_argument('--provider-only', action='store_true', help='Run SQL-backed provider boundary and diagnostic acceptance without real vendor calls')
    parser.add_argument('--screening-only', action='store_true', help='Run existing Agent and normalization bound to SQL workflow')
    parser.add_argument('--events-only', action='store_true', help='Run durable Agent event, cursor and stream acceptance')
    parser.add_argument('--binding-only', action='store_true', help='Run batch model snapshot/version and immutability acceptance')
    args = parser.parse_args()
    if sum((args.content_only, args.auth_only, args.invite_only, args.retention_only, args.task_only,
            args.admin_ai_only, args.drain_only, args.history_only, args.announcements_only,
            args.email_only, args.application_only, args.provider_only, args.screening_only, args.events_only, args.binding_only)) > 1:
        parser.error('Choose only one diagnostic subset')
    if sys.flags.optimize:
        raise SystemExit('Acceptance assertions require normal Python mode')
    target = args.evidence.resolve()
    if target.exists() or not target.parent.is_dir():
        raise SystemExit('Evidence must use a new path in an existing directory')
    source_binding = review_source_binding(args.review_package)
    if not source_binding['passed']:
        raise SystemExit('Review subject or source status check failed before container creation')
    identity = uuid4().hex
    logdir = ROOT / '.test-tmp'
    logdir.mkdir(exist_ok=True)
    logfile = logdir / ('management-' + identity + '.log')
    test_temp = logdir / ('temp-' + identity)
    test_temp.mkdir(parents=True, exist_ok=False)
    object_temp = logdir / ('objects-' + identity)
    object_temp.mkdir(parents=True, exist_ok=False)
    tunnel = None
    create_attempted = False
    container_identity = {}
    container_removal = {}
    passed = False
    phase = 'preflight'
    summary = {}
    source_at_start = source_hashes()
    env = dict(os.environ, ENV='local', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
        DOTENV_DISABLED='1', LLM_API_KEY='', SMTP_PASSWORD='', SMTP_ENABLED='false')
    # Windows antivirus/indexer ACLs can deny the system temp directory to
    # subprocesses.  Keep synthetic fixtures inside this workspace-owned,
    # disposable directory so test failures reflect the code under review.
    env['TEMP'] = str(test_temp)
    env['TMP'] = str(test_temp)
    env['TMPDIR'] = str(test_temp)
    env['DB_REVIEW_OBJECTS_ROOT'] = str(object_temp)
    env['DATABASE_TEST_URL'] = 'postgresql+psycopg://review_bootstrap:synthetic-local-review-only@127.0.0.1:15439/review_test'
    env['DATABASE_URL'] = 'postgresql+psycopg://resume_migrator:review-migrator-only@127.0.0.1:15439/review_test'
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', PORT))
        phase = 'create'
        create_attempted = True
        remote('create', identity, local=args.local)
        deadline = monotonic() + 40
        while not remote('ready', identity, local=args.local)['ready']:
            if monotonic() > deadline:
                raise RuntimeError('Synthetic readiness timeout')
            sleep(0.5)
        remote('init', identity, local=args.local)
        phase = 'identity'
        container_identity = remote('identity', identity, local=args.local)
        phase = 'tunnel'
        if not args.local:
            tunnel = subprocess.Popen(['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                '-o', 'ExitOnForwardFailure=yes', '-L', '127.0.0.1:15439:127.0.0.1:15439', '--', HOST],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = monotonic() + 15
        while True:
            if tunnel is not None and tunnel.poll() is not None:
                raise RuntimeError('Synthetic tunnel unavailable')
            try:
                with socket.create_connection(('127.0.0.1', PORT), timeout=1):
                    break
            except OSError:
                if monotonic() > deadline:
                    raise RuntimeError('Synthetic tunnel timeout')
                sleep(0.1)
        with logfile.open('xb') as log:
            os.chmod(logfile, 0o600)
            phase = 'migrate'
            result = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=BACKEND,
                                    env=env, stdout=log, stderr=log, timeout=90)
            if result.returncode:
                raise RuntimeError('Synthetic migration failed')
            remote('grants', identity, local=args.local)
            phase = 'test'
            modules = ['test_db_content_live', 'test_db_original_objects',
                       'test_db_original_live', 'test_db_original_render', 'test_db_original_worker',
                       'test_db_accounts_live', 'test_db_account_lifecycle_live',
                       'test_db_config_diagnostics_live',
                       'test_db_management_http', 'test_db_http_preview', 'test_db_auth', 'test_db_config_resolved',
                        'test_db_auth_live', 'test_db_config_service', 'test_config_merge', 'test_db_secrets',
                       'test_db_audit', 'test_database_roles', 'test_database_migration',
                       'test_db_http_shadow', 'test_config_dotenv', 'test_database_management_guard', 'test_db_analysis_workflow',
                       'test_db_task_lifecycle', 'test_db_analysis_service', 'test_db_quota_live',
                       'test_db_task_scope_live', 'test_db_admin_ai', 'test_db_admin_ai_live',
                       'test_db_reconcile', 'test_db_reconcile_empty',
                       'test_db_public_auth', 'test_db_public_auth_live', 'test_db_invites', 'test_db_invites_live',
                       'test_db_change_log', 'test_db_analysis_history', 'test_db_analysis_history_live',
                       'test_db_announcements', 'test_db_announcements_live', 'test_db_application', 'test_db_provider_gateway',
                       'test_db_screening', 'test_screening_agent', 'test_evidence_pipeline', 'test_db_task_events', 'test_db_model_binding', 'test_db_deployment', 'test_db_task_features']
            if args.binding_only:
                modules = ['test_database_roles', 'test_db_model_binding', 'test_db_screening', 'test_db_task_events']
            if args.events_only:
                modules = ['test_database_roles', 'test_db_task_events', 'test_db_screening']
            if args.screening_only:
                modules = ['test_database_roles', 'test_db_screening', 'test_screening_agent', 'test_evidence_pipeline']
            if args.application_only:
                modules = ['test_database_roles', 'test_db_application', 'test_db_deployment', 'test_db_task_features']
            if args.provider_only:
                modules = ['test_database_roles', 'test_db_provider_gateway', 'test_db_config_resolved',
                           'test_db_config_diagnostics_live', 'test_db_application']
            if args.content_only:
                modules = ['test_db_content_live', 'test_db_original_objects', 'test_db_http_preview',
                           'test_db_original_live', 'test_db_original_render', 'test_db_original_worker',
                           'test_database_roles', 'test_db_reconcile_empty', 'test_database_management_guard']
            if args.auth_only:
                modules = ['test_database_roles',
                           'test_db_public_auth', 'test_db_public_auth_live', 'test_database_management_guard']
            if args.invite_only:
                modules = ['test_database_roles', 'test_db_invites', 'test_db_invites_live',
                           'test_db_public_auth', 'test_db_auth_state_migration',
                           'test_db_auth_state_migration_live', 'test_db_change_log',
                           'test_database_management_guard']
            if args.retention_only:
                modules = ['test_database_roles', 'test_db_retention', 'test_db_retention_live']
            if args.task_only:
                modules = ['test_database_roles', 'test_db_task_lifecycle', 'test_db_analysis_service',
                           'test_db_analysis_workflow', 'test_db_quota_live', 'test_db_task_scope_live']
            if args.admin_ai_only:
                modules = ['test_database_roles', 'test_db_admin_ai', 'test_db_admin_ai_live']
            if args.drain_only:
                modules = ['test_db_workflow_drain_live']
            if args.history_only:
                modules = ['test_database_roles', 'test_db_analysis_history', 'test_db_analysis_history_live',
                           'test_db_http_preview', 'test_db_http_shadow', 'test_db_audit',
                           'test_db_audit_live', 'test_database_management_guard']
            if args.announcements_only:
                modules = ['test_database_roles', 'test_db_announcements', 'test_db_announcements_live',
                           'test_db_http_preview', 'test_db_http_shadow', 'test_db_audit', 'test_db_audit_live',
                           'test_database_management_guard', 'test_database_migration',
                           'test_db_analysis_history', 'test_db_analysis_history_live']
            if args.email_only:
                modules = ['test_database_roles', 'test_db_email_notifications', 'test_db_email_notifications_live', 'test_db_http_preview',
                           'test_db_audit', 'test_db_audit_live', 'test_database_management_guard',
                           'test_database_migration']
            # Use unittest's result object for counts; diagnostics remain synthetic and local.
            def harness(selected):
                return '''import json,unittest
suite=unittest.defaultTestLoader.loadTestsFromNames(''' + repr(selected) + ''')
result=unittest.TextTestRunner(verbosity=2,failfast=True).run(suite)
from test_db_accounts_live import SHADOW_EVIDENCE
from test_db_config_resolved import SHADOW_EVIDENCE as CONFIG_SHADOW
from test_db_content_live import SHADOW_EVIDENCE as CONTENT_SHADOW
from test_db_analysis_history_live import SHADOW_EVIDENCE as HISTORY_SHADOW
from test_db_announcements_live import SHADOW_EVIDENCE as ANNOUNCEMENT_SHADOW
print('RESULT_JSON='+json.dumps({'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'offline_shadow':SHADOW_EVIDENCE+CONFIG_SHADOW+CONTENT_SHADOW+HISTORY_SHADOW+ANNOUNCEMENT_SHADOW}))
raise SystemExit(not result.wasSuccessful())
'''
            migration_modules = ['test_db_account_migration_live', 'test_db_public_auth_migration_live',
                                 'test_db_invite_migration_live', 'test_db_auth_state_migration_live',
                                 'test_db_announcement_migration_live']
            if args.application_only or args.provider_only or args.screening_only or args.events_only or args.binding_only:
                migration_modules = []
            if args.invite_only:
                migration_modules = ['test_db_public_auth_migration_live', 'test_db_invite_migration_live',
                                     'test_db_auth_state_migration_live']
            elif args.retention_only:
                # Retention does not change schema; policy checks and the live
                # suite run in one guarded PostgreSQL acceptance.
                migration_modules = ['test_db_retention']
            elif args.task_only:
                migration_modules = ['test_db_task_lifecycle']
            elif args.admin_ai_only:
                migration_modules = []
            elif args.drain_only:
                migration_modules = []
            elif args.history_only:
                migration_modules = []
            elif args.announcements_only:
                migration_modules = ['test_db_announcement_migration_live']
            elif args.email_only:
                migration_modules = []
            elif args.auth_only:
                migration_modules = ['test_db_public_auth_migration_live', 'test_db_auth_state_migration_live']
            first = subprocess.run([sys.executable, '-c', harness(migration_modules)], cwd=BACKEND,
                                   env=env, stdout=log, stderr=log, timeout=120)
            if first.returncode:
                raise RuntimeError('Synthetic migration acceptance failed')
            # Downgrade/re-upgrade recreates tables with fail-closed privileges.
            # Apply the actual reviewed allowlist again before runtime tests.
            remote('grants', identity, local=args.local)
            result = subprocess.run([sys.executable, '-c', harness(modules)], cwd=BACKEND, env=env,
                                    stdout=log, stderr=log, timeout=1200)
        summaries = []
        for line in logfile.read_text(encoding='utf-8', errors='replace').splitlines():
            if line.startswith('RESULT_JSON='):
                summaries.append(json.loads(line.removeprefix('RESULT_JSON=')))
        summary = {key: sum(item[key] for item in summaries) for key in ('run', 'failures', 'errors', 'skipped')}
        summary['offline_shadow'] = [item for part in summaries for item in part['offline_shadow']]
        if result.returncode or len(summaries) != 2 or summary['skipped']:
            raise RuntimeError('Synthetic management tests failed or skipped')
        if source_hashes() != source_at_start:
            raise RuntimeError('Acceptance source changed during run')
        passed = True
    except Exception as exc:
        summary.update(passed=False, error_type=type(exc).__name__, failed_phase=phase)
    finally:
        if tunnel is not None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=10)
            except subprocess.TimeoutExpired:
                tunnel.kill()
                tunnel.wait(timeout=5)
            if tunnel.stderr:
                tunnel.stderr.close()
        removed = False
        port_checks = {}
        if create_attempted:
            if not container_identity:
                try:
                    container_identity = remote('identity', identity, local=args.local)
                except Exception:
                    passed = False
            identity_id = str(container_identity.get('container_id') or '')
            try:
                removal = remote('remove', identity, local=args.local,
                                 container_id=identity_id)
                removed = (bool(identity_id) and removal.get('removed') is True
                           and removal.get('container_id') == identity_id)
                if not removed:
                    passed = False
            except Exception:
                passed = False
            removal_checks = {
                'container_id': identity_id, 'container_name': NAME,
                'inspect_absent': False, 'container_id_absent': False,
                'port_released': False,
            }
            if removed:
                try:
                    absence = remote('absence', identity, local=args.local,
                                     container_id=identity_id)
                    removal_checks.update({
                        'inspect_absent': absence.get('inspect_absent') is True,
                        'container_id_absent': absence.get('container_id_absent') is True,
                        'port_released': absence.get('port_released') is True,
                    })
                    if not (removal_checks['inspect_absent']
                            and removal_checks['container_id_absent']
                            and removal_checks['port_released']
                            and absence.get('container_name') == NAME
                            and absence.get('container_id') == identity_id):
                        passed = False
                except Exception:
                    passed = False
            container_removal = removal_checks
        try:
            source_unchanged = source_hashes() == source_at_start
        except OSError:
            source_unchanged = False
        if not source_unchanged:
            passed = False
        try:
            source_binding_at_end = review_source_binding(args.review_package)
        except (OSError, RuntimeError, ValueError):
            source_binding_at_end = {'passed': False}
        if source_binding_at_end != source_binding or not source_binding_at_end.get('passed'):
            passed = False
        try:
            port_checks['local'] = {
                'host': '127.0.0.1', 'port': PORT, 'scope': 'local',
                'method': 'connect-and-bind-v1', 'released': loopback_port_is_free()}
        except OSError as exc:
            port_checks['local'] = {
                'host': '127.0.0.1', 'port': PORT, 'scope': 'local',
                'method': 'connect-and-bind-v1', 'released': False,
                'error_type': type(exc).__name__}
        if not args.local and create_attempted:
            try:
                remote_port = remote('port-free', identity, local=False)
                port_checks['remote'] = {
                    'host': remote_port.get('host', '127.0.0.1'),
                    'port': remote_port.get('port', PORT), 'scope': 'remote',
                    'method': remote_port.get('method', 'connect-and-bind-v1'),
                    'released': remote_port.get('released') is True}
            except Exception as exc:
                port_checks['remote'] = {
                    'host': '127.0.0.1', 'port': PORT, 'scope': 'remote',
                    'method': 'connect-and-bind-v1', 'released': False,
                    'error_type': type(exc).__name__}
        loopback_port_released = bool(port_checks) and all(
            check.get('released') is True for check in port_checks.values())
        if not loopback_port_released:
            passed = False
        evidence = {'observed_at_utc': datetime.now(timezone.utc).isoformat(),
            'scope': ('synthetic-batch-model-binding' if args.binding_only else
                      'synthetic-durable-task-events' if args.events_only else
                      'synthetic-sql-screening-agent' if args.screening_only else
                      'synthetic-sql-provider-adapters' if args.provider_only else
                      'synthetic-composed-sql-application' if args.application_only else
                      'synthetic-invite-workflow' if args.invite_only else 'synthetic-public-auth-adapters' if args.auth_only else
                      'synthetic-content-adapters' if args.content_only else 'synthetic-retention-cleanup' if args.retention_only else
                      'synthetic-task-quota-transactions' if args.task_only else
                      'synthetic-admin-ai-sql-paths' if args.admin_ai_only else
                      'synthetic-long-request-sse-worker-drain' if args.drain_only else
                      'synthetic-read-only-analysis-history' if args.history_only else
                      'synthetic-announcement-adapters' if args.announcements_only else
                      'synthetic-email-notification-adapters' if args.email_only else
                      'synthetic-management-content-auth-and-invite-adapters'),
            'invite_only': args.invite_only,
            'content_only': args.content_only,
            'auth_only': args.auth_only,
            'retention_only': args.retention_only,
            'task_only': args.task_only,
            'admin_ai_only': args.admin_ai_only,
            'drain_only': args.drain_only,
            'history_only': args.history_only, 'local_docker': args.local,
            'announcements_only': args.announcements_only,
            'email_only': args.email_only,
            'application_only': args.application_only,
            'provider_only': args.provider_only,
            'screening_only': args.screening_only,
            'events_only': args.events_only,
            'binding_only': args.binding_only,
            'passed': passed, 'tests': summary, 'temporary_container_removed': removed,
            'container_identity': container_identity, 'container_removal': container_removal,
            'loopback_port_released': loopback_port_released,
            'loopback_port_checks': port_checks,
            'production_acceptance': False, 'production_dual_write_enabled': False,
            'synthetic_diagnostics': logfile.relative_to(ROOT).as_posix(), 'independent_review': False,
            'source_unchanged': source_unchanged, 'source_hash_mode': SOURCE_HASH_MODE,
            'source_sha256': source_at_start,
            'source_binding': source_binding, 'source_binding_at_end': source_binding_at_end}
        with target.open('x', encoding='utf-8') as output:
            json.dump(evidence, output, indent=2, sort_keys=True)
            output.write('\n')
        shutil.rmtree(test_temp, ignore_errors=True)
        shutil.rmtree(object_temp, ignore_errors=True)
        print(json.dumps(evidence, sort_keys=True))
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
