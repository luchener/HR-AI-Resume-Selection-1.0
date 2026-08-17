# 1.0.0 验证报告

## 自动化检查

| 检查 | 结果 |
| --- | --- |
| Next.js production build | 通过 |
| TypeScript 类型检查 | 通过 |
| 后端 `unittest` | 21 项通过 |
| 无真实模型 HTTP E2E | 7 项通过 |
| Python `py_compile` | 通过 |
| 后端 `/ping` | HTTP 200 |
| Next.js `/api` 到 Flask 代理 | 通过 |
| AI 配置非法参数校验 | HTTP 422 |

## 覆盖场景

- PDF/DOCX 上传限制与 `application/octet-stream` DOCX 兼容。
- 简历与岗位上传阶段不调用模型。
- 单候选人和三候选人并发分析。
- 单个候选人失败时保留其余结果。
- 模型 JSON 截断、reasoning content 和修复重试。
- 分数、招聘建议、适配标签与 AI 美化扣分标准化。
- 请求级模型配置透传、连接测试和缓存隔离。
- DeepSeek 默认模型配置从旧值迁移到 `deepseek-v4-flash`。
- 桌面与移动端模型配置弹窗布局、必填校验和自定义服务商切换。

测试材料由 `apps/backend/test_helpers.py` 在运行时生成，不使用真实候选人简历。

## 未包含

自动化测试不会使用仓库维护者的真实 API Key。执行完整 E2E 前，需要在浏览器或后端 `.env` 中提供有效模型凭证；完整测试会产生少量模型 token 消耗。
