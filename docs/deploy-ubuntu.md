# 部署指南：Ubuntu Server 24.04 LTS

本指南适用于在**腾讯云 Ubuntu Server 24.04 LTS 64bit** 上完整部署「AI 简历智选」系统。
采用 Docker Compose 一键部署（推荐），后端 Flask + Gunicorn，前端 Next.js。

---

## 目录

1. [服务器准备](#1-服务器准备)
2. [安装 Docker](#2-安装-docker)
3. [上传项目代码](#3-上传项目代码)
4. [配置环境变量（关键）](#4-配置环境变量关键)
5. [构建并启动](#5-构建并启动)
6. [验证部署](#6-验证部署)
7. [配置 Nginx 反向代理 + HTTPS（推荐）](#7-配置-nginx-反向代理--https推荐)
8. [日常维护](#8-日常维护)
9. [故障排查](#9-故障排查)

---

## 1. 服务器准备

### 1.1 服务器规格建议

| 项目 | 建议 |
|------|------|
| 配置 | 2核 4GB 起步（并发分析建议 4核 8GB） |
| 系统 | Ubuntu Server 24.04 LTS 64bit |
| 带宽 | 1Mbps 起步（页面/API 轻量，够用） |
| 磁盘 | 40GB 起步（简历 PDF/DOCX 会占空间） |

### 1.2 开放安全组端口

登录腾讯云控制台 → 云服务器 → 安全组 → 添加入站规则：

| 端口 | 用途 | 是否必须 |
|------|------|---------|
| 22 | SSH 连接 | 必须 |
| 3000 | 前端页面（Docker 直接暴露） | 用 Nginx 反代后可关 |
| 8000 | 后端 API（Docker 直接暴露） | 用 Nginx 反代后可关 |
| 80 | HTTP（Nginx 反代） | 推荐 |
| 443 | HTTPS（Nginx 反代） | 推荐 |

> **强烈建议**：只开 22/80/443，前端和 API 都走 Nginx 反代（见第 7 节）。
> Docker 的 3000/8000 端口只监听内网即可，或用防火墙挡住公网访问。

### 1.3 SSH 连接服务器

```bash
# 在你自己的电脑上（Windows PowerShell / macOS / Linux 终端）：
ssh root@你的服务器公网IP
```

---

## 2. 安装 Docker

```bash
# 更新系统包
sudo apt update && sudo apt upgrade -y

# 安装 Docker（官方脚本，会自动装 compose 插件）
curl -fsSL https://get.docker.com | sudo sh

# 把当前用户加入 docker 组（免 sudo，重新登录后生效）
sudo usermod -aG docker $USER

# 验证（如果提示权限不足，先退出 SSH 重新登录）
docker --version
docker compose version
```

> 腾讯云镜像源加速（可选，国内拉基础镜像更快）：
> 编辑 `/etc/docker/daemon.json`，写入：
> ```json
> { "registry-mirrors": ["https://docker.m.daocloud.io"] }
> ```
> 然后 `sudo systemctl restart docker`。

---

## 3. 上传项目代码

> 三种方式任选其一：**git clone**（有仓库时最方便）、**本地打包上传**（推荐，自动排除大目录）、**手动上传**（图形工具拖拽）。

### 方式 A：git clone（推荐，如果你有代码仓库）

```bash
cd /opt
git clone <你的仓库地址> AIResumeSmartSelection1.0-CloudDeploymentVersion
cd AIResumeSmartSelection1.0-CloudDeploymentVersion
```

### 方式 B：本地打包上传（tar + scp，推荐）

一条命令自动打包并排除大目录，包体只有 1-3 MB，上传快。

**第 1 步：本地打包**（Windows PowerShell，在项目根目录执行）：

```powershell
cd D:\项目\简历筛选系统\AIResumeSmartSelection1.0-CloudDeploymentVersion

tar -cvf deploy.tar `
  --exclude="node_modules" `
  --exclude=".venv" `
  --exclude=".next" `
  --exclude="data" `
  --exclude="logs" `
  --exclude=".git" `
  apps docker-compose.yml package.json .dockerignore .gitignore
```

> 打包内容：`apps`（前后端源码）、`docker-compose.yml`、`package.json`、`.dockerignore`、`.gitignore`；
> 自动排除：`node_modules`、`.venv`、`.next`、`data`、`logs`、`.git` 等大目录/运行时数据。

**第 2 步：上传到服务器**（还是本地 PowerShell）：

```powershell
scp deploy.tar ubuntu@你的服务器IP:/opt/
```

> 若提示 `scp 不是内部命令`：Windows 设置 → 应用 → 可选功能 → 添加「OpenSSH 客户端」，或用 WinSCP 直接拖 `deploy.tar`。

**第 3 步：服务器上解压**：

```bash
ssh ubuntu@你的服务器IP

cd /opt
mkdir -p AIResumeSmartSelection1.0-CloudDeploymentVersion
tar -xvf deploy.tar -C AIResumeSmartSelection1.0-CloudDeploymentVersion
cd AIResumeSmartSelection1.0-CloudDeploymentVersion

ls          # 应该看到 apps/、docker-compose.yml、package.json
ls apps/backend/   # 确认 .env 在（打包时未排除它）
```

> ⚠️ **注意**：打包会把本地 `apps/backend/.env`（含 API Key 和密钥）一起带上，部署方便；
> 但**不要把这个 tar 包发给别人或传网盘**。如果解压后没有 `.env`，按第 4 节在服务器上重建。

### 方式 C：手动上传到服务器（WinSCP / FinalShell / scp）

用 WinSCP、FinalShell 等图形工具，或 `scp` 命令，把项目**手动上传**到服务器的 `/opt/AIResumeSmartSelection1.0-CloudDeploymentVersion` 目录（在 `/opt` 下新建该文件夹）。

**不需要上传的目录**（本地才有，服务器构建时自动生成，传了反而慢）：

- `node_modules/`（根目录 + `apps/frontend/node_modules/`）
- `apps/backend/.venv/`
- `apps/frontend/.next/`
- `apps/backend/data/`、`apps/backend/logs/`
- `.git/`（如有）

**需要上传的内容**（保持目录结构一致）：

```
apps/backend/               # 后端源码 + Dockerfile + .env
apps/frontend/              # 前端源码 + Dockerfile
docker-compose.yml
package.json
.dockerignore
.gitignore
```

上传后在服务器确认：

```bash
ls /opt/AIResumeSmartSelection1.0-CloudDeploymentVersion
# 期望看到：apps/  docker-compose.yml  package.json  .dockerignore
```

> ⚠️ **重要**：如果 `apps/backend/.env` 没有随上传带上来（本地 .env 含密钥，注意不要放进公开仓库），在服务器上按第 4 节重新创建。

---

## 4. 配置环境变量（关键）

### 4.1 创建后端 .env

在服务器上创建 `apps/backend/.env`：

```bash
cd /opt/AIResumeSmartSelection1.0-CloudDeploymentVersion
cp apps/backend/.env.sample apps/backend/.env
nano apps/backend/.env
```

**必须修改的内容**（其余保持默认即可）：

```ini
# ① 运行环境设为生产
ENV="production"

# ② 填写你的 DeepSeek API Key（必填！没有它 AI 分析无法工作）
LLM_API_KEY="sk-你的真实密钥"

# ③ 生成随机密钥（production 模式启动校验，不能是默认值）
SESSION_SECRET_KEY="<用下面命令生成的随机串>"
JWT_SECRET_KEY="<用下面命令生成的随机串>"
```

生成随机密钥：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # 生成 2 次，分别填 SESSION_SECRET_KEY 和 JWT_SECRET_KEY
```

> 保存：Ctrl+O 回车，Ctrl+X 退出（nano 编辑器）。

### 4.2 前端 .env（可选）

默认配置已可用（同源反代），**无需修改**。前端 `.env` 内容：

```ini
NEXT_PUBLIC_API_URL=""                     # 留空 = 同源相对路径
BACKEND_INTERNAL_URL="http://127.0.0.1:9001"  # 仅本地开发用；Docker 里由 compose 覆盖为 http://backend:8000
```

### 4.3 SMTP 邮件服务（注册邮箱验证码 + 忘记密码重置必需）

注册需发邮箱验证码、忘记密码需发重置验证码，**必须配置 SMTP**，否则相关功能返回 503。在 `apps/backend/.env` 末尾追加：

```ini
# 163 邮箱示例
SMTP_HOST="smtp.163.com"
SMTP_PORT=465
SMTP_USER="你的163邮箱@163.com"
SMTP_PASSWORD="你的163邮箱授权码"
SMTP_FROM="你的163邮箱@163.com"
```

> 🔑 **授权码不是邮箱登录密码**：登录网页版邮箱 → 设置 → POP3/SMTP/IMAP → 开启 SMTP 服务 → 按提示用手机发短信验证 → 复制生成的授权码填入。163 授权码只在开启时显示一次，忘了可重新生成。
>
> 也可用 QQ 邮箱（`smtp.qq.com`，授权码同理在"设置→账户"里生成）。
>
> 可选：`RESET_TOKEN_TTL_SECONDS=1800` 可调整验证码有效期（默认 30 分钟）。

---

## 5. 构建并启动

```bash
cd /opt/AIResumeSmartSelection1.0-CloudDeploymentVersion

# 首次构建（下载依赖 + 编译前端，可能需要 5-15 分钟）
docker compose up -d --build

# 查看启动状态
docker compose ps

# 查看日志（Ctrl+C 退出日志跟踪）
docker compose logs -f backend
docker compose logs -f frontend
```

> **关于构建速度（腾讯云常见坑）**：后端 Dockerfile 默认从 `files.pythonhosted.org` 拉 pip 依赖，腾讯云国际站经常**连接超时**（`Read timed out`）导致构建失败。项目已内置**清华镜像源**（`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple`），构建秒级完成；若你自己改过 Dockerfile 或超时依旧，可临时加 `--build-arg PIP_INDEX_URL` 或直接改第 14 行的 pip 命令。

> **关于重建（重要）**：`docker compose up -d --build` 只会重建"源码有变化"的服务，且**如果新镜像没被用到，容器不一定会被替换**。改了后端代码后，务必确认后端容器已重建：
> ```bash
> docker compose up -d backend          # 强制重新创建后端容器
> docker ps                             # 看 backend 的 UP 时长是否"刚刚"
> ```

**启动成功的标志**：

```
backend 容器状态 healthy（健康检查通过）
frontend 容器状态 healthy
```

> 如果 backend 启动失败，先看日志：
> ```bash
> docker compose logs backend
> ```
> 常见原因见[故障排查](#9-故障排查)。

---

## 6. 验证部署

在服务器上验证：

```bash
# ① 后端健康检查
curl http://127.0.0.1:8000/ping
# 期望输出：{"database":"reachable","message":"pong"}

# ② 发送注册邮箱验证码（验证 SMTP 是否可用；会真实发邮件）
curl -X POST http://127.0.0.1:8000/api/v1/auth/email-code/send \
  -H "Content-Type: application/json" \
  -d '{"email":"你收到的测试邮箱@163.com"}'
# 期望输出：{"detail":"验证码已发送，请查收邮件。"}，且邮箱能收到 6 位验证码
# 若返回 503 "邮件服务未配置"，说明 .env 的 SMTP 配置缺失/有误
```

浏览器访问：

- 直接访问：`http://你的服务器IP:3000`
- 或 Nginx 反代后：`http://你的域名`

**首次使用流程**：
1. 打开页面自动跳转到登录页
2. 点「注册」→ 填用户名/密码/邮箱 → 点「发送验证码」→ 邮箱收到 6 位码 → 填入 → 注册成功
3. 登录后即可上传简历分析（API Key 已在服务器配置，无需前端填写）
4. 忘记密码：登录页点「忘记密码」→ 输邮箱 → 邮箱收 6 位码 → 输码 + 新密码 → 重置成功

---

## 7. 配置 Nginx 反向代理 + HTTPS（推荐）

让用户只通过 80/443 访问，隐藏内部端口，并启用 HTTPS。

### 7.1 安装 Nginx + Certbot

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 7.2 创建 Nginx 配置

```bash
sudo nano /etc/nginx/sites-available/resume-matcher
```

写入（把 `yourdomain.com` 换成你的域名）：

```nginx
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;

    # 前端（Next.js）
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1200s;   # 长分析请求不超时
    }

    # 后端 API（可选，前端 Next.js 已自带 /api 转发；保留则双保险）
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 1200s;
        proxy_buffering off;        # SSE 流式响应不缓冲
    }
}
```

启用并测试：

```bash
sudo ln -s /etc/nginx/sites-available/resume-matcher /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 7.3 自动申请 HTTPS 证书

```bash
# 需要先把域名 A 记录解析到服务器 IP
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com
```

完成后访问 `https://yourdomain.com` 即全站 HTTPS，证书 90 天自动续期（certbot 自带 timer）。

### 7.4 关闭公网直连 Docker 端口（可选，更安全）

腾讯云安全组里**移除** 3000/8000 的入站规则，只保留 22/80/443。
Docker 端口仍在本机监听（Nginx 反代走本机回环），公网无法直接访问。

---

## 8. 日常维护

### 8.1 更新代码重新部署

```bash
cd /opt/AIResumeSmartSelection1.0-CloudDeploymentVersion
git pull                          # 或重新上传代码（tar/scp）

# 重新构建受影响的服务（改哪端构建哪端，节省时间）
docker compose build backend      # 改后端（含邮件、认证）
docker compose build frontend     # 改前端页面

# 关键：强制重建容器，让新镜像生效（否则可能还在跑旧容器！）
docker compose up -d backend
docker compose up -d frontend

# 确认容器确实是"刚重建"的（UP 时长应该是分钟级，不是小时级）
docker ps
```

> ⚠️ **真实踩过的坑**：只执行 `docker compose build` 不会自动替换正在运行的容器；而 `docker compose up -d`（不带 `--build`）只有在镜像 ID 变化时才会重建容器。**改后端后请始终显式 `docker compose up -d backend`**，并用 `docker ps` 确认 UP 时长，否则服务端的 `data` 目录代码再新，容器内跑的还是旧镜像里的旧代码（邮件内容/验证码逻辑都不生效）。

> 验证容器内代码是否真的是新版（后端）：
> ```bash
> docker exec hr-ai-resume-selection-backend sh -c "grep -c create_email_code /app/auth.py"
> # 输出 ≥1 说明是验证码新版；输出 0 说明仍是旧镜像
> ```

### 8.2 查看日志

```bash
docker compose logs -f backend    # 后端日志（含 AI 分析调用记录）
docker compose logs -f frontend   # 前端日志
```

### 8.3 数据备份

数据都存 Docker 卷里（简历/JD/用户 JSON 文件）：

```bash
# 查看卷名
docker volume ls

# 备份数据卷（backend-data 和 backend-logs）
docker run --rm -v hr-ai-resume-selection_backend-data:/data -v /opt/backups:/backup \
  alpine tar czf /backup/backend-data-$(date +%Y%m%d).tar.gz -C /data .

# 建议配合 crontab 定时备份：
# crontab -e 添加：
# 0 2 * * * docker run --rm -v hr-ai-resume-selection_backend-data:/data -v /opt/backups:/backup alpine tar czf /backup/backend-data-$(date +\%Y\%m\%d).tar.gz -C /data .
```

### 8.4 数据迁移（换服务器）

在新服务器启动同版本服务后，把备份解压进卷即可：

```bash
docker run --rm -v hr-ai-resume-selection_backend-data:/data -v /opt/backups:/backup \
  alpine tar xzf /backup/backend-data-XXXXXXXX.tar.gz -C /data
```

### 8.5 管理员紧急重置用户密码

用户忘记密码且未绑定邮箱（老账号）或邮件服务故障时，管理员可在服务器直接重置：

```bash
cd /opt/AIResumeSmartSelection1.0-CloudDeploymentVersion/apps/backend
docker compose exec backend python reset_password_cli.py <用户名>
# 或指定新密码：
docker compose exec backend python reset_password_cli.py --new-password <新密码> <用户名>
```

> 该命令生成/指定一个临时密码，重置后该用户旧会话全部失效，需登录后立即改密。仅供管理员经身份核验后使用。

---

## 9. 故障排查

### 9.1 后端容器启动失败 / unhealthy

```bash
docker compose logs backend
```

常见原因与解决：

| 报错 | 原因 | 解决 |
|------|------|------|
| `SESSION_SECRET_KEY 必须改成随机字符串` | .env 里密钥是默认值 | 按第 4.1 节生成随机密钥 |
| `JWT_SECRET_KEY 必须改成随机字符串` | 同上 | 同上 |
| `LLM_API_KEY 未配置` | .env 未填 API Key | 填上 DeepSeek Key 后 `docker compose restart backend` |
| `ModuleNotFoundError: No module named 'mailer'` | 后端 Dockerfile 的 COPY 漏了新模块 | 确认 Dockerfile 第 17-19 行包含 `mailer.py` 后重新 `docker compose build backend` |
| 构建卡住/失败：`Read timed out`（files.pythonhosted.org） | 腾讯云访问 PyPI 超时 | Dockerfile 已内置清华镜像源；若仍失败检查镜像源配置后重试 |
| 代码改了但行为不变（邮件/验证码还是旧的） | 构建了镜像但容器没重建，仍跑旧容器 | `docker compose up -d backend` 强制重建，`docker ps` 确认 UP 时长 |
| `邮件服务未配置` / 发码/重置返回 503 | .env 未配 SMTP | 按第 4.3 节填 SMTP，`docker compose restart backend` |
| 验证码已注册却说"该邮箱已被注册" | 该邮箱已绑定账号 | 用忘记密码流程或管理员工具处理 |

### 9.2 前端无法登录 / API 404

- 确认 frontend 容器 healthy：`docker compose ps`
- 确认前端能访问后端：`docker exec hr-ai-resume-selection-frontend wget -qO- http://backend:8000/ping`
- 确认浏览器访问的是 3000 端口（或 Nginx 反代路径）

### 9.3 页面加载但分析一直转圈

- 检查 DeepSeek API Key 是否有效：`.env` 的 `LLM_API_KEY`
- 查看后端日志：`docker compose logs -f backend`，看 AI 调用报错
- 腾讯云服务器访问外网 API（DeepSeek）一般无障碍；如被墙可换 `LLM_BASE_URL`

### 9.4 内存不足导致构建失败（OOM）

```bash
# 限制构建内存（可选）
# 在服务器上加 swap（2GB 示例）：
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 9.5 端口被占用

```bash
sudo ss -tlnp | grep -E '3000|8000'    # 查看占用
```

---

## 附：默认端口与架构速查

```
公网(80/443 Nginx)
   │
   ├── :3000  frontend (Next.js 15)  ── /api/* rewrite ──┐
   │                                                      │
   └── :8000  backend (Flask + Gunicorn 4 workers)  ←────┘
                │
                ├── data/users/      用户账号（JSON）
                ├── data/resumes/    简历（JSON）
                ├── data/jobs/       JD（JSON）
                └── logs/            日志
```
