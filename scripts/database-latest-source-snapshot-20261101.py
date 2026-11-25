"""Latest production JSON consistency snapshot (2026-11-01 rehearsal).

Preflight is read-only. Snapshot reuses the reviewed drain protocol: maintenance
gate -> pause schedulers -> close admission -> wait drain -> verify exclusive
writer -> clean stop -> copy+verify -> resume admission -> start service ->
health -> remove gate. The public gate stays closed until health is confirmed.

Writes only to the new rehearsal state directory; never touches the 2026-10-16
or 2026-10-30 certificates.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket

STATE = Path(os.environ.get('RESUME_DB_STAGING_DIR', Path.home() / '.resume-workbench' / 'staging')) / 'rehearsal-20261101-v1'
LOCK = Path('/run/lock/resume-json-snapshot-20261101.lock')
REHEARSAL_DB = 'resume_rehearsal_20261101'
REHEARSAL_PORT = 15453
APP_PORT = 18040
FRONTEND_PORT = 13040
SQL_IMAGE = 'resume-candidate-sql-backend:92635d24c6a5-r2'


def reviewed_protocol():
    path = Path(__file__).with_name('database-production-drain.py')
    spec = importlib.util.spec_from_file_location('installed_source_drain', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def port_free(port):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))
    return True


def docker_image_digest(name):
    result = os.popen("docker image inspect --format '{{.Id}}' " + name + " 2>/dev/null").read().strip()
    return result or None


def preflight(protocol):
    if socket.gethostname() != os.environ.get('RESUME_EXPECTED_HOSTNAME'):
        raise ValueError('Unexpected migration host')
    before = protocol.inspect()
    if (not before['State']['Running'] or before['State'].get('Health', {}).get('Status') != 'healthy'
            or before['Config']['Image'] != 'hr-ai-resume-selection-backend:source-drain-20261016'):
        raise ValueError('Unexpected production JSON identity')
    protocol.IMAGE = before['Image']
    environment = dict(item.split('=', 1) for item in before['Config']['Env'] if '=' in item)
    if environment.get('SOURCE_DRAIN_STATE_PATH') != protocol.REGISTRY:
        raise ValueError('Shared source drain is not configured')
    guard = 'if (-f /run/resume-source-maintenance) { return 503; }'
    if protocol.SITE.read_text().count(guard) != 2 or protocol.FLAG.exists():
        raise ValueError('Existing maintenance gate needs inspection')
    status = protocol.control('status')
    active = {key: status[key] for key in ('requests', 'streams', 'workers', 'scheduled', 'commands')}
    if status['draining'] or any(active.values()):
        raise ValueError('Drain registry is not idle')
    if sum(1 for item in status['processes'] if item['exited_at'] is None) != 5:
        raise ValueError('Unexpected registered process population')
    if any(row['outcome'] == 'failed' and row['count'] for row in status['outcomes']):
        raise ValueError('Failed activity needs review')
    consumers = protocol.source_consumers()
    if consumers != [{'container': protocol.BACKEND, 'writable': True}]:
        raise ValueError('Unexpected production source consumer')
    if protocol.external_app_processes():
        raise ValueError('External source writer requires inspection')
    stat = os.statvfs('/opt')
    free_bytes = stat.f_bavail * stat.f_frsize
    if free_bytes < 2 * 1024 ** 3:
        raise ValueError('Insufficient free space for snapshot and rehearsal')
    if STATE.exists():
        raise ValueError('Rehearsal state already exists; inspect it explicitly')
    if docker_image_digest('postgres:16-alpine') is None:
        raise ValueError('PostgreSQL image missing')
    if docker_image_digest(SQL_IMAGE) is None:
        raise ValueError('Reviewed SQL migration image missing')
    existing = os.popen("docker ps -a --format '{{.Names}}'").read().split()
    if 'resume-rehearsal-app' in existing:
        raise ValueError('Rehearsal application container already exists')
    if 'resume-db-rehearsal-20261101' in existing:
        labels = os.popen("docker inspect --format '{{index .Config.Labels \"purpose\"}}' "
                          "resume-db-rehearsal-20261101").read().strip()
        if labels != 'rehearsal-20261101':
            raise ValueError('Unexpected container owns the rehearsal database name')
    for port in (APP_PORT, FRONTEND_PORT):
        port_free(port)
    # 15453 may already be owned by this rehearsal's own PostgreSQL (created before
    # the maintenance window); any other occupant is refused.
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', REHEARSAL_PORT)) == 0:
            names = os.popen("docker ps --filter publish=%d --format '{{.Names}}'" % REHEARSAL_PORT).read().split()
            if names != ['resume-db-rehearsal-20261101']:
                raise ValueError('Rehearsal port is occupied by an unexpected service')
    return {
        'preflight_passed': True,
        'production_image': before['Config']['Image'],
        'production_container_id': before['Id'],
        'production_health': before['State']['Health']['Status'],
        'active': active,
        'registered_processes': 5,
        'source_consumers': consumers,
        'free_bytes': free_bytes,
        'postgres_image': docker_image_digest('postgres:16-alpine'),
        'sql_image': SQL_IMAGE,
        'sql_image_id': docker_image_digest(SQL_IMAGE),
        'rehearsal_database': REHEARSAL_DB,
        'rehearsal_ports': [REHEARSAL_PORT, APP_PORT, FRONTEND_PORT],
        'production_database_cutover': False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'snapshot'])
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run on the reviewed cloud server as root')
    try:
        protocol = reviewed_protocol()
        accepted = preflight(protocol)
        if args.action == 'preflight':
            print(json.dumps(accepted, ensure_ascii=False))
            return 0
        with LOCK.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if STATE.exists():
                raise ValueError('Existing rehearsal state refused')
            STATE.mkdir(mode=0o700)
            protocol.STATE = STATE
            certificate = {
                'rehearsal': '2026-11-01-latest-production',
                'production_image': accepted['production_image'],
                'production_container_id': accepted['production_container_id'],
                'sql_image': accepted['sql_image'],
                'sql_image_id': accepted['sql_image_id'],
                'rehearsal_database': accepted['rehearsal_database'],
                'production_database_cutover': False,
                'created_at': __import__('datetime').datetime.now(
                    __import__('datetime').timezone.utc).isoformat(),
            }
            protocol.write(STATE / 'bootstrapped.json', json.dumps(certificate, indent=2))
            protocol.report('rehearsal-preconditions', accepted)
            protocol.snapshot()
            snapshot = json.loads((STATE / 'snapshot.json').read_text())
            resumed = json.loads((STATE / 'resumed.json').read_text())
            result = {
                'passed': bool(snapshot['production_frozen_snapshot_acceptance']) and resumed['backend_healthy'],
                'snapshot_manifest_sha256': snapshot['snapshot']['manifest_sha256'],
                'snapshot_files': snapshot['snapshot'].get('files'),
                'snapshot_bytes': snapshot['snapshot'].get('bytes'),
                'container_exit': snapshot['container_exit'],
                'maintenance_seconds': snapshot['maintenance_seconds_before_resume'],
                'backend_healthy': resumed['backend_healthy'],
                'public_gate_removed': resumed['public_gate_removed'],
                'production_database_cutover': False,
            }
            protocol.report('rehearsal-snapshot-closeout', result)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['passed'] else 2
    except Exception as error:
        print(json.dumps({'passed': False, 'error_type': type(error).__name__,
                          'detail': str(error)[:200],
                          'production_database_cutover': False,
                          'private_state_retained': STATE.exists()}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
