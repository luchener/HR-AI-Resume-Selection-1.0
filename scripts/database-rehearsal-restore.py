"""Replayable rehearsal restore bound to one PostgreSQL container.

Every operation (create, ACL, restore, grants, checks, cleanup) executes inside the
single container named by --pg-container, so the tool cannot create in one cluster
and restore into another. The container identity, purpose label and published port
are verified before any write. Source DSNs must point at --init-db with the
expected roles; the target DSN is rebuilt from the parsed source URL.

Failures (including timeouts) always attempt cleanup and report the verified result
(dropped / retained / failed); no raw stderr, DSN or command line is printed.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse

TARGET_RE = re.compile(r'resume_sql_preview_[a-z0-9_]{1,40}')
REMOTE_DUMP = '/tmp/rehearsal-restore.dump'
REMOTE_GRANTS = '/tmp/rehearsal-grants.sql'


class StageError(RuntimeError):
    def __init__(self, stage, detail=''):
        super().__init__(stage)
        self.stage = stage
        self.detail = detail


def load_config(run_dir):
    env = {}
    for line in (run_dir / 'migration.env').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            env[key] = value
    deployment = json.loads((run_dir / 'deployment.json').read_text())
    for key in ('DATABASE_MIGRATION_URL', 'DATABASE_URL'):
        if key not in env:
            raise StageError('config', 'missing migration environment key')
    if 'database_url' not in deployment:
        raise StageError('config', 'missing deployment database url')
    return env, deployment


def validate_source(dsn, role, expected_db):
    parsed = urllib.parse.urlsplit(dsn)
    if parsed.scheme not in ('postgresql', 'postgresql+psycopg') or not parsed.hostname:
        raise StageError('config', 'unsupported source DSN')
    if parsed.username != role:
        raise StageError('config', 'source DSN role mismatch')
    if parsed.path.lstrip('/') != expected_db:
        raise StageError('config', 'source DSN does not point at --init-db')
    if parsed.query or parsed.fragment:
        raise StageError('config', 'source DSN query/fragment is not allowed')
    return parsed


def target_dsn(parsed, target_db):
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '/' + target_db, '', ''))


def local_dsn(parsed, target_db):
    """Same credentials, inside the container network namespace on 5432."""
    netloc = parsed.netloc
    credentials = netloc.rsplit('@', 1)[0] if '@' in netloc else ''
    host = '127.0.0.1:5432'
    return urllib.parse.urlunsplit((parsed.scheme, (credentials + '@' if credentials else '') + host,
                                    '/' + target_db, '', ''))


def _docker(args, *, input_text=None, timeout=300):
    return subprocess.run(['docker', *args], input=input_text, capture_output=True,
                          text=True, timeout=timeout)


def _exec(container, script, *, password, args=(), timeout=300):
    return _docker(['exec', '-i', container, 'sh', '-c', script, 'sh', *args],
                   input_text=password + '\n', timeout=timeout)


def inspect_container(name, expected_purpose):
    result = _docker(['inspect', name], timeout=60)
    if result.returncode:
        raise StageError('container', 'container not found')
    payload = json.loads(result.stdout)[0]
    if not payload['State']['Running']:
        raise StageError('container', 'container is not running')
    purpose = payload['Config'].get('Labels', {}).get('purpose')
    if purpose != expected_purpose:
        raise StageError('container', 'container purpose mismatch')
    ports = payload['HostConfig'].get('PortBindings') or {}
    mapping = ports.get('5432/tcp') or []
    host_port = mapping[0].get('HostPort') if mapping else None
    if not host_port:
        raise StageError('container', 'container does not publish PostgreSQL port')
    return {'id': payload['Id'], 'purpose': purpose, 'host_port': host_port}


def verify_privileges(psql, checks, target):
    def q(sql):
        return psql(sql, target)
    checks['schema_version'] = q('SELECT version_num FROM alembic_version;')
    checks['table_owners'] = q("SELECT coalesce(json_agg(DISTINCT tableowner),'[]') "
                               "FROM pg_tables WHERE schemaname='public';")
    checks['sequence_owners'] = q("SELECT coalesce(json_agg(DISTINCT sequenceowner),'[]') "
                                  "FROM pg_sequences WHERE schemaname='public';")
    checks['view_owners'] = q("SELECT coalesce(json_agg(DISTINCT schemaname||':'||viewowner),'[]') "
                              "FROM pg_views WHERE schemaname NOT IN ('pg_catalog','information_schema');")
    checks['app_is_super'] = q("SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles "
                               "WHERE rolname='resume_app';")
    checks['app_public_create'] = q("SELECT has_schema_privilege('resume_app','public','CREATE');")
    checks['app_temp'] = q("SELECT has_database_privilege('resume_app',current_database(),'TEMP');")
    checks['readonly_select_users'] = q("SELECT has_table_privilege('resume_readonly','public.users','SELECT');")
    checks['readonly_insert_users'] = q("SELECT has_table_privilege('resume_readonly','public.users','INSERT');")
    checks['app_users_privileges'] = q(
        "SELECT coalesce(json_agg(DISTINCT privilege_type ORDER BY privilege_type),'[]') "
        "FROM information_schema.role_table_grants WHERE table_schema='public' "
        "AND table_name='users' AND grantee='resume_app';")
    checks['migrator_owns_tables'] = checks['table_owners'] == '["resume_migrator"]'
    checks['migrator_owns_sequences'] = checks['sequence_owners'] == '["resume_migrator"]'
    checks['app_safe'] = (checks['app_is_super'] == 'f' and checks['app_public_create'] == 'f'
                          and checks['app_temp'] == 'f')
    checks['readonly_safe'] = (checks['readonly_insert_users'] == 'f'
                               and checks['readonly_select_users'] == 'f')


def cleanup_target(args, password):
    """Drop this run's target; report dropped only when an existence query proves absence."""
    drop_exit = None
    verify = 'unknown'
    try:
        drop = _exec(args.pg_container,
                     'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -X -h 127.0.0.1 '
                     '-U resume_bootstrap_admin -d postgres -c "DROP DATABASE IF EXISTS ' + args.target_db + '"',
                     password=password, timeout=120)
        drop_exit = drop.returncode
        exists = _exec(args.pg_container,
                       'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -X -h 127.0.0.1 '
                       '-U resume_bootstrap_admin -d postgres -At -c "SELECT 1 FROM pg_database WHERE datname=\''
                       + args.target_db + '\';"', password=password, timeout=60)
        if exists.returncode != 0:
            return {'status': 'failed', 'drop_exit': drop_exit, 'verify': 'query_failed'}
        if exists.stdout.strip() == '':
            verify = 'absent'
            return {'status': 'dropped', 'drop_exit': drop_exit, 'verify': verify}
        verify = 'present'
        return {'status': 'retained', 'drop_exit': drop_exit, 'verify': verify}
    except Exception:
        return {'status': 'failed', 'drop_exit': drop_exit, 'verify': verify}


def run(args, *, run_dir=None):
    report = {'target': args.target_db, 'stage': 'validate', 'checks': {}}
    created = False
    password = ''
    run_dir = run_dir or args.password_file.parent
    try:
        if not TARGET_RE.fullmatch(args.target_db):
            raise StageError('validate', 'target database name is outside the isolated guard')
        if not args.dump.is_file():
            raise StageError('validate', 'dump missing')
        env, deployment = load_config(run_dir)
        migrator = validate_source(env['DATABASE_MIGRATION_URL'], 'resume_migrator', args.init_db)
        app_source = validate_source(deployment['database_url'], 'resume_app', args.init_db)
        container = inspect_container(args.pg_container, args.purpose)
        report.update({'container_id': container['id'], 'purpose': container['purpose'],
                       'host_port': container['host_port'],
                       'dump_sha256': hashlib.sha256(args.dump.read_bytes()).hexdigest(),
                       'dump_bytes': args.dump.stat().st_size})
        password = args.password_file.read_text().strip()

        def psql(sql, database='postgres', timeout=300):
            result = _exec(args.pg_container,
                           'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -X -h 127.0.0.1 '
                           '-U resume_bootstrap_admin -d "$1" -v ON_ERROR_STOP=1 -At -c "$2"',
                           password=password, args=(database, sql), timeout=timeout)
            if result.returncode:
                raise StageError('sql', 'psql failed')
            return result.stdout.strip()

        report['stage'] = 'create'
        if psql("SELECT 1 FROM pg_database WHERE datname='%s';" % args.target_db) == '1':
            raise StageError('create', 'target already exists')
        psql('CREATE DATABASE %s OWNER resume_migrator;' % args.target_db)
        created = True
        psql('REVOKE ALL ON DATABASE %s FROM PUBLIC;' % args.target_db)
        psql('GRANT CONNECT ON DATABASE %s TO resume_migrator, resume_app, resume_readonly;' % args.target_db)
        report['stage'] = 'restore'
        if _docker(['cp', str(args.dump), args.pg_container + ':' + REMOTE_DUMP], timeout=300).returncode:
            raise StageError('restore', 'copy dump failed')
        restore = _exec(args.pg_container,
                        'IFS= read -r PGPASSWORD; export PGPASSWORD; exec pg_restore -h 127.0.0.1 '
                        '-U resume_bootstrap_admin --exit-on-error -d "$1" ' + REMOTE_DUMP,
                        password=password, args=(args.target_db,), timeout=1800)
        _exec(args.pg_container, 'rm -f ' + REMOTE_DUMP, password=password, timeout=30)
        if restore.returncode:
            raise StageError('restore', 'pg_restore failed')
        report['stage'] = 'grants'
        grants = re.findall(r"<<'SQL'\n(.*?)\nSQL", args.init_script.read_text(), re.S)
        if len(grants) != 2:
            raise StageError('grants', 'unexpected init script structure')
        grants_file = run_dir / 'grants-retained.sql'
        grants_file.write_text(grants[1] + '\n')
        os.chmod(grants_file, 0o600)
        if _docker(['cp', str(grants_file), args.pg_container + ':' + REMOTE_GRANTS], timeout=120).returncode:
            raise StageError('grants', 'copy grants failed')
        applied = _exec(args.pg_container,
                        'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -h 127.0.0.1 '
                        '-U resume_bootstrap_admin -v ON_ERROR_STOP=1 -f ' + REMOTE_GRANTS + ' -d "$1"',
                        password=password, args=(args.target_db,), timeout=300)
        _exec(args.pg_container, 'rm -f ' + REMOTE_GRANTS, password=password, timeout=30)
        if applied.returncode:
            raise StageError('grants', 'grants failed')
        report['stage'] = 'verify'
        verify_privileges(psql, report['checks'], args.target_db)
        checks = report['checks']
        if not (checks['migrator_owns_tables'] and checks['migrator_owns_sequences']
                and checks['app_safe'] and checks['readonly_safe']
                and checks['schema_version'] == '0024_task_features'):
            raise StageError('verify', 'ownership or privilege assertion failed')
        report['stage'] = 'migrator-upgrade'
        upgrade = _docker(['run', '--rm', '--network', 'container:' + args.pg_container,
                           '-e', 'DATABASE_URL=' + local_dsn(migrator, args.target_db),
                           '--entrypoint', 'python', args.sql_image, '-m', 'alembic', 'upgrade', 'head'],
                          timeout=300)
        checks['migrator_upgrade_exit'] = upgrade.returncode
        if upgrade.returncode:
            raise StageError('migrator-upgrade', 'alembic upgrade failed')
        report['stage'] = 'app-guard'
        guard = _docker(['run', '--rm', '--network', 'container:' + args.pg_container,
                         '-e', 'DATABASE_URL=' + local_dsn(app_source, args.target_db),
                         '--entrypoint', 'python', args.sql_image, '-c',
                         'from db_security import migration_engine; from db_runtime import verify_runtime; '
                         'import os; engine=migration_engine(os.environ["DATABASE_URL"]); '
                         'print(verify_runtime(engine)); engine.dispose()'], timeout=300)
        checks['app_runtime_guard'] = guard.returncode == 0 and guard.stdout.strip() == 'True'
        if not checks['app_runtime_guard']:
            raise StageError('app-guard', 'runtime guard failed')
        report['passed'] = True
        report['production_database_cutover'] = False
        return finish(args, report, 0)
    except Exception as error:  # includes TimeoutExpired, OSError, config/key errors
        report['passed'] = False
        report['error_type'] = type(error).__name__
        report['stage'] = error.stage if isinstance(error, StageError) else 'unexpected'
        report['production_database_cutover'] = False
        report['cleanup'] = 'none'
        if created:
            cleanup = cleanup_target(args, password)
            report['cleanup'] = cleanup['status']
            report['cleanup_detail'] = {'drop_exit': cleanup.get('drop_exit'),
                                        'verify': cleanup.get('verify')}
        return finish(args, report, 2)


def finish(args, report, code):
    if args.output:
        report['certificate_written'] = True
    text = json.dumps(report, ensure_ascii=False)
    try:
        if args.output:
            args.output.write_text(text + '\n')
    except OSError:
        # The restore itself may have succeeded, but without a certificate the
        # overall run is not passed. The dedicated clone is retained on purpose
        # (never deleted just because evidence writing failed).
        report['certificate_written'] = False
        if report.get('passed'):
            report['restore_passed'] = True
            report['clone_state'] = 'retained'
        report['passed'] = False
        text = json.dumps(report, ensure_ascii=False)
        code = 2
    print(text)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dump', required=True, type=pathlib.Path)
    parser.add_argument('--password-file', required=True, type=pathlib.Path)
    parser.add_argument('--init-script', required=True, type=pathlib.Path)
    parser.add_argument('--target-db', required=True)
    parser.add_argument('--pg-container', required=True)
    parser.add_argument('--purpose', default='rehearsal-20261101')
    parser.add_argument('--sql-image', required=True)
    parser.add_argument('--init-db', default='resume_sql_preview_rehearsal_20261101')
    parser.add_argument('--output', type=pathlib.Path)
    return run(parser.parse_args(argv))


if __name__ == '__main__':
    sys.exit(main())
