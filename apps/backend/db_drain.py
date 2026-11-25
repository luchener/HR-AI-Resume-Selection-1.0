"""Process-local admission and activity accounting for isolated DB rehearsals.

All writers/HTTP streams in a deployment must participate for a drain proof.
This object alone cannot certify production or coordinate multiple processes.
"""
from contextlib import contextmanager
from threading import Condition
from time import monotonic


class DrainingError(RuntimeError):
    pass


class DrainTracker:
    def __init__(self):
        self._condition = Condition()
        self._draining = False
        self._counts = {'requests': 0, 'streams': 0, 'workers': 0}

    def enter(self, kind):
        with self._condition:
            if kind not in self._counts:
                raise ValueError('Unknown activity kind')
            if self._draining:
                raise DrainingError('Analysis preview is draining')
            self._counts[kind] += 1
        closed = False

        def close():
            nonlocal closed
            with self._condition:
                if not closed:
                    closed = True
                    self._counts[kind] -= 1
                    self._condition.notify_all()
        return close

    @contextmanager
    def activity(self, kind):
        close = self.enter(kind)
        try:
            yield
        finally:
            close()

    def begin(self):
        """Irreversibly stop new admission for this rehearsal process."""
        with self._condition:
            self._draining = True
            self._condition.notify_all()

    def status(self):
        with self._condition:
            return {'draining': self._draining, **self._counts}

    def wait(self, timeout):
        if timeout < 0 or timeout > 60:
            raise ValueError('Drain wait must be between 0 and 60 seconds')
        deadline = monotonic() + timeout
        with self._condition:
            while any(self._counts.values()):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return self._draining
