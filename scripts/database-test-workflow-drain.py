"""Synthetic Flask-client drain proof on an EMPTY guarded review_test database.

Requires migrated schema/roles/grants. Does not stop containers or read source
files. Output contains counts/UTC timestamps only, never tokens or report data.
This is not a production network/process or snapshot consistency certificate.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock
from time import monotonic, time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps' / 'backend'))

import jwt
import sqlalchemy as sa

from db_analysis_workflow import AnalysisWorkflow
from db_auth import AuthService
from db_http_preview import create_preview_app
from db_runtime import build_runtime, verify_runtime
from db_security import migration_engine
from db_store import Store
from db_test_guard import review_test_url


def main():
    url = review_test_url(os.environ['DATABASE_TEST_URL'])
    bootstrap = migration_engine(url.render_as_string(hide_password=False))
    app_url = url.set(username='resume_app', password='review-app-only')
    engine, sessions = build_runtime(app_url.render_as_string(hide_password=False))
    workflow = None
    release, started = Event(), Event()
    lock = Lock()
    starts = 0
    checkpoints = []
    response = None
    try:
        verify_runtime(engine)
        with bootstrap.connect() as conn:
            if conn.scalar(sa.text('SELECT count(*) FROM analysis_tasks')) or conn.scalar(sa.text('SELECT count(*) FROM quota_reservations')):
                raise RuntimeError('Refusing a nonempty task/reservation rehearsal target')
            head = conn.scalar(sa.text('SELECT version_num FROM alembic_version'))
        auth = AuthService(sessions)
        user, error = auth.create_user('drain-' + uuid4().hex, 'SyntheticPassword9')
        if error:
            raise RuntimeError('Synthetic account creation failed')
        store = Store(session_factory=sessions)
        resumes = [store.save_resume('Synthetic drain fixture', {}, user['user_id']) for _ in range(2)]
        job = store.save_job(None, 'Synthetic job fixture', {}, user['user_id'])

        def analyzer(**kwargs):
            nonlocal starts
            with lock:
                starts += 1
                if starts == 2:
                    started.set()
            if not release.wait(timeout=20):
                raise RuntimeError('Synthetic inference was not released')
            return {'final_score': 82, 'summary': 'Synthetic completed analysis'}

        workflow = AnalysisWorkflow(sessions, analyzer, max_workers=2)
        app = create_preview_app(store, auth, 'synthetic-local-drain-jwt-secret-at-least-48-bytes',
                                 analysis_workflow=workflow)
        secret = 'synthetic-local-drain-jwt-secret-at-least-48-bytes'
        token = jwt.encode({'user_id': user['user_id'], 'username': user['username'],
            'pwd_ver': 1, 'iss': 'resume-matcher', 'jti': str(uuid4()), 'iat': time(), 'exp': time() + 3600},
            secret, algorithm='HS256')
        auth_header = {'Authorization': 'Bearer ' + token}

        def request_json():
            with app.test_client() as client:
                result = client.post('/api/v1/resumes/hr-analysis',
                    json={'resume_id': resumes[0], 'job_id': job},
                    headers={**auth_header, 'Idempotency-Key': str(uuid4())})
                if result.status_code != 200:
                    raise RuntimeError('Synthetic long request did not complete')
                return result.get_json()['data']['task_id']

        def checkpoint(name):
            checkpoints.append({'name': name, 'at_utc': datetime.now(timezone.utc).isoformat(),
                                **workflow.drain_report()})

        with ThreadPoolExecutor(max_workers=1) as requests:
            long_request = requests.submit(request_json)
            try:
                client = app.test_client()
                response = client.post('/api/v1/resumes/hr-analysis?stream=true',
                    json={'resume_id': resumes[1], 'job_id': job},
                    headers={**auth_header, 'Idempotency-Key': str(uuid4())}, buffered=False)
                assert response.status_code == 200
                first = json.loads(next(response.response).decode().removeprefix('data: '))
                assert first['status'] == 'starting'
                assert started.wait(timeout=10)
                checkpoint('active_long_request_stream_and_workers')
                assert checkpoints[-1]['requests'] == checkpoints[-1]['streams'] == 1
                assert checkpoints[-1]['workers'] == checkpoints[-1]['unfinished_tasks'] == 2
                workflow.tracker.begin()
                refused = client.post('/api/v1/resumes/hr-analysis',
                    json={'resume_id': resumes[0], 'job_id': job},
                    headers={**auth_header, 'Idempotency-Key': str(uuid4())})
                assert refused.status_code == 503
                assert not workflow.tracker.wait(0.05)
                checkpoint('admission_stopped_existing_work_retained')
            finally:
                release.set()
            long_request.result(timeout=10)
            deadline = monotonic() + 10
            while workflow.tracker.status()['workers']:
                if monotonic() >= deadline:
                    raise RuntimeError('Synthetic workers did not finish')
                Event().wait(0.02)
            checkpoint('workers_committed_stream_still_open')
            assert checkpoints[-1]['streams'] == 1 and not checkpoints[-1]['complete']
            assert checkpoints[-1]['unfinished_tasks'] == checkpoints[-1]['pending_reservations'] == 0
            events = [json.loads(chunk.decode().removeprefix('data: ')) for chunk in response.response]
            assert events[-1]['status'] == 'completed'
            committed = workflow.snapshot(task_id=first['task_id'], owner_user_id=user['user_id'], password_version=1)
            assert events[-1]['result']['data'] == committed
            response.close()
            response = None
            assert workflow.tracker.wait(10)
            checkpoint('requests_streams_workers_and_durable_work_drained')
            assert checkpoints[-1]['complete']
        print(json.dumps({'schema': head, 'scope': 'isolated_flask_client_and_postgresql',
            'production_acceptance': False, 'new_request_after_drain_http_status': 503,
            'sse_completion_matches_committed_results': True, 'checkpoints': checkpoints}, indent=2))
    finally:
        release.set()
        if response is not None:
            response.close()
        if workflow is not None:
            workflow.close()
        engine.dispose()
        bootstrap.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
