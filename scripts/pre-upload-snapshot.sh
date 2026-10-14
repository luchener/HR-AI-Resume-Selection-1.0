#!/usr/bin/env bash
# =====================================================================
# 上传前快照（服务器侧，覆盖式部署专用）
#
# 为什么要它：
#   服务器是"tar 覆盖源码 + 事后 scripts/git-snapshot.sh(git add -A)"的模式，
#   git-snapshot.sh 的 add -A 会把**上一次部署的文件**一次性提交掉 —— 也就是说，
#   在它跑之前，服务器仓库里躺着一堆"已上线但还没提交"的变更。
#   这次覆盖一旦出问题，没有快照就只能靠记忆回滚。
#
# 什么时候跑（顺序不能变）：
#   ① 上传/解压之前  ② scripts/git-snapshot.sh 之前  ③ 重建容器之前
#
# 用法：
#   bash scripts/pre-upload-snapshot.sh "deploy: 真实性核验 v3（13 条规则）"
#   可选环境变量：REPO_DIR=/opt/resume-matcher-agent-cn
#
# 产物：$REPO_DIR/.deploy-backup-<时间戳>/（.gitignore 已排除该前缀，不会被下次 add -A 吞掉）
#   repo.bundle          完整提交历史（单文件，可离线 git clone 还原）
#   uncommitted.patch    未提交的已跟踪改动
#   untracked-files.tar.gz  未跟踪的新文件（覆盖式部署的命门）
#   env/                 服务器 .env（含密钥，git 里没有）
#   MANIFEST.txt         这次快照到底记录了哪个状态
#   RESTORE.md           照着念就能回滚的命令
# =====================================================================
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/resume-matcher-agent-cn}"
NOTE="${1:-上传前快照}"
STAMP="$(date +%Y%m%d-%H%M%S)"
SNAP="$REPO_DIR/.deploy-backup-$STAMP"

cd "$REPO_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[中止] $REPO_DIR 不是 git 仓库 —— 无法快照。" >&2
  echo "       请先 git init 并做一次提交，或至少先跑 sudo bash scripts/backup.sh 保住业务数据。" >&2
  exit 1
fi

mkdir -p "$SNAP"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
SHA="$(git rev-parse HEAD)"
DIRTY="$(git status --porcelain | wc -l | tr -d ' ')"
TAG="predeploy-$STAMP"

echo "== 上传前快照 =="
echo "仓库：$REPO_DIR"
echo "分支：$BRANCH  提交：$(git rev-parse --short HEAD)  未提交条目：$DIRTY"
echo "目录：$SNAP"
echo

# ① 先打标签：必须在 bundle 之前，否则 bundle 里没有这个锚点，
#    后面"从 bundle 整体还原"就找不到"上传前"是哪个提交。
git tag -a "$TAG" -m "上传前快照：$NOTE" "$SHA"
echo "[1/5] 已打标签：$TAG（建议顺手 git push origin $TAG 备份到远端）"

# ② 提交历史整体打包（--all 含所有分支与标签，单文件可离线克隆）
git bundle create "$SNAP/repo.bundle" --all
if git bundle verify "$SNAP/repo.bundle" >"$SNAP/bundle-verify.txt" 2>&1; then
  echo "[2/5] 历史打包完成 + 校验通过：repo.bundle（标签 $TAG 已包含在内）"
else
  echo "[2/5] [警告] repo.bundle 校验失败，见 $SNAP/bundle-verify.txt" >&2
fi

# ③ 未提交内容 —— 覆盖式部署模式下上一次部署的文件就在这里
if [ "$DIRTY" -gt 0 ]; then
  git status --porcelain >"$SNAP/status.txt"
  git diff HEAD >"$SNAP/uncommitted.patch"
  git ls-files --others --exclude-standard >"$SNAP/untracked-list.txt"
  if [ -s "$SNAP/untracked-list.txt" ]; then
    if tar -czf "$SNAP/untracked-files.tar.gz" -T "$SNAP/untracked-list.txt" 2>/dev/null; then
      echo "[3/5] 已存未提交改动：uncommitted.patch + untracked-files.tar.gz（$(wc -l <"$SNAP/untracked-list.txt" | tr -d ' ') 个新文件）"
    else
      echo "[3/5] [警告] 未跟踪文件打包部分失败（多为构建产物），清单见 untracked-list.txt" >&2
    fi
  else
    echo "[3/5] 已存未提交改动：uncommitted.patch（无新文件）"
  fi
else
  echo "[3/5] 工作区干净（上一次部署已经提交过），无需 patch"
fi

# ④ 密钥：.gitignore 排除，bundle 里没有，必须单独存
mkdir -p "$SNAP/env"
ENV_COUNT=0
for f in apps/backend/.env apps/frontend/.env.local apps/frontend/.env; do
  if [ -f "$f" ]; then
    install -m 600 "$f" "$SNAP/env/$(echo "$f" | tr '/' '_')"
    ENV_COUNT=$((ENV_COUNT + 1))
  fi
done
echo "[4/5] 已存 $ENV_COUNT 个 .env（权限 600）"

# ⑤ 业务数据（简历/账号在 docker 卷里，git 快照管不到）——复用现成的每日备份脚本
DATA="未备份（docker 或 scripts/backup.sh 不可用）"
if command -v docker >/dev/null 2>&1 && [ -x scripts/backup.sh ]; then
  if sudo -n true 2>/dev/null; then
    if sudo -n bash scripts/backup.sh >/dev/null 2>&1; then
      DATA="已备份 → /opt/backups/resume-data-<时间戳>.tar.gz"
    else
      DATA="备份失败（看 /opt/backups/backup.log）"
    fi
  else
    DATA="需手动执行：sudo bash scripts/backup.sh"
  fi
fi
echo "[5/5] 业务数据：$DATA"

# 清单 + 回滚说明
{
  echo "上传前快照清单"
  echo "时间        ：$(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "说明        ：$NOTE"
  echo "仓库        ：$REPO_DIR"
  echo "分支        ：$BRANCH"
  echo "提交        ：$SHA"
  echo "标签        ：$TAG"
  echo "未提交条目  ：$DIRTY"
  echo "业务数据    ：$DATA"
  echo "bundle 摘要 ：$(sha256sum "$SNAP/repo.bundle" 2>/dev/null | cut -d' ' -f1 || echo 'sha256sum 不可用')"
  echo
  echo "最近 5 次提交："
  git log --oneline -5 | sed 's/^/  /'
} >"$SNAP/MANIFEST.txt"

cat >"$SNAP/RESTORE.md" <<EOF
# 回滚到这次上传之前

快照时间：$(date '+%Y-%m-%d %H:%M:%S')
快照目录：$SNAP
当时提交：$SHA（分支 $BRANCH，标签 $TAG，未提交 $DIRTY 条）

## 1. 源码回到上传前（覆盖式部署模式）

关键点：\`git reset --hard\` **只会还原已跟踪文件**，这次覆盖**新加**的文件它不管，
所以必须配一步 \`git clean -fd\`（\`-f -d\` 不带 \`-x\`，所以 .env / data/ / 快照目录这些
被 .gitignore 忽略的东西**不会**被删）。顺序不能颠倒：先清干净，再放回上传前的内容。

\`\`\`bash
cd $REPO_DIR

# ① 先看看这次覆盖动了哪些文件（心里有数，尤其"新增"的那些）
git status --porcelain

# ② 回到上传前的提交
git reset --hard $SHA

# ③ 清掉这次覆盖新加的文件（忽略文件不受影响）
git clean -fd

# ④ 把"上次部署但还没提交"的部分放回去（有才执行）
[ -s $SNAP/uncommitted.patch ] && git apply $SNAP/uncommitted.patch
[ -f $SNAP/untracked-files.tar.gz ] && tar -xzf $SNAP/untracked-files.tar.gz

# ⑤ 到这里应与你上传前的状态一致（可再 git status 看一眼）
\`\`\`

## 2. 重建容器

\`\`\`bash
cd $REPO_DIR
sudo docker compose -f docker-compose.secure.yml build backend frontend
sudo docker compose -f docker-compose.secure.yml up -d --force-recreate --no-deps backend frontend
curl -s http://127.0.0.1:8000/ping
\`\`\`

## 3. 数据卷（只在数据也坏了时做，会覆盖现有数据）

\`\`\`bash
ls -lt /opt/backups/resume-data-*.tar.gz | head -3        # 先确认要拿哪一份
sudo tar -xzf /opt/backups/resume-data-<时间戳>.tar.gz \\
  -C /var/lib/docker/volumes/hr-ai-resume-selection_backend-data/_data
\`\`\`

## 4. 兜底：从 bundle 整体还原（连历史一起）

\`\`\`bash
git clone $SNAP/repo.bundle /tmp/restored-repo
cd /tmp/restored-repo && git log --oneline -5
# 确认无误后再决定怎么覆盖回 $REPO_DIR
\`\`\`

## 5. 密钥

$SNAP/env/ 下是当时的 .env（权限 600）：
\`\`\`bash
diff $SNAP/env/apps_backend_.env $REPO_DIR/apps/backend/.env
\`\`\`
EOF

chmod 600 "$SNAP"/*.patch "$SNAP"/*.txt "$SNAP"/*.md "$SNAP"/*.bundle 2>/dev/null || true
du -sh "$SNAP" 2>/dev/null | sed 's/^/快照体积：/'

# ⑥ 自检：上面那个 heredoc 是**不带引号**的（要展开 $SNAP/$SHA 这些变量），
#    所以正文里任何**未转义的**反引号都会被 bash 当成命令替换**真的执行**——
#    这不仅会把说明文字打出空洞，更糟的是真的会跑命令
#    （2026-10-14 首次上线时踩过：正文里的 git clean -fd 被真的执行，删掉了一个未跟踪文件）。
#    这里直接查"行内命令名是否原样保留"，把这类事故挡在快照这一步。
if ! grep -qF '关键点：`git reset --hard`' "$SNAP/RESTORE.md"; then
  echo "[自检失败] RESTORE.md 里的行内反引号被命令替换吃掉了。" >&2
  echo "           修法：把 heredoc 正文里的反引号写成 \\\` （已转义的代码围栏不受影响）。" >&2
  echo "           快照目录仍保留：$SNAP（bundle/env/数据都在，只是 RESTORE.md 的说明有洞）" >&2
  exit 1
fi
echo "[自检] RESTORE.md 行内命令名完整保留（heredoc 反引号未被命令替换）"

echo
echo "✅ 快照完成：$SNAP"
echo "   回滚步骤已写进 $SNAP/RESTORE.md"
echo "   下一步才轮到上传解压；最后一步才是 scripts/git-snapshot.sh（它会 git add -A）"
