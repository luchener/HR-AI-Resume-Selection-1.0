"""Opt-in, shared admission control for the authoritative JSON application.

Keep this SQLite registry OUTSIDE the source data directory, on a local volume
shared by every Gunicorn worker and administrative/scheduled writer. It contains
only activity kinds, random identifiers, PIDs and timestamps. A crashed activity
is deliberately retained. Empty counters alone do not certify a snapshot: also
stop all processes and verify their exit, and exclude external volume writers.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import argparse
import json
import os
from pathlib import Path
import sqlite3
import threading
from time import monotonic, sleep
import uuid


KINDS = ('requests', 'streams', 'workers', 'scheduled', 'commands')
_current = ContextVar('source_drain_activity', default=None)


class DrainUnavailable(RuntimeError):
    pass


class AdmissionClosed(DrainUnavailable):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


class Coordinator:
    def __init__(self, path):
        self.path = str(Path(path).resolve())

    @contextmanager
    def _transaction(self):
        # mode=rw never silently creates a replacement after registry loss.
        conn = None
        try:
            conn = sqlite3.connect(Path(self.path).as_uri() + '?mode=rw', uri=True, timeout=5)
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT version FROM control WHERE id=1').fetchone() != (1,):
                raise DrainUnavailable('Invalid drain registry')
            yield conn
            conn.commit()
        except sqlite3.Error:
            raise DrainUnavailable('Drain registry unavailable') from None
        finally:
            if conn is not None:
                conn.close()

    @classmethod
    def initialize(cls, path):
        path = Path(path).resolve()
        # Refuse existing state: replacing it could erase live or crashed work.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript('''
                CREATE TABLE control (
                    id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
                    epoch INTEGER NOT NULL, draining INTEGER NOT NULL, begun_at TEXT);
                INSERT INTO control VALUES (1,1,0,0,NULL);
                CREATE TABLE activities (
                    token TEXT PRIMARY KEY, pid INTEGER NOT NULL, kind TEXT NOT NULL,
                    parent TEXT, started_at TEXT NOT NULL);
                CREATE TABLE processes (
                    token TEXT PRIMARY KEY, pid INTEGER NOT NULL, role TEXT NOT NULL,
                    started_at TEXT NOT NULL, exited_at TEXT);
                CREATE TABLE events (
                    seq INTEGER PRIMARY KEY, pid INTEGER NOT NULL, kind TEXT NOT NULL,
                    at TEXT NOT NULL);
                CREATE TABLE finished (
                    token TEXT PRIMARY KEY, pid INTEGER NOT NULL, kind TEXT NOT NULL,
                    outcome TEXT NOT NULL, at TEXT NOT NULL);
            ''')
            conn.commit()
        finally:
            conn.close()
        return cls(path)

    def enter(self, kind, parent=None):
        if kind not in KINDS:
            raise ValueError('Unknown drain activity')
        token = uuid.uuid4().hex
        with self._transaction() as conn:
            if parent is not None:
                # A child of previously accepted work may finish during drain.
                if conn.execute('SELECT 1 FROM activities WHERE token=?', (parent,)).fetchone() is None:
                    raise AdmissionClosed('Parent activity has ended')
            elif conn.execute('SELECT draining FROM control WHERE id=1').fetchone()[0]:
                raise AdmissionClosed('Source writes are draining')
            conn.execute('INSERT INTO activities VALUES (?,?,?,?,?)',
                         (token, os.getpid(), kind, parent, _now()))
        return Lease(self, token)

    def begin(self):
        with self._transaction() as conn:
            conn.execute('UPDATE control SET draining=1,begun_at=COALESCE(begun_at,?),'
                         'epoch=epoch+CASE WHEN draining=0 THEN 1 ELSE 0 END WHERE id=1', (_now(),))
        return self.status()

    def resume(self):
        # Explicit operator action. Never deletes outstanding work or evidence.
        with self._transaction() as conn:
            conn.execute('UPDATE control SET draining=0,begun_at=NULL,epoch=epoch+1 WHERE id=1')
            self._event(conn, 'admission_resumed')
        return self.status()

    @staticmethod
    def _event(conn, kind):
        conn.execute('INSERT INTO events(pid,kind,at) VALUES (?,?,?)', (os.getpid(), kind, _now()))

    def process_started(self, role):
        if role not in ('master', 'worker'):
            raise ValueError('Unknown process role')
        token = uuid.uuid4().hex
        with self._transaction() as conn:
            if conn.execute('SELECT draining FROM control WHERE id=1').fetchone()[0]:
                raise AdmissionClosed('Cannot start processes during drain')
            conn.execute('INSERT INTO processes VALUES (?,?,?,?,NULL)',
                         (token, os.getpid(), role, _now()))
            self._event(conn, role + '_started')
        return token

    def process_exited(self, token):
        with self._transaction() as conn:
            row = conn.execute('SELECT role FROM processes WHERE token=? AND pid=? AND exited_at IS NULL',
                               (token, os.getpid())).fetchone()
            if row is None:
                raise DrainUnavailable('Unknown exiting process')
            conn.execute('UPDATE processes SET exited_at=? WHERE token=?', (_now(), token))
            self._event(conn, row[0] + '_exit_hook')

    def status(self):
        with self._transaction() as conn:
            epoch, draining, begun = conn.execute('SELECT epoch,draining,begun_at FROM control WHERE id=1').fetchone()
            counts = dict.fromkeys(KINDS, 0)
            counts.update(dict(conn.execute('SELECT kind,count(*) FROM activities GROUP BY kind')))
            processes = [{'pid':pid, 'role':role, 'started_at':started, 'exited_at':exited}
                         for pid,role,started,exited in conn.execute(
                             'SELECT pid,role,started_at,exited_at FROM processes ORDER BY started_at')]
            events = [{'seq':seq, 'pid':pid, 'kind':kind, 'at':at}
                      for seq,pid,kind,at in conn.execute('SELECT seq,pid,kind,at FROM events ORDER BY seq')]
            outcomes = [{'kind':kind, 'outcome':outcome, 'count':count}
                        for kind,outcome,count in conn.execute(
                            'SELECT kind,outcome,count(*) FROM finished GROUP BY kind,outcome ORDER BY kind,outcome')]
        return {'observed_at':_now(), 'epoch':epoch, 'draining':bool(draining),
                'begun_at':begun, **counts, 'admission_drained':bool(draining) and not any(counts.values()),
                'processes':processes, 'events':events, 'outcomes':outcomes, 'production_acceptance':False}

    def wait(self, timeout):
        if not 0 <= timeout <= 60:
            raise ValueError('Drain wait must be between 0 and 60 seconds')
        deadline = monotonic() + timeout
        while True:
            report = self.status()
            if report['admission_drained'] or monotonic() >= deadline:
                return report
            sleep(min(0.1, max(0, deadline - monotonic())))


class Lease:
    def __init__(self, coordinator, token):
        self.coordinator, self.token = coordinator, token
        self._lock = threading.Lock()
        self._closed = False

    @contextmanager
    def activate(self):
        token = _current.set(self)
        try:
            yield
        finally:
            _current.reset(token)

    def close(self, outcome='completed'):
        if outcome not in ('completed','cancelled','failed','rejected'):
            raise ValueError('Unknown activity outcome')
        with self._lock:
            if self._closed:
                return
            with self.coordinator._transaction() as conn:
                conn.execute('INSERT INTO finished SELECT token,pid,kind,?,? FROM activities WHERE token=?',
                             (outcome,_now(),self.token))
                conn.execute('DELETE FROM activities WHERE token=?', (self.token,))
            self._closed = True


def configured():
    path = os.environ.get('SOURCE_DRAIN_STATE_PATH', '').strip()
    return Coordinator(path) if path else None


@contextmanager
def activity(kind):
    parent = _current.get()
    coordinator = parent.coordinator if parent else configured()
    if coordinator is None:
        yield
        return
    lease = coordinator.enter(kind, parent.token if parent else None)
    try:
        with lease.activate():
            yield
    except BaseException:
        lease.close('failed')
        raise
    finally:
        lease.close()


def writer(kind):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with activity(kind):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def start_thread(target, *, args=(), kwargs=None, name=None, daemon=True):
    """Reserve BEFORE dispatch; keep tracking after the HTTP/SSE parent closes."""
    parent = _current.get()
    coordinator = parent.coordinator if parent else configured()
    lease = coordinator.enter('workers', parent.token if parent else None) if coordinator else None

    def run():
        if lease is None:
            target(*args, **(kwargs or {}))
            return
        try:
            with lease.activate():
                target(*args, **(kwargs or {}))
        except BaseException:
            lease.close('failed')
            raise
        finally:
            lease.close()

    worker = threading.Thread(target=run, name=name, daemon=daemon)
    try:
        worker.start()
    except BaseException:
        if lease:
            lease.close('failed')
        raise
    return worker


class _ResponseIterator:
    def __init__(self, result, lease, close_stream, complete_outcome):
        self.result, self.iterator = result, iter(result)
        self.lease, self.close_stream = lease, close_stream
        self.complete_outcome = complete_outcome
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            with self.lease.activate():
                return next(self.iterator)
        except StopIteration:
            self.close(self.complete_outcome())
            raise
        except BaseException:
            self.close('failed')
            raise

    def close(self, outcome='cancelled'):
        if self.closed:
            return
        self.closed = True
        try:
            with self.lease.activate():
                close = getattr(self.result, 'close', None)
                if close:
                    close()
        finally:
            try:
                self.close_stream(outcome)
            finally:
                self.lease.close(outcome)


class DrainMiddleware:
    def __init__(self, app, coordinator):
        self.app, self.coordinator = app, coordinator

    def __call__(self, environ, start_response):
        # The real app's /ping hook skips housekeeping. No other GET is assumed
        # read-only: auth, caches and lazy cleanup can write on read requests.
        if environ.get('PATH_INFO') == '/ping' and environ.get('REQUEST_METHOD') in ('GET', 'HEAD'):
            return self.app(environ, start_response)
        try:
            lease = self.coordinator.enter('requests')
        except DrainUnavailable:
            body = b'{"detail":"Service is draining or unavailable"}'
            start_response('503 Service Unavailable', [('Content-Type','application/json'),
                           ('Content-Length',str(len(body))), ('Retry-After','30'), ('Cache-Control','no-store')])
            return [body]
        stream = None
        complete_outcome = 'completed'

        def respond(status, headers, exc_info=None):
            nonlocal stream, complete_outcome
            code = int(status.split()[0])
            complete_outcome = 'failed' if code >= 500 else 'rejected' if code >= 400 else 'completed'
            if stream is None and any(k.lower() == 'content-type' and v.lower().startswith('text/event-stream') for k,v in headers):
                stream = self.coordinator.enter('streams', lease.token)
            return start_response(status, headers, exc_info)

        try:
            with lease.activate():
                result = self.app(environ, respond)
            return _ResponseIterator(result, lease, lambda outcome: stream.close(outcome) if stream else None,
                                     lambda: complete_outcome)
        except BaseException:
            if stream:
                stream.close('failed')
            lease.close('failed')
            raise


def main():
    parser = argparse.ArgumentParser(description='Private JSON source drain control (no HTTP admin endpoint)')
    parser.add_argument('--state', required=True)
    parser.add_argument('action', choices=('init', 'begin', 'status', 'wait', 'resume'))
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    try:
        c = Coordinator.initialize(args.state) if args.action == 'init' else Coordinator(args.state)
        if args.action == 'wait':
            report = c.wait(args.timeout)
        elif args.action in ('begin', 'resume'):
            report = getattr(c, args.action)()
        else:
            report = c.status()
        print(json.dumps(report, sort_keys=True))
        return 1 if args.action == 'wait' and not report['admission_drained'] else 0
    except (OSError, DrainUnavailable, ValueError):
        print('{"error":"Drain control failed; no state was cleared"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
