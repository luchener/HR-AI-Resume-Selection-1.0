"""Disposable server only. Never copy these routes into the production image."""
import json
from pathlib import Path
from time import monotonic, sleep

from flask import Response, stream_with_context
import app as application
import auth
import store


app = application.app
CONTROL = Path('/review')
auth._PUBLIC_PATHS.update({'/_review/long', '/_review/sse'})


def wait_for(name):
    deadline = monotonic() + 60
    while not (CONTROL/name).exists():
        if monotonic() >= deadline:
            raise RuntimeError('Synthetic rehearsal timed out')
        sleep(0.05)


def synthetic_analysis(*args, **kwargs):
    kwargs['on_event']({'status':'processing','message':'synthetic'})
    wait_for('release-worker')
    store._write_json('/app/data/review-worker.json', {'synthetic':True,'completed':True})
    return {'candidate_name':'Synthetic', 'final_score':80, 'summary':'Synthetic report'}


application._run_hr_analysis = synthetic_analysis


@app.get('/_review/long')
def long_request():
    (CONTROL/'long-started').touch()
    wait_for('release-long')
    store._write_json('/app/data/review-long.json', {'synthetic':True,'completed':True})
    return {'completed':True}


@app.get('/_review/sse')
def stream():
    def generate():
        yield from application._hr_analysis_stream(
            ['synthetic-resume'], 'synthetic-job', 'synthetic-request', 'synthetic-user', None, {})
        # Keep a real socket open after the actual legacy worker has committed.
        while not (CONTROL/'release-stream').exists():
            yield ': synthetic keepalive\n\n'
            sleep(0.05)
    return Response(stream_with_context(generate()), mimetype='text/event-stream')
