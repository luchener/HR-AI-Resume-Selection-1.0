---
name: AI 简历智选
description: 面向招聘团队的多候选人筛选工作台——严谨、克制、以数据为先的专业工具
colors:
  primary: "#3e6fd3"
  primary-deep: "#1b2a45"
  primary-soft: "#eaf0fb"
  sidebar: "#111c31"
  ink: "#1b273d"
  body: "#435168"
  sub: "#5e7190"
  line: "#dce2eb"
  line-soft: "#e5e9ef"
  soft: "#f3f6fa"
  mist: "#f7f8fa"
  good: "#1d7f5c"
  good-soft: "#e6f7ee"
  good-deep: "#197050"
  good-ink: "#2f6b4a"
  good-border: "#bfe3d0"
  good-border-soft: "#cfe7d8"
  bad: "#b23b4e"
  bad-soft: "#fff4f2"
  bad-border: "#efb5ad"
  bad-hover: "#972f40"
  bad-hover-soft: "#f6d9d5"
  warn: "#8b6514"
  warn-soft: "#fffcf5"
  warn-border: "#efcf8a"
  warn-border-soft: "#e8dfd0"
  warn-border-faint: "#eee3cd"
  warn-panel: "#fffaf0"
  warn-ink: "#8a6d1f"
  warn-ink-deep: "#6b5314"
  warn-strong: "#5a4a10"
  warn-divider: "#f0e2bd"
  warn-btn-border: "#d9c98f"
  warn-btn-hover: "#fdf6e3"
  brand-hover-soft: "#2f5cb8"
  violet: "#995c87"
typography:
  body:
    fontFamily: "Geist Sans, 'PingFang SC', 'Microsoft YaHei', sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
  title:
    fontFamily: "Geist Sans, 'PingFang SC', 'Microsoft YaHei', sans-serif"
    fontSize: "1.25rem"
    fontWeight: 600
    lineHeight: 1.3
  label:
    fontFamily: "Space Grotesk, monospace"
    fontSize: "0.75rem"
    fontWeight: 500
    letterSpacing: "0.05em"
rounded:
  sm: "4px"
  md: "6px"
  lg: "8px"
  xl: "12px"
  full: "9999px"
spacing:
  sm: "8px"
  md: "16px"
  lg: "20px"
  xl: "24px"
  section: "28px"
components:
  button-primary:
    backgroundColor: "{colors.primary-deep}"
    textColor: "#ffffff"
    rounded: "{rounded.md}"
    padding: "0 20px"
    height: "40px"
  button-secondary:
    backgroundColor: "#ffffff"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    height: "40px"
  button-danger:
    backgroundColor: "transparent"
    textColor: "{colors.bad}"
    rounded: "{rounded.md}"
    height: "32px"
  input-field:
    backgroundColor: "#ffffff"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    border: "1px solid {colors.line-soft}"
  card:
    backgroundColor: "#ffffff"
    rounded: "{rounded.md}"
    border: "1px solid {colors.line}"
  chip-status:
    backgroundColor: "{colors.brand-soft}"
    textColor: "{colors.brand}"
    rounded: "{rounded.full}"
---

# Design System: AI 简历智选

## Overview

**Creative North Star: "招聘审计台"**

这套设计语言像一座专业的审计工作台：冷静、秩序、可信。它服务的不是访（landing）而是"用"——HR 每天在这个台面上读简历、对 JD、判匹配。因此一切视觉决策都以「信息可信度」为优先：浅灰底布景让白卡浮出，细边框代替厚重阴影来分区，三色状态系统（成功/警告/危险）让筛选结论一眼可辨。

密度是中高、克制是纪律。页面信息量大（得分、维度、风险列表），所以字号层级平缓、留白适中、动效近乎为零。**设计目的不是让人赞叹界面，而是让人信任结论。**

**Key Characteristics:**
- 扁平为主：主界面无阴影，靠 1px 细边框 + 白卡分区
- 三色语义系统：good（#1d7f5c）/ warn（#8b6514）/ bad（#b23b4e）贯穿状态、徽章、警示条
- 深蓝品牌锚点：侧栏（navy #111c31）与主按钮（brand-deep #1b2a45）构成深色权威框架
- 品牌蓝 #3e6fd3 只用于强调性元素（图标、链接、活跃态、进度条），稀有才有力量
- 表格是信息主战场：细分隔线 + 悬停浅底，让大列表可扫读

## Colors

稳重的冷调商务色板：以深蓝权威为锚、浅灰中性为肤、三色语义为信。所有配色均满足 WCAG AA 对比度（sub #5e7190 在白底 4.9:1）。

### Primary
- **商务蓝** (#3e6fd3): 品牌强调色。仅用于需要引导注意的活跃元素：侧栏活跃项图标、链接 hover、积分要点圆点、按钮 hover 前的浅蓝底徽章、进度条填充。稀缺性即力量——同一屏内出现频率被刻意压低。
- **深靛蓝** (#1b2a45): 主按钮与主要行动面。比商务蓝更沉稳，作为"执行"的视觉语义。
- **浅靛底** (#eaf0fb): 品牌蓝的浅色底座。徽章、图标容器、分类标签的底。
- **墨蓝** (#111c31): 侧栏与深色权威框架。整个应用的"导航权威"由它承担，与内容区浅灰形成最强的明暗对比。

### Neutral
- **墨黑** (#1b273d): 标题与主文字。
- **石青** (#435168): 正文。
- **雾蓝** (#5e7190): 次要文字、表头、辅助信息（过 AA）。
- **雾线** (#dce2eb): 卡片主边框。
- **浅雾线** (#e5e9ef): 内部分隔线、表单边框。
- **浅雾** (#f3f6fa): 页面底色、悬停底。
- **白雾** (#f7f8fa): 表头底、浅灰区块、进度条轨道。

### Semantic
- **成功绿** (#1d7f5c / 浅底 #e6f7ee): 已上传、匹配点、正面建议。
- **危险红** (#b23b4e / 浅底 #fff4f2): 错误、风险预警、拒绝类操作。
- **警示金** (#8b6514 / 浅底 #fffcf5): 时间冲突、待确认、非阻断提示。
- **紫棠** (#995c87): 专用语义——简历 AI 美化程度（可信度扣分）指标的唯一定色。

### Named Rules
**The Rarity Rule.** 商务蓝 #3e6fd3 只在"需要被看到"的地方出现（活跃态、要点、进度）。任何一个屏幕里它都不该超过 10% 的面积——它罕见，才有效。
**The One-Language Rule.** 成功、警告、危险三种状态永远用自己的浅底+深色字组合（good-soft + good 等），永不混用，也不引入第四种近似色。

## Typography

**Display/Body Font:** Geist Sans（中文回退 PingFang SC / Microsoft YaHei）
**Label/Mono Font:** Space Grotesk（编号、步骤、方案标签）

**Character:** 朴素、现代、可信。Geist 的几何感克制得体，中文回退字体衔接自然；几乎不做装饰性排版，字号与字重承担全部层级。

### Hierarchy
- **Page Title**（600 / 1.25rem / 1.3）: 页面主标题与卡片标题。
- **Section Label**（600 / 0.75rem / uppercase + 品牌蓝）: 卡片顶部的编号章节标签（"01 · Resume files"）。
- **Body**（400 / 0.875rem / 1.5, #435168）: 正文内容。
- **Label/Meta**（500 / 0.75–0.8125rem, #5e7190）: 表头、表单标签、辅助说明、文件元信息。
- **Data Emphasis**（600 / 0.875rem, #1b273d）: 表格中的人名、得分（得分用品牌蓝 #3e6fd3 高亮）。

### Named Rules
**The No-Decorative-Type Rule.** 排版只服务于阅读与比较：不加斜体装饰、不做渐变文字、不为强调而放大字号。层级靠字重（600 vs 400）与小字号（0.75 vs 0.875rem）区分，而非花哨。

## Layout

- **应用骨架**: 桌面端 272px 固定侧栏（`lg:grid-cols-[272px_minmax(0,1fr)]`）+ 弹性内容区；移动端侧栏折叠为顶部 2 列网格导航。
- **页面容器**: 内容区浅雾底 #f3f6fa，页面主体为独立白卡 section；页头（header）用 1px 下边框（line）与正文分隔，pb-7。
- **卡片网格**: 度量卡片桌面 4 列（xl:grid-cols-4），平板 2 列，移动 1 列；卡片间用细边框区分而非间距留白。
- **工作台双栏**: 简历上传（minmax(340px,0.82fr)）与 JD 输入（minmax(500px,1.18fr)）双栏并排，JD 区略宽——因为它是分析标准的来源。
- **卡片内边距**: p-5 → sm:p-7（卡片 padding 20–28px），内部分隔用 line-soft。
- **表格**: `rounded-md border border-line bg-white` + overflow-x-auto 横向滚动；th 使用雾蓝 0.75rem 半粗字、行间 line-soft 分隔、悬停浅雾底（hover:bg-soft/60）。
- **弹窗**: 居中 448px（max-w-sm）或 576px（max-w-xl）宽白卡，fixed 全屏遮罩 + shadow-2xl。

## Elevation & Depth

**扁平优先。** 主界面不使用投影：深度由「浅雾底 + 白卡 + 1px 细边框」的明暗分层表达。层级 = 边框的清晰度，而不是阴影的浓度；表面在静止时是平的。

阴影仅保留在两类"浮层"场景：
- **登录/重置密码卡片**: 大环境投影 `0 24px 80px rgba(19,31,51,0.1)` —— 认证面作为"焦点仪式"被抬升；品牌 logo（48px 圆角方块图）挂亮蓝硬阴影签名 `6px 6px 0 var(--color-nav-accent)`，是认证语境的专属记号。
- **弹窗与下拉菜单**: `shadow-2xl`（下拉菜单再加 border-line）。

### 品牌 Logo（resume-screening-logo）
- **资产**: `public/brand/resume-screening-logo.svg`（主）+ `.png`（512）+ `resume-screening-favicon.svg/png`（favicon 精简版）+ `email-avatar.svg/256/512.png`（邮箱头像，圆形深底）。
- **概念**: 深靛蓝圆角方块（brand-deep #1b2a45）内一张白色简历文档，履历行线 + 亮蓝 AI 扫描线（nav-accent #78a0ff）贯穿文档下部，右下角一枚深蓝圆盘 + 薄荷绿勾（#52e0aa）的「评分盖章」徽章——传达"AI 逐行读取简历并量化出可采信的匹配评分"。
- **语义**: 深靛蓝 = 权威（同侧栏/主按钮）；亮蓝扫描线 = AI 量化读取；绿勾 = 匹配通过。favicon 版放大勾/徽章保证 16px 可读；邮箱头像供 luchenstudio@163.com 与产品内管理员联系方式复用。
- **落点**: AppShell 侧栏品牌区（44px）、登录/注册/重置密码认证卡（48px）、浏览器 favicon、OpenGraph 分享图、AppShell 版本卡与冻结弹窗管理员邮箱（email-avatar 圆形小像）。

### 认证语境规格（登录/注册/申请/解冻）
- **布局**: 居中单卡 max-w-md，页面底浅雾 bg-soft，移动端 p-5/sm:p-10 内边距自适应。
- **分段控件**（登录/注册切换）: bg-soft 分段条内两个 36px 高按钮，选中白底+墨字+shadow-sm；命中区 `after:-inset-1` 扩展至触屏 44px。
- **链接色**: 白底上用 text-brand（4.72:1）；浅靛提示块（bg-brand-soft）内用 text-brand-deep（13.07:1）保小字对比。
- **状态反馈**: 成功（bg-good-soft + text-good-ink + text-good 图标）、错误（bg-bad-soft + border-bad-border + text-bad）内联于字段下方或表单底部，role="alert"/"status" 语义完整。
- **冻结提示弹窗**: 与全站弹窗同一套 dialog-behavior（Esc 关闭、焦点陷阱、滚动锁、焦点恢复）。

### Named Rules
**The Flat-By-Default Rule.** 卡片与表格表面静止时一律无阴影。投影是浮层（弹窗/下拉/认证卡）的专属语言，出现在内容表面上即是噪声。

## Shapes

**安静的中度圆角。** 基础半径 8px（`--radius: 0.5rem`），派生阶梯：sm 4px / md 6px / lg 8px / xl 12px / full 9999px。圆角服务于"分区"而不是"可爱"——它让卡片、输入、按钮彼此呼应，但不会抢视觉焦点。

- **控件**（按钮/输入/下拉）: 4–6px（rounded-md），紧凑利落。
- **容器**（卡片/表格/弹窗）: 8px（rounded-md/lg），温和收边。
- **徽章/头像/进度圆点**: full（9999px）。
- 边框 1px，默认 line-soft；卡片主边框用 line。
- 无渐变、无描边装饰、无装饰性几何图形——唯一的"图形个性"是上传区虚线框（border-dashed）+ 品牌蓝图标块（#17243b 底 + 白色图标）。

### Named Rules
**The Small-Radius Rule.** 圆角是辅助声部：控件半径 ≤6px，容器 ≤8px，从不使用超过 12px 的圆角（徽章除外）。大圆角会把专业工具变成"玩具"。

## Components

### Buttons
- **Shape:** 4–6px 圆角（rounded-md），高度 40px（h-9/h-10），内边距 16–20px。
- **Primary（主行动）**: 深靛蓝 #1b2a45 底 + 白字，hover 转 #263a5e（transition-colors 150–300ms）；禁用态换 #c9cfd9 底 + #7a8698 字（约 3:1，可读但退场）。
- **ConfirmDialog（产品级确认弹窗）**: 替代原生 window.confirm——危险操作 danger 用 bad #b23b4e 底 + 白字（5.79:1），hover 转 bad-hover #972f40（7.51:1）；中性确认用 Primary 规格。统一键盘行为：Esc 关闭、背景滚动锁定、默认聚焦「取消」、Tab 焦点陷阱、关闭后焦点恢复（与 AdminModal/抽屉同一套 dialog-behavior）。
- **Secondary（次行动）**: 白底 + 雾线边（line-soft）+ 墨黑字，hover 变浅雾底（hover:bg-mist）。
- **Danger（危险操作）**: 无底 + 危险红字，hover 危险浅底（hover:bg-bad-soft），常见于表格行内操作。
- **Success（正面行动）**: 成功绿 #1d7f5c 底 + 白字，hover #18694d（如"归档"按钮）。
- 表单主按钮（如登录、分析、保存）→ 全宽 h-12，加粗字重，居中。

### Chips & Badges
- 状态徽章: 浅底 + 深字（good-soft/good · warn-soft/warn · bad-soft/bad），rounded-full，内边距 4–12px，0.75–0.8125rem。
- 分类标签: 品牌浅靛底 #eaf0fb + 商务蓝字，rounded，0.6875rem，紧凑（px-1.5 py-0.5）。
- 小字号徽章对比度规则: 字号 ≤0.75rem 的徽章/计数（如"2/3"、步骤点）文字改用深靛蓝 #1b2a45 或成功深档 #197050——浅底 + 基础品牌色在极小字号下仅 4.15–4.46:1，不足 WCAG AA；加深档过线（12.5:1 / 5.44:1）。
- 编号徽章: 白底雾边 rounded-md 或品牌浅靛底 rounded-full，表头/进度步骤用。

### Cards / Containers
- **Corner Style:** 8px（rounded-md/lg）。
- **Background:** 纯白 #ffffff 在浅雾页面底 #f3f6fa 之上。
- **Border:** 1px line #dce2eb（主卡）/ line-soft（内部分组）。
- **Shadow Strategy:** 无（见 Elevation——Flat-By-Default）。
- **Internal Padding:** 20–28px（p-5 → sm:p-7），内部区块用 border-t line-soft 分隔。

### Inputs / Fields
- **Style:** 白底 + 1px 雾线边框（border-line-soft，认证页输入高 48px h-12 + 左图标 40px 内边距）+ 6–8px 圆角。
- **Focus:** 品牌蓝边框（focus:border-brand）+ 2px 品牌蓝/20 轻 ring（focus:ring-brand/20）。
- **Error / Disabled:** 错误红框（border-bad-border）+ 危险浅底（bg-bad-soft）+ 错误聚焦边框（focus:border-bad + focus:ring-bad-border）；disabled 换浅雾底（bg-soft）+ 弱字（text-disabled-fg）或降透明度（opacity-60）。

### Navigation（侧栏）
- **Style:** 墨蓝 #111c31 实底 + 白字；品牌区（logo 44px + "AI 简历智选" + 英文副标）。
- **Nav Item:** 默认透明边 + slate-400 字 + white/5 图标底；hover border-white/10 + white/5 底 + slate-200 字。
- **Active:** 0.5 透明度蓝天边框（border-[#78a0ff]/50）+ 深靛底（#1b2a47），图标换亮蓝 #78a0ff 底 + 墨字 #11203b —— "亮图标 + 深底"是活跃态的固定回响。
- **Mobile:** 2 列网格平铺，每条目保持纵向（图标上、文字下）。
- 底部: 项目信息卡（white/5 底）+ 用户卡片（头像圈 #78a0ff + 首字母、改密/退出图标钮）。

### Upload Dropzone（签名组件）
- 虚线边框（border-dashed）rounded-md，浅雾底 mist；hover/拖入: 边框转商务蓝 + 品牌浅靛底（border-brand bg-brand-soft）。
- 内部: 深靛蓝图标块（size-14 rounded-md）+ 标题 + 说明（PDF/DOCX · 30MB）+ 次按钮"选择文件"。
- 文件列表行: line-soft 边框行 + 品牌浅靛图标块（size-9）；失败行转危险红边框 + 危险浅底。

## Do's and Don'ts

### Do:
- **Do** 用三色语义系统（good/warn/bad 浅底+深字）表达一切状态结论——它让 HR 扫一眼就懂。
- **Do** 用深靛蓝 #1b2a45 作为唯一的主行动色，保持"执行 = 深蓝"的肌肉记忆。
- **Do** 保持卡片扁平：白卡 + 1px 细边框 + 浅灰底，让信息密度可控。
- **Do** 让商务蓝 #3e6fd3 稀缺：只给图标、链接、要点圆点、进度条、活跃态。
- **Do** 用表格承载数据比较（细分隔线 + 行悬停浅底），并允许横向滚动。

### Don't:
- **Don't** 给主内容区的卡片加阴影——扁平是纪律，投影是浮层专属。
- **Don't** 引入第四种状态色或渐变——三色系统 + 紫棠（仅 AI 美化指标）是全部。
- **Don't** 使用装饰性排版：无渐变文字、无斜体强调、无动效表演（仅保留 loading spinner 与失败 shake）。
- **Don't** 让字体层级失真：正文 ≤0.875rem、标题 1.25rem，用字重而非字号膨胀来分级。
- **Don't** 使用超过 12px 的容器圆角（徽章除外）——大圆角破坏工具感。
- **Don't** 使用亮色渐变、紫色系（紫棠除外）、圆角方形图标瓷片等典型 AI 生成痕迹。