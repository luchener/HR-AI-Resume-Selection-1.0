#!/usr/bin/env bash
# 部署后校验：把 scripts/verify-quota-deployed.py 送进 backend 容器执行（不触碰真实数据）。
# 用法：bash scripts/verify-deployed-quota.sh [ssh-host]
set -euo pipefail
HOST='${1:?用法: bash scripts/verify-deployed-quota.sh <user>@<host>}'
DOCKER='${DOCKER:-sudo docker}'
VOL='/var/lib/docker/volumes/hr-ai-resume-selection_backend-data/_data'
CONTAINER=hr-ai-resume-selection-backend
HERE='$(cd "$(dirname "$0")" && pwd)'

scp -q "$HERE/verify-quota-deployed.py" "$HOST:/home/ubuntu/verify-quota-deployed.py"
ssh "$HOST" "set -e
  $DOCKER cp /home/ubuntu/verify-quota-deployed.py $CONTAINER:/app/data/verify-quota.py 2>/dev/null || sudo cp /home/ubuntu/verify-quota-deployed.py $VOL/verify-quota.py
  set +e
  $DOCKER exec -e PYTHONPATH=/app -w /app $CONTAINER python /app/data/verify-quota.py
  RC=$?
  set -e
  rm -f /home/ubuntu/verify-quota-deployed.py
  exit $RC"
