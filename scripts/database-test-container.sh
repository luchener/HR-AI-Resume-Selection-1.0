#!/bin/sh
# Isolated synthetic-data test server; never mounts production volumes.
set -eu
name=resume-db-review-test
if sudo docker container inspect "$name" >/dev/null 2>&1; then
  echo 'Test container already exists; inspect it rather than overwrite.' >&2
  exit 1
fi
sudo docker run -d --name "$name" --label purpose=resume-database-review \
  --memory=192m --cpus=0.5 --pids-limit=128 \
  --tmpfs /var/lib/postgresql/data:rw,size=128m \
  -e POSTGRES_USER=review_bootstrap -e POSTGRES_DB=review_test \
  -e POSTGRES_PASSWORD=synthetic-local-review-only \
  -p 127.0.0.1:15439:5432 postgres:16-alpine \
  -c shared_buffers=16MB -c max_connections=15
