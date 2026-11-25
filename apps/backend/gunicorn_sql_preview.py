"""Single-process isolated SQL preview; construct owned resources after fork."""
import json
from time import monotonic, sleep

bind = '0.0.0.0:8000'
workers = 1
worker_class = 'gthread'
threads = 8
preload_app = False
timeout = 1200
graceful_timeout = 1250
accesslog = None
errorlog = '-'


def worker_exit(server, worker):
    app = getattr(worker, 'wsgi', None)
    application = getattr(app, 'extensions', {}).get('db_application')
    if application is None:
        return
    deadline = monotonic() + 1200
    while True:
        report = application.close(timeout=60)
        if report.get('closed') or monotonic() >= deadline:
            break
        sleep(0.1)
    # Fixed booleans only: no identities, configuration, or exception messages.
    worker.log.info(json.dumps({'sql_preview_closed': report.get('closed') is True,
                               'sql_preview_drained': report.get('complete') is True}))
