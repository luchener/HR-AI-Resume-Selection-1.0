#!/bin/sh
set -eu
# Run inside the ephemeral review container only. All data is synthetic.
[ "$POSTGRES_DB" = review_test ] || exit 2
# Refuse an existing restore destination rather than replacing any database.
if psql -U review_bootstrap -d review_test -Atc "SELECT 1 FROM pg_database WHERE datname='review_restore'" | grep -q 1; then
  echo 'Restore target already exists; refusing overwrite.' >&2
  exit 2
fi
umask 077
pg_dump -U review_bootstrap -d review_test -Fc -f /tmp/review.dump
createdb -U review_bootstrap review_restore
pg_restore -U review_bootstrap -d review_restore --exit-on-error /tmp/review.dump
psql -U review_bootstrap -d review_restore -v ON_ERROR_STOP=1 <<'SQL'
SELECT count(*) AS restored_tables FROM information_schema.tables WHERE table_schema = 'public';
SELECT version_num AS restored_migration_version FROM alembic_version;
SQL
