# 邀请码注册机制设计（v3 — 参数定稿）

> v3 变更记录：申请理由必填；邀请码 **4 位**、字母+数字混合、**不区分大小写**；有效期默认 **24 小时**；拒绝可填理由并可发拒信；**同一邮箱被拒满 3 次后不再接收新申请**；管理员白名单默认 `luchenstudio@163.com`；界面规范：**不使用 emoji**，图标统一 lucide。
>
> 核心闭环（不变）：申请（公开）→ 审批（管理员）→ 自动发码邮件 → 注册三步前置校验（仅最后一步消费）。本版为定稿，评审通过后按 §10 实施。

## 0. 一句话结论

- 邀请码 = **4 位字母数字（不区分大小写）**，一次性、**强绑定申请邮箱**、默认 **24 小时**有效，磁盘只存哈希。
- 必须**输入邀请码并校验通过后**，才可填写邮箱、发送邮箱验证码（前端 UI 锁定 + 服务端强制校验双保险）。
- 邀请码由 **用户申请（理由必填）→ 管理员审批（邮箱白名单）→ 系统发码邮件** 产生。
- 管理员可填写拒绝理由；同一邮箱申请被拒满 **3 次**后，系统不再接收该邮箱的新申请。

> **4 位码为什么安全（重要论证）**：单看 4 位（32 字符表 ≈ 105 万组合）熵很低，但本设计里**光有码注册不了** —— 注册必须同时满足：① 码有效未用；② 邮箱 == 码绑定的申请邮箱；③ 能收到该邮箱的邮箱验证码。攻击者即使猜中一个有效码，也进不了绑定邮箱收验证码。叠加一次性 + 24h 过期 + 每 IP 失败限流 + 单码尝试锁定，4 位码在本链路中可接受（管理员主动外发的短码也更便于口头/短信传达）。

---

## 1. 已确认参数（定稿）

| 参数 | 值 | 说明 |
|---|---|---|
| 邀请码长度 | **4 位** | 字母+数字混合（32 字符安全表，排除 0O1lI），**不区分大小写**，统一按大写规范化 |
| 邀请码有效期 | **24 小时** | `expires_at = 批准时刻 + 24h` |
| 邀请码使用 | 一次性 + 强绑定申请邮箱 | 注册后立即作废 |
| 申请理由 note | **必填** | 2–200 字符，供管理员判断 |
| 拒绝理由 | 管理员可填（选填） | 填写则发拒信邮件（含理由）；不填则不打扰 |
| 拒绝上限 | **同一邮箱被拒 ≥3 次 → 不再接收新申请** | 返回明确提示，需管理员 CLI 放行 |
| 管理员白名单 | **`luchenstudio@163.com`** | `.env` 的 `ADMIN_EMAILS` 默认值 |
| 邮箱验证码 | 保留，逻辑不变 | 找回密码链路零改动 |
| UI | **全程不用 emoji** | 图标统一 lucide-react，沿用现有设计系统 |

---

## 2. 端到端流程

```
【A. 申请】（公开 · 免登录 · 理由必填）
申请人: 注册页 → 「申请邀请码」→ 填邮箱 + 申请理由(必填) → 提交
服务端: POST /api/v1/invite-request {email, note}
        ├─ 校验：格式 / note 必填 / 同邮箱仅 1 条 pending / 每 IP 每日 3 条 / 60s 冷却
        ├─ 黑名单：该邮箱历史被拒 ≥3 次 → 429 拒绝接收（不发邮件）
        └─ 生成 data/invite_requests/<id>.json (status=pending)

【B. 审批】（管理员 luchenstudio@163.com · 登录后「账号管理 → 申请审批」页）
管理员: 待审批列表 → 点「同意」/「拒绝」
同意: POST /api/v1/admin/invite-requests/<id>/approve
      ├─ 锁内复检 pending → 生成 4 位一次性码（绑定申请邮箱）→ 落盘
      ├─ request 标记 approved + code_hash + reviewed_by/at
      └─ SMTP 发送邀请码邮件（24h 内有效）→ 失败标记 code_sent=false，可「补发」
拒绝: POST /api/v1/admin/invite-requests/<id>/reject {reason?}
      └─ status=rejected + 记录原因；reason 非空 → 发拒信邮件；被拒次数累计 ≥3 → 该邮箱后续申请被拒收

【C. 注册前置校验】（公开 · 非消费）
申请人: 输入邀请码 → 点「验证」
服务端: POST /api/v1/auth/invite-code/check   （限流；不消费）
        └─ 通过 → 前端解锁【邮箱输入框 + 发送验证码按钮】
申请人: 填邮箱(须与码绑定邮箱一致) → 点「发送验证码」
服务端: POST /api/v1/auth/invite-code/send-email-code {email, invite_code}
        ├─ 非消费校验邀请码；码与邮箱不匹配 → 422
        └─ 通过 → 生成邮箱验证码并发送（现状不变）

【D. 注册】（公开 · 最终消费）
服务端: POST /api/v1/auth/register {username,password,email,code,invite_code}
        ├─ ① 非消费校验邀请码 + 绑定邮箱匹配
        ├─ ② 消费式校验邮箱验证码（verify_email_code_once，现状）
        └─ ③ 文件锁内：复检邀请码 → 建号 → 邀请码回填+归档+删除（一次性）
失败语义：①②③ 任一失败都不消费邀请码；只有建号成功才扣减。
```

---

## 3. 数据模型与存储

### 3.1 目录
```
data/
├── users/                  # 现状
├── password_resets/        # 现状（邮箱验证码）
├── invite_requests/        # 新增：申请单
├── invite_codes/           # 新增：活跃邀请码（文件名=sha256）
└── invite_codes_audit/     # 新增：已消费码归档（审计）
```

### 3.2 申请单 `data/invite_requests/<request_id>.json`
```jsonc
{
  "request_id": "uuid",
  "email": "applicant@example.com",   // 规范化小写
  "note": "…",                        // ★ 必填（2–200 字符）
  "status": "pending",                // pending | approved | rejected
  "created_at": "",
  "ip_hash": "sha256(ip)",            // 防滥用审计
  "reviewed_by": null, "reviewed_at": null,
  "code_hash": null,                  // 批准后回填
  "code_sent": false, "sent_at": null,
  "reject_reason": null               // 管理员填写 → 发拒信邮件
}
```

### 3.3 邀请码 `data/invite_codes/<code_hash>.json`
```jsonc
{
  "code_hash": "sha256(规范化 CODE)", // 磁盘零明文
  "bound_email": "applicant@example.com",  // ★ 强绑定
  "request_id": "uuid",
  "created_by": "管理员 user_id",
  "created_at": "",
  "expires_at": "批准时刻 + 24h",     // ★ 24 小时
  "used": false,
  "used_by_user_id": null, "used_at": null,
  "note": ""
}
```

### 3.4 码的格式与规范化
- 字符表沿用安全表 `ABCDEFGHJKMNPQRSTUVWXYZ23456789`（32 字符，排除易混淆 0O1lI），随机取 **4 位**，例如 `A1B2`。
- **不区分大小写**：存储与比较一律用大写；前端输入自动转大写、去空白，提交前统一 `.toUpperCase()`。
- 单码**尝试锁定**：`invite-code/check` 对同一码失败累计达上限（如 10 次）即作废该码（防对单码爆破）。
- 校验（含注册）统一错误文案 `422 邀请码无效或不可用`，不区分不存在/已用/过期。

---

## 4. 接口设计

### 4.1 公开接口（加入 `_PUBLIC_PATHS`）

| 接口 | 说明 | 限流 |
|---|---|---|
| `POST /api/v1/invite-request` `{email, note}` | 提交申请（note 必填） | 同邮箱 1 条 pending；每 IP 3 条/日；60s 冷却；**被拒≥3 次拒收** |
| `POST /api/v1/auth/invite-code/check` `{invite_code}` | 非消费校验（注册页解锁邮箱区） | 每 IP 失败 10 次/小时 → 429；单码失败 10 次作废 |
| `POST /api/v1/auth/invite-code/send-email-code` `{email, invite_code}` | 发邮箱验证码（必带邀请码，bound_email 匹配） | 现有 60s/邮箱 + 邀请码校验 |
| `POST /api/v1/auth/register` `{username,password,email,code,invite_code}` | 注册（最终消费） | 邀请码校验 + 现状 |

错误码：
- 申请：note 缺失 → `422 请填写申请理由`；同邮箱已有 pending → `409 该邮箱已有申请在审批中`；超限 → `429 申请过于频繁`；**被拒≥3 次 → `429 该邮箱的申请已多次未通过，暂不再接收新申请，如有需要请联系管理员`**。
- 邀请码无效/已用/过期 → 统一 `422 邀请码无效或不可用`。
- 码与邮箱不匹配 → `422 该邀请码仅限申请时填写的邮箱使用`。
- 邮箱验证码错误 → `409`（现状不变）。

### 4.2 管理员接口（`/api/v1/admin/`，白名单鉴权，非管理员 403）

| 接口 | 说明 |
|---|---|
| `GET /api/v1/admin/invite-requests?status=pending\|approved\|rejected` | 申请单列表（邮箱脱敏展示，含被拒历史计数提示） |
| `POST /api/v1/admin/invite-requests/<id>/approve` | 通过：生成 4 位码 + 发码邮件 |
| `POST /api/v1/admin/invite-requests/<id>/reject` `{reason?}` | 拒绝：reason 非空 → 发拒信（含理由） |
| `POST /api/v1/admin/invite-requests/<id>/resend` | 补发码邮件（同一码） |
| `GET /api/v1/admin/invite-codes?status=…` | 邀请码总览（不回明文） |
| `POST /api/v1/admin/invite-codes` `{count,note}` | 兜底手动生成（应急；明文仅此一次返回） |
| `POST /api/v1/admin/invite-codes/<hash>/revoke` | 作废未用码 |

### 4.3 审批原子性
1. 文件锁内：复检 `pending` → 生成码文件 → 回填 request → **先落盘**（并发二次审批返回 409）。
2. 锁外 SMTP 发码邮件：失败 → 返回 502 并提示「管理页补发」，码不重生成。
3. 拒绝不生成码；拒信邮件发送失败不影响 rejected 状态（可人工处理）。

### 4.4 拒绝与拒收规则（核心）
- 每次 reject 写入 `reject_reason`；管理员可填可不填。
- reason 非空 → 调用 `mailer.send_invite_rejection_email(email, reason)` 发拒信（文案含理由 + 联系邮箱）。
- **拒收黑名单**：统计该邮箱（小写归一）历史 `rejected` 申请单数量，**≥3** 时 `invite-request` 直接 429 拒收（不计冷却、不发任何邮件、不留新单）；管理员可 CLI 手动清/放行。
- 配置项 `INVITE_REQUEST_MAX_REJECTIONS=3`（见 §8）。

---

## 5. 管理员识别

- `.env`：`ADMIN_EMAILS="luchenstudio@163.com"`（默认值已定，逗号分隔可加人）。
- 判定：JWT 有效 + 用户 email ∈ 白名单 → 管理员；`/api/v1/admin/*` 非管理员一律 `403 无权限`；接口不回传"谁是管理员"。
- `auth/me` 增加 `is_admin` 字段，供前端显示「邀请码管理」菜单。
- **鸡生蛋**：首个账号 = 管理员先用 CLI（或临时手动生成）取码注册，邮箱用白名单邮箱；之后全部走审批闭环。

---

## 6. 邮件模板（mailer.py 新增 2 个）

1. **发码邮件** `send_invite_email(to_email, code, expires_at)`：复用 `_build_html_email` 品牌卡片；文案写明：邀请码（大字 4 位）、**24 小时内有效**、一次性、仅限本邮箱注册、防骗提示。
2. **拒信邮件** `send_invite_rejection_email(to_email, reason)`：文案含拒绝理由与联系邮箱 `luchenstudio@163.com`。

> 依赖：本机制强依赖 SMTP（发码/拒信/邮箱验证码都走邮件），部署时必须配置 SMTP（现状已有配置项）。

---

## 7. 前端改动与 UI 规范

### 7.1 通用 UI 规范（本次一并执行）
- **不使用 emoji**：所有文案/图标用文字 + lucide-react 图标（现有模式），延续当前设计系统（navy 侧栏、卡片、圆角、focus ring）。
- 图标选型示例：邀请码验证 `TicketIcon`、申请理由 `MessageSquareTextIcon`、审批通过 `CheckCircle2Icon`、拒绝 `XCircleIcon`、补发 `SendIcon`、作废 `BanIcon`。

### 7.2 注册页 `app/(default)/login/page.tsx`（注册 Tab）
```
[申请邀请码]（顶部链接 → 申请子页/弹层：邮箱 + 申请理由(必填, 2–200字) + 提交
              → 提示"已提交，等待管理员审批，邀请码将发送至你的邮箱"）
[邀请码]  4 位，输入自动大写去空格   [验证] → 通过后显示 "已通过邀请码校验"
[用户名] / [密码] / [确认密码]
[邮箱]         ← 邀请码未通过校验前 disabled 置灰
[发送验证码]   ← 同上 disabled
[邮箱验证码]
[注册并登录]
```
- 提交注册再次携带 `invite_code`（服务端才是真防线）。
- 错误提示统一用接口 detail 文案（无 emoji，用错误色块即可）。

### 7.3 管理员「账号管理」页 `/admin`（仅 `is_admin` 显示入口；并入登录防爆破的冻结账号 Tab）
- Tab1「申请审批」（pending/approved/rejected 子切换）：申请邮箱（脱敏如 `l***@163.com`）| 申请理由 | 提交时间 | 该邮箱历史被拒次数 | 操作 [同意] [拒绝(填原因)] [补发码邮件]。
- 同意后：页面提示"已通过，邀请码已发送至该邮箱"（明文不回显，杜绝页面留存；如需口头告知可点「复制码」—— 明文仅在同意响应中出现一次）。
- Tab2「邀请码总览」：状态筛选（活跃/已用/过期/全部）、绑定邮箱、过期时间、使用人；[作废]；「手动生成」表单（应急，明文仅一次展示）。
- Tab3「冻结账号」：冻结列表 + 兜底解冻（见 LOGIN_SECURITY_DESIGN.md）。
- 页面守卫：`auth/me` 无 `is_admin` → 整页"无权限"提示；403 兜底。

---

## 8. 配置项（`.env` / `.env.sample` 定稿值）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ADMIN_EMAILS` | `"luchenstudio@163.com"` | 管理员邮箱白名单（逗号分隔） |
| `INVITE_CODE_TTL_HOURS` | `24` | 邀请码有效期（小时） |
| `INVITE_CODE_LENGTH` | `4` | 邀请码长度（字母数字混合，不区分大小写） |
| `INVITE_REQUEST_NOTE_REQUIRED` | `true` | 申请理由必填 |
| `INVITE_REQUEST_MAX_REJECTIONS` | `3` | 同一邮箱被拒次数上限，达到后拒收新申请 |
| `INVITE_REQUEST_MAX_PER_IP_DAY` | `3` | 每 IP 每日申请数 |

---

## 9. 安全清单（4 位码的补偿性防护）

1. **码本身**：一次性、24h 过期、强绑定邮箱、磁盘零明文（sha256 文件名）、统一错误文案。
2. **使用门槛**：注册 = 码 + 绑定邮箱 + 邮箱验证码三重持有证明 —— 猜中码也无法注册（进不了绑定邮箱）。
3. **爆破防护**：`check`/注册按 IP 失败限流（文件计数，跨 gunicorn worker 有效）；单码失败累计 10 次自动作废。
4. **发码闸门前置**：`email-code/send` 必须带有效邀请码，无法绕过 UI 刷 SMTP。
5. **滥用防护**：申请按 IP/邮箱限流；被拒 ≥3 次邮箱拒收。
6. **审计链**：申请单 request_id → 码 → 使用 user_id；审批/拒绝均记录 reviewed_by/at 与理由。
7. 说明：生产 gunicorn 多 worker 下，纯内存计数不可靠 —— IP/码计数一律**落盘**（`data/rate_limits/`），与现有 JSON 存储一致。

---

## 10. 落地清单（评审通过后实施）

> **状态：已完成（2026-09 实施）**。后端/前端/测试/配置/文档均已落地，测试结果：`test_invite_flow.py` 11/11 OK、`test_login_lockout.py` 12/12 OK、回归批次 76 OK、`smoke_test_auth.py` 47/47。

**后端（apps/backend）**
- [x] `config.py`：§8 配置 + `INVITE_REQUESTS_DIR` / `INVITE_CODES_DIR` / audit / `rate_limits` 目录。
- [x] `auth.py`：邀请码生成/校验/消费；申请单 CRUD + 被拒计数拒收；IP/码限流；`register_with_email_code` 注入邀请码校验。
- [x] `app.py`：4 个公开路由 + admin 路由组 + `require_admin` + `auth/me` 加 `is_admin`。
- [x] `mailer.py`：`send_invite_code_email`（4 位码大字卡片 + 24h 提示）、`send_invite_rejection_email`。
- [x] `invite_cli.py`：gen / requests list|approve|reject / codes list|revoke / resend / unblock（解除拒收）。
- [x] `test_invite_flow.py`：无 note 422；无码/伪码注册 422；check 通过但邮箱不匹配 422；send-email-code 缺码/伪码/不匹配 422；完整注册成功且码一次性；用户名冲突不耗码；过期码 422；被拒 3 次后第 4 次申请 429 拒收；非管理员 403；并发审批 409；码大小写不敏感注册成功。

**前端（apps/frontend）**
- [x] `login/page.tsx`：邀请码验证解锁（禁用邮箱/发码按钮）+ 申请入口（邮箱+理由必填）+ 423 自助解冻子面板。
- [x] `/admin`：Tab1 待审批（同意/拒绝填理由/补发码邮件）+ Tab2 邀请码总览（筛选/手动生成/作废）+ Tab3 冻结账号（兜底解冻）+ is_admin 守卫。
- [x] `app-shell.tsx`：导航按 `is_admin` 显示「账号管理」。
- [x] 全程无 emoji，图标用 lucide（§7.1）。

**配置与文档**
- [x] `.env.sample` 追加 §8 定稿值（`ADMIN_EMAILS="luchenstudio@163.com"` 等）。
- [x] `docs/ARCHITECTURE.md` 同步（注册/解冻路由、数据目录、前端鉴权表）。
- [ ] `docs/deploy-ubuntu.md` 同步（部署文档可后续补邀请码/防爆破小节）。
- [x] v3 评审通过后按本清单实施。

---

## 11. 兼容性与影响面
- 老用户、登录、找回密码、`data/users`、`data/password_resets` 零改动。
- 注册与 `email-code/send` 变破坏性变更（缺邀请码即 422）；同仓同版本发布可接受。
- 冒烟测试直接调用 `auth_module.create_user(...)` 的用例不受影响；走路由的注册用例需预先生成测试邀请码。
- 邮箱验证码发送前置邀请码闸门后，SMTP 流量将由"有效申请人"触发，垃圾骚扰邮件（刷发码）基本杜绝。
