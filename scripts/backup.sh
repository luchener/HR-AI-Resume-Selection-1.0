#!/bin/bash
# 简历筛选系统 data/ 每日备份：保留最近 7 天，其余自动清理
# 用法：crontab 0 3 * * * /opt/resume-matcher-agent-cn/scripts/backup.sh
set -e
VOL_SRC="/var/lib/docker/volumes/hr-ai-resume-selection_backend-data/_data"
DST=/opt/backups
LOG=/opt/backups/backup.log
STAMP=$(date +%F-%H%M%S)
ts() { date "+%Y-%m-%d %H:%M:%S"; }

mkdir -p "$DST"
if ! sudo test -d "$VOL_SRC"; then
  echo "$(ts) [ERROR] volume $VOL_SRC missing" >> "$LOG"
  exit 1
fi
# 用 sudo 打包卷内容（卷属 root），输出到 ubuntu 可写的备份目录
sudo tar -czf "$DST/resume-data-$STAMP.tar.gz" -C "$VOL_SRC" .
SIZE=$(stat -c %s "$DST/resume-data-$STAMP.tar.gz")
DELETED=$(find "$DST" -name 'resume-data-*.tar.gz' -mtime +7 -delete -print 2>/dev/null | wc -l)
TOTAL=$(du -sm "$DST" | cut -f1)
echo "$(ts) [OK] backup=$STAMP size=$((SIZE/1024))KB total=${TOTAL}MB deleted=$DELETED" >> "$LOG"
