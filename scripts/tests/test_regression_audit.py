#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_regression_audit.py — `regression_audit.py` 的回归 + 变异对拍（纯 stdlib，不依赖 pytest）

为什么有这个脚本
----------------
`regression_audit.py`（v5.28.0 回归防线）的检查码在真实工程上短期内跑不到真实数据——
**没有夹具它们就是未验证代码**。两条判据缺一不可（本框架踩过的坑）：

  ① **能拦**：每个检查码都要有一个**故意让它失败**的夹具；
  ② **不乱拦**：每个检查码都要有成对的**干净反向断言**（改了不该报 = 假阳性，同样致命）。

第三层：**检查器自身的变异**——把 `regression_audit.py` 改瞎四处，断言对应夹具
**必须失去命中**。没有这一层，上面所有断言可能只是"夹具恰好让脚本退出 1"。

用法
----
    python test_regression_audit.py          # 跑全部，失败则非零退出
    python test_regression_audit.py -v       # 打印每条实际结果
"""

import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REG_AUDIT = next((p for p in (os.path.join(HERE, "regression_audit.py"),
                              os.path.join(HERE, os.pardir, "regression_audit.py"))
                  if os.path.exists(p)), os.path.join(HERE, "regression_audit.py"))
TMP = os.path.join(HERE, "_fixture_tmp")

CASES_HEADER = "id | scope | method | data | anti_mock | source | last"

CLEAN_ROWS = [
    "CAS-001 | card-001 | 等价类+边界值 | 金额 0/1/上限/上限+1 各一次 | 写入后直查库核余额 | AC:REQ-1.1 | pass",
    "CAS-002 | tc-001 | 状态迁移+穿透 | 状态机 5 态各走一次 | 库内状态字段直查 + 拦截器日志留痕 | 发现:tc-001#F-01 | pass",
    "CAS-003 | global | 场景法 | 中文名/空文件/边界值 | canary 值 9999 不得出现在响应体 | 反馈:card-002 用户三次反馈列表不刷新 | pass",
]

FINDINGS = """# 发现项

<!-- machine:findings -->
id | severity | owner | ref | state | anchor | entry
F-01 | required | B | card-002 | fixed | repos/app/src/list.py:88 | 复跑 test_refresh
F-02 | required | B | card-003 | open | repos/app/src/detail.py:10 | 复跑 test_detail
<!-- /machine:findings -->
"""

CARD_TMPL = """---
id: %(card)s
title: %(title)s
req: ""
source: %(source)s
anchor: %(anchor)s
entry: %(entry)s
status: %(status)s
reg_case: %(reg_case)s
reg_note: %(reg_note)s
---
# 修复卡正文

现象/复现/影响面。
"""

TURBO_TMPL = """# 极速修复台账

<!-- machine:turbo -->
id | at | source | basis | symptom | files | depth | evidence | pack | exit | exit_reason | verified
%(rows)s
<!-- /machine:turbo -->
"""

TURBO_CLEAN_ROW = ("TRB-001 | 2026-11-01 10:00 | 用户反馈按钮无响应 | card-001+CAS-003 | "
                   "按钮无响应 | 1 | smoke | project-state/运行日志.md | - | | | 否")

FAILS = []
PASSES = []
VERBOSE = "-v" in sys.argv


def check(cond, label):
    if cond:
        PASSES.append(label)
    else:
        FAILS.append(label)
    if VERBOSE:
        print("  %s %s" % ("ok  " if cond else "FAIL", label))


# ------------------------------------------------------------------ 夹具
def write_card(base, card, status="done", source="tc-001#F-01",
               anchor="repos/app/src/list.py:88", entry="复跑 test_refresh",
               reg_case="CAS-002", reg_note="", title="修复列表不刷新"):
    d = os.path.join(base, "需求卡", card)
    if not os.path.isdir(d):
        os.makedirs(d)
    with open(os.path.join(d, "card.md"), "w", encoding="utf-8") as fh:
        fh.write(CARD_TMPL % {"card": card, "title": title, "source": source,
                              "anchor": anchor, "entry": entry, "status": status,
                              "reg_case": reg_case, "reg_note": reg_note})


def write_tc(base, tc, status="in_progress", use="internal", cadence="round-1",
             findings=FINDINGS):
    d = os.path.join(base, "测试卡", tc)
    os.makedirs(d)
    with open(os.path.join(d, "tc.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nid: %s\nstatus: %s\ndelivery_use: %s\ncadence_round: %s\n"
                 "test_depth: targeted\n---\n" % (tc, status, use, cadence))
    if findings is not None:
        with open(os.path.join(d, "发现项.md"), "w", encoding="utf-8") as fh:
            fh.write(findings)


def write_turbo(base, rows):
    d = os.path.join(base, "问题")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "极速修复台账.md"), "w", encoding="utf-8") as fh:
        fh.write(TURBO_TMPL % {"rows": "\n".join(rows)})


def spec_card(base, spec):
    """卡规格：`card-NNN` = 默认修复卡；`card-NNN:status:source:reg_case`（source 可含 `回归:`，
    用 rpartition 拆最后一列；source/anchor 全空 = 普通功能卡）。"""
    parts = spec.split(":", 2)
    if len(parts) == 1:
        write_card(base, spec)
        return
    name, status, rest = parts[0], parts[1], parts[2] if len(parts) > 2 else ""
    source, _sep, reg_case = rest.rpartition(":")
    if not source and not reg_case and rest in ("", ":",):
        source, reg_case = "", ""
    if not source:
        write_card(base, name, status=status, source="", anchor="", entry="",
                   reg_case="", reg_note="", title="普通功能卡")
    else:
        write_card(base, name, status=status, source=source,
                   anchor="repos/app/src/list.py:88", entry="复跑", reg_case=reg_case)


def make_fixture(name, rows=None, cards=None, tcs=None, lib=True, block=True,
                 turbo_rows=None, findings=FINDINGS, encoding="utf-8", extra_rows=()):
    """回归防线夹具。

    默认件：CAS 三条全 pass（一条 AC、两条基线）+ 修复卡 card-002（done、哨兵 CAS-002）
    + 普通卡 card-001（done、无哨兵——不受哨兵线管）+ 测试卡 tc-001（in_progress）。
    卡规格见 spec_card；测试卡规格 `tc-NNN:status:delivery_use`（cadence 用 tcs 元组另行写）。
    """
    root = os.path.join(TMP, name)
    if os.path.isdir(root):
        shutil.rmtree(root)
    base = os.path.join(root, "project-state")
    os.makedirs(os.path.join(base, "代码", "测试"))
    os.makedirs(os.path.join(base, "需求卡"))
    os.makedirs(os.path.join(base, "测试卡"))
    if lib:
        if block:
            body = list(rows if rows is not None else CLEAN_ROWS) + list(extra_rows)
            parts = ["# 用例库", "", "> 机器只读下面的机读块。", "",
                     "```text", "<!-- machine:cases -->", CASES_HEADER]
            parts += body
            parts += ["<!-- /machine:cases -->", "```"]
        else:
            parts = ["# 用例库", "", "（没有机读块）", ""]
        with open(os.path.join(base, "代码", "测试", "用例库.md"), "w", encoding=encoding) as fh:
            fh.write("\n".join(parts) + "\n")
    for c in (cards if cards is not None else ["card-001:done::", "card-002"]):
        spec_card(base, c)
    for t in (tcs if tcs is not None else ["tc-001"]):
        if isinstance(t, tuple):
            write_tc(base, t[0], status=t[1], use=t[2], cadence=t[3] if len(t) > 3 else "round-1")
        elif ":" in t:
            name_, status, use = (t.split(":") + ["internal"])[:3]
            write_tc(base, name_, status=status, use=use)
        else:
            write_tc(base, t)
    if turbo_rows is not None:
        write_turbo(base, turbo_rows)
    return root


def run(root, *args, env=None):
    cmd = [sys.executable, "-X", "utf8", REG_AUDIT, root] + list(args)
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       cwd=os.path.dirname(REG_AUDIT), env=e)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def run_json(root, *args):
    code, sout, serr = run(root, "--json", *args)
    try:
        data = json.loads(sout)
    except ValueError:
        print("JSON 解析失败：\n%s\n%s" % (sout, serr))
        raise
    return code, data


def codes_of(data):
    return [i["code"] for i in data["items"]]


def expect(root, want_exit, want_code, sev=None, absent=()):
    c, data = run_json(root)
    codes = codes_of(data)
    tag = os.path.basename(root)
    check(c == want_exit, "%s: 退出码 %s（实际 %s）" % (tag, want_exit, c))
    check(want_code in codes, "%s: 命中 %s（实际 %s）" % (tag, want_code, codes))
    for a in absent:
        check(a not in codes, "%s: **不许**报 %s（实际 %s）" % (tag, a, codes))
    if sev:
        check(data["counts"][sev] >= 1, "%s: 至少 1 个 %s" % (tag, sev))
    return data


def rows_with(cid_old, cid_new_last):
    """把 CLEAN_ROWS 里某条用例的 last 换掉（单点变异辅助）。"""
    out_rows = []
    for r in CLEAN_ROWS:
        if r.startswith(cid_old + " "):
            r = r.rsplit("|", 1)[0] + "| " + cid_new_last
        out_rows.append(r)
    return out_rows


# ------------------------------------------------------------------ 1. 干净夹具
print("== 干净夹具 ==")
CLEAN = make_fixture("clean")
c, data = run_json(CLEAN)
check(c == 0, "干净：退出码 0（实际 %s）" % c)
check(data["ok"] is True, "干净：ok=True")
check(data["cases"] == 3, "干净：3 条用例（实际 %s）" % data["cases"])
check(data["baseline"]["total"] == 2, "干净：基线哨兵集 2 条（AC 不算）（实际 %s）" % data["baseline"]["total"])
check(data["baseline"]["pass"] == 2, "干净：基线全 pass")
check(data["fix_cards"] == 1, "干净：只识别出 1 张修复卡（card-001 是普通卡）（实际 %s）" % data["fix_cards"])
check(data["counts"]["ERROR"] == 0 and data["counts"]["WARN"] == 0,
      "干净：0 ERROR / 0 WARN（实际 %s）" % data["counts"])
check(data["counts"]["INFO"] == 1, "干净：1 INFO（没用过极速修复，是 INFO 不是「没查」）")
_c, sout, _e = run(CLEAN)
check("四条线都干净" in sout, "干净：人读输出声明四条线")
check("不归本脚本" in sout, "干净：印出诚实边界")

# ------------------------------------------------------------------ 2. 线 1：哨兵绑定
print("== 线 1：哨兵绑定（SENTINEL-*）==")
expect(make_fixture("s_missing", cards=["card-001:done::", "card-002:done:tc-001#F-01:"]),
       1, "SENTINEL-MISSING", sev="ERROR",
       absent=("BASELINE-FAIL-NO-CARD", "BASELINE-FAIL-CARD-DONE"))
expect(make_fixture("s_inprogress", cards=["card-001:done::", "card-002:in_progress:tc-001#F-01:"]),
       0, "TURBO-NOT-USED", absent=("SENTINEL-MISSING",))
expect(make_fixture("s_waiver_empty", cards=["card-001:done::", "card-002:done:tc-001#F-01:-"]),
       1, "SENTINEL-WAIVER-EMPTY", sev="ERROR")
# 豁免写明 reg_note → 放行（真人/声学/物理面合法豁免）
root = make_fixture("s_waiver_ok", cards=["card-001:done::", "card-002:done:tc-001#F-01:-"])
p = os.path.join(root, "project-state", "需求卡", "card-002", "card.md")
txt = open(p, encoding="utf-8").read().replace("reg_note: \n",
                                               "reg_note: 真人声学面无法自动化复现\n")
with open(p, "w", encoding="utf-8") as fh:
    fh.write(txt)
c, data = run_json(root)
check(c == 0 and "SENTINEL-WAIVER-EMPTY" not in codes_of(data),
      "线1：reg_case=- 且 reg_note 写明依据 → 放行（实际 %s / %s）" % (c, codes_of(data)))
expect(make_fixture("s_dangling",
                    extra_rows=["CAS-010 | tc-001 | 场景法 | 造数脚本 seed 7 | 直查库 | 发现:tc-001#F-02 | pass"],
                    cards=["card-001:done::", "card-002:done:tc-001#F-01:CAS-999"]),
       1, "SENTINEL-DANGLING", sev="ERROR")
expect(make_fixture("s_notpass", rows=rows_with("CAS-002", "unknown")),
       1, "SENTINEL-NOT-PASS", sev="ERROR",
       absent=("BASELINE-FAIL-NO-CARD", "BASELINE-FAIL-CARD-DONE"))
root = make_fixture("s_retired", rows=rows_with("CAS-002", "retired"))
c, data = run_json(root)
check(c == 0 and "SENTINEL-RETIRED" in codes_of(data) and data["counts"]["WARN"] == 1,
      "线1：哨兵 retired → WARN 不拦退出码（提示换哨兵）（实际 %s / %s）" % (c, codes_of(data)))
check(run_json(root)[1]["baseline"]["total"] == 1, "线1：retired 不进基线哨兵集")

# ------------------------------------------------------------------ 3. 线 2/3：基线全绿与二进宫处置
print("== 线 2/3：基线全绿（BASELINE-*）==")
fail_rows = rows_with("CAS-002", "fail")
# 纯基线线：fail 挂在**无卡引用**的 CAS 上（CAS-005，发现项 F-02 没有修复卡在夹具里）
unref_fail = ["CAS-005 | tc-001 | 场景法 | 造数脚本 seed 5 | 直查库 | 发现:tc-001#F-02 | fail"]
expect(make_fixture("b_no_card", extra_rows=unref_fail),
       1, "BASELINE-FAIL-NO-CARD", sev="ERROR", absent=("SENTINEL-NOT-PASS",))
# 有开着的回归修复卡在跟（source: 回归:CAS-002）→ 在跟，不打
root = make_fixture("b_tracked", rows=fail_rows,
                    cards=["card-001:done::", "card-002", "card-003:in_progress:回归:CAS-002:"])
c, data = run_json(root)
check(c == 0 and not [x for x in codes_of(data) if x.startswith("BASELINE-FAIL")],
      "线3：回归修复卡开着（在跟）→ 不打（实际 %s / %s）" % (c, codes_of(data)))
# 关联卡已收口且哨兵挂（reg_case=自己）→ SENTINEL-NOT-PASS 覆盖，不重复报 BASELINE
expect(make_fixture("b_done_dup", rows=fail_rows),
       1, "SENTINEL-NOT-PASS", sev="ERROR",
       absent=("BASELINE-FAIL-NO-CARD", "BASELINE-FAIL-CARD-DONE"))
# 复挂哨兵的关联卡已收口、但其 reg_case 不是本 CAS → BASELINE-FAIL-CARD-DONE
root = make_fixture(
    "b_card_done",
    rows=fail_rows + ["CAS-004 | tc-001 | 变异 | seed 9 | 直查库 | 回归:CAS-002 | pass"],
    cards=["card-001:done::", "card-002:done:tc-001#F-01:CAS-004",
           "card-003:done:回归:CAS-002:CAS-004"])
c, data = run_json(root)
check("BASELINE-FAIL-CARD-DONE" in codes_of(data) and "SENTINEL-NOT-PASS" not in codes_of(data),
      "线3：复挂哨兵的关联卡已收口 → BASELINE-FAIL-CARD-DONE（实际 %s）" % codes_of(data))
expect(make_fixture("b_unknown", extra_rows=[
    "CAS-005 | tc-001 | 场景法 | 造数脚本 seed 5 | 直查库 | 发现:tc-001#F-02 | unknown"]),
       0, "BASELINE-UNKNOWN", sev="WARN")
# AC 基线不在基线哨兵集：AC 用例 fail 不触发二进宫处置（需求回归归覆盖矩阵管）
root = make_fixture("b_ac_fail", rows=rows_with("CAS-001", "fail"))
c, data = run_json(root)
check(c == 0 and not [x for x in codes_of(data) if x.startswith("BASELINE-FAIL")],
      "线2：AC 用例 fail 不算基线哨兵（实际 %s / %s）" % (c, codes_of(data)))
root = make_fixture("b_retired", rows=rows_with("CAS-003", "retired"))
check(run_json(root)[1]["baseline"]["total"] == 1, "线2：retired 不进基线（实际 %s）"
      % run_json(root)[1]["baseline"]["total"])

# ------------------------------------------------------------------ 4. 交付/上线收口门
print("== 交付/上线收口门（BASELINE-SHIP-NOT-GREEN）==")
expect(make_fixture("ship_green", tcs=["tc-001", ("tc-002", "done", "deliverable")]),
       0, "TURBO-NOT-USED", absent=("BASELINE-SHIP-NOT-GREEN",))
expect(make_fixture("ship_unknown", rows=rows_with("CAS-002", "unknown"),
                    tcs=["tc-001", ("tc-002", "done", "deliverable")]),
       1, "BASELINE-SHIP-NOT-GREEN", sev="ERROR")
expect(make_fixture("ship_skip", rows=rows_with("CAS-002", "skip"),
                    tcs=["tc-001", ("tc-002", "done", "deliverable")]),
       1, "BASELINE-SHIP-NOT-GREEN", sev="ERROR")
root = make_fixture("ship_cadence", rows=rows_with("CAS-002", "skip"),
                    tcs=["tc-001", ("tc-002", "done", "internal", "ship-20261101")])
check("BASELINE-SHIP-NOT-GREEN" in codes_of(run_json(root)[1]),
      "收口门：cadence_round 以 ship 开头等同 deliverable")
root = make_fixture("ship_wip", rows=rows_with("CAS-002", "skip"),
                    tcs=["tc-001", ("tc-002", "in_progress", "deliverable")])
check("BASELINE-SHIP-NOT-GREEN" not in codes_of(run_json(root)[1]),
      "收口门：deliverable 轮还没跑完（in_progress）不判结果门")

# ------------------------------------------------------------------ 5. 线 4：极速修复的债
print("== 线 4：极速修复的债（TURBO-SENTINEL-*）==")
expect(make_fixture("t_missing",
                    turbo_rows=[TURBO_CLEAN_ROW.replace("card-001+CAS-003", "card-001")
                                .replace("| 否", "| 是")]),
       1, "TURBO-SENTINEL-MISSING", sev="ERROR")
root = make_fixture("t_clean", turbo_rows=[TURBO_CLEAN_ROW.replace("| 否", "| 是")])
c, data = run_json(root)
check(c == 0 and data["counts"]["ERROR"] == 0,
      "线4：verified=是 且 basis 带 CAS（哨兵 pass）→ 干净（实际 %s / %s）"
      % (c, codes_of(data)))
expect(make_fixture("t_dangling",
                    turbo_rows=[TURBO_CLEAN_ROW.replace("card-001+CAS-003", "CAS-999")
                                .replace("| 否", "| 是")]),
       1, "TURBO-SENTINEL-DANGLING", sev="ERROR")
expect(make_fixture("t_notpass", rows=rows_with("CAS-003", "fail"),
                    turbo_rows=[TURBO_CLEAN_ROW.replace("| 否", "| 是")]),
       1, "TURBO-SENTINEL-NOT-PASS", sev="ERROR")
expect(make_fixture("t_debt", turbo_rows=[TURBO_CLEAN_ROW]),
       0, "TURBO-SENTINEL-DEBT", sev="WARN")
root = make_fixture("t_absent")
c, data = run_json(root)
check("TURBO-NOT-USED" in codes_of(data) and data["counts"]["ERROR"] == 0,
      "线4：没台账 = 没用过极速（INFO），不是 ERROR 也不是「无法检查」")
root = make_fixture("t_noblock")           # 手工写一个无块台账
write_turbo_noblock = os.path.join(root, "project-state", "问题")
os.makedirs(write_turbo_noblock, exist_ok=True)
with open(os.path.join(write_turbo_noblock, "极速修复台账.md"), "w", encoding="utf-8") as fh:
    fh.write("# 极速修复台账\n\n（散文，没有机读块）\n")
c, data = run_json(root)
check(c == 0 and "TURBO-SENTINEL-MISSING" not in codes_of(data),
      "线4：台账无机读块 → 本脚本不重复报（K 组的地盘），退出码 0（实际 %s）" % c)

# ------------------------------------------------------------------ 6. 三态退出码
print("== 三态退出码 ==")
root = make_fixture("nolib", lib=False, cards=[], tcs=[])
c, data = run_json(root)
check(c == 2, "无用例库：退出码 2（实际 %s）" % c)
check(data["ok"] is None, "无用例库：ok=None（不是「没问题」）")
_c, sout, _e = run(root)
check("无法检查" in sout, "无库：人读输出明写「无法检查」")
expect(make_fixture("noblock", block=False), 1, "CASES-BLOCK-MISSING", sev="ERROR")
c, _sout, _e = run(make_fixture("cli"), "--no-such-flag")
check(c == 2, "未知参数：退出码 2")
c, _sout, _e = run(CLEAN, "--help")
check(c == 0, "--help：退出码 0")

# ------------------------------------------------------------------ 7. --impact 碰撞复验
print("== --impact 碰撞复验 ==")
c, sout, _e = run(CLEAN, "--impact", "repos/app/src/list.py")
check(c == 0, "--impact：退出码 0（实际 %s）" % c)
check("F-01" in sout and "tc-001#F-01" in sout, "--impact：命中同文件发现项 F-01")
check("CAS-002" in sout and "碰撞哨兵集" in sout, "--impact：印出碰撞哨兵集（CAS-002）")
check("state=fixed" in sout, "--impact：fixed 历史发现项也在列（历史修复碰撞正是本工具的意义）")
check("detail.py" not in sout.split("【碰撞哨兵集】")[0].split("同文件历史发现项")[1],
      "--impact：不同文件的 F-02 不混入碰撞集")
c, sout, _e = run(CLEAN, "--impact", "card-002")
check("list.py" in sout and "CAS-002" in sout,
      "--impact：card-NNN 自动解析 anchor（实际命中 list.py）")
c, sout, _e = run(CLEAN, "--impact", "repos/app/src/other.py")
check(c == 0 and "没有已知哨兵" in sout, "--impact：无碰撞 → 明说没有已知哨兵")
c, sout, _e = run(make_fixture("imp_nolib", lib=False), "--impact", "x.py")
check(c == 2, "--impact：无用例库 → 退出码 2（无法检查）")
c, sout, _e = run(CLEAN, "--impact", "未知")
check(c == 2, "--impact：未知/空路径 → 退出码 2")

# ------------------------------------------------------------------ 8. 编码
print("== 编码 ==")
root = make_fixture("gbk", encoding="gbk")
c, data = run_json(root)
check(c == 0 and data["counts"]["ERROR"] == 0, "GBK 用例库：能读、退出码 0")
c, _sout, serr = run(root, env={"PYTHONIOENCODING": "gbk"})
check(c == 0 and "Traceback" not in serr, "stdout 强指 GBK：不崩、无 Traceback")

# ------------------------------------------------------------------ 9. 检查器自身变异
print("== 检查器自身变异（反向断言要有牙）==")
MUTATIONS = [
    ("不查哨兵缺失", 'if rc == "":', "if False:",
     "s_missing", "SENTINEL-MISSING"),
    ("不查基线 fail", 'if row["last"] != "fail" or cid in reg_flagged:', "if True:",
     "b_no_card", "BASELINE-FAIL-NO-CARD"),
    ("不查收口门",
     'is_ship = t["delivery_use"] == "deliverable" or t["cadence_round"].startswith("ship")',
     "is_ship = False", "ship_unknown", "BASELINE-SHIP-NOT-GREEN"),
    ("不查极速哨兵", 'if verified == "是":', "if False:",
     "t_missing", "TURBO-SENTINEL-MISSING"),
]
SRC = open(REG_AUDIT, encoding="utf-8").read()
MUT_DIR = os.path.join(TMP, "_mutated")
if os.path.isdir(MUT_DIR):
    shutil.rmtree(MUT_DIR)
os.makedirs(MUT_DIR)
for label, old, new, fixture, code in MUTATIONS:
    check(old in SRC, "变异锚点存在：%s" % label)
    mpath = os.path.join(MUT_DIR, "mut_%s.py" % re.sub(r"\W", "_", label))
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(SRC.replace(old, new, 1))
    p = subprocess.run([sys.executable, "-X", "utf8", mpath, os.path.join(TMP, fixture), "--json"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        mcodes = codes_of(json.loads(p.stdout.decode("utf-8", "replace")))
    except ValueError:
        mcodes = ["<JSON 解析失败>"]
    check(code not in mcodes, "变异 [%s]：%s 必须失去命中（实际 %s）" % (label, code, mcodes))

# ------------------------------------------------------------------ 收尾
print("")
print("PASS %d / FAIL %d" % (len(PASSES), len(FAILS)))
if FAILS:
    print("失败清单：")
    for f in FAILS:
        print("  FAIL %s" % f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILS else 0)
