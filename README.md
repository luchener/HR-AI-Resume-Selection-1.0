# AI 简历智选 1.0

<img width="2866" height="1577" alt="image" src="https://github.com/user-attachments/assets/24d1c4a2-24ff-4041-bba3-222768e90bec" />


AI 简历智选是面向公司内部招聘团队的多候选人筛选工作台。HR 可以在一个页面中提交 1 至 3 份 PDF/DOCX 简历和一份岗位描述，系统根据当前岗位要求动态建立量化标准，输出可追溯的匹配证据、短板、风险和招聘建议。

系统不会为不同岗位写死技术栈或关键词。分析提示会围绕本次 JD 提取硬性门槛、职责、技能、项目、行业和岗位层级，再用同一套招聘维度评估每位候选人。

## 核心能力

- 一次上传 1 至 3 份简历，每份最大 30 MB，支持 PDF 和 DOCX。
- 简历与岗位描述集中在同一个工作台，减少页面跳转。
- 三份简历并发分析，候选人结果可在报告页快速切换。
- 从最高学历记录中提取学历、学校名称、院校层次、专业和毕业时间。
- 按基础信息、工作履历、专业技能、项目证据、竞争力、招聘风险和岗位专项要求进行量化评分。
- 输出综合得分、岗位契合度、匹配亮点、项目匹配点、短板、风险、适配标签和招聘建议。
- 招聘建议与最终分数保持一致：80 分及以上优先面试，60 至 79 分储备观察，60 分以下淘汰。
- **硬性门槛确定性校验**：JD 中的学历层级、工作年限、必备证书等硬性要求由服务端确定性判定——不达标直接扣分并封顶（学历不达标封顶淘汰级），不再全靠模型自觉；学历真实性仍保留人工核实提示。
- 识别简历 AI 美化程度，并将可信度扣分纳入最终得分。
- Agent 分层自校（确定性预检 + LLM 深度核查，6 条规则），报告页可展开查看检测过程；排名分数统一取系统最终分，与报告头部一致。
- 提供深度优化结果，并可进入内置 Resume Studio 继续编辑。
- 仅使用 AI 生成招聘分析，不提供本地关键词评分兜底。

## AI 模型配置

默认使用服务端配置，页面不提供自定义模型配置：

- 默认服务商：DeepSeek（`LLM_BASE_URL=https://api.deepseek.com/v1`）。
- 默认模型：`deepseek-v4-flash`。
- API Key 由后端 `.env` 统一配置，用户无需也不可能自行输入。

`ALLOW_CUSTOM_AI_CONFIG=on` 时，页面右上角显示模型配置中心：支持任意 OpenAI Chat Completions 兼容接口、API Key 显隐/清除/连接测试/模型切换；配置保存在当前浏览器 `localStorage` 中，分析时随请求发送，后端只在请求生命周期内使用，不写盘不入日志，分析缓存按模型配置隔离。**生产环境默认关闭（`false`）**，防止绕过管理员配置的模型与密钥。

## 分析维度

| 维度 | 主要内容 |
| --- | --- |
| 基础信息 | 最高学历、学校与层次、统招状态、专业、毕业时间、所在地、薪资、证书 |
| 工作履历 | 总年限、相关年限、行业、公司背景、岗位层级、管理规模、稳定性、空窗期 |
| 专业能力 | JD 硬技能、工具熟练度、软实力、职责覆盖度 |
| 项目证据 | 项目类型、规模、负责模块、量化成果、同行业案例 |
| 竞争力 | 业绩、荣誉、实习、附加能力、持续学习 |
| 招聘风险 | 履历断层、频繁跳槽、时间冲突、能力断层、地点/薪资不符、技能空白 |
| 岗位专项 | 根据管理、技术、销售、应届生等岗位类型动态生成 |
| 可信度 | AI 美化程度、模板化表达、缺少事实或量化证据 |

## 登录与账号安全

- **四位数字图形验证码**：登录前置校验，一次性使用、300 秒有效，验证码错误不计入防爆破失败次数。
- **登录防爆破三层**：IP 限流（10 分钟 10 次）→ 账号冷却（连续失败 3 次暂停 5 分钟）→ 账号冻结（窗口内 6 次失败冻结，支持邮箱自助解冻）。
- **注册邀请制**：新账号需管理员审批发放邀请码，注册时校验邮箱验证码 + 邀请码。
- **账号软删除**：删除账号后 90 天内可恢复；**仅超级管理员**（`.env` 白名单邮箱）可删除，删除时须验证操作者管理员密码；恢复后旧会话全部失效需重新登录。
- 密码使用 PBKDF2-SHA256（200k 迭代）+ 随机盐存储，不落明文；改密/重置后旧 JWT 全部失效。
- 管理员操作全程审计（删除/冻结/权限变更/重置密码等），支持 CSV/JSON 导出。

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
│  ├─ auth.py    JWT 认证 + 用户管理 + 验证码 + 软删除           │
│  ├─ app.py     路由 + HR 分析编排（ThreadPool 并发）            │
│  ├─ screening_agent.py   Agent 分析（需求抽取→经验匹配→自校）   │
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
│   ├── backend/          # Flask 后端（核心 py 文件）
│   │   ├── auth.py       # JWT、用户、密码、验证码、软删除与邀请码
│   │   ├── app.py        # 路由 + 分析编排 + 统一打分
│   │   ├── screening_agent.py # Agent 分析（需求抽取→经验匹配→报告→自校）
│   │   ├── store.py      # JSON 存储（原子写）
│   │   ├── config.py     # 配置（.env 读取）
│   │   ├── mailer.py     # SMTP HTML/纯文本验证码邮件
│   │   ├── reset_password_cli.py # 管理员重置无邮箱账号
│   │   └── llm.py / parser.py / prompts.py / run.py
│   │       # LLM 调用、文档解析、Prompt、启动辅助
│   │   ├── .env          # 密钥（gitignore，不入库）
│   │   └── Dockerfile
│   └── frontend/         # Next.js 前端
│       ├── app/          # 页面路由（login/dashboard/admin/archives）
│       ├── components/workbench/  # 工作台组件 + auth-context
│       ├── lib/api/      # API 封装（带 JWT 头）
│       ├── public/a4cv/  # 独立简历编辑器
│       └── Dockerfile
├── docker-compose.yml
├── docker-compose.secure.yml  # 生产安全加固 compose
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
GitHub 部署的正确姿势（三步）
下载仓库后，用样例模板生成：
cp apps/backend/.env.sample apps/backend/.env
编辑填入真实配置：LLM API Key、SMTP 邮箱密码、JWT_SECRET_KEY（必填，随机长字符串）、ADMIN_EMAILS（管理员白名单）等

## 数据与安全

- `apps/backend/data/` 保存运行时简历与岗位 JSON，已被 Git 忽略。
- `.env`、日志、构建目录、依赖目录和本地测试文件均已被 Git 忽略。
- 验证码（登录图形验证码/邮箱验证码）磁盘只存哈希，一次性消费，文件泄露无法直接利用。
- 登录失败记录、邀请码均以 sha256 哈希作文件名落盘，磁盘不落明文用户名标识。
- 仓库测试使用运行时生成的匿名 DOCX，不包含真实候选人信息。
- 公司多人使用建议集中部署，不建议把本地 `data/` 目录同步到公共位置。
- 生产环境必须使用 HTTPS，并限制内部系统访问范围。
- 当前 JSON 文件存储适合内部轻量部署；需要审计、权限和高并发时，应接入认证与数据库。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) | 项目架构、登录安全（验证码/防爆破）、数据存储、打分机制（含硬门槛扣分）、API、Docker 部署与已知问题 |
| [docs/LOGIN_SECURITY_DESIGN.md](./docs/LOGIN_SECURITY_DESIGN.md) | 登录安全与账号生命周期设计方案 |
| [docs/INVITE_CODE_DESIGN.md](./docs/INVITE_CODE_DESIGN.md) | 邀请制注册设计方案 |
| [docs/CONFIGURING.md](./docs/CONFIGURING.md) | 配置说明 |
| [docs/deploy-ubuntu.md](./docs/deploy-ubuntu.md) | 腾讯云 Ubuntu 24.04 完整部署指南 |

## 说明

项目基于 Resume Matcher 的开源思路进行中文招聘场景重构，并集成 [a4cv](https://github.com/irenerachel/a4cv) Resume Studio。当前版本重点服务招聘筛选，不将输出作为唯一录用依据；涉及年龄、性别等敏感信息时，应遵守所在地法律与公司的公平招聘政策。

## License

见 [LICENSE](./LICENSE)。
