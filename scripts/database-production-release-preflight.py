#!/usr/bin/env python3
"""Run the read-only database release preflight.

This command never connects to a production database, changes service state, or
reads secret values.  It validates the latest isolated acceptance evidence,
parses the Python sources, checks the safe runtime flags, and records the
remaining release blockers in a non-secret JSON report.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "docs" / "database-evidence"
SOURCE_HASH_MODE = "lf-normalized-sha256-v1"
PORT_CHECK_METHOD = 'connect-and-bind-v1'

REQUIRED_EVIDENCE = {
    '2026-10-29-r03-task-final10.json': 73,
    '2026-10-29-r03-admin-ai-final10.json': 14,
    '2026-10-29-r03-full-final10.json': 427,
    '2026-10-29-r03-retention-final10.json': 15,
    '2026-10-29-r03-drain-final10.json': 1,
    '2026-10-29-r03-email-final10.json': 77,
}

SOURCE_MANIFEST_PATH = ROOT / "scripts" / "database-source-manifest.json"

SAFE_FLAG_NAMES = (
    "DATABASE_URL",
    "DATABASE_MIGRATION_URL",
    "DB_DUAL_WRITE",
    "DATABASE_DUAL_WRITE",
    "DUAL_WRITE",
    "DB_SHADOW_WRITE",
    "DATABASE_SHADOW_WRITE",
)


LOOPBACK_PORT = 15439
CONTAINER_NAME = 'resume-db-review-test'
CONTAINER_PURPOSE = 'resume-account-adapter-review'
CONTAINER_IMAGE = 'postgres:16-alpine'
CONTAINER_NETWORK_MODE = 'bridge'
TMPFS_TARGET = '/var/lib/postgresql/data'
TMPFS_OPTIONS = 'rw,size=128m'
CONTAINER_IDENTITY_KEYS = frozenset({
    'container_id', 'container_name', 'review_session', 'purpose_label',
    'labels', 'image_reference', 'image_id', 'network_mode', 'storage', 'port',
})
CONTAINER_LABEL_KEYS = frozenset({'purpose', 'review-session'})
CONTAINER_STORAGE_KEYS = frozenset({
    'tmpfs_target', 'tmpfs_options', 'persistent_volume_count', 'volume_ids',
    'persistent_volume_absent',
})
CONTAINER_PORT_KEYS = frozenset({
    'container_port', 'host_ip', 'host_port', 'binding_count',
    'host_config_matches', 'network_settings_matches',
})
CONTAINER_REMOVAL_KEYS = frozenset({
    'container_id', 'container_name', 'inspect_absent', 'container_id_absent',
    'port_released',
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(value, dict):
        return None, "top-level JSON value is not an object"
    return value, None


def _load_source_manifest() -> dict:
    try:
        value = json.loads(SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read source manifest: {type(exc).__name__}") from None
    if not isinstance(value, dict) or set(value) != {"format", "source_hash_mode", "files"}:
        raise ValueError("source manifest has an unsupported shape")
    if type(value["format"]) is not int or value["format"] != 1:
        raise ValueError("source manifest format is unsupported")
    if value["source_hash_mode"] != SOURCE_HASH_MODE:
        raise ValueError("source hash mode differs from the preflight contract")
    paths = value["files"]
    if not isinstance(paths, list) or not paths:
        raise ValueError("manifest files must be a non-empty list")
    if any(not isinstance(path, str) or not path or "\\" in path for path in paths):
        raise ValueError("manifest contains an invalid path")
    if len(set(paths)) != len(paths):
        raise ValueError("manifest contains duplicate paths")
    for relative in paths:
        pure = Path(relative)
        if (pure.is_absolute() or pure.as_posix() != relative
                or any(part in {"", ".", ".."} for part in pure.parts)):
            raise ValueError("manifest path escapes the workspace")
        try:
            (ROOT / relative).resolve().relative_to(ROOT)
        except (OSError, ValueError):
            raise ValueError("manifest path escapes the workspace") from None
    if "scripts/database-source-manifest.json" not in paths:
        raise ValueError("manifest must bind its own file")
    return value


def _source_files_from_manifest() -> tuple[str, ...]:
    return tuple(_load_source_manifest()["files"])


def _source_hash_mode_from_manifest() -> str:
    return _load_source_manifest()["source_hash_mode"]


def _source_digest_bytes(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise TypeError("source content must be bytes")
    canonical = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(canonical).hexdigest()


def _valid_git_object_id(value) -> bool:
    return (isinstance(value, str) and len(value) == 40
            and all(char in "0123456789abcdef" for char in value))


def _review_subject_from_data(value: dict) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ValueError("review package must be a JSON object")
    candidate = value.get("candidate_state")
    if not isinstance(candidate, dict):
        raise ValueError("review package is missing candidate_state")
    commit = candidate.get("review_subject_commit")
    tree = candidate.get("review_subject_tree")
    if not _valid_git_object_id(commit) or not _valid_git_object_id(tree):
        raise ValueError("review package must bind full lowercase commit and tree IDs")
    return commit, tree


def _load_review_subject(path: Path) -> tuple[str, str]:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(ROOT)
    except (OSError, ValueError):
        raise ValueError("review package must be an existing file inside the workspace") from None
    value, error = _read_json(resolved)
    if error:
        raise ValueError(f"review package cannot be read: {error}") from None
    return _review_subject_from_data(value)


def _manifest_sha256(manifest: dict) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_manifest_check(value: dict, expected_paths: tuple[str, ...],
                           expected_hash_mode: str) -> tuple[bool, dict]:
    manifest = value.get("source_sha256")
    if not isinstance(manifest, dict):
        return False, {"passed": False, "failures": ["missing source_sha256 object"],
                       "source_hash_mode": value.get("source_hash_mode")}
    expected = set(expected_paths)
    actual = set(manifest)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    invalid = []
    unreadable = []
    mismatches = []
    for relative in expected_paths:
        digest = manifest.get(relative)
        if relative not in manifest:
            continue
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            invalid.append(relative)
            continue
        try:
            path = (ROOT / relative).resolve()
            path.relative_to(ROOT)
        except (OSError, ValueError):
            invalid.append(relative)
            continue
        try:
            observed = _source_digest_bytes(path.read_bytes())
        except OSError:
            unreadable.append(relative)
            continue
        if observed != digest:
            mismatches.append(relative)
    failures = []
    if value.get("source_hash_mode") != expected_hash_mode:
        failures.append("source_hash_mode mismatch")
    if missing:
        failures.append(f"missing {len(missing)} source paths")
    if unexpected:
        failures.append(f"unexpected {len(unexpected)} source paths")
    if invalid:
        failures.append(f"invalid {len(invalid)} source entries")
    if unreadable:
        failures.append(f"unreadable {len(unreadable)} source files")
    if mismatches:
        failures.append(f"{len(mismatches)} source hashes differ")
    details = {
        "passed": not failures,
        "source_hash_mode": value.get("source_hash_mode"),
        "expected_source_hash_mode": expected_hash_mode,
        "required_files": len(expected_paths),
        "verified_files": (len(expected_paths) - len(missing) - len(invalid)
                           - len(unreadable) - len(mismatches)),
        "missing": missing[:10],
        "unexpected": unexpected[:10],
        "invalid": invalid[:10],
        "unreadable": unreadable[:10],
        "mismatches": mismatches[:10],
        "failures": failures,
    }
    return not failures, details


def _loopback_port_check(value: dict) -> tuple[bool, dict]:
    checks = value.get('loopback_port_checks')
    failures = []
    if value.get('loopback_port_released') is not True:
        failures.append('loopback_port_released must be true')
    if not isinstance(checks, dict) or not checks:
        failures.append('missing loopback_port_checks object')
        checks = {}
    observed = {}
    for scope, item in sorted(checks.items()):
        if not isinstance(item, dict):
            failures.append(f'{scope}: invalid loopback port check')
            continue
        observed[scope] = {
            'host': item.get('host'),
            'port': item.get('port'),
            'scope': item.get('scope'),
            'method': item.get('method'),
            'released': item.get('released'),
        }
        if item.get('host') != '127.0.0.1':
            failures.append(f'{scope}: unexpected loopback host')
        if type(item.get('port')) is not int or item.get('port') != LOOPBACK_PORT:
            failures.append(f'{scope}: unexpected loopback port')
        if item.get('scope') != scope:
            failures.append(f'{scope}: scope does not match check key')
        if item.get('method') != PORT_CHECK_METHOD:
            failures.append(f'{scope}: unexpected port check method')
        if item.get('released') is not True:
            failures.append(f'{scope}: loopback port not released')
    if 'local' not in observed:
        failures.append('missing local loopback port check')
    return not failures, {'passed': not failures, 'checks': observed, 'failures': failures}


def _unexpected_keys(value: dict, allowed) -> list[str]:
    return sorted(set(value) - set(allowed))


def _container_identity_check(value: dict) -> tuple[bool, dict]:
    identity = value.get('container_identity')
    removal = value.get('container_removal')
    failures = []
    if not isinstance(identity, dict):
        return False, {'passed': False, 'identity': {},
                       'failures': ['missing container_identity object']}
    if not isinstance(removal, dict):
        failures.append('missing container_removal object')
        removal = {}
    else:
        for key in _unexpected_keys(removal, CONTAINER_REMOVAL_KEYS):
            failures.append(f'unexpected container removal field: {key}')

    for key in _unexpected_keys(identity, CONTAINER_IDENTITY_KEYS):
        failures.append(f'unexpected container identity field: {key}')

    container_id = identity.get('container_id')
    review_session = identity.get('review_session')
    image_id = identity.get('image_id')
    labels_value = identity.get('labels')
    storage_value = identity.get('storage')
    port_value = identity.get('port')
    labels = labels_value if isinstance(labels_value, dict) else {}
    storage = storage_value if isinstance(storage_value, dict) else {}
    port = port_value if isinstance(port_value, dict) else {}
    for name, raw, allowed in (
        ('labels', labels_value, CONTAINER_LABEL_KEYS),
        ('storage', storage_value, CONTAINER_STORAGE_KEYS),
        ('port', port_value, CONTAINER_PORT_KEYS),
    ):
        if not isinstance(raw, dict):
            failures.append(f'container identity {name} must be an object')
        else:
            for key in _unexpected_keys(raw, allowed):
                failures.append(f'unexpected container identity {name} field: {key}')

    if not (isinstance(container_id, str) and len(container_id) == 64
            and all(char in '0123456789abcdef' for char in container_id)):
        failures.append('invalid container id')
    if identity.get('container_name') != CONTAINER_NAME:
        failures.append('unexpected container name')
    if not (isinstance(review_session, str) and len(review_session) == 32
            and all(char in '0123456789abcdef' for char in review_session)):
        failures.append('invalid review session')
    if identity.get('purpose_label') != CONTAINER_PURPOSE:
        failures.append('unexpected purpose label')
    if labels.get('purpose') != CONTAINER_PURPOSE or labels.get('review-session') != review_session:
        failures.append('container labels do not match identity')
    if identity.get('image_reference') != CONTAINER_IMAGE:
        failures.append('unexpected container image reference')
    if not (isinstance(image_id, str) and image_id.startswith('sha256:')
            and len(image_id) == 71
            and all(char in '0123456789abcdef' for char in image_id[7:])):
        failures.append('invalid image id')
    if identity.get('network_mode') != CONTAINER_NETWORK_MODE:
        failures.append('unexpected network mode')

    if storage.get('tmpfs_target') != TMPFS_TARGET:
        failures.append('unexpected tmpfs target')
    if storage.get('tmpfs_options') != TMPFS_OPTIONS:
        failures.append('unexpected tmpfs options')
    if type(storage.get('persistent_volume_count')) is not int or storage.get('persistent_volume_count') != 0:
        failures.append('persistent volume count must be zero')
    if storage.get('volume_ids') != []:
        failures.append('volume id list must be empty')
    if storage.get('persistent_volume_absent') is not True:
        failures.append('persistent volume absence must be true')

    if port.get('container_port') != '5432/tcp':
        failures.append('unexpected container port')
    if port.get('host_ip') != '127.0.0.1':
        failures.append('unexpected container host ip')
    if type(port.get('host_port')) is not int or port.get('host_port') != LOOPBACK_PORT:
        failures.append('unexpected container host port')
    if type(port.get('binding_count')) is not int or port.get('binding_count') != 1:
        failures.append('container binding count must be one')
    if port.get('host_config_matches') is not True:
        failures.append('host config binding mismatch')
    if port.get('network_settings_matches') is not True:
        failures.append('network settings binding mismatch')

    if removal.get('container_id') != container_id:
        failures.append('removal container id mismatch')
    if removal.get('container_name') != CONTAINER_NAME:
        failures.append('removal container name mismatch')
    for field in ('inspect_absent', 'container_id_absent', 'port_released'):
        if removal.get(field) is not True:
            failures.append(f'removal {field} must be true')

    observed = {
        'container_id': container_id, 'container_name': identity.get('container_name'),
        'review_session': review_session, 'image_reference': identity.get('image_reference'),
        'image_id': image_id, 'network_mode': identity.get('network_mode'),
        'storage': storage, 'port': port, 'removal': removal,
    }
    return not failures, {'passed': not failures, 'identity': observed, 'failures': failures}


def _evidence_check(expected_commit=None, expected_tree=None) -> dict:
    files = {}
    failures = []
    manifest_digests = set()
    try:
        expected_paths = _source_files_from_manifest()
        expected_hash_mode = _source_hash_mode_from_manifest()
    except ValueError as exc:
        return {
            "passed": False,
            "source_manifest_files": 0,
            "files": files,
            "failures": [f"source manifest unavailable: {exc}"],
        }
    for name, expected_run in REQUIRED_EVIDENCE.items():
        path = EVIDENCE_DIR / name
        value, error = _read_json(path)
        if error:
            failures.append(f"{name}: {error}")
            continue
        tests = value.get("tests")
        if not isinstance(tests, dict):
            failures.append(f"{name}: missing tests object")
            continue
        run = tests.get("run")
        errors = tests.get("errors")
        test_failures = tests.get("failures")
        skipped = tests.get("skipped")
        if not all(type(item) is int for item in (run, errors, test_failures, skipped)):
            failures.append(f"{name}: tests counters must be JSON integers")
            run = errors = test_failures = skipped = None
        if run != expected_run or errors != 0 or test_failures != 0 or skipped != 0:
            failures.append(
                f"{name}: expected run={expected_run}, errors=0, failures=0, skipped=0; "
                f"got run={run}, errors={errors}, failures={test_failures}, skipped={skipped}"
            )
        if value.get("production_acceptance") is not False:
            failures.append(f"{name}: production_acceptance must remain false")
        if value.get("production_dual_write_enabled") is not False:
            failures.append(f"{name}: production_dual_write_enabled must remain false")
        for field in ('passed', 'source_unchanged', 'temporary_container_removed'):
            if value.get(field) is not True:
                failures.append(f'{name}: {field} must be true')
        source_binding = value.get('source_binding')
        end_binding = value.get('source_binding_at_end')
        for label, binding in (('source_binding', source_binding),
                               ('source_binding_at_end', end_binding)):
            if not isinstance(binding, dict):
                failures.append(f'{name}: missing {label}')
                continue
            for flag in ('passed', 'worktree_matches_head', 'review_subject_matches_head'):
                if binding.get(flag) is not True:
                    failures.append(f'{name}: {label}.{flag} must be true')
            if (not _valid_git_object_id(expected_commit)
                    or not _valid_git_object_id(expected_tree)
                    or binding.get('commit') != expected_commit
                    or binding.get('tree') != expected_tree
                    or binding.get('expected_commit') != expected_commit
                    or binding.get('expected_tree') != expected_tree):
                failures.append(f'{name}: {label} differs from expected review subject')
        manifest_passed, manifest = _source_manifest_check(value, expected_paths, expected_hash_mode)
        source_manifest = value.get("source_sha256")
        if isinstance(source_manifest, dict):
            manifest_digests.add(_manifest_sha256(source_manifest))
        port_passed, port_check = _loopback_port_check(value)
        identity_passed, identity_check = _container_identity_check(value)
        if not port_passed:
            failures.append(
                f'{name}: loopback port check failed: '
                + '; '.join(port_check.get('failures', []))
            )
        if not identity_passed:
            failures.append(
                f'{name}: container identity check failed: '
                + '; '.join(identity_check.get('failures', []))
            )
        if not manifest_passed:
            failures.append(
                f"{name}: source manifest check failed: {'; '.join(manifest.get('failures', []))}"
            )
        files[name] = {
            'loopback_port_check': port_check,
            'container_identity_check': identity_check,
            "scope": value.get("scope"),
            "tests": {key: tests.get(key) for key in ("run", "failures", "errors", "skipped")},
            "production_acceptance": value.get("production_acceptance"),
            "production_dual_write_enabled": value.get("production_dual_write_enabled"),
            "source_hash_mode": value.get("source_hash_mode"),
            "source_manifest": manifest,
            "source_binding": source_binding,
            "source_binding_at_end": end_binding,
        }
    if len(manifest_digests) > 1:
        failures.append("source manifest digests differ across evidence files")
    manifest_sha256 = next(iter(manifest_digests)) if len(manifest_digests) == 1 else None
    return {
        "passed": not failures,
        "source_manifest_files": len(expected_paths),
        "source_manifest_sha256": manifest_sha256,
        "files": files,
        "failures": failures,
    }


def _syntax_check() -> dict:
    checked = 0
    failures = []
    roots = (ROOT / "apps" / "backend", ROOT / "scripts")
    for source_root in roots:
        for path in sorted(source_root.rglob("*.py")):
            # Test fixtures may be owned by another local account.  They are
            # not part of the release source and are intentionally excluded.
            if any(
                part.startswith("auth-state-")
                or part in {"auth-test-temp", ".test-tmp", "__pycache__"}
                for part in path.parts
            ):
                continue
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                checked += 1
            except (OSError, UnicodeError, SyntaxError) as exc:
                failures.append(f"{path.relative_to(ROOT)}: {type(exc).__name__}: {exc}")
    return {"passed": not failures, "files_checked": checked, "failures": failures}


def _git_diff_check() -> dict:
    try:
        proc = subprocess.run(
            ["git", "diff", "--check"], cwd=ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
    except OSError as exc:
        return {"passed": False, "exit_code": None, "output": f"{type(exc).__name__}: {exc}"}
    return {"passed": proc.returncode == 0, "exit_code": proc.returncode, "output": proc.stdout[-4000:]}


def _git_dirty_source_paths(expected_paths: tuple[str, ...]) -> set[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all",
             "--", "apps/backend", "scripts"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout.strip()[-4000:])
    expected = set(expected_paths)
    dirty = set()
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if path in expected or _is_source_path(path):
            dirty.add(path)
    return dirty


def _is_source_path(relative: str) -> bool:
    path = Path(relative)
    if relative.startswith("apps/backend/"):
        return path.suffix.lower() != ".pdf"
    if relative.startswith("scripts/database-drain-review/"):
        return True
    if relative.startswith("scripts/database-"):
        return True
    return False


def _tracked_source_paths() -> set[str]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--", "apps/backend", "scripts"], cwd=ROOT,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    except OSError as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from None
    if proc.returncode != 0:
        raise RuntimeError("git tracked source inventory unavailable")
    return {path for path in proc.stdout.splitlines() if _is_source_path(path)}


def _git_binding(expected_commit=None, expected_tree=None) -> dict:
    def read(*args):
        try:
            proc = subprocess.run(
                ["git", *args], cwd=ROOT, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
        except OSError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if proc.returncode != 0:
            return None, proc.stdout.strip()[-4000:]
        return proc.stdout.strip(), None

    commit, commit_error = read("rev-parse", "HEAD")
    tree, tree_error = read("rev-parse", "HEAD^{tree}")
    failures = []
    if not _valid_git_object_id(expected_commit):
        failures.append("expected review commit is missing or invalid")
    if not _valid_git_object_id(expected_tree):
        failures.append("expected review tree is missing or invalid")
    if commit_error or not commit or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        failures.append("git commit unavailable")
    if tree_error or not tree or len(tree) != 40 or any(c not in "0123456789abcdef" for c in tree):
        failures.append("git tree unavailable")
    if _valid_git_object_id(expected_commit) and commit != expected_commit:
        failures.append("HEAD commit differs from review subject commit")
    if _valid_git_object_id(expected_tree) and tree != expected_tree:
        failures.append("HEAD tree differs from review subject tree")
    dirty = set()
    manifest_paths = ()
    try:
        manifest_paths = _source_files_from_manifest()
        tracked = _tracked_source_paths()
        dirty = _git_dirty_source_paths(manifest_paths)
    except (OSError, RuntimeError, ValueError) as exc:
        failures.append(f"git source status unavailable: {exc}")
        tracked = set()
    unmanifested_tracked = sorted(tracked - set(manifest_paths))
    if unmanifested_tracked:
        failures.append(f"{len(unmanifested_tracked)} tracked source files are missing from the manifest")
    if dirty:
        failures.append(f"{len(dirty)} source files are dirty or unmanifested")
    return {
        "passed": not failures,
        "commit": commit,
        "tree": tree,
        "expected_commit": expected_commit,
        "expected_tree": expected_tree,
        "review_subject_matches_head": (
            _valid_git_object_id(expected_commit) and _valid_git_object_id(expected_tree)
            and commit == expected_commit and tree == expected_tree),
        "worktree_matches_head": not dirty and not any(
            failure.startswith("git source status unavailable") for failure in failures),
        "dirty_source_files": sorted(dirty)[:20],
        "unmanifested_tracked_files": unmanifested_tracked[:20],
        "failures": failures,
    }


def _safe_environment_check() -> dict:
    set_flags = [name for name in SAFE_FLAG_NAMES if os.environ.get(name)]
    return {"passed": not set_flags, "set_flags": set_flags}


def _route_key(path: str) -> str:
    """Normalize Flask converter syntax while preserving route identity."""
    import re
    # Parameter names are implementation details; route shape is what the
    # contract comparison needs (e.g. ``<request_id>`` and ``<identity>``).
    return re.sub(r"<(?:(?:[^:>]+):)?[^>]+>", "{}", path)


def _routes_from_file(path: Path) -> set[tuple[str, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return set()
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            target = call.func if call is not None else decorator
            if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "app" and target.attr in {"get", "post", "put", "patch", "delete", "route"}):
                continue
            if not call or not call.args or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
                continue
            path_value = _route_key(call.args[0].value)
            methods = {target.attr.upper()} if target.attr != "route" else set()
            if target.attr == "route":
                for keyword in call.keywords:
                    if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple, ast.Set)):
                        methods.update(item.value.upper() for item in keyword.value.elts
                                       if isinstance(item, ast.Constant) and isinstance(item.value, str))
            routes.update((method, path_value) for method in methods if method in {"GET", "POST", "PUT", "PATCH", "DELETE"})
    return routes


def _route_coverage_check() -> dict:
    production = _routes_from_file(ROOT / "apps" / "backend" / "app.py")
    preview = set()
    preview_files = [ROOT / "apps" / "backend" / "db_http_preview.py"]
    preview_files.extend(sorted((ROOT / "apps" / "backend").glob("db_*_http.py")))
    for path in preview_files:
        preview.update(_routes_from_file(path))
    missing = sorted({f"{method} {path}" for method, path in production - preview})
    covered = len(production) - len(missing)
    return {
        "production_routes": len(production),
        "preview_routes": len(preview),
        "covered_routes": covered,
        "coverage_percent": round((covered / len(production) * 100), 2) if production else 100.0,
        "missing_routes": missing,
        "passed": not missing,
    }


def _local_health_check() -> dict:
    """Probe only the local development port; never treat it as production health."""
    url = os.environ.get("PREFLIGHT_LOCAL_HEALTH_URL", "http://127.0.0.1:8000/ping")
    try:
        with urllib.request.urlopen(url, timeout=1.0) as response:
            body = response.read(512).decode("utf-8", errors="replace")
            return {"observed": True, "url": url, "status": response.status,
                    "body_shape": "json-or-text", "body_bytes": len(body)}
    except (OSError, urllib.error.URLError, ValueError):
        return {"observed": False, "url": url, "status": None,
                "reason": "local development endpoint unavailable"}


def build_report(*, expected_commit=None, expected_tree=None) -> dict:
    evidence = _evidence_check(expected_commit, expected_tree)
    syntax = _syntax_check()
    diff = _git_diff_check()
    binding = _git_binding(expected_commit, expected_tree)
    environment = _safe_environment_check()
    route_coverage = _route_coverage_check()
    local_health = _local_health_check()
    blockers = [
        "生产运行时仍未接入数据库适配，JSON 继续作为权威存储。",
        "生产持续影子验证、变更日志双写和切换回滚演练尚未验收。",
        "广州 COS 的真实加密异地获取、解密、原件/密钥/应用恢复及生产 RPO/RTO 尚未验收。",
        "SQL 任务恢复仍要求操作员确认 worker 已停止，自动租约失效检测未接入。",
        "本报告是执行者复核；尚无独立第三方代码审查签字。",
    ]
    if not route_coverage['passed']:
        blockers.insert(1, f"仍有 {len(route_coverage['missing_routes'])} 个生产接口缺少 SQL 预览适配；静态覆盖不代表契约验收完成。")
    checks_passed = all(check['passed'] for check in (evidence, syntax, diff, binding, environment))
    source_binding = {
        **binding,
        "source_manifest_files": evidence.get("source_manifest_files"),
        "source_manifest_sha256": evidence.get("source_manifest_sha256"),
    }
    review_subject = {
        "expected_commit": expected_commit,
        "expected_tree": expected_tree,
        "matches_head": binding.get("review_subject_matches_head") is True,
    }
    return {
        "format": 1,
        "date": _utc_now()[:10],
        "scope": "production-release-preflight-read-only",
        "status": "preflight_passed_with_release_blockers" if checks_passed else "preflight_failed",
        "observed_at_utc": _utc_now(),
        "mutations_performed": False,
        "review_subject": review_subject,
        "source_binding": source_binding,
        "checks": {
            "isolated_acceptance_evidence": evidence,
            "python_ast_syntax": syntax,
            "git_diff_check": diff,
            "git_binding": binding,
            "safe_runtime_flags": environment,
            "static_route_coverage": route_coverage,
            "local_health_probe": local_health,
        },
        "production_runtime": {
            "json_authoritative": True,
            "database_cutover": False,
            "dual_write_enabled": False,
            "production_health_claimed": False,
        },
        "release_blockers": blockers,
        "decision": "Do not enable production dual-write, production database writes, or database cutover.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-package", type=Path, required=True,
                        help="Successor review package declaring candidate_state commit/tree")
    parser.add_argument("--output", type=Path, required=True,
                        help="New report path; existing evidence is never overwritten")
    args = parser.parse_args(argv)
    try:
        expected_commit, expected_tree = _load_review_subject(
            args.review_package if args.review_package.is_absolute()
            else ROOT / args.review_package)
    except ValueError as exc:
        parser.error(str(exc))
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output = output.resolve()
    try:
        output.relative_to(ROOT)
    except ValueError:
        parser.error("--output must be inside the workspace")
    if output.exists():
        parser.error("--output already exists; choose a new evidence path")
    report = build_report(expected_commit=expected_commit, expected_tree=expected_tree)
    report["review_package"] = {
        "path": (args.review_package if args.review_package.is_absolute()
                 else ROOT / args.review_package).resolve().relative_to(ROOT).as_posix(),
        "expected_commit": expected_commit,
        "expected_tree": expected_tree,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as report_file:
        report_file.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(output), "status": report["status"],
                      "evidence_passed": report["checks"]["isolated_acceptance_evidence"]["passed"],
                      "syntax_passed": report["checks"]["python_ast_syntax"]["passed"],
                      "git_diff_check_passed": report["checks"]["git_diff_check"]["passed"],
                      "safe_runtime_flags": report["checks"]["safe_runtime_flags"]["passed"]},
                     ensure_ascii=False))
    return 0 if report['status'] != 'preflight_failed' else 1


if __name__ == "__main__":
    sys.exit(main())
