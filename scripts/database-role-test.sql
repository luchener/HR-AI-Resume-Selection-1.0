-- Development rehearsal only. Run as bootstrap AFTER migrations and grants:
-- psql -X --set=ON_ERROR_STOP=1 --file=database-role-test.sql ...
-- Metadata assertions only: no writes or intentionally failing statements.
BEGIN;
DO $test$
DECLARE
  role_name text;
  object_name text;
BEGIN
  FOREACH role_name IN ARRAY ARRAY['resume_migrator', 'resume_app', 'resume_readonly'] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_roles WHERE rolname = role_name AND rolcanlogin
        AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
        AND NOT rolreplication AND NOT rolbypassrls AND NOT rolinherit
    ) THEN
      RAISE EXCEPTION 'Unexpected role attributes for %', role_name;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
               WHERE r.rolname = role_name) THEN
      RAISE EXCEPTION 'Unexpected role membership for %', role_name;
    END IF;
    IF NOT has_database_privilege(role_name, current_database(), 'CONNECT')
       OR has_database_privilege(role_name, current_database(), 'CREATE')
       OR has_database_privilege(role_name, current_database(), 'TEMP') THEN
      RAISE EXCEPTION 'Unexpected database rights for %', role_name;
    END IF;
  END LOOP;
  IF NOT has_schema_privilege('resume_migrator', 'public', 'CREATE') THEN
    RAISE EXCEPTION 'Migrator cannot create schema objects';
  END IF;
  FOREACH role_name IN ARRAY ARRAY['resume_app', 'resume_readonly'] LOOP
    IF has_schema_privilege(role_name, 'public', 'CREATE')
       OR has_schema_privilege(role_name, 'reporting', 'CREATE') THEN
      RAISE EXCEPTION 'Runtime role has schema CREATE: %', role_name;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               JOIN pg_roles r ON r.oid = c.relowner
               WHERE n.nspname IN ('public', 'reporting') AND r.rolname = role_name) THEN
      RAISE EXCEPTION 'Runtime role owns schema objects: %', role_name;
    END IF;
  END LOOP;
  FOR object_name IN SELECT format('%I.%I', n.nspname, c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  LOOP
    IF has_table_privilege('resume_readonly', object_name, 'SELECT')
       OR has_any_column_privilege('resume_readonly', object_name, 'SELECT') THEN
      RAISE EXCEPTION 'Readonly can read base object %', object_name;
    END IF;
    IF has_table_privilege('resume_app', object_name, 'TRUNCATE,TRIGGER,REFERENCES') THEN
      RAISE EXCEPTION 'App has non-DML table rights on %', object_name;
    END IF;
  END LOOP;
  IF has_table_privilege('resume_app', 'public.audit_logs', 'UPDATE,DELETE,TRUNCATE')
     OR NOT has_table_privilege('resume_app', 'public.audit_logs', 'SELECT')
     OR NOT has_table_privilege('resume_app', 'public.audit_logs', 'INSERT') THEN
    RAISE EXCEPTION 'Audit privileges are not SELECT/INSERT only';
  END IF;
  FOREACH object_name IN ARRAY ARRAY['public.alembic_version', 'public.legacy_records'] LOOP
    IF has_table_privilege('resume_app', object_name, 'SELECT,INSERT,UPDATE,DELETE') THEN
      RAISE EXCEPTION 'App can access migration-only object %', object_name;
    END IF;
  END LOOP;
  FOREACH object_name IN ARRAY ARRAY['reporting.user_status', 'reporting.resume_status',
                                    'reporting.analysis_status', 'reporting.audit_events'] LOOP
    IF NOT has_table_privilege('resume_readonly', object_name, 'SELECT')
       OR has_table_privilege('resume_readonly', object_name, 'INSERT,UPDATE,DELETE,TRUNCATE') THEN
      RAISE EXCEPTION 'Unexpected reporting view privileges: %', object_name;
    END IF;
  END LOOP;
END;
$test$;
ROLLBACK;
