#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_test_scope_audit.py — `test_scope_audit.py` 的回归与变异测试（v5.9.0）

体例照 `test_case_audit.py` / `test_recall.py`：
  1 干净夹具不误报   2 单点变异逐条命中   3 成对反向断言（**不许报**）
  4 三态退出码（0/1/2 且 2 专门表示「无法检查」）   5 格式旧 ≠ 内容缺
  6 顺序与编码   7 检查器自身变异 + 反向控制
末尾**全绿才清场**（失败留着复现）。

运行：python -X utf8 scripts/tests/test_test_scope_audit.py [-v]
"""

import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# v5.28.0 修正：本文件的被测对象是 `scripts/test_scope_audit.py`（v5.9.0 测试量档位检查器）。
# 此前解析器先试同目录同名文件——v5.26.0 起 `scripts/tests/test_scope_audit.py` 是
# **探索域检查器 `scope_audit.py` 的回归用例**（命名撞车），同目录候选从此指错脚本，
# 本测试全红。被测脚本只认上层 `scripts/`；同目录候选删除。
AUDIT = os.path.join(HERE, os.pardir, "test_scope_audit.py")
TMP = os.path.join(HERE, "_scope_fixture_tmp")
MUT = os.path.join(TMP, "_mutated")

VERBOSE = "-v" in sys.argv
PASS = 0
FAILS = []

CODE_RE = re.compile(r"DEPTH-[A-Z-]+")


def check(cond, label):
    global PASS
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL %s" % label)


def codes(text):
    return set(CODE_RE.findall(text))


# ------------------------------------------------------------------ 夹具
D_CLEAN = [
    {"tc": "tc-001", "depth": "full", "use": "deliverable",
     "cadence": "milestone-M5", "note": "-", "notdoing": ""},
    {"tc": "tc-002", "depth": "targeted", "use": "internal",
     "cadence": "milestone-M5", "note": "只跑受影响维度：登录与订单状态机",
     "notdoing": "性能压测"},
    {"tc": "tc-003", "depth": "smoke", "use": "demo",
     "cadence": "every-5-12", "note": "仅关键路径最小闭环", "notdoing": "并发与压测"},
]


def render(c):
    tc = c["tc"]
    pairs = [("id", tc), ("title", "系统级测试 %s" % tc), ("scope", "全链路"),
             ("cadence_round", c.get("cadence", "milestone-M5")),
             ("status", "todo"), ("attempts", "0"),
             ("assignee", "测试工程师"), ("deps", "[]"),
             ("dimensions", "[功能正确性, 数据一致性]")]
    if not c.get("legacy"):
        if c.get("depth") is not None:
            pairs.append(("test_depth", c["depth"]))
        if c.get("use") is not None:
            pairs.append(("delivery_use", c["use"]))
        if c.get("note") is not None:
            pairs.append(("depth_note", c["note"]))
        if c.get("notdoing") is not None:
            pairs.append(("not_doing", "[%s]" % c["notdoing"]))
    drop = set(c.get("drop", ()))
    pairs = [(k, v) for k, v in pairs if k not in drop]
    body = ["---"]
    body += ["%s: %s" % (k, v) for k, v in pairs]
    body += ["created: 2026-09-28", "updated: 2026-09-28", "---", "",
             "# %s" % tc, "", "## 测试范围", "", "- 被测对象：全链路",
             "- 不做清单（not_doing）：见 frontmatter", ""]
    return "\n".join(body)


def make_fixture(name, cards=None, none=False, raw=None, encoding="utf-8", filenames=None):
    root = os.path.join(TMP, name)
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    if none:
        os.makedirs(root)
        return root
    tcdir = os.path.join(root, "project-state", "测试卡")
    os.makedirs(tcdir)
    if raw is not None:
        d = os.path.join(tcdir, "tc-001")
        os.makedirs(d)
        with open(os.path.join(d, "tc.md"), "wb") as fh:
            fh.write(raw)
        return root
    for c in (cards or []):
        d = os.path.join(tcdir, c.get("dir") or c["tc"])
        os.makedirs(d)
        if c.get("nofile"):
            continue
        with open(os.path.join(d, "tc.md"), "wb") as fh:
            fh.write(render(c).encode(encoding))
    return root


def run(root, *args, **kw):
    script = kw.get("script") or AUDIT
    cmd = [sys.executable, "-X", "utf8", script, root] + list(args)
    env = dict(os.environ)
    env.update(kw.get("env") or {})
    p = subprocess.run(cmd, cwd=os.path.dirname(script), capture_output=True, env=env)
    text = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    return p.returncode, text


def run_json(root, *args, **kw):
    code, text = run(root, "--json", *args, **kw)
    try:
        return code, json.loads(text[text.index("{"):text.rindex("}") + 1]), text
    except Exception:
        return code, None, text


def expect(root, want_exit, want_code=None, absent=(), label="", *args):
    code, text = run(root, *args)
    tag = label or (want_code or "clean")
    check(code == want_exit, "%s: 退出码 = %d（实得 %d）" % (tag, want_exit, code))
    cs = codes(text)
    if want_code:
        check(want_code in cs, "%s: 命中 %s（实得 %s）" % (tag, want_code, sorted(cs) or "无"))
    for a in absent:
        check(a not in cs, "%s: **不许**报 %s（实得 %s）" % (tag, a, sorted(cs) or "无"))
    return code, text


def main():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)

    print("== test_test_scope_audit.py | 被测：%s ==" % AUDIT)

    # ------------------------------------------------ 1 干净夹具不误报
    print("\n[1] 干净夹具")
    clean = make_fixture("clean", D_CLEAN)
    code, text = expect(clean, 0, label="干净三档")
    check(codes(text) == set(), "干净三档: 无任何 code（实得 %s）" % (sorted(codes(text)) or "无"))
    check("full 1" in text and "targeted 1" in text and "smoke 1" in text,
          "干净三档: 档位分布 full/targeted/smoke 各 1")
    check("**只证明「声明得对」" in text or "只证明「声明得对」" in text,
          "干净三档: 明写「只证明声明得对，不证明档位定得合理」")
    code, text = run(clean, "--check")
    check(code == 0 and "ERROR 0" in text, "--check: 单行计数且退出码 0")
    code, js, _t = run_json(clean)
    check(code == 0 and js and js.get("ok") is True and js.get("exit") == 0,
          "--json: ok=True / exit=0")
    check(js and js.get("dist") == {"full": 1, "targeted": 1, "smoke": 1, "?": 0},
          "--json: dist 精确")

    # ---------------------------------------- 2 单点变异逐条命中
    print("\n[2] 单点变异逐条命中")
    def one(**over):
        cards = [dict(c) for c in D_CLEAN]
        for c in cards:
            if c["tc"] == "tc-002":
                c.update(over)
        return make_fixture("one_%s" % "_".join("%s%s" % (k, v) for k, v in over.items())[:40], cards)

    expect(one(depth="quick"), 1, "DEPTH-INVALID", label="档位乱写")
    expect(one(use="whatever"), 1, "DEPTH-DELIVERY-INVALID", label="用途乱写")
    expect(one(depth="full", use="demo"), 0, absent=("DEPTH-DELIVERY-MISMATCH",),
           label="demo 定全量=保守允许")
    r = make_fixture("mismatch", [dict(D_CLEAN[0], tc="tc-001", depth="targeted", use="deliverable"),
                                  D_CLEAN[2]])
    expect(r, 1, "DEPTH-DELIVERY-MISMATCH", label="交付包定定向")
    r = make_fixture("ship", [dict(D_CLEAN[0], tc="tc-001", depth="targeted", use="internal",
                                   cadence="ship-M5", notdoing="压测"),
                              D_CLEAN[2]])
    expect(r, 1, "DEPTH-SHIP-NOT-FULL", label="上线前一轮定定向")
    expect(one(note=None), 1, "DEPTH-NOT-DOING-MISSING", label="定向缺不测说明")
    expect(one(notdoing=None), 1, "DEPTH-NOT-DOING-MISSING", label="定向缺不做清单")
    expect(one(note="x", notdoing=""), 1, "DEPTH-NOT-DOING-MISSING", label="定向不做清单为空")
    expect(one(depth=None), 1, "DEPTH-MISSING", label="有用途无档位")
    expect(one(use=None), 1, "DEPTH-DELIVERY-MISSING", label="有档位无用途")
    r = make_fixture("nofm", raw="没有 frontmatter 的卡\n".encode("utf-8"))
    expect(r, 1, "DEPTH-NO-FRONTMATTER", label="无 frontmatter")
    r = make_fixture("unclosed", raw="---\nid: tc-001\ntest_depth: full\n".encode("utf-8"))
    expect(r, 1, "DEPTH-NO-FRONTMATTER", label="frontmatter 未闭合")
    r = make_fixture("sep", [dict(D_CLEAN[0], tc="tc-001", depth="'FULL'", use="'Deliverable'"),
                             D_CLEAN[2]])
    expect(r, 0, label="引号包裹的大小写档位")

    # -------------------------------- 3 成对反向断言（不许报）
    print("\n[3] 成对反向断言：full 档不要求不测清单")
    r = make_fixture("full_note", [dict(D_CLEAN[0], tc="tc-001", depth="full", use="deliverable",
                                        cadence="ship-M1", note=None, notdoing=None), D_CLEAN[2]])
    code, text = expect(r, 0, absent=("DEPTH-NOT-DOING-MISSING", "DEPTH-DELIVERY-MISMATCH"),
                        label="全量档无说明无清单")
    check("不测范围摘要" in text, "全量档: 仍印出摘要表头（人眼可扫）")

    print("\n[3b] 连续冒烟：2 张不报，3 张才报")
    two = [dict(D_CLEAN[2], tc="tc-001", depth="smoke"), dict(D_CLEAN[2], tc="tc-002", depth="smoke"),
           dict(D_CLEAN[0], tc="tc-003", depth="full", use="deliverable", cadence="ship-M5")]
    r = make_fixture("smoke2", two)
    expect(r, 0, absent=("DEPTH-SMOKE-STREAK",), label="连 2 张冒烟")
    three = [dict(D_CLEAN[2], tc="tc-001", depth="smoke"), dict(D_CLEAN[2], tc="tc-002", depth="smoke"),
             dict(D_CLEAN[2], tc="tc-003", depth="smoke")]
    r = make_fixture("smoke3", three)
    code, text = expect(r, 0, "DEPTH-SMOKE-STREAK", label="连 3 张冒烟")
    check("冒烟是例外不是常态" in text, "连 3 张冒烟: 提示语点明「例外不是常态」")

    print("\n[3c] 行内注释与引号不被当内容")
    r = make_fixture("raw_comment", raw=(
        "---\nid: tc-001\ntitle: t\nscope: s\ncadence_round: milestone-M5   # 触发点\n"
        "test_depth: targeted   # 本轮定向\ndelivery_use: demo # 给人看\n"
        "depth_note: \"只跑受影响维度：A 与 B\"\nnot_doing: [压测, 并发]\n---\n正文\n"
    ).encode("utf-8"))
    expect(r, 0, label="行内注释+引号")

    print("\n[3d] 大小写与空白容错")
    r = make_fixture("case", [dict(D_CLEAN[2], tc="tc-001", depth="Targeted", use="DEMO"),
                              D_CLEAN[2] and dict(D_CLEAN[0], tc="tc-002", depth=" FULL ", use="Deliverable")])
    expect(r, 0, absent=("DEPTH-INVALID", "DEPTH-DELIVERY-INVALID"), label="大小写与首尾空白")

    # --------------------------------- 4 格式旧 ≠ 内容缺
    print("\n[4] 格式旧 ≠ 内容缺（v5.9.0 迁移纪律）")
    legacy = [{"tc": "tc-001", "legacy": True, "cadence": "milestone-M5"},
              {"tc": "tc-002", "legacy": True, "cadence": "milestone-M5"}]
    r = make_fixture("legacy", legacy)
    code, text = expect(r, 0, "DEPTH-FORMAT-STALE",
                        absent=("DEPTH-MISSING", "DEPTH-DELIVERY-MISSING", "DEPTH-NOT-DOING-MISSING"),
                        label="两张旧卡")
    check(text.count("DEPTH-FORMAT-STALE") >= 2, "两张旧卡: 每卡各一条（不逐键报）")
    check("格式旧 ≠ 内容缺" in text, "旧卡: 提示语点明迁移纪律")
    code, text = run(r, "--allow-unmigrated")
    check(code == 0 and "DEPTH-FORMAT-STALE" in text, "--allow-unmigrated: 仍为 0 且保留提示")
    mixed = [{"tc": "tc-001", "legacy": True, "cadence": "milestone-M5"},
             dict(D_CLEAN[0], tc="tc-002")]
    r = make_fixture("mixed", mixed)
    expect(r, 0, "DEPTH-FORMAT-STALE", label="旧卡+新卡混排")
    check(codes(run(r)[1]) == {"DEPTH-FORMAT-STALE"}, "旧卡+新卡混排: 只有迁移提示一个 code")

    # --------------------------------- 5 三态退出码
    print("\n[5] 三态退出码：0 / 1 / 2，且 2 = 无法检查")
    r = make_fixture("notc", none=True)
    code, text = run(r)
    check(code == 2, "无 测试卡 目录: 退出码 2（实得 %d）" % code)
    check("DEPTH-NO-TESTCARD" in text, "无 测试卡 目录: 命中 DEPTH-NO-TESTCARD")
    check("无法检查，不是「没问题」" in text, "无 测试卡 目录: 明写「无法检查 ≠ 没问题」")
    check("豁免依据" in text, "无 测试卡 目录: 指向 #24 豁免依据")
    code, js, _t = run_json(r)
    check(code == 2 and js and js.get("ok") is None and js.get("exit") == 2 and js.get("cards") == 0,
          "无 测试卡 目录: --json ok=None / exit=2 / cards=0")
    r = make_fixture("emptycard", [{"tc": "tc-001", "nofile": True}])
    code, text = run(r)
    check(code == 2 and "DEPTH-NO-TESTCARD" in text, "有目录无 tc.md: 退出码 2（同样无法检查）")
    code, _t = run(clean)
    check(code == 0, "干净: 退出码 0")
    bad = make_fixture("bad", [dict(D_CLEAN[1], tc="tc-001", depth="quick"), D_CLEAN[2]])
    code, text = run(bad)
    check(code == 1 and "退出码 1" in text and "第 19 项" in text,
          "有 ERROR: 退出码 1 且指向前置检查第 19 项")
    code, text = run(clean, "--nope")
    check(code == 2 and "未知参数" in text, "未知参数: 退出码 2（不能当通过）")
    code, text = run(clean, "--help")
    check(code == 0 and "退出码：0 干净" in text, "--help: 退出码 0 且印出三态语义")
    code, text = run(os.path.join(TMP, "不存在的根"))
    check(code == 2, "根不存在: 退出码 2")

    # --------------------------------- 6 顺序与编码
    print("\n[6] 顺序与编码")
    r = make_fixture("order", [dict(D_CLEAN[2], tc="tc-003", depth="smoke"),
                               dict(D_CLEAN[2], tc="tc-010", depth="smoke"),
                               dict(D_CLEAN[2], tc="tc-002", depth="smoke")])
    code, text = expect(r, 0, "DEPTH-SMOKE-STREAK", label="乱序建目录 3 张冒烟")
    check("tc-002 → tc-003 → tc-010" in text, "乱序建目录: 按序号排序后判定（2→3→10）")
    r = make_fixture("gbk", D_CLEAN, encoding="gbk")
    expect(r, 0, label="GBK 编码")
    r = make_fixture("gbk_bad", [dict(D_CLEAN[1], tc="tc-001", depth="quick")], encoding="gbk")
    expect(r, 1, "DEPTH-INVALID", label="GBK 编码也能报错")
    r = make_fixture("projroot", D_CLEAN)
    r2 = os.path.join(r, "project-state")
    code_a, text_a = run(r, "--check")
    code_b, text_b = run(r2, "--check")
    check(code_a == 0 and code_b == 0 and text_a == text_b, "传 project-state 与传根等价")

    # --------------------------------- 7 检查器自身变异 + 反向控制
    print("\n[7] 检查器自身变异（只会变松，不会变紧？——这里验的是「失去检测力」）")
    src = open(AUDIT, "r", encoding="utf-8").read()
    os.makedirs(MUT, exist_ok=True)
    mutations = [
        ("h1_delivery",
         'if use == "deliverable" and has_depth and depth and depth != "full":',
         "if False:", "交付包必须全量"),
        ("h2_ship",
         'if cadence.strip().lower().startswith("ship") and has_depth and depth and depth != "full":',
         "if False:", "上线前必须全量"),
        ("h3_notdoing",
         'if has_depth and depth in DEPTHS and depth != "full":',
         "if False:", "非全量必须写不清什么"),
        ("h4_streak",
         'if len(run) > SMOKE_STREAK_MAX:',
         "if False:", "连续冒烟上限"),
    ]
    # 变异用的夹具：各自触发对应规则
    fx = {
        "h1_delivery": make_fixture("mut_h1", [dict(D_CLEAN[0], tc="tc-001", depth="targeted",
                                                    use="deliverable", notdoing="压测",
                                                    note="只跑受影响维度：A 与 B"), D_CLEAN[2]]),
        "h2_ship": make_fixture("mut_h2", [dict(D_CLEAN[0], tc="tc-001", depth="targeted",
                                                use="internal", cadence="ship-M5", notdoing="压测",
                                                note="只跑受影响维度：A 与 B"),
                                           D_CLEAN[2]]),
        "h3_notdoing": make_fixture("mut_h3", [dict(D_CLEAN[1], tc="tc-001", note=None,
                                                    notdoing=None), D_CLEAN[2]]),
        "h4_streak": make_fixture("mut_h4", three),
    }
    want = {"h1_delivery": "DEPTH-DELIVERY-MISMATCH", "h2_ship": "DEPTH-SHIP-NOT-FULL",
            "h3_notdoing": "DEPTH-NOT-DOING-MISSING", "h4_streak": "DEPTH-SMOKE-STREAK"}
    for name, old, new, desc in mutations:
        path = os.path.join(MUT, "%s.py" % name)
        check(old in src, "变异 %s: 锚点存在（%s）" % (name, desc))
        if old not in src:
            continue
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src.replace(old, new))
        code, text = run(fx[name], script=path)
        check(want[name] not in codes(text),
              "变异 %s: 该规则**失去检测力**（%s 未命中）" % (name, want[name]))
        check(code == 0, "变异 %s: 触发夹具从 ERROR 变干净（证明确实是这个判据在管）" % name)
        code, text = run(clean, script=path)
        check(code == 0 and codes(text) == set(),
              "变异 %s: 反向控制——干净夹具仍 0 且无 code" % name)

    # ------------------------------------------------------------ 汇总
    print("\n断言：%d 通过 / %d 失败" % (PASS, len(FAILS)))
    if FAILS:
        print("失败清单：")
        for f in FAILS:
            print("  - %s" % f)
        print("（夹具保留在 %s 供复现）" % TMP)
        return 1
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
