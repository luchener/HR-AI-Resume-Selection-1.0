#!/bin/sh
# Official postgres entrypoint sources this non-executable file on first init.
# It can also be invoked explicitly with `sh ... grants` after each migration.
set -eu

mode=${1:-init}
case "$mode" in
  # When sourced, positional arguments may be postgres or the init-file list.
  postgres|/docker-entrypoint-initdb.d/*) mode=init ;;
  init|grants) ;;
  *) printf '%s\n' 'Usage: database-init.sh [init|grants]' >&2; exit 2 ;;
esac
: "${POSTGRES_DB:?POSTGRES_DB is required}"
[ "${POSTGRES_USER:-}" = resume_bootstrap_admin ] || {
  printf '%s\n' 'POSTGRES_USER must be the dedicated resume_bootstrap_admin superuser.' >&2
  exit 2
}

if [ "$mode" = init ]; then
  : "${MIGRATOR_PASSWORD:?MIGRATOR_PASSWORD is required}"
  : "${APP_PASSWORD:?APP_PASSWORD is required}"
  : "${READONLY_PASSWORD:?READONLY_PASSWORD is required}"
  [ "$MIGRATOR_PASSWORD" != "$APP_PASSWORD" ] &&
    [ "$MIGRATOR_PASSWORD" != "$READONLY_PASSWORD" ] &&
    [ "$APP_PASSWORD" != "$READONLY_PASSWORD" ] &&
    [ "$MIGRATOR_PASSWORD" != "${POSTGRES_PASSWORD:-}" ] &&
    [ "$APP_PASSWORD" != "${POSTGRES_PASSWORD:-}" ] &&
    [ "$READONLY_PASSWORD" != "${POSTGRES_PASSWORD:-}" ] || {
      printf '%s\n' 'Use four distinct database passwords.' >&2; exit 2;
    }
  # Quoted heredoc: no shell SQL interpolation. Passwords are read from the
  # environment by psql, not passed on its command line. Never enable shell tracing.
  psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 <<'SQL'
\getenv migrator_password MIGRATOR_PASSWORD
\getenv app_password APP_PASSWORD
\getenv readonly_password READONLY_PASSWORD
BEGIN;
SELECT format('CREATE ROLE %I', name)
FROM (VALUES ('resume_migrator'), ('resume_app'), ('resume_readonly')) AS roles(name)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = name) \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L', 'resume_migrator', :'migrator_password') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L', 'resume_app', :'app_password') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L', 'resume_readonly', :'readonly_password') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO resume_migrator, resume_app, resume_readonly', current_database()) \gexec
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO resume_migrator;
GRANT USAGE ON SCHEMA public TO resume_app;
CREATE SCHEMA IF NOT EXISTS reporting AUTHORIZATION resume_bootstrap_admin;
REVOKE ALL ON SCHEMA reporting FROM PUBLIC;
GRANT USAGE ON SCHEMA reporting TO resume_readonly;
-- Object ownership belongs to the migrator; bootstrap remains database owner.
-- Fail closed: future tables (including future audit tables) have no app/reader grants.
ALTER DEFAULT PRIVILEGES FOR ROLE resume_migrator IN SCHEMA public
  REVOKE ALL ON TABLES FROM PUBLIC, resume_app, resume_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE resume_migrator IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM PUBLIC, resume_app, resume_readonly;
-- Function PUBLIC EXECUTE is a global default, so revoke without IN SCHEMA.
ALTER DEFAULT PRIVILEGES FOR ROLE resume_migrator
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, resume_app, resume_readonly;
COMMIT;
SQL
else
  # Run only after migrations, before enabling any runtime connection.
  # Explicit allowlist: no grants to alembic_version or legacy_records.
  psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 <<'SQL'
BEGIN;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, resume_app, resume_readonly;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, resume_app, resume_readonly;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, resume_app, resume_readonly;
REVOKE ALL ON SCHEMA public FROM resume_app, resume_readonly;
GRANT USAGE ON SCHEMA public TO resume_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.users, public.resumes, public.jobs, public.analysis_tasks,
  public.analyses, public.archives, public.quotas, public.system_configs
  TO resume_app;
-- Staged migration tables: grant only these reviewed names when present.
-- UUID/composite-key tables require no sequence privileges. This does not enable adapters.
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO resume_app', 'public', name)
FROM (VALUES ('job_requirements'), ('evidence_assessments'), ('quota_policies'),
             ('quota_daily_usage'), ('quota_reservations'), ('analysis_task_batches'),
             ('analysis_task_inputs'), ('original_gc'), ('auth_limits'), ('auth_challenges'),
             ('auth_invites'), ('auth_login_state'), ('invite_requests')) AS staged(name)
WHERE to_regclass(format('%I.%I', 'public', name)) IS NOT NULL \gexec
-- The change log is opt-in and only receives runtime DML in an explicitly
-- enabled isolated adapter; production JSON has no DATABASE_URL and does not
-- use this table.
SELECT format('GRANT SELECT, INSERT, UPDATE ON TABLE %I.%I TO resume_app', 'public', name)
FROM (VALUES ('change_log'), ('announcements'), ('announcement_dismissals'),
             ('email_images'), ('email_broadcasts'), ('email_broadcast_recipients')) AS staged(name)
WHERE to_regclass(format('%I.%I', 'public', name)) IS NOT NULL \gexec
-- No UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, ownership or DDL for runtime audit access.
GRANT SELECT, INSERT ON public.audit_logs TO resume_app;
SELECT 'GRANT SELECT, INSERT ON public.analysis_task_artifacts TO resume_app'
WHERE to_regclass('public.analysis_task_artifacts') IS NOT NULL \gexec
GRANT USAGE ON SEQUENCE public.audit_logs_id_seq, public.quotas_id_seq TO resume_app;
SELECT 'GRANT SELECT, INSERT ON public.analysis_task_events TO resume_app'
WHERE to_regclass('public.analysis_task_events') IS NOT NULL \gexec
-- Views intentionally exclude passwords/salts, email, free text, JSON payloads,
-- raw resumes, object keys, hashes and system configuration values.
-- Bootstrap owns these views; readonly receives no direct base-table access.
CREATE OR REPLACE VIEW reporting.user_status WITH (security_barrier=true) AS
  SELECT id, status, created_at, deleted_at FROM public.users;
CREATE OR REPLACE VIEW reporting.resume_status WITH (security_barrier=true) AS
  SELECT id, owner_user_id, parse_status, created_at, deleted_at FROM public.resumes;
CREATE OR REPLACE VIEW reporting.analysis_status WITH (security_barrier=true) AS
  SELECT id, owner_user_id, status, final_score, created_at FROM public.analyses;
CREATE OR REPLACE VIEW reporting.audit_events WITH (security_barrier=true) AS
  SELECT id, actor_user_id, action, target_type, created_at FROM public.audit_logs;
REVOKE ALL ON ALL TABLES IN SCHEMA reporting FROM PUBLIC, resume_app, resume_readonly;
GRANT SELECT ON reporting.user_status, reporting.resume_status,
  reporting.analysis_status, reporting.audit_events TO resume_readonly;
COMMIT;
SQL
fi
