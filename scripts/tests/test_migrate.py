#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate.py 的回归测试（v4.9.0 补）。

**为什么这个测试必须存在**：`migrate.py` 是唯一会**改真实项目文件**的框架工具。一个只在
dry-run 下验证过的迁移器，等于把"会不会覆盖真清单"这个问题留到别人项目上第一次 --apply 时才回答。
所以本测试的重心不是"迁移能跑通"，而是**数据安全**：

- **dry-run 一个字节都不许改**（含不建备份目录）——否则"先看看计划"就可能已经动了现场；
- **--apply 后 agent.md 的清单正文必须逐字还在**（改动位置可以，改一个字符都不行）——
  这条是防"迁移"变"丢失"的唯一硬证据；
- **空小节一律不动**：拆完留的是同名指针小节（0 条编号项），写入侧若按标题匹配就会把指针当清单
  写回目标文件、**覆盖掉真清单**。这条守卫分两层验：detect 判 DONE 时 apply 压根不会被调（M17/M18
  验的是这一层的幂等），以及 **detect 万一判错**时写入侧自己必须拒绝（M24/M25 把 apply_split 真正
  调到，验的是这一层）。只验第一层是不够的——把守卫删掉，第一层照样全绿。
- **已迁移的树再跑一次 = 什么都不做**（幂等）。

用法：python scripts/tests/test_migrate.py
      MG_SCRIPT=<别的路径> 可测别处的副本。
退出码 0=全过；1=有失败。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parents[1]
SCRIPT = Path(os.environ.get("MG_SCRIPT") or (FRAMEWORK / "scripts" / "migrate.py"))
ROOT = Path(tempfile.mkdtemp(prefix="mg_test_"))

fails: list = []
passes: list = []


def check(cond, label, detail=""):
    (passes if cond else fails).append(label)
    tail = f"  -- {detail}" if detail and not cond else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{tail}")


# ---------------------------------------------------------------- 沙箱

# 四条编号项 + 两条编号项：条数是断言的一部分（拆过去之后必须仍是 4 / 2 条）
AGENT_MD = """---
name: "测试框架"
description: "沙箱"
version: "1.0.0"
---

# 测试框架

## 验收标准

1. 甲条
2. 乙条
3. 丙条
4. 丁条

## 注意事项

1. 戊条
2. 己条

## 任务执行纪律

1. 庚条
"""

# 旧结构：没有 schema_version（migrate 的 config-schema 步骤就是为它准备的）
CONFIG_OLD = {
    "_comment_schema_version": "沙箱：故意不给 schema_version，模拟旧项目",
    "project_name": "沙箱项目",
    "paths": {
        "_comment": "沙箱",
        "project_state": "project-state",
        "layout": {"开发记忆区": "project-state"},
        "根文件": {"framework": "agent.md  # 不能移动"},
    },
}


def w(relpath, text):
    p = ROOT / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def build(config=None):
    if ROOT.exists():
        shutil.rmtree(ROOT, ignore_errors=True)
    w("agent.md", AGENT_MD)
    w("project-state/config.json",
      json.dumps(config if config is not None else CONFIG_OLD, ensure_ascii=False, indent=2))
    w("project-state/协作规范.md", "# 协作规范\n")


def run(*extra):
    return subprocess.run([sys.executable, str(SCRIPT), str(ROOT), *extra],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def snapshot():
    """树里所有文件的字节 → 用于"一个字节都没改"的断言。"""
    out = {}
    for p in sorted(ROOT.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(ROOT))] = p.read_bytes()
    return out


def numbered(text, label="验收标准"):
    """取某小节里的编号项文本（去掉 `1. ` 前缀），用于逐字比对。"""
    m = re.search(r"^##\s*%s\s*$(.*?)(?=^##\s|\Z)" % label, text, re.M | re.S)
    if not m:
        return []
    return [re.sub(r"^\d+\.\s*", "", l) for l in m.group(1).splitlines() if re.match(r"^\d+\.\s", l)]


# ---------------------------------------------------------------- 用例

print("基线：dry-run 不改动任何文件")

build()
before = snapshot()
r = run()
after = snapshot()
check(before == after, "M1 dry-run 一个字节都没改（含未建备份目录）",
      "新增/变化：%s" % sorted(set(after) ^ set(before))[:5])
check("迁移计划" in r.stdout and "未改动任何文件" in r.stdout,
      "M2 dry-run 自称「未改动任何文件」", r.stdout[-300:])
check("schema_version" in r.stdout, "M3 dry-run 报出 config-schema 待迁移", r.stdout[-400:])
check(r.returncode == 1, "M4 有待迁移项 → 退出码 1（可被 CI 判为未迁移完）", "rc=%d" % r.returncode)

print("config-schema：加字段但不动原有内容")

build()
cfg_before = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
r = run("--only", "config-schema", "--apply")
cfg_after = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
check(cfg_after.get("schema_version") is not None, "M5 --apply 后 schema_version 已补上", str(cfg_after))
check(all(cfg_after.get(k) == v for k, v in cfg_before.items()),
      "M6 原有键与值逐一未变（只插了 schema_version）",
      "前=%s\n后=%s" % (cfg_before, cfg_after))
check("schema_version" not in cfg_after.get("paths", {}),
      "M7 schema_version 插在顶层（不在 paths 里）", str(list(cfg_after)))
check(any(p.name.startswith("migrate_backup_") for p in (ROOT / "tmp").glob("*"))
      if (ROOT / "tmp").is_dir() else False,
      "M8 --apply 生成备份目录（可回退）", str(list((ROOT / "tmp").glob("*"))))

print("幂等：已迁移的树再跑一次 = 什么都不做")

build()
run("--only", "config-schema", "--apply")
after_first = snapshot()
r = run("--only", "config-schema", "--apply")
after_second = snapshot()
check(after_first == after_second, "M9 第二次 --apply 未改任何文件（幂等）",
      "变化：%s" % sorted(k for k in set(after_first) | set(after_second)
                          if after_first.get(k) != after_second.get(k))[:5])
check("已到位" in r.stdout, "M10 第二次报「已到位」", r.stdout[-300:])

print("split-checklists：清单搬家，正文逐字不许变")

build()
ac_before = numbered(AGENT_MD, "验收标准")
note_before = numbered(AGENT_MD, "注意事项")
r = run("--only", "split-checklists", "--apply")
agent_after = (ROOT / "agent.md").read_text(encoding="utf-8")
ac_file = ROOT / "project-state/验收标准.md"
note_file = ROOT / "project-state/注意事项.md"
check(ac_file.is_file() and note_file.is_file(), "M11 两份清单文件已生成",
      str(sorted(p.name for p in (ROOT / "project-state").iterdir())))
ac_after = numbered(ac_file.read_text(encoding="utf-8") if ac_file.is_file() else "", "验收标准")
note_after = numbered(note_file.read_text(encoding="utf-8") if note_file.is_file() else "", "注意事项")
check(ac_after == ac_before, "M12 验收标准正文逐字搬过去、条数不变（%d 条）" % len(ac_before),
      "前=%s\n后=%s" % (ac_before, ac_after))
check(note_after == note_before, "M13 注意事项正文逐字搬过去、条数不变（%d 条）" % len(note_before),
      "前=%s\n后=%s" % (note_before, note_after))
check(numbered(agent_after, "验收标准") == [],
      "M14 agent.md 该小节已无编号项（变成指针）", repr(numbered(agent_after, "验收标准")))
check("验收标准.md" in agent_after and "注意事项.md" in agent_after,
      "M15 agent.md 指针指向两份契约文件", agent_after[:200])
check(numbered(agent_after, "任务执行纪律") == ["庚条"],
      "M16 不在迁移清单里的小节原样未动（任务执行纪律）",
      repr(numbered(agent_after, "任务执行纪律")))

print("幂等：拆完再跑一次，真清单不会被指针覆盖")

# 拆完之后 agent.md 留的是**同名指针小节**（0 条编号项）。第二次 --apply 时 detect 判 DONE
# （小节已空 + 目标文件在）→ **apply_split 根本不会被调用**，所以本组验的是"状态判定的幂等"，
# 不是写入侧守卫（实测：把守卫整段删掉，M17/M18 依旧全绿）。守卫的真用例是下面的 M24/M25。
build()
run("--only", "split-checklists", "--apply")
ac_file_first = (ROOT / "project-state/验收标准.md").read_bytes()
r = run("--only", "split-checklists", "--apply")
ac_file_second = (ROOT / "project-state/验收标准.md").read_bytes()
check(ac_file_first == ac_file_second,
      "M17 第二次 --apply 未覆盖真清单",
      "第一次 %d 字节 → 第二次 %d 字节" % (len(ac_file_first), len(ac_file_second)))
check(numbered(ac_file_second.decode("utf-8"), "验收标准") == ac_before,
      "M18 两次之后清单仍是原来的 %d 条（没被指针覆盖）" % len(ac_before),
      repr(numbered(ac_file_second.decode("utf-8"), "验收标准")))

print("数据安全：写入侧自己拒绝空小节（detect 判错时也不许凭空造/覆盖）")

# **本组才是守卫的真用例**：构造"detect 判 TODO、小节在、但里面 0 条编号项"的现场。
# 关键在**保留 `## 验收标准` 标题**、只清空正文——标题在 → `_find_section` 找得到 →
# 不会走"小节不存在"那条 continue；0 条编号项 → 只有写入侧守卫挡得住。
# （写成"整段删掉"是测不到守卫的：那会被"找不到小节"提前 continue 掉，删掉守卫照样绿。）
build()
w("agent.md", AGENT_MD.replace("## 验收标准\n\n1. 甲条\n2. 乙条\n3. 丙条\n4. 丁条\n\n",
                              "## 验收标准\n\n> 指针小节（0 条编号项）。\n\n"))
r = run("--only", "split-checklists", "--apply")
ac_path = ROOT / "project-state/验收标准.md"
check(not ac_path.exists(),
      "M24 小节在但 0 条编号项 → 写入侧拒绝，不凭空造空清单",
      "文件却出现了：%s" % (ac_path.read_text(encoding="utf-8")[:200] if ac_path.exists() else ""))
check((ROOT / "project-state/注意事项.md").is_file(),
      "M25 同一棵树里真有编号项的另一份仍正常拆出（拒绝只针对空小节，不是整步罢工）",
      str(sorted(p.name for p in (ROOT / "project-state").iterdir())))

print("清单丢失探测：两边都没有 → 必须报出来，不许静默通过")

build()
w("agent.md", AGENT_MD.replace("## 验收标准\n\n1. 甲条\n2. 乙条\n3. 丙条\n4. 丁条\n\n", ""))
r = run()
check("清单丢了" in r.stdout, "M19 agent.md 与契约文件都没有清单 → 报「清单丢了」", r.stdout[-500:])

print("退出码与健壮性")

build()
r = run("--list")
check("config-schema" in r.stdout and r.returncode == 0, "M20 --list 列出步骤且退出码 0", r.stdout[:200])

r = subprocess.run([sys.executable, str(SCRIPT), str(ROOT.parent)],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check(r.returncode == 2, "M21 非框架根 → 退出码 2", "rc=%d %s" % (r.returncode, r.stderr[-200:]))

build()
r = run("--only", "不存在的步骤", "--apply")
check(r.returncode in (0, 1), "M22 未知步骤 id 不崩（无匹配即空跑）", "rc=%d" % r.returncode)

# GBK 控制台：中文输出不能因编码崩掉
env = dict(os.environ, PYTHONIOENCODING="gbk")
build()
r = subprocess.run([sys.executable, str(SCRIPT), str(ROOT)],
                   capture_output=True, text=True, encoding="gbk", errors="replace", env=env)
check("Traceback" not in (r.stderr or ""), "M23 GBK 控制台下不 traceback", (r.stderr or "")[-300:])

print("指导模式开关迁移（v5.0.0）：补字段，但不许替用户把显式声明的 off 掀回 on")

build()
r = run("--only", "guidance")
check("[待迁移] guidance " in r.stdout,
      "M26 旧项目缺 guidance → 该步骤报「待迁移」", r.stdout[-400:])

cfg_before = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
run("--only", "guidance", "--apply")
cfg_after = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
check(cfg_after.get("guidance") == "on", "M27 --apply 后补上 guidance=on（默认开）",
      str(cfg_after.get("guidance")))
check("_comment_guidance" in cfg_after,
      "M28 同时写入可读说明（配置项自带解释，不靠人去翻文档）", str(sorted(cfg_after)))
check(all(cfg_after.get(k) == v for k, v in cfg_before.items()),
      "M29 原有键的值一个没动（迁移只做加法）",
      str(sorted(set(cfg_before) - set(cfg_after))))

snap_first = snapshot()
run("--only", "guidance", "--apply")
check(snapshot() == snap_first, "M30 第二次 --apply 幂等（不再写文件）",
      str(sorted(set(snapshot()) ^ set(snap_first))[:5]))

# 关键安全属性：迁移是"补缺"，不是"改成我要的样子"。
build(dict(CONFIG_OLD, guidance="off"))
run("--only", "guidance", "--apply")
cfg_after = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
check(cfg_after.get("guidance") == "off",
      "M31 已显式声明 off → 迁移**不得**改成 on（迁移不替用户打开指导模式）",
      str(cfg_after.get("guidance")))

build(dict(CONFIG_OLD, guidance="false"))
r = run("--only", "guidance")
check("需人工" in r.stdout and "false" in r.stdout,
      "M32 取值不认识 → 报「需人工」并回显原值", r.stdout[-400:])
run("--only", "guidance", "--apply")
cfg_after = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
check(cfg_after.get("guidance") == "false",
      "M33 需人工的取值 --apply 不得覆盖（人没看，就不许动）", str(cfg_after.get("guidance")))

print("指导发现台账迁移（v5.1.0）：补文件，但**只建不改**——台账里是人写的东西")

build()
LEDGER = ROOT / "project-state/记忆/指导发现台账.md"
r = run("--only", "guidance-ledger")
# 断言必须落在**该步骤那一行**上：末尾的统计行（「待迁移 N 项」）恒定包含这个词，
# 拿它当判据就永远测不出错——一个不会红的断言和一个永远返回 0 的检查器一样有害。
check("[待迁移] guidance-ledger" in r.stdout,
      "M34 旧项目缺台账 → 该步骤报「待迁移」", r.stdout[-500:])
check(not LEDGER.exists(), "M35 dry-run 不建文件（默认只报告）")

run("--only", "guidance-ledger", "--apply")
check(LEDGER.is_file(), "M36 --apply 后台账就位")
body = LEDGER.read_text(encoding="utf-8")
check("| 编号 |" in body and "归属" in body,
      "M37 台账带七列表头（G11 按表头解析，列名不能少）", body[:400])
hdr = [l for l in body.splitlines() if l.startswith("| 编号 |")]
check(len(hdr) == 1 and len(hdr[0].strip().strip("|").split("|")) == 7,
      "M37b 表头正好 7 列——模板少一列就是在教人少填一列（v5.24.0 起七列；G11 对老台账的六列仍兼容）",
      str(hdr))
check("触发原话" in body and "摩擦类型" in body,
      "M37c 七列含「触发原话」与「摩擦类型」（v5.24.0：改写原话等于替用户做结论；空着 = 草稿未转正）",
      body[:900])
check("绕过" in body and "验收太晚" in body,
      "M37d 摩擦类型五选一写进表头说明（每类只对应一种固定改法，不是自由发挥）", body[:900])
check("CR-YYYYMMDD-NNN" in body and "path_audit.py" in body,
      "M38 表头说明写清三类归属，且要求**真实存在**（避免填了等于没填）", body[:900])
check("注意事项 43" in body,
      "M39 台账指向纪律条款（表格不解释规则，只给指针）", body[:900])

snap_first = snapshot()
run("--only", "guidance-ledger", "--apply")
check(snapshot() == snap_first, "M40 第二次 --apply 幂等（不再写文件）",
      str(sorted(set(snapshot()) ^ set(snap_first))[:5]))

# 数据安全属性：台账是**人写的记录**。迁移若会覆盖它，等于把用户的登记抹掉——
# 比不迁移坏得多（同 v4.4.0「假保护」的教训：文档承诺的硬保护必须落到代码里）。
# 变异证据（2026-09-15 实测）：只把 detect 改成 TODO（骗过状态层）时 M42 仍然通过
# → 顶住文件的是**写入侧那道 if os.path.isfile 守卫**，不是状态判断；把守卫一起拆掉
# 才红。这正是 注意事项 40 说的「写入侧必须自己拒绝动手，不能只靠状态判断」。
mine = "# 指导发现台账\n\n> 我自己写的说明。\n\n| 编号 | 发现日期 | 场景（谁发现的、怎么发现的） | 现有检查能否抓到 | 归属 | 状态 |\n" \
       "|---|---|---|---|---|---|\n| FIND-001 | 2026-09-15 | 我发现的坑 | 否 | G05 | 待办 |\n"
LEDGER.write_text(mine, encoding="utf-8")
r = run("--only", "guidance-ledger")
check("[已到位] guidance-ledger" in r.stdout,
      "M41 台账已存在 → 该步骤报「已到位」而不是「待迁移」", r.stdout[-500:])
check("[待迁移] guidance-ledger" not in r.stdout,
      "M41b 已存在时不得同时出现在待迁移清单里（避免「再补一遍」的误读）", r.stdout[-500:])
run("--only", "guidance-ledger", "--apply")
check(LEDGER.read_text(encoding="utf-8") == mine,
      "M42 已存在的台账一个字节都不动（只建不改：覆盖它 = 抹掉用户的登记）")

print("运行模式显式化（v5.3.0）：默认 quick；full 要有入口凭据，但**工具不替用户改档**")

build()
r = run("--only", "mode-explicit")
check("[todo] mode-explicit" in r.stdout.lower() or u"[待迁移] mode-explicit" in r.stdout,
      "M43 旧项目未声明 mode → 该步骤报「待迁移」", r.stdout[-500:])
r = run("--only", "mode-explicit", "--apply")
cfg_after = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
check(cfg_after.get("mode") == "quick",
      "M44 未声明时补的是 quick（v5.3.0 默认档——不是 full）",
      str(cfg_after.get("mode")))
check("_comment_mode" in cfg_after,
      "M45 同时写入可读说明（两个入口 + 不许静默切档）",
      str(sorted(cfg_after))[:200])
check(all(cfg_after.get(k) == v for k, v in CONFIG_OLD.items() if k != "_comment_schema_version"),
      "M46 原有键的值一个没动（迁移只做加法）",
      str(sorted(set(cfg_after) - set(CONFIG_OLD))))

cfg_before = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
run("--only", "mode-explicit", "--apply")
cfg_again = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
check(cfg_again == cfg_before, "M47 第二次 --apply 幂等（不再写文件）",
      str(sorted(set(cfg_again) ^ set(cfg_before))[:5]))

# 关键安全属性①：mode=full 本身不是错（切档是一等公民），
# 错的是「没有入口凭据」——而凭据缺失时工具必须停手交人工，
# **不能自己把档降下来**（那是用工具替用户做决定）。
build(dict(CONFIG_OLD, mode="full"))
r = run("--only", "mode-explicit")
check(u"[需人工] mode-explicit" in r.stdout,
      "M48 mode=full 且无凭据 → 报「需人工」（不当错误报，也不放过）", r.stdout[-500:])
r = run("--only", "mode-explicit", "--apply")
cfg_after = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
check(cfg_after.get("mode") == "full",
      "M49 需人工时 --apply 不得把 full 降成 quick（说不上来凭据就改档 = 替用户做决定）",
      str(cfg_after.get("mode")))

# 已告知的留痕：只认白名单位置里的“运行模式”段。
_plan = ROOT / "project-state/主控工作计划.md"
_plan.write_text(
    u"# 主控工作计划\n\n## 运行模式\n\n"
    u"- 当前档位：完整模式\n"
    u"- 怎么进来的：经理按 U1、U4 评估为大项目，**已告知用户**（2026-09-17）\n\n"
    u"## 班形\n\n不相干\u3002\n", encoding="utf-8")
r = run("--only", "mode-explicit")
check(u"[已到位] mode-explicit" in r.stdout,
      "M50 有“运行模式”段 + 告知留痕 → 该步骤报「已到位」（切档是一等公民）", r.stdout[-500:])

# 反向：同一句话写在别的段里不算凭据——那正是「静默切档」最常见的样子。
_plan.write_text(
    u"# 主控工作计划\n\n## 班形\n\n"
    u"足迹里提一句：这个项目走完整模式，已告知用户。\n", encoding="utf-8")
r = run("--only", "mode-explicit")
check(u"[需人工] mode-explicit" in r.stdout,
      "M51 告知字样写在别的段里 → 不算凭据（只认「运行模式」段）", r.stdout[-500:])
_plan.unlink()

# 反向：入口①——用户明确指定。
build(dict(CONFIG_OLD, mode="full"))
(ROOT / "project-state/主控工作计划.md").write_text(
    u"# 主控工作计划\n\n## 运行模式\n\n"
    u"- 当前档位：完整模式\n- 怎么进来的：**用户明确**要求用完整模式。\n",
    encoding="utf-8")
r = run("--only", "mode-explicit")
check(u"[已到位] mode-explicit" in r.stdout,
      "M52 入口①：用户明确指定也算凭据（两个入口都要认，不只认经理评估）", r.stdout[-500:])

# 模式与档位是两件事：快速模式却按三库要求 = 矛盾，要补。
build(dict(CONFIG_OLD, mode="quick", profile="C-standard-env"))
r = run("--only", "mode-explicit")
check(u"[待迁移] mode-explicit" in r.stdout,
      "M53 quick + profile=C → 报「待迁移」（「快速模式却按三库要求」是真矛盾）", r.stdout[-500:])
run("--only", "mode-explicit", "--apply")
cfg_after = json.loads((ROOT / "project-state/config.json").read_text(encoding="utf-8"))
check(cfg_after.get("mode") == "quick" and str(cfg_after.get("profile", "")).upper().startswith("A"),
      "M54 补齐后 mode=quick 且 profile 定到 A（不只改 mode）",
      "%s / %s" % (cfg_after.get("mode"), cfg_after.get("profile")))

# 反向：mode=full + profile=C 是**一致**的（完整模式默认三环境），不能误伤。
build(dict(CONFIG_OLD, mode="full", profile="C-standard-env"))
(ROOT / "project-state/主控工作计划.md").write_text(
    u"# 主控工作计划\n\n## 运行模式\n\n经理评估 U2 命中，已告知用户。\n",
    encoding="utf-8")
snap_first = snapshot()
run("--only", "mode-explicit", "--apply")
check(snapshot() == snap_first,
      "M55 full + profile=C 且入口完整 → 一个字节都不动（不得误伤正确配置）",
      str(sorted(k for k in set(snapshot()) | set(snap_first) if snapshot().get(k) != snap_first.get(k))[:5]))

shutil.rmtree(ROOT, ignore_errors=True)

# ---------------------------------------------------------------- 汇总

print()
print("=" * 72)
print("结果：%d 通过 / %d 失败" % (len(passes), len(fails)))
if fails:
    print("失败项：")
    for f in fails:
        print("  - %s" % f)
print("=" * 72)
sys.exit(1 if fails else 0)
