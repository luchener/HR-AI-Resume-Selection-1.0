-- Run as bootstrap against resume_staging only; never transfers database/reporting ownership.
\set ON_ERROR_STOP on
SELECT current_database() = 'resume_staging' AS staging_target \gset
\if :staging_target
BEGIN;
SELECT format('ALTER TABLE %I.%I OWNER TO resume_migrator', n.nspname,c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p')
AND pg_get_userbyid(c.relowner)='resume_bootstrap_admin' \gexec
SELECT format('ALTER SEQUENCE %I.%I OWNER TO resume_migrator', n.nspname,c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind='S'
AND pg_get_userbyid(c.relowner)='resume_bootstrap_admin' \gexec
COMMIT;
SELECT count(*) = 0 AS owners_valid FROM pg_class c
JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p','S')
AND pg_get_userbyid(c.relowner) <> 'resume_migrator' \gset
\if :owners_valid
\echo staging_object_owners_verified
\else
\quit 3
\endif
\else
\echo refusing_non_staging_database
\quit 2
\endif
