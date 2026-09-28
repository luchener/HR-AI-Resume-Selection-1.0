# 部署操作手册（增量部署）

> 说明：项目**不依赖一次性部署脚本**。部署所需的基础设施（docker-compose、Dockerfile、
> git 快照工具）均随仓库/服务器常驻。本手册给出安全、可复现的增量部署步骤。

## 0. 前提（服务器已有）

- 项目目录含 `docker-compose(.secure).yml`、`apps/backend/Dockerfile`、`apps/frontend/Dockerfile(.secure)`
- 服务器本地 git 仓库已初始化（部署后自动快照：`scripts/git-snapshot.sh`）
- 容器可正常构建/运行（`docker compose -f docker-compose.secure.yml ps`）

## 1. 本地：打包变更文件（增量，显式列文件）

**不要打包 `apps/backend/.env`**（密钥单独管理，服务器已有自己的 .env）。

```powershell
cd <项目根目录>
tar -czf deploy-batch.tar.gz \
  apps/backend/xxx.py \
  "apps/frontend/app/(default)/admin/page.tsx" \
  apps/frontend/components/workbench/xxx.tsx
```

## 2. 上传（scp 到服务器家目录，再 sudo 移至项目目录）

```powershell
scp deploy-batch.tar.gz <user>@<server>:~/
ssh <user>@<server> "sudo cp ~/deploy-batch.tar.gz /opt/<项目目录>/ && cd /opt/<项目目录> && sudo tar xzf deploy-batch.tar.gz && rm -f deploy-batch.tar.gz ~/deploy-batch.tar.gz"
```

> 安全要点：
> - **不要从 /tmp 解压**（公共可写目录，防恶意同名 tar 覆盖源码）；从项目目录内解压并随即删除
> - 解压用 `sudo tar` 保持属主一致（root:root）

## 3. 服务器：重建并重启容器

```bash
cd /opt/<项目目录>
sudo docker compose -f docker-compose.secure.yml build backend frontend
sudo docker compose -f docker-compose.secure.yml up -d --force-recreate --no-deps backend frontend
```

## 4. 验证

```bash
curl -s http://127.0.0.1:8000/ping        # backend 存活
docker compose -f docker-compose.secure.yml ps   # 双容器 healthy
```

## 5. 记录变更（git 快照）

```bash
cd /opt/<项目目录> && bash scripts/git-snapshot.sh "deploy: <说明>"
```

## 回滚

git 仓库含每次部署提交：`git checkout <上一提交> -- <文件>` 可恢复被覆盖文件；
或 `git log --oneline` 查看历史后整体回退。

## 一次性脚本策略

如需自动化，可临时生成 `deploy-*.sh`（本地），执行后**立即删除**（本地+服务器），
不保留常驻脚本，避免脚本被篡改后经 sudo 提权。

