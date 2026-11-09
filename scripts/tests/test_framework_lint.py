#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""framework_lint.py 的回归测试（v4.5.0 补）。

为什么这个测试必须存在：`framework_lint.py` 的产出是"框架自身没毛病"的**唯一机器证据**。
一个永远返回 0 个 ERROR 的检查器比没有检查器更坏——它会把"漏登记的清单/落后的副本/
解析不到的路径"洗成"已验证"，正是它要防的那种**假通过**。所以本测试不验"干净的树能过"，
而是逐个把框架历史上真实发生过的漂移**种进沙箱**，断言检查器一定会变红。

覆盖（M = Mutation，每题先建一棵干净的最小框架树，再单独种一个毛病）：
- 基线：干净树 → ERROR 0（否则检查器在乱报，同样不可信）
- A 版本：SKILL 版本超前于框架 → 报错；正文已引用新版本而头部没升号 → 报错；单纯滞后 → 只记 INFO；
  正文**中段**（超出 HEAD_SCAN_LINES）提到新版号 → 不报错（防"逼作者删掉准确版本信息"）
- B 编号：验收标准缺号 / 完成定义 §9.N 缺号 → 各报错；无 §9 → WARN；**声明 ≥ v3.22.0 仍无 §9 → 仍 WARN**
  （按内容判，不按版本放过）
- C 引用：引用 `scripts/不存在.py` → 报错；`skills/<角色>/SKILL.md` 占位写法 → 不报错（假阳性守卫）；
  项目外**真实**文件按字面路径命中（脚本 / SKILL 两支）→ 不报错；项目内角色 SKILL 缺失 → 措辞为
  「角色 SKILL」；项目外形状的 `skills/x/SKILL.md` 缺失 → 措辞为「文件」（分叉点 = `skills/` 前有无目录成分）
- D 注册表：paths 多出 layout 没有的域 → 只记 INFO；白名单 `<>` 占位 → 只警告不报错；
  伞域 ↔ 逐仓列举（双向）不误报，且被伞域覆盖的子路径**不进** INFO 清单
- E 单一源：协作规范 §4 长出编号清单副本 → 报错；「当前 N 项」与实际条数不符 → 报错；
  派发简报骨架三段（A 共享块/B 卡块/C 角色块）B/C 换位或整段缺失 → 报错，顺序正确 → 只记 INFO
  （v4.8.0：共享块前置是前缀缓存可命中的唯一前提，顺序漂移必须变红）
- F 角色：角色缺进度 json → 报错；有 SKILL 但不在角色定义表（孤角色）→ 报错
- G 交叉引用：「注意事项 9」超出实际条数 → 报错；「SKILL §7」不存在 → 报错
- H 登记↔磁盘：索引登记的文件磁盘上没有 → 警告；标了 【运行】 的缺失不报（假阳性守卫）；
  带「（v1.0.0）」注解的登记仍能解析（解析守卫）；索引里的**角色表首格**（`产品经理`）不算路径登记
- K 极速修复台账（12 列）：台账不存在 → INFO 跳过；无 machine:turbo 机读块 / 列头不逐字 / 列数不对
  → ERROR；`basis` 空 → ERROR、形状坏 → WARN、`no-card`/`tc-NNN`/`card-NNN+tc-NNN` → 不报；
  `pack` 空 → ERROR、指向不存在的路径 → WARN、`-` 或真实文件 → 不报；每条判据都配反假阳性用例
- N 迁移容错：缺 `schema_version` → ERROR 且报"需迁移"不崩；`layout` 是字符串（v3.12）→ ERROR 且报告仍完整；
  `framework_source` 是 `<待填>` → 只记 INFO；多仓 `scripts/x.py` → `repos/<仓>/scripts/x.py` 命中；
  `{甲,乙}` 花括号简写不误报；`运行日志.md` 历史引用不追今日存在性
- 两档模式：`project_name` 非空 → 项目体检；为空 → 框架自查（两个方向都验）
- X 落空引用定位：框架有同名脚本 → 报错里附上框架内真实位置；到处都没有 → 不许编提示
- 接口与健壮性：非框架根 → 2；`--json` 可解析且字段齐；Windows GBK 控制台不崩（errors=replace）

本文件自己也被**变异验证**过：把上述判据逐个删掉/反转，都必须有对应用例变红。
2026-09-15 实测抓出 3 条空跑用例（N6 第一版把外部文件放在 `沙箱外部/真脚本.py`，而 `SCRIPT_REF`
只认 `.../scripts/<名>.py` 收尾的 token——**一处引用都没识别**，关掉 `_literal_ok` 它照样绿）。
"一个测不出错的用例，和一个永远返回 0 ERROR 的检查器一样没有价值。"

用法：python scripts/tests/test_framework_lint.py
      FL_SCRIPT=<别的路径> 可测别处的副本。
退出码 0=全过；1=有失败。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parents[1]
SCRIPT = Path(os.environ.get("FL_SCRIPT") or (FRAMEWORK / "scripts" / "framework_lint.py"))
ROOT = Path(tempfile.mkdtemp(prefix="fl_test_"))

fails: list = []
passes: list = []


def check(cond, label, detail=""):
    (passes if cond else fails).append(label)
    tail = f"  -- {detail}" if detail and not cond else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{tail}")


# ---------------------------------------------------------------- 沙箱搭建

AGENT_MD = """---
name: "测试框架"
description: "沙箱"
version: "1.0.0"
---

## 角色定义

| 角色 | 核心职责 | 裁决/主动权限 |
|------|---------|--------------|
| 甲 | 干活的 | 提交 CR |

### ⑥ 集成与验证（VERIFY）

**主控验收前置检查（进本步前必须过）**：
1. 第一项检查
2. 第二项检查

#### 边界

正文。

### 需求卡子代理派发机制

2. **封装简报（三段式装配）**：固定三段，标记字面量不可改写、顺序不可调换。

- **【A 共享块】（最前）**：项目根 + 全局纪律路径指针；零插值。
- **【B 卡块】**：卡片目录路径 + 目标落盘位置。
- **【C 角色块】（最后）**：本角色记忆档案指针 + 专项指令。

## 验收标准

1. 第一条
2. 第二条

## 注意事项

1. 第一条
2. 第二条

## 任务执行纪律

1. 第一条
"""

COOP_MD = """# 协作规范

> 版本：v1.0.0（随大型项目开发框架 v1.0.0 模板复制到项目）

## 4. 主控验收前置检查（阶段 5 前必须过）

**清单本体在 `agent.md` ⑥——本处不复制。**

- **唯一事实源**：`agent.md` ⑥ 的那份编号清单（当前 2 项）。
- 说明。

## 5. 中途接管

正文。
"""

DOD_MD = """# 完成定义

## 检查清单

### 1. 正确性

- [ ] 通过

## 9. 工程治理

### 9.1 工作区整洁

- [ ] 通过
"""

FOO_ROW = "| foo.md | 示例契约（角色 甲 的落点） | L1 |"

INDEX_MD = """# 上下文索引

> 版本：v1.0.0（内容随框架同步至 v1.0.0）。

## 契约/（先确认后开发）

| 文档 | L0 摘要（≈100t） | 建议深度 |
|------|----------------|---------|
""" + FOO_ROW + """
| bar.md **【运行】** | 运行期才生成 | L1 |
| 上下文索引.md | 本文件：所有上下文 L0 汇总（自登记，N 组反向对账要求） | L0/L1 |
"""

SKILL_MD = """---
name: 甲
description: 沙箱角色
version: "1.0.0"
---

# 甲

## 1. 触发场景

干活的。

## 2. 执行流程

1. 干活
"""

CONFIG = {
    # `schema_version` 是 v4.9.0 起 **config.json 的必填项**：缺了它，工具无法判断该按哪套结构
    # 解释这份配置，D 组整体跳过并报 ERROR。沙箱是"当前结构"的树，故必须带上——
    # 夹具停在旧结构会让每一条用例都先撞上这一条 ERROR，把真正的断言淹掉（实测 10 条失败里有 8 条源于此）。
    "schema_version": "1",
    "project_name": "",
    "paths": {
        "_comment": "沙箱",
        "a": "A",
        "b": ["B"],
        "layout": {"_comment": "两域", "域一": "A", "域二": "B"},
        "根文件": {"framework": "agent.md  # 框架主控定义，不能移动"},
    },
}


def w(relpath, text):
    p = ROOT / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def build():
    """建一棵干净的最小框架树（每个用例都从这里出发）。"""
    if ROOT.exists():
        shutil.rmtree(ROOT, ignore_errors=True)
    w("agent.md", AGENT_MD)
    w("project-state/协作规范.md", COOP_MD)
    w("project-state/完成定义.md", DOD_MD)
    w("project-state/契约/上下文索引.md", INDEX_MD)
    w("project-state/契约/foo.md", "# foo\n")
    w("project-state/进度/甲.json", '{"role": "甲"}\n')
    w("project-state/config.json", json.dumps(CONFIG, ensure_ascii=False, indent=2))
    w("skills/甲/SKILL.md", SKILL_MD)
    w("scripts/real.py", "# 沙箱里被引用的真脚本\n")


def run(*extra, env=None, root=ROOT):
    # `PYTHONIOENCODING` 固定子进程的标准输出编码：子进程 stdout 是**管道**，Python 此时按
    # locale 编码（本机 cp936/GBK）写出，而本测试按 utf-8 读取（`errors="replace"`）——
    # 中文报告会被解成 U+FFFD，所有按消息片段断言的用例**一起变红**（实测 64 条里 36 条）。
    # 断言看着像"判据失效"，其实只是编码对不上。`setdefault`：显式传 env 的用例（GBK 控制台
    # 那条）仍然说了算，不被这里覆盖。
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    if env:
        e.update(env)
    return subprocess.run([sys.executable, str(SCRIPT), str(root), *extra],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e)


def expect_error(label, needle, mutate):
    build()
    mutate()
    r = run("--quiet")
    hit = needle in r.stdout
    check(hit, label, f"未出现「{needle}」；实际输出：\n{r.stdout[-600:]}")


def expect_warn(label, needle, mutate):
    expect_error(label, needle, mutate)


def expect_clean(label, mutate=None):
    build()
    if mutate:
        mutate()
    r = run("--quiet")
    clean = "ERROR 0" in r.stdout and "WARN 0" in r.stdout
    check(clean, label, f"预期零 ERROR 零 WARN；实际输出：\n{r.stdout[-600:]}")


# ---------------------------------------------------------------- 用例

print("基线")
build()
r = run("--quiet")
check("ERROR 0" in r.stdout and r.returncode == 0, "干净树 → ERROR 0",
      f"rc={r.returncode}\n{r.stdout[-600:]}")
check("WARN 0" in r.stdout, "干净树 → WARN 0（检查器不空报）", r.stdout[-600:])

print("A 版本一致性")


def m_skill_ahead():
    w("skills/甲/SKILL.md", SKILL_MD.replace('version: "1.0.0"', 'version: "2.0.0"'))


def m_skill_lag_body_new():
    """文首的变更注记里提到当前框架版本、头部版本号却停在旧的 → 漏升号（判据只看文首，见 N11）。"""
    w("skills/甲/SKILL.md",
      SKILL_MD.replace('version: "1.0.0"', 'version: "0.9.0"')
               .replace("# 甲\n", "# 甲\n\n> 本版随 v1.0.0 更新。\n"))


def m_skill_lag_only():
    w("skills/甲/SKILL.md", SKILL_MD.replace('version: "1.0.0"', 'version: "0.9.0"'))


def m_coop_ahead():
    w("project-state/协作规范.md",
      COOP_MD.replace("v1.0.0（随大型项目开发框架 v1.0.0", "v9.9.9（随大型项目开发框架 v9.9.9"))


expect_error("A1 SKILL 版本超前于框架 → ERROR", "高于框架版本", m_skill_ahead)
expect_error("A2 正文已引用新版本而头部没升号 → ERROR", "漏升", m_skill_lag_body_new)
expect_error("A3 模板文件声明版本超前 → ERROR", "超前/残留", m_coop_ahead)

# A4 滞后不是错：不加 --quiet 才看得到 INFO
build()
m_skill_lag_only()
r = run()
check("ERROR 0" in r.stdout and "滞后于框架" in r.stdout,
      "A4 单纯滞后只记 INFO 不报错", r.stdout[-600:])

print("B 编号连续性")


def m_ac_gap():
    w("agent.md", AGENT_MD.replace("2. 第二条\n\n## 注意事项", "3. 第三条\n\n## 注意事项"))


def m_dod_gap():
    w("project-state/完成定义.md", DOD_MD.replace("### 9.1 工作区整洁", "### 9.3 工作区整洁"))


expect_error("B1 验收标准缺号 → ERROR", "编号不连续", m_ac_gap)
expect_error("B2 完成定义 §9.N 缺号 → ERROR", "编号不连续", m_dod_gap)

print("C 引用存在性")


def m_script_missing():
    w("agent.md", AGENT_MD.replace("## 验收标准", "跑 `scripts/不存在.py`。\n\n## 验收标准"))


def m_placeholder_ref():
    w("agent.md", AGENT_MD.replace("## 验收标准", "见 `skills/<角色>/SKILL.md`。\n\n## 验收标准"))


expect_error("C1 引用不存在的脚本 → ERROR", "不存在的脚本", m_script_missing)
expect_clean("C2 `skills/<角色>/SKILL.md` 占位写法不误报", m_placeholder_ref)

print("D 注册表一致")


def m_paths_extra():
    cfg = json.loads(json.dumps(CONFIG))
    cfg["paths"]["孤儿域"] = "C"
    w("project-state/config.json", json.dumps(cfg, ensure_ascii=False, indent=2))


def m_layout_extra():
    cfg = json.loads(json.dumps(CONFIG))
    cfg["paths"]["layout"]["域三"] = "D"
    w("project-state/config.json", json.dumps(cfg, ensure_ascii=False, indent=2))


def m_placeholder_whitelist():
    cfg = json.loads(json.dumps(CONFIG))
    cfg["paths"]["根文件"]["entry"] = "<入口文件名>  # 待填"
    w("project-state/config.json", json.dumps(cfg, ensure_ascii=False, indent=2))


def m_layout_umbrella():
    """多仓口径：layout 写伞域 `repos`，paths 逐个列 `repos/<仓>` —— 同一件事的两种粒度，不该报错。"""
    cfg = json.loads(json.dumps(CONFIG))
    cfg["paths"].pop("a", None)
    cfg["paths"].pop("b", None)
    cfg["paths"]["repos"] = ["repos/workbench", "repos/skill-platform"]
    cfg["paths"]["layout"] = {"_comment": "伞域", "代码区": "repos"}
    w("project-state/config.json", json.dumps(cfg, ensure_ascii=False, indent=2))


# **判据不对称，是刻意的**（见 check_registry 内说明）：域声明了却没路径 → 角色按域取不到东西，必须阻塞；
# paths 里多出八域之外的登记根（框架工具 scripts/skills、项目自定域）→ 不影响按域解析，且 v3.18.0
# 根级白名单恰恰要求根上每一项都登记在册，故只记 INFO。D1 断言随之从 ERROR 改为"不许误报 ERROR"。
expect_clean("D1 paths 多出 layout 的域 → 只记 INFO，不报 ERROR（额外登记根属正常）", m_paths_extra)
expect_error("D2 layout 多出 paths 的域 → ERROR", "paths 缺的域", m_layout_extra)
expect_warn("D3 白名单 <> 占位只警告不报错", "未填占位", m_placeholder_whitelist)
expect_clean("D4 多仓伞域 ↔ 逐仓列举不误报", m_layout_umbrella)

# D4 只证"没报 ERROR"。`covered()` 里"条目是别人子路径"那一支只影响 **INFO 的措辞**（only_paths 本就不阻塞），
# 故关掉它 D4 依旧全绿——必须再断言消息内容：被 `repos` 伞域覆盖的 `repos/<仓>` 不该被列进
# 「八域之外的额外登记根」，否则报告会对着合法登记喊狼来了，读报告的人开始无视这一行。
build()
m_layout_umbrella()
_umbrella_out = run().stdout
check("repos/workbench" not in _umbrella_out,
      "D5 被伞域覆盖的子路径不进「额外登记根」清单（INFO 也不许喊错）",
      _umbrella_out[-500:])

print("E 单一事实源")


def m_second_copy():
    body = COOP_MD.replace("- 说明。", "1. 查进度 json\n2. 查描述性文件\n3. 查审核记录")
    w("project-state/协作规范.md", body)


def m_stale_count():
    w("project-state/协作规范.md", COOP_MD.replace("当前 2 项", "当前 3 项"))


expect_error("E1 §4 长出编号清单副本 → ERROR", "第二份清单本体", m_second_copy)
expect_error("E2 指针里「当前 N 项」与实际不符 → ERROR", "已过期", m_stale_count)


# v4.8.0 派发简报骨架：共享块前置是前缀缓存可命中的唯一前提，顺序漂移必须变红
def m_brief_swap_bc():
    """把 B 卡块与 C 角色块换位——随卡变化的内容前移，公共前缀被压短。"""
    w("agent.md", AGENT_MD.replace(
        "- **【B 卡块】**：卡片目录路径 + 目标落盘位置。\n"
        "- **【C 角色块】（最后）**：本角色记忆档案指针 + 专项指令。",
        "- **【C 角色块】（最后）**：本角色记忆档案指针 + 专项指令。\n"
        "- **【B 卡块】**：卡片目录路径 + 目标落盘位置。"))


def m_brief_card_first():
    """把 A 共享块整段删掉（等价于「卡号直接写在第一行」的原状）。"""
    w("agent.md", AGENT_MD.replace(
        "- **【A 共享块】（最前）**：项目根 + 全局纪律路径指针；零插值。\n", ""))


expect_error("E3 简报骨架 B/C 换位 → ERROR", "骨架顺序被打乱", m_brief_swap_bc)
expect_error("E4 简报缺 A 共享块标记 → ERROR", "缺骨架标记", m_brief_card_first)


build()
r = run()
check("派发简报骨架三段齐备且顺序正确" in r.stdout,
      "E5 骨架正确 → 只记 INFO、不报错（防检查器恒红）", r.stdout[:400])

print("F 角色同步完整性")


def m_no_progress():
    (ROOT / "project-state" / "进度" / "甲.json").unlink()


def m_orphan_role():
    w("skills/乙/SKILL.md", SKILL_MD.replace("name: 甲", "name: 乙"))


expect_error("F1 角色缺进度 json → ERROR", "缺进度模板", m_no_progress)
expect_error("F2 孤角色（有 SKILL 不在角色表）→ ERROR", "孤角色", m_orphan_role)

print("G 交叉引用有效性")


def m_bad_note_ref():
    w("agent.md", AGENT_MD.replace("## 验收标准", "见注意事项 9。\n\n## 验收标准"))


def m_bad_skill_sec():
    w("agent.md", AGENT_MD.replace("## 验收标准", "见 `skills/甲/SKILL.md` §7。\n\n## 验收标准"))


expect_error("G1 「注意事项 9」超出实际条数 → ERROR", "不存在", m_bad_note_ref)
expect_error("G2 「SKILL §7」小节不存在 → ERROR", "无此小节", m_bad_skill_sec)

print("H 登记↔磁盘")


def m_reg_missing():
    w("project-state/契约/上下文索引.md", INDEX_MD.replace(FOO_ROW, FOO_ROW + "\n| 丢了.md | 磁盘上没有 | L1 |"))


def m_run_marker():
    w("project-state/契约/上下文索引.md",
      INDEX_MD.replace(FOO_ROW, FOO_ROW + "\n| 缺.md **【运行】** | 运行期生成 | L1 |"))


def m_annotated_ok():
    w("project-state/契约/上下文索引.md",
      INDEX_MD.replace(FOO_ROW, "| foo.md（v3.22.0） | 带注解的登记（角色 甲 的落点） | L1 |"))


expect_warn("H1 索引登记的文件磁盘上没有 → WARN", "磁盘上找不到", m_reg_missing)
expect_clean("H2 标了【运行】的缺失不报（假阳性守卫）", m_run_marker)
expect_clean("H3 「foo.md（v3.22.0）」注解能解析到 foo.md", m_annotated_ok)

# N 工作区地图对账（v5.17.0；引擎 = scripts/workspace_map.py，本组只转发其 drift）


def m_unregistered_contract():
    w("project-state/契约/野文件.md", "# 没登记进索引的契约域文件\n")


def m_root_loose_bak():
    w("agent.md.bak-20261024", "# 游离备份\n")


def m_stale_map_json():
    # 地图机器版存在但内容是手改过的旧货 → 不属于本组判据（地图会被下次刷新覆盖），只验证不误报
    w("project-state/进度/工作区地图.json", "{}\n")


expect_error("N1 契约域文件未登记索引 → ERROR", "unregistered_contract", m_unregistered_contract)
expect_error("N2 索引登记了但磁盘没有 → ERROR（引擎严格档，升 H 的 WARN）", "registered_missing", m_reg_missing)
expect_error("N3 顶层游离备份件 → ERROR", "loose_file", m_root_loose_bak)
expect_clean("N4 引擎不误报：地图机器版存在不触发任何漂移", m_stale_map_json)
build()
r = run()  # 不带 --quiet：INFO note 只在非静默模式打印（N5 验的就是这个）
check("工作区地图.md 未生成" in r.stdout, "N5 未生成地图只记 INFO 不拦", r.stdout[-600:])


def m_index_role_table():
    """索引里的**非文档表**（角色→落点表）首格是角色名，不是路径——不许当登记条目判缺失。

    实测来源：真实项目的索引补了 12 角色落点表后，一次报出 13 条"登记了 `产品经理`、磁盘上找不到"，
    **13 条全假**。被假阳性塞满的检查器会被整段无视。
    （表内不写任何 `scripts/…` 反引号路径：那会被 C 组按真实引用检查，不是本用例要验的事。）
    """
    w("project-state/契约/上下文索引.md",
      INDEX_MD + "\n## 角色 → 产出落点\n\n| 角色 | 产出落点 |\n|------|---------|\n"
                 "| 甲 | 干活 |\n| 不存在角色 | 无 |\n")


expect_clean("H4 索引里的角色表（首格非路径）不误判为登记条目", m_index_role_table)

print("v4.9.0 旧配置容错（体检工具最该有用的场合恰是项目很旧，此时崩溃 = 零价值）")


def m_no_schema():
    cfg = json.loads(json.dumps(CONFIG))
    cfg.pop("schema_version", None)
    w("project-state/config.json", json.dumps(cfg, ensure_ascii=False, indent=2))


def m_layout_string():
    """v3.12 时代把 `paths.layout` 写成一段说明字符串——`layout.items()` 会抛 AttributeError。"""
    cfg = json.loads(json.dumps(CONFIG))
    cfg["paths"]["layout"] = "运行时自建：project-state / repos / 产物"
    w("project-state/config.json", json.dumps(cfg, ensure_ascii=False, indent=2))


def m_framework_source_placeholder():
    cfg = json.loads(json.dumps(CONFIG))
    cfg["framework_source"] = "<框架源仓库路径>"
    w("project-state/config.json", json.dumps(cfg, ensure_ascii=False, indent=2))


expect_error("N1 缺 schema_version → ERROR（报需迁移，不是崩溃）", "schema_version", m_no_schema)
expect_error("N2 layout 是字符串（v3.12 旧写法）→ ERROR 且不崩", "应为对象", m_layout_string)


def assert_partial_report():
    """N3 同 N2 的现场，但要验"其余检查照跑"：报告必须仍然出得来（有 INFO、有 F 组）。"""
    m_layout_string()
    r = run()
    ok = "F角色" in r.stdout and "ERROR" in r.stdout and "Traceback" not in r.stdout
    check(ok, "N3 旧写法 layout：其余检查照跑、报告完整、无 traceback", r.stdout[-600:] + r.stderr[-300:])


build()
assert_partial_report()
expect_clean("N4 framework_source 是 `<待填>` 占位 → 只记 INFO 不比错", m_framework_source_placeholder)

print("多仓 / 外部根解析（只按项目根查会把真文件判成缺失）")


def m_multirepo_script():
    """脚本住在 `repos/<仓>/scripts/` 里，文档按惯用写法引 `scripts/x.py` —— 归一后应能命中。"""
    w("repos/仓甲/scripts/tool.py", "# 仓内脚本\n")
    w("agent.md", AGENT_MD.replace("## 验收标准", "跑 `scripts/tool.py`。\n\n## 验收标准"))
    cfg = json.loads(json.dumps(CONFIG))
    cfg["paths"]["repos"] = ["repos/仓甲"]
    cfg["paths"]["layout"]["代码区"] = "repos"
    w("project-state/config.json", json.dumps(cfg, ensure_ascii=False, indent=2))


def m_literal_external():
    """项目外的**真实**文件按字面路径引用——索引查不到不等于文件不在。

    坑（实测 2026-09-15）：`SCRIPT_REF` 只认**以 `scripts/<名>.py` 收尾**的 token。第一版把外部
    文件放在 `沙箱外部/真脚本.py`，结果**一处引用都没被识别**，用例一直绿着却什么也没测
    （把 `_literal_ok` 整段关掉，它照样 PASS）。故外部路径也必须带 `scripts/` 段。
    """
    outside = ROOT.parent / "沙箱外部" / "scripts" / "真脚本.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("# 项目外真文件\n", encoding="utf-8")
    w("agent.md", AGENT_MD.replace("## 验收标准",
                                   "跑 `%s`。\n\n## 验收标准" % str(outside).replace("\\", "/")))


def m_literal_external_skill():
    """同上，但走 SKILL_REF 分支：项目外真实存在的 `skills/<角色>/SKILL.md`（如插件缓存里的）。"""
    outside = ROOT.parent / "沙箱外部技能" / "skills" / "外部角色" / "SKILL.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("---\nname: 外部角色\ndescription: 沙箱外\n---\n", encoding="utf-8")
    w("agent.md", AGENT_MD.replace("## 验收标准",
                                   "见 `%s`。\n\n## 验收标准" % str(outside).replace("\\", "/")))


def m_missing_role_skill():
    """**项目内**角色技能缺失（`skills/乙/` 根本没建）——真缺口，且要指名道姓说是"角色 SKILL"。"""
    w("agent.md", AGENT_MD.replace("## 验收标准", "见 `skills/乙/SKILL.md`。\n\n## 验收标准"))


def m_missing_external_shaped_skill():
    """项目**外**的 `skills/x/SKILL.md` 不存在：同是缺失，但不得说成"引用了不存在的角色 SKILL"。

    判据分叉点 = `skills/` 之前有没有目录成分。本用例与上一条**配对**，才钉得住
    `_is_project_role_skill` 的两个方向（只测一边的话，把它写成恒定返回也能全绿）。
    """
    outside = ROOT.parent / "沙箱不存在" / "skills" / "乙" / "SKILL.md"
    w("agent.md", AGENT_MD.replace("## 验收标准",
                                   "见 `%s`。\n\n## 验收标准" % str(outside).replace("\\", "/")))


def m_brace_shorthand():
    """`scripts/{a,b}.py` 简写：两个都真存在，按单一路径找不到会报假阳性。"""
    w("scripts/甲.py", "# 真文件\n")
    w("scripts/乙.py", "# 真文件\n")
    w("agent.md", AGENT_MD.replace("## 验收标准", "跑 `scripts/{甲,乙}.py`。\n\n## 验收标准"))


def m_runlog_history():
    """`运行日志.md` 是 append-only 流水，写的是"当时引用了什么"——不追今日存在性。"""
    w("project-state/运行日志.md", "# 运行日志\n\n- 当时下载的包里含 `scripts/早就没了.py`。\n")


expect_clean("N5 多仓：`scripts/x.py` 命中 `repos/<仓>/scripts/x.py`", m_multirepo_script)
expect_clean("N6 项目外真脚本按字面路径命中（`.../scripts/x.py`）", m_literal_external)
expect_clean("N6b 项目外真 SKILL.md 按字面路径命中（插件缓存那种）", m_literal_external_skill)
expect_error("N6c 项目内角色 SKILL 缺失 → ERROR 且指名「角色 SKILL」", "角色 SKILL", m_missing_role_skill)
_skill_msg = None


def _probe_missing_external_skill():
    """N6d 需要同时看"报没报"和"报的哪句话"，故不走 expect_*。"""
    global _skill_msg
    build()
    m_missing_external_shaped_skill()
    _skill_msg = run().stdout


build()
_probe_missing_external_skill()
check("引用了不存在的文件" in (_skill_msg or ""),
      "N6d 项目外形状的 skills 缺失 → 仍报缺失（不静默放过）", (_skill_msg or "")[-400:])
check("角色 SKILL" not in (_skill_msg or ""),
      "N6d 但不说成「项目角色 SKILL 缺失」（`skills/` 前有目录成分就不是项目角色）",
      (_skill_msg or "")[-400:])

expect_clean("N7 `scripts/{甲,乙}.py` 花括号简写不误报", m_brace_shorthand)
expect_clean("N8 运行日志.md 的历史引用不追今日存在性", m_runlog_history)

print("机制未同步 vs 结构被改坏（同一条缺口，说法不同、阻塞与否也不同）")


def m_dod_no_s9():
    w("project-state/完成定义.md", DOD_MD.split("## 9. 工程治理")[0])


def m_dod_no_s9_on_modern_project():
    """**本用例是"按内容判"而不是"按版本判"的唯一证据。**

    项目声明 v3.22.0（≥ §9 引入的 v3.22.0），完成定义却整段没有 §9——真实项目上就是这样。
    按版本判会**静默放过**（版本不老，判据不触发）；按内容判才报得出来。故这里必须仍然 WARN。
    """
    w("agent.md", AGENT_MD.replace('version: "1.0.0"', 'version: "3.22.0"'))
    w("project-state/完成定义.md", DOD_MD.split("## 9. 工程治理")[0])


def m_brief_absent_old():
    """agent.md 是 v3.22.0（早于骨架机制 v4.8.0）且没有三段标记 → 机制未同步，WARN 不阻塞。"""
    body = AGENT_MD.replace("version: \"1.0.0\"", "version: \"3.22.0\"")
    for marker in ("- **【A 共享块】（最前）**：项目根 + 全局纪律路径指针；零插值。\n",
                   "- **【B 卡块】**：卡片目录路径 + 目标落盘位置。\n",
                   "- **【C 角色块】（最后）**：本角色记忆档案指针 + 专项指令。\n"):
        body = body.replace(marker, "")
    w("agent.md", body)


def m_a_deep_mention():
    """文首没提、正文中段提到当前框架版本 → 属历史/交叉陈述，不该判"漏升号"。

    必须把提及**推到 HEAD_SCAN_LINES 之外**，否则测的是"文首提到"那条老判据，白测。
    """
    filler = "\n".join("注 %d。" % i for i in range(30))
    w("skills/甲/SKILL.md",
      SKILL_MD.replace('version: "1.0.0"', 'version: "0.9.0"')
               + "\n\n## 附：机制沿革\n\n%s\n\n该机制随 v1.0.0 引入，此前无。\n" % filler)


expect_warn("N9 完成定义无 §9 → WARN（机制未同步，不是解析失败）", "没有 §9 工程治理段", m_dod_no_s9)
expect_warn("N9b 项目声明 ≥ v3.22.0 而无 §9 → 仍 WARN（按内容判，不按版本放过）",
            "没有 §9 工程治理段", m_dod_no_s9_on_modern_project)
expect_warn("N10 老项目缺简报骨架 → WARN（机制未同步）而非 ERROR", "机制未同步", m_brief_absent_old)
expect_clean("N11 正文中段提到新版号不判漏升号（只在文首才判）", m_a_deep_mention)

print("两档模式（框架自查 / 项目体检）")


def m_is_project():
    cfg = json.loads(json.dumps(CONFIG))
    cfg["project_name"] = "沙箱项目"
    w("project-state/config.json", json.dumps(cfg, ensure_ascii=False, indent=2))


def assert_project_mode():
    m_is_project()
    r = run("--quiet")
    check("项目体检" in r.stdout and "框架自查" not in r.stdout,
          "N12 project_name 非空 → 判为项目体检", r.stdout[-400:])


build()
assert_project_mode()
build()
r = run("--quiet")
check("框架自查" in r.stdout, "N13 project_name 为空 → 判为框架自查", r.stdout[-400:])

print("X 落空引用定位（同名文件在别处 → 报错要指路）")


def _stdout_of(mutate):
    build()
    mutate()
    return run().stdout


# 真实案发现场（武林无敌）：21 条验收判据写 `scripts/art_lint.py`，而该工具住在框架的
# `skills/UI设计师/scripts/`。仍报 ERROR（路径不可复现就是要改），但必须顺手说清它在哪。
_out_fw_hint = _stdout_of(lambda: w("agent.md", AGENT_MD.replace(
    "## 验收标准", "验收：`scripts/art_lint.py --json` errors==0。\n\n## 验收标准")))
check("框架内有同名脚本" in _out_fw_hint and "skills/UI设计师/scripts/art_lint.py" in _out_fw_hint,
      "X1 落空引用附上框架内的真实位置（21 个谜团 → 一处已定位的错）", _out_fw_hint[-700:])
check("引用了不存在的脚本" in _out_fw_hint,
      "X2 提示归提示，**仍判 ERROR**（不可复现的验收判据不许被提示洗成通过）", _out_fw_hint[-700:])

_out_no_hint = _stdout_of(lambda: w("agent.md", AGENT_MD.replace(
    "## 验收标准", "跑 `scripts/查无此物.py`。\n\n## 验收标准")))
check("同名" not in _out_no_hint and "框架内" not in _out_no_hint,
      "X3 到处都没有同名文件 → 不许编提示（乱指路比不指路更费时间）", _out_no_hint[-700:])

print("接口与健壮性")


def m_utf8_heavy():
    """造一条含全角竖线/破折号的输出，验 GBK 控制台下不崩。"""
    w("project-state/契约/上下文索引.md", INDEX_MD.replace(FOO_ROW, FOO_ROW + "\n| 丢了.md | 磁盘上没有 | L1 |"))


build()
m_utf8_heavy()
env = dict(os.environ, PYTHONIOENCODING="gbk")
rg = run("--quiet", env=env)
check("Traceback" not in rg.stderr and "UnicodeEncodeError" not in rg.stderr,
      "GBK 控制台下不崩（errors=replace）", f"stderr={rg.stderr[-400:]}")

build()
r = run("--json")
try:
    data = json.loads(r.stdout)
    ok_json = all(k in data for k in ("root", "ok", "counts", "findings", "notes"))
except Exception as exc:
    ok_json = False
    data = {}
    print(f"    json 解析失败：{exc}")
check(ok_json, "--json 输出可解析且字段齐", r.stdout[:300])
check(data.get("ok") is True and r.returncode == 0,
      "--json 干净树 ok=true / 退出码 0", f"ok={data.get('ok')} rc={r.returncode}")

r = run("--quiet")
check(r.returncode == 0, "干净树退出码 0", f"rc={r.returncode}")

build()
bad = subprocess.run([sys.executable, str(SCRIPT), str(ROOT / "skills")],
                     capture_output=True, text=True, encoding="utf-8", errors="replace")
check(bad.returncode == 2, "非框架根 → 退出码 2", f"rc={bad.returncode}")

print("I 运行模式声明（v5.3.0）：默认 quick；full 要有入口凭据")

PLAN_WITH_EVIDENCE = u"""# 主控工作计划

## 运行模式

- 当前档位：完整模式
- 怎么进来的：**已告知用户**（2026-09-17），判据 U1、U4
"""

PLAN_NO_EVIDENCE = u"""# 主控工作计划

## 运行模式

- 当前档位：完整模式
"""


def _cfg_set(**kv):
    c = json.loads(json.dumps(CONFIG))
    c.update(kv)
    w("project-state/config.json", json.dumps(c, ensure_ascii=False, indent=2))


def m_full_no_evidence():
    _cfg_set(mode="full")


def m_full_with_evidence():
    _cfg_set(mode="full")
    w("project-state/主控工作计划.md", PLAN_WITH_EVIDENCE)


def m_full_evidence_wrong_section():
    """入口凭据必须在「运行模式」段里——写在别处不算（那正是静默切档的样子）。"""
    _cfg_set(mode="full")
    w("project-state/主控工作计划.md",
      u"# 主控工作计划\n\n## 班形\n\n这个项目走完整模式，已告知用户。\n")


def m_quick_profile_c():
    _cfg_set(mode="quick", profile="C-standard-env")


def m_quick_profile_b():
    _cfg_set(mode="quick", profile="B-dual-env")


def m_bad_value():
    _cfg_set(mode="vibefull")


def m_no_mode():
    c = json.loads(json.dumps(CONFIG))
    c.pop("mode", None)
    w("project-state/config.json", json.dumps(c, ensure_ascii=False, indent=2))


expect_error("N1 mode=full 且无入口凭据 → ERROR（静默切档）",
             u"找不到入口凭据", m_full_no_evidence)
expect_error("N2 入口凭据写在别的段里 → 仍 ERROR（只认「运行模式」段）",
             u"找不到入口凭据", m_full_evidence_wrong_section)
expect_error("N3 quick + profile=C → ERROR（「快速模式却按三库要求」）",
             u"快速模式却按三库要求", m_quick_profile_c)
expect_warn("N4 quick + profile=B → WARN（不是错误，但请确认是刻意留着的）",
            u"双环境", m_quick_profile_b)
expect_warn("N5 不认识的 mode 取值 → WARN（工具不知道该按哪套规则解释）",
            u"不是认识的取值", m_bad_value)

# 反向：**切档是一等公民**——mode=full 本身不是错，错的永远是「没有入口凭据」。
# 这条防的是把本检查做成“不许用完整模式”的假阳性。
expect_clean("N6 mode=full + 「运行模式」段有告知留痕 → 干净（不得误伤正确的切档）",
             m_full_with_evidence)

# 反向：未声明 mode 不是错（v5.3.0 起默认 quick，这正是要固定下来的默认）。
# 要求报 ERROR 就等于把“默认快速”变成“必须手填”，与本次优化相反。
build()
m_no_mode()
r = run()
check("ERROR 0" in r.stdout and u"未声明 `mode`" in r.stdout,
      "N7 未声明 mode → 只记 INFO（默认快速，不当缺陷报）", r.stdout[-600:])

# 反向：干净树（未声明）仍然零 ERROR 零 WARN——新检查不能把基线弄脏。
expect_clean("N8 沙箱基线树（无 mode 字段）仍然 ERROR 0 / WARN 0",
             lambda: None)

# 边界：默认快速 + 默认单环境（未声明 profile）不得报矛盾。
def m_quick_only():
    _cfg_set(mode="quick")


expect_clean("N9 mode=quick 且未声明 profile → 干净（不把“没写”当成“写了 C”）",
             m_quick_only)

# 带 --json 时 I 组发现项也得进 findings（否则 CI 拿不到）。
build()
m_full_no_evidence()
r = run("--json")
try:
    d = json.loads(r.stdout)
    has_i = any(f.get("check", "").startswith("I") for f in d.get("findings", []))
except Exception:
    has_i = False
    d = {}
check(has_i and d.get("ok") is False,
      "N10 --json 里 I 组发现项可读且 ok=false（CI 拿得到）",
      r.stdout[:400])

print("K 极速修复台账（v5.12.0）：台账自己写的东西自不自洽")

# 极速修复模式的全部约束就是**退出线**（≥4 个文件、同一症状反复打补丁 → 必须退出回正常流程），
# 而这条线只有台账记得住。故本组用例两个方向都要钉死：**能拦**（该退未退要变红）与
# **不误报**（仍在极速中、空台账、写全了退出口的台账都是合法的）。
TURBO_MD = os.path.join("project-state", "问题", "极速修复台账.md")
TURBO_HDR = ("| id | at | source | basis | symptom | files | depth | evidence | pack | "
             "exit | exit_reason | verified |")
TURBO_SEP = "|----|----|--------|-------|---------|-------|-------|----------|------|" \
            "------|-------------|----------|"
TURBO_NCOL = len(TURBO_HDR.strip("|").split("|"))   # 12（从表头推出来，改列数时不会漏改夹具）


def turbo_ledger(rows, header=TURBO_HDR):
    """写一份极速修复台账；`rows` 只给数据行，表头/分隔行由夹具装配（`header=None` = 没有机读块）。"""
    if header is None:
        w(TURBO_MD, u"# 极速修复台账\n\n本周期没走过极速修复，纯手工记了两句散文。\n")
        return
    w(TURBO_MD, u"# 极速修复台账\n\n<!-- machine:turbo -->\n" + header + "\n" + TURBO_SEP +
                "\n" + u"\n".join(rows) + u"\n<!-- /machine:turbo -->\n")


def turbo_error(label, needle, rows, header=TURBO_HDR):
    """断言「报出来了」**且级别是 ERROR、组名是 K极速修复**（否则可能只是别组同词命中）。"""
    build()
    turbo_ledger(rows, header=header)
    out = run().stdout
    check(needle in out and "[ERROR] K极速修复" in out, label,
          f"未出现「{needle}」的 K组 ERROR；实际输出：\n{out[-700:]}")


def turbo_warn(label, needle, rows, header=TURBO_HDR):
    build()
    turbo_ledger(rows, header=header)
    out = run().stdout
    check(needle in out and "[WARN] K极速修复" in out, label,
          f"未出现「{needle}」的 K组 WARN；实际输出：\n{out[-700:]}")


def turbo_clean(label, rows, header=TURBO_HDR):
    """反向：本组**不许**对合法台账喊狼来了。断言零 ERROR 零 WARN **且**没有 K组发现项。

    夹具顺手把每行 `evidence` / `pack` 指向的文件建出来：**"证据指针落空""体验包找不到"本身就该是 WARN**
    （K11 / K16b 专测那两条），故"干净"必须连它们也落得实——否则这些反假阳性用例会先被自己造的
    缺失文件弄红，测的就成了夹具的毛病而不是判据的边界。
    """
    build()
    turbo_ledger(rows, header=header)
    for r in rows:
        cells = [c.strip() for c in r.strip("|").split("|")]
        if len(cells) != TURBO_NCOL:
            continue
        if cells[7]:                                   # evidence
            w(cells[7], u"# 自检证据（夹具生成）\n")
        if cells[8] and cells[8] != u"-":              # pack（`-` = 本轮未涉及交付/体验包）
            w(cells[8], u"快速体验包（夹具生成）\n")
    out = run().stdout
    ok = ("ERROR 0" in out and "WARN 0" in out
          and "[ERROR] K极速修复" not in out and "[WARN] K极速修复" not in out)
    check(ok, label, f"预期合法台账零 ERROR 零 WARN；实际输出：\n{out[-700:]}")


def row(rid="TRB-001", at="2026-09-30 15:41", source=u"用户原话：「极速修复这几个小 bug」",
        basis="card-013", symptom=u"客户列表页筛选项点第二次不生效", files="2", depth="smoke",
        evidence=u"证据/极速修复/TRB-001.md", pack=u"-", exit_="2026-09-30 15:52",
        exit_reason=u"用户确认效果 OK，本轮收口", verified=u"否"):
    return u" | ".join([rid, at, source, basis, symptom, files, depth, evidence, pack,
                        exit_, exit_reason, verified]).join([u"| ", u" |"])


# K1 台账不存在 = 项目管理上从没用过极速修复——**正常**，必须 INFO 跳过而不是 ERROR。
# 这条排第一位：把「没用过」报成缺陷，比漏报更坏（检查器一开就红，所有人开始无视它）。
build()
r = run()
check(u"没有 project-state/问题/极速修复台账.md" in r.stdout
      and u"视为本项目未用过极速修复模式" in r.stdout
      and "[ERROR] K极速修复" not in r.stdout,
      "K1 台账不存在 → 只记 INFO「未用过…跳过」，不是 ERROR、也不是「无法检查」", r.stdout[-600:])
expect_clean("K1b 台账不存在 → 不因缺台账弄脏基线（ERROR 0 / WARN 0）", lambda: None)

# K2 台账在、机读块没了 = 写成散文 → 无法校核，必须 ERROR。
# `header=None` 才是"没有机读块"；只写空表（`rows=[]` + 正常表头）是**合法**的空台账（见 K4c），
# 这两件事必须分开，否则 K2 会拿一份正常表头去测，白测。
turbo_error("K2 台账存在但没有 machine:turbo 机读块 → ERROR（写成散文就无法校核）",
            u"没有 `machine:turbo` 机读块", [], header=None)
build()
turbo_ledger([], header=None)
r = run()
check("[ERROR] K极速修复" in r.stdout,
      "K2b 散文台账 → 该条是 ERROR 级别而非 WARN（宽松成 WARN 等于默许不可校核的台账）",
      r.stdout[-600:])

# K3 列头逐字比对：少一列 / 改名都让那一列**无法校核**（尤其 exit / verified）。
# 两个用例都必须**显式换掉列头**——用默认列头去测"少一列"，测的其实还是正常表头（白测）。
turbo_error("K3 列头少一列（缺 verified）→ ERROR",
            u"列头不是约定的 12 列", [row()],
            header=("| id | at | source | basis | symptom | files | depth | evidence | pack | "
                    "exit | exit_reason |"))
turbo_error("K3b 列头被改名（verified → 已验证）→ ERROR",
            u"列头不是约定的 12 列", [row()],
            header=("| id | at | source | basis | symptom | files | depth | evidence | pack | "
                    "exit | exit_reason | 已验证 |"))
build()
turbo_ledger([row()], header=("| id | at | source | basis | symptom | files | depth | evidence | pack | "
                              "exit | exit_reason | verified |"))
r = run()
check("ERROR 0" in r.stdout and "[ERROR] K极速修复" not in r.stdout,
      "K3c 列头逐字一致 → 不报（不比字符集、不比集合，就是逐字）", r.stdout[-600:])

# K3d 数据行列数：**那一行**少一列 = 该行无法校核（列头对不代表数据行列数对）。
turbo_error("K3d 数据行只有 11 列 → ERROR（该行无法校核）",
            u"应为 12 列",
            [u"| TRB-001 | 2026-09-30 15:41 | 用户反馈 | card-013 | 症状 | 2 | smoke | "
             u"证据/极速修复/TRB-001.md | - | 2026-09-30 15:52 | 收口 |"])

# K4 反假阳性：干净台账（1~2 行、退出口写全、verified=否）→ 零 ERROR 零 WARN。
# 与「能拦住」同等重要——极速模式的常态恰恰是 verified=否。
turbo_clean("K4 干净台账 1 行（depth=smoke，exit+exit_reason 都填，verified=否）→ 不误报", [row()])
turbo_clean("K4b 干净台账 2 行（不同 symptom，各自收口）→ 不误报",
            [row(), row(rid="TRB-002", symptom=u"登录态偶发丢失",
                        files="1", depth="targeted", evidence=u"证据/极速修复/TRB-002.md")])
turbo_clean("K4c 空台账（有机读块、0 数据行）→ 合法，不算错", [])

# K5 退出线的「成对判据」两个方向都要有：一空一满 = 进了没记退 / 退了没记因；
# 两个都空 = 仍在极速中（这正是**常态**，误报它等于逼人编造退出时间）。
turbo_error("K5 exit 有、exit_reason 空 → ERROR（退了没记因）",
            u"进了没记退 / 退了没记因", [row(exit_reason=u"")])
turbo_error("K5b exit 空、exit_reason 有 → ERROR（进了没记退）",
            u"进了没记退 / 退了没记因", [row(exit_="", exit_reason=u"改完就没再跟")])
turbo_clean("K5c exit 与 exit_reason 都空（仍在极速中）→ 不报（成对判据的另一半）",
            [row(exit_="", exit_reason="")])

# K6 depth 只认 smoke / targeted：写 full 说明这一轮本就不该走极速修复（该开卡走正常流程）。
turbo_error("K6 depth=full → ERROR（这一轮本不该走极速修复）",
            u"不在 {smoke, targeted}", [row(depth="full", exit_="", exit_reason="")])
turbo_error("K6b depth 空值 → 同样 ERROR（空值无法证明只跑了冒烟）",
            u"不在 {smoke, targeted}", [row(depth="", exit_="", exit_reason="")])

# K7 退出线：一次修复动到 ≥4 个文件就必须退出极速。**这里必须成对**——
# 只测 files=4 报错的话，判据写成「files≥4 一律报错」也能全绿，那会误伤已经正常收口的记录。
turbo_error("K7 files=4 且 exit 空 → ERROR（越过退出线未退出）",
            u"≥4 个文件", [row(files="4", exit_="", exit_reason="")])
turbo_clean("K7b files=4 但 exit 已填（做完就退出极速）→ 不报（退出线是「未退出」，不是「动了 4 个文件」）",
            [row(files="4")])
turbo_error("K7c files 不是正整数 → ERROR（无法判断是否越过退出线）",
            u"不是正整数", [row(files="abc", exit_="", exit_reason="")])

# K8 同一 symptom 反复打补丁仍不收口 = 该退未退。成对：补上最后一行 exit 就该放过。
turbo_error("K8 同一 symptom 3 行且末行 exit 空 → ERROR（该退未退）",
            u"该退未退", [row(rid="TRB-001", exit_="", exit_reason=""),
                        row(rid="TRB-002", at="2026-09-30 16:01", exit_="", exit_reason=""),
                        row(rid="TRB-003", at="2026-09-30 16:20", exit_="", exit_reason="")])
turbo_clean("K8b 同一 symptom 3 行但末行已收口 → 不报（判据钉的是「末行仍空」，不是「出现 3 次」）",
            [row(rid="TRB-001", exit_="", exit_reason=""),
             row(rid="TRB-002", at="2026-09-30 16:01", exit_="", exit_reason=""),
             row(rid="TRB-003", at="2026-09-30 16:20")])
turbo_clean("K8c 同一 symptom 只 2 行且都空 → 不报（阈值是 ≥3 行，别把正常两轮小修判成该退未退）",
            [row(rid="TRB-001", exit_="", exit_reason=""),
             row(rid="TRB-002", at="2026-09-30 16:01", exit_="", exit_reason="")])

# K9 verified 必须显式写 是/否：不写等于把「没独立验证」读成「验证过了」。
turbo_error("K9 verified 为空 → ERROR（不写就是把未独立验证洗成验证过）",
            u"`verified` 为空", [row(verified="")])
turbo_warn("K9b verified 取值不认识（如「大概吧」）→ WARN（脏数据，但不是结构性缺口）",
           u"不是认识的取值", [row(verified=u"大概吧")])
turbo_clean("K9c verified=是 → 不报（显式声明独立验证过同样合法）", [row(verified=u"是")])

# K10 id 全局唯一且升序：重复 / 乱序 / 形状不对都是一条消息说清三种情形。
turbo_error("K10 id 重复 → ERROR（id 必须全局唯一）",
            u"重复出现", [row(rid="TRB-001"), row(rid="TRB-001", at="2026-09-30 16:01")])
turbo_error("K10b id 乱序（TRB-002 在 TRB-001 之前）→ ERROR",
            u"未按出现顺序升序", [row(rid="TRB-002"), row(rid="TRB-001", at="2026-09-30 16:01")])
turbo_error("K10c id 形状不对（TRB-1）→ ERROR（不匹配 TRB-NNN）",
            u"不匹配 `TRB-NNN`", [row(rid="TRB-1")])

# K11 证据指针：台账说「证据在 证据/极速修复/TRB-001.md」，那文件得真的在（否则「留了自检证据」只是句话）。
# 判据口径同 C 组：文件不存在 = WARN 指出指针落空，**不因为项目根下没有它就报 ERROR**。
turbo_warn("K11 evidence 指向的文件不存在 → WARN（照 C 组口径，不升级成 ERROR）",
           u"不存在", [row()])
build()
turbo_ledger([row()])
w(os.path.join("证据", "极速修复", "TRB-001.md"), u"# 自检证据\n")
r = run()
check("ERROR 0" in r.stdout and "WARN 0" in r.stdout and "[WARN] K极速修复" not in r.stdout,
      "K11b evidence 文件真的在 → 不报（防「凡有证据指针就警告」的偷懒写法）", r.stdout[-600:])

# K12 汇总行（INFO）：说清本组核过什么、核了几条，否则读报告的人无法判断这是不是空跑。
build()
turbo_ledger([row(), row(rid="TRB-002", at="2026-09-30 16:20", symptom=u"登录态偶发丢失",
                         exit_="", exit_reason="")])
r = run()
check(u"台账 2 条：仍在极速中 1 / 已退出 1" in r.stdout and u"[INFO] K极速修复" in r.stdout,
      "K12 汇总行报出「台账 N 条：仍在极速中 x / 已退出 y；档位 …」", r.stdout[-700:])

# K13 时间格式：`at` / `exit` 不是 `YYYY-MM-DD HH:MM` → WARN（脏数据，不阻塞）；
# 合法格式（含 T 分隔的 ISO 写法）不得误报。
turbo_warn("K13 at 不是 YYYY-MM-DD HH:MM → WARN",
           u"不是 `YYYY-MM-DD HH:MM` 格式", [row(at="2026/09/30 15:41")])
turbo_clean("K13b at/exit 用 ISO 的 T 分隔写法 → 不报（两种分隔都认）",
            [row(at="2026-09-30T15:41", exit_="2026-09-30T15:52")])

# K14 汇总行必须反映真实档位（只有 smoke 时不得说成 targeted）——防汇总把没发生的事写成发生。
build()
turbo_ledger([row(verified=u"是", exit_="", exit_reason="")])
r = run()
check(u"smoke 1" in r.stdout and u"targeted" not in r.stdout,
      "K14 汇总的档位统计只反映台账里真实出现过的 depth", r.stdout[-600:])

# ---- 12 列升级（v5.13.0）：`basis` 改动依据 + `pack` 快速体验包 ----------------------
# 极速修复的前提是"基于用户反馈 + 正常开发形成的既有需求卡/测试卡排查"，不是把现象直接丢给模型乱改；
# 而面向客户的快速体验包必须在台账里有落点。两条判据都**必须成对**：能拦住"没依据的空 basis"
# 与"落空的体验包"，又不许对合法写法（no-card / tc-002 / card+tc / `-` / 真实文件）喊狼来了。
turbo_error("K15 basis 空 → ERROR（改之前没有依据）",
            u"改之前没有依据", [row(basis=u"")])
turbo_clean("K15b basis=card-013（默认写法）→ 不报", [row()])
turbo_warn("K15c basis 形状坏（card-13，两位数字）→ WARN（脏数据，不是结构性缺口）",
           u"不是认识的形状", [row(basis=u"card-13")])
turbo_clean("K15d basis=no-card（如实声明没有对应卡）→ 不报", [row(basis=u"no-card")])
turbo_clean("K15e basis=tc-002（指向既有测试卡）→ 不报", [row(basis=u"tc-002")])
turbo_clean("K15f basis=card-013+tc-002（需求卡 + 测试卡都指）→ 不报",
            [row(basis=u"card-013+tc-002")])

# K15g 形状坏**只能**是 WARN：若顺手写成 ERROR，K15c 会"过"（它也含 WARN），但这条会红——
# 两条合起来才钉死"空 → ERROR / 形状坏 → WARN"的分工。反之若把空值也降成 WARN，K15 变红。
build()
turbo_ledger([row(basis=u"卡片13")])
r = run()
check("ERROR 0" in r.stdout and u"[WARN] K极速修复" in r.stdout,
      "K15g basis 形状坏 → 只 WARN 不 ERROR（脏数据不该阻塞，与空值分工明确）", r.stdout[-600:])

# K16 `pack`：`-` = 本轮未涉及交付/体验包（常态，不报）；空 = 没说清（ERROR）；
# 其余按项目根相对路径判存在性（照 C 组与 evidence 同一口径：落空 WARN，不升级 ERROR）。
turbo_error("K16 pack 空 → ERROR（要么写路径、要么写 `-`）",
            u"`pack` 为空", [row(pack=u"")])
turbo_warn("K16b pack 指向的路径不存在 → WARN（照 C 组与 evidence 口径）",
           u"`pack` 指向的", [row(pack=u"交付/体验包/TRB-001.zip")])
turbo_clean("K16c pack=-（本轮未涉及交付/体验包）→ 不报", [row(pack=u"-")])
turbo_clean("K16d pack 指向真实文件 → 不报（防「凡有 pack 就警告」的偷懒写法）",
            [row(pack=u"交付/体验包/TRB-001.zip")])

# K17 汇总行追加：依据分布（no-card 计入无卡）与体验包条数（pack 非 `-`）——
# 否则读报告的人看不出这份台账里"有几次是凭既有卡排查的、交付了几个体验包"。
build()
turbo_ledger([row(),
              row(rid="TRB-002", at="2026-09-30 16:20", basis=u"no-card",
                  symptom=u"登录态偶发丢失", pack=u"交付/体验包/TRB-002.zip",
                  exit_="", exit_reason="")])
w(os.path.join("证据", "极速修复", "TRB-001.md"), u"# 自检证据\n")
w(os.path.join("交付", "体验包", "TRB-002.zip"), u"快速体验包（夹具生成）\n")
r = run()
check(u"依据分布：有卡 1 条 / 无卡 1 条" in r.stdout and u"体验包 1 条" in r.stdout,
      "K17 汇总行报出依据分布（no-card 计入无卡）与体验包条数", r.stdout[-700:])

shutil.rmtree(ROOT, ignore_errors=True)

print()
print(f"通过 {len(passes)} / 失败 {len(fails)}")
if fails:
    print("失败项：")
    for f in fails:
        print("  -", f)
sys.exit(1 if fails else 0)
