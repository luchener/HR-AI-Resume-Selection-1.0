"""Private 24-hour HR result cache, shared by workers on the same data volume.

Callers MUST authorize source records before every read (including after waiting
for singleflight). No source text, IDs, model credentials or cache key are saved.
Expired reports are swept on cache access; fixed lock stripes bound lock files.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import tempfile
import threading
import time

import config
import safe_fs

TTL_SECONDS = 24 * 3600
MAX_ENTRIES = 1000
_LOCKS = [threading.Lock() for _ in range(65)]
try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt


def directory() -> str:
    return os.path.join(config.DATA_DIR, "analysis_cache")


def key_digest(key: tuple) -> str:
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def _lock(slot: int):
    # Never unlink lock files: that would create different inodes for waiters.
    root = directory()
    os.makedirs(root, mode=0o700, exist_ok=True)
    with _LOCKS[slot]:
        path = os.path.join(root, f"_lock_{slot:02d}")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+b", buffering=0) as handle:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in (13, 36):
                            raise
                        time.sleep(0.05)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def singleflight(key: tuple):
    with _lock(int(key_digest(key)[:8], 16) % 64):
        yield


def cacheable(result: dict, raw: dict | None = None) -> bool:
    if not isinstance(result, dict) or result.get("analysis_incomplete"):
        return False
    scoring = result.get("scoring_meta")
    if isinstance(scoring, dict) and (scoring.get("incomplete") or scoring.get("decision_ready") is not True):
        return False
    source = raw if raw is not None else result
    meta = source.get("agent_meta")
    validation = source.get("agent_validation")
    if not isinstance(meta, dict) or meta.get("validation_passed") is not True:
        return False
    if meta.get("recheck_required") or source.get("recheck_required"):
        return False
    if not isinstance(validation, dict) or validation.get("passed") is not True or validation.get("recheck_required"):
        return False
    scores = source.get("score_breakdown")
    maxima = {"hard_requirements": 25, "responsibility_overlap": 25, "skills_projects": 25, "industry_background": 15, "evidence_bonus": 10}
    return isinstance(scores, dict) and all(
        type(scores.get(name)) in (int, float) and 0 <= scores[name] <= maximum
        for name, maximum in maxima.items()
    )


def _prune(now: float, *, reserve: int = 0) -> None:
    entries = []
    for name in safe_fs.list_files(directory(), suffix=".json"):
        path = os.path.join(directory(), name)
        try:
            with open(path, encoding="utf-8") as handle:
                entry = json.load(handle)
            created = float(entry["created_at"])
            if not 0 <= now - created < TTL_SECONDS:
                os.unlink(path)
            else:
                entries.append((created, path))
        except (OSError, ValueError, TypeError, KeyError):
            try:
                os.unlink(path)
            except OSError:
                pass
    for _, path in sorted(entries)[:max(0, len(entries) - MAX_ENTRIES + reserve)]:
        try:
            os.unlink(path)
        except OSError:
            pass


def get(key: tuple) -> dict | None:
    with _lock(64):
        now = time.time()
        _prune(now)
        try:
            with open(os.path.join(directory(), key_digest(key) + ".json"), encoding="utf-8") as handle:
                entry = json.load(handle)
            value = entry["value"]
            return value if cacheable(value) else None
        except (OSError, ValueError, TypeError, KeyError):
            return None


def put(key: tuple, value: dict) -> None:
    if not cacheable(value):
        return
    with _lock(64):
        now = time.time()
        _prune(now, reserve=1)
        fd, temp_path = tempfile.mkstemp(prefix="_write_", dir=directory())
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"created_at": now, "value": value}, handle, ensure_ascii=False, allow_nan=False)
            os.replace(temp_path, os.path.join(directory(), key_digest(key) + ".json"))
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
