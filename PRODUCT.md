# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

招聘团队多人共用（含管理员）：

- 主要使用者为公司内部 HR / 招聘专员。登录账号后，在同一个工作台提交 1 至 3 份候选人简历和一份岗位描述，系统并发分析并输出可追溯的匹配证据、短板、风险和招聘建议，用于初筛决策（面试、储备、淘汰）。
- 管理员（运维/招聘负责人）通过后台管理邀请码与账号、冻结/解冻用户、查看用量排行与运维数据。
- 系统为公司内部工具，不面向外部求职者或公众。

## Product Purpose

面向公司内部招聘团队的多候选人筛选工作台：把简历和岗位描述集中在一个页面，围绕每次 JD 动态建立量化标准，输出可追溯的匹配证据、短板、风险和招聘建议，让简历初筛既快又准、结果可信可用。

成功标准（用户确认）：缩短初筛时间 + 筛选结果客观、可追溯、少漏人，且 HR 愿意直接采信分析报告。

## Positioning

与开源 Resume Matcher 思路不同，本系统是针对中文招聘场景的重构：

- 不为不同岗位写死技术栈或关键词；分析围绕本次 JD 动态抽取硬性门槛、职责、技能、项目、行业和岗位层级，再用同一套招聘维度评估每位候选人。
- 公开的差异化手段：识别简历 AI 美化程度并将可信度扣分纳入总分；招聘建议与最终分数硬性一致（≥80 优先面试、60–79 储备观察、<60 淘汰）；仅使用 AI 生成招聘分析，不提供本地关键词评分兜底。

## Operating Context

页面流程：登录页 → 工作台 → 分析报告 → Resume Studio 编辑器(/a4cv) 深度优化。

前端页面（Next.js App Router）：首页 / 登录 / dashboard(工作台) / admin(管理后台) / archives(简历档案) / reset-password。

后端 API（Flask，/api/v1/*）按域划分：
- auth：注册、邀请码、图形验证码、邮箱验证码、登录、改密、重置密码、冻结/解冻
- admin：邀请码（生成/吊销）、用户管理（增删改查、禁用）、用量排行、运维导出
- resumes：上传、improve（深度优化）、hr-analysis（HR 分析）、review-markers、improved-markdown
- jobs：上传岗位描述、岗位列表
- archives：简历档案增删改查、标签、分类、回收站
- ai/test：模型连接测试

部署与运行环境：Docker Compose（backend Gunicorn 4 workers + frontend Next.js 双容器）、Nginx 反向代理、生产推荐腾讯云 Ubuntu 24.04 + HTTPS；本地开发 Node 20+/Python 3.12。

模型配置：默认 DeepSeek（deepseek-v4-flash），页面右上角可切换任意 OpenAI Chat Completions 兼容服务商；用户配置存浏览器 localStorage，随请求发送，后端仅在请求生命周期内使用，不写入 JSON 数据或日志；多人并发时每请求独立 LLM 客户端，分析缓存按模型配置隔离。

## Capabilities and Constraints

已确认功能：
- 一次上传 1–3 份简历（PDF/DOCX，每份 ≤30MB）+ 1 份岗位描述，三份简历并发分析，报告页快速切换候选人结果。
- 分析维度：基础信息、工作履历、专业技能、项目证据、竞争力、招聘风险、岗位专项要求、可信度（AI 美化识别）。
- 输出：综合得分、岗位契合度、匹配亮点、项目匹配点、短板、风险、适配标签、招聘建议。
- Resume Studio（a4cv）深度优化与继续编辑。

技术约束：
- 后端存储为 JSON 文件（data/users、resumes、jobs），零数据库依赖，原子写，Docker volume 持久化。
- 生产环境必须 HTTPS；API Key 不落库、不进日志。
- 同时涉及年龄、性别等敏感信息时遵守所在地法律与公司公平招聘政策；分析输出不作为唯一录用依据。
- 当前 JSON 存储适合内部轻量部署；需要审计、权限与高并发时应接入认证与数据库。

## Brand Commitments

- 产品名称：「AI 简历智选」（1.0）。
- 已有品牌资产：apps/frontend/public/brand/resume-screening-logo.svg。
- 界面风格承诺（用户确认）：公司内部风格——稳重、专业、不花哨。

## Evidence on Hand

- README.md：完整产品说明、架构图、技术栈、部署要求。
- docs/：ARCHITECTURE.md、PROJECT_STRUCTURE.md、CONFIGURING.md、SERVER_DEPLOY.md、BAOTA_DEPLOY.md、deploy-ubuntu.md、LOGIN_SECURITY_DESIGN.md、INVITE_CODE_DESIGN.md、E2E_REPORT.md。
- 代码库为篇幅内的主要证据：前端页面与组件、后端路由与 Prompt 模板（prompts.py、screening_agent.py、resume_sanitize.py 等）。
- 明确缺失：仓库不含真实候选人/岗位数据（测试使用匿名 DOCX）；无真实用户评价、客户案例或市场规模数据，后续工作不得编造。

## Product Principles

1. 围绕本次 JD 动态定标准：不为岗位写死关键词或技术栈。
2. 结论必须有可追溯证据：评分、匹配亮点、短板、风险与招聘建议保持内部一致。
3. 速度与质量并重：三份并发分析 + 量化评估，让 HR 愿意直接采信。
4. 公司内部工具审美：稳重、专业、克制，界面服务于效率与可信度，不做炫技动效。
5. 数据与隐私安全：HTTPS、API Key 不落库、公平招聘合规。

## Accessibility & Inclusion

公司内部办公场景，用户为成人办公人员；已确认的硬性约束是涉及年龄、性别等敏感信息时遵守法律与公平招聘政策。未建立 WCAG 等具体可访问性标准，后续按产品级要求补充。