#!/bin/sh
# Explicit synthetic-only role initialization inside the reviewed tmpfs container.
set -eu
[ "${POSTGRES_DB:-}" = review_test ] || exit 2
[ "${POSTGRES_USER:-}" = review_bootstrap ] || exit 2
# Alias the production bootstrap role for this test only, leaving Docker's original
# POSTGRES_USER untouched for cleanup and pg_dump tools.
psql -X -U review_bootstrap -d review_test -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE resume_bootstrap_admin LOGIN SUPERUSER;
SQL
POSTGRES_USER=resume_bootstrap_admin MIGRATOR_PASSWORD=review-migrator-only APP_PASSWORD=review-app-only READONLY_PASSWORD=review-reader-only sh /tmp/database-init.sh init
