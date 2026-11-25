#!/bin/sh
set -eu
# Exact ephemeral target only; inspect identity and mounts before removal.
name=resume-db-review-test
purpose=$(sudo docker inspect --format '{{index .Config.Labels "purpose"}}' "$name")
[ "$purpose" = resume-database-review ] || { echo 'Refusing unexpected container'; exit 2; }
mounts=$(sudo docker inspect --format '{{range .Mounts}}{{.Type}} {{.Destination}}{{println}}{{end}}' "$name")
printf '%s\n' "$mounts"
if printf '%s\n' "$mounts" | grep -E '(^volume |^bind )'; then
  echo 'Refusing removal of a container with persistent mounts' >&2
  exit 2
fi
sudo docker rm -f "$name"
