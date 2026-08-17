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

## 技术栈

| 模块 | 技术与职责 |
| --- | --- |
| 前端 | Next.js 15.3、React 19、TypeScript 5、Tailwind CSS 4、Lucide React |
| 后端 | Python 3.12、Flask 3、Gunicorn 23 |
| AI 接入 | OpenAI Python SDK 1.75、请求级 OpenAI 兼容客户端、结构化 JSON 重试/修复 |
| 文档解析 | `pdfminer.six` 解析 PDF；标准库 ZIP/XML 解析 DOCX |
| 数据存储 | 本地 JSON 文件，原子写入，无数据库依赖 |
| 批量分析 | `ThreadPoolExecutor`，最多 3 位候选人并发且结果相互隔离 |
| 流式输出 | Server-Sent Events（SSE） |
| 简历编辑 | 内置 a4cv Resume Studio 静态应用 |
| 部署 | Docker Compose，或 Nginx + Next.js + Gunicorn |
| 测试 | Python `unittest`、HTTP E2E、TypeScript/Next.js 构建检查、Playwright 页面检查 |

## 系统架构

```text
Browser
  ├─ 上传 1-3 份简历与岗位描述
  ├─ 保存当前用户的 AI 模型配置
  └─ 展示候选人报告 / Resume Studio
          │
          ▼
Next.js 15
  ├─ 工作台与分析报告 UI
  ├─ /api/* 同源反向代理
  └─ sessionStorage / localStorage 会话状态
          │
          ▼
Flask 3
  ├─ parser.py：PDF / DOCX 文本提取
  ├─ prompts.py：JD 驱动的招聘分析策略
  ├─ llm.py：请求级模型客户端与 JSON 修复
  ├─ app.py：并发分析、评分标准化和 API
  └─ store.py：简历与岗位 JSON 存储
          │
          ▼
DeepSeek / 任意 OpenAI 兼容模型服务
```

## 项目结构

```text
.
├─ apps/
│  ├─ backend/                 # Flask API、LLM、解析、存储和测试
│  └─ frontend/                # Next.js 工作台、报告页和 Resume Studio
├─ docs/                       # 项目结构、部署与验证文档
├─ docker-compose.yml          # 前后端容器编排
├─ start-resume-matcher.ps1    # Windows 启动脚本
├─ 一键启动.bat                 # Windows 双击入口
├─ package.json                # 根目录统一脚本
└─ README.md
```

完整文件职责见 [docs/PROJECT_STRUCTURE.md](./docs/PROJECT_STRUCTURE.md)。

## 环境要求

- Node.js 20 或更高版本。
- Python 3.12 或更高版本。
- npm。
- 可用的 DeepSeek API Key，或其他 OpenAI 兼容服务凭证。
- Docker 部署时需要 Docker Engine 20.10+ 与 Docker Compose v2。

## 本地运行

### Windows 一键启动

首次运行先安装依赖：

```powershell
python -m venv apps/backend/.venv
apps/backend/.venv/Scripts/python.exe -m pip install -r apps/backend/requirements.txt
cd apps/frontend
npm ci
cd ../..
```

之后双击 `一键启动.bat`，或执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-resume-matcher.ps1
```

默认地址：

- 前端：`http://127.0.0.1:3003`
- 后端：`http://127.0.0.1:9001`

### 手动启动

复制配置文件：

```bash
cp apps/backend/.env.sample apps/backend/.env
cp apps/frontend/.env.sample apps/frontend/.env
```

启动后端：

```bash
cd apps/backend
python -m venv .venv
# Windows: .venv/Scripts/python -m pip install -r requirements.txt
# Linux/macOS: .venv/bin/python -m pip install -r requirements.txt
.venv/Scripts/python run.py --host 127.0.0.1 --port 9001
```

启动前端：

```bash
cd apps/frontend
npm ci
npm run dev -- -p 3003
```

打开 `http://127.0.0.1:3003`，先在右上角配置并测试 AI 模型，再上传简历和岗位描述。

## Docker Compose

```bash
cp apps/backend/.env.sample apps/backend/.env
# ENV=production 时必须修改 SESSION_SECRET_KEY
docker compose up --build -d
```

Docker 默认仅绑定本机回环地址：

- 前端：`127.0.0.1:3000`
- 后端：`127.0.0.1:8000`

生产环境应由 Nginx 统一提供 HTTPS 和 `/api/` 反向代理。详细步骤见：

- [服务器部署](./docs/SERVER_DEPLOY.md)
- [宝塔面板部署](./docs/BAOTA_DEPLOY.md)

## 环境变量

### 后端 `apps/backend/.env`

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ENV` | `local` | `local` 或 `production` |
| `SESSION_SECRET_KEY` | `change-me` | production 必须改为随机值 |
| `LLM_API_KEY` | 空 | 可选的服务端默认 API Key |
| `LLM_BASE_URL` | `https://api.deepseek.com` | 可选的服务端默认 Base URL |
| `LLM_MODEL` | `deepseek-v4-flash` | 可选的服务端默认模型 |
| `BACKEND_PORT` | `9001` | 本地 Flask 端口；Docker 内部固定为 8000 |
| `ALLOWED_ORIGINS` | 本地开发来源 | 跨域前端来源；同源代理可留空 |
| `LOG_DIR` | `apps/backend/logs` | 后端日志目录 |

### 前端 `apps/frontend/.env`

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NEXT_PUBLIC_API_URL` | 空 | 空值表示浏览器请求同源 `/api/*` |
| `BACKEND_INTERNAL_URL` | `http://127.0.0.1:9001` | Next.js 同源代理访问的后端地址 |

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/ping` | 后端健康检查 |
| `POST` | `/api/v1/ai/test` | 测试浏览器提交的模型配置 |
| `POST` | `/api/v1/resumes/upload` | 上传并解析单份简历 |
| `POST` | `/api/v1/resumes/hr-analysis` | 分析 1 至 3 位候选人 |
| `POST` | `/api/v1/resumes/improve?stream=true` | SSE 深度优化 |
| `POST` | `/api/v1/resumes/improved-markdown` | 生成 Resume Studio Markdown |
| `GET` | `/api/v1/resumes?resume_id=...` | 获取简历数据 |
| `POST` | `/api/v1/jobs/upload` | 保存岗位描述 |
| `GET` | `/api/v1/jobs?job_id=...` | 获取岗位数据 |

## 测试与构建

```bash
# 后端招聘分析测试（不调用真实模型）
cd apps/backend
python -m unittest -v test_hr_analysis.py

# HTTP 冒烟测试；默认生成匿名 DOCX，不依赖真实简历文件
python test_e2e.py --base-url http://127.0.0.1:9001 --skip-llm

# 前端检查
cd ../frontend
npm run lint
npm run build
```

完整 E2E 会调用模型并产生少量 token 消耗：

```bash
python apps/backend/test_e2e.py --base-url http://127.0.0.1:9001
```

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
