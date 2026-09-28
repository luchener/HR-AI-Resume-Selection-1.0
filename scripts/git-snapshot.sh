#!/usr/bin/env bash
# 部署后自动 git 快照：在每次部署解压覆盖后调用，把变更记录进服务器本地 git。
# 用法：bash scripts/git-snapshot.sh "deploy: <说明>"
set -euo pipefail

cd /opt/resume-matcher-agent-cn

MESSAGE="${1:-deploy: 更新部署}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "git 仓库未初始化，跳过快照"; exit 0
fi

# 暂存所有变更（.gitignore 已排除 .env/数据/构建产物/备份目录）
git add -A

# 有变更才提交
if git diff --cached --quiet; then
  echo "无变更，跳过提交"
else
  git commit -m "${MESSAGE}" >/dev/null
  echo "已提交: $(git log -1 --format=%h) ${MESSAGE}"
fi