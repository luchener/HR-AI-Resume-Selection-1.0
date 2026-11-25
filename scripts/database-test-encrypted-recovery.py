"""Opt-in synthetic recovery across two disposable PostgreSQL containers.

Encryption, ciphertext acquisition, decryption, source/original/key recovery
and restored Python application processes run locally. Docker runs on the
existing development host because local Docker is unavailable. No production
volume, database, configuration, provider credential or business file is read.
Only the fixed labelled tmpfs containers below may be created/removed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from time import monotonic, sleep
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / 'apps/backend'
sys.path.insert(0, str(BACKEND))
from db_recovery_bundle import captured, digest_file, keyring, pack_bundle, restore_bundle, verify_restored

HOST = os.environ.get('RESUME_QA_SSH_TARGET', 'resume-qa.invalid')
SOURCE = 'resume-db-review-test'
TARGET = 'resume-db-review-restore'
LABEL = 'resume-encrypted-recovery-review'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '--', HOST]
stage = 'initializing'
diagnostic_markers = []


def remote(code, payload=None, timeout=60):
    if payload is not None:
        # Fixed Python wrapper receives data through stdin, never argv.
        code = 'import base64;exec(compile(base64.b64decode(' + repr(__import__('base64').b64encode(code.encode()).decode()) + "),'synthetic-remote','exec'))"
        args = SSH + ['sudo -n python3 -c ' + __import__('shlex').quote(code)]
    else:
        args, payload = SSH + ['sudo -n python3 -'], code.encode()
    result = subprocess.run(args, input=payload, capture_output=True, timeout=timeout)
    if result.returncode:
        try:
            safe = json.loads(result.stdout)
            allowed = {'schema_exists', 'permission_denied', 'authentication_failed', 'bad_archive', 'owner_required'}
            diagnostic_markers.extend(x for x in safe.get('safe_errors', []) if x in allowed)
        except (ValueError, TypeError, AttributeError):
            pass
        raise RuntimeError('Captured synthetic remote operation failed')
    return result.stdout


def docker(name, args, payload=None, timeout=60):
    if name not in {SOURCE, TARGET}:
        raise ValueError('Invalid disposable container identity')
    code = '''import json,subprocess,sys
name,arguments=''' + repr((name, args)) + '''
x=json.loads(subprocess.run(['docker','inspect',name],capture_output=True,text=True,check=True).stdout)[0]
if x['Config'].get('Labels',{}).get('purpose')!=''' + repr(LABEL) + ''':raise SystemExit(2)
if any(m['Type'] in ('volume','bind') for m in x['Mounts']):raise SystemExit(2)
r=subprocess.run(['docker','exec','-i',name,*arguments],input=sys.stdin.buffer.read(),capture_output=True)
if r.returncode:
 stderr=r.stderr.decode(errors='replace').lower()
 markers={'schema_exists':'schema "public" already exists','permission_denied':'permission denied',
          'authentication_failed':'password authentication failed','bad_archive':'does not appear to be a valid archive',
          'owner_required':'must be owner'}
 print(json.dumps({'safe_errors':[label for label,needle in markers.items() if needle in stderr]}))
 raise SystemExit(2)
sys.stdout.buffer.write(r.stdout)
'''
    return remote(code, payload=payload or b'', timeout=timeout)


def sql(name, database, statement):
    return docker(name, ['psql', '-X', '-U', 'review_bootstrap', '-d', database,
                         '-v', 'ON_ERROR_STOP=1', '-At'], payload=statement.encode()).decode().strip()


def provision(name, database, port, created):
    if (name, database, port) not in {(SOURCE, 'review_test', 15439), (TARGET, 'review_restore', 15440)}:
        raise ValueError('Only the fixed synthetic PostgreSQL containers are allowed')
    code = '''import json,os,subprocess,tempfile
name,database,port=''' + repr((name, database, port)) + '''
if subprocess.run(['docker','inspect',name],capture_output=True).returncode==0:raise SystemExit(2)
with tempfile.TemporaryDirectory(prefix='synthetic-recovery-env-') as folder:
 p=os.path.join(folder,'postgres.env')
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as out:out.write('POSTGRES_USER=review_bootstrap\\nPOSTGRES_DB='+database+'\\nPOSTGRES_PASSWORD=synthetic-local-review-only\\nPOSTGRES_INITDB_ARGS=--auth-host=scram-sha-256\\n')
 r=subprocess.run(['docker','run','-d','--name',name,'--label','purpose=''' + LABEL + '''',
  '--memory=192m','--cpus=0.5','--pids-limit=128','--tmpfs','/var/lib/postgresql/data:rw,size=128m',
  '--env-file',p,'-p','127.0.0.1:'+str(port)+':5432','postgres:16-alpine',
  '-c','shared_buffers=16MB','-c','max_connections=15'],capture_output=True)
if r.returncode:raise SystemExit(2)
print(json.dumps({'created':True}))
'''
    remote(code)
    created.append(name)
    deadline = monotonic() + 45
    while True:
        try:
            if sql(name, database, 'SELECT 1') == '1': return
        except Exception:
            if monotonic() >= deadline: raise
            sleep(1)


def remove(name):
    if name not in {SOURCE, TARGET}:
        raise ValueError('Only fixed disposable containers may be removed')
    code = '''import json,subprocess
name=''' + repr(name) + '''
r=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
if r.returncode:raise SystemExit(2)
x=json.loads(r.stdout)[0]
if x['Config'].get('Labels',{}).get('purpose')!=''' + repr(LABEL) + ''':raise SystemExit(2)
if any(m['Type'] in ('bind','volume') for m in x['Mounts']):raise SystemExit(2)
subprocess.run(['docker','rm','-f',name],capture_output=True,check=True)
'''
    remote(code)


def init_roles(name, database, script, *, grants=False):
    if not grants:
        sql(name, database, 'CREATE ROLE resume_bootstrap_admin LOGIN SUPERUSER;')
    prefix = ('export POSTGRES_USER=resume_bootstrap_admin POSTGRES_DB=' + database + '\n'
              'export MIGRATOR_PASSWORD=review-migrator-only APP_PASSWORD=review-app-only '
              'READONLY_PASSWORD=review-reader-only\n').encode()
    docker(name, ['sh', '-s', 'grants' if grants else 'init'], payload=prefix + script)


def available_port(port):
    with socket.socket() as sock: sock.bind(('127.0.0.1', port))


def subprocess_env(**values):
    # Inherit executable lookup/OS settings but remove any database/provider
    # settings from the caller. Actual recovered runtime is in a copied folder.
    result = {k: v for k, v in os.environ.items()
              if not k.startswith(('DATABASE_', 'MIGRATION_', 'LLM_', 'SMTP_', 'SOURCE_DRAIN_', 'JWT_', 'SESSION_'))
              and k not in {'PYTHONPATH', 'PYTHONHOME'}}
    result.update(values)
    result['PYTHONDONTWRITEBYTECODE']='1'
    return result


def local_python(args, *, cwd, env):
    result = subprocess.run([sys.executable, *args], cwd=cwd, env=env, capture_output=True, timeout=90)
    if result.returncode: raise RuntimeError('Captured synthetic Python operation failed')
    return result.stdout


def probe_app(runtime, env, *, preview, resume_id, job_id):
    ready = runtime.parent / ('preview-ready.json' if preview else 'legacy-ready.json')
    code = '''import json,os
from pathlib import Path
from werkzeug.serving import make_server
'''
    if preview:
        code += '''from db_auth import AuthService
from db_http_preview import create_preview_app
from db_runtime import build_runtime,verify_runtime
from db_store import Store
engine,sessions=build_runtime(os.environ['DATABASE_URL']);verify_runtime(engine)
app=create_preview_app(Store(session_factory=sessions),AuthService(sessions),os.environ['JWT_SECRET_KEY'])
'''
    else:
        code += 'from app import app\n'
    code += 'server=make_server("127.0.0.1",0,app)\nPath(' + repr(str(ready)) + ').write_text(json.dumps({"port":server.server_port}))\nserver.serve_forever()\n'
    startup_log = tempfile.TemporaryFile()
    process = subprocess.Popen([sys.executable, '-c', code], cwd=runtime, env=env,
                               stdout=subprocess.DEVNULL, stderr=startup_log)
    try:
        deadline = monotonic() + 30
        while not ready.exists():
            if process.poll() is not None or monotonic() >= deadline:
                startup_log.seek(0)
                diagnostics=startup_log.read().decode(errors='replace')
                for module in re.findall(r"No module named '([A-Za-z_][A-Za-z_0-9.]*)'",diagnostics):
                    diagnostic_markers.append('missing_python_module:'+module)
                for marker in ('FileNotFoundError','ImportError','SyntaxError','PermissionError'):
                    if marker in diagnostics:diagnostic_markers.append(marker)
                raise RuntimeError('Restored application did not start')
            sleep(0.1)
        port = json.loads(ready.read_text())['port']
        def request(method, path, data=None, headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
            try:
                body = json.dumps(data) if data is not None else None
                connection.request(method, path, body, headers={'Content-Type': 'application/json', **(headers or {})})
                response = connection.getresponse(); payload = response.read()
                return response.status, json.loads(payload)
            finally: connection.close()
        assert request('GET', '/ping')[0] == 200
        assert request('GET', '/api/v1/auth/me')[0] == 401
        status, payload = request('POST', '/api/v1/auth/login', {'username':'recovery_owner','password':'SyntheticPassword9'})
        assert status == 200
        token = payload['data']['token']
        headers = {'Authorization':'Bearer '+token}
        assert request('GET', '/api/v1/auth/me', headers=headers)[0] == 200
        assert request('GET', '/api/v1/resumes?resume_id=' + resume_id, headers=headers)[0] == 200
        assert request('GET', '/api/v1/jobs?job_id=' + job_id, headers=headers)[0] == 200
        return {'http_startup': True, 'ping': 200, 'unauthenticated_me':401,
                'login':200, 'authenticated_me':200, 'resume_read':200, 'job_read':200}
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        startup_log.close()


def main():
    global stage
    if not __debug__:
        raise SystemExit('Synthetic acceptance checks must not run with Python optimization enabled')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-synthetic', action='store_true', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args=parser.parse_args()
    if args.evidence.exists(): raise SystemExit('Evidence destination already exists')
    gpg=shutil.which('gpg') or r'C:\Program Files\Git\usr\bin\gpg.exe'
    created=[]; tunnel=None; source_engine=None; timings={}; result=None
    try:
        with tempfile.TemporaryDirectory(prefix='rr-') as folder:
            root=Path(folder)
            scratch=root/'scratch';scratch.mkdir(mode=0o700)
            keys=root/'independent-keys';keys.mkdir(mode=0o700)
            work=root/'input';work.mkdir(mode=0o700)
            for part in ('database','source','originals','roles','runtime','secrets'):
                (work/part).mkdir(mode=0o700)
            stage='synthetic-key-generation'
            password=b'Synthetic-Independent-Recovery-Key9'
            with keyring(gpg,scratch) as base:
                captured(base+['--pinentry-mode','loopback','--passphrase-fd','0','--quick-generate-key',
                    'Synthetic Full Recovery <recovery@example.test>','rsa2048','encr','1d'],payload=password+b'\n')
                listed=captured(base+['--with-colons','--list-keys']).decode()
                recipient=next(line.split(':')[9] for line in listed.splitlines() if line.startswith('fpr:'))
                public=keys/'public.asc';private=keys/'private.asc'
                public.write_bytes(captured(base+['--armor','--export',recipient]))
                private.write_bytes(captured(base+['--pinentry-mode','loopback','--passphrase-fd','0',
                    '--armor','--export-secret-keys',recipient],payload=password+b'\n'))
                private.chmod(0o600)
            stage='provision-disposable-source'
            available_port(15439);available_port(15440)
            provision(SOURCE,'review_test',15439,created)
            tunnel=subprocess.Popen(['ssh','-N','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes',
                '-o','ConnectTimeout=10','-L','127.0.0.1:15439:127.0.0.1:15439',
                '-L','127.0.0.1:15440:127.0.0.1:15440','--',HOST],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            deadline=monotonic()+15
            while True:
                try:
                    with socket.create_connection(('127.0.0.1',15439),timeout=1):break
                except OSError:
                    if tunnel.poll() is not None or monotonic()>=deadline:raise RuntimeError('Synthetic tunnel unavailable')
                    sleep(0.2)
            roles=(REPO/'scripts/database-init.sh').read_bytes()
            init_roles(SOURCE,'review_test',roles)
            (work/'roles/database-init.sh').write_bytes(roles)
            for path in BACKEND.glob('*.py'):
                if not path.name.startswith('test_'):shutil.copyfile(path,work/'runtime'/path.name)
            for name in ('requirements.txt','requirements-database.txt','alembic.ini'):
                shutil.copyfile(BACKEND/name,work/'runtime'/name)
            shutil.copyfile(BACKEND/'school_tiers.json',work/'runtime/school_tiers.json')
            shutil.copytree(BACKEND/'tools',work/'runtime/tools',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            shutil.copytree(BACKEND/'migrations',work/'runtime/migrations',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            (work/'runtime/recovery-start.json').write_text(json.dumps({'scope':'synthetic-backend-json-and-db-preview','schema':'0010_task_inputs'}))
            from cryptography.fernet import Fernet
            config={'DATABASE_CONFIG_KEY':Fernet.generate_key().decode(),'DATABASE_CONFIG_KEY_ID':'migration-v1',
                'MIGRATION_ADMIN_EMAILS':'owner@example.test','MIGRATION_AUTO_UNFREEZE_HOURS':'0',
                'JWT_SECRET_KEY':'synthetic-restored-jwt-at-least-48-characters-for-testing',
                'SESSION_SECRET_KEY':'synthetic-restored-session-at-least-48-characters-for-testing',
                'ENV':'local','LLM_API_KEY':'','SMTP_PASSWORD':'','SMTP_ENABLED':'false','CAPTCHA_ENABLED':'off'}
            (work/'secrets/application.json').write_text(json.dumps(config),encoding='utf-8')
            source_url='postgresql+psycopg://resume_migrator:review-migrator-only@127.0.0.1:15439/review_test'
            target_url='postgresql+psycopg://resume_migrator:review-migrator-only@127.0.0.1:15440/review_restore'
            env=subprocess_env(**config,DATABASE_URL=source_url)
            stage='migrate-and-import-synthetic-source'
            local_python(['-m','alembic','upgrade','head'],cwd=BACKEND,env=env)
            init_roles(SOURCE,'review_test',roles,grants=True)
            uid,rid,jid,aid=[str(uuid4()) for _ in range(4)]
            from db_auth import hash_password
            hashed,salt=hash_password('SyntheticPassword9')
            original=b'%PDF-1.4\nSynthetic original; not a real resume.\n%%EOF\n'
            now=datetime.now(timezone.utc).isoformat()
            rows={
                'users':{'user_id':uid,'username':'recovery_owner','email':'owner@example.test',
                    'password_hash':hashed,'password_salt':salt,'password_version':1,'created_at':now},
                'resumes':{'resume_id':rid,'user_id':uid,'content':'Synthetic recovery resume','processed':{},
                    'original':{'ext':'pdf','name':'synthetic.pdf','bytes':len(original),'sha256':hashlib.sha256(original).hexdigest(),
                        'storage_key':'resume_originals/'+rid+'.pdf'},'created_at':now},
                'jobs':{'job_id':jid,'user_id':uid,'content':'Synthetic recovery job','processed':{},'created_at':now},
                'archives':{'archiveId':aid,'user_id':uid,'resume_id':rid,'job_id':jid,'created_at':now},
            }
            for name,row in rows.items():
                identity=row.get('user_id') if name=='users' else row.get('resume_id') if name=='resumes' else row.get('job_id') if name=='jobs' else row['archiveId']
                (work/'source'/name).mkdir()
                (work/'source'/name/(identity+'.json')).write_text(json.dumps(row),encoding='utf-8')
            (work/'source/resume_originals').mkdir();(work/'source/resume_originals'/f'{rid}.pdf').write_bytes(original)
            (work/'source/system_config').mkdir()
            (work/'source/system_config/llm.json').write_text(json.dumps({'api_key':'synthetic-provider-never-called','model':'synthetic'}))
            from db_migrate_json import inventory,apply_import
            from db_security import migration_engine
            from sqlalchemy import text
            from sqlalchemy.orm import Session
            from db_import_attestation import target_attestation
            source_engine=migration_engine(source_url)
            report=inventory(work/'source')
            os.environ.update({k:v for k,v in config.items() if k.startswith(('DATABASE_','MIGRATION_'))})
            apply_import(report,source_url)
            from db_originals import copy_originals,original_manifest
            copy_originals(work/'source',work/'originals')
            with Session(source_engine) as session,session.begin():
                expected_attestation=target_attestation(session)
            recovery_point=datetime.now(timezone.utc).isoformat()
            stage='database-dump-and-encrypt'
            (work/'database/database.dump').write_bytes(docker(SOURCE,['pg_dump','-U','review_bootstrap','-d','review_test',
                '--schema=public','--no-owner','--no-acl','-Fc']))
            backup=root/'backup-store';backup.mkdir(mode=0o700)
            packed=pack_bundle(work,backup/'full.tar.gpg',gpg=gpg,public_key=public,recipient=recipient,
                               scratch=scratch,point=recovery_point)
            # Remove the source container and source files before restoration;
            # no original key/config/source can accidentally satisfy a probe.
            source_engine.dispose();source_engine=None
            remove(SOURCE);created.remove(SOURCE)
            shutil.rmtree(work)
            del config,env,report,roles,hashed,salt
            stage='acquire-encrypted-backup'
            recovery_started=monotonic();started=monotonic()
            acquisition=root/'recovery-download';acquisition.mkdir(mode=0o700)
            acquired=acquisition/'full.tar.gpg';shutil.copyfile(backup/'full.tar.gpg',acquired)
            assert digest_file(acquired)==packed['ciphertext_sha256']
            timings['acquisition_seconds']=round(monotonic()-started,3)
            stage='recover-key-and-decrypt'
            started=monotonic();restored=root/'recovered'
            recovered=restore_bundle(acquired,restored,expected_sha256=packed['ciphertext_sha256'],
                gpg=gpg,private_key=private,recipient=recipient,passphrase=password,scratch=scratch)
            verify_restored(restored,packed['ciphertext_sha256'],packed['manifest_sha256'])
            timings['key_import_decrypt_extract_seconds']=round(monotonic()-started,3)
            secrets=json.loads((restored/'secrets/application.json').read_text())
            stage='restore-database-as-migrator'
            started=monotonic()
            provision(TARGET,'review_restore',15440,created)
            init_roles(TARGET,'review_restore',(restored/'roles/database-init.sh').read_bytes())
            assert sql(TARGET,'review_restore',"SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")=='0'
            dump=(restored/'database/database.dump').read_bytes()
            toc=docker(TARGET,['pg_restore','--list'],payload=dump).decode()
            # Role initialization already created public and its ACLs. Migrator
            # owns restored business objects, not database/bootstrap namespaces.
            # Exclude only those two namespace entries; retain every data item.
            filtered=[];skipped_schema_entries=0
            for line in toc.splitlines():
                if re.match(r'^\d+; \d+ \d+ (SCHEMA - public |COMMENT - SCHEMA public )',line):
                    line=';'+line;skipped_schema_entries+=1
                filtered.append(line)
            assert 1<=skipped_schema_entries<=2
            docker(TARGET,['sh','-c','umask 077; cat > /tmp/recovery-toc.list'],
                   payload=('\n'.join(filtered)+'\n').encode())
            docker(TARGET,['sh','-c','IFS= read -r PGPASSWORD; export PGPASSWORD; exec pg_restore -h 127.0.0.1 '
                '-U resume_migrator -d review_restore --no-owner --no-acl --use-list /tmp/recovery-toc.list --exit-on-error'],
                payload=b'review-migrator-only\n'+dump)
            init_roles(TARGET,'review_restore',(restored/'roles/database-init.sh').read_bytes(),grants=True)
            timings['database_restore_and_grants_seconds']=round(monotonic()-started,3)
            stage='independent-reconciliation-and-originals'
            started=monotonic()
            target_engine=migration_engine(target_url)
            try:
                from alembic.autogenerate import compare_metadata
                from alembic.migration import MigrationContext
                from db_check_schema import metadata
                with target_engine.connect() as connection:
                    schema_differences=compare_metadata(MigrationContext.configure(connection,opts={'compare_type':True}),metadata())
                    assert not schema_differences
                with Session(target_engine) as session,session.begin():
                    session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                    assert target_attestation(session)==expected_attestation
                    assert session.scalar(text('SELECT version_num FROM alembic_version'))=='0010_task_inputs'
                    assert not session.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner<>'resume_migrator'"))
                recovered_env=subprocess_env(**secrets,DATABASE_RECONCILE_URL=target_url,DATABASE_URL=target_url)
                reconciliation=json.loads(local_python(['-m','db_reconcile',str(restored/'source')],cwd=restored/'runtime',env=recovered_env))
                assert reconciliation['mismatch_count']==0
                from db_originals import verify_original_copy
                originals=verify_original_copy(restored/'originals',original_manifest(restored/'source'))
                # Restored configuration key actually decrypts the database.
                from db_secrets import ConfigCipher,EncryptedConfigRepository
                with Session(target_engine) as session,session.begin():
                    cipher=ConfigCipher(secrets['DATABASE_CONFIG_KEY'],key_id=secrets['DATABASE_CONFIG_KEY_ID'])
                    assert EncryptedConfigRepository(session,cipher).get() is not None
                app_url=target_url.replace('resume_migrator:review-migrator-only','resume_app:review-app-only')
                from db_runtime import build_runtime,verify_runtime
                app_engine,_=build_runtime(app_url)
                try:
                    assert verify_runtime(app_engine)
                    with app_engine.connect() as connection:
                        assert not connection.scalar(text("SELECT has_table_privilege(current_user,'legacy_records','SELECT')"))
                        assert not connection.scalar(text("SELECT has_table_privilege(current_user,'audit_logs','DELETE')"))
                finally:app_engine.dispose()
                reader=migration_engine(target_url.replace('resume_migrator:review-migrator-only','resume_readonly:review-reader-only'))
                try:
                    with reader.connect() as connection:
                        assert connection.scalar(text('SELECT count(*) FROM reporting.user_status'))==1
                        from sqlalchemy.exc import DBAPIError
                        try:connection.execute(text('SELECT * FROM public.users'))
                        except DBAPIError as exc:
                            assert getattr(exc.orig,'sqlstate',None)=='42501'
                        else:raise AssertionError('Reader unexpectedly accessed user data')
                finally:reader.dispose()
            finally:target_engine.dispose()
            timings['reconciliation_originals_and_roles_seconds']=round(monotonic()-started,3)
            stage='start-restored-applications'
            started=monotonic()
            # Each process starts from code restored from the encrypted bundle;
            # legacy JSON startup gets a separate working copy, preserving proof.
            runtime=root/'running-backend';shutil.copytree(restored/'runtime',runtime)
            shutil.copytree(restored/'source',runtime/'data')
            env=subprocess_env(**secrets,DATABASE_URL=app_url,LOG_DIR=str(root/'logs'))
            stage='start-restored-json-backend'
            legacy=probe_app(runtime,env,preview=False,resume_id=rid,job_id=jid)
            stage='start-restored-database-preview'
            preview=probe_app(runtime,env,preview=True,resume_id=rid,job_id=jid)
            timings['application_startup_and_http_seconds']=round(monotonic()-started,3)
            timings['synthetic_end_to_end_recovery_seconds']=round(monotonic()-recovery_started,3)
            stage='verify-recovered-files-after-application-probes'
            verify_restored(restored,packed['ciphertext_sha256'],packed['manifest_sha256'])
            result={'observed_at_utc':datetime.now(timezone.utc).isoformat(),'scope':'synthetic-local-encryption-and-backend-recovery',
                'passed':True,'production_acceptance':False,'encrypted_offsite_recovery_acceptance':False,
                'source_destroyed_before_restore':True,'source_and_restore_postgres_containers_separate':True,
                'no_production_business_data_read':True,'independent_synthetic_private_key_import':True,
                'package':packed,'restored':recovered,'independent_reconciliation':reconciliation,
                'schema_differences':0,
                'originals':originals,'database_owner_and_runtime_permissions_verified':True,
                'restored_config_key_decrypted_database':True,'legacy_json_backend':legacy,'database_preview_backend':preview,
                'timings':timings,'production_rpo_seconds':None,'production_rto_seconds':None,
                'limitations':['Acquisition was a local ciphertext file copy, not an offsite transfer.',
                    'GPG recovery key was synthetic and kept separately in temporary local storage; user custody is not configured.',
                    'PostgreSQL used two disposable remote development containers via loopback SSH tunnels.',
                    'Python dependencies reused the installed local runtime; frontend, DNS, TLS and dependency/image acquisition were not restored.',
                    'Application probes cover backend startup, login, authorization and resume/job reads; SQL business adapters remain incomplete.']}
    except Exception as exc:
        sqlstate=getattr(getattr(exc,'orig',None),'sqlstate',None)
        sqlstate=sqlstate if isinstance(sqlstate,str) and re.fullmatch('[0-9A-Z]{5}',sqlstate) else None
        print(json.dumps({'passed':False,'stage':stage,'error_type':type(exc).__name__,'raw_output_suppressed':True,
                          'sqlstate':sqlstate,'safe_diagnostic_markers':diagnostic_markers,'production_acceptance':False}));return 2
    finally:
        if source_engine is not None:source_engine.dispose()
        if tunnel is not None:
            tunnel.terminate()
            try:tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:tunnel.kill();tunnel.wait(timeout=5)
        for name in reversed(created):remove(name)
    args.evidence.parent.mkdir(parents=True,exist_ok=True)
    with args.evidence.open('x',encoding='utf-8') as handle:json.dump(result,handle,indent=2,sort_keys=True);handle.write('\n')
    print(json.dumps({'passed':True,'production_acceptance':False,'checked_fields':result['independent_reconciliation']['checked_fields'],
                      'mismatch_count':0,'verified_originals':result['originals']['verified_originals'],'timings':timings}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
