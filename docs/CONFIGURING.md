# 配置说明

项目使用两个本地环境文件：

- `apps/backend/.env`：后端运行、服务端默认模型和安全设置。
- `apps/frontend/.env`：浏览器 API 地址与 Next.js 后端代理。

从同目录 `.env.sample` 复制后使用。`.env` 已被 Git 忽略。

## 浏览器 AI 模型配置

推荐用户在页面右上角“配置 AI 模型”中设置：

- DeepSeek（默认）：Base URL `https://api.deepseek.com`，模型 `deepseek-v4-flash`。
- 其他：任意 OpenAI Chat Completions 兼容 Base URL 和模型 ID。

API Key 保存在当前浏览器 `localStorage`，仅在连接测试或分析请求中发送给后端。后端不会持久化请求级 API Key。多人并发请求不会共用客户端，缓存也按模型配置隔离。

## 后端环境变量

```env
ENV="local"
SESSION_SECRET_KEY="change-me"
LLM_API_KEY=""
LLM_BASE_URL="https://api.deepseek.com"
LLM_MODEL="deepseek-v4-flash"
BACKEND_PORT=9001
```

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `ENV` | 否 | `local` 或 `production` |
| `SESSION_SECRET_KEY` | production 必填 | production 不允许使用 `change-me` |
| `LLM_API_KEY` | 否 | 旧客户端或无人值守场景的服务端默认 Key |
| `LLM_BASE_URL` | 否 | 服务端默认 OpenAI 兼容 Base URL |
| `LLM_MODEL` | 否 | 服务端默认模型；兼容历史变量 `LL_MODEL` |
| `BACKEND_PORT` | 否 | 本地默认 `9001`；Docker 内部使用 `8000` |
| `ALLOWED_ORIGINS` | 否 | 跨域前端来源，逗号分隔 |
| `LOG_DIR` | 否 | 日志目录 |

## 前端环境变量

```env
NEXT_PUBLIC_API_URL=""
BACKEND_INTERNAL_URL="http://127.0.0.1:9001"
```

- `NEXT_PUBLIC_API_URL` 为空：浏览器访问同源 `/api/*`，由 Next.js 转发。
- `NEXT_PUBLIC_API_URL` 为完整 URL：浏览器直接访问后端，需要正确配置 CORS。
- `BACKEND_INTERNAL_URL`：Next.js 服务端代理访问 Flask 的地址。

生产部署建议保持 `NEXT_PUBLIC_API_URL` 为空，由 Nginx/Next.js 同源代理 API，并强制启用 HTTPS。
