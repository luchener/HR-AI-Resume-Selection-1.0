"""Explicit process-local GC scheduling for SQL rehearsals, never auto-started.

All HTTP writers and workers must share this tracker. A report only certifies
this process plus the inspected durable queue, never other deployment writers.
Stop retains unfinished intents. Drain never unlinks after claiming completion.
"""
import math
from threading import Event, Lock, Thread
from time import monotonic

import sqlalchemy as sa

from db_original_gc import original_gc


class OriginalGCWorker:
    def __init__(self, service, tracker, *, interval=5, batch_limit=20):
        if (type(interval) not in (int, float) or not math.isfinite(interval) or not 0.01 <= interval <= 3600 or
                type(batch_limit) is not int or not 1 <= batch_limit <= 100):
            raise ValueError('Invalid GC worker bounds')
        self.service, self.tracker = service, tracker
        self.interval, self.batch_limit = interval, batch_limit
        self._lock, self._stop = Lock(), Event()
        self._thread = None
        self._started = False
        self._runs = self._removed = self._failures = 0

    def start(self):
        with self._lock:
            if self._started or self._stop.is_set():
                raise ValueError('GC worker cannot be restarted')
            # Register lifetime before thread launch; draining cannot miss a
            # scheduled thread that has not yet begun executing.
            close = self.tracker.enter('workers')
            self._thread = Thread(target=self._run, args=(close,), name='db-original-gc', daemon=False)
            try:
                self._thread.start()
            except BaseException:
                close()
                self._stop.set()
                self._thread = None
                raise
            self._started = True
        return self

    def _run(self, close):
        try:
            while not self._stop.is_set() and not self.tracker.status()['draining']:
                try:
                    count = self.service.collect_originals(limit=self.batch_limit)
                except Exception:
                    with self._lock:
                        self._failures += 1
                    # Do not log exception values: storage/SQL errors can carry
                    # sensitive paths or connection parameters. Intents persist.
                else:
                    with self._lock:
                        self._runs += 1
                        self._removed += count
                self._stop.wait(self.interval)
        finally:
            close()

    @staticmethod
    def _timeout(timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError('Timeout must be between zero and 60 seconds')

    def stop(self, *, timeout=10):
        self._timeout(timeout)
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def drain(self, *, timeout=10):
        self._timeout(timeout)
        deadline = monotonic() + timeout
        self.tracker.begin()
        self.stop(timeout=timeout)
        self.tracker.wait(max(0, deadline - monotonic()))
        return self.report()

    def report(self):
        before = self.tracker.status()
        with self._lock:
            stats = dict(started=self._started, stopped=self._stop.is_set(),
                         alive=self._thread is not None and self._thread.is_alive(),
                         runs=self._runs, removed=self._removed, failures=self._failures)
        try:
            with self.service.sessions() as session, session.begin():
                # Count WITHOUT SKIP LOCKED and include foreign storage. Neither
                # another collector's lock nor an unmounted store proves empty.
                pending, local = session.execute(sa.select(sa.func.count(), sa.func.count().filter(
                    original_gc.c.storage_id == self.service.originals.storage_id)).select_from(original_gc)).one()
        except Exception:
            pending = local = None
        activity = self.tracker.status()
        # If an admitted writer was active before querying, its last commit may
        # follow the empty-queue snapshot. Require quiescence on BOTH sides.
        complete = (before['draining'] and not any(before[k] for k in ('requests', 'streams', 'workers')) and
                    activity['draining'] and not stats['alive'] and
                    not any(activity[k] for k in ('requests', 'streams', 'workers')) and pending == 0)
        return dict(**stats, **activity, pending=pending, pending_local=local, complete=complete)
