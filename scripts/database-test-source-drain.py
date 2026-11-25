"""Real four-worker Gunicorn/socket/SIGTERM drill using synthetic JSON only.

Build scripts/database-drain-review/Dockerfile as resume-source-drain:review.
All mounts are created inside a fresh local .test-tmp directory. No SSH,
production environment, credentials, provider calls or emails are involved.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
from time import monotonic, sleep
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'apps'/'backend'))
from source_drain import Coordinator


def docker(*args):
    result = subprocess.run(['docker',*args], capture_output=True, text=True, encoding='utf-8', timeout=40)
    if result.returncode:
        raise RuntimeError('Isolated Docker operation failed')
    return result.stdout


def wait_for(check, timeout=20):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        value = check()
        if value:
            return value
        sleep(0.1)
    raise RuntimeError('Isolated drain condition timed out')


def manifest(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Unexpected source symlink')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def run(output, execution_context='local-isolated'):
    temp_root = ROOT/'apps'/'backend'/'.test-tmp'
    temp_root.mkdir(exist_ok=True)
    name = 'resume-source-drain-review-' + uuid.uuid4().hex[:8]
    purpose = 'resume-source-drain-review'
    evidence = {'production_acceptance':False, 'scope':'isolated synthetic JSON; real Gunicorn and sockets',
                'execution_context':execution_context,
                'started_at':datetime.now(timezone.utc).isoformat(), 'checkpoints':[]}
    with tempfile.TemporaryDirectory(prefix='source-drain-',dir=temp_root) as work:
        work = Path(work).resolve()
        if not work.is_relative_to(temp_root.resolve()):
            raise RuntimeError('Invalid review workspace')
        control, source, logs = work/'control', work/'source', work/'logs'
        for path in (control,source,logs):
            path.mkdir()
            # Linux bind mounts retain host ownership, unlike Docker Desktop.
            # Only these freshly created, private synthetic directories change
            # owner so the rehearsal image's non-root user can write them.
            if sys.platform.startswith('linux'):
                import os
                if os.geteuid()==0:
                    os.chown(path,10001,10001)
        # Live SQLite access stays inside Linux; Windows and Linux file locks
        # over a Docker bind mount must not be treated as one lock domain.
        class LiveControl:
            def status(self):
                return json.loads(docker('exec',name,'python','source_drain.py','--state','/review/control.sqlite','status'))
            def begin(self):
                return json.loads(docker('exec',name,'python','source_drain.py','--state','/review/control.sqlite','begin'))
            def wait(self,timeout):
                return json.loads(docker('exec',name,'python','source_drain.py','--state','/review/control.sqlite',
                                         'wait','--timeout',str(timeout)))
        c = LiveControl()
        running = False
        scheduler = None
        executor = ThreadPoolExecutor(max_workers=1)
        connection = None
        long_future = None
        try:
            memory,cpu=('256m','0.5') if execution_context=='server-isolated' else ('512m','2')
            docker('run','-d','--name',name,'--label','purpose='+purpose,
                   '--memory='+memory,'--memory-swap='+memory,'--cpus='+cpu,'-p','127.0.0.1::8000',
                   '--mount',f'type=bind,source={control},target=/review',
                   '--mount',f'type=bind,source={source},target=/app/data',
                   '--mount',f'type=bind,source={logs},target=/app/logs',
                   'resume-source-drain:review')
            running = True
            detail = json.loads(docker('inspect',name))[0]
            if detail['Config']['Labels'].get('purpose') != purpose or len(detail['Mounts']) != 3:
                raise RuntimeError('Review container identity mismatch')
            if any(m['Type'] != 'bind' for m in detail['Mounts']):
                raise RuntimeError('Unexpected review mount')
            port = int(detail['NetworkSettings']['Ports']['8000/tcp'][0]['HostPort'])
            def request(path,method='GET'):
                conn = http.client.HTTPConnection('127.0.0.1',port,timeout=30)
                try:
                    conn.request(method,path)
                    response = conn.getresponse()
                    response.read()
                    return response.status
                finally:
                    conn.close()
            def ready():
                if not (control/'control.sqlite').exists():
                    return False
                try:
                    report = c.status()
                    return len([p for p in report['processes'] if p['role']=='worker']) == 4 and request('/ping')==200
                except (OSError, RuntimeError, socket.error):
                    return False
            wait_for(ready,30)
            evidence['worker_count'] = 4
            long_future = executor.submit(request,'/_review/long')
            wait_for(lambda:(control/'long-started').exists())
            connection = http.client.HTTPConnection('127.0.0.1',port,timeout=30)
            connection.request('GET','/_review/sse')
            stream = connection.getresponse()
            if stream.status != 200:
                raise RuntimeError('Synthetic SSE was not accepted')
            for _ in range(64):
                line = stream.readline()
                if not line:
                    raise RuntimeError('Synthetic SSE ended before worker admission')
                if b'processing' in line:
                    break
            else:
                raise RuntimeError('Synthetic SSE never admitted its worker')
            scheduler = subprocess.Popen(['docker','exec',name,'python','rehearsal_writer.py'],
                                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            wait_for(lambda:(control/'scheduled-started').exists())
            active = c.status()
            if not (active['requests']==2 and active['streams']==1 and active['workers']==1 and active['scheduled']==1):
                raise RuntimeError('Missing active write source')
            evidence['checkpoints'].append({'name':'active_long_sse_worker_scheduler',**active})
            begun = c.begin()
            if request('/_review/long','POST')!=503 or request('/ping')!=200:
                raise RuntimeError('Admission or health check mismatch')
            evidence['checkpoints'].append({'name':'admission_closed',**begun,'new_request_status':503,'ping_status':200})
            for marker in ('release-long','release-worker','release-scheduled'):
                (control/marker).touch()
            if long_future.result(timeout=20)!=200:
                raise RuntimeError('Long request did not finish')
            if scheduler.wait(timeout=20)!=0:
                raise RuntimeError('Scheduled write did not finish')
            wait_for(lambda:c.status()['workers']==0 and c.status()['scheduled']==0)
            pending_stream = c.status()
            if not (pending_stream['streams']==1 and pending_stream['requests']==1 and not pending_stream['admission_drained']):
                raise RuntimeError('Open SSE falsely certified as drained')
            evidence['checkpoints'].append({'name':'writers_finished_stream_open',**pending_stream})
            (control/'release-stream').touch()
            stream.read()
            connection.close()
            connection = None
            drained = c.wait(10)
            if not drained['admission_drained']:
                raise RuntimeError('Source did not drain')
            evidence['checkpoints'].append({'name':'all_admitted_work_finished',**drained})
            evidence['shutdown_requested_at'] = datetime.now(timezone.utc).isoformat()
            evidence['shutdown_signal'] = 'SIGTERM'
            docker('stop','--time','20',name)
            c = Coordinator(control/'control.sqlite')
            stopped = json.loads(docker('inspect',name))[0]
            state = stopped['State']
            exited = c.status()
            if state['Running'] or state['ExitCode']!=0 or state['OOMKilled']:
                raise RuntimeError('Container did not stop cleanly')
            if any(p['exited_at'] is None for p in exited['processes']) or not exited['admission_drained']:
                raise RuntimeError('Missing process-exit or work-completion evidence')
            evidence['checkpoints'].append({'name':'confirmed_process_exit',**exited})
            evidence['container_exit'] = {key:state[key] for key in ('ExitCode','OOMKilled','StartedAt','FinishedAt')}
            if not all((source/name).is_file() for name in ('review-long.json','review-worker.json','review-scheduled.json')):
                raise RuntimeError('A writer did not persist its synthetic result')
            before = manifest(source)
            archive = work/'snapshot.tar.gz'
            with tarfile.open(archive,'w:gz') as handle:
                handle.add(source,arcname='source')
            copied = work/'copied'
            with tarfile.open(archive,'r:gz') as handle:
                handle.extractall(copied,filter='data')
            if manifest(source)!=before or manifest(copied/'source')!=before or c.status()['epoch']!=drained['epoch']:
                raise RuntimeError('Snapshot changed or did not match')
            evidence['snapshot'] = {'file_count':len(before),'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                                    'source_and_copy_match':True,'source_stable':True,'admission_epoch_unchanged':True}
            evidence['completed_at'] = datetime.now(timezone.utc).isoformat()
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({'passed':True,'workers':4,'snapshot_files':len(before),'production_acceptance':False}))
        finally:
            for marker in ('release-long','release-worker','release-scheduled','release-stream'):
                (control/marker).touch()
            if connection:
                connection.close()
            if scheduler and scheduler.poll() is None:
                try:
                    scheduler.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    scheduler.terminate()
                    scheduler.wait(timeout=5)
            executor.shutdown(wait=True)
            if running:
                detail = json.loads(docker('inspect',name))[0]
                if detail['Config']['Labels'].get('purpose') != purpose:
                    raise RuntimeError('Refusing to remove an unrelated container')
                docker('rm','-f',name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'docs'/'database-evidence'/'2026-10-16-source-drain.json')
    parser.add_argument('--execution-context',choices=('local-isolated','server-isolated'),default='local-isolated')
    args = parser.parse_args()
    run(args.output.resolve(),args.execution_context)
