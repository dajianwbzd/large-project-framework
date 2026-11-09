# ux_gate —— 体验门禁四层工具（框架通用版，v5.17.0 起）

> 这层工具是 `project-state/契约/EXPERIENCE.md` 里「可验收条款（ux-clause）」的**执行体**：
> 契约写判据，这里跑判据，测试卡/交付记录里的 `machine:ux` 账本写结论。
> **"UX 门通过"不得被说成"体验没问题"** —— L1/L2 通过只能写"结构性体验未见违例"。

## 0. 底线

- **零依赖**：Python 3.8+ 标准库（`zlib` 解 PNG，不装 Pillow / numpy / Playwright / pnpm 包）。
- **不含任何项目的硬编码**：端口、路径、选择器、尺寸、阈值、文案全部来自命令行参数与 `clauses.json`。
- **工具不负责启动应用**：CDP 端口谁开谁负责（各宿主/项目自己的启动脚本）；本目录只管"连上去断言"。
- **不许静默通过**：参数没填 / 规则没实现 / 没有基线 → 一律 `skipped` 且必须写原因；`skipped` 不算失败，但也不算通过。

## 1. 文件

| 文件 | 作用 |
|---|---|
| `clauses.json` | 条款模板（schema `ux-clauses/1.0`，19 条）。**复制到 `project-state/契约/ux/clauses.json` 后逐条填 `params`** |
| `static_check.py` | **L1 静态**：不启动应用，扫 CSS/HTML/JS 结构、文案、键位表、栅格声明 |
| `cdp_client.py` | 零依赖 CDP 客户端（HTTP 发现目标 + 手写 WebSocket），也是独立探针：`--eval` / `--shot` / `--console` |
| `runtime_assert.py` | **L2 运行时相对断言** + **L3 像素基线**（`--baseline`） |
| `README.md` | 本文件 |

四层里的 **L4 隔离评审**不在这里：那是 UI设计师 `R-ui-23` 的 **Pass 2'（交付前第二遍走查）**，
结论落账本；产品经理只做放行裁决（`R-pmgr-83`）。工具不评审美。

## 2. 快速开始

```powershell
$FW = "<框架根>"                       # 本项目里 = 大型项目开发框架 目录
$P  = "<项目根>"                       # 含 project-state/ 的项目目录

# ① 落地条款：模板复制过去再填 params（选择器 / 尺寸 / 文案按项目改）
New-Item -ItemType Directory -Force "$P\project-state\契约\ux" | Out-Null
Copy-Item "$FW\scripts\ux_gate\clauses.json" "$P\project-state\契约\ux\clauses.json"

# ② L1 静态（毫秒级，必须 <1s；不需要起应用）
python -X utf8 "$FW\scripts\ux_gate\static_check.py" $P --json-out "$P\project-state\证据\ux\L1.json"

# ③ 起应用（带调试端口，归项目启动脚本）之后跑 L2
python -X utf8 "$FW\scripts\ux_gate\runtime_assert.py" $P --port 9333 `
    --depth full --json-out "$P\project-state\证据\ux\L2.json"

# ④ L3 像素：先采基线（连跑 10 次取峰值），再比对
python -X utf8 "$FW\scripts\ux_gate\runtime_assert.py" $P --port 9333 `
    --baseline "$P\project-state\证据\ux\基线" --baseline-capture
python -X utf8 "$FW\scripts\ux_gate\runtime_assert.py" $P --port 9333 `
    --baseline "$P\project-state\证据\ux\基线" --json-out "$P\project-state\证据\ux\L3.json"
```

冒烟档只跑「窗口 / 导航 / 状态」三组：

```powershell
python -X utf8 "$FW\scripts\ux_gate\runtime_assert.py" $P --port 9333 --depth smoke
```

零依赖探针（排查"CDP 连不上 / 页面白屏"时先用它）：

```powershell
python -X utf8 "$FW\scripts\ux_gate\cdp_client.py" --port 9333 --eval "location.hash"
python -X utf8 "$FW\scripts\ux_gate\cdp_client.py" --port 9333 --shot "D:\tmp\shot.png" --console 2
```

## 3. 四层与档位（与 EXPERIENCE.md 映射表同口径）

| 层 | 跑什么 | 结论措辞 |
|---|---|---|
| L1 | `static_check.py`：可判的结构性/文案条款 | "结构性体验未见违例" |
| L2 | `runtime_assert.py`：CDP 相对断言（视口内/命中自己/可点/持久化/几何/iframe/键位/浮层焦点/状态渲染） | "结构性体验未见违例" |
| L3 | `runtime_assert.py --baseline`：固定取样条件后像素比对（**不做审美评分**） | "改动页像素无漂移" |
| L4 | UI设计师 Pass 2'（人判） | 逐条 `strong / adequate / thin / broken` + 依据 |

档位（跟既有 `test_depth` 对齐，**不新造概念**）：

| 档位 | UX 门下限 | 允许措辞 |
|---|---|---|
| `smoke` | L1 全跑 + L2 只跑窗口/导航/状态三组；L3 不出基线 | **只能写"冒烟未见异常"** |
| `targeted` | L1 全跑 + L2 受影响模块 + L3 只比改动页 | "受影响面未见违例"（点名范围） |
| `full` | L1 + L2 全 + L3 全量基线 + L4 | "按档通过"，列出 skipped 与原因 |
| 交付包 / 上线前 | **必须 `full`**；体验包可 smoke/targeted，但必须写"哪些面未独立验证" | 沿用交付口径 |

## 4. 阈值纪律（L3，写死在这层，不许各项目自己放松）

1. **先固定取样条件**：尺寸 / DPR / 主题 / 字体 / 关动画。基线取样脚本不替你做这些——`params.size` + `params.dpr` 至少填对，主题与动画归项目"证据准备"步骤。
2. **基线取样连跑 10 次**（`--capture-times 10`，默认就是 10），取相邻两次差异的**峰值**写进 `<基线>\peak.json`；判定阈值 = **峰值 ×2**，且不低于 50 px。
3. **抗锯齿**：单通道差 ≤ `0.1 × 255` 视为噪声（pixelmatch `threshold=0.1` 的语义），不计入 diff 像素。
4. **掩蔽判据**：`diff > 500 px` 或 `diff_ratio > 0.05` → 直接判**掩蔽**（FAIL），意思是"取样条件没固定住"，
   不是"页面变了"。此时**先查尺寸/DPR/主题/动画/字体加载**，不许调阈值绕过。可用 `--max-diff-pixels` / `--max-diff-ratio` 调，但调了必须在账本 `note` 写明理由。
5. 基线图放 `证据/ux/基线/base/<条款id>.png`，本轮截图落 `证据/ux/`（`--shot-dir` 可改），两者都要能指到（账本 `evidence` 列）。

## 5. 条款文件（`clauses.json`）

每条条款字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | `UX-<节缩码>-NN`，**必须能在 `EXPERIENCE.md` 找到**（对不上 = 契约缺口 → 提 CR 回填） |
| `title` | ✅ | 一句话 |
| `clause` | ✅ | 条款原文（该怎样） |
| `judgment` | ✅ | 判据（怎么算兑现） |
| `layer` | ✅ | `L1` / `L2` / `L3` / `L4` / `L1+L2` / `L2+L3` / `L2+L4` … |
| `check_kind` | ✅ | `static`（L1）/ `runtime`（L2）/ `pixel`（L3）/ `review`（L4，机器不跑） |
| `check_rule` | 机器跑时必填 | 必须命中 `static_check.py` / `runtime_assert.py` 里已注册的规则名；未实现 → skipped |
| `params` | 机器跑时必填 | 规则参数（选择器/尺寸/表达式）；**留空 → skipped，不当作通过** |
| `evidence` | ✅ | 证据形态（脚本 JSON / 截图 / 基线 diff） |
| `contract` | 建议 | 回指 `EXPERIENCE.md` 的节名 |
| `level` | 建议 | 该条在哪个档位生效（`smoke`/`targeted`/`full`） |
| `groups` | 建议 | `window` / `nav` / `status`（smoke 档只跑这三组） |

看有哪些规则：`python -X utf8 static_check.py --list-rules`、`runtime_assert.py --list-rules`。

### 已注册规则

**L1 静态（`static_check.py`）**：`unique_declaration`（某属性只在一个文件声明）、`explicit_value`（选择器必须显式声明某属性=某值，防继承）、`no_pattern`（禁用写法，命中即违例）、`token_present`（宿主标记/钩子必须都在）、`keybinding_conflict`（同组合键不得双绑）、`guard_whitelist`（危险写法只允许在白名单函数内）、`aria_label`（纯图标按钮必须有无障碍名）、`forbid_words`（UI 文案禁用词）、`clause_refs_resolve`（条款↔契约互指，**每次运行都会自动跑一条元检查**）、`clause_instances_present`（条款对象文件必须存在）。

**L2 运行时（`runtime_assert.py`）**：`in_viewport`、`hit_test`、`no_h_overflow`、`no_overlap`、`persist`、`probe_persist`、`tab_dedupe`、`iframe_fit`、`key_probe`、`empty_state`、`text_present`、`no_console_error`、`axe_scan`、`state_renders`、`input_kept`、`overlay_focus`、`submit_once`、`confirm_gate`、`undo_window`、`journey`、`recover_action`。

**L3 像素**：`check_kind = "pixel"` 的条款自动走基线比对（`check_rule` 写 `pixel_baseline` 即可）。

## 6. 与 lint 的关系

`scripts/framework_lint.py` 的 **U 组**（v5.17.0）查的是**账本自洽**，不是重跑断言：

- `machine:ux` 块必须存在、列头逐字、列数对、`id` 形如 `UXR-NNN` 且升序、`at` 时间格式；
- `verdict ∈ {pass, fail, skipped}`；`skipped` 必须在 `note` 写原因（留空 = ERROR）；
- `clauses` 里的每个 id 必须能在 `EXPERIENCE.md` 找到（否则 = 契约缺口，ERROR）；
- `evidence` 指向的项目根相对路径必须存在；
- **只有 `full` 档的行才允许 `pass`**（smoke/targeted 出 pass = 越级结论，ERROR）。

所以正确姿势：**先跑工具出 JSON → 再写账本 → 再跑 lint**。工具通过 ≠ 账本写完 ≠ 门禁关闭。

## 7. 常见坑（都实测过）

1. **CDP 端口不是本工具开的**。`--port` 必须与项目启动脚本里的 `--remote-debugging-port` 一致；连不上先用 `cdp_client.py --eval "location.hash"` 确认目标页存在。
2. **长连接会卡**：页面 reload / 真实点击后，旧的 WebSocket 会话可能永久等不到回包（实测）。所以 `runtime_assert.py` 的 `One` **每条命令开一条短连接**——不要为了省事改成复用长连接。
3. **JS 的 `.click()` 不搬焦点**：浮层类条款（`overlay_focus`）必须用 CDP `Input.dispatchMouseEvent` 真点（`page.click()` 已封装），否则"打开即聚焦"永远判不出来。
4. **`desktop` 域不要硬套 axe**：Windows 用 UIA / macOS 用 AX；`axe_scan` 无 axe 源时只 skipped，别把它当"可访问性已过"。
5. **像素基线最容易假阳性**：主题切换、字体加载（FOUT）、动画没关、DPR 变了都会让 diff 爆掉——先按 §4 的掩蔽判据查取样条件。
6. **规则未实现 / 参数没填 = skipped**：`skipped` 必须进账本并写原因；把 skipped 当通过 = 门禁造假。
7. **条款 id 只增不改**：删除条款要保留 id 并标「（vX.Y.Z 废止）」，否则历史账本会变成"引用了不存在的条款"。

## 8. 边界

- **不做**：审美评分、视觉设计判断、性能压测、功能正确性验收（这些各有归属，别混进 UX 门）。
- **不做**：启动应用、装依赖、起服务器、改项目代码。工具只读（L3 会写字面截图与基线）。
- **不做**：替产品经理拍板放行。取舍永远是 `R-pmgr-83`。
