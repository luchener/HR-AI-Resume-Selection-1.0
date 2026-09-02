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
8. [简历重点标记模块](#八简历重点标记模块)
9. [主要文件职责](#附主要文件职责)

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

| 层 | 技术 | 版本 |
|----|------|------|
| 前端框架 | Next.js (App Router) | 15.3.0 |
| 前端 UI | React + Tailwind CSS v4 + lucide-react | 19 / 4 / 0.501 |
| 前端导出 | html2canvas（报告图片实时生成 → JPEG 下载，不落盘） | 1.4.1 |
| 前端设计令牌 | Tailwind v4 `@theme` 18 语义色（ink/body/sub/line/brand/good/bad/warn/violet），核心页硬编码色值从 148 种收敛到 6 种 | — |
| 前端阅读逻辑 | 侧栏页内导航（scroll-spy 高亮）、信息去重、collapsible 折叠徽章、可点击排名行 | — |
| 后端框架 | Flask | 3.0.* |
| WSGI 服务器 | Gunicorn | 23.* |
| LLM 调用 | openai SDK（兼容 DeepSeek 等） | 1.75.* |
| 认证 | PyJWT (HS256) + PBKDF2 密码哈希 | 2.10.* |
| 文档解析 | pdfminer.six + 手写 DOCX(zip+xml) 解析 | 20250327 |
| 存储 | JSON 文件（零数据库依赖） | — |
| 部署 | Docker Compose（backend + frontend 双容器） | — |

### 1.3 目录结构

```
AIResumeSmartSelection1.0-CloudDeploymentVersion/
├── apps/
│   ├── backend/          # Flask 后端（15 个核心 py 文件）
│   │   ├── auth.py       # JWT、用户、密码和验证码
│   │   ├── app.py        # 路由 + 分析编排（含 HR 分析 + 简历重点标记）
│   │   ├── store.py      # JSON 存储（原子写，含归档人才库）
│   │   ├── config.py     # 配置（.env 读取）
│   │   ├── mailer.py     # SMTP HTML/纯文本验证码邮件
│   │   ├── reset_password_cli.py # 管理员重置无邮箱账号
│   │   ├── llm.py        # LLM 调用 + JSON 容错解析
│   │   ├── parser.py     # PDF/DOCX 文本提取
│   │   ├── prompts.py    # HR 分析 Prompt 模板
│   │   ├── resume_sanitize.py  # PDF 解析器 ASCII 垃圾过滤
│   │   ├── resume_review.py    # 零 token 简历重点标记生成
│   │   ├── screening_agent.py  # Agent 驱动招聘分析（需求抽取→经验匹配→报告→校验）
│   │   ├── run.py        # 启动辅助
│   │   └── test_*.py     # 单元测试（不进 Docker 镜像，见 .dockerignore）
│   │   ├── .env          # 密钥（gitignore，不入库）
│   │   └── Dockerfile
│   └── frontend/         # Next.js 前端
│       ├── app/          # 页面路由（login/dashboard/首页/archives）
│       ├── app/(default)/css/globals.css  # 全局样式 + @theme 语义色令牌
│       ├── components/workbench/  # 工作台组件 + auth-context
│       │   ├── app-shell.tsx         # 侧栏壳（导航 + 用户 + 版权）
│       │   ├── analysis-workbench.tsx # 首页工作台（上传 + JD + 分析触发）
│       │   ├── analysis-context.tsx  # 分析结果上下文 + 类型定义
│       │   ├── report-export.tsx     # 统一导出中心（图片/PDF/Word）
│       │   ├── resume-review-panel.tsx # 简历重点标记面板
│       │   └── auth-context.tsx      # 登录态管理
│       ├── lib/api/      # API 封装（带 JWT 头，含 archives.ts 归档客户端）
│       └── public/a4cv/  # 独立简历编辑器
│       └── Dockerfile
├── docker-compose.yml
├── package.json          # 根脚本（build/start/docker:*）
├── .dockerignore
└── docs/                 # 文档
```

### 1.4 运行环境要求

#### 本地开发环境

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10/11、macOS、Ubuntu 22.04+ |
| Node.js | 20 LTS 或更高 |
| npm/pnpm | npm 10+ 或 pnpm 9+ |
| Python | 3.12 |
| 内存 | 8 GB 推荐；前端构建建议至少 4 GB 可用内存 |
| 网络 | 可访问 DeepSeek 或其他 OpenAI 兼容 LLM API |

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

| 并发点 | 实现 | 说明 |
|--------|------|------|
| 文件写入 | 原子写：`写 .tmp 临时文件 → fsync → os.replace` | 断电/多 worker 并发不会产生半写文件 |
| 多 worker | Gunicorn `-w 4`，每个 worker 独立进程 | 支持多用户同时分析不排队 |
| 批量分析 | `ThreadPoolExecutor(max_workers=3)`，**每个请求内部**并发 | 不是全局共享线程池，请求间天然隔离 |
| 分析缓存 | `_HR_ANALYSIS_CACHE`（进程内 dict） | key 含 `user_id`：`(version, user_id, resume_id, job_id, config_fingerprint)`，杜绝跨用户结果串味 |
| CORS | `after_request` + `before_request` 预检 | 同源反代部署留空即可 |
| 文件锁 | 原子写天然串行化 | 无需额外锁，UUID 文件名避免冲突 |

### 2.4 前端鉴权实现

| 模块 | 职责 |
|------|------|
| `components/workbench/auth-context.tsx` | `AuthProvider`：localStorage 恢复登录态、未登录重定向 `/login`、`login()/logout()`；公开页白名单含 `/login`、`/reset-password` |
| `app/(default)/login/page.tsx` | 登录/注册页（注册密码≥8位、两次确认、邮箱验证码发送+输入） |
| `lib/api/screening.ts` | 所有 API 调用统一注入 `Authorization: Bearer <token>`；401 统一处理（清 token + 跳登录） |
| `components/workbench/app-shell.tsx` | 侧边栏显示用户名 + 登出 + 修改密码（弹窗）；登出时清 sessionStorage 分析结果 |
| `app/(default)/reset-password/page.tsx` | 忘记密码：两步（邮箱 → 验证码+新密码） |

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
├── jobs/                      # 岗位 JD
│   └── <job_id>.json
└── archives/                  # 归档人才库（纯 JSON，无图片文件）
    ├── <archive_id>.json      # 归档明细（含完整 analysis 快照）
    └── _index.json            # 全局索引（active / trashed 双列表）
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
归档人才库 → data/archives/<archive_id>.json（分析结果快照 + 完整 analysis，无图片）
```

> 分析结果不落盘是**有意的设计**：简历/JD 原始数据持久化，AI 分析结果每次实时生成，避免磁盘膨胀且保证用最新模型配置。

> 归档人才库是**唯一的分析结果落盘点**：归档时把当次完整 `hr_analysis` 写入归档 JSON，供候选人才库长期查看与导出。报告图片**不存储**，查看/导出时由前端用归档里的 `analysis` 实时 html2canvas 生成 JPEG 下载（见 8.6）。

### 3.5 归档人才库文件结构

```jsonc
// data/archives/<archive_id>.json
{
  "archive_id": "uuid...",
  "user_id": "uuid...",             // ← 归属字段（多用户隔离）
  "resume_id": "uuid...",           // 关联简历
  "job_id": "uuid...",              // 关联岗位
  "candidate_name": "苏明远",
  "final_score": 86,
  "fit_tag": "高匹配",
  "recruitment_recommendation": "优先面试",
  "job_title": "AI Agent 工程师",   // JD 自动提取的岗位名
  "category": "IT",                 // 岗位分类：HR 预设/自定义，空则回退 job_title
  "custom_tags": ["AI", "社招"],     // HR 自定义标签
  "analysis_snapshot": { ... },      // 精简摘要（列表页展示）
  "analysis": { ... },              // 完整 hr_analysis（详情页 + 实时导出报告图片）
  "status": "active",               // active | trashed（软删除）
  "trashed_at": null,
  "created_at": "ISO 时间"
}
```

**归档要点**：
- **幂等**：同一 `(user_id, resume_id, job_id)` 重复归档返回已有记录，不重复建号。
- **不存图片**：无 `image_path` 字段、无 `archive_images/` 目录。报告图片由前端用 `analysis` 实时生成。
- **`analysis` 仅在详情接口返回**（`GET /archives/<id>`），列表接口只返回 `analysis_snapshot`，避免响应过大。
- **简历标记要点不进归档**：导出报告图片时经 `review-markers` 接口实时生成（零 Token，见第八章）。

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

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/resumes/upload` | multipart 上传 PDF/DOCX 简历 |
| `GET` | `/api/v1/resumes` | 获取当前用户简历 |
| `GET` | `/api/v1/resumes/<resume_id>` | 获取当前用户指定简历 |
| `DELETE` | `/api/v1/resumes/<resume_id>` | 删除当前用户简历 |
| `POST` | `/api/v1/jobs/upload` | 提交 JD，关联简历 |
| `GET` | `/api/v1/jobs` | 获取当前用户 JD/任务 |
| `GET` | `/api/v1/jobs/<job_id>` | 获取当前用户 JD |
| `POST` | `/api/v1/resumes/hr-analysis` | 执行 HR 分析（单份或批量） |
| `GET` | `/api/v1/resumes/hr-analysis` | 查询分析结果/缓存结果 |
| `GET` | `/api/v1/resumes` | 获取简历内容（含自动 ASCII 垃圾过滤） |
| `POST` | `/api/v1/resumes/review-markers` | 生成简历重点标记（匹配亮点/岗位匹配/待核实/学历待核实） |

#### 归档人才库接口

以下接口均需要 JWT，`archive_id` 资源均校验归属（跨用户返回 404）：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/archives` | 幂等创建归档（`resume_id` + `job_id` 唯一），携带 `category` 与完整 `analysis` |
| `GET` | `/api/v1/archives` | 人才库列表（active），支持 `name` / `job_title` / `category` / `tag` 筛选与 `sort` 排序；`meta` 返回 `job_titles` / `categories` |
| `GET` | `/api/v1/archives/trash` | 回收站列表（trashed） |
| `GET` | `/api/v1/archives/<archive_id>` | 归档详情（含完整 `analysis`，供导出报告图片） |
| `PATCH` | `/api/v1/archives/<archive_id>/tags` | 更新自定义标签 |
| `PATCH` | `/api/v1/archives/<archive_id>/category` | 更新岗位分类 |
| `DELETE` | `/api/v1/archives/<archive_id>` | 移入回收站（软删除） |
| `POST` | `/api/v1/archives/<archive_id>/restore` | 从回收站恢复 |
| `DELETE` | `/api/v1/archives/trash/<archive_id>` | 回收站内彻底删除单条 |
| `DELETE` | `/api/v1/archives/trash` | 清空回收站 |

> 归档报告图片**无独立上传/下载接口**：图片不落盘，由前端用归档详情里的 `analysis` 实时生成 JPEG 下载（见 8.6）。

### 4.5 错误状态码

| 状态码 | 含义 |
|---:|---|
| 200 | 成功 |
| 400 | 参数或业务校验失败 |
| 401 | 未认证或 JWT 无效 |
| 404 | 资源不存在或不属于当前用户 |
| 409 | 注册冲突或验证码业务冲突 |
| 422 | 请求字段或格式不合法 |
| 429 | 频率限制 |
| 502 | SMTP/LLM 外部服务失败 |
| 503 | 服务未配置或暂不可用 |

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

| 服务 | 容器端口 | 作用 |
|---|---:|---|
| `backend` | 8000 | Flask + Gunicorn API |
| `frontend` | 3000 | Next.js production server |

首次构建：

```bash
cd /opt/resume-matcher-agent-cn
docker compose up -d --build
docker compose ps
docker compose logs -f backend
```

### 6.2 后端构建注意事项

后端 Dockerfile 使用 **通配符 `COPY apps/backend/*.py ./`** 复制所有 `.py` 文件，新增模块自动出现在构建上下文中，无需手动修改 COPY 列表。

**`.dockerignore` 排除后端测试文件**（`test_*.py`、`smoke_test_*.py`、`*.log`），测试文件不进生产镜像。

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

> ⚠️ **已知问题（containerd 网络层挂起）**：本服务器（腾讯云 Ubuntu 24.04）偶发容器网络命名空间挂起，表现为 `127.0.0.1:3000` 连接超时（nginx 504）但容器内进程正常启动。连 `robots.txt` 静态路由都超时即属此问题。处置：`docker compose up -d --force-recreate frontend` 强制重建网络命名空间，约 10-20 秒恢复。若 backend 也挂起：`sudo systemctl restart docker` + `docker compose up -d`（全容器重启，中断约 1-2 分钟）。

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

---

## 八、简历重点标记模块

### 8.1 功能概述

基于已有 HR 分析结果，在原简历正文上以**高亮标记**方式标注四类信息，不修改原简历内容，纯辅助审阅用途：

| 标记类型 | 颜色 | 来源 | 说明 |
|---|---|---|---|
| `strength` | 绿色 | `strengths` | 匹配亮点：核心优势与岗位影响 |
| `match` | 蓝色 | `skill_match.project_match_points` / `hard_skills` | 岗位匹配：项目或技能与 JD 的对应 |
| `risk` | 红色 | `risk_points` | 待核实：需面试或材料复核 |
| `verify` | 黄色 | 简历原文 + `education_history` | 学历待核实：无法从简历本身验证，建议背调 |

### 8.2 架构与数据流

```
┌──────────┐     ┌──────────────┐     ┌──────────────────┐
│ 用户请求  │ ──→ │ review-markers │ ──→ │ build_review_markers │
│ POST     │     │ 路由          │     │ （零 token）      │
│          │     │              │     │                  │
│          │     │ fetchResume  │     │ ① 获取简历原文    │
│          │     │ + fetchHR    │     │ ② 提取分析结果    │
│          │     │              │     │ ③ 关键词匹配定位   │
│          │     │              │     │ ④ 返回标注列表    │
└──────────┘     └──────────────┘     └──────────────────┘
                                              │
                                              ▼
                                     ┌──────────────────┐
                                     │ 前端渲染面板       │
                                     │                  │
                                     │ 高亮原文          │
                                     │ 综合得分 / 建议    │
                                     │ 标记清单表格      │
                                     │ 导出：图片/PDF/Word│
                                     └──────────────────┘
```

### 8.3 实现细节

#### 简历内容清洗（`resume_sanitize.py`）

PDF 解析器（pdfminer）会在简历正文前后注入大量 ASCII 二进制垃圾：单字符碎片（`Bi`, `B`）、稀疏 ASCII 串（`B 4 0 9 y-F F d S x o m 6 W`）、Base64/MD5 哈希（`e6887c80f740582c1HB409y-FFdSxom6W`）。`sanitize_resume_content()` 通过正则逐行过滤：

| 模式 | 正则 | 示例 |
|---|---|---|
| 短 ASCII | `^[A-Za-z0-9 _\-]{1,2}$` | `Bi`, `B`, `N` |
| 稀疏 ASCII | `^([A-Za-z0-9\-]{1,4})( [A-Za-z0-9\-]{1,4}){1,}$` | `B 4 0 9 y-F F d S x o m 6 W` |
| 长二进制 | `^[A-Za-z0-9+/=_\-]{15,}$` | `e6887c80f740582c1HB409y-FFdSxom6W` |

清洗后仅保留中文、电话号码、邮箱等有效内容。

#### 标记生成（`resume_review.py` → `build_review_markers`）

**零 token 设计**：不依赖任何 LLM 调用，通过关键词匹配将分析结果映射到简历原文位置：

```python
def build_review_markers(content, analysis, *, candidate_name):
    annotations = []
    # 从分析结果中取出各类文本
    _add_annotation(annotations, content, "strength", ..., analysis["strengths"])
    _add_annotation(annotations, content, "match",  ..., skill_match["project_match_points"])
    _add_annotation(annotations, content, "match",  ..., skill_match["hard_skills"])
    _add_annotation(annotations, content, "risk",   ..., analysis["risk_points"])
    _add_annotation(annotations, content, "verify", ..., _find_education_refs(content, education_history))
    return {"candidate_name", "annotations", "summary", "notice"}
```

`_find_quote()` 用全文搜索 + 分词回退策略定位原文中的确切位置（`start`, `end`），确保所有标记引用均可在简历原文中追溯。

#### Agent 分析驱动

`screening_agent.run_screening_agent()` 执行 5 步 Agent 流程：

```
① 需求抽取    _extract_requirements         → 提取岗位要求清单（1 次 LLM）
② 经验匹配    _find_resume_experiences      → 纯关键词匹配（0 次 LLM）
③ 报告生成    _call_json(_report_prompt)    → 生成 JSON 报告（1 次 LLM）
④ 报告校验    _validate_report              → 校验 + 必要时重试（0~1 次 LLM）
⑤ 自校        _self_reflect                 → 分层：预检通过即跳过；疑点触发深度核查（0~2 次 LLM）
```

**自校步骤**（`_self_reflect`）为分层设计：先跑确定性预检 `_precheck_findings`（0 次 LLM），预检干净且 `ai_risk` 非 medium/high 时直接通过（跳过 LLM）；预检发现疑点或报告自报美化风险时，才发起 LLM 深度核查（包含简历原文摘录 `experiences` 作为论断依据，预算门槛 `budget["calls"] < MAX_AGENT_LLM_CALLS - 1`，`MAX_AGENT_LLM_CALLS=5`）。深度核查发现违反项则用修正提示重新生成报告，**修订稿必须重新过 `_validate_report` 结构校验，不通过则回退保留原报告**：

| 规则编号 | 规则名称 | 示例 |
|---|---|---|
| 1 | 论断是否超出简历依据 | 简历写"了解 Python"，报告断言"精通 Python" |
| 2 | 评分依据是否可追溯 | 扣分 15 分（中度 AI 美化），但未引用具体句子 |
| 3 | 加分项是否与岗位相关 | PMP 证书对纯前端岗列为加分项 |
| 4 | 缺失项是否标注"未提供" | 简历未提及薪资，报告写成"符合预期" |
| 5 | 风险判断是否区分"风险"和"未体现" | 简历未提及空窗期，报告写"无空窗期" |

**预算分配**（`MAX_AGENT_LLM_CALLS = 5`，单份简历含需求抽取）：

| 场景 | 调用次数 |
|---|---|
| 干净报告（预检通过，跳过深度核查） | 1(抽)+1(生成) = **2** |
| 无重试有修订 | 1(抽)+1(生成)+1(自校)+1(修订) = **4** |
| 有重试无修订 | 1(抽)+1(生成)+1(重试)+1(自校) = **4** |
| 有重试有修订 | 1(抽)+1(生成)+1(重试)+1(自校)+1(修订) = **5** |

批量模式下需求已预提取（见 8.4），干净报告每份仅需 **1** 次调用。

### 8.4 批量分析 LLM 调用优化

**核心问题**：同一 JD 分析 3 份简历时，`_extract_requirements`（仅依赖 `job_content`）被重复调用 3 次，浪费 2 次 LLM 调用。

**优化方案**：`_run_hr_batch_analysis()` 在进入线程池之前预提取一次需求，将结果传入每个并发的分析任务：

```python
# 预提取一次（共享 JD）
precomputed_requirements = screening_agent._extract_requirements(job_content, ai_config, _req_budget)

# 并发分析时传入预提取结果，跳过重复抽取
futures = {
    executor.submit(
        _run_hr_analysis, resume_id, job_id, user_id, ai_config,
        precomputed_requirements=precomputed_requirements
    ): resume_id
    for resume_id in pending
}
```

| 指标 | 优化前（无自校） | 优化后（含自校） | 降幅 |
|---|---|---|---|
| 调用次数（无重试） | 3×2=6 | 1+3×2=7 | 预提取节省 2 次（相对无自校） |
| 调用次数（全部重试） | 3×3=9 | 1+3×3=10 | 预提取节省 2 次（相对无自校） |
| Token 消耗 | ~14600 | ~12000 | — |

单份分析路径不受影响（`precomputed_requirements=None` 时走原有逻辑）。

### 8.5 HR 分析输出字段

#### `basic_screening`（基础信息筛选）

| 字段 | 说明 |
|---|---|
| `native_place` | 籍贯 |
| `age` | 年龄 |
| `gender` | 性别 |
| `work_location` | 工作所在地 |
| `salary_expectation` | 期望薪资 |

#### `education_history`（教育经历数组，按学历从高到低排序）

| 字段 | 说明 |
|---|---|
| `degree` | 学历（博士/硕士/本科/专科/其他/未提供） |
| `school_name` | 学校全称 |
| `school_tier` | 层次（985/211/一本/二本/专科/其他） |
| `major` | 学习专业名称（简历原文，不做匹配判断） |
| `graduation_year` | 毕业时间 |

**排序逻辑**（`_normalize_education_history`）：博士 > 硕士 > 本科 > 专科 > 大专 > 其他。**全段保留**：Prompt 规则要求报告所有教育经历，前端无 `[:8]` 截断（commit 8ded42a 移除原限制，经历列表超出 8 项时仍完整展示）。

### 8.6 统一导出中心

**位置**：`components/workbench/report-export.tsx`（默认导出 `ReportExportCenter`）。

三个按钮位于报告页指标卡下方，导出一份 HTML 文档模型 + 三种格式，**无服务端依赖**：

| 按钮 | 技术 | 说明 |
|------|------|------|
| **导出图片** | `html2canvas` 隐藏 offscreen div → 2x 缩放 → JPEG（q0.9）下载 | 最稳定，直接下载 |
| **打印 PDF** | 隐藏 iframe `doc.write` + `win.print()` | 用户选"另存为 PDF"；替代 `window.open`（不被弹窗拦截） |
| **导出 Word** | 同一 HTML + `xmlns:w` Office 命名空间 + `.doc` 扩展名 | Word 打开即可编辑 |

**导出内容**：评估总览 → 核心判定 → Agent 校验（始终输出，通过时绿色一行）→ 基础信息 → 教育经历（全段，无 `[:8]` 截断）→ 工作履历 + 经历明细 → 匹配亮点 → 短板与风险 → 专业技能 → 竞争力加分项 → 岗位定制判断 → 美化程度判断依据 → 简历标记要点表。

**注意**：导出不包含评分构成（裸分数无参考价值）和候选人排名（单份报告不需要交叉对比，批量场景中排名在页面查看）。

**标记数据懒加载**：首次导出才请求 `fetchResumeReviewMarkers`，失败不阻断报告生成（跳过标记表）。切换候选人时组件按 `resume_id` 重挂载，缓存清零。

**Word 安全样式**：导出 HTML 仅使用 `table`/`h*`/`p`/`ul` 等安全标签，避免 flex 布局造成 Word 排版塌陷。

#### 归档报告图片实时导出（方案 B）

`report-export.tsx` 额外导出两个函数，供候选人才库「导出报告图片」使用——**图片不存储、不落盘，按需实时生成**：

| 函数 | 说明 |
|------|------|
| `captureReportPng(opts)` | 用 `analysis` + `resumeId` 生成报告图片 `Blob`（JPEG q0.9，2x 缩放），**不触发下载**；失败返回 `null` |
| `downloadReportImage(opts)` | 调用 `captureReportPng` 后直接触发浏览器下载（`候选人分析报告-<姓名>.jpg`），返回是否成功 |

数据流：

```
归档详情（detail.analysis，完整 hr_analysis，已存 JSON）
   │
   ▼
buildReportInner(analysis) → 报告 HTML（13 个模块）
   │  + fetchResumeReviewMarkers(resume_id, analysis)  ← 简历标记要点，实时零 Token，不进归档 JSON
   ▼
html2canvas 离屏渲染 → canvas.toBlob('image/jpeg', 0.9) → Blob
   ▼
URL.createObjectURL + <a download> → 浏览器下载 JPEG
```

要点：
- **只存「配方」（analysis JSON），不存「成品」（图片）**——每份归档仅几 KB JSON，1000 份 <10MB，替代原先每份 300KB–4MB PNG 的方案。
- 数据隔离天然成立：导出用的是**该归档自己存的那份 `analysis`**，与"重新分析任何人"互不影响。
- 旧归档若缺完整 `analysis`（本轮之前归档的），前端禁用导出按钮并提示重新归档。

### 8.7 批量分析切换

`ResumeReviewPanel` 使用 `key={data.resume_id}` 强制 React 在切换候选人时重新挂载组件，所有内部 state 重置；组件挂载时 `useEffect` 自动加载新候选人的简历重点标记。

### 8.8 Agent 自校（分层）

**位置**：`screening_agent._self_reflect()`，在报告校验通过后执行。

**第一层：确定性预检**（`_precheck_findings`，0 次 LLM，高精度低误报）：

| 预检项 | 对应规则 | 逻辑 |
|---|---|---|
| 薪资疑似编造 | 规则 4 | 报告写了薪资期望，但简历原文无任何薪资字样（正则：薪资/薪酬/工资/N k/万…） |
| 扣分无依据 | 规则 2 | `ai_deduction > 0` 但 `deduction_reasons` 为空 |
| 强断言无出处 | 规则 1 | 优势含"精通/资深/主导…"等强断言词，且其中的技术项（ASCII 词）在简历原文完全未出现 |

**触发深度核查的条件**：预检发现疑点，或报告自报 `ai_risk` 为 medium/high。预检干净且无风险 → 直接通过（`mode: "预检"`），**不消耗 LLM**。

**第二层：LLM 深度核查**：预算门槛 `budget["calls"] < MAX_AGENT_LLM_CALLS - 1`（`MAX_AGENT_LLM_CALLS = 5`）。Prompt 包含报告 JSON、简历原文摘录 `experiences[:8]`（论断必须以此为依据核对）、岗位要求，以及预检疑点（供重点核实）。

**5 条核查规则**（Prompt 模板）：

```python
【核查规则】
1. 论断是否超出简历依据
   例如：简历写"了解 Python"，报告断言"精通 Python"——这是超出简历依据。
2. 评分依据是否可追溯
   例如：报告扣 15 分（中度 AI 美化），但未引用具体句子——这是评分依据不可追溯。
3. 加分项是否与岗位相关
   例如：简历有"PMP 证书"，但岗位是纯前端开发，仍列为加分项——这是加分项与岗位无关。
4. 缺失项是否标注了"未提供"
   例如：简历未提及薪资期望，报告写成"符合预期"——这是编造缺失信息。
5. 风险判断是否区分了"风险"和"未体现"
   例如：简历未提及空窗期，报告写"无空窗期，稳定性好"——这是将未体现误判为事实。
```

**输出字段**（`hr_analysis.agent_validation`）：

```python
{
    "checked_rules": 5,              # 核查规则总数
    "issues": [
        {"rule": 1, "problem": "论断超出简历依据", "fix": "已修正为'了解 Python，有 2 年开发经历'"}
    ],
    "passed": false,                  # true = 无问题 / 问题已修正
    "mode": "预检",                    # "预检" = 确定性预检通过；"深度" = LLM 深度核查
    "revised": false                  # true = 修订稿已重新生成且通过结构校验后生效
}
```

**修订回退**：深度核查触发修订后，修订稿重新过 `_validate_report`；结构校验不通过（或需求抽取本身失败）时回退保留原报告，`revised` 置回 `false`。

**前端展示**：dashboard 页面「Agent 校验」模块始终显示（只要 `hr_analysis.agent_validation` 存在）：
- 全部通过 → 绿色单行徽章「✓ Agent 校验：已核查 N 项要求，全部通过」，不可展开
- 检出问题 → 琥珀色折叠徽章「Agent 校验：已核查 N 项要求，检出 N 个问题并已修正」，点「详情」展开问题表格（问题类型/问题/修正三列）
- 右侧导航「Agent 校验」锚点同步存在，可滚动跳转
- 导出报告同样始终输出校验结论（通过时一行绿色 `✓ Agent 校验`，有问题时带问题表）

### 8.9 跨候选人对比

**位置**：`app._compare_candidates()`，在批量分析（≥2 份简历）完成后调用。

**输入**：N 份候选人的分析结果摘要（姓名、得分、优势、短板），LLM 生成：

| 输出字段 | 说明 |
|---|---|
| `ranking[]` | 排名列表，每项含 `rank` / `name` / `score` / `difference`（核心差异点） |
| `pairwise[]` | 两两对比自然语言描述 |
| `recommendation` | 优先面试建议 |

**容错**：LLM 调用失败（无 key / 超时 / 网络异常）时返回 `None` 并记日志，批量响应不受影响（对比只是增强能力）。前端在切换候选人时保留 `comparison` 字段，避免「候选人排名」消失。

**前端展示**：dashboard 页面「候选人排名」模块，展示排名表格 + 差异对比 + 优先面试建议。排名在页面始终显示（批量模式），**导出报告中不包含**（单份决策报告无需交叉对比）。

---

## 附：多用户改造文件清单

| 文件 | 变更 |
|------|------|
| `apps/backend/auth.py` | 🆕 新增：JWT 认证 + 用户管理 + 中间件 |
| `apps/backend/app.py` | 认证中间件 + auth 路由 + 所有 store 调用传 user_id |
| `apps/backend/store.py` | 全部读写加 user_id 归属校验 |
| `apps/backend/config.py` | 新增 JWT_SECRET_KEY + production 校验 |
| `apps/backend/requirements.txt` | + PyJWT |
| `apps/backend/.env` / `.env.sample` | production 配置 + JWT 密钥 |
| `apps/backend/Dockerfile` | 复制 auth.py；gunicorn 4 workers |
| `apps/frontend/...` | 登录页、auth-context、API 鉴权头、移除模型配置、登出 |
| `docker-compose.yml` | 端口 0.0.0.0；ENV 从 .env 读取 |
| `package.json` / `.gitignore` | 构建脚本、忽略测试临时目录 |
| `docs/ARCHITECTURE.md` | 📄 架构与部署文档 |

### 简历重点标记模块文件清单

| 文件 | 职责 |
|------|------|
| `apps/backend/resume_sanitize.py` | 🆕 PDF 解析 ASCII 垃圾过滤（短 ASCII/稀疏 ASCII/长二进制三模式） |
| `apps/backend/resume_review.py` | 🆕 零 token 简历重点标记生成（关键词匹配 + 原文定位） |
| `apps/backend/screening_agent.py` | 🆕 Agent 驱动招聘分析（需求抽取→经验匹配→报告→校验→重试→自校） |
| `apps/backend/app.py` | 集成清洗、标记路由、批量分析优化（预提取需求）、教育经历排序、跨候选人对比 |
| `apps/backend/prompts.py` | Prompt 模板：新增 `education_history` 数组 schema |
| `apps/frontend/components/workbench/resume-review-panel.tsx` | 🆕 简历重点标记面板（高亮渲染 + 三种导出） |
| `apps/frontend/lib/api/screening.ts` | 新增 `fetchResumeReviewMarkers` / `fetchResumeView` API |
| `apps/frontend/components/workbench/analysis-context.tsx` | `HrAnalysis` 类型：新增 `education_history` / `agent_validation`；新增 `CandidateComparison` |
| `apps/frontend/app/(default)/dashboard/page.tsx` | 报告页：指标卡 → 导出中心 → 排名 → 核心判定 → Agent 校验（始终可见）→ 基础信息 → 教育经历 → 工作履历 → 亮点/短板/风险 → 技能/加分 → 岗位定制/美化依据；右侧 sticky 决策摘要 + 页内导航（scroll-spy）|
| `apps/frontend/components/workbench/report-export.tsx` | 🆕 统一导出中心：单 HTML 模型 × 3 格式（JPEG/PDF/Word），懒加载标记数据；另导出 `captureReportPng` / `downloadReportImage` 供归档实时导出 |
| `.dockerignore` | 排除 `test_*.py` / `smoke_test_*.py` / `*.log`（不进生产镜像） |
| `apps/backend/Dockerfile` | `COPY apps/backend/*.py ./`（通配符，新模块自动包含） |
| `package.json`（前端） | + `html2canvas@1.4.1`（报告图片导出依赖） |

### 归档人才库 + 回收站模块文件清单

| 文件 | 职责 |
|------|------|
| `apps/backend/config.py` | 🆕 `ARCHIVES_DIR` 目录常量（随 `DATA_DIR` 自动创建；无图片目录） |
| `apps/backend/store.py` | 🆕 归档读写：`save_archive`（幂等，存 `category` + 完整 `analysis`）/ `find_existing_archive` / `list_archives` / `query_archives`（姓名模糊+岗位+分类+标签）/ `get_distinct_job_titles` / `get_distinct_categories` / `soft_delete_archive` / `restore_archive` / `permanent_delete_archive` / `empty_trash` / `update_archive_tags` / `update_archive_category`；`_index.json` 全局索引（active/trashed 双列表） |
| `apps/backend/app.py` | 🆕 归档 API：`POST /api/v1/archives`（幂等创建，接收 `category` + 完整 `analysis`）、`GET /api/v1/archives`（列表+姓名/岗位/分类/标签筛选+排序，`meta` 返回分类）、`GET /api/v1/archives/trash`、`GET /api/v1/archives/<id>`（详情，含完整 `analysis`）、`PATCH /api/v1/archives/<id>/tags`、`PATCH /api/v1/archives/<id>/category`、`DELETE /api/v1/archives/<id>`（软删）、`POST /api/v1/archives/<id>/restore`、`DELETE /api/v1/archives/trash/<id>`（彻底删）、`DELETE /api/v1/archives/trash`（清空） |
| `apps/frontend/lib/api/archives.ts` | 🆕 归档 API 客户端：`ARCHIVE_PRESET_CATEGORIES` 预设分类、`createArchive`（含 `category`）、`fetchArchives`（含分类筛选）、`fetchTrash`、`fetchArchiveDetail`、`updateArchiveTags`、`updateArchiveCategory`、`moveToTrash`、`restoreArchive`、`permanentDeleteArchive`、`emptyTrash` |
| `apps/frontend/components/workbench/app-shell.tsx` | 侧边导航 🆕 新增「候选人才库」入口（`/archives`，`ArchiveIcon`），`active` 类型扩展为 `'home' \| 'report' \| 'archives'` |
| `apps/frontend/app/(default)/archives/page.tsx` | 🆕 候选人才库页：双 Tab（在库人才 / 回收站）、姓名查询（防抖）、分类筛选（预设 + 已有分类）、标签筛选、动态排名（同分并列）、详情抽屉（岗位分类编辑 + 标签编辑 + 「导出报告图片」实时生成下载）、恢复/彻底删除/清空回收站（二次确认） |
| `apps/frontend/app/(default)/dashboard/page.tsx` | 报告页头部 🆕 「归档到人才库」按钮：弹出分类选择框（预设 chips + 自定义输入），归档时只传 `category` + 完整 `analysis`，不再上传图片 |
| `apps/frontend/components/workbench/report-export.tsx` | 🆕 `captureReportPng`（生成 JPEG blob）/ `downloadReportImage`（实时生成 + 下载）；移除 `onPngBlob` 回调 |
| `apps/backend/test_archives.py` | 🆕 store 层单测：归档 CRUD、幂等、姓名/岗位/分类/标签查询、分类去重与回退、软删/恢复/彻底删/清空、归属隔离（临时目录隔离） |
| `apps/backend/test_archives_api.py` | 🆕 API 冒烟测试：全链路（归档→查询→改分类/标签→软删→恢复→彻底删）、分类创建与 PATCH、鉴权、归属隔离、清空回收站 |

### 归档数据流（无 LLM 调用，无图片存储）

```
分析工作台「归档到人才库」
   │ 弹窗选岗位分类（预设 chips / 自定义，空则回退 JD 岗位名）
   │ POST /api/v1/archives {resume_id, job_id, category, analysis…}
   ▼
store.save_archive（读已有 resume/job 文件，自动带出 job_title/final_score/candidate_name）
   ├─ data/archives/<archive_id>.json   ← 归档明细（analysis_snapshot 摘要 + 完整 analysis）
   └─ data/archives/_index.json         ← 用户索引（active / trashed 双列表）
   │
   ▼
候选人才库页（/archives）
   ├─ 列表：GET /api/v1/archives?name=&job_title=&category=&tag=&sort=
   ├─ 动态排名：前端对当前筛选结果按 final_score 降序、同分并列
   ├─ 详情抽屉：编辑岗位分类 / 标签；「导出报告图片」→ 前端用 detail.analysis 实时生成 JPEG 下载
   └─ 回收站：软删 → GET /archives/trash → 恢复 / 彻底删除 / 清空
```
