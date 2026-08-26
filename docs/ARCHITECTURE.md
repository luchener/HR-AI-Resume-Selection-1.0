# AI 简历智选 · 项目架构与部署文档

> 版本：1.0 Cloud Deployment Version
> 本文档整理自实际代码，覆盖：项目架构技术栈、登录模块与并发隔离实现、
> 账号数据保存逻辑、腾讯云 Ubuntu 24.04 成功部署步骤。

---

## 目录

1. [项目架构与技术栈](#一项目架构与技术栈)
2. [登录模块与并发隔离技术实现](#二登录模块与并发隔离技术实现)
3. [账号数据保存逻辑结构](#三账号数据保存逻辑结构)
4. [核心 API 接口](#四核心-api-接口)
5. [本地开发与测试](#五本地开发与测试)
6. [Docker 构建与发布](#六docker-构建与发布)
7. [云服务器部署步骤（腾讯云 Ubuntu 24.04）](#七云服务器部署步骤腾讯云-ubuntu-2404)
8. [主要文件职责](#附主要文件职责)

---

## 一、项目架构与技术栈

### 1.1 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│  用户浏览器                                                    │
│  Next.js 15 前端（React 19 + Tailwind CSS 4）                 │
│  登录页 → 工作台 → 分析报告 → Resume Studio 编辑器(/a4cv)       │
└──────────────────────────┬───────────────────────────────────┘
                           │ 同源 /api/*（Next rewrites 转发）
┌──────────────────────────▼───────────────────────────────────┐
│  Nginx 反向代理（可选，生产推荐：80/443 + HTTPS）               │
└──────────────────────────┬───────────────────────────────────┘
┌──────────────────────────▼───────────────────────────────────┐
│  backend 容器 :8000（Gunicorn 4 workers）                     │
│  Flask 3.0 + Python 3.12                                      │
│  ├─ auth.py    JWT 认证 + 用户管理                             │
│  ├─ app.py     路由 + HR 分析编排（ThreadPool 并发）            │
│  ├─ store.py   JSON 文件存储（原子写）                          │
│  ├─ llm.py     OpenAI 兼容 LLM 调用 + JSON 容错解析             │
│  ├─ parser.py  PDF/DOCX 文本提取                               │
│  └─ prompts.py HR 分析 Prompt 模板                             │
└────────────┬──────────────────────────────┬───────────────────┘
             ▼                              ▼
   data/users/  data/resumes/  data/jobs/  日志 logs/backend.log
   （JSON 文件，Docker volume 持久化）
```

### 1.2 技术栈清单

| 层          | 技术                                        | 版本           |
| ----------- | ------------------------------------------- | -------------- |
| 前端框架    | Next.js (App Router)                        | 15.3.0         |
| 前端 UI     | React + Tailwind CSS + lucide-react         | 19 / 4 / 0.501 |
| 后端框架    | Flask                                       | 3.0.*          |
| WSGI 服务器 | Gunicorn                                    | 23.*           |
| LLM 调用    | openai SDK（兼容 DeepSeek 等）              | 1.75.*         |
| 认证        | PyJWT (HS256) + PBKDF2 密码哈希             | 2.10.*         |
| 文档解析    | pdfminer.six + 手写 DOCX(zip+xml) 解析      | 20250327       |
| 存储        | JSON 文件（零数据库依赖）                   | —              |
| 部署        | Docker Compose（backend + frontend 双容器） | —              |

### 1.3 目录结构

```
AIResumeSmartSelection1.0-CloudDeploymentVersion/
├── apps/
│   ├── backend/          # Flask 后端（10 个核心 py 文件）
│   │   ├── auth.py       # JWT、用户、密码和验证码
│   │   ├── app.py        # 路由 + 分析编排
│   │   ├── store.py      # JSON 存储（原子写）
│   │   ├── config.py     # 配置（.env 读取）
│   │   ├── mailer.py     # SMTP HTML/纯文本验证码邮件
│   │   ├── reset_password_cli.py # 管理员重置无邮箱账号
│   │   └── llm.py / parser.py / prompts.py / run.py
│   │       # LLM 调用、文档解析、Prompt、启动辅助
│   │   ├── .env          # 密钥（gitignore，不入库）
│   │   └── Dockerfile
│   └── frontend/         # Next.js 前端
│       ├── app/          # 页面路由（login/dashboard/首页）
│       ├── components/workbench/  # 工作台组件 + auth-context
│       ├── lib/api/      # API 封装（带 JWT 头）
│       ├── public/a4cv/  # 独立简历编辑器
│       └── Dockerfile
├── docker-compose.yml
├── package.json          # 根脚本（build/start/docker:*）
├── .dockerignore
└── docs/                 # 文档
```

### 1.4 运行环境要求

#### 本地开发环境

| 项目     | 要求                                       |
| -------- | ------------------------------------------ |
| 操作系统 | Windows 10/11、macOS、Ubuntu 22.04+        |
| Node.js  | 20 LTS 或更高                              |
| npm/pnpm | npm 10+ 或 pnpm 9+                         |
| Python   | 3.12                                       |
| 内存     | 8 GB 推荐；前端构建建议至少 4 GB 可用内存  |
| 网络     | 可访问 DeepSeek 或其他 OpenAI 兼容 LLM API |

#### 生产环境

推荐腾讯云 Ubuntu Server 24.04 LTS：

- 2 核 4 GB 起步；并发分析推荐 4 核 8 GB；
- 磁盘 40 GB 起步，按简历数量扩容；
- Docker Engine 24+、Docker Compose v2；
- 安全组只开放 22、80、443，3000/8000 不直接暴露公网；
- 域名 A 记录指向服务器公网 IP；
- 已开通 SMTP 服务并取得授权码；
- 服务器可以访问 LLM API。

### 1.5 环境变量

后端配置文件为 `apps/backend/.env`，禁止提交到 Git。

```ini
ENV="production"
SESSION_SECRET_KEY="随机生成的长字符串"
JWT_SECRET_KEY="随机生成的长字符串"
LLM_API_KEY="sk-你的APIKey"
LLM_BASE_URL="https://api.deepseek.com/v1"
LLM_MODEL="deepseek-chat"

# 163 SMTP 示例
SMTP_HOST="smtp.163.com"
SMTP_PORT=465
SMTP_USER="your-account@163.com"
SMTP_PASSWORD="163邮箱授权码"
SMTP_FROM="your-account@163.com"

# 可选配置
RESET_TOKEN_TTL_SECONDS=1800
LOG_DIR="logs"
ALLOWED_ORIGINS=""
GUNICORN_WORKERS=4
NEXT_PUBLIC_API_URL=""
```

生成随机密钥：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`SMTP_PASSWORD` 是邮箱 SMTP 授权码，不是网页登录密码。生产环境禁止使用默认密钥；修改 `JWT_SECRET_KEY` 会令所有旧 JWT 失效；修改 SMTP 配置后必须重启 backend。

---

## 二、登录模块与并发隔离技术实现

### 2.1 JWT 认证流程

```
注册 POST /api/v1/auth/register        登录 POST /api/v1/auth/login
  ├─ 用户名唯一校验（2-64字符）           ├─ PBKDF2 校验密码（200k 迭代）
  ├─ 密码 ≥8 位                           └─ 成功 → 签发 JWT
  ├─ 邮箱验证码校验（先发码再建号）         （HS256，7 天过期）
  ├─ PBKDF2-SHA256 + 随机盐哈希
  └─ 签发 JWT → 返回 {token, user_id}
```

**认证中间件**（`auth.py` 的 `init_auth(app)` 一行注册）：

```python
app = Flask(__name__)
auth_mod.init_auth(app)   # 注册 require_auth (before_request) + _AuthError 错误处理器
```

- 白名单放行：`/ping`、`/api/v1/auth/register`、`/api/v1/auth/login`、`/api/v1/auth/email-code/send`、两个 reset-password 接口
- `require_auth` 内部先放行 OPTIONS（CORS 预检），再校验 Bearer token，失败返回 401
- 校验通过后把 `{user_id, username}` 注入 Flask `g`，供路由取用

**安全细节**：

- 密码哈希：PBKDF2-HMAC-SHA256，**200,000 次迭代 + 16 字节随机盐**，常量时间比较（`hmac.compare_digest`）防时序攻击
- 用户不存在时也执行一次哈希（防用户名枚举的时序侧信道）
- **用户名大小写不敏感**：`admin` / `Admin` / `ADMIN` 视为同一账号 —— 注册查重与登录匹配都统一转小写比较（`create_user` 和 `find_user_by_username`），杜绝因大小写不同产生的重复账号；JWT 用户名保留首次注册时的大小写
- JWT 密钥来自 `.env` 的 `JWT_SECRET_KEY`，production 下必须是随机值（启动强校验）
- 前端 token 存 localStorage，401 时自动清除并跳登录页

### 2.2 多用户数据隔离（核心）

**存储层归属绑定**（`store.py`）：

```python
# 写入：每条简历/JD 都带 user_id
save_resume(content, processed, user_id)   # → data/resumes/<uuid>.json 内含 user_id
save_job(resume_id, content, processed, user_id)

# 读取：user_id 不匹配即视为不存在（返回 None → 前端 404）
get_resume(resume_id, user_id)  # record.get("user_id") != user_id → None
get_job(job_id, user_id)
```

**效果**：用户 A 的 resume_id 被用户 B 猜测/枚举时，返回 **404**（而不是 403/500），不泄露资源是否存在；用户 B 也无法用 A 的 resume_id 上传 JD 或触发分析（同样 404/400）。

**请求级隔离**（`app.py`）：

- `_current_user_id()` 从 `g.auth_user` 取当前用户
- **线程池任务显式传参**：`_run_hr_batch_analysis` 用 `ThreadPoolExecutor(max_workers=3)` 并行分析，worker 线程内**不能访问 Flask 的 `g`**（线程不共享请求上下文），所以 `user_id` 作为参数显式传入每个任务 —— 这是并发实现的关键点。

```python
futures = {
    executor.submit(_run_hr_analysis, resume_id, job_id, user_id, ai_config): resume_id
    for resume_id in pending
}
```

### 2.3 并发安全设计

| 并发点    | 实现                                                      | 说明                                                         |
| --------- | --------------------------------------------------------- | ------------------------------------------------------------ |
| 文件写入  | 原子写：`写 .tmp 临时文件 → fsync → os.replace`           | 断电/多 worker 并发不会产生半写文件                          |
| 多 worker | Gunicorn `-w 4`，每个 worker 独立进程                     | 支持多用户同时分析不排队                                     |
| 批量分析  | `ThreadPoolExecutor(max_workers=3)`，**每个请求内部**并发 | 不是全局共享线程池，请求间天然隔离                           |
| 分析缓存  | `_HR_ANALYSIS_CACHE`（进程内 dict）                       | key 含 `user_id`：`(version, user_id, resume_id, job_id, config_fingerprint)`，杜绝跨用户结果串味 |
| CORS      | `after_request` + `before_request` 预检                   | 同源反代部署留空即可                                         |
| 文件锁    | 原子写天然串行化                                          | 无需额外锁，UUID 文件名避免冲突                              |

### 2.4 前端鉴权实现

| 模块                                    | 职责                                                         |
| --------------------------------------- | ------------------------------------------------------------ |
| `components/workbench/auth-context.tsx` | `AuthProvider`：localStorage 恢复登录态、未登录重定向 `/login`、`login()/logout()`；公开页白名单含 `/login`、`/reset-password` |
| `app/(default)/login/page.tsx`          | 登录/注册页（注册密码≥8位、两次确认、邮箱验证码发送+输入）   |
| `lib/api/screening.ts`                  | 所有 API 调用统一注入 `Authorization: Bearer <token>`；401 统一处理（清 token + 跳登录） |
| `components/workbench/app-shell.tsx`    | 侧边栏显示用户名 + 登出 + 修改密码（弹窗）；登出时清 sessionStorage 分析结果 |
| `app/(default)/reset-password/page.tsx` | 忘记密码：两步（邮箱 → 验证码+新密码）                       |

### 2.5 邮箱验证码机制（注册绑定 + 忘记密码重置，统一实现）

**统一机制**（`auth.py` 的 `email code` 部分）：验证码按**邮箱 + 用途**存储，磁盘只存 SHA-256 哈希。6 位**全大写字母+数字**（排除易混淆 `0O1lI`，`ABCDEFGHJKMNPQRSTUVWXYZ23456789` 36 字符，36^6≈21.8 亿组合）。

**大小写不敏感**：邮件中显示大写；校验时统一先 `.upper()` 再哈希比对，因此用户填写大写/小写/混合均能通过；前端输入框也自动转大写（`onChange` 直接 `.toUpperCase()`），所见即所得。两种用途用常量区分：

- `PURPOSE_EMAIL_VERIFY`（注册绑定邮箱）
- `PURPOSE_RESET_PASSWORD`（忘记密码重置）

```
发码  POST /api/v1/auth/email-code/send (body: {email})       ← 仅注册用
  ├─ 校验邮箱格式 + 未注册 + SMTP 已配置
  ├─ 频率限制：同邮箱 60 秒冷却 → 429
  └─ 生成 6 位码 → 存哈希 → SMTP 发送（mailer.py）

注册  POST /api/v1/auth/register (body: {username,password,email,code})
  ├─ 先消费式校验注册验证码（失败不建号）
  └─ 建号（用户名/邮箱唯一 + 密码强度校验）→ 签发 JWT
```

```
重置第一步  申请 POST /api/v1/auth/reset-password/request (body: {email})
  ├─ find_user_by_email → 生成重置验证码（60秒冷却 → 429）
  ├─ SMTP 发送邮件（mailer.py；未配置 SMTP → 503）
  └─ 无论邮箱是否存在都返回同一文案（防邮箱枚举）

重置第二步  确认 POST /api/v1/auth/reset-password/confirm (body: {email,code,new_password})
  ├─ 校验验证码：哈希匹配 + 未过期 + 未超限（一次失败累计，5 次作废）
  └─ 更新密码：password_version +1 → 用户所有旧 token 自动失效
```

**安全细节**：

- 验证码只存哈希（`data/password_resets/<purpose>.<sha256>.json`），文件泄露无法直接利用
- 30 分钟有效 + **一次性消费**（用后即删，防重放）
- **防爆破三重防线**：① 同邮箱 60 秒发码冷却；② 同一验证码尝试 5 次作废；③ 一次性消费
- SMTP 未配置时申请接口返回 503（明确提示管理员），不假装成功
- 防枚举：未知邮箱返回与真实请求相同的通用文案
- **管理员兜底**：`python reset_password_cli.py <用户名>` 直接为指定用户重置密码（生成临时密码，绕开邮箱验证码，覆盖老账号未绑定邮箱 / SMTP 故障场景）
- 重置成功后旧密码立即失效、所有会话 token 吊销（pwd_ver 机制）

**邮件格式**（`mailer.py`）：`multipart/alternative` 双版本 —— HTML（品牌头 + 32px 大字验证码卡片 + 有效期/防骗提示，内联样式 + table 布局，兼容网易/QQ/Gmail）与纯文本降级版（防垃圾邮件误判、兼容旧客户端）。

---

## 三、账号数据保存逻辑结构

所有数据存 JSON 文件（零数据库），Docker 部署时用 volume 持久化到宿主机。

### 3.1 目录结构

```
data/                          # BASE_DIR/data（启动自动创建）
├── users/                     # 用户账号
│   └── <user_id>.json         # 一个用户一个文件（含邮箱、密码版本号）
├── password_resets/           # 邮箱验证码（注册/重置，只存哈希，30分钟有效）
│   └── <purpose>.<sha256>.json
├── resumes/                   # 简历
│   └── <resume_id>.json
└── jobs/                      # 岗位 JD
    └── <job_id>.json
logs/                          # 日志（.env LOG_DIR 可配置）
└── backend.log
```

### 3.2 用户账号文件结构

```jsonc
// data/users/<uuid>.json
{
  "user_id": "uuid...",
  "username": "alice",            // 唯一，注册时查重
  "password_hash": "base64...",   // PBKDF2-SHA256 哈希值（200k 迭代）
  "password_salt": "base64...",   // 16 字节随机盐
  "created_at": "2026-08-20T16:00:00+00:00"
}
```

> 🔒 **安全说明**：文件里**不存明文密码**，只存 `哈希 + 盐`。即使 data 目录泄露，也无法反推出密码。

### 3.3 简历 / JD 文件结构

```jsonc
// data/resumes/<resume_id>.json
{
  "resume_id": "uuid...",
  "user_id": "uuid...",          // ← 归属字段（多用户隔离的关键）
  "content": "提取出的简历文本...",
  "content_type": "md",
  "created_at": "ISO 时间",
  "processed": {}                // 结构化数据（LLM 解析结果）
}

// data/jobs/<job_id>.json
{
  "job_id": "uuid...",
  "resume_id": "uuid...",        // 关联的简历
  "user_id": "uuid...",          // ← 归属字段
  "content": "JD 文本...",
  "created_at": "ISO 时间",
  "processed": {}                // 本地规则提取的 JD 摘要
}
```

### 3.4 数据生命周期

```
用户注册 → data/users/<uuid>.json（含 PBKDF2 哈希）
上传简历 → 解析 PDF/DOCX → data/resumes/<uuid>.json（带 user_id）
粘贴 JD  → 本地规则摘要 → data/jobs/<uuid>.json（带 user_id + resume_id）
AI 分析 → 结果只存内存缓存（_HR_ANALYSIS_CACHE），不落盘
```

> 分析结果不落盘是**有意的设计**：简历/JD 原始数据持久化，AI 分析结果每次实时生成，避免磁盘膨胀且保证用最新模型配置。

---

## 四、核心 API 接口

API 基础地址：本地为 `http://127.0.0.1:8000`，生产环境通过 `https://你的域名` 同源访问。受保护接口需要：

```http
Authorization: Bearer <JWT>
Content-Type: application/json
```

### 4.1 健康检查

#### `GET /ping`

无需认证。

```json
{"message":"pong","database":"reachable"}
```

### 4.2 注册与登录

#### `POST /api/v1/auth/email-code/send`

发送注册验证码：

```json
{"email":"user@example.com"}
```

成功返回 `200`。`422` 表示邮箱格式错误或已注册，`429` 表示 60 秒冷却中，`503` 表示 SMTP 未配置，`502` 表示 SMTP 发送失败。

#### `POST /api/v1/auth/register`

```json
{
  "username":"alice",
  "password":"StrongPass123",
  "email":"user@example.com",
  "code":"A7K2MP"
}
```

成功返回 `data.user_id`、`data.username`、`data.token`。

#### `POST /api/v1/auth/login`

```json
{"username":"alice","password":"StrongPass123"}
```

用户名大小写不敏感，成功返回 JWT，凭据错误返回 `401`。

#### `GET /api/v1/auth/me`

需要认证，返回当前用户信息。

#### `POST /api/v1/auth/change-password`

```json
{"old_password":"OldPass123","new_password":"NewPass456"}
```

成功后密码版本递增，旧 JWT 失效，客户端需要重新登录。

### 4.3 忘记密码

#### `POST /api/v1/auth/reset-password/request`

```json
{"email":"user@example.com"}
```

未知邮箱与已知邮箱返回统一提示，降低邮箱枚举风险。已绑定邮箱会收到重置验证码。

#### `POST /api/v1/auth/reset-password/confirm`

```json
{
  "email":"user@example.com",
  "code":"A7K2MP",
  "new_password":"NewStrong456"
}
```

验证码填写不区分大小写，成功后所有旧 token 失效。

### 4.4 简历、JD 与分析

以下接口均需要 JWT，具体字段以当前 `app.py` 为准：

| 方法     | 路径                          | 说明                         |
| -------- | ----------------------------- | ---------------------------- |
| `POST`   | `/api/v1/resumes/upload`      | multipart 上传 PDF/DOCX 简历 |
| `GET`    | `/api/v1/resumes`             | 获取当前用户简历             |
| `GET`    | `/api/v1/resumes/<resume_id>` | 获取当前用户指定简历         |
| `DELETE` | `/api/v1/resumes/<resume_id>` | 删除当前用户简历             |
| `POST`   | `/api/v1/jobs/upload`         | 提交 JD，关联简历            |
| `GET`    | `/api/v1/jobs`                | 获取当前用户 JD/任务         |
| `GET`    | `/api/v1/jobs/<job_id>`       | 获取当前用户 JD              |
| `POST`   | `/api/v1/resumes/hr-analysis` | 执行 HR 分析                 |
| `GET`    | `/api/v1/resumes/hr-analysis` | 查询分析结果/缓存结果        |

### 4.5 错误状态码

| 状态码 | 含义                       |
| -----: | -------------------------- |
|    200 | 成功                       |
|    400 | 参数或业务校验失败         |
|    401 | 未认证或 JWT 无效          |
|    404 | 资源不存在或不属于当前用户 |
|    409 | 注册冲突或验证码业务冲突   |
|    422 | 请求字段或格式不合法       |
|    429 | 频率限制                   |
|    502 | SMTP/LLM 外部服务失败      |
|    503 | 服务未配置或暂不可用       |

通用错误格式：

```json
{"detail":"错误说明","request_id":"auth:uuid"}
```

---

## 五、本地开发与测试

### 5.1 后端

```powershell
cd apps/backend
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.sample .env
# 编辑 .env 填写 LLM_API_KEY、JWT_SECRET_KEY 等
python run.py
```

### 5.2 前端

```powershell
cd apps/frontend
npm install
npm run dev
```

### 5.3 测试

```powershell
cd apps/backend
python -m unittest test_hr_analysis
python smoke_test_auth.py

cd ../frontend
npx tsc --noEmit -p tsconfig.json
```

未配置 `LLM_API_KEY` 时，冒烟测试中的 AI 分析部分可能打印预期的模型配置错误；认证、权限和验证码断言仍应通过。

---

## 六、Docker 构建与发布

### 6.1 服务

| 服务       | 容器端口 | 作用                      |
| ---------- | -------: | ------------------------- |
| `backend`  |     8000 | Flask + Gunicorn API      |
| `frontend` |     3000 | Next.js production server |

首次构建：

```bash
cd /opt/resume-matcher-agent-cn
docker compose up -d --build
docker compose ps
docker compose logs -f backend
```

### 6.2 后端构建注意事项

后端 Dockerfile 使用显式 `COPY` 文件列表，新模块必须加入 COPY 行；当前包含 `mailer.py` 和 `reset_password_cli.py`。

腾讯云访问官方 PyPI 可能出现 `ReadTimeoutError`。当前 Dockerfile 使用清华镜像：

```dockerfile
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt
```

### 6.3 更新代码后的正确流程

```bash
cd /opt/resume-matcher-agent-cn

# 改后端
docker compose build backend
docker compose up -d backend

# 改前端
docker compose build frontend
docker compose up -d frontend

# 两端都改
docker compose build backend frontend
docker compose up -d backend frontend
```

仅执行 `docker compose build` 不会自动替换当前运行容器。构建后必须执行对应的 `up -d 服务名`，并检查容器启动时间。

验证 backend 是否包含验证码新版：

```bash
docker exec hr-ai-resume-selection-backend \
  grep -c create_email_code /app/auth.py
```

输出大于等于 1 才说明容器内加载的是新版代码。

### 6.4 前端括号路径

```powershell
tar -cf frontend-patch.tar `
  "apps/frontend/app/(default)/login/page.tsx" `
  "apps/frontend/app/(default)/reset-password/page.tsx"
```

不能把 `(default)` 下文件扁平复制到 `apps/frontend/` 根目录。

---

## 七、云服务器部署步骤（腾讯云 Ubuntu 24.04）

以下为**实战验证过的完整流程**，含踩坑点标注。

### Step 1：服务器准备

- 腾讯云控制台 → 安全组 → 入站规则开放：**22、80、443**（如不用 Nginx 则开放 3000、8000）
- SSH 登录：`ssh ubuntu@你的IP`

### Step 2：安装 Docker

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && exit   # 重新登录使权限生效
```

> ⚠️ **踩坑点**：报 `Cannot connect to the Docker daemon` 时，先 `sudo systemctl start docker && sudo systemctl enable docker`；再不行就是权限问题，**必须重新登录 SSH** 让 docker 组生效。

### Step 3：上传代码（两种方式任选）

**方式 ①：本地打包上传（推荐，自动排除大目录）**

在本地项目根目录（Windows PowerShell）执行：

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

```powershell
# 上传到服务器
scp deploy.tar ubuntu@你的IP:/opt/
```

```bash
# 服务器上解压
cd /opt
mkdir -p AIResumeSmartSelection1.0-CloudDeploymentVersion
tar -xvf deploy.tar -C AIResumeSmartSelection1.0-CloudDeploymentVersion
cd AIResumeSmartSelection1.0-CloudDeploymentVersion
```

> ⚠️ 打包会把本地 `apps/backend/.env`（含密钥）一起带上，部署方便；但**不要外传这个 tar 包**。
> 如果解压后没有 `.env`，按 Step 4 在服务器上重建。

**方式 ②：手动上传到 /opt（WinSCP / FinalShell / scp）**

把项目**手动上传**到服务器的 `/opt/AIResumeSmartSelection1.0-CloudDeploymentVersion` 目录。

> ⚠️ **不要上传这些目录**（本地才有，服务器不需要，上传了也没用且拖慢速度）：
>
> - `node_modules/`（根目录 + `apps/frontend/node_modules/`）— 构建时在服务器自动安装
> - `apps/backend/.venv/` — Python 虚拟环境，构建时在容器里重建
> - `apps/frontend/.next/` — 前端构建产物，构建时重新生成
> - `apps/backend/data/`、`apps/backend/logs/` — 运行时数据
> - `.git/`（如有）

**需要上传的内容**（保持目录结构一致）：

```
apps/backend/               # 后端源码 + Dockerfile + .env
apps/frontend/              # 前端源码 + Dockerfile
docker-compose.yml
package.json
.dockerignore
.gitignore
```

上传后在服务器确认（两种方式都要做）：

```bash
ls /opt/AIResumeSmartSelection1.0-CloudDeploymentVersion
# 期望看到：apps/  docker-compose.yml  package.json  .dockerignore
```

### Step 4：配置 .env（关键）

```bash
cd /opt/AIResumeSmartSelection1.0-CloudDeploymentVersion
ls apps/backend/.env          # 确认 .env 已随上传带上来
# 如果没有 .env（打包/上传时漏了），则：
# cp apps/backend/.env.sample apps/backend/.env
nano apps/backend/.env
```

必改 3 项：`ENV="production"`、`LLM_API_KEY="sk-..."`（DeepSeek）、随机 `SESSION_SECRET_KEY`/`JWT_SECRET_KEY`（用 `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` 生成）。

### Step 5：构建启动

```bash
cd /opt/AIResumeSmartSelection1.0-CloudDeploymentVersion
docker compose up -d --build      # 首次 5-15 分钟
docker compose ps                 # 两个容器 healthy
curl http://127.0.0.1:8000/ping   # → {"database":"reachable","message":"pong"}
```

> ⚠️ **踩坑点**：如果 `docker compose ps` 只有 backend 没有 frontend，或报 `Conflict. The container name ... is already in use`：
>
> ```bash
> docker compose down --remove-orphans
> docker rm -f hr-ai-resume-selection-frontend   # 强制删旧容器
> docker compose up -d
> ```

### Step 6：Nginx 反代 + HTTPS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo nano /etc/nginx/sites-available/resume-matcher
```

```nginx
server {
    listen 80;
    server_name www.luchenstudio.cn;
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 1200s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/resume-matcher /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d www.luchenstudio.cn    # 自动 HTTPS
```

> ⚠️ **踩坑点**：`Internal Server Error` 时，先 `docker compose ps` 确认 frontend 容器存在（90% 是前端容器没起来），再 `docker compose logs backend --tail 80` 看后端日志。

### Step 7：验证

浏览器访问 `https://www.luchenstudio.cn/` → 自动跳登录页 → 注册 → 登录 → 上传简历分析。

---

## 附：多用户改造文件清单

| 文件                                | 变更                                                 |
| ----------------------------------- | ---------------------------------------------------- |
| `apps/backend/auth.py`              | 🆕 新增：JWT 认证 + 用户管理 + 中间件                 |
| `apps/backend/app.py`               | 认证中间件 + auth 路由 + 所有 store 调用传 user_id   |
| `apps/backend/store.py`             | 全部读写加 user_id 归属校验                          |
| `apps/backend/config.py`            | 新增 JWT_SECRET_KEY + production 校验                |
| `apps/backend/requirements.txt`     | + PyJWT                                              |
| `apps/backend/.env` / `.env.sample` | production 配置 + JWT 密钥                           |
| `apps/backend/Dockerfile`           | 复制 auth.py；gunicorn 4 workers                     |
| `apps/frontend/...`                 | 登录页、auth-context、API 鉴权头、移除模型配置、登出 |
| `docker-compose.yml`                | 端口 0.0.0.0；ENV 从 .env 读取                       |
| `package.json` / `.gitignore`       | 构建脚本、忽略测试临时目录                           |

一、基础信息筛选

1. 学历信息 

•	最高学历、院校层次（985/211 / 一本 / 二本 / 专科）、统招 / 非统招
•	专业匹配度：对口 / 相关 / 无关专业
•	毕业时间、是否应届生

2. 年龄、性别（部分岗位参考）、工作所在地、期望薪资
3. 证书资质 

•	硬性必备证书（计算机、财会、教资、行业资格证）
•	加分证书（英语四六级、PMP、软考、技能认证）
•	证书有效期、含金量
二、工作履历硬指标（核心硬性门槛）

1. 总工作年限、相关岗位从业年限
2. 行业匹配：过往行业是否和招聘业务一致
3. 公司背景：大厂 / 上市公司 / 中小企业 / 初创
4. 岗位层级：专员 / 主管 / 经理 / 总监 / 管理岗（带人规模）
5. 跳槽稳定性 

•	每份工作在职时长、短期跳槽（1 年内频繁换工作）
•	空窗期时长、空窗原因合理性

6. 岗位职责重合度：过往工作内容是否覆盖 JD 核心工作

三、专业技能匹配（AI 重点打分维度）

1. 硬性技术栈 / 岗位技能 

•	IT 岗：编程语言、框架、服务器、运维工具、数据库
•	人事行政：招聘、绩效、薪酬、员工关系、OA 系统
•	市场运营：投放、短视频、活动策划、数据分析

2. 工具掌握程度：熟练 / 了解 / 精通
3. 项目经验匹配 

•	项目类型、项目规模、个人负责模块
•	项目成果量化数据（降本、提效、营收、用户量）
•	是否有同行业标杆项目经验

4. 软实力：沟通、统筹、跨部门协作、抗压、执行力

四、综合竞争力加分项

1. 业绩量化成果（带数据成果优先）
2. 获奖荣誉：校内奖、行业奖项、公司绩效评优
3. 实习经历（应届生重点）、校企项目
4. 附加能力：外语、跨部门管理、跨区域项目、独立操盘项目
5. 学习能力：自学新技术、持续进修、在职提升学历

五、简历风险预警维度（HR 淘汰关键，项目必加）

1. 履历断层：长期空窗无合理解释
2. 高频跳槽：1 年 2 份及以上工作，稳定性差
3. 履历造假风险：工作时间冲突、项目逻辑矛盾、学历存疑
4. 能力断层：岗位跨度极大，无过渡经验
5. 薪资预期严重超出岗位预算
6. 地点不符：无法到岗、异地不接受通勤
7. 技能空白：JD 核心要求完全无相关经验
8. 管理矛盾：管理岗无带人经验，简历夸大层级

六、适配招聘岗位的定制化维度（动态字段）

1. 管理岗专属：团队管理人数、预算管控、团队搭建、人才培养经验
2. 技术岗专属：故障排查、架构设计、落地项目、线上运维经验
3. 销售业务岗：业绩指标、客户资源、回款数据、拓客渠道
4. 应届生专属：实习、校园干部、竞赛、毕业设计、校园项目

七、最终综合输出字段（可直接 JSON 结构化存入项目）

1. 整体匹配得分（0-100 分）
2. 核心优势
3. 短板不足
4. 招聘风险点
5. 综合录用建议：优先面试 / 储备观察 / 淘汰
6. 适配岗位标签（高匹配 / 部分匹配 / 不匹配）
7. 简历AI美化程度（轻度/中度/重度）

后端 AI 分析筛选逻辑输出：

- 基础信息筛选：学历、院校层次、学历类型、专业匹配、毕业时间、应届状态、年龄、性别、所在地、期望薪资。
- 证书资质：必备证书、加分证书、有效期和含金量。
- 工作履历硬指标：总年限、相关年限、行业匹配、公司背景、岗位层级、带人规模、跳槽稳定性、空窗期、职责重合度。
- 专业技能匹配：硬技能、工具熟练度、项目匹配点、量化成果、软实力。
- 综合竞争力：业绩、奖项、实习、外语、管理、跨区域项目、学习能力。
- 风险预警：履历断层、高频跳槽、时间冲突、学历存疑、能力断层、薪资/地点不符、技能空白、管理经验夸大。
- 岗位专项判断：管理岗、技术岗、销售业务岗、应届生等专项维度。
- 最终输出：匹配得分、核心优势、短板不足、风险点、招聘建议、适配标签、AI 美化风险。

招聘分析工作台，分区展示：

- 基础信息筛选
- 工作履历硬指标
- 专业技能与项目匹配
- 证书资质
- 岗位专项判断
- 核心优势与加分项
- 短板不足
- 招聘风险预警
- 适配标签和招聘建议
