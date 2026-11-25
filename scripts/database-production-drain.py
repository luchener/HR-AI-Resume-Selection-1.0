"""Linux root only: controlled JSON drain rollout and frozen source acceptance.

Only fixed, reviewed production identities are accepted. Provider secrets and
business data stay on the server; command output is captured, never displayed.
Each action saves safe counters/statuses. No database cutover or dual writes.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import time

REPO = Path(os.environ.get('RESUME_PROJECT_DIR', Path(__file__).resolve().parents[1]))
STATE = Path(os.environ.get('RESUME_DB_STAGING_DIR', Path.home() / '.resume-workbench' / 'staging')) / 'drain-20261016'
ASSETS = Path(os.environ.get('RESUME_DRAIN_ASSET_DIR', Path.home() / '.cache' / 'resume-production-drain'))
BACKEND = 'hr-ai-resume-selection-backend'
FRONTEND = 'hr-ai-resume-selection-frontend'
DATA = 'hr-ai-resume-selection_backend-data'
LOGS = 'hr-ai-resume-selection_backend-logs'
IMAGE = 'hr-ai-resume-selection-backend:source-drain-20261016'
BASE_TAG = 'resume-source-drain-base:20261016'
MIGRATION_IMAGE = 'resume-db-migration:drain-0010'
SITE = Path(os.environ.get('RESUME_NGINX_SITE', '/etc/nginx/sites-enabled/resume-workbench.conf'))
PUBLIC_HOST = os.environ.get('RESUME_PUBLIC_HOST', 'workbench.example.invalid')
FLAG = Path('/run/resume-source-maintenance')
COMPOSE = REPO/'docker-compose.secure.yml'
REGISTRY = '/app/logs/source-drain/control.sqlite'
SOURCE_FILES = ('app.py','auth.py','config.py','retention.py','admin_owner_cli.py',
                'auth_admin_cli.py','invite_cli.py','reset_password_cli.py',
                'source_drain.py','gunicorn_source_drain.py')
stage = 'initializing'


class OperationFailed(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, data, *, mode=0o600, uid=0, gid=0):
    if isinstance(data, (dict,list)):
        data = json.dumps(data,indent=2,sort_keys=True).encode()
    if isinstance(data,str):data=data.encode()
    temporary=path.with_name('.'+path.name+'.new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        os.chown(temporary,uid,gid)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def run(args, *, payload=None, timeout=90, check=True):
    result=subprocess.run(args,input=payload,capture_output=True,text=True,
                          encoding='utf-8',timeout=timeout)
    if check and result.returncode:
        raise OperationFailed('Captured command failed')
    return result


def inspect(name=BACKEND):
    return json.loads(run(['docker','inspect',name]).stdout)[0]


def container(code, payload=None, *, timeout=90):
    result=run(['docker','exec','-i',BACKEND,'python','-c',code],
               payload=json.dumps(payload) if payload is not None else '',timeout=timeout)
    return json.loads(result.stdout)


def helper(code, *, mounts=(), payload=None, user='1000:1000', timeout=90):
    args=['docker','run','--rm','-i','--network','none','--read-only',
          '--memory=128m','--memory-swap=128m','--cpus=0.5','--cap-drop=ALL',
          '--security-opt=no-new-privileges','--user',user]
    for mount in mounts:args+=['--mount',mount]
    args+=['--entrypoint','python',IMAGE,'-c',code]
    return json.loads(run(args,payload=json.dumps(payload) if payload is not None else '',timeout=timeout).stdout)


def control(action):
    code="import source_drain,json;c=source_drain.Coordinator("+repr(REGISTRY)+");print(json.dumps(c."+action+"()))"
    return helper(code,mounts=('type=volume,source='+LOGS+',target=/app/logs',))


def report(name, data):
    write(STATE/(name+'.json'),data)


def health(timeout=90):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        x=inspect()
        if x['State']['Running'] and x['State'].get('Health',{}).get('Status')=='healthy':return True
        time.sleep(2)
    return False


def status_code(path, *, public=False):
    connection=None
    try:
        if public:
            ctx=ssl.create_default_context();ctx.check_hostname=False
            connection=http.client.HTTPSConnection('127.0.0.1',443,timeout=10,context=ctx)
        else:connection=http.client.HTTPConnection('127.0.0.1',8000,timeout=10)
        connection.request('GET',path,headers={'Host':PUBLIC_HOST,'Connection':'close',
            'User-Agent':'Mozilla/5.0 production-maintenance-verification'})
        response=connection.getresponse();response.read();return response.status
    finally:
        if connection:connection.close()


def scheduler_state():
    result=run(['systemctl','list-timers','--all','--no-pager','--output=json'])
    timers=[x['unit'] for x in json.loads(result.stdout) if re.fullmatch(r'[A-Za-z0-9@_.-]+\.timer',x.get('unit',''))]
    active=[unit for unit in ['cron.service']+timers
            if run(['systemctl','is-active',unit],check=False).stdout.strip()=='active']
    services=[x.get('activates') for x in json.loads(result.stdout) if x.get('unit') in timers]
    return {'active':active,'all_timers':timers,'timer_services':[s for s in services if s]}


def pause_schedulers():
    state=scheduler_state();report('scheduler-paused',state)
    try:
        if state['active']:run(['systemctl','stop',*state['active']],timeout=60)
        if any(run(['systemctl','is-active',unit],check=False).stdout.strip()=='active' for unit in state['active']):
            raise OperationFailed('Scheduler pause incomplete')
        if any(run(['systemctl','is-active',unit],check=False).stdout.strip() in ('active','activating') for unit in state['timer_services']):
            raise OperationFailed('A previously triggered scheduled service remains active')
    except BaseException:
        restore_schedulers(state)
        raise
    return state


def restore_schedulers(state):
    if state and state['active']:run(['systemctl','start',*state['active']],timeout=60)


def source_consumers():
    rows=[]
    ids=run(['docker','ps','-q']).stdout.split()
    if not ids:return rows
    for x in json.loads(run(['docker','inspect',*ids]).stdout):
        for mount in x['Mounts']:
            source=mount.get('Source','').rstrip('/') or ('/' if mount.get('Source')=='/' else '')
            target=data_path()
            if (mount.get('Name')==DATA or (source and
                (source==target or source.startswith(target+'/') or target.startswith(source.rstrip('/')+'/')))):
                rows.append({'container':x['Name'].lstrip('/'),'writable':mount['RW']})
    return rows


def data_path():
    return json.loads(run(['docker','volume','inspect',DATA]).stdout)[0]['Mountpoint'].rstrip('/')


def open_source_files():
    prefix=data_path()+'/'
    opened=[]
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():continue
        try:
            for fd in (process/'fd').iterdir():
                try:
                    target=os.readlink(fd)
                    if target.startswith(prefix):
                        flags=re.search(r'(?m)^flags:\s+([0-7]+)',(process/'fdinfo'/fd.name).read_text())
                        writable=bool(flags and (int(flags.group(1),8)&3))
                        opened.append({'pid':int(process.name),'writable':writable})
                except FileNotFoundError:pass
        except (FileNotFoundError,ProcessLookupError):pass
    return opened


def external_app_processes():
    rows=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            command=(p/'cmdline').read_bytes().split(b'\0')
            if any((str(REPO)+'/apps/backend/').encode() in part or
                   any(part.endswith(('/'+name).encode()) or part==name.encode()
                       for name in SOURCE_FILES if name.endswith('_cli.py')) or
                   os.fsencode(REPO / 'scripts' / 'backup.sh') in part or
                   b'/var/lib/docker/volumes/hr-ai-resume-selection_backend-data' in part
                   for part in command):
                rows.append({'pid':int(p.name),'category':'external_application_command'})
        except (FileNotFoundError,ProcessLookupError):pass
    return rows


def bootstrap_processes():
    # Host root can inspect the container's actual process FDs, including
    # detached daemon threads. No command-line arguments or data are reported.
    lines=run(['docker','top',BACKEND,'-eo','pid,ppid,comm,nlwp']).stdout.splitlines()[1:]
    rows=[]
    for line in lines:
        fields=line.split();pid,ppid=int(fields[0]),int(fields[1])
        p=Path('/proc')/str(pid)
        cmd=(p/'cmdline').read_bytes().split(b'\0')
        if not any(arg==b'gunicorn' or arg.endswith(b'/gunicorn') for arg in cmd):continue
        established=set()
        for name in ('tcp','tcp6'):
            for line in (p/'net'/name).read_text().splitlines()[1:]:
                values=line.split()
                if values[3]=='01':established.add(values[9])
        sockets=set()
        for fd in (p/'fd').iterdir():
            try:
                target=os.readlink(fd)
                match=re.fullmatch(r'socket:\[(\d+)\]',target)
                if match:sockets.add(match.group(1))
            except FileNotFoundError:pass
        rows.append({'pid':pid,'ppid':ppid,'threads':len(list((p/'task').iterdir())),
                     'established_socket_fds':len(sockets&established),
                     'wait_channel':(p/'wchan').read_text().strip()})
    parents={r['pid'] for r in rows}
    masters=[r for r in rows if r['ppid'] not in parents]
    workers=[r for r in rows if r['ppid'] in parents]
    idle=(len(masters)==1 and len(workers)==4 and all(r['threads']==1 and r['established_socket_fds']==0
          and r['wait_channel'] not in ('0','') for r in workers))
    return {'observed_at':now(),'master_count':len(masters),'worker_count':len(workers),'idle':idle,'processes':rows}


def maintenance_setup():
    text=SITE.read_text();guard='    if (-f /run/resume-source-maintenance) { return 503; }\n'
    if guard not in text:
        if '127.0.0.1:8000' not in text or '127.0.0.1:3000' not in text:
            raise OperationFailed('Unexpected Nginx site')
        patched,count=re.subn(r'(?m)^(server\s*\{\s*\n)',lambda m:m.group(1)+guard,text)
        if count!=2:raise OperationFailed('Unexpected Nginx structure')
        write(STATE/'nginx-before.conf',text)
        write(SITE,patched,mode=0o644)
        if run(['nginx','-t'],check=False).returncode:
            write(SITE,text,mode=0o644);raise OperationFailed('Nginx validation failed')
        run(['systemctl','reload','nginx'])
    FLAG.touch(mode=0o600,exist_ok=False)
    # Nginx reload replaces workers asynchronously. Require the new gate to be
    # observed before changing a scheduler, application process or source file.
    observed=[];deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        observed.append(status_code('/api/v1/auth/me',public=True))
        if len(observed)>=2 and observed[-2:]==[503,503]:break
        time.sleep(0.5)
    else:
        report('public-gate-check',{'statuses':observed,'passed':False})
        raise OperationFailed('Public admission gate not active')


def compose_up():
    run(['docker','compose','-f',str(COMPOSE),'up','-d','--no-deps','--no-build','backend'],timeout=120)
    if not health():raise OperationFailed('Backend health unavailable')


def prepare():
    global stage
    stage='prepare-validation'
    if (STATE/'prepared.json').exists():raise OperationFailed('Prepared state already exists')
    baseline=json.loads((ASSETS/'baseline.json').read_text())
    x=inspect()
    if x['Image']!=baseline['image_id'] or not x['State']['Running']:
        raise OperationFailed('Deployed image changed')
    for name,digest in baseline['source_baseline'].items():
        data=(REPO/'apps/backend'/name).read_bytes().replace(b'\r\n',b'\n')
        if hashlib.sha256(data).hexdigest()!=digest:raise OperationFailed('Server source differs')
    if source_consumers()!=[{'container':BACKEND,'writable':True}]:
        raise OperationFailed('Unexpected source consumer')
    for name,digest in json.loads((ASSETS/'manifest.json').read_text()).items():
        p=ASSETS/name
        if p.is_symlink() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:
            raise OperationFailed('Code asset verification failed')
    write(STATE/'compose-before.yml',COMPOSE.read_bytes())
    for name in SOURCE_FILES:
        p=REPO/'apps/backend'/name
        if p.exists():write(STATE/('before-'+name),p.read_bytes())
    stage='build-production-overlay'
    run(['docker','tag',x['Image'],BASE_TAG])
    run(['docker','build','--network=none','--build-arg','SOURCE_DRAIN_BASE='+BASE_TAG,
         '-f',str(ASSETS/'apps/backend/Dockerfile.source-drain'),'-t',IMAGE,str(ASSETS/'production-context')],timeout=180)
    stage='build-migration-overlay'
    run(['docker','build','--network=none','-t',MIGRATION_IMAGE,str(ASSETS/'migration-context')],timeout=180)
    migration_smoke=run(['docker','run','--rm','--network=none','--read-only','--cap-drop=ALL',
        '--entrypoint','python',MIGRATION_IMAGE,'-c',
        "import os,json;print(json.dumps({'uid':os.geteuid(),'config_readable':os.access('/app/alembic.ini',os.R_OK)}))"])
    migration_smoke=json.loads(migration_smoke.stdout)
    if migration_smoke['uid']!=10001 or not migration_smoke['config_readable']:
        raise OperationFailed('Migration runtime must be non-root with readable configuration')
    stage='production-image-smoke'
    code="import os,json;from pathlib import Path;import source_drain;p=Path('/app/logs/control.sqlite');source_drain.Coordinator.initialize(p);os.environ['SOURCE_DRAIN_STATE_PATH']=str(p);import app;print(json.dumps({'drain_middleware':isinstance(app.app.wsgi_app,source_drain.DrainMiddleware)}))"
    result=run(['docker','run','--rm','--network=none','--read-only','--memory=192m','--memory-swap=192m','--cpus=0.5',
                '--tmpfs','/app/data:uid=1000,gid=1000,mode=700','--tmpfs','/app/logs:uid=1000,gid=1000,mode=700',
                '--tmpfs','/tmp:uid=1000,gid=1000,mode=700','-e','ENV=local','--entrypoint','python',IMAGE,'-c',code])
    if not json.loads(result.stdout).get('drain_middleware'):raise OperationFailed('Image smoke failed')
    report('prepared',{'at':now(),'baseline_image':x['Image'],'production_image':IMAGE,
           'migration_image':MIGRATION_IMAGE,'source_consumers':source_consumers(),
           'production_restarted':False,'database_cutover':False})


def bootstrap():
    global stage
    if not (STATE/'prepared.json').exists() or (STATE/'bootstrapped.json').exists():
        raise OperationFailed('Invalid bootstrap phase')
    prepared=json.loads((STATE/'prepared.json').read_text())
    if inspect()['Image']!=prepared['baseline_image']:raise OperationFailed('Running baseline changed')
    if FLAG.exists():raise OperationFailed('An existing maintenance gate needs explicit recovery')
    paused=None;stopped=False;deployed=False;started=time.monotonic()
    try:
        stage='bootstrap-maintenance-gate';maintenance_setup()
        stage='bootstrap-schedulers';paused=pause_schedulers()
        stage='bootstrap-old-process-idle'
        samples=[];deadline=time.monotonic()+600;idle_since=None
        while time.monotonic()<deadline:
            sample=bootstrap_processes();samples.append(sample)
            if sample['idle']:
                idle_since=idle_since or time.monotonic()
                if time.monotonic()-idle_since>=10:break
            else:idle_since=None
            time.sleep(1)
        else:raise OperationFailed('Existing work did not become idle')
        report('bootstrap-process-observations',{'samples':samples,'registry_acceptance':False})
        if external_app_processes():raise OperationFailed('External application command remains active')
        if open_source_files():raise OperationFailed('Source files remain open')
        stage='bootstrap-stop-old-master'
        run(['docker','update','--restart=no',BACKEND])
        # PID1 is a shell in the old image; signal its sole Gunicorn child
        # explicitly, after consecutive idle observations. Never force kill.
        result=container("import os,signal,json;from pathlib import Path;children=Path('/proc/1/task/1/children').read_text().split();assert len(children)==1;os.kill(int(children[0]),signal.SIGTERM);print(json.dumps({'signal':'SIGTERM'}))")
        deadline=time.monotonic()+45
        while time.monotonic()<deadline and inspect()['State']['Running']:time.sleep(0.5)
        old=inspect()['State']
        if old['Running'] or old['ExitCode']!=0 or old['OOMKilled']:
            raise OperationFailed('Old processes did not exit cleanly')
        stopped=True
        stage='bootstrap-install-code'
        for name in SOURCE_FILES:
            write(REPO/'apps/backend'/name,(ASSETS/'production-context/apps/backend'/name).read_bytes(),mode=0o644,uid=1000,gid=1000)
        text=(STATE/'compose-before.yml').read_text()
        text,count=re.subn(r'(?m)^    image: hr-ai-resume-selection-backend:1\.0\.0\s*$',
                          '    image: '+IMAGE,text)
        if count!=1:raise OperationFailed('Unexpected Compose image')
        if '    stop_grace_period:' not in text:text=text.replace('    restart: unless-stopped\n','    restart: unless-stopped\n    stop_grace_period: 1250s\n',1)
        text=text.replace('      LOG_DIR: /app/logs\n','      LOG_DIR: /app/logs\n      SOURCE_DRAIN_STATE_PATH: '+REGISTRY+'\n',1)
        if text.count('SOURCE_DRAIN_STATE_PATH:')!=1:raise OperationFailed('Unexpected drain environment')
        write(COMPOSE,text,mode=0o644,uid=1000,gid=1000)
        # Persist the setting for future Compose builds and for host CLI imports
        # (which must fail closed unless run with the shared registry mounted).
        env_path=REPO/'apps/backend/.env';env=env_path.read_text()
        existing=re.findall(r'(?m)^SOURCE_DRAIN_STATE_PATH=(.*)$',env)
        if existing and existing!=[REGISTRY]:raise OperationFailed('Conflicting authoritative drain setting')
        if not existing:
            s=env_path.stat();write(env_path,env.rstrip('\n')+'\nSOURCE_DRAIN_STATE_PATH='+REGISTRY+'\n',uid=s.st_uid,gid=s.st_gid)
        dockerfile=REPO/'apps/backend/Dockerfile';recipe=dockerfile.read_text()
        old='CMD ["sh", "-c", "gunicorn -w ${GUNICORN_WORKERS} -b 0.0.0.0:8000 --timeout 1200 app:app"]'
        new='CMD ["sh", "-c", "exec gunicorn -c python:gunicorn_source_drain -w ${GUNICORN_WORKERS} -b 0.0.0.0:8000 --timeout 1200 --graceful-timeout 1200 app:app"]'
        if old in recipe:
            if not (STATE/'Dockerfile-before-drain').exists():write(STATE/'Dockerfile-before-drain',recipe)
            s=dockerfile.stat();write(dockerfile,recipe.replace(old,new),mode=0o644,uid=s.st_uid,gid=s.st_gid)
        elif new not in recipe:raise OperationFailed('Unexpected future build command')
        stage='bootstrap-initialize-registry'
        helper("import json;from pathlib import Path;import source_drain;p=Path("+repr(REGISTRY)+");p.parent.mkdir(mode=0o700,exist_ok=True);c=source_drain.Coordinator.initialize(p);print(json.dumps(c.status()))",
               mounts=('type=volume,source='+LOGS+',target=/app/logs',))
        stage='bootstrap-start-new-service';compose_up();deployed=True
        state=control('status')
        active=[p for p in state['processes'] if p['exited_at'] is None]
        if len(active)!=5 or sum(p['role']=='master' for p in active)!=1 or state['draining']:
            raise OperationFailed('Process registration incomplete')
        if status_code('/api/v1/auth/me')!=401:raise OperationFailed('HTTP contract failed')
        report('bootstrapped',{'at':now(),'maintenance_seconds':round(time.monotonic()-started,3),
              'old_process_idle_observed':True,'old_container_exit':{k:old[k] for k in ('ExitCode','OOMKilled','FinishedAt')},
              'old_process_registry_evidence':False,'registered_active_processes':len(active),
              'gunicorn_is_pid1':state['processes'][0]['pid']==1,'production_image':IMAGE,
              'production_database_cutover':False,'snapshot_created':False})
    finally:
        restore_schedulers(paused)
        if deployed and health(10):FLAG.unlink(missing_ok=True)
        elif not stopped:
            run(['docker','update','--restart=unless-stopped',BACKEND],check=False)
            if inspect()['State']['Running']:FLAG.unlink(missing_ok=True)
        # After a failed replacement, leave public maintenance closed. Preserve
        # all state and data for an explicit, reviewed recovery; never kill work.


COPY_SOURCE = r'''
import hashlib,json,os,shutil
from pathlib import Path
import source_drain
source=Path('/source');destination=Path('/output/snapshot');destination.mkdir(mode=0o700)
c=source_drain.Coordinator('/app/logs/source-drain/control.sqlite')
before_control=c.status()
assert before_control['admission_drained'] and all(p['exited_at'] for p in before_control['processes'])
def manifest(root):
    result={}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():raise ValueError('Source symlink')
        if p.is_file():result[p.relative_to(root).as_posix()]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    return result
before=manifest(source)
for name in before:
    target=destination/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (source/name).open('rb') as src,target.open('xb') as dst:
        os.chmod(target,0o600);shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
after=manifest(source);copied=manifest(destination);after_control=c.status()
assert before==after==copied and after_control['epoch']==before_control['epoch'] and after_control['admission_drained']
with Path('/output/source-manifest.json').open('x') as handle:json.dump(before,handle,sort_keys=True)
summary={'files':len(before),'bytes':sum(x['bytes'] for x in before.values()),
 'manifest_sha256':hashlib.sha256(json.dumps(before,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
 'source_before_after_and_copy_match':True,'admission_epoch_unchanged':True,'epoch':before_control['epoch']}
print(json.dumps(summary))
'''


def snapshot():
    global stage
    if not (STATE/'bootstrapped.json').exists() or (STATE/'snapshot.json').exists():
        raise OperationFailed('Invalid snapshot phase')
    if not inspect()['State']['Running']:raise OperationFailed('Backend is not running')
    if FLAG.exists():raise OperationFailed('An existing maintenance gate needs explicit recovery')
    paused=None;closed=False;stopped=False;started=time.monotonic();certificate=None
    try:
        stage='snapshot-maintenance-gate';maintenance_setup()
        stage='snapshot-pause-schedulers';paused=pause_schedulers()
        stage='snapshot-close-admission'
        initial=control('status');begun=control('begin');closed=True
        report('drain-initial',{'initial':initial,'begun':begun,'at':now()})
        if status_code('/api/v1/auth/me')!=503 or status_code('/ping')!=200:
            raise OperationFailed('Admission or health mismatch')
        stage='snapshot-wait-for-drain'
        deadline=time.monotonic()+1200
        while time.monotonic()<deadline:
            drained=control('status')
            report('drain-latest',drained)
            if drained['admission_drained']:break
            time.sleep(5)
        else:raise OperationFailed('Drain deadline exceeded; no work was killed')
        active=[p for p in drained['processes'] if p['exited_at'] is None]
        if len(active)!=5:raise OperationFailed('Unexpected registered process population')
        if any(x['outcome']=='failed' and x['count'] for x in drained['outcomes']):
            raise OperationFailed('Failed activity needs review')
        consumers=source_consumers()
        if consumers!=[{'container':BACKEND,'writable':True}]:raise OperationFailed('Unexpected source consumer')
        if open_source_files():raise OperationFailed('Source FD remained open')
        if external_app_processes():raise OperationFailed('External application command remains active')
        stage='snapshot-stop-registered-processes'
        run(['docker','update','--restart=no',BACKEND])
        run(['docker','stop','--time','1250',BACKEND],timeout=1260)
        stopped=True;exited=inspect()['State'];final=control('status')
        if exited['Running'] or exited['ExitCode']!=0 or exited['OOMKilled'] or not final['admission_drained'] or any(not p['exited_at'] for p in final['processes']):
            raise OperationFailed('Missing clean shutdown evidence')
        if source_consumers() or open_source_files():raise OperationFailed('An external source consumer remains')
        stage='snapshot-copy-and-verify'
        output=STATE/'frozen';output.mkdir(mode=0o700);os.chown(output,1000,1000)
        copied=helper(COPY_SOURCE,mounts=('type=volume,source='+DATA+',target=/source,readonly',
              'type=volume,source='+LOGS+',target=/app/logs',
              'type=bind,source='+str(output)+',target=/output'),timeout=120)
        certificate={'at':now(),'production_frozen_snapshot_acceptance':True,'production_database_cutover':False,
              'initial_counts':{k:initial[k] for k in ('requests','streams','workers','scheduled','commands')},
              'final_drain':final,'snapshot':copied,'all_running_source_consumers_stopped':True,
              'host_source_open_fds':0,'paused_scheduler_count':len(paused['active']),
              'external_application_process_count':0,'source_consumers_before_stop':consumers,
              'paused_scheduler_units':paused['active'],
              'shutdown_signal':'SIGTERM','container_exit':{k:exited[k] for k in ('ExitCode','OOMKilled','StartedAt','FinishedAt')},
              'production_business_load_injected':False,'synthetic_long_sse_worker_scheduler_evidence_separate':True,
              'maintenance_seconds_before_resume':round(time.monotonic()-started,3)}
        report('snapshot',certificate)
        for path in [output/'snapshot',*list((output/'snapshot').rglob('*'))]:
            os.chown(path,10001,10001)
    finally:
        stage='snapshot-resume-service'
        if closed:control('resume')
        if stopped:run(['docker','start',BACKEND],timeout=45)
        run(['docker','update','--restart=unless-stopped',BACKEND],check=False)
        resumed=health()
        restore_schedulers(paused)
        if resumed:FLAG.unlink(missing_ok=True)
        report('resumed',{'at':now(),'backend_healthy':resumed,'public_gate_removed':not FLAG.exists(),
                         'snapshot_completed':certificate is not None})
        if not resumed:raise OperationFailed('Service health needs recovery; maintenance remains closed')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','bootstrap','snapshot'))
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('Run with server administrative privileges')
    STATE.mkdir(mode=0o700,exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_uid!=0 or stat.S_IMODE(STATE.stat().st_mode)!=0o700:
        raise SystemExit('Private state permissions invalid')
    with (STATE/'operation.lock').open('a') as lock:
        os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            globals()[args.action]()
            print(json.dumps({'action':args.action,'passed':True,'at':now(),'production_database_cutover':False}))
            return 0
        except Exception as exc:
            failure={'action':args.action,'passed':False,'stage':stage,'error_type':type(exc).__name__,
                     'raw_output_suppressed':True,'production_database_cutover':False,'at':now()}
            if isinstance(exc,OperationFailed):failure['reason']=str(exc)
            report('failure',failure);print(json.dumps(failure));return 2


if __name__=='__main__':
    raise SystemExit(main())
