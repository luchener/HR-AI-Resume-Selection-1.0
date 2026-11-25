#!/bin/sh
# Read-only live source copy for compatibility preflight, not a quiescent backup.
set -eu
umask 077
work=/tmp/resume-db-source-preflight
if sudo test -e "$work"; then
  echo 'Preflight staging exists; refusing overwrite.' >&2
  exit 2
fi
sudo install -d -m 700 -o ubuntu -g ubuntu "$work"
# Production volume is mounted read-only. No application environment or keys copied.
sudo docker run --rm --network none --memory=128m --cpus=0.5 \
  -v hr-ai-resume-selection_backend-data:/source:ro \
  -v "$work:/snapshot" --entrypoint sh postgres:16-alpine \
  -c 'umask 077; tar -czf /snapshot/source.tar.gz -C /source .; chmod 600 /snapshot/source.tar.gz'
sudo chown ubuntu:ubuntu "$work/source.tar.gz"
echo 'Source snapshot prepared (live-copy consistency only).'
