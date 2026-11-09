#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guide_audit.py 的回归测试（v5.0.0 新增；v5.1.0 加覆盖面声明与 G11；v5.4.0 加 T-K 快速模式裁剪面；v5.1.1 加 T-L 发现号形态闸门）。

为什么这个测试必须存在：`guide_audit.py` 是指导模式的**探测点**——它替项目经理去看
「机制有没有被绕过」。探测器有两种坏法，而且都比"没有探测器"更坏：

1. **永远报警**（假阳性）：新项目第一次体检就被自己的模板吓一跳，人就不看了。
   实测踩过：框架仓库自己跑出「PRD 有 3 条 REQ 但 0 张卡承接」ERROR——模板的示例 REQ
   被当成漏建卡。**这就是本文件里 T-C/T-D 两组断言的由来。**
2. **永远不响**（假阴性）：看起来有护栏，其实绕过它照样能过。T-A 组用一份"每一项都违规"
   的样本证明**每个检查号都能真的抓到**；T-B 组把违规逐项修好，证明**每一项都会重新变绿**
   （只会变红不会变绿的灯，和坏掉的灯没有区别）。

v5.1.0 又多了第三种坏法（比前两种更隐蔽）：**结论脱离范围**——`ERROR 0` 被读成"没问题"。
故 T-I 组钉住覆盖面声明（三处输出同源、范围跟随 `--check`、`off` 时 `covered` 为空），
T-J 组钉住 G11 台账（零归属 / 假登记 / 缺失三种状态各自的行为），T-F 组升级为**三方配对**
（脚本 ↔ 手册 ↔ 回归用例）。

覆盖：
- T-A 阳性：每个检查号在违规样本上都能抓到，且用户点名的两个场景
      （①需求没建卡 ②运维把 prod 当 test 用）有专门断言。
- T-B 变异/反向：逐项修好后对应检查号必须消失（防"永远报警"）。
- T-C 未开工不误报：只有模板、没有开工痕迹的工作区 → ERROR 0（台账缺失只记 WARN）。
- T-D 档位守卫：profile=A（非三环境）时 G05 不得套三库条款。
- T-E 只读承诺：跑完文件内容一字节不变（含 --write 只新增报告）。
- T-F --selfcheck：三方配对（脚本 ↔ 手册处方 ↔ 回归用例）缺任一方必须退出 1。
- T-G 参数与退出码：--check 过滤、未知检查号、非工作区、GBK 控制台不崩。
- T-H 指导模式开关：`off` 绝不能被读成"没问题"。
- T-I 覆盖面声明（v5.1.0）：终端 / --json / --write 三处同源，范围跟随 --check。
- T-J G11 台账（v5.1.0）：零归属 / 假登记是 ERROR，台账缺失是 WARN（快速模式只提醒）。
- T-K 快速模式不裁机制（v5.4.0）：同一份违规样本在 `mode=full` 与 `mode=quick` 下**ERROR
      集合逐字相同**（"需求卡照建"不是口头承诺），而**降噪只发生在 G02 占位契约引用与
      G11 台账缺失**两处——承诺的差别是真的，且没有扩到别处。
- T-L 台账数据表闸门（F-41，v5.1.1）：台账正文里 ≥6 列的**数据表**（编号栏是字数 / 文件名）
      不得被读成发现行；形态合法的真坏行照抓，被跳过的表行留 INFO（不静默）。

用法：python scripts/tests/test_guide_audit.py
退出码 0=全过；1=有失败（会把失败项打出来）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parents[1]
SCRIPT = Path(os.environ.get("GUIDE_SCRIPT") or (FRAMEWORK / "scripts" / "guide_audit.py"))
ROOT = Path(tempfile.mkdtemp(prefix="guide_test_"))
SANDBOX = Path(tempfile.mkdtemp(prefix="guide_sandbox_"))

fails: list = []
passes: list = []


def _load_audit_module():
    """直接载入被测脚本，好让断言跟着 CATALOG / COVERAGE_UNCOVERED 走。

    硬编码一份检查号清单，等于把"新增检查号要配回归"这件事交给记性——而这正是
    `--selfcheck` 的第三方要拦的。清单在这里自动同步，断言就不会悄悄漏掉新号。
    """
    spec = importlib.util.spec_from_file_location("guide_audit_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_AUDIT = _load_audit_module()
ALL_CHECKS = sorted(_AUDIT.CATALOG)
COVERAGE_DIMS = [d for d, _ in _AUDIT.COVERAGE_UNCOVERED]


def check(cond, label, detail=""):
    (passes if cond else fails).append(label)
    tail = f"  -- {detail}" if detail and not cond else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{tail}")


def touch(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def run(*args, root=None, env=None):
    cmd = [sys.executable, str(SCRIPT)]
    if root is not None:
        cmd.append(str(root))
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=120)


def run_bytes(*args, root=None, env=None):
    """GBK 控制台场景：PYTHONIOENCODING=gbk 时子进程输出的字节是 GBK，
    用 utf-8 去解会是满屏乱码——那是测试脚手架的问题，不是脚本的问题。"""
    cmd = [sys.executable, str(SCRIPT)]
    if root is not None:
        cmd.append(str(root))
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, env=env, timeout=120)


def audit(root, *args, env=None):
    """跑一次 --json，返回 (退出码, 解析结果 或 None)。"""
    p = run("--json", *args, root=root, env=env)
    try:
        return p.returncode, json.loads(p.stdout)
    except Exception:
        return p.returncode, None


def ids(data, level=None):
    return sorted({f["check"] for f in data["findings"] if level is None or f["level"] == level})


def msg_of(data, cid):
    return " ".join(f["message"] for f in data["findings"] if f["check"] == cid)


def snapshot(base: Path):
    """文件内容指纹表，用来证明"只报告不改动"（键统一成 posix 风格，免得跨平台比不出）。"""
    out = {}
    for f in sorted(base.rglob("*")):
        if f.is_file():
            out[f.relative_to(base).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------- 样本

CARD = """---
id: {cid}
title: {title}
req: {req}
source: {source}
status: {status}
assignee: {assignee}
deps: {deps}
clarify_open: {clarify}
req_confirm: {confirm}
priority: P0
---

## 需求描述

{desc}

## 进度日志（progress）

{progress}
"""

PRD = """# 产品需求文档

## 功能需求

- REQ-1.1 用户能登录
- REQ-1.2 用户能退出
- REQ-1.3 用户能改密码
"""

ENV_MD = """# 环境清单

## 端口分配

| 服务 | dev | test | prod |
|------|-----|------|------|
| 前端 | 5173 | 5174 | <按网关> |
| 后端 | 8000 | 8001 | <按网关> |
| PostgreSQL | 5432 | 5433 | 5433 |
| Redis | 6379 | 6380 | <按生产> |

> 三环境端口/库名/`DATABASE_URL` 必须互不相同，防止跨环境串数据。
"""

CR_TPL = """# 变更申请单 CR-{no}

## A. 基本信息
- 目标环境：prod

## G. 审批栏（缺任一 → 不得执行）
| 角色 | 结论 | 署名 | 时间 | 备注 |
|---|---|---|---|---|
| 主控（放行） | 同意 / 驳回 | {sign} | {time} | |
| 安全审计（数据/配置类必签） | 风险可接受 / 需补充 | | | |
| 项目经理（登记知会） | 已登记 | | | |

## H. 执行留痕栏（执行后回填，只追加不回改）
- 执行人：{who}
"""

LEDGER_HEAD = """# 指导发现台账

> 归属只能指向**真实存在**的东西：检查号（`G\\d\\d`，须在脚本 CATALOG 里）/
> 变更单（`CR-YYYYMMDD-NNN`，`问题/变更申请单/` 下须有单）/ 检查工具脚本
> （`scripts/` 下须存在）。零归属 = 未收口；指向查不到的号 = 假登记。

| 编号 | 日期 | 场景 | 现有检查能否抓到 | 归属 | 状态 |
|---|---|---|---|---|---|
"""

LEDGER_ROW = ("| {no} | {date} | {scene} | {catch} | {owner} | {state} |\n")

# 每条对应 G11 的一种坏法。**FIND-001 必须是好行**——否则"报错"可能只是因为整张表坏了，
# 而证明不了「坏行会被单独挑出来、好行不会被误伤」。
DIRTY_LEDGER_ROWS = [
    dict(no="FIND-001", date="2026-09-15", scene="需求没建卡就开工（已升成检查点）",
         catch="是", owner="G01", state="已升成检查点"),
    dict(no="FIND-002", date="2026-09-15", scene="零归属：归属栏空着",
         catch="否", owner="", state="待办"),
    dict(no="FIND-003", date="2026-09-15", scene="假登记：指向脚本里没有的检查号",
         catch="否", owner="G99", state="待办"),
    dict(no="FIND-004", date="2026-09-15", scene="假登记：指向不存在的变更单",
         catch="否", owner="CR-20260101-999", state="待办"),
    dict(no="FIND-005", date="2026-09-15", scene="假登记：指向不存在的检查工具",
         catch="否", owner="no_such_tool.py", state="待办"),
    dict(no="FIND-006", date="2026-09-15", scene="归属栏写了一句没法复核的话",
         catch="否", owner="已修复（回头补）", state="待办"),
    dict(no="FIND-007", date="2026-09-15", scene="「能否抓到」填了别的值",
         catch="待定", owner="G05", state="待办"),
]

CLEAN_LEDGER_ROWS = [
    dict(no="FIND-001", date="2026-09-15", scene="需求没建卡就开工",
         catch="是", owner="G01", state="已升成检查点"),
    dict(no="FIND-002", date="2026-09-15", scene="证据落进 tmp",
         catch="是", owner="G07", state="已升成检查点"),
    dict(no="FIND-003", date="2026-09-15", scene="要动目录结构，走变更单",
         catch="否", owner="CR-20260101-001", state="已转变更单"),
    dict(no="FIND-004", date="2026-09-15", scene="合规类，由既有工具守",
         catch="否", owner="path_audit.py", state="已归属既有工具"),
]


def build_dirty(base: Path):
    """每一项都违规的样本：用来证明每个检查号都能真的抓到。"""
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    ps = base / "project-state"
    touch(base / "skills" / "后端开发" / "SKILL.md", "x")
    touch(base / "skills" / "架构师" / "SKILL.md", "x")
    touch(ps / "config.json", json.dumps({
        "schema_version": "1", "mode": "full", "profile": "C-standard-env",
        "paths": {"tmp": "tmp", "repos": ["repos"], "project_state": "project-state"},
    }, ensure_ascii=False, indent=2))
    touch(ps / "契约" / "产品需求文档.md", PRD)
    touch(ps / "契约" / "环境.md", ENV_MD)
    touch(ps / "任务清单.md", "# 任务清单\n\n- T-01 登录（引 card-001）\n- T-09 旧任务（引 card-098）\n")
    touch(ps / "进度" / "后端开发.json", '{"role": "后端开发"}')
    touch(ps / "进度" / "overall.json", '{"go": false}')

    # card-001：未建"契约"就开工 + 待澄清未清零 + 悬空依赖 + 卷宗混入代码
    touch(ps / "需求卡" / "card-001" / "card.md", CARD.format(
        cid="card-001", title="登录", req="REQ-1.1", source="", status="in_progress",
        assignee="后端开发", deps="[card-099]", clarify="2", confirm='""',
        desc="关联契约：`project-state/契约/不存在的契约.md`", progress=""))
    touch(ps / "需求卡" / "card-001" / "证据" / "patch.py", "print('code in card dir')\n")

    # card-002：置 done 但进度日志空 + 验收记录空壳 + 用 dev 证据验收 + 卷宗混入构建产物
    touch(ps / "需求卡" / "card-002" / "card.md", CARD.format(
        cid="card-002", title="退出", req="REQ-1.2", source="", status="done",
        assignee="后端开发", deps="[]", clarify="0", confirm='""', desc="退出登录", progress=""))
    touch(ps / "需求卡" / "card-002" / "验收-测试.md",
          "## 验收结论\n\n执行环境：dev\n\n- 结果：<填>\n")
    touch(ps / "需求卡" / "card-002" / "node_modules" / "pkg" / "a.js", "x\n")

    # card-003：req_confirm 假登记（登记了文件却不存在）+ 追溯链断 + 架构师无进度落盘
    touch(ps / "需求卡" / "card-003" / "card.md", CARD.format(
        cid="card-003", title="改密码", req='""', source='""', status="in_progress",
        assignee="架构师", deps="[]", clarify="0",
        confirm="需求卡/card-003/需求确认-产品经理.md", desc="改密码", progress=""))

    # card-004：状态非法 + 无归属人 + 目录名与 id 不一致
    touch(ps / "需求卡" / "card-004" / "card.md", CARD.format(
        cid="card-00X", title="个人中心", req='""', source='""', status="doing",
        assignee='""', deps="[]", clarify="0", confirm='""', desc="个人中心", progress=""))

    # G06：审批栏空栏，但执行留痕已回填 = 未批准就执行
    touch(ps / "问题" / "变更申请单" / "CR-20260101-001-数据配置.md",
          CR_TPL.format(no="20260101-001", sign="", time="", who="运维：张三（已执行）"))

    # G07：证据落进 tmp
    touch(base / "tmp" / "验收-测试-证据.txt", "证据\n")

    # G11：台账在，但登记行各有各的坏法（见 LEDGER_ROW 注释）
    touch(ps / "记忆" / "指导发现台账.md", LEDGER_HEAD + "".join(
        LEDGER_ROW.format(**row) for row in DIRTY_LEDGER_ROWS))


def clean_all(base: Path):
    """把违规逐项修好——对应检查号必须随之消失（防"永远报警"）。"""
    ps = base / "project-state"
    touch(ps / "契约" / "不存在的契约.md", "# 补上这份契约\n")
    touch(base / "tmp" / "note.txt", "普通临时件\n")
    (base / "tmp" / "验收-测试-证据.txt").unlink()
    shutil.rmtree(ps / "需求卡" / "card-001" / "证据")
    shutil.rmtree(ps / "需求卡" / "card-002" / "node_modules")

    touch(ps / "任务清单.md", "# 任务清单\n\n- T-01 登录（引 card-001）\n")

    touch(ps / "需求卡" / "card-001" / "card.md", CARD.format(
        cid="card-001", title="登录", req="REQ-1.1", source="", status="in_progress",
        assignee="后端开发", deps="[]", clarify="0", confirm='""',
        desc="关联契约：`project-state/契约/不存在的契约.md`",
        progress="- `[2026-09-15 10:00] 开工：接了登录接口，`pytest -k login` 8 项全过\n"))
    touch(ps / "需求卡" / "card-001" / "需求确认-产品经理.md", "# 需求确认\n\n逐条确认 3 项，待澄清 0 项。\n")

    touch(ps / "需求卡" / "card-002" / "card.md", CARD.format(
        cid="card-002", title="退出", req="REQ-1.2", source="", status="done",
        assignee="后端开发", deps="[]", clarify="0", confirm='""', desc="退出登录",
        progress="- `[2026-09-15 11:00] 开工：退出接口完成，dev 自测通过\n"
                 "- `[2026-09-15 12:00] 自验：test 环境 `pytest -k logout` 全绿\n"))
    touch(ps / "需求卡" / "card-002" / "验收-测试.md",
          "## 验收结论\n\n执行环境：test（自检全绿）\n\n"
          "- 命令：`pytest -k logout -q`\n- 结果：12 passed，0 failed\n"
          "- 证据：`证据/logout-pytest.txt`\n- 结论：本卡测试路验收通过\n")
    touch(ps / "需求卡" / "card-002" / "需求确认-产品经理.md", "# 需求确认\n\n逐条确认 2 项。\n")

    touch(ps / "需求卡" / "card-003" / "card.md", CARD.format(
        cid="card-003", title="改密码", req="REQ-1.3", source="", status="in_progress",
        assignee="架构师", deps="[]", clarify="0", confirm="需求卡/card-003/需求确认-产品经理.md",
        desc="改密码", progress="- `[2026-09-15 10:30] 开工：拆改密码接口\n"))
    touch(ps / "需求卡" / "card-003" / "需求确认-产品经理.md", "# 需求确认\n\n逐条确认 3 项。\n")

    touch(ps / "需求卡" / "card-004" / "card.md", CARD.format(
        cid="card-004", title="个人中心", req="REQ-1.4", source="", status="todo",
        assignee="架构师", deps="[]", clarify="0", confirm='""', desc="个人中心", progress=""))

    touch(ps / "进度" / "架构师.json", '{"role": "架构师"}')
    touch(ps / "问题" / "变更申请单" / "CR-20260101-001-数据配置.md",
          CR_TPL.format(no="20260101-001", sign="老王", time="2026-01-01 10:00",
                        who="运维：张三（已执行）"))
    touch(ps / "契约" / "环境.md", ENV_MD.replace("| 5432 | 5433 | 5433 |", "| 5432 | 5433 | 6543 |"))

    # G11：修好的台账——四条归属分别落在三类真实存在的东西上（检查号 / 变更单 / 工具脚本）
    touch(ps / "记忆" / "指导发现台账.md", LEDGER_HEAD + "".join(
        LEDGER_ROW.format(**row) for row in CLEAN_LEDGER_ROWS))


# ---------------------------------------------------------------- 测试组


def test_a_positive():
    print("\n测试 A（阳性）：每一项违规各自的检查号都要能抓到")
    build_dirty(ROOT)
    code, data = audit(ROOT)
    if data is None:
        check(False, "样本可跑出 --json", "输出无法解析")
        return
    got = ids(data)
    for cid in ALL_CHECKS:
        check(cid in got, f"{cid} 被抓到", f"实际 {got}")
    check(code == 1, "有 ERROR 时退出码为 1", str(code))
    check("card-099" in msg_of(data, "G01") and "card-098" in msg_of(data, "G01"),
          "G01 抓到两张悬空引用的卡")
    check("REQ-1.3" in msg_of(data, "G01"), "G01 抓到无卡承接的 REQ-1.3")
    check("不存在的契约.md" in msg_of(data, "G02"), "G02 指名了缺失的契约文件")
    check("进度日志" in msg_of(data, "G03"), "G03 抓到「置 done 但无进度留痕」")
    check("空壳" in msg_of(data, "G03"), "G03 抓到空壳验收记录")
    check("clarify_open=2" in msg_of(data, "G04"), "G04 抓到待澄清未清零就开工")
    check("假登记" in msg_of(data, "G04"), "G04 抓到 req_confirm 假登记")
    # 用户点名的场景②：运维把线上库偷偷换成测试库
    check("G05" in ids(data, "ERROR") and "prod 与 test 共用同一目标" in msg_of(data, "G05"),
          "G05 抓到 prod 端口与 test 相同（= 环境降级/混用）")
    check("验收证据只认 test" in msg_of(data, "G05"), "G05 抓到用 dev 证据顶替 test 验收")
    check("未批准就执行" in msg_of(data, "G06"), "G06 抓到审批栏空栏却已执行")
    check("tmp" in msg_of(data, "G07"), "G07 抓到证据落进 tmp")
    check("代码源文件" in msg_of(data, "G08"), "G08 抓到卷宗混入代码副本")
    check("构建产物/依赖目录" in msg_of(data, "G08"), "G08 抓到卷宗混入 node_modules")
    check("status 非法" in msg_of(data, "G09") and "assignee 为空" in msg_of(data, "G09"),
          "G09 抓到状态非法与无归属人")
    check("架构师" in msg_of(data, "G10"), "G10 抓到角色有在办卡却无进度落盘")
    # G11：三类假登记 + 零归属 + 无法复核的归属，逐条点名
    g11 = msg_of(data, "G11")
    check("FIND-001" not in g11, "G11 不误伤好行（归属到真实检查号的那条）", g11)
    check("FIND-002" in g11 and "零归属" in g11, "G11 抓到零归属", g11)
    check("FIND-003" in g11 and "不存在的检查号" in g11 and "G99" in g11,
          "G11 抓到假登记（不存在的检查号）", g11)
    check("FIND-004" in g11 and "不存在的变更单" in g11 and "CR-20260101-999" in g11,
          "G11 抓到假登记（不存在的变更单）", g11)
    check("FIND-005" in g11 and "不存在的检查工具" in g11 and "no_such_tool.py" in g11,
          "G11 抓到假登记（不存在的检查工具）", g11)
    check("FIND-006" in g11 and "没有可识别的号" in g11,
          "G11 抓到无法复核的归属（写了一句没号的话）", g11)
    check("FIND-007" in g11 and "既非「是」也非「否」" in msg_of(data, "G11"),
          "G11 的「能否抓到」列取值非法时报 WARN", g11)
    check("G11" in ids(data, "ERROR") and "G11" in ids(data, "WARN"),
          "G11 同时按 ERROR（假登记/零归属）与 WARN（取值无法复核）两档报", str(data["counts"]))


def test_b_mutation():
    print("\n测试 B（变异/反向）：逐项修好后必须变绿——只会红的灯和坏灯没区别")
    clean_all(ROOT)
    code, data = audit(ROOT)
    if data is None:
        check(False, "修好的样本可跑出 --json", "输出无法解析")
        return
    got = ids(data)
    leftover = [c for c in ALL_CHECKS if c in got]
    check(not leftover, "修好后各项检查全部安静", f"仍报 {leftover}：{data['findings']}")
    check(code == 0, "无 ERROR 时退出码为 0", str(code))
    check(data["counts"]["ERROR"] == 0 and data["counts"]["WARN"] == 0,
          "修好后 ERROR 0 / WARN 0", str(data["counts"]))
    check((ROOT / "project-state" / "记忆" / "指导发现台账.md").is_file(),
          "修好的样本里台账仍在——G11 安静是因为登记行干净，不是因为台账被删了")


def test_c_not_started():
    print("\n测试 C（假阳性守卫）：只有模板、无开工痕迹的工作区不得报错")
    base = SANDBOX / "fresh"
    ps = base / "project-state"
    touch(ps / "config.json", json.dumps({"schema_version": "1", "mode": "full",
                                          "profile": "C-standard-env"}, ensure_ascii=False))
    touch(ps / "契约" / "产品需求文档.md", PRD)          # 模板里的示例 REQ
    touch(ps / "契约" / "环境.md", ENV_MD.replace("| 5432 | 5433 | 5433 |", "| 5432 | 5433 | <按生产> |"))
    code, data = audit(base)
    if data is None:
        check(False, "空工作区可跑出 --json", "输出无法解析")
        return
    check(code == 0 and data["counts"]["ERROR"] == 0,
          "未开工：ERROR 0（模板示例 REQ 不算漏建卡）", str(data["findings"]))
    check("G01" not in ids(data), "未开工不报 G01")
    check("G05" not in ids(data), "未开工不报 G05（prod 占位只是还没填）")
    # v5.1.0：台账没建是**流程类**，不该在新项目第一次体检时就把人拦下
    check("G11" not in ids(data, "ERROR"), "未开工不报 G11 ERROR（台账还没建而已）")
    check("G11" in ids(data, "WARN") and "自增强回路没有入口" in msg_of(data, "G11"),
          "未开工时台账缺失记 WARN 并说明回路没入口", msg_of(data, "G11"))


def test_d_profile_guard():
    print("\n测试 D（档位守卫）：profile=A 时不得硬套三库条款")
    base = SANDBOX / "profile_a"
    shutil.copytree(ROOT, base, ignore=shutil.ignore_patterns("__pycache__"))
    cfg = base / "project-state" / "config.json"
    d = json.loads(cfg.read_text(encoding="utf-8"))
    d["profile"] = "A-single-env"
    cfg.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    code, data = audit(base)
    if data is None:
        check(False, "档位 A 样本可跑出 --json", "输出无法解析")
        return
    check("G05" not in ids(data), "档位 A 下 G05 完全沉默", str(ids(data)))
    ok, _ = audit(base)
    check(ok == 0, "档位 A 下退出码 0（三库违规样本不适用该档）", str(ok))


def test_e_readonly():
    print("\n测试 E（只读承诺）：跑完文件内容一字节不变")
    before = snapshot(ROOT)
    code, data = audit(ROOT)
    after = snapshot(ROOT)
    check(before == after, "普通运行不改动任何文件",
          str([k for k in set(before) | set(after) if before.get(k) != after.get(k)]))
    # --write 的断言要在**有发现项**的状态下做：空报告里没有处置列，测不出东西
    build_dirty(ROOT)
    before = snapshot(ROOT)
    p = run("--write", root=ROOT)
    check(p.returncode in (0, 1), "--write 正常返回", (p.stderr or "")[-400:])
    after2 = snapshot(ROOT)
    added = sorted(set(after2) - set(before))
    check(len(added) == 1 and added[0].startswith("project-state/指导体检"),
          "--write 只新增一份体检报告", str(added))
    check(all(before[k] == after2[k] for k in before), "--write 不修改既有文件")
    if added:
        body = (ROOT / added[0]).read_text(encoding="utf-8")
        check("处置" in body and "责任人" in body, "报告含处置与责任人列")
        check("## 覆盖面与盲区" in body and all(d in body for d in COVERAGE_DIMS),
              "落盘报告带覆盖面声明（盲区逐条列出）",
              [d for d in COVERAGE_DIMS if d not in body])


BOX_MANUAL_BODY = "症状与判据出处、最小修复动作、责任人写在这里。" + "补足字数。" * 30


def _box_manual(body=BOX_MANUAL_BODY, ids=None, dims=None):
    """造一份指导手册。ids/dims 显式传入要包含哪些，便于造出"缺号""缺盲区"的坏样本。"""
    ids = ALL_CHECKS if ids is None else ids
    dims = COVERAGE_DIMS if dims is None else dims
    parts = ["## {cid} 处方\n\n{body}\n\n".format(cid=cid, body=body) for cid in ids]
    if dims:
        parts.append("## 5 诚实边界\n\n"
                     + "".join("- %s：未覆盖\n" % d for d in dims))
    return "".join(parts)


def _selfcheck(box):
    return subprocess.run([sys.executable, str(box / "scripts" / "guide_audit.py"), "--selfcheck"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_f_selfcheck():
    """三方配对：脚本 ↔ 手册处方 ↔ 回归用例——缺任一方都得拦住。

    v5.1.0 起这里多了两方**故意会报错**的输入（回归文件、覆盖盲区）。为什么值得钉：
    这三方正是自增强回路的闸门，闸门自己坏了，"加了检查号"就等于只加了个号。
    """
    print("\n测试 F（--selfcheck）：脚本 ↔ 手册处方 ↔ 回归用例 三方必须配对")
    box = SANDBOX / "box"
    if box.exists():
        shutil.rmtree(box, ignore_errors=True)
    touch(box / "scripts" / "guide_audit.py", SCRIPT.read_text(encoding="utf-8"))
    touch(box / "scripts" / "tests" / "test_guide_audit.py",
          Path(__file__).read_text(encoding="utf-8"))

    # 手册不存在
    p = _selfcheck(box)
    check(p.returncode == 1, "手册不存在 → 退出 1", str(p.returncode))
    check("没人知道怎么修" in p.stdout, "手册缺失时的措辞点明了后果")

    # 手册缺条目：只写前 3 个检查号
    touch(box / "skills" / "项目经理" / "指导手册.md",
          _box_manual(ids=ALL_CHECKS[:3]))
    p = _selfcheck(box)
    check(p.returncode == 1, "手册缺条目 → 退出 1", str(p.returncode))
    check(ALL_CHECKS[3] in p.stdout and ALL_CHECKS[-1] in p.stdout,
          "点名了缺失的检查号", p.stdout)
    check("过短" not in p.stdout, "写足的条目不被误判为过短", p.stdout)

    # 条目齐全但只写占位 → 标为过短
    touch(box / "skills" / "项目经理" / "指导手册.md", _box_manual(body="TODO"))
    p = _selfcheck(box)
    check(p.returncode == 0, "占位手册退出码仍是 0（过短是 WARN 不是 ERROR）", str(p.returncode))
    check("过短" in p.stdout, "占位条目被标为「过短，疑似占位」", p.stdout)

    # 齐全且写足 → 0
    touch(box / "skills" / "项目经理" / "指导手册.md", _box_manual())
    p = _selfcheck(box)
    check(p.returncode == 0 and "配对完整" in p.stdout, "齐全 → 退出 0 且报配对完整", p.stdout)

    # 手册漏解释某条覆盖盲区（脚本正按 COVERAGE_UNCOVERED 输出）→ ERROR
    touch(box / "skills" / "项目经理" / "指导手册.md",
          _box_manual(dims=COVERAGE_DIMS[1:]))
    p = _selfcheck(box)
    check(p.returncode == 1, "手册漏解释覆盖盲区 → 退出 1", str(p.returncode))
    check(COVERAGE_DIMS[0] in p.stdout and "没解释" in p.stdout,
          "点名了未解释的盲区", p.stdout)

    # 回归用例不存在 → ERROR（加了检查号但没人钉得住它）
    touch(box / "skills" / "项目经理" / "指导手册.md", _box_manual())
    (box / "scripts" / "tests" / "test_guide_audit.py").unlink()
    p = _selfcheck(box)
    check(p.returncode == 1 and "回归用例不存在" in p.stdout,
          "回归用例缺失 → 退出 1 并说明后果", p.stdout)

    # 回归用例存在但没提到某个号 → ERROR（弱代理：零出现一定没有断言）
    touch(box / "scripts" / "tests" / "test_guide_audit.py",
          "只看不带任何检查号的用例。\n" * 5)
    p = _selfcheck(box)
    check(p.returncode == 1 and "零出现" in p.stdout,
          "回归用例零提及某检查号 → 退出 1", p.stdout)


def test_g_args():
    print("\n测试 G（参数与退出码）")
    build_dirty(ROOT)
    code, data = audit(ROOT, "--check", "G05")
    check(data is not None and ids(data) == ["G05"], "--check 只留指定检查号",
          str(ids(data) if data else None))
    code, data = audit(ROOT, "--check", "G99")
    check(code == 2, "未知检查号 → 退出 2", str(code))
    p = run(root=SANDBOX / "not_a_workspace")
    check(p.returncode == 2, "非框架工作区 → 退出 2", str(p.returncode))

    env = dict(os.environ, PYTHONIOENCODING="gbk")
    p = run(root=ROOT, env=env)
    check("UnicodeEncodeError" not in (p.stderr or ""), "GBK 控制台不崩", (p.stderr or "")[-400:])
    raw = run_bytes(root=ROOT, env=env)
    out = raw.stdout.decode("gbk", errors="replace")
    check("指导体检" in out and "G05" in out, "GBK 控制台下报告仍可读", out[:200])

    # 卡卷宗大文件阈值：放一个 40KB 文件，阈值调到 20KB 必须被点出来
    big = ROOT / "project-state" / "需求卡" / "card-001" / "证据" / "dump.txt"
    touch(big, "x" * 40000)
    code, data = audit(ROOT, "--max-mb", "0.02")
    check("超过" in msg_of(data, "G07"), "--max-mb 生效（卡卷宗大文件阈值可调）",
          msg_of(data, "G07"))
    code, data = audit(ROOT)
    check("超过" not in msg_of(data, "G07"), "默认 5MB 阈值下同一文件不报", msg_of(data, "G07"))
    big.unlink()

    p = run("--check", root=ROOT)
    check(p.returncode == 2, "--check 缺参数 → 退出 2", str(p.returncode))
    p = run("--max-mb", "abc", root=ROOT)
    check(p.returncode == 2, "--max-mb 非数字 → 退出 2", str(p.returncode))


def set_guidance(base: Path, value):
    """改夹具 config.json 的 guidance。value=None 表示删掉该键。"""
    p = base / "project-state" / "config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if value is None:
        cfg.pop("guidance", None)
    else:
        cfg["guidance"] = value
    touch(p, json.dumps(cfg, ensure_ascii=False, indent=2))


def test_h_guidance_switch():
    """指导模式开关（v5.0.0）：**"关掉"绝不能被读成"没问题"**。

    这一段的价值全在"夹具是脏的"上——样本里十项违规齐全，所以：
    - 开关失效（写了没人读）→ off 时 findings 非空 → 断言失败；
    - 拿"未发现问题"冒充"已关掉" → off 时输出里出现"未发现"字样 → 断言失败。
    一个"永远返回干净"的开关和一个"永远返回 0 ERROR"的检查器一样有害。
    """
    print("\n测试 H（指导模式开关）")
    build_dirty(ROOT)                      # 十项违规齐全：开关一旦失效立刻暴露

    code, data = audit(ROOT, "--check", "G05")
    check(data is not None and data["guidance"] == "on" and code == 1,
          "未声明 guidance → 默认 on（不因缺字段而静默关闭）", str(data and data.get("guidance")))
    set_guidance(ROOT, None)
    code, data = audit(ROOT, "--check", "G05")
    check(code == 1 and ids(data) == ["G05"], "缺 guidance 键时检查照常跑", str(ids(data)))

    set_guidance(ROOT, "off")
    code, data = audit(ROOT, "--check", "G05")
    check(code == 0, "guidance=off → 退出 0（不构成拦截）", str(code))
    check(data is not None and data["guidance"] == "off" and data["skipped"] == "guidance-off",
          "off 时 JSON 显式标 skipped=guidance-off", str(data and data.get("skipped")))
    check(data is not None and data["ok"] is None,
          "off 时 ok=None（既非通过也非失败，防止消费方误读成 passed）",
          str(data and data.get("ok")))
    check(data is not None and data["findings"] == [],
          "off 时确实没跑检查（夹具是脏的，若开关失效这里会非空）", str(len(data["findings"])))
    check(data is not None and any("未做任何检查" in n for n in data["notes"]),
          "off 时报告写明「本次未做任何检查」", str(data and data["notes"])[:200])

    p = run(root=ROOT)
    txt = p.stdout
    check("未做任何检查" in txt, "off 时纯文本报告写明未做检查", txt[:300])
    check("未发现" not in txt,
          "off 时**不得**出现「未发现…」字样（没跑 ≠ 干净）", txt[:300])

    code, data = audit(ROOT, "--check", "G05", "--force")
    check(code == 1 and ids(data) == ["G05"],
          "--force 在 off 下仍跑一次（不改开关）", str(ids(data)))
    check(data is not None and data["guidance"] == "off",
          "--force 不篡改开关状态（报告如实标 off）", str(data and data.get("guidance")))

    for weird in ("false", "0", "关闭", "flase"):
        set_guidance(ROOT, weird)
        code, data = audit(ROOT, "--check", "G05")
        check(code == 1 and data is not None and data["guidance"] == "on",
              "非 off 取值 %r 按 on 处理（拼错不许静默关掉整层指导）" % weird,
              str(data and data.get("guidance")))

    for sloppy in ("OFF", " off ", "Off"):
        set_guidance(ROOT, sloppy)
        code, data = audit(ROOT, "--check", "G05")
        check(code == 0 and data is not None and data["guidance"] == "off",
              "写法 %r 仍算关（大小写/空白不敏感：意图明确就不该不算数）" % sloppy,
              str(data and data.get("guidance")))

    set_guidance(ROOT, "off")
    base = ROOT / "project-state"
    before = snapshot(base)
    run(root=ROOT)
    check(snapshot(base) == before, "off 路径同样只读（不写任何文件）")
    set_guidance(ROOT, "on")


def test_i_coverage():
    """覆盖面声明（v5.1.0）：**结论脱离范围就没有意义**。

    为什么值得一组断言：这一层要拦的坏法是"`ERROR 0` 被读成没问题"——而它恰恰**不会**
    以报错的形式出现（脚本跑得好好的，人把结论读宽了）。所以唯一能钉住它的是**输出内容**：
    范围必须与结论同现、范围必须跟着 `--check` 收窄、`off` 时必须 `covered: []`。
    """
    print("\n测试 I（覆盖面声明）：结论必须带范围——ERROR 0 ≠ 没问题")
    build_dirty(ROOT)
    _, data = audit(ROOT)
    check(data["coverage"]["covered"] == ALL_CHECKS,
          "全量运行：covered = 全部检查号", str(data["coverage"]["covered"]))
    check([u["dimension"] for u in data["coverage"]["uncovered"]] == COVERAGE_DIMS,
          "uncovered 逐条列出盲区（与脚本唯一事实源同源）",
          str(data["coverage"]["uncovered"])[:200])

    _, data = audit(ROOT, "--check", "G05")
    check(data["coverage"]["covered"] == ["G05"],
          "--check G05：covered 只有 G05（范围跟着实际看过的号走）",
          str(data["coverage"]["covered"]))
    check(len(data["coverage"]["uncovered"]) == len(COVERAGE_DIMS),
          "缩范围不缩盲区（只看一个号，盲区也不会少）",
          str(len(data["coverage"]["uncovered"])))

    txt = run(root=ROOT).stdout
    check("覆盖面与盲区" in txt and "不覆盖" in txt, "终端输出带覆盖面声明")
    check(all(d in txt for d in COVERAGE_DIMS), "终端逐条念了盲区",
          [d for d in COVERAGE_DIMS if d not in txt])
    check("这 %d 类**已知**" % len(ALL_CHECKS) in txt,
          "声明写的是「这 N 类已知」，而不是笼统的干净", txt[-600:])
    check("「环境没问题」「没有绕过」**不成立**" in txt,
          "声明显式否掉了超出范围的读法（范围外的话不许说）", txt[-600:])

    txt5 = run("--check", "G05", root=ROOT).stdout
    check("G05 这 1 类" in txt5, "--check G05 的声明收窄到 G05",
          txt5[-400:])
    check("这 %d 类" % len(ALL_CHECKS) not in txt5,
          "只看一个号时**不得**声称全范围（narrowed run 不许冒充全量）", txt5[-400:])

    clean = SANDBOX / "coverage_clean"
    if clean.exists():
        shutil.rmtree(clean, ignore_errors=True)
    shutil.copytree(ROOT, clean, ignore=shutil.ignore_patterns("__pycache__"))
    clean_all(clean)
    txt = run(root=clean).stdout
    check("未发现" in txt and "范围见下方覆盖声明" in txt,
          "无发现项时「未发现」同句给出范围（不再光秃秃地说未发现）", txt[:400])

    # 关掉指导模式时：一行都没查，covered 必须是空的——最容易被误读成"通过"的状态
    set_guidance(clean, "off")
    _, data = audit(clean)
    check(data["coverage"]["covered"] == [] and data["ok"] is None,
          "guidance=off：covered 为空数组（没检查 ≠ 检查过）",
          str(data["coverage"]["covered"]))
    check([u["dimension"] for u in data["coverage"]["uncovered"]] == COVERAGE_DIMS,
          "off 时盲区照列（被关掉只会让未覆盖的更多，不会更少）")


def test_j_ledger():
    """G11 台账（v5.1.0）：三种状态三种行为——缺失提醒、有坏账报错、空表说明白。"""
    print("\n测试 J（G11 台账）：零归属/假登记是 ERROR，台账缺失只提醒")
    base = SANDBOX / "ledger_missing"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    build_dirty(base)
    ledger = base / "project-state" / "记忆" / "指导发现台账.md"
    ledger.unlink()
    code, data = audit(base)
    check("G11" in ids(data, "WARN") and "G11" not in ids(data, "ERROR"),
          "台账缺失 → 只 WARN（回路没入口，不是写假账）", str(ids(data)))
    check("自增强回路没有入口" in msg_of(data, "G11") and "没有台账 ≠ 没有绕过" in msg_of(data, "G11"),
          "台账缺失的措辞点明「没有台账 ≠ 没有绕过」与补建命令", msg_of(data, "G11"))

    quick = SANDBOX / "ledger_quick"
    if quick.exists():
        shutil.rmtree(quick, ignore_errors=True)
    shutil.copytree(base, quick, ignore=shutil.ignore_patterns("__pycache__"))
    cfgp = quick / "project-state" / "config.json"
    cfg = json.loads(cfgp.read_text(encoding="utf-8"))
    cfg["mode"] = "quick"
    touch(cfgp, json.dumps(cfg, ensure_ascii=False, indent=2))
    _, data = audit(quick)
    check("G11" not in ids(data), "快速模式：台账缺失降为 INFO（不再占一个 WARN）", str(ids(data)))
    check(any("G11" in n and "流程类只提醒" in n for n in data["notes"]),
          "快速模式的提醒里写明了为什么只提醒", str(data["notes"])[:300])

    empty = SANDBOX / "ledger_empty"
    if empty.exists():
        shutil.rmtree(empty, ignore_errors=True)
    shutil.copytree(base, empty, ignore=shutil.ignore_patterns("__pycache__"))
    touch(empty / "project-state" / "记忆" / "指导发现台账.md", LEDGER_HEAD)
    _, data = audit(empty)
    check("G11" not in ids(data) and any("不等于没有绕过" in n for n in data["notes"]),
          "台账在但无登记行 → INFO，且写明「这不等于没有绕过」", str(data["notes"])[:300])


def _set_mode(base: Path, mode: str):
    """把样本的 config.json 改成指定运行模式（v5.4.0 T-K 用）。"""
    cfgp = base / "project-state" / "config.json"
    cfg = json.loads(cfgp.read_text(encoding="utf-8"))
    cfg["mode"] = mode
    touch(cfgp, json.dumps(cfg, ensure_ascii=False, indent=2))


def _levels(data):
    """{检查号: {级别, …}}——按号比级别，比只比 ERROR 更能抓住"降噪扩到了别处"。"""
    out = {}
    for f in data["findings"]:
        out.setdefault(f["check"], set()).add(f["level"])
    return out


def test_k_quick_keeps_mechanisms():
    """快速模式只裁角色数/文档深度/测试密度，**机制一样不裁**（v5.4.0）。

    「快速模式只是激发少量角色而已，需求卡什么的还是要构建的」这句话要有机器证据，
    故分两半，缺一半都不算数：

    ① **不裁**：同一份"每项都违规"的样本，`mode=full` 与 `mode=quick` 的 **ERROR 集合
       逐字相同**。只验这一半会漏掉"降噪压根没生效 = 承诺的差别是假的"。
    ② **降噪是真的、且很窄**：塞一条 `<占位>` 契约引用，`full` 抓到（WARN）、`quick` 放过
       （静默）。只验这一半会漏掉"降噪扩到了不该扩的地方"（比如把"未建卡"也放过）。
    """
    print("\n测试 K（快速模式裁剪面）：mode=quick 不得改变 ERROR 集合")
    base = SANDBOX / "quick_vs_full"
    # **自建脏样本，不抄 ROOT**：`test_b_mutation()` 会把 ROOT 逐项修好，
    # 抄 ROOT 等于拿"已经没问题的样本"去证"坏样本照旧被抓"，断言会假失败。
    build_dirty(base)

    # 给一张已开工的卡塞一条 <占位> 契约引用——这是快速模式**允许**放过的那一项
    card = base / "project-state" / "需求卡" / "card-002" / "card.md"
    with card.open("a", encoding="utf-8") as fh:
        fh.write("\n关联契约：`project-state/契约/<契约名>.md`\n")

    _set_mode(base, "quick")
    cq, dq = audit(base)
    _set_mode(base, "full")
    cf, df = audit(base)
    if dq is None or df is None:
        check(False, "快速/完整两份样本都能跑出 --json", "输出无法解析")
        return

    eq, ef = set(ids(dq, "ERROR")), set(ids(df, "ERROR"))
    check(eq == ef, "两种模式的 ERROR 检查号集合逐字相同（快速模式不裁机制）",
          f"quick 多出 {sorted(eq - ef)} / full 多出 {sorted(ef - eq)}")

    # **按「检查号 → 级别集合」逐号比对**（比只比 ERROR 更强）：唯一允许出现差别的是 G02
    # ——full 多一个 WARN（占位契约引用），quick 放过它。其余检查号的级别一个都不许变。
    lvq, lvf = _levels(dq), _levels(df)
    diff = sorted(cid for cid in set(lvq) | set(lvf) if lvq.get(cid) != lvf.get(cid))
    check(diff == ["G02"], "级别集合的差别只有 G02（降噪没有扩到第二个检查号）", f"实际差别 {diff}")
    check("WARN" in lvf.get("G02", set()) and "WARN" not in lvq.get("G02", set()),
          "差别就在这一处：full 记 WARN、quick 静默", f"quick={sorted(lvq.get('G02') or [])}")

    # 用户点名的那件事：有了开工痕迹却没有卡 = G01，**快速模式下照样是 ERROR**
    check("ERROR" in lvq.get("G01", set()),
          "快速模式下 G01（未建卡 / 卡引用悬空）仍是 ERROR——「需求卡照建」不是口头承诺",
          str(sorted(lvq.get("G01") or [])))
    for cid, what in (("G04", "待澄清未清零就开工"), ("G06", "未批准就执行"),
                      ("G08", "置 done 无对应产出"), ("G09", "验收记录空壳")):
        check("ERROR" in lvq.get(cid, set()), f"快速模式下 {cid}（{what}）仍是 ERROR",
              str(sorted(lvq.get(cid) or [])))
    # 顺带钉住一件事：**级别不因模式而变**（G03/G07/G10 在本样本里本就是 WARN——那是
    # 脚本按"做错了能不能撤销"定的档，不是被快速模式削掉的）；"不是 ERROR"≠"没被抓到"。
    for cid in ("G03", "G07", "G10"):
        check(cid in lvq and lvq.get(cid) == lvf.get(cid),
              f"{cid} 在两种模式下级别一致（WARN 也是被抓到，只是档位不同）",
              f"quick={sorted(lvq.get(cid) or [])} full={sorted(lvf.get(cid) or [])}")

    check("契约引用未填实" in msg_of(df, "G02"),
          "完整模式下占位契约引用被记为 WARN（G02 本身有效）", msg_of(df, "G02")[:160])
    check("契约引用未填实" not in msg_of(dq, "G02"),
          "快速模式下该占位被降噪——这是快速模式允许放过的两项之一（另一项见 T-J）",
          msg_of(dq, "G02")[:160])
    check("不存在的契约.md" in msg_of(dq, "G02"),
          "降噪很窄：缺文件（非占位）在快速模式下照样是 ERROR", msg_of(dq, "G02")[:160])
    check(cq == 1 and cf == 1, "两种模式退出码都是 1（有 ERROR 就是 1）", f"quick={cq} full={cf}")


def test_l_ledger_data_table():
    """G11 的发现号形态闸门（F-41）：正文里的**数据表**不得被读成发现行。

    实测形态（2026-09-19 真事）：`F-40` 处置节里一张 6 列 CSS 像素实测表
    （`全角字数 / clientWidth / scrollWidth / main 宽 / size 宽 / 判定`）凑够了 6 个单元格，
    旧版 `check_g11` 只按「以 `|` 开头且 ≥6 单元格」判定，于是把它读成 5 条发现——
    编号被读成 `4`/`16`/`32`/`64`/短名 `a.png`，归属栏读到 `24.1` ⇒ 产出 5 条 ERROR + 5 条 WARN，
    **全部与台账真实内容无关**。本组同时钉住两面：数据表必须被放过，真坏行必须仍被抓到。
    """
    print("\n测试 L（G11 数据表闸门）：正文数据表不算发现行，真坏行照抓")
    base = SANDBOX / "ledger_table"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    build_dirty(base)
    ledger = base / "project-state" / "记忆" / "指导发现台账.md"

    # ① 一张 6 列**数据表** + ② 一条**真坏行**（零归属），同存一份台账
    data_table = (
        "\n### 处置节里的实测数据表（编号栏是字数，不是发现号）\n\n"
        "| 全角字数 | name.clientWidth | name.scrollWidth | main 宽 | size 宽 | 判定 |\n"
        "|---|---|---|---|---|---|\n"
        "| 4 | 322 | 322 | 321.9 | 24.1 | 未触发省略（基线） |\n"
        "| 16 | 322 | 322 | 321.9 | 24.1 | 未触发省略（基线） |\n"
        "| 32 | 322 | 451 | 321.9 | 24.1 | 省略生效 |\n"
        "| 64 | 322 | 851 | 321.9 | 24.1 | 省略生效 |\n"
        "| 短名 `a.png` | 322 | 322 | 321.9 | 24.1 | 负控 |\n"
    )
    real_bad_row = LEDGER_ROW.format(no="FIND-999", date="2026-09-20",
                                     scene="构造的零归属坏行", catch="否",
                                     owner="", state="待办")
    touch(ledger, ledger.read_text(encoding="utf-8") + data_table + real_bad_row)
    _, data = audit(base)
    g11 = msg_of(data, "G11")

    # 数据表必须**整表**放过：旧版那 5 个假编号一个都不许再出现在报错里
    for decoy in ("4", "16", "32", "64", "a.png"):
        check(("发现 %s " % decoy) not in g11 and ("台账 %s " % decoy) not in g11,
              "数据表行（编号栏 %s）不再被读成发现行" % decoy, g11)
    check("321.9" not in g11, "数据表里的几何读数（如 321.9）不再被读成「能否抓到」列", g11)
    # 但形态合法的真坏行必须**照抓**——闸门不能把真发现一起放过
    check("FIND-999" in g11 and "零归属" in g11,
          "同一份台账里形态合法的真坏行仍被 G11 抓到（闸门不是把 G11 关掉）", g11)
    check(any("按数据表跳过" in n for n in data["notes"]),
          "被跳过的表格行留下 INFO 痕迹（静默跳过 = 新盲区）", str(data["notes"])[:400])


def test_m_cr_section_shape():
    """G06 的**章节形状**闸门（F-42），两面各钉一次。

    F-42 第一面：定位靠**章节字母**（模板写的是 `## G. 审批栏` / `## H. 执行留痕栏`）。
    单证中途插入章节会把这两节顺延成 H/I，正则按字母找就扑空 ⇒ 一个**已签**的单被
    报成「未签」——判对的读数、走错的判定路径。

    F-42 第二面：`section_of()` 找不到章节时返回空串，「**章节不存在**」和
    「**章节存在但没签**」于是产出同一句话，两种事实读数同形。

    本组用五张单证把这两面同时钉住：字母错位的已签单必须被读成已签；而「缺章节」
    与「确未签」必须产出**不同**的读数，且缺章节+已执行仍保留 ERROR 的严重性。
    """
    print("\n测试 M（G06 章节形状闸门 · F-42）：字母错位仍可读，缺章节与未签读数分形")
    base = SANDBOX / "cr_section_shape"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    build_dirty(base)
    crdir = base / "project-state" / "问题" / "变更申请单"

    signed_tbl = ("| 主控（放行） | 同意 | 老王 | 2026-01-01 10:00 | 探针 |\n")
    unsigned_tbl = "| 主控（放行） | 同意 | | | |\n"
    trace = "- 执行人：运维：张三（已执行）\n"

    def cr(no, title, body):
        touch(crdir / f"CR-{no}-{title}.md", f"# 变更申请单 CR-{no}\n\n## A. 基本信息\n\n{body}")

    # ① 缺审批栏章节、也没执行 → 缺章节（WARN）
    cr("20260101-101", "缺章节", "无审批栏、无留痕。\n")
    # ② 缺审批栏章节、但已执行 → 无从证明已批准（ERROR，严重性必须保留）
    cr("20260101-102", "缺章节已执行", "## H. 执行留痕栏\n" + trace)
    # ③ 审批栏在、但确未签 → 未签（WARN，与「缺章节」不同的读数）
    cr("20260101-103", "确未签", "## G. 审批栏\n| 角色 | 结论 | 署名 | 时间 | 备注 |\n"
                                "|---|---|---|---|---|\n" + unsigned_tbl)
    # ④ 模板字母（G/H）已签 + 有留痕 → 不得报任何 G06
    cr("20260101-104", "模板字母已签", "## G. 审批栏\n| 角色 | 结论 | 署名 | 时间 | 备注 |\n"
                                     "|---|---|---|---|---|\n" + signed_tbl
                                     + "\n## H. 执行留痕栏\n" + trace)
    # ⑤ **字母错位**（章节被顺延成 H/I）已签 + 有留痕 → 同样不得报任何 G06（第一面）
    cr("20260101-105", "字母错位已签", "## H. 审批栏\n| 角色 | 结论 | 署名 | 时间 | 备注 |\n"
                                     "|---|---|---|---|---|\n" + signed_tbl
                                     + "\n## I. 执行留痕栏\n" + trace)

    _, data = audit(base)
    # 只取落在本组这五张单上的 G06 发现，免得被 build_dirty 自带那张单干扰。
    # 标记用**完整单号前缀**（`CR-20260101-101-`）：光用 `101` 会命中自带单
    # `CR-20260101-001-…`（`2026**0101**` 里就含 `101`），那会把别人的发现算进来。
    nos = ("CR-20260101-101-", "CR-20260101-102-", "CR-20260101-103-",
           "CR-20260101-104-", "CR-20260101-105-")
    by_no = {}
    for f in data["findings"]:
        if f["check"] != "G06":
            continue
        for no in nos:
            if no in (f.get("where") or ""):
                by_no.setdefault(no[12:15], []).append(f)

    def msg(no):
        return " ".join(f["message"] for f in by_no.get(no, []))

    def lv(no):
        return sorted(f["level"] for f in by_no.get(no, []))

    check(lv("101") == ["WARN"] and "缺「审批栏」章节" in msg("101"),
          "① 缺审批栏章节且未执行 ⇒ 单条 WARN，文案点明「缺章节」", str(by_no.get("101"))[:300])
    check(lv("102") == ["ERROR"],
          "② 缺审批栏章节**但已执行** ⇒ 仍是 ERROR（缺任一不得执行，且无从证明已批准）",
          str(by_no.get("102"))[:300])
    check(lv("103") == ["WARN"] and "未签" in msg("103") and "缺「审批栏」章节" not in msg("103"),
          "③ 审批栏在而确未签 ⇒ 文案是「未签」，**不是**「缺章节」——两种事实读数必须分形",
          str(by_no.get("103"))[:300])
    check(lv("101") != lv("103") or msg("101") != msg("103"),
          "「缺章节」与「确未签」的读数不得同形（第二面）",
          "101=%s / 103=%s" % (msg("101"), msg("103")))
    check(by_no.get("104") == None,
          "④ 模板字母（G/H）已签 + 有留痕 ⇒ 无任何 G06 发现", str(by_no.get("104"))[:300])
    check(by_no.get("105") == None,
          "⑤ **字母错位**（H/I）已签 + 有留痕 ⇒ 无任何 G06 发现（第一面：按标题找、字母可错位）",
          str(by_no.get("105"))[:300])


def main():
    print("=" * 72)
    print(f"guide_audit 回归测试（脚本：{SCRIPT}）")
    print("=" * 72)
    test_a_positive()
    test_b_mutation()
    test_c_not_started()
    test_d_profile_guard()
    test_e_readonly()
    test_f_selfcheck()
    test_g_args()
    test_h_guidance_switch()
    test_i_coverage()
    test_j_ledger()
    test_k_quick_keeps_mechanisms()
    test_l_ledger_data_table()
    test_m_cr_section_shape()
    print("\n" + "=" * 72)
    print(f"结果：{len(passes)} 通过 / {len(fails)} 失败")
    for f in fails:
        print(f"  FAIL -> {f}")
    print("=" * 72)
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        os.chdir(HERE)
        shutil.rmtree(ROOT, ignore_errors=True)
        shutil.rmtree(SANDBOX, ignore_errors=True)
    sys.exit(code)
