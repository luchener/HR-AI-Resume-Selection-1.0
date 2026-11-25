"""Reuse the installed JSON drain protocol for a new, separately certified copy.

preflight is read-only. snapshot briefly closes admission, waits for accepted work,
stops the registered JSON backend, copies/verifies the source, then resumes it.
No source file is edited and no database/traffic cutover occurs.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket

STAGING = Path(os.environ.get('RESUME_DB_STAGING_DIR', Path.home() / '.resume-workbench' / 'staging'))
STATE = STAGING / 'drain-20261030-v1'
PRIOR = STAGING / 'drain-20261016' / 'bootstrapped.json'


def reviewed_protocol():
    path = Path(__file__).with_name('database-production-drain.py')
    spec = importlib.util.spec_from_file_location('installed_source_drain', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def preflight(protocol):
    if socket.gethostname() != os.environ.get('RESUME_EXPECTED_HOSTNAME'):
        raise ValueError('Unexpected migration host')
    before = protocol.inspect()
    if (not before['State']['Running'] or before['State'].get('Health', {}).get('Status') != 'healthy'
            or before['Config']['Image'] != 'hr-ai-resume-selection-backend:source-drain-20261016'):
        raise ValueError('Unexpected production JSON identity')
    # Helpers use the exact immutable image of the current running service.
    protocol.IMAGE = before['Image']
    environment = dict(item.split('=', 1) for item in before['Config']['Env'] if '=' in item)
    if environment.get('SOURCE_DRAIN_STATE_PATH') != protocol.REGISTRY:
        raise ValueError('Shared source drain is not configured')
    guard = 'if (-f /run/resume-source-maintenance) { return 503; }'
    if protocol.SITE.read_text().count(guard) != 2 or protocol.FLAG.exists():
        raise ValueError('Existing maintenance gate needs inspection')
    previous = json.loads(PRIOR.read_text())
    if previous.get('production_image') != before['Config']['Image']:
        raise ValueError('Previous bootstrap identity mismatch')
    status = protocol.control('status')
    if (status['draining'] or sum(p['exited_at'] is None for p in status['processes']) != 5
            or any(row['outcome'] == 'failed' and row['count'] for row in status['outcomes'])):
        raise ValueError('Registered source activity needs inspection')
    if protocol.source_consumers() != [{'container': protocol.BACKEND, 'writable': True}]:
        raise ValueError('Unexpected production source consumer')
    if protocol.external_app_processes():
        raise ValueError('External source writer requires inspection')
    stat = os.statvfs('/opt')
    if stat.f_bavail * stat.f_frsize < 1024 ** 3:
        raise ValueError('Insufficient free space for a private source copy')
    return {'preflight_passed': True, 'existing_bootstrap_reused': True,
        'previous_bootstrap_sha256': hashlib.sha256(PRIOR.read_bytes()).hexdigest(),
        'production_image_id': before['Image'], 'production_container_id': before['Id'],
        'active': {key: status[key] for key in ('requests', 'streams', 'workers', 'scheduled', 'commands')},
        'registered_processes': 5, 'production_database_cutover': False}


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
            print(json.dumps(accepted))
            return 0
        with Path('/run/lock/resume-json-snapshot-20261030.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if STATE.exists():
                raise ValueError('Existing snapshot state refused; inspect it explicitly')
            STATE.mkdir(mode=0o700)
            protocol.STATE = STATE
            # Keep the real prior deployment certificate intact. This copy is a
            # reference to the still-installed infrastructure, not a new rollout.
            protocol.write(STATE / 'bootstrapped.json', PRIOR.read_bytes())
            protocol.report('existing-bootstrap-reference', accepted)
            protocol.snapshot()
            certificate = json.loads((STATE / 'snapshot.json').read_text())
            resumed = json.loads((STATE / 'resumed.json').read_text())
            result = {'passed': certificate['production_frozen_snapshot_acceptance'] and resumed['backend_healthy'],
                'snapshot': certificate['snapshot'], 'clean_container_exit': certificate['container_exit']['ExitCode'] == 0,
                'initial_activity': certificate['initial_counts'], 'backend_healthy': resumed['backend_healthy'],
                'public_gate_removed': resumed['public_gate_removed'], 'production_database_cutover': False}
            protocol.report('current-source-closeout', result)
            print(json.dumps(result))
            return 0 if result['passed'] else 2
    except Exception as error:
        print(json.dumps({'passed': False, 'error_type': type(error).__name__,
                          'production_database_cutover': False, 'private_state_retained': STATE.exists()}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
