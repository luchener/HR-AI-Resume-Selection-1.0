# 数据保留期与删除（DATA RETENTION）

> 用户上传的简历原文是个人信息，不该永久留在磁盘上。本机制给它一个保留期，
> 到期自动清除；候选人打分结果（归档）不受影响。

## 1. 策略

| 对象 | 到期行为 |
|---|---|
| 简历原文 `data/resumes/<id>.json` | **删除** |
| 简历原始文件 `data/resume_originals/<id>.<ext>` | **删除**（随简历记录一并删除；另回收记录已不存在的孤儿文件） |
| 岗位原文 `data/jobs/<id>.json` | **删除** |
| 归档 `data/archives/<id>.json` | **保留**，只加 `source_purged=true` + `source_purged_at` |
| 审计记录 / 使用统计 | 原有策略不变（180 天 / 365 天） |

理由：归档是筛选结果（分数、匹配标签、报告快照），删了等于毁掉用户的工作成果；
原文才是需要控制留存期的个人信息。归档被标记后，前端提示"原文已清除"，
简历原文标记面板给出「简历原文已不可用（可能已删除，或超过保留期已自动清除）」，
不再回落到裸的 HTTP 404 报错。

## 2. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `RETENTION_ENABLED` | `on` | 总开关（与《隐私政策》30 天口径一致，默认开启）|
| `RETENTION_DAYS` | `30` | 保留天数 |

**默认开启**：系统内《隐私政策》已对外承诺"简历原文最长保留 30 天"，代码默认值必须与之一致，否则文档就是假话。开启状态下的首次运行会一次性删掉所有超期数据（不可逆），因此部署前先看影响面：

```bash
# 容器内
docker exec hr-ai-resume-selection-backend python -m retention --dry-run
# 输出：保留期 30 天 / 超期简历 N 条 / 超期岗位 M 条 / [dry-run] 会标记归档 K 条
```

影响面确认无误后正常启动即可；如需临时关闭，在 `apps/backend/.env` 写 `RETENTION_ENABLED=off` 并重建后端容器。
不带 `--dry-run` 直接执行就是立即清理（`python -m retention`）。

## 3. 触发方式（懒清理，无后台任务）

> ⚠️ **自动清理只在 `ENV=production` 执行**（手动清理 `python -m retention` 不受限制）。
>
> **但别指望这道护栏救本地数据**：`apps/backend/.env` 里 `ENV` 就是 `production`，
> 本机跑测试同样是生产环境。真正兜住数据的是**测试隔离**。
>
> **2026-09-29 真实事故**：`RETENTION_ENABLED` 改为默认 on 后，本地 `data/` 下
> 83 份简历 + 63 个岗位被一次测试进程清空——`test_notifications` / `test_quota` 当时
> 只隔离了自己用到的目录，没隔离 `store.RESUMES_DIR/JOBS_DIR`，第一个请求就触发了
> 自动清理。事后 `store.delete_resume` 用的是 `os.remove`，不进回收站，无法恢复。
>
> **测试模块硬性约定**：任何 import app 且使用 `test_client()` 的测试模块，必须重定向
> `config.DATA_DIR`（在 import app 之前）或 `store.RESUMES_DIR/JOBS_DIR/ARCHIVES_DIR`，
> 否则会在真实 `data/` 上做破坏性操作。

```
任意请求 → app.py 的 before_request
              └─ retention.maybe_purge()
                    ├─ 开关关闭 / 未到期 → 立刻返回（一次布尔判断）
                    ├─ 进程内节流：5 分钟内不重复看哨兵
                    ├─ 磁盘哨兵 data/.last_retention：每天最多真正清理一次
                    └─ purge()：扫描 → 标记归档 → 删原文 → 写日志
```

不引入 APScheduler 之类的常驻任务，也不额外占用启动时间；清理异常一律吞掉，
绝不影响正常请求。多 worker 并发时可能同时进入清理，删除操作是幂等的
（文件不存在即跳过），标记归档带 `source_purged` 幂等判断。

## 4. 过期判定

优先用记录里的 `created_at`（UTC ISO），缺失时退回**文件 mtime**。
目录里的 `_index.json` 等下划线开头的文件一律跳过。

## 5. 手动删除（与保留期互补）

保留期是"到期自动"，手动删除是"现在就要"，都是用户对自己数据的处置权：

| 接口 | 行为 |
|---|---|
| `DELETE /api/v1/resumes/<resume_id>` | 永久删除简历原文；引用它的归档**移入回收站**（可恢复） |
| `DELETE /api/v1/jobs/<job_id>` | 同上，针对岗位原文 |

前端入口：候选人才库列表行内「删除原文」按钮（确认弹窗写清后果；已过保留期清除的记录按钮置灰）。

## 6. 测试

`test_retention.py`（7 例）：只清超期 / 保留未超期 / 归档保留并标记 / dry-run 不落盘 /
开关关闭零动作 / 每日哨兵只清理一次 /
**非生产环境（ENV≠production）绝不自动删（2026-09-29 事故回归）**。
`test_captcha_image.py`（6 例）覆盖验证码图片编码（PNG 结构 + CRC + 像素分布 + 随机性）。
`test_atomic_write.py`（4 例）覆盖原子写的 Windows 重试（杀毒瞬时占用 .tmp → WinError 5）。
