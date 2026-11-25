"""Synthetic independent scheduler process, using the production admission gate."""
from pathlib import Path
from time import monotonic, sleep
import source_drain
import store


with source_drain.activity('scheduled'):
    Path('/review/scheduled-started').touch()
    deadline = monotonic() + 60
    while not Path('/review/release-scheduled').exists():
        if monotonic() >= deadline:
            raise RuntimeError('Synthetic scheduler timed out')
        sleep(0.05)
    store._write_json('/app/data/review-scheduled.json', {'synthetic':True,'completed':True})
