# AI 简历智选 1.0

![AI 简历智选](./apps/frontend/public/brand/resume-screening-logo.svg)

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
- 识别简历 AI 美化程度，并将可信度扣分纳入最终得分。
- 提供深度优化结果，并可进入内置 Resume Studio 继续编辑。
- 仅使用 AI 生成招聘分析，不提供本地关键词评分兜底。

## AI 模型配置

页面右上角提供独立的模型配置中心：

- 默认服务商：DeepSeek。
- 默认 Base URL：`https://api.deepseek.com`。
- 默认模型：`deepseek-v4-flash`。
- 其他服务商：支持任意 OpenAI Chat Completions 兼容接口。
- 支持 API Key 显隐、清除、连接测试和模型切换。

配置保存在当前浏览器的 `localStorage` 中。分析或连接测试时，浏览器会把配置随本次请求发送到后端；后端只在请求生命周期内使用，不会把 API Key 写入 JSON 数据或日志。多人同时使用时，每个请求使用独立客户端，分析缓存也按模型配置隔离。

生产环境必须使用 HTTPS。若不希望用户自行管理 Key，也可以在后端 `.env` 中提供服务端默认模型配置。

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
- 服务器可以访问 LLM API。。
## 数据与安全

- `apps/backend/data/` 保存运行时简历与岗位 JSON，已被 Git 忽略。
- `.env`、日志、构建目录、依赖目录和本地测试文件均已被 Git 忽略。
- 仓库测试使用运行时生成的匿名 DOCX，不包含真实候选人信息。
- 公司多人使用建议集中部署，不建议把本地 `data/` 目录同步到公共位置。
- 浏览器模型配置包含 API Key，生产环境必须使用 HTTPS，并限制内部系统访问范围。
- 当前 JSON 文件存储适合内部轻量部署；需要审计、权限和高并发时，应接入认证与数据库。

## 说明

项目基于 Resume Matcher 的开源思路进行中文招聘场景重构，并集成 [a4cv](https://github.com/irenerachel/a4cv) Resume Studio。当前版本重点服务招聘筛选，不将输出作为唯一录用依据；涉及年龄、性别等敏感信息时，应遵守所在地法律与公司的公平招聘政策。

## License

见 [LICENSE](./LICENSE)。
