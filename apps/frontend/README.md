# AI 简历智选前端

基于 Next.js 15、React 19、TypeScript 和 Tailwind CSS 4 的招聘筛选工作台。

## 主要模块

- `app/(default)/page.tsx`：统一上传工作台。
- `app/(default)/dashboard/page.tsx`：多候选人分析报告与深度优化。
- `components/workbench/ai-model-config.tsx`：AI 模型配置中心。
- `components/workbench/analysis-workbench.tsx`：简历/JD 提交和分析流程。
- `lib/api/screening.ts`：后端 API 与 SSE 客户端。
- `public/a4cv/`：Resume Studio 静态应用。

## 开发

```bash
cp .env.sample .env
npm ci
npm run dev -- -p 3003
```

默认通过 `BACKEND_INTERNAL_URL=http://127.0.0.1:9001` 将浏览器的同源 `/api/*` 请求转发到 Flask。

## 检查与构建

```bash
npm run lint
npm run build
npm run start
```

`NEXT_PUBLIC_API_URL` 为空时使用同源代理；设置为完整 URL 时由浏览器直接访问后端。修改 `NEXT_PUBLIC_*` 变量后需要重新构建。
