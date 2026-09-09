# AI 简历智选 · 项目架构与部署文档

> 版本：1.0 Cloud Deployment Version
> 本文档整理自实际代码，覆盖：项目架构技术栈、登录模块与并发隔离实现、
> 账号数据保存逻辑、腾讯云 Ubuntu 24.04 成功部署步骤。
>
> 📌 **近期更新（2026-09）**：
> - 登录安全升级：**四位数字图形验证码**（登录前置，一次性，TTL 300s，见 2.6 / 4.2）；**账号软删除 + 90 天可恢复**（仅超级管理员可删、需验证操作者管理员密码、恢复后旧 token 全失效，见 3.2 / 4.2）
> - **硬性门槛确定性扣分**：学历层级/年限/证书硬性要求由服务端确定性判定 `not_met` 并扣分/封顶（学历不达标封顶淘汰级），不再全靠 LLM 自觉（见 8.5 打分机制 / 8.8 前置判定）
> - **打分一致性**：LLM 不再自评排名分数，服务端统一用 `final_score` 覆盖排名分数并重排（见 8.9）
> - **Agent 自校扩展**：预检新增规则 6（分数-证据一致性），深度核查规则 5 → 6 条；前端「Agent 校验」可展开查看检测过程 5 步明细（见 8.8）
> - 认证安全加固：邀请制注册（邀请码申请/审批/生成/作废全流程）+ 登录防爆破（账号级冷却 → 账号冻结 → IP 限流，见 2.6 / 2.7 / 4.2）
> - 邀请码修复：过期时间统一 ISO 输出（修复 1970 显示）、作废接口支持完整 64 位哈希（修复无法作废）、注册页输入位数与生成端动态对齐（修复位数不一致，见 2.7）
> - 登录页 UX：邀请码校验成功/失败交互（抖动动画）、登录失败次数提示、「申请邀请码」返回按钮移至提交下方
> - 生产部署改用 `docker-compose.secure.yml`（healthcheck + 网络/资源加固，见 6.3）

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
│       ├── app/          # 页面路由（login/dashboard/首页/archives/admin）
│       ├── app/(default)/css/globals.css  # 全局样式 + @theme 语义色令牌
│       ├── components/workbench/  # 工作台组件 + auth-context
│       │   ├── app-shell.tsx         # 侧栏壳（导航 + 用户 + 版权）
│       │   ├── analysis-workbench.tsx # 首页工作台（上传 + JD + 分析触发）
│       │   ├── analysis-context.tsx  # 分析结果上下文 + 类型定义
│       │   ├── report-export.tsx     # 统一导出中心（图片/PDF/Word）
│       │   ├── resume-review-panel.tsx # 简历重点标记面板
│       │   └── auth-context.tsx      # 登录态管理
│       ├── app/(default)/admin/page.tsx  # 账号管理（审批/邀请码/冻结/用户/操作记录 五 Tab）
│       ├── lib/api/      # API 封装（带 JWT 头；含 auth-admin.ts 认证/管理接口、archives.ts 归档客户端）
│       └── public/a4cv/  # 独立简历编辑器
│       └── Dockerfile
├── docker-compose.yml
├── docker-compose.secure.yml  # 生产安全加固 compose（healthcheck/网络/资源限制）
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

# 登录防爆破（默认值即示例；生产可调）
LOGIN_FAIL_LIMIT=3              # 连续失败 N 次 → 冷却
LOGIN_LOCK_MINUTES=5            # 冷却时长（分钟）
LOGIN_FREEZE_WINDOW_MINUTES=10  # 冻结统计窗口（分钟）
LOGIN_FREEZE_THRESHOLD=6        # 窗口内失败 N 次 → 冻结账号
LOGIN_MAX_FAIL_PER_IP_10MIN=10  # 单 IP 10 分钟失败上限
LOGIN_IP_LOCK_MINUTES=10        # IP 锁定时长（分钟）
LOGIN_UNFREEZE_VIA_EMAIL=on     # 冻结账号邮箱自助解冻（on/off）
LOGIN_FREEZE_AUTO_UNFREEZE_HOURS=0  # 冻结后自动解冻小时数（0=关闭）

# 邀请码（注册邀请制）
INVITE_CODE_LENGTH=4            # 邀请码位数（≥4，env 可调）
INVITE_CODE_TTL_HOURS=24        # 邀请码有效期（小时）
INVITE_REQUEST_NOTE_REQUIRED=true   # 申请邀请码必填申请理由
INVITE_REQUEST_MAX_REJECTIONS=3     # 同一邮箱被拒上限（此后拒收）
INVITE_REQUEST_MAX_PER_IP_DAY=3     # 单 IP 每日申请上限

# 登录图形验证码（四位数字，一次性）
CAPTCHA_ENABLED=on              # 登录前置验证码开关（on/off；默认开启）
CAPTCHA_TTL_SECONDS=300         # 验证码有效期（秒，最小 30）

# 超级管理员白名单（删除账号等敏感操作仅限白名单邮箱账号）
ADMIN_EMAILS="admin@example.com"

# 账号软删除
DELETED_RESTORE_WINDOW_DAYS=90  # 软删除账号可恢复窗口（天）

# AI 配置
ALLOW_CUSTOM_AI_CONFIG=false    # 是否允许前端自定义 AI 模型配置（默认关闭=用服务端配置）
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
| `components/workbench/auth-context.tsx` | `AuthProvider`：localStorage 恢复登录态、挂载后调 `/auth/me` 刷新 `is_admin`（管理员标记不入登录响应）、未登录重定向 `/login`；公开页白名单含 `/login`、`/reset-password` |
| `app/(default)/login/page.tsx` | 登录/注册页。注册走邀请制：邀请码校验（红色错误/绿色成功态 + 抖动动画）解锁邮箱区 → 发送邮箱验证码 → 注册（服务端三重校验）；另有「申请邀请码」子面板（邮箱+理由，「返回登录/注册」按钮置于提交下方）与 423「账号自助解冻」子面板（用户名/密码/绑定邮箱/验证码，**返回按钮为全宽边框样式置于提交下方**）；423 按 `frozen_by` 分流：`admin` → 弹窗「账号异常请联系系统管理员处理！」（含原因 + 管理员email），`auto` → 自助解冻面板；登录失败次数前端累计提示（连续失败 3 次将暂停登录 5 分钟、6 次将冻结账号） |
| `lib/api/auth-admin.ts` | 注册页/解冻页/管理页的专用 API：`register-config`、`invite-code/check`、`invite-code/send-email-code`、`invite-request`、`unfreeze/send-code`、`unfreeze` 与全部 `/admin/*` 管理接口（含冻结、使用统计、使用排行、审计导出 `downloadAdminOpsExport` CSV/JSON） |
| `lib/api/screening.ts` | 分析工作台 API 调用统一注入 `Authorization: Bearer <token>`；401 统一处理（清 token + 跳登录） |
| `components/workbench/app-shell.tsx` | 侧边栏显示用户名 + 登出 + 修改密码（弹窗）；「账号管理」导航项仅 `is_admin` 可见；登出时清 sessionStorage 分析结果 |
| `app/(default)/admin/page.tsx` | 账号管理页（仅 `is_admin`，页面守卫 + 403 兜底）：五 Tab —— 申请审批（同意/拒绝+理由/补发码邮件）、邀请码总览（筛选/手动生成/作废）、冻结账号（列表 + 来源/原因 + 兜底解冻）、用户管理（创建/改邮箱/管理员标记/冻结/解冻/重置密码/删除 + **「…」菜单收纳次要操作** + 使用统计/使用排行）、操作记录（类型/关键字过滤、20 条/页分页、**CSV/JSON 导出**）；**Tab 状态 URL 记忆**（`/admin?tab=users` 刷新不丢）；弹窗统一走 `AdminModal`（Esc 关闭/自动聚焦/锁滚动） |
| `app/(default)/reset-password/page.tsx` | 忘记密码：两步（邮箱 → 验证码+新密码），「返回登录」为全宽边框按钮置于提交下方 |
| `components/workbench/admin-modal.tsx` | 管理后台统一弹窗组件：Esc 关闭、打开自动聚焦首个输入框、背景滚动锁定、role=dialog + aria-modal；被 admin 页 4 个弹窗（使用统计/冻结/改邮箱/使用排行）复用 |

### 2.5 邮箱验证码机制（注册绑定 + 忘记密码重置，统一实现）

**统一机制**（`auth.py` 的 `email code` 部分）：验证码按**邮箱 + 用途**存储，磁盘只存 SHA-256 哈希。6 位**全大写字母+数字**（排除易混淆 `0O1lI`，`ABCDEFGHJKMNPQRSTUVWXYZ23456789` 36 字符，36^6≈21.8 亿组合）。

**大小写不敏感**：邮件中显示大写；校验时统一先 `.upper()` 再哈希比对，因此用户填写大写/小写/混合均能通过；前端输入框也自动转大写（`onChange` 直接 `.toUpperCase()`），所见即所得。三种用途用常量区分：
- `PURPOSE_EMAIL_VERIFY`（注册绑定邮箱）
- `PURPOSE_RESET_PASSWORD`（忘记密码重置）
- `PURPOSE_UNFREEZE`（登录冻结账号的邮箱自助解冻）

```
发码  POST /api/v1/auth/invite-code/send-email-code (body: {email, invite_code})   ← 注册（邀请制）
  ├─ 邀请码校验（须有效未用、且 email 与该码绑定的申请邮箱一致）→ 422
  ├─ 校验邮箱格式 + 未注册 + SMTP 已配置
  ├─ 频率限制：同邮箱 60 秒冷却 → 429
  └─ 生成 6 位码 → 存哈希 → SMTP 发送（mailer.py）

注册  POST /api/v1/auth/register (body: {username,password,email,code,invite_code})
  ├─ ① 非消费校验邀请码（失败不扣码）  ② 消费式校验邮箱验证码（失败不扣码）
  ├─ ③ 文件锁内：复检邀请码 → 建号 → 消费邀请码（原子；用户名冲突不耗码）
  └─ 建号成功 → 签发 JWT
```

```
重置第一步  申请 POST /api/v1/auth/reset-password/request (body: {email})
  ├─ find_user_by_email → 生成重置验证码（60秒冷却 → 429）
  ├─ SMTP 发送邮件（mailer.py；未配置 SMTP → 503）
  └─ 无论邮箱是否存在都返回同一文案（防邮箱枚举）

重置第二步  确认 POST /api/v1/auth/reset-password/confirm (body: {email,code,new_password})
  ├─ 校验验证码：哈希匹配 + 未过期 + 未超限（一次失败累计，5 次作废）
  └─ 更新密码：password_version +1 → 用户所有旧 token 自动失效

解冻发码    POST /api/v1/auth/unfreeze/send-code (body: {username,email})   ← 登录防爆破配套
  ├─ 账号须存在（404）且处于冻结状态（409）且 email = 账号绑定邮箱（422，未绑定提示联系管理员）
  └─ 复用邮箱验证码机制（PURPOSE_UNFREEZE，60s 冷却/30 分钟有效）→ SMTP 发送

账号解冻    POST /api/v1/auth/unfreeze (body: {username,password,email,code})
  ├─ 冻结检查 → 每 IP 只读限流 → 密码校验（错误计入登录失败窗口）
  ├─ 邮箱=绑定邮箱 + 消费式校验解冻验证码（防重放）
  └─ 成功 → 清失败记录 → 签发 JWT
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

### 2.6 登录安全（图形验证码 → 账号冷却 → 冻结 → IP 限流，四层防护）

登录接口 `POST /api/v1/auth/login` 在**校验密码之前**依次执行四层策略，全部通过后才验密码：

```
POST /api/v1/auth/login (body: {username, password, captcha_id, captcha_code})
  ├─ 第 0 层：四位数字验证码（前置，默认开启）
  │    └─ GET /api/v1/auth/captcha 获取 {captcha_id, code, ttl_seconds}
  │    ├─ 缺 captcha_id/code → 422「请填写验证码」
  │    ├─ 校验失败（verify_captcha：一次性 sha256 比对 + TTL 过期）→ 422「验证码错误或已过期」
  │    └─ 验证码失败**不记账**（不影响防爆破失败计数与 IP 预算）
  ├─ 第 1 层：每 IP 失败上限（防跨用户名撞库）
  │    └─ ip_login_rate_exceeded(ip) → 10 分钟窗口内失败 ≥10 次（LOGIN_MAX_FAIL_PER_IP_10MIN）
  │         → 429「尝试次数过多，请在 N 分钟后重试」+ Retry-After 头
  │    （只读检查；计数仅在密码真正错误时由 record_ip_login_failure 落账）
  ├─ 第 2 层：账号级策略 login_policy_check(username)
  │    ├─ 已冻结 → 423「该账号已临时冻结…」（含自助解冻引导文案）
  │    ├─ 冷却中（连续失败 ≥3 次 LOGIN_FAIL_LIMIT，且距上次失败 <5 分钟 LOGIN_LOCK_MINUTES）
  │    │    → 429「尝试过于频繁，请在 N 分钟后重试」+ Retry-After
  │    └─ 冻结可自动解冻（LOGIN_FREEZE_AUTO_UNFREEZE_HOURS>0 且到期）→ 先清标记再继续
  └─ 第 3 层：PBKDF2 密码校验
       ├─ 成功 → clear_login_failures → 签发 JWT
       └─ 失败 → record_login_failure（连续计数 +1；10 分钟 LOGIN_FREEZE_WINDOW_MINUTES 窗口内
            失败 ≥6 次 LOGIN_FREEZE_THRESHOLD → 冻结账号）→ 冻结则 423，否则 401
```

**验证码实现**（`auth.py`）：四位随机数字，一次性使用（`verify_captcha` 用后即删，防重放）；TTL 默认 300 秒（`CAPTCHA_TTL_SECONDS`，最小 30）；存储只写 `sha256(<id>:<code>)` 哈希到 `data/rate_limits/captcha_<id>.json`（磁盘不存明文码），`_purge_stale_captchas` 随获取惰性清理过期文件。`CAPTCHA_ENABLED=off` 时 `GET /api/v1/auth/captcha` 返回 404，登录跳过验证码校验——前端在验证码不可用时会自动降级为普通登录，不阻塞。

| 参数（config.py） | 默认 | 含义 |
|---|---|---|
| `CAPTCHA_ENABLED` | on | 登录前置四位数字验证码开关 |
| `CAPTCHA_TTL_SECONDS` | 300 | 验证码有效期（秒，最小 30） |
| `LOGIN_FAIL_LIMIT` | 3 | 连续失败达到该次数后进入冷却 |
| `LOGIN_LOCK_MINUTES` | 5 | 冷却时长（分钟） |
| `LOGIN_FREEZE_WINDOW_MINUTES` | 10 | 冻结统计滑动窗口（分钟） |
| `LOGIN_FREEZE_THRESHOLD` | 6 | 窗口内失败达到该次数 → 冻结账号 |
| `LOGIN_MAX_FAIL_PER_IP_10MIN` | 10 | 单 IP 10 分钟失败上限 |
| `LOGIN_IP_LOCK_MINUTES` | 10 | IP 锁定时长（分钟） |
| `LOGIN_UNFREEZE_VIA_EMAIL` | on | 冻结账号是否允许邮箱自助解冻 |
| `LOGIN_FREEZE_AUTO_UNFREEZE_HOURS` | 0 | 冻结后自动解冻小时数（0=不自动解冻） |

**实现要点**（`auth.py`）：
- **内联说明**：验证码存储只写哈希（`data/rate_limits/captcha_<id>.json`，sha256 一次性比对），失败不累计防爆破计数（避免验证码输错连累账号/IP 预算）。
- **失败记录落盘**：`data/login_failures/<sha256(username)>.json`，文件名即哈希（磁盘不存明文用户名标识），记录 `consecutive_failures`（连续计数，登录成功即清零）、`last_failure_at`、`failures[]`（窗口时间戳 + IP 哈希，供冻结判定）、`frozen`/`frozen_at`。
- **IP 计数与拦截分离**：IP 限流是「只读检查、失败才计数」——成功登录 / 冷却拦截不会消耗 IP 预算（防误伤共享出口 IP 的正常用户）。
- **冻结响应码 423**：按 `frozen_by` 分流——`admin` 手动冻结 → 前端弹窗「账号异常请联系系统管理员处理！+ 冻结原因 + 管理员email」（来自响应字段，不展示自助解冻）；`auto` 自动冻结 → 前端自动弹出「账号自助解冻」子面板（用户名 + 密码 + 绑定邮箱 + 邮箱验证码），走 `unfreeze/send-code` + `unfreeze` 全流程；管理员也可在管理页「冻结账号」Tab 直接解冻。管理员手动冻结的账号**禁止**自助解冻与重置密码（重置请求/确认均 423）。
- **前端失败次数提示**：登录失败时前端累计 `loginFailCount`，底部错误条追加「密码错误 N 次。连续失败 3 次将暂停登录 5 分钟，6 次将冻结账号…」（423 直接开解冻面板，不计入该计数）。

### 2.7 邀请码机制与生命周期（注册邀请制）

```
管理员生成（审批通过 / 手动应急）          ── generate_invite_code()
   └─ 明文邀请码仅返回一次（生成响应 / 审批邮件）
       └─ 磁盘只存哈希：data/invite_codes/<sha256(码)>.json
            ├─ code_hash（sha256 hex，文件名即哈希）
            ├─ bound_email（绑定申请邮箱，空=不绑定仅应急）
            ├─ expires_at（epoch 秒：now + TTL_HOURS×3600）
            └─ created_by / request_id / note / used / created_at
用户注册流程
   ├─ POST /api/v1/auth/invite-code/check   非消费预校验（存在/未用/未过期）→ 解锁邮箱区
   ├─ POST /api/v1/auth/invite-code/send-email-code  发邮箱验证码（校验绑定邮箱一致）
   └─ POST /api/v1/auth/register            文件锁内：复检 → 建号 → 消费（原子，用户名冲突不耗码）
        └─ 消费后：活跃文件删除 → 写入 data/invite_codes_audit/<hash>.json（审计归档）
管理员作废（未使用的码）
   └─ POST /api/v1/admin/invite-codes/revoke  → 活跃文件标记 revoked 后移入 audit 归档
```

**邀请码格式**：长度 `config.INVITE_CODE_LENGTH`（默认 **4**，环境变量可调且下限 4），字符集 `ABCDEFGHJKMNPQRSTUVWXYZ23456789`（32 字符，排除易混淆 `0O1lI`），大小写不敏感（统一转大写）。有效期 `INVITE_CODE_TTL_HOURS`（默认 24 小时）。

**位数一致性**：`GET /api/v1/auth/register-config` 返回 `invite_code_length`（当前为 4），前端注册页邀请码输入框的 `maxLength` 与占位文案「N 位邀请码」**动态取该值**，与管理端生成的邀请码位数永远一致（历史版本前端写死 `maxLength=8`，与后端生成位数脱节，易混淆）。

**过期时间显示**：`expires_at` 磁盘存储为 **epoch 秒**（float），列表接口输出前统一经 `_epoch_or_iso()` 转为 **ISO 字符串**（兼容毫秒级旧数据），前端 `fmtTime` 也兼容 ISO / epoch 秒 / 毫秒三种输入——避免「秒被当毫秒 → 显示 1970-01-01」的历史缺陷。

**作废接口入参**（`revoke_invite_code`）：同时支持**明文邀请码**（内部再哈希定位）与**完整 64 位哈希**（正则 `^[0-9a-fA-F]{64}$` 直接按文件名定位）。管理端列表返回**完整哈希**（不再截断），前端表格用 `前8…后4` 短哈希展示、作废时回传完整哈希——修复历史版本「12 字符截断哈希被再次哈希 → 永远 404 无法作废」的缺陷。

---

## 三、账号数据保存逻辑结构

所有数据存 JSON 文件（零数据库），Docker 部署时用 volume 持久化到宿主机。

### 3.1 目录结构

```
data/                          # BASE_DIR/data（启动自动创建）
├── users/                     # 用户账号
│   └── <user_id>.json         # 一个用户一个文件（含邮箱、密码版本号）
├── password_resets/           # 邮箱验证码（注册/重置/自助解冻，只存哈希，30分钟有效）
│   └── <purpose>.<sha256>.json
├── invite_requests/           # 邀请码申请单（v3 邀请制）
│   └── <request_id>.json      # 申请邮箱/理由/状态/审批人/拒信理由（邮箱明文，属申请单本身）
├── invite_codes/              # 活跃邀请码（只存哈希）
│   └── <sha256>.json          # 绑定邮箱 + 过期时间（epoch 秒）；文件名即哈希，磁盘零明文
├── invite_codes_audit/        # 已消费/作废邀请码归档（审计链：码→使用人）
├── login_failures/            # 登录失败记录（连续/窗口计数 + 冻结标记，sha256 文件名）
│   └── <sha256(username)>.json # consecutive_failures / failures[] / frozen / frozen_at
├── rate_limits/               # 各维度限流计数（IP 失败/申请冷却等，按 key 轮转）
│   ├── captcha_<id>.json      # 登录验证码（只存 sha256 哈希 + 过期时间，一次性，TTL 300s）
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
  "password_version": 1,          // 密码版本号（重置/改密 +1，旧 JWT 全失效；恢复账号时也 +1）
  "created_at": "2026-08-20T16:00:00+00:00",
  "deleted_at": null,             // 软删除时间（null=未删除）；删除后 90 天内可恢复
  "deleted_by": null,             // 操作者 user_id（软删除责任人审计）
  "restored_at": null             // 恢复时间（null=从未恢复）
}
```

> 🔒 **安全说明**：文件里**不存明文密码**，只存 `哈希 + 盐`。即使 data 目录泄露，也无法反推出密码。
>
> 🗑️ **软删除**：删除账号不物理移除文件——置 `deleted_at`/`deleted_by` 后移出活跃索引（JWT 校验立即拒绝），保留使用统计供审计；`list_deleted_users()` 只返回删除时间在 `DELETED_RESTORE_WINDOW_DAYS`（默认 90）内的账号，`restore_user()` 恢复时 `deleted_at`/`deleted_by` 清空、`restored_at` 落时间、`password_version` +1（旧 token 全失效，需重新登录）。

### 3.2.1 邀请码 / 登录失败记录文件结构

```jsonc
// data/invite_codes/<sha256(邀请码)>.json   （文件名即哈希，磁盘零明文）
{
  "code_hash": "sha256 hex…",     // 与文件名一致（64 位 hex）
  "bound_email": "apply@example.com", // 绑定申请邮箱；"" = 不绑定（仅管理员应急）
  "expires_at": 1788578607.07,    // epoch 秒（now + TTL_HOURS×3600）；列表输出前转 ISO 字符串
  "used": false,                  // 已消费标记（消费后文件移入 invite_codes_audit/）
  "used_by_username": null,       // 使用人（审计链：码→使用人）
  "used_at": null,
  "created_at": "ISO 时间",
  "created_by": "admin",
  "request_id": "manual-uuid…",   // 手动生成前缀 manual-；审批生成用申请单 id
  "note": "2026 春招活动"
}

// data/login_failures/<sha256(username)>.json
{
  "consecutive_failures": 2,      // 连续失败计数（登录成功即清零）
  "last_failure_at": 1788578607.07,
  "failures": [ {"ts": 1788578607.07, "ip_hash": "rl:<ip> sha256 前 24 位"}, … ],  // 窗口内时间戳
  "frozen": false,                // 冻结标记（窗口内失败 ≥ LOGIN_FREEZE_THRESHOLD 置 true）
  "frozen_at": null
}
```

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
  "code":"A7K2MP",
  "invite_code":"AB3X"
}
```

成功返回 `data.user_id`、`data.username`、`data.token`。邀请码须先通过 `invite-code/check` 预校验（见 2.7）；服务端在文件锁内做「复检邀请码 → 建号 → 消费邀请码」原子操作（用户名冲突不耗码）。

#### `POST /api/v1/auth/login`

```json
{"username":"alice","password":"StrongPass123","captcha_id":"...","captcha_code":"1234"}
```

用户名大小写不敏感，成功返回 JWT。**登录前置验证码**（`CAPTCHA_ENABLED=on` 时）：`captcha_id`/`captcha_code` 缺失 → `422`；验证码错误或过期 → `422`（一次性消费，失败不计入防爆破计数）。**防爆破响应码**：`429`（IP 限流 / 账号冷却，含 `Retry-After` 头）、`423`（账号已冻结，前端弹出「账号自助解冻」面板）、`401`（密码错误）。失败计数与冻结判定见 2.6。

#### `GET /api/v1/auth/captcha`

获取登录验证码（四位随机数字）：

```json
{"request_id":"...","data":{"captcha_id":"...","code":"1234","ttl_seconds":300}}
```

一次性使用；`CAPTCHA_ENABLED=off` 时返回 `404`（前端登录页自动降级为无验证码提交）。验证码无法获取时（服务端未启用）不影响登录流程。

#### `GET /api/v1/auth/me`

需要认证，返回当前用户信息。

#### `POST /api/v1/auth/change-password`

```json
{"old_password":"OldPass123","new_password":"NewPass456"}
```

成功后密码版本递增，旧 JWT 失效，客户端需要重新登录。

#### 邀请码注册相关接口（注册邀请制，见 2.7）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/auth/register-config` | 注册页配置：`invite_required`、`contact_email`、`invite_code_length`（邀请码位数，前端输入框 maxLength/占位文案动态取用） |
| `POST` | `/api/v1/auth/invite-code/check` | 非消费预校验邀请码（存在/未用/未过期），通过返回 `{"valid": true}`；不区分失败原因统一文案防枚举；每 IP 每小时限 10 次 |
| `POST` | `/api/v1/auth/invite-code/send-email-code` | 校验邀请码 + 绑定邮箱一致后发送注册邮箱验证码（同邮箱 60s 冷却） |
| `POST` | `/api/v1/invite-request` | 提交邀请码申请（公开）：邮箱 + 申请理由，同邮箱 60s 冷却 / 每 IP 每日上限 / 被拒超限拒收 |
| `POST` | `/api/v1/auth/unfreeze/send-code` | 冻结账号发解冻验证码（账号须存在且冻结，email 须等于绑定邮箱） |
| `POST` | `/api/v1/auth/unfreeze` | 冻结账号自助解冻：密码 + 绑定邮箱 + 消费式验证码，成功清失败记录并签发 JWT |

#### 管理员邀请码接口（均需管理员 JWT）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/admin/invite-codes?status=all\|active\|used\|expired` | 邀请码总览（不回明文）：返回**完整 64 位 code_hash**、`expires_at`（ISO 字符串）、状态、绑定邮箱（脱敏）、使用人 |
| `POST` | `/api/v1/admin/invite-codes/generate` | 手动生成邀请码（1–20 个，可选绑定邮箱 / 备注 / 有效天数），明文仅本次响应返回一次 |
| `POST` | `/api/v1/admin/invite-codes/revoke` | 作废未使用邀请码（入参支持明文码或完整 64 位哈希）；不存在/已使用/已作废 → 404 |
| `GET` | `/api/v1/admin/users/frozen` | 冻结账号列表（含 `frozen_by`: `admin`手动/`auto`自动、`frozen_reason`；`frozen_at` 输出 ISO 字符串——内部存储 epoch 秒、输出层统一转换，修复前端 1970 显示） |
| `POST` | `/api/v1/admin/users/<username>/freeze` | 管理员手动冻结（防异常消耗 token 等），body `{reason?}`；冻结后拒绝登录（423）/拒绝重置密码/禁止自助解冻，原因展示在登录弹窗与审计中 |
| `POST` | `/api/v1/admin/users/<username>/unfreeze` | 管理员兜底解冻 |

#### 管理员用户管理接口（均需管理员 JWT，CRUD）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/admin/users?keyword=&page=&size=` | 用户列表：支持用户名/邮箱关键字搜索与分页；每项含 `is_admin` / `frozen` / `email_bound` / 注册时间（脱敏，不含密码哈希） |
| `POST` | `/api/v1/admin/users` | 管理员代建用户（绕过邀请码）：`{username, password, email?, is_admin?}`；用户名/邮箱冲突 → 409。**`is_admin` 仅超级管理员可指定**（普通管理员请求 → 403） |
| `PATCH` | `/api/v1/admin/users/<username>` | 修改用户：`{email?}`（改绑定邮箱，要求格式合法且全局唯一）、`{is_admin?}`（设/取消管理员标记，**仅超级管理员可操作**）；**防锁死**：禁止取消/删除最后一个管理员、白名单邮箱管理员不可改不可降级 |
| `POST` | `/api/v1/admin/users/<username>/reset-password` | 管理员重置密码：生成 12 位临时密码仅此一次返回，密码版本 +1 使旧 token 全部失效 |
| `DELETE` | `/api/v1/admin/users/<username>` | 删除用户（**软删除**，90 天内可恢复）：**仅超级管理员**（.env 白名单邮箱）可操作，且必须提交操作者自己的管理员密码 `{admin_password}`（密码校验失败 → 403）；禁止删除自己与最后一个管理员（白名单邮箱管理员除外）。删除留痕 `user_delete` 审计 |
| `GET` | `/api/v1/admin/users/deleted` | 软删除账号列表（删除时间在 90 天恢复窗口内，按删除时间倒序）：含 `username`/`email`（脱敏）/`deleted_at`/`deleted_by` |
| `POST` | `/api/v1/admin/users/<username>/restore` | 恢复软删除账号（仅 90 天内）：`password_version` +1、旧 token 全失效、需重新登录；超过窗口 → 400「已超过可恢复期限」；留痕 `user_restore` 审计 |
| `GET` | `/api/v1/admin/ops?op=&keyword=&page=&size=` | 管理操作审计（创建/删除/冻结/解冻/授予·取消管理员/改邮箱/重置密码），含操作者、目标用户、时间与标准化详情（详情不含临时密码明文）；`op` 精确过滤 + `keyword` 搜索操作者/目标；前端按 20 条/页分页展示 |
| `GET` | `/api/v1/admin/ops/export?op=&keyword=&format=csv\|json` | 审计导出（管理员）：遵循当前过滤导出**全部结果**（不受分页限制）；`csv` 带 utf-8-sig BOM（Excel 直接打开不乱码）+ `Content-Disposition: attachment`，`json` 返回完整字段数组 |
| `GET` | `/api/v1/admin/users/<username>/usage?granularity=day\|month\|year&buckets=N` | 用户使用次数聚合：`granularity=day`（默认 30 天）/`month`（12 月）/`year`（5 年），`labels` 一律字符串，返回 `{labels, series:{login, analysis, total}, summary}`；`summary` = 区间合计（total/login_total/analysis_total）、日均/月均/年均（avg_per_bucket）、峰值（peak_label/peak_total）、活跃期数（active_buckets）、首次/最近使用 |
| `GET` | `/api/v1/admin/users/usage-ranking?limit=N` | 全用户使用排行：按登录+分析总量降序（含 last_usage），帮助发现异常高消耗账号 |

> 管理员判定（`auth.is_admin`）= `.env` 白名单邮箱 `ADMIN_EMAILS` **或** 用户记录 `is_admin` 标记；两者任一为真即管理员（`/auth/me` 的 `is_admin` 与 `require_auth` 的 `/admin/*` 鉴权均按此综合判定）。
>
> **权限分级**：`.env` 白名单邮箱用户为**超级管理员**（`auth.is_super_admin`，`/auth/me` 返回 `is_super_admin`）。分配/取消管理员权限（PATCH `is_admin`、POST 创建管理员）仅超级管理员可操作；**管理员身份账号的敏感操作（冻结/解冻/删除/重置密码/改邮箱）同样仅超级管理员**（普通管理员对其操作 → 403）；普通管理员仍可对普通用户做全部管理动作。
>
> **使用统计埋点**：登录成功（`login` 路由）与 HR 筛选分析成功（`_run_hr_analysis` 返回前，含缓存命中）各记一次，按天聚合存 `data/usage/<user_id>.json`（UTC 日期键）。删除用户时同步清理统计文件。
>
> **HR 分析缓存**：`_HR_ANALYSIS_CACHE` 为 **LRU（最多 1000 条）+ TTL（24h）** 的进程内缓存，命中时「同一简历+同一职位+同一模型配置」重复分析不调 LLM、0 token 消耗；超容量淘汰最久未用、过期条目惰性剔除，避免内存无界增长。
>
> **配置**：`ADMIN_OPS_RETENTION_DAYS`（默认 180，`.env` 可覆盖）与 `USER_USAGE_RETENTION_DAYS`（默认 365）控制审计与使用统计的保留天数；**懒清理**（`auth.maybe_prune`）在查询审计/使用统计/使用排行时每日最多执行一次（`data/.last_prune` 哨兵），删除过期审计文件与过期使用统计天键（空文件一并删除）——不依赖 cron，随查询自然发生。
>
> **数据备份**：数据存放于 Docker 命名卷 `hr-ai-resume-selection_backend-data`（容器内 `/app/data`）。服务器 `crontab` 每日 03:00 执行 `scripts/backup.sh`（sudo 打包卷 → `/opt/backups/resume-data-<时间戳>.tar.gz`，保留最近 7 天，日志 `/opt/backups/backup.log`）。
>
> **管理操作审计**：每次管理动作原子写 `data/admin_ops/<ts>_<uuid>.json` 一条（`data/usage`、`data/admin_ops` 同属 `data/` 目录，随备份/安全补丁清单处理）。审计详情标准化为动词短语（如「删除用户 xxx」「修改邮箱：a@x.com → b@x.com」「重置密码」），**不落临时密码明文**；支持按过滤条件一键导出 CSV/JSON。

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
| 423 | 账号已冻结（登录防爆破；前端引导邮箱自助解冻） |
| 429 | 频率限制（登录冷却 / IP 限流 / 发码冷却 / 校验限次） |
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

认证 / 安全相关套件**必须各自独立进程运行**（`test_invite_flow` 与 `test_login_lockout` 在同一进程内会互相污染共享的限流/临时目录状态）：

```powershell
cd apps/backend

# 业务套件（可放同一进程）
python -m unittest test_hr_analysis test_archives test_archives_api test_resume_review
python -m unittest test_review_markers_route test_screening_agent test_agent_upgrades test_e2e

# 认证 / 安全套件（各自独立进程）
python -m unittest test_invite_flow      # 邀请码注册全流程（11 项）
python -m unittest test_login_lockout    # 登录防爆破：冷却/冻结/解冻（12 项）

cd ../frontend
npx tsc --noEmit -p tsconfig.json
```

> 后端容器验证新版代码：`docker exec hr-ai-resume-selection-backend grep -c _epoch_or_iso /app/app.py`（输出 ≥1 即新版已生效）。

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

生产环境使用安全加固 compose 文件（含 healthcheck 与网络加固）：

```bash
cd /opt/resume-matcher-agent-cn

# 改后端
docker compose -f docker-compose.secure.yml build backend
docker compose -f docker-compose.secure.yml up -d --force-recreate --no-deps backend

# 改前端
docker compose -f docker-compose.secure.yml build frontend
docker compose -f docker-compose.secure.yml up -d --force-recreate --no-deps frontend

# 两端都改
docker compose -f docker-compose.secure.yml build backend frontend
docker compose -f docker-compose.secure.yml up -d --force-recreate backend frontend

# 查看健康状态（healthy 才算就绪）
docker compose -f docker-compose.secure.yml ps
```

仅执行 `docker compose build` 不会自动替换当前运行容器。构建后必须执行对应的 `up -d 服务名`，并检查容器启动时间；`--force-recreate` 可同时规避下文 known issue 的挂起风险。

> ⚠️ **服务器 `.env` 保留真实密钥**：`.env` 内含生产密钥，部署更新代码时**只替换源码文件**（tar 包只含 `apps/`、`docker-compose*.yml` 等，不含 `.env`），切勿用本地/示例 `.env` 覆盖服务器配置。

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

#### 打分机制（服务端统一计算，`app._normalize_hr_analysis`）

LLM 只输出分项依据，**不计算最终得分**；服务端按固定公式统一定档，保证报告页头部、候选人排名与导出一致：

1. **基础分** `base_score`：五维分项之和，各维上限固定：

   | 分项 | 上限 |
   |---|---:|
   | `hard_requirements` 硬性要求 | 25 |
   | `responsibility_overlap` 职责重合度 | 25 |
   | `skills_projects` 技能与项目 | 25 |
   | `industry_background` 行业背景 | 15 |
   | `evidence_bonus` 加分证据 | 10 |
   | **合计** | **100** |

   分项缺失（LLM 未输出完整五项）时回退 `job_fit_score`；五项齐全时以分项之和为准（`_validate_report` 要求总和与 `job_fit_score` 一致，不一致触发重试）。

2. **AI 美化扣分** `deduction`：按 `ai_risk` 档位钳制（`_AI_RISK_RULES`）：

   | ai_risk | 扣分范围 |
   |---|---|
   | none | 0 |
   | light（轻微） | 5–10 |
   | medium（中度） | 15–20 |
   | high（重度/模板） | 30（固定） |

   LLM 建议的 `ai_deduction` 会被钳到对应档位区间；`final_score = max(0, base_score − deduction)`。

3. **硬门槛确定性扣分** `hard_gate_deduction`（服务端确定性兜底，不依赖 LLM 自觉）：
   从 `requirements_checklist` 读取 `status == "not_met"` 的硬性要求（见 8.8 前置判定）：
   - 每条确定性不达标扣 10 分（上限 30 分）；
   - **学历层级不达标** → `final_score` 额外封顶 **59**（强制淘汰级 D）；
   - 其他硬门槛（年限/证书）不达标 → 封顶 **69**（储备观察级）。
   返回字段 `hard_gate_deduction` 记录实际扣除值。

4. **等级与建议**（基于扣分后的 `final_score`）：

   | final_score | 等级 | 招聘建议 | fit_tag |
   |---|---|---|---|
   | ≥90 | S级（优质适配） | 优先面试 | 高匹配 |
   | 80–89 | A级（良好适配） | 优先面试 | 高匹配 |
   | 70–79 | B级（基本适配） | 储备观察 | 部分匹配 |
   | 60–69 | C级（适配一般） | 储备观察 | 部分匹配 |
   | <60 | D级（不适配） | 淘汰 | 不匹配 |

   LLM 请求的 `recruitment_recommendation`/`fit_tag` 只允许**不高于**分数推导档位（防止 LLM 提级），低于则可保留。

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

### 8.8 Agent 自校（分层）+ 硬性要求前置判定

**位置**：`screening_agent._self_reflect()`，在报告校验通过后执行。硬性要求判定（`_build_requirements_checklist`）在自校前完成，其结果既供打分兜底（8.5 第 3 条）也供自校参考。

**硬性要求前置判定**（确定性，不消耗 LLM）：

| 类别 | 判定逻辑 | 状态 |
|---|---|---|
| education（学历层级） | JD 明确要求（如"本科及以上"），简历最高学历档位明确低于 → **not_met**；层级达标 → manual_review（真实性无法自动验证，提示人工核实） | not_met / manual_review |
| experience（年限） | JD 要求 N 年、简历 `total_years`/`relevant_years` 明确 <N → **not_met**；无数值不断言 | not_met / manual_review |
| certificate（证书） | JD 用"必须持有/必备"强约束、简历证书列表核心词无匹配 → **not_met**；简历未列证书不断言 | not_met / manual_review |
| 其他 | 简历有关键词依据 → met；无依据 → not_mentioned（不推断不达标） | met / not_mentioned |

`not_met` 为**确定性不达标**，服务端据此扣分/封顶（见 8.5 第 3 条）；`manual_review` 不扣分，仅保留人工核实提示（前端教育经历模块显示"人工待审核"横幅）。

**第一层：确定性预检**（`_precheck_findings`，0 次 LLM，高精度低误报）：

| 预检项 | 对应规则 | 逻辑 |
|---|---|---|
| 薪资疑似编造 | 规则 4 | 报告写了薪资期望，但简历原文无任何薪资字样（正则：薪资/薪酬/工资/N k/万…） |
| 扣分无依据 | 规则 2 | `ai_deduction > 0` 但 `deduction_reasons` 为空 |
| 强断言无出处 | 规则 1 | 优势含"精通/资深/主导…"等强断言词，且其中的技术项（ASCII 词）在简历原文完全未出现 |
| 分数与证据不一致 | 规则 6 | `hard_requirements ≥ 20` 且**过半硬性要求**在简历原文找不到证据（2 字中文片段 + ASCII 词宽匹配） |

**触发深度核查的条件**：预检发现疑点，或报告自报 `ai_risk` 为 medium/high。预检干净且无风险 → 直接通过（`mode: "预检"`），**不消耗 LLM**。

**第二层：LLM 深度核查**：预算门槛 `budget["calls"] < MAX_AGENT_LLM_CALLS - 1`（`MAX_AGENT_LLM_CALLS = 5`）。Prompt 包含报告 JSON、简历原文摘录 `experiences[:8]`（论断必须以此为依据核对）、岗位要求，以及预检疑点（供重点核实）。

**6 条核查规则**（Prompt 模板）：

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
6. 分数与证据是否一致（防分数虚高）
   例如：简历内容很少、无可核验经历，但 hard_requirements 或 skills_projects 打 20/25 以上高分；
   或岗位要求清单有硬性要求（hard=true），简历经历摘录中完全没有对应证据，报告却给高分——应下调对应维度分数。
```

**输出字段**（`hr_analysis.agent_validation`）：

```python
{
    "checked_rules": 6,              # 核查规则总数
    "issues": [
        {"rule": 1, "problem": "论断超出简历依据", "fix": "已修正为'了解 Python，有 2 年开发经历'"}
    ],
    "passed": false,                  # true = 无问题 / 问题已修正
    "mode": "预检",                    # "预检" = 确定性预检通过；"深度" = LLM 深度核查
    "revised": false                  # true = 修订稿已重新生成且通过结构校验后生效
}
```

**修订回退**：深度核查触发修订后，修订稿重新过 `_validate_report`；结构校验不通过（或需求抽取本身失败）时回退保留原报告，`revised` 置回 `false`。

**前端展示**：dashboard 页面「Agent 校验」模块始终显示（只要 `hr_analysis.agent_validation` 存在），**可展开查看检测过程**：
- 全部通过 → 绿色单行徽章「✓ Agent 校验：已核查 N 项要求，全部通过」，可展开查看「检测过程」5 步明细（来自 `analysis.agent_trace.steps`：规划→检索→检索中→生成→校验→自校）
- 检出问题 → 琥珀色折叠徽章「Agent 校验：已核查 N 项要求，检出 N 个问题并已修正」，点「详情」展开问题表格（问题类型/问题/修正三列）
- 右侧导航「Agent 校验」锚点同步存在，可滚动跳转
- 导出报告同样始终输出校验结论（通过时一行绿色 `✓ Agent 校验`，有问题时带问题表）

**教育经历人工待审核**：dashboard 教育经历模块在 `education_history` 非空时显示警示横幅「学历信息为简历自述，未经权威渠道核验，请结合学信网或学历/学位证书原件复核（人工待审核）。」——与硬性要求判定中的 `manual_review` 一致。

### 8.9 跨候选人对比

**位置**：`app._compare_candidates()`，在批量分析（≥2 份简历）完成后调用。

**输入**：N 份候选人的分析结果摘要（姓名、得分、优势、短板），LLM 生成：

| 输出字段 | 说明 |
|---|---|
| `ranking[]` | 排名列表，每项含 `rank` / `name` / `score` / `difference`（核心差异点） |
| `pairwise[]` | 两两对比自然语言描述 |
| `recommendation` | 优先面试建议 |

**分数一致性（关键约束）**：LLM **不允许重新打分**——Prompt 明确排名中的 `score` 仅是占位示例，服务端在后处理（`score_by_name` 映射）用系统 `final_score` **覆盖**每个排名的分数：先精确匹配姓名，再空白不敏感包含匹配兜底；未匹配到 → 0 分；随后按分数降序排序并重新编号 1..N。报告页「候选人排名」得分单元格优先读取 `hr_analysis.final_score`（系统分数），保证排名与报告头部、右侧面板完全一致，杜绝 LLM 自评分造成的显示不一致。

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

### 认证安全加固（邀请制注册 + 登录防爆破）文件清单

| 文件 | 职责 |
|------|------|
| `apps/backend/config.py` | 防爆破参数（`LOGIN_FAIL_LIMIT` / `LOGIN_LOCK_MINUTES` / `LOGIN_FREEZE_WINDOW_MINUTES` / `LOGIN_FREEZE_THRESHOLD` / `LOGIN_MAX_FAIL_PER_IP_10MIN` / `LOGIN_IP_LOCK_MINUTES` / `LOGIN_UNFREEZE_VIA_EMAIL` / `LOGIN_FREEZE_AUTO_UNFREEZE_HOURS`）与邀请码参数（`INVITE_CODE_LENGTH` / `INVITE_CODE_TTL_HOURS` / `INVITE_REQUEST_*`）；🆕 保留策略 `ADMIN_OPS_RETENTION_DAYS`（180）/ `USER_USAGE_RETENTION_DAYS`（365）/ `PRUNE_TOUCH_FILE`（懒清理哨兵）；新增 `INVITE_REQUESTS_DIR` / `INVITE_CODES_DIR` / `INVITE_CODES_AUDIT_DIR` / `LOGIN_FAILURES_DIR` |
| `apps/backend/auth.py` | 🆕 邀请码生命周期：`generate_invite_code` / `validate_invite_code` / `consume_invite_code` / `revoke_invite_code`（支持明文码或 64 位哈希）/ `list_invite_codes` / 申请单 `create_invite_request` / `list_invite_requests`；🆕 登录防爆破：`login_policy_check`（冷却/冻结）/ `record_login_failure` / `ip_login_rate_exceeded` / `record_ip_login_failure` / `clear_login_failures` / `list_frozen_users` / `unfreeze_user` / 邮箱自助解冻；🆕 保留策略懒清理：`prune_admin_ops` / `prune_user_usage` / `maybe_prune` / `_prune_due`（每日最多一次，随查询触发）；自校验长度与过期均基于 config |
| `apps/backend/app.py` | 邀请码路由（`register-config` 含 `invite_code_length`、`invite-code/check`、`invite-code/send-email-code`、`invite-request`、管理员 generate/list/revoke/冻结列表/解冻）；登录防爆破三层拦截（IP 限流 → 冷却/冻结 → 密码校验，423/429/401）；列表接口输出完整 64 位哈希 + `_epoch_or_iso()` 统一过期时间为 ISO 字符串 |
| `apps/backend/mailer.py` | 邀请码邮件（审批通过补发码 / 手动生成邮件通知），HTML + 纯文本双版本 |
| `apps/frontend/app/(default)/login/page.tsx` | 登录/注册/申请邀请码/自助解冻四面板；邀请码校验前置解锁（成功绿色/失败红色+抖动动画）；邀请码输入框 `maxLength` 与占位文案动态取 `invite_code_length`；登录失败次数累计提示（N 次 → 3 次冷却预警 → 6 次冻结）；「申请邀请码」子面板返回按钮置于提交下方；423 按 `frozen_by` 分流（admin → 冻结弹窗含原因+管理员email / auto → 自助解冻面板）；**解冻面板与重置密码页返回按钮均为全宽边框样式** |
| `apps/frontend/lib/api/auth-admin.ts` | 🆕 注册配置/邀请码校验/发码/申请/解冻/管理员全部 API；`RegisterConfig` 含 `invite_code_length`；🆕 审计导出 `downloadAdminOpsExport(op, keyword, csv\|json)`（Blob 下载，文件名取自 Content-Disposition） |
| `apps/frontend/app/(default)/admin/page.tsx` | 邀请码总览 Tab：状态筛选（all/active/used/expired）、手动生成（明文仅展示一次 + 一键复制）、作废（完整 64 位哈希回传，短哈希 `前8…后4` 展示）；冻结账号 Tab（来源/原因列）；🆕 用户管理 Tab（改邮箱/冻结/解冻/管理员标记/使用统计/使用排行，次要操作收「…」菜单）、🆕 操作记录 Tab（类型过滤 + 关键字 + 20 条/页分页 + CSV/JSON 导出 + 保留期提示）、🆕 Tab URL 记忆（`history.replaceState`，刷新不丢）、🆕 弹窗统一 `AdminModal`；`fmtTime` 兼容 epoch 秒/毫秒/ISO 三种时间格式（修复 1970 显示） |
| `apps/frontend/tailwind.config.js` | 🆕 `animation.shake` + `keyframes.shake`（邀请码校验失败抖动反馈） |
| `apps/backend/test_invite_flow.py` | 🆕 邀请码注册全流程单测（11 项，独立进程运行） |
| `apps/backend/test_login_lockout.py` | 🆕 登录防爆破单测（12 项，独立进程运行） |
| `docker-compose.secure.yml` | 🛡️ 生产安全 compose：healthcheck、容器网络与资源限制、非 root 运行等安全加固（与普通 compose 并存，部署时显式指定） |

### 登录验证码 + 账号软删除 + 硬门槛打分优化文件清单

| 文件 | 职责 |
|------|------|
| `apps/backend/config.py` | 🆕 验证码参数（`CAPTCHA_ENABLED` / `CAPTCHA_TTL_SECONDS`）、超级管理员白名单 `ADMIN_EMAILS`、软删除窗口 `DELETED_RESTORE_WINDOW_DAYS` |
| `apps/backend/auth.py` | 🆕 验证码：`new_captcha` / `verify_captcha`（一次性 sha256，TTL 300s）/ `_purge_stale_captchas`；🆕 账号软删除：`delete_user`（软删置标记）/ `list_deleted_users` / `restore_user`（恢复后 `password_version`+1）/ `is_user_deleted`；`decode_jwt` 拒绝已删除账号；审计新增 `user_restore` 操作类型 |
| `apps/backend/app.py` | 🆕 `GET /api/v1/auth/captcha`、登录路由验证码前置校验（失败不记账）；🆕 `DELETE /api/v1/admin/users/<username>`（仅超管 + 验证操作者管理员密码 + 软删除）、`GET /api/v1/admin/users/deleted`、`POST /api/v1/admin/users/<username>/restore`；🆕 打分统一化：`_normalize_hr_analysis` 读取 `requirements_checklist` 的 `not_met` 做确定性扣分（每条 10 分上限 30）+ 学历封顶 59 / 其他封顶 69；🆕 `_compare_candidates` 后处理用系统 `final_score` 覆盖 LLM 排名分数并重排 |
| `apps/backend/screening_agent.py` | 🆕 硬门槛确定性判定：`_check_education_gate`（学历层级）/ `_check_experience_years_gate`（年限）/ `_check_certificate_gate`（证书核心词）接入 `_build_requirements_checklist` 产出 `not_met`；🆕 自校预检规则 6（分数-证据一致性，2 字片段宽匹配），深度核查规则 5 → 6 |
| `apps/frontend/app/(default)/login/page.tsx` | 🆕 四位验证码 UI（数字展示 + 刷新、`captchaUnavailable` 降级、登录失败自动刷新、切换模式清空） |
| `apps/frontend/app/(default)/admin/page.tsx` | 🆕 删除账号需弹窗输入管理员密码（仅超管可见删除按钮）；🆕 「已删除账号」恢复区（90 天内可恢复）；操作记录增加 `user_restore` 标签 |
| `apps/frontend/app/(default)/dashboard/page.tsx` | 🆕 排名得分单元格改读 `hr_analysis.final_score`（与头部一致）；移除硬性要求矩阵展示；教育经历人工待审核横幅；「Agent 校验」可展开查看检测过程 5 步明细 |
| `apps/frontend/lib/api/auth-admin.ts` | 🆕 `fetchCaptcha` / `adminDeleteUser`（带 `admin_password`）/ `fetchDeletedUsers` / `adminRestoreUser` |
| `apps/backend/test_captcha.py` | 🆕 验证码单测（7 项，独立进程运行） |
| `apps/backend/test_user_admin.py` | 🆕 用户管理 + 软删除/恢复单测（36 项，独立进程运行） |
| `apps/backend/test_hr_analysis.py` | 🆕 硬门槛扣分/封顶与自校规则 6 专项测试（11 项新增，并入业务套件） |
