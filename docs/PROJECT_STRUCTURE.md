# 项目结构

本文档描述 AI 简历智选 1.0 当前有效源码。依赖、构建缓存、运行数据、日志和本地简历不属于源码。

## 完整目录

```text
HR-AI-Resume-Selection-1.0/
├─ apps/
│  ├─ backend/
│  │  ├─ app.py                         # Flask 路由、并发分析、评分标准化、SSE
│  │  ├─ config.py                      # 环境变量、目录、CORS 和生产校验
│  │  ├─ llm.py                         # 请求级模型客户端、兼容调用、JSON 修复
│  │  ├─ parser.py                      # PDF / DOCX 文本提取与清洗
│  │  ├─ prompts.py                     # JD 驱动的招聘分析和深度优化提示词
│  │  ├─ store.py                       # 简历与岗位 JSON 原子存储
│  │  ├─ run.py                         # 本地 Flask 启动入口
│  │  ├─ test_helpers.py                # 运行时生成匿名 DOCX 测试材料
│  │  ├─ test_hr_analysis.py            # 招聘分析、模型隔离和接口单元测试
│  │  ├─ test_e2e.py                    # 运行中服务的 HTTP 冒烟测试
│  │  ├─ requirements.txt               # Python 直接依赖
│  │  ├─ Dockerfile                     # Gunicorn 后端镜像
│  │  └─ .env.sample                    # 后端配置模板
│  └─ frontend/
│     ├─ app/
│     │  ├─ layout.tsx                  # 根布局、字体和 SEO 元数据
│     │  ├─ favicon.ico                 # AI 简历智选 favicon
│     │  └─ (default)/
│     │     ├─ layout.tsx               # AI 模型和分析状态 Provider
│     │     ├─ page.tsx                 # 统一分析工作台入口
│     │     ├─ dashboard/
│     │     │  └─ page.tsx              # 多候选人报告和深度优化页
│     │     └─ css/
│     │        └─ globals.css           # Tailwind 入口与全局样式
│     ├─ components/
│     │  └─ workbench/
│     │     ├─ ai-model-config.tsx      # 模型配置、浏览器存储、连接测试弹窗
│     │     ├─ analysis-context.tsx     # 报告类型、状态与 sessionStorage
│     │     ├─ analysis-workbench.tsx   # 简历/JD 上传、校验和分析流程
│     │     └─ app-shell.tsx            # 侧栏、流程导航和品牌外壳
│     ├─ lib/
│     │  └─ api/
│     │     ├─ config.ts                # 浏览器 API Base URL
│     │     └─ screening.ts             # 上传、分析、模型测试和 SSE 客户端
│     ├─ public/
│     │  ├─ brand/                      # Logo 和 favicon 源文件
│     │  └─ a4cv/                       # Resume Studio 静态应用及本地依赖
│     ├─ package.json                   # 前端脚本和直接依赖
│     ├─ package-lock.json              # 前端依赖锁文件
│     ├─ next.config.ts                 # /api 同源代理与长请求超时
│     ├─ tsconfig.json                  # TypeScript 配置
│     ├─ eslint.config.mjs              # ESLint 配置
│     ├─ postcss.config.mjs             # Tailwind PostCSS 插件
│     ├─ tailwind.config.js             # Tailwind 内容扫描配置
│     ├─ Dockerfile                     # Next.js 前端镜像
│     ├─ README.md                      # 前端开发说明
│     └─ .env.sample                    # 前端配置模板
├─ docs/
│  ├─ PROJECT_STRUCTURE.md              # 本文件
│  ├─ CONFIGURING.md                    # 模型和环境配置
│  ├─ E2E_REPORT.md                     # 当前版本验证结果
│  ├─ SERVER_DEPLOY.md                  # Linux / Nginx 部署
│  ├─ BAOTA_DEPLOY.md                   # 宝塔部署
│  └─ a4cv-integration/                 # Resume Studio 集成资料
├─ docker-compose.yml                   # 前后端容器与数据卷
├─ package.json                         # 根目录统一开发、构建和测试脚本
├─ package-lock.json                    # 根目录工具依赖锁文件
├─ start-resume-matcher.ps1             # Windows 一键启动实现
├─ 一键启动.bat                          # Windows 双击入口
├─ .dockerignore
├─ .gitignore
├─ README.md
└─ LICENSE
```

## 前端边界

```text
app/(default)/page.tsx
  -> AnalysisWorkbench
     -> uploadResume / uploadJobDescription / analyzeResumes
     -> AnalysisContext 保存报告
     -> 跳转 /dashboard

AiModelProvider
  -> localStorage 保存当前浏览器模型配置
  -> /api/v1/ai/test 验证连接
  -> 分析、重分析、深度优化时注入 ai_config

app/(default)/dashboard/page.tsx
  -> 候选人切换与结构化报告
  -> improveResumeStream 深度优化
  -> sessionStorage 交接 Resume Studio Markdown
```

## 后端边界

```text
app.py
  ├─ parser.py        上传阶段只做文档文本提取
  ├─ store.py         保存简历与岗位 JSON
  ├─ prompts.py       根据当前 JD 构造招聘分析任务
  └─ llm.py           使用本次请求的模型配置调用 AI

hr-analysis
  -> 单人直接分析 / 多人最多 3 个线程并发
  -> _normalize_hr_analysis 统一字段和分数
  -> 模型配置指纹参与缓存键
  -> 返回单人结果或 batch_analyses
```

## 运行时目录

以下路径会自动生成，已通过 `.gitignore` 排除：

- `node_modules/`、`apps/frontend/node_modules/`：Node.js 依赖。
- `apps/frontend/.next/`、`apps/frontend/out/`：Next.js 构建产物。
- `apps/backend/.venv/`、`.venv/`：Python 虚拟环境。
- `apps/backend/data/`：简历与岗位 JSON，可能包含个人信息。
- `apps/backend/logs/`、`*.log`：运行日志。
- `__pycache__/`、`.pytest_cache/`、`*.pyc`：Python 缓存。
- 各级 `.env`：API Key 和本机配置。

测试不依赖仓库内的真实简历。`test_helpers.py` 会在内存中生成匿名 DOCX。
