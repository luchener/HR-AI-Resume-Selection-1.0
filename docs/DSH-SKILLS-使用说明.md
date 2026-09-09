# DSH Skills 使用说明（impeccable & taste-skill）

本文档说明本项目已安装的两个 AI 设计类 Skill 的用法。
安装位置：`<项目根>/.dsh/skills/`（DSH 项目级技能目录，DSH 会自动发现，无需重启）。
安装方式：使用官方 GitHub 源码包（impeccable-main.zip / taste-skill-main.zip）解压后复制，
与官方 `npx impeccable install` / `npx skills add` 的安装结果一致。
两个技能已出现在 DSH 的技能目录中：`impeccable` 与 `design-taste-frontend`。

- **impeccable**（v4.2.2，Apache 2.0）— 源自 https://github.com/pbakaus/impeccable
- **taste-skill**（DSH 技能名为 `design-taste-frontend`，v2）— 源自 https://github.com/Leonxlnx/taste-skill

---

## 1. impeccable —— 前端界面设计指挥

用途：设计、重设计、审查、打磨任何前端界面（落地页、仪表盘、产品 UI、组件、表单、设置页、
空状态等）。它把 AI 当作品牌级设计总监来用：不产出平庸的"AI 味"界面。

### 启动

每个会话首次使用前运行一次（在项目根目录）：

```
/impeccable init
```

`init` 会检查项目、补齐缺失的产品上下文，并生成 `PRODUCT.md`（产品事实档案）。
首次之后每个会话运行 `impeccable context`（由 SKILL.md 的 Setup 引导，Windows 上用
`.dsh/skills/impeccable/scripts/impeccable.cmd context`）。

### 常用命令（23 个）

| 命令 | 作用 |
|---|---|
| `/impeccable init` | 一次性初始化，写 PRODUCT.md |
| `/impeccable shape <feature>` | 写代码前先规划 UX/UI |
| `/impeccable document` | 从现有代码生成 DESIGN.md |
| `/impeccable extract` | 抽取可复用 token/组件进设计系统 |
| `/impeccable critique <target>` | UX 设计评审（启发式评分） |
| `/impeccable audit <target>` | 技术质量检查（a11y、性能、响应式） |
| `/impeccable polish <target>` | 发布前最终打磨 |
| `/impeccable bolder <target>` | 让平淡的设计更大胆 |
| `/impeccable quieter <target>` | 让过于张扬的设计收敛 |
| `/impeccable distill <target>` | 去芜存菁、简化 |
| `/impeccable harden <target>` | 错误处理、i18n、边界情况 |
| `/impeccable onboard <target>` | 首启流程、空状态设计 |
| `/impeccable animate <target>` | 增加有目的的动效 |
| `/impeccable colorize` | 给单色 UI 加策略性色彩 |
| `/impeccable typeset` | 修正字体层级与排版 |
| `/impeccable layout` | 修正间距、节奏、视觉层级 |
| `/impeccable delight` | 增加记忆点与个性 |
| `/impeccable overdrive` | 突破常规的视觉效果 |
| `/impeccable clarify` | 改进 UX 文案 |
| `/impeccable adapt` | 适配不同设备/屏幕 |
| `/impeccable optimize` | 定位并修复 UI 性能 |
| `/impeccable live` | 浏览器内可视化变体迭代 |
| `/impeccable pin <cmd>` | 把某命令固定为短命令（如 pin audit → /audit） |

不带参数输入 `/impeccable` 会展示情境感知的命令菜单。
也可以直接用自然语言：`/impeccable redo this hero section`。

### 独立检测 CLI（可选）

项目已安装 launcher 脚本，可手动运行设计反模式检测（61 条确定性规则，无需 LLM）：

```
.dsh\skills\impeccable\scripts\impeccable.cmd detect <file-or-dir>
```

在 PowerShell 中请用 `&` 调用，例如：

```
& ".dsh\skills\impeccable\scripts\impeccable.cmd" detect src/
```

首次运行会下载引擎二进制到 `~/.impeccable/bin/`（需要网络，仅一次）。

### 重要约定

- 编辑 UI 前先读 `reference/craft-floor.md`（质量底线与绝对禁止项）。
- 细节打磨（refinement）保留现有身份；整体重设计（redesign）才替换 DESIGN.md。
- 不要为打磨而修补被抛弃的设计方向。
- 运行产生的临时文件在 `.impeccable/` 下，建议按官方模板加入 `.gitignore`。

---

## 2. taste-skill —— 反"Sloppy"前端品味框架

用途：让 AI 生成的前端更有布局、排版、动效与间距品味，避免千篇一律的样板界面。
本仓库主 skill（`design-taste-frontend`，v2 实验版）已安装。

### 使用方式

该 skill 是**自动加载式的指令文件**：DSH 的模型在使用时会根据目录描述自动加载并遵循其规则
（DSH 目录中的技能名为 `design-taste-frontend`），无需像 impeccable 那样敲命令。你只需要正常
提出前端开发需求即可，模型会在合适的时机加载它并按照其风格要求输出。

也可以手动显式要求（在提示词里点名技能名或"taste-skill"）：

```
请加载 design-taste-frontend 技能并按它的要求实现这个页面
```

### 风格旋钮（文件顶部 1-10 数值，只有这一个主 skill 有）

- **DESIGN_VARIANCE**（设计差异度）：布局实验程度。低=居中简洁，高=不对称/现代。
- **MOTION_INTENSITY**（动效强度）：动画深度。低=仅 hover，高=滚动/磁性效果。
- **VISUAL_DENSITY**（视觉密度）：每屏信息量。低=留白多，高=密集仪表盘风格。

### 同仓库其他变体（未安装，按需另装）

| 安装名 | 说明 |
|---|---|
| `design-taste-frontend-v1` | v1 原版 |
| `gpt-taste` | GPT/Codex 更严格变体 |
| `image-to-code` | 图片→分析→编码流水线 |
| `redesign-existing-projects` | 先审计再改现有项目 |
| `high-end-visual-design` | 柔和高端视觉 |
| `full-output-enforcement` | 强制完整输出、禁占位注释 |
| `minimalist-ui` | Notion/Linear 风极简 UI |
| `industrial-brutalist-ui` | 瑞士字体硬核风格 |
| `stitch-design-taste` | Google Stitch 兼容规则 |
| `imagegen-frontend-web` / `-mobile` / `brandkit` | 图像生成类（产出参考图） |

官方安装：`npx skills add https://github.com/Leonxlnx/taste-skill --skill "<安装名>"`

---

## 3. 卸载 / 更新

- 卸载：删除 `.dsh/skills/impeccable`（或 `taste-skill`）目录即可，DSH 会自动停用。
- 更新原则：用新版 zip 重新解压后覆盖同名目录即可（内容就地替换）。
- 官方 CLI 安装方式（需可访问 npm 网络时）：`npx impeccable install` 安装/更新 impeccable；
  `npx skills add https://github.com/Leonxlnx/taste-skill` 安装 taste-skill。
- 全局安装状态：两个 skill 已同时安装到项目级 `.dsh/skills/` 和用户级
  `C:\Users\3.0368\.dsh\skills\`（所有项目均可用）。项目级副本优先级更高，
  如需只保留全局副本，删除项目级目录即可。