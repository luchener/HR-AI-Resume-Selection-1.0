"""Server root only: import the certified frozen JSON into a NEW sandbox DB.

Never changes the production JSON adapter or upgrades the previous rehearsal.
All credentials, originals and business data remain on the server. Prints only
counts and fixed checks. Existing databases and output directories are refused.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from urllib.parse import urlsplit, urlunsplit

STAGING = Path(os.environ.get('RESUME_DB_STAGING_DIR', Path.home() / '.resume-workbench' / 'staging'))
STATE=STAGING/'drain-20261016'
ASSETS = Path(os.environ.get('RESUME_DRAIN_ASSET_DIR', Path.home() / '.cache' / 'resume-production-drain'))
DATABASE='resume_drain_20261016'
IMAGE='resume-db-migration:drain-0010'
CONTAINER='resume-db-staging'
stage='initializing'


def run(args, *, payload=None, timeout=120):
    result=subprocess.run(args,input=payload,capture_output=True,text=True,encoding='utf-8',timeout=timeout)
    if result.returncode:raise RuntimeError('Captured operation failed')
    return result.stdout


def env_file(path):
    result={}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        name,value=line.split('=',1)
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*',name):raise ValueError('Unexpected environment key')
        parsed=shlex.split(value,comments=True)
        if len(parsed)!=1:raise ValueError('Unexpected environment value')
        result[name]=parsed[0]
    return result


def save(name,value):
    path=STATE/(name+'.json')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as handle:
        json.dump(value,handle,indent=2,sort_keys=True);handle.flush();os.fsync(handle.fileno())


def sql(sql_text, *, database='postgres'):
    password=env_file(STAGING/'bootstrap.env')['POSTGRES_PASSWORD']
    if '\n' in password or '\r' in password:raise ValueError('Invalid password representation')
    # Password is stdin to a captured subprocess; never a command argument.
    return run(['docker','exec','-i',CONTAINER,'sh','-c',
        'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -X -h 127.0.0.1 -U resume_bootstrap_admin -d "$1" -v ON_ERROR_STOP=1 -At',
        'private-psql',database],payload=password+'\n'+sql_text)


def tool(*args, mounts=(), payload=None, timeout=120):
    command=['docker','run','--rm','-i','--network=container:'+CONTAINER,'--read-only','--memory=256m','--memory-swap=256m',
             '--cpus=0.5','--cap-drop=ALL','--security-opt=no-new-privileges',
             '--env-file',str(STATE/'rehearsal.env'),'--mount',
             'type=bind,source='+str(STATE/'frozen/snapshot')+',target=/snapshot,readonly']
    for mount in mounts:command+=['--mount',mount]
    command+=['--entrypoint','python',IMAGE,*args]
    return run(command,payload=json.dumps(payload) if payload is not None else '',timeout=timeout)


def objects(output):
    decoder=json.JSONDecoder();cursor=0;result=[]
    while True:
        start=output.find('{',cursor)
        if start<0:return result
        try:
            value,end=decoder.raw_decode(output[start:]);result.append(value);cursor=start+end
        except ValueError:cursor=start+1


def main():
    global stage
    if os.geteuid()!=0:raise SystemExit('Run on the reviewed server with administrative privileges')
    with (STATE/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            stage='verify-frozen-source'
            if (STATE/'rehearsal.json').exists():raise ValueError('Rehearsal already certified')
            certificate=json.loads((STATE/'snapshot.json').read_text())
            if certificate.get('production_frozen_snapshot_acceptance') is not True:raise ValueError('Uncertified snapshot')
            trusted=json.loads((STATE/'frozen/source-manifest.json').read_text())
            observed={}
            for p in sorted((STATE/'frozen/snapshot').rglob('*')):
                if p.is_symlink():raise ValueError('Unexpected snapshot symlink')
                if p.is_file():observed[p.relative_to(STATE/'frozen/snapshot').as_posix()]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            digest=hashlib.sha256(json.dumps(observed,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if observed!=trusted or digest!=certificate['snapshot']['manifest_sha256']:raise ValueError('Snapshot changed')
            stage='prepare-isolated-environment'
            environment=env_file(STAGING/'migration.env')
            url=urlsplit(environment['DATABASE_MIGRATION_URL'])
            if url.username!='resume_migrator' or url.query or url.fragment or url.scheme not in ('postgresql','postgresql+psycopg'):
                raise ValueError('Unexpected migration target')
            replacement=urlunsplit((url.scheme,url.netloc,'/'+DATABASE,'',''))
            environment.update(DATABASE_MIGRATION_URL=replacement,DATABASE_URL=replacement,DATABASE_RECONCILE_URL=replacement)
            path=STATE/'rehearsal.env'
            if path.exists():
                if env_file(path)!=environment:raise ValueError('Existing rehearsal environment changed')
            else:
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as handle:
                    for name,value in environment.items():
                        if '\r' in value or '\n' in value:raise ValueError('Invalid environment representation')
                        handle.write(name+'='+value+'\n')
            stage='create-empty-database'
            exists=sql("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname='"+DATABASE+"');\n").strip()
            marker={'database':DATABASE,'snapshot_manifest_sha256':digest,'created_by':'database-frozen-source-rehearsal'}
            if exists=='f':
                sql('CREATE DATABASE '+DATABASE+' OWNER resume_migrator;\nREVOKE ALL ON DATABASE '+DATABASE+' FROM PUBLIC;\nGRANT CONNECT ON DATABASE '+DATABASE+' TO resume_migrator,resume_app,resume_readonly;\n')
                sql('REVOKE ALL ON SCHEMA public FROM PUBLIC;\nGRANT USAGE,CREATE ON SCHEMA public TO resume_migrator;\nCREATE SCHEMA reporting AUTHORIZATION resume_bootstrap_admin;\nREVOKE ALL ON SCHEMA reporting FROM PUBLIC;\nGRANT USAGE ON SCHEMA reporting TO resume_readonly;\n',database=DATABASE)
                save('target-created',marker)
            elif exists!='t' or json.loads((STATE/'target-created.json').read_text())!=marker:
                raise ValueError('Unrecognized destination; refusing overwrite')
            stage='migrate-new-database'
            tool('-m','alembic','upgrade','head')
            stage='verify-schema'
            schema=tool('-c',"import os,json;from sqlalchemy import text;from alembic.autogenerate import compare_metadata;from alembic.migration import MigrationContext;from db_security import migration_engine;from db_check_schema import metadata;e=migration_engine(os.environ['DATABASE_URL']);c=e.connect();diff=compare_metadata(MigrationContext.configure(c,opts={'compare_type':True}),metadata());version=c.scalar(text('select version_num from alembic_version'));print(json.dumps({'schema_differences':len(diff),'version':version}));c.close();e.dispose()")
            schema=json.loads(schema)
            if schema['schema_differences']!=0 or not schema['version'].startswith('0010_'):raise ValueError('Schema mismatch')
            stage='apply-reviewed-grants'
            grants=re.findall(r"<<'SQL'\n(.*?)\nSQL",(ASSETS/'database-init.sh').read_text(),re.S)
            if len(grants)!=2:raise ValueError('Unexpected grants script structure')
            sql(grants[1]+'\n',database=DATABASE)
            stage='offline-source-preflight'
            tool('-m','db_migrate_json','/snapshot')
            stage='import-frozen-source'
            imported=objects(tool('-m','db_migrate_json','/snapshot','--apply'))[-1]['imported']
            stage='replay-frozen-source'
            replayed=objects(tool('-m','db_migrate_json','/snapshot','--apply'))[-1]['imported']
            if replayed.get('unchanged_snapshot')!=1:raise ValueError('Replay was not unchanged')
            stage='independent-field-reconciliation'
            reconciled=json.loads(tool('-m','db_reconcile','/snapshot'))
            if reconciled['mismatch_count']!=0:raise ValueError('Independent reconciliation failed')
            stage='verify-originals-independently'
            destination=STATE/'originals';destination.mkdir(mode=0o700);os.chown(destination,10001,10001)
            original_result=json.loads(tool('-c',"import json;from pathlib import Path;from db_originals import original_manifest,copy_originals,verify_original_copy;trusted=original_manifest(Path('/snapshot'));copied=copy_originals(Path('/snapshot'),Path('/originals'));verified=verify_original_copy(Path('/originals'),trusted);print(json.dumps({'verified_originals':verified['verified_originals'],'bytes':sum(x['bytes'] for x in trusted)}))",
                mounts=('type=bind,source='+str(destination)+',target=/originals',)))
            summary={'at':datetime.now(timezone.utc).isoformat(),'target_database':DATABASE,
                'snapshot_manifest_sha256':digest,'snapshot_files':len(observed),'schema':schema,'imported':imported,
                'replay_unchanged':True,'independent_reconciliation':reconciled,'originals':original_result,
                'existing_staging_database_upgraded':False,'production_json_authoritative':True,
                'production_database_cutover':False,'encrypted_offsite_recovery_acceptance':False,
                'business_adapter_acceptance':False}
            save('rehearsal',summary)
            print(json.dumps(summary));return 0
        except Exception as exc:
            failure={'passed':False,'stage':stage,'error_type':type(exc).__name__,'raw_output_suppressed':True,
                     'production_database_cutover':False}
            print(json.dumps(failure));return 2


if __name__=='__main__':
    raise SystemExit(main())
