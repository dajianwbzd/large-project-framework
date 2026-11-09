---
# 设计契约 · 视觉（DESIGN.md）— 遵循 Google Labs design.md 规范
# 分工：本文件管「长什么样」（令牌 = 机器可读的规范值；正文 = 人读的理由）。
#       「怎么运作」在 EXPERIENCE.md。两份契约冲突时以契约为准。
# 门禁：完成前必须 `design.md lint` 零 error（无 Node 时用 skills/UI设计师/scripts/design_lint.py）。
# 令牌 → 代码：`design.md export --format json-tailwind|css-vars|dtcg`，前端只引用令牌符号。
# 说明：以下为**可直接 lint 通过的起始示例**，值按项目替换；替换后重跑 lint。
version: alpha
name: <项目名> 设计语言
description: >-
  一句话说明这套设计语言的取向，以及方向来源（direction-<n>-<slug>.html，用户于 <日期> 选定）。
colors:
  primary: "#1A1C1E"        # 必须有 primary；色值须为合法 CSS 颜色
  secondary: "#5C6266"      # 用于次要文字；须在 surface-muted 上也满足 4.5:1（表头即在 muted 底上）
  surface: "#FFFFFF"
  surface-muted: "#F4F5F7"
  border: "#E5E7EB"
  alert: "#B8422E"
  focus: "#1B4DFF"          # 焦点环专用；定义了就必须被某个组件引用（否则 lint 报 orphaned-tokens）
typography:
  h1:
    fontFamily: "-apple-system, Segoe UI, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: 28px
    fontWeight: 700
    lineHeight: 1.25
  body:
    fontFamily: "-apple-system, Segoe UI, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
  label-caps:
    fontFamily: "-apple-system, Segoe UI, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: 11px
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: 0.08em
rounded:
  sm: 4px
  md: 8px
  none: 0px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
components:
  # 子令牌仅限：backgroundColor textColor typography rounded padding size height width
  # 禁止自造子令牌（如 radius/borderColor/accentColor）——lint 会报 invalid-subtoken
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "{spacing.sm}"
  card-surface:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.md}"
    padding: "{spacing.md}"
  row-divider:
    backgroundColor: "{colors.border}"      # 分隔线用 backgroundColor + height 表达（规范无 borderColor 子令牌）
    height: "1px"
  badge-alert:
    backgroundColor: "{colors.alert}"
    textColor: "{colors.surface}"
    rounded: "{rounded.sm}"
    padding: "{spacing.xs}"
  table-header:
    backgroundColor: "{colors.surface-muted}"
    textColor: "{colors.secondary}"
    typography: "{typography.label-caps}"
    padding: "{spacing.sm}"
  focus-ring:
    backgroundColor: "{colors.focus}"
    size: "2px"
---

<!-- 正文顺序锁定（可省略、不可乱序）：
     Overview → Colors → Typography → Layout → Elevation & Depth → Shapes → Components → Do's and Don'ts
     有意省略的章节，建议在 frontmatter 的 omitted 中声明。 -->

## Overview

一句话说清这套设计语言的**取向与理由**（例如"运营值守场景，无障碍优先与零歧义：能用结构表达的就不用装饰表达"）。
同时写清它与其它候选方向的关键差别（为什么没选另外几个方向）。

## Colors

逐个令牌写**用途**与**对比度**，并说明为什么是这些颜色。示例格式：

- **Primary（#1A1C1E）**：主文字与主操作底色。对 `surface` 对比度 <n>:1。
- **Secondary（#6C7278）**：次要文字（元信息、说明）。对比度 <n>:1。
- **Alert（#B8422E）**：告警与破坏性操作。**从不单独使用**——必然同时出现文字标记（颜色不是唯一信息载体）。
- **Focus（#1B4DFF）**：焦点环专用色，<n>px 实线 + 偏移。

## Typography

说明层级策略（**是让字重还是让字号承担层级**）以及为什么。列出各级用途与字重/行高选择理由。

## Layout

容器与最大宽度、纵向节奏（区块间/区块内用哪个间距令牌）、单屏量级（决定密度）、响应式降级规则（如窄屏表格→卡片列表）。

## Elevation & Depth

**明确说明用不用阴影以及为什么**（如"不用阴影：值守场景投影仪下阴影不可辨，层级由规则线与底色表达"）。这是下游最容易各写一套的地方。

## Shapes

圆角策略与理由；有无例外（如头像为圆形，因其表征"人"而非"容器"）。

## Components

每个组件给**视觉规格**（解剖、用色、尺寸、各状态外观）——**行为规格写 EXPERIENCE.md，此处不重复**。

| 组件 | 视觉规格 |
|---|---|
| `button-primary` | 底 `{colors.primary}`、字 `{colors.surface}`、内边距 `{spacing.sm}`、圆角 `{rounded.md}`。悬停/焦点/禁用各写一句 |
| `card-surface` | … |
| `badge-alert` | … |

## Do's and Don'ts

- **Do** <可执行的正面规则，如"用 1px 分隔线划分区块，而不是加阴影">
- **Do** <如"让每种状态除颜色外都有文字标记">
- **Don't** <如"不要用红色底色充当告警信号——颜色不是唯一信息载体">
- **Don't** <如"不要引入第三条灰色阶；层级只由线宽、底色、字重表达">
