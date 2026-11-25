"""Gunicorn process evidence. Inactive unless SOURCE_DRAIN_STATE_PATH is set."""
import source_drain


def on_starting(server):
    coordinator = source_drain.configured()
    if coordinator:
        server.source_drain_token = coordinator.process_started('master')


def post_worker_init(worker):
    coordinator = source_drain.configured()
    if coordinator:
        worker.source_drain_token = coordinator.process_started('worker')


def worker_exit(server, worker):
    coordinator = source_drain.configured()
    token = getattr(worker, 'source_drain_token', None)
    if coordinator and token:
        coordinator.process_exited(token)


def on_exit(server):
    coordinator = source_drain.configured()
    token = getattr(server, 'source_drain_token', None)
    if coordinator and token:
        coordinator.process_exited(token)
