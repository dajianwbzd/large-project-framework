#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_case_audit.py — `case_audit.py` 的回归 + 变异对拍（纯 stdlib，不依赖 pytest）

为什么有这个脚本
----------------
`case_audit.py` 的多数判据**在真实工程上一次都跑不到**：真实工程还没有用例库。
也就是说「anti_mock 为空侦测」「来源指针悬空侦测」「格式旧不逐行重复报」这些分支
**从没被任何真实数据执行过**——发布时它们是**未验证代码**。

两条判据缺一不可（本框架踩过的坑）：
  ① **能拦**：每个检查都要有一个**故意让它失败**的夹具。只跑真实工程 = 只证明「不报错」。
  ② **不乱拦**：还要有一个**干净夹具**，断言 0 ERROR 0 WARN。
     变异只能证明 ①，所以「改了该报」与「改了不该报」两类断言在本文件里**成对出现**。

第三层：**检查器自身的变异**（对应 `mutate_recall_checker.py` 的做法）——
把 `case_audit.py` 改瞎三处，断言对应的夹具**必须失去命中**。
没有这一层，上面所有断言可能只是"夹具恰好让脚本退出 1"。

用法
----
    python test_case_audit.py          # 跑全部，失败则非零退出
    python test_case_audit.py -v       # 打印每条实际结果
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 回归与脚本同目录（草稿期）或脚本在上层（框架 `scripts/tests/` 期）——两种布局都要能跑。
CASE_AUDIT = next((p for p in (os.path.join(HERE, "case_audit.py"),
                               os.path.join(HERE, os.pardir, "case_audit.py"))
                   if os.path.exists(p)), os.path.join(HERE, "case_audit.py"))
TMP = os.path.join(HERE, "_fixture_tmp")

HEADER = "id | scope | method | data | anti_mock | source | last"

CLEAN_ROWS = [
    "CAS-001 | card-001 | 等价类+边界值 | 金额 0/1/上限/上限+1 各一次 | 写入后直查库核余额；越权用另一账号复读应 403 | AC:REQ-1.1 | pass",
    "CAS-002 | tc-001 | 状态迁移+穿透 | 状态机 5 态各走一次，非法转换 3 条 | 库内状态字段直查 + 拦截器日志留痕 | 发现:tc-001#F-01 | pass",
    "CAS-003 | global | 变异 | seed 固定 42 跑全量 | canary 值 9999 不得出现在响应体 | 反馈:card-001 用户三次反馈列表不刷新 | pass",
]

FINDINGS = """# 发现项

<!-- machine:findings -->
id | severity | owner | ref | state | anchor | entry
F-01 | required | B | card-002 | open | repos/app/src/list.py:88 | 复跑 test_refresh
F-02 | required | B | card-002 | open | repos/app/src/list.py:120 | 复跑 test_refresh_dup
F-03 | nit | C | - | accepted | 未知 | 未复现
<!-- /machine:findings -->
"""

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
def make_fixture(name, rows=None, header=HEADER, lib=True, block=True, tc=True,
                 findings=FINDINGS, encoding="utf-8", raw_lib=None, piped=False):
    root = os.path.join(TMP, name)
    if os.path.isdir(root):
        shutil.rmtree(root)
    base = os.path.join(root, "project-state")
    os.makedirs(os.path.join(base, "代码", "测试"))
    os.makedirs(os.path.join(base, "需求卡", "card-001"))
    os.makedirs(os.path.join(base, "需求卡", "card-002"))
    if tc:
        os.makedirs(os.path.join(base, "测试卡", "tc-001"))
        with open(os.path.join(base, "测试卡", "tc-001", "tc.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nid: tc-001\nstatus: in_progress\n---\n")
        with open(os.path.join(base, "测试卡", "tc-001", "发现项.md"), "w", encoding="utf-8") as fh:
            fh.write(findings)
    if lib:
        if raw_lib is not None:
            text = raw_lib
        else:
            body = list(rows if rows is not None else CLEAN_ROWS)
            hdr = header
            if piped:
                hdr = "| " + header + " |"
                body = ["| " + r + " |" for r in body]
            parts = ["# 用例库", "", "> 人读表照旧：一段一段写，机器只读下面的机读块。", ""]
            if block:
                parts += ["```text", "<!-- machine:cases -->", hdr]
                parts += body
                parts += ["<!-- /machine:cases -->", "```"]
            text = "\n".join(parts) + "\n"
        with open(os.path.join(base, "代码", "测试", "用例库.md"), "w", encoding=encoding) as fh:
            fh.write(text)
    return root


def run(root, *args, env=None):
    cmd = [sys.executable, "-X", "utf8", CASE_AUDIT, root] + list(args)
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       cwd=os.path.dirname(CASE_AUDIT), env=e)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def run_json(root, *args):
    code, sout, serr = run(root, "--json", *args)
    try:
        data = json.loads(sout)
    except ValueError:
        print("JSON 解析失败：\n%s\n%s" % (sout, serr))
        raise
    return code, data


def mutate(row_index, column, value):
    """把干净夹具的第 N 行某列换掉（单点变异）。"""
    rows = list(CLEAN_ROWS)
    cells = [c.strip() for c in rows[row_index].split("|")]
    cells[column] = value
    rows[row_index] = " | ".join(cells)
    return rows


def expect(root, want_exit, want_code, sev=None, absent=()):
    c, data = run_json(root)
    codes = [i["code"] for i in data["items"]]
    tag = os.path.basename(root)
    check(c == want_exit, "%s: 退出码 %s（实际 %s）" % (tag, want_exit, c))
    check(want_code in codes, "%s: 命中 %s（实际 %s）" % (tag, want_code, codes))
    for a in absent:
        check(a not in codes, "%s: **不许**报 %s（实际 %s）" % (tag, a, codes))
    if sev:
        check(data["counts"][sev] >= 1, "%s: 至少 1 个 %s" % (tag, sev))
    return data


# ------------------------------------------------------------------ 1. 干净夹具
print("== 干净夹具 ==")
CLEAN = make_fixture("clean")
c, data = run_json(CLEAN)
check(c == 0, "干净：退出码 0（实际 %s）" % c)
check(data["ok"] is True, "干净：ok=True")
check(data["cases"] == 3, "干净：3 条用例（实际 %s）" % data["cases"])
check(data["counts"] == {"ERROR": 0, "WARN": 0, "INFO": 0},
      "干净：0 ERROR / 0 WARN / 0 INFO（实际 %s）" % data["counts"])
_c, sout, _e = run(CLEAN)
check("带反 mock 判据的用例 3/3" in sout, "干净：印出带反 mock 判据的用例数")
# 行首行尾带 `|` 的写法（表头与数据行同时带）同样干净——剥边规则跟随表头
c, data = run_json(make_fixture("clean_piped", piped=True))
check(c == 0 and data["cases"] == 3, "干净（带边 `|` 写法）：退出码 0 / 3 条用例")

# ------------------------------------------------------------------ 2. 单点变异（该报的必须报）
print("== 单点变异（该报的必须报）==")
CASES_MUT = [
    ("anti_mock 空", "m_antimock_empty", 0, 4, "", "CASE-ANTIMOCK-MISSING", "ERROR"),
    ("anti_mock 占位 -", "m_antimock_dash", 0, 4, "-", "CASE-ANTIMOCK-MISSING", "ERROR"),
    ("anti_mock 写「无」", "m_antimock_wu", 0, 4, "无", "CASE-ANTIMOCK-MISSING", "ERROR"),
    ("data 空", "m_data_empty", 1, 3, "-", "CASE-DATA-MISSING", "ERROR"),
    ("method 空", "m_method_empty", 1, 2, "-", "CASE-METHOD-MISSING", "ERROR"),
    ("source 空", "m_source_empty", 1, 5, "-", "CASE-SOURCE-MISSING", "ERROR"),
    ("source 凭空前缀", "m_source_head", 1, 5, "随手:REQ-1.1", "CASE-SOURCE-MISSING", "ERROR"),
    ("source 指向不存在的测试卡", "m_source_tc", 1, 5, "发现:tc-009#F-01", "CASE-SOURCE-DANGLING", "ERROR"),
    ("source 指向不存在的发现项", "m_source_fid", 1, 5, "发现:tc-001#F-99", "CASE-SOURCE-DANGLING", "ERROR"),
    ("id 为空", "m_id_empty", 2, 0, "", "CASE-ID-EMPTY", "ERROR"),
    ("id 重复", "m_id_dup", 2, 0, "CAS-001", "CASE-ID-DUP", "ERROR"),
    ("scope 悬空卡", "m_scope_card", 0, 1, "card-099", "CASE-SCOPE-DANGLING", "ERROR"),
    ("scope 非卡非轮", "m_scope_bad", 0, 1, "模块A", "CASE-SCOPE-DANGLING", "ERROR"),
    ("scope 空", "m_scope_empty", 0, 1, "-", "CASE-SCOPE-MISSING", "ERROR"),
    ("last 空", "m_last_empty", 0, 6, "-", "CASE-LAST-MISSING", "ERROR"),
    ("last 非法值", "m_last_invalid", 0, 6, "done", "CASE-LAST-INVALID", "ERROR"),
]
for label, fname, ri, col, val, code, sev in CASES_MUT:
    expect(make_fixture(fname, rows=mutate(ri, col, val)), 1, code, sev=sev)

# 字段里混进 `|`
rows_bad = list(CLEAN_ROWS)
rows_bad[0] = "CAS-001 | card-001 | 等价类 | 金额 0|1|上限 | 判据 | AC:REQ-1.1 | pass"
expect(make_fixture("m_pipe", rows=rows_bad), 1, "CASE-FIELD-PIPE", sev="ERROR")
# 表头不带边、数据行带边 = 风格混用，同样报（格子数与表头不一致）
expect(make_fixture("m_pipe2", rows=["| " + r + " |" for r in CLEAN_ROWS]), 1,
       "CASE-FIELD-PIPE", sev="ERROR")

# ------------------------------------------------------------------ 3. 成对反向断言（不该报的不许报）
print("== 成对反向断言（不该报的不许报）==")
data = expect(make_fixture("n_last_unknown", rows=mutate(0, 6, "unknown")), 0,
              "CASE-LAST-UNKNOWN", sev="WARN",
              absent=("CASE-LAST-INVALID", "CASE-LAST-MISSING"))
expect(make_fixture("n_id_fmt", rows=mutate(0, 0, "CAS-9")), 0, "CASE-ID-FORMAT", sev="WARN",
       absent=("CASE-ID-DUP", "CASE-ID-EMPTY"))
rows = list(CLEAN_ROWS)
rows[2] = rows[2].replace("CAS-003", "CAS-000")
expect(make_fixture("n_id_order", rows=rows), 0, "CASE-ID-ORDER", sev="WARN",
       absent=("CASE-ID-DUP",))
expect(make_fixture("n_method_unknown", rows=mutate(0, 2, "玄学")), 0, "CASE-METHOD-UNKNOWN",
       sev="WARN", absent=("CASE-METHOD-MISSING",))
expect(make_fixture("n_feedback", rows=mutate(0, 5, "反馈:用户说列表不刷新")), 0,
       "CASE-FEEDBACK-NO-ANCHOR", sev="WARN",
       absent=("CASE-SOURCE-MISSING", "CASE-SOURCE-DANGLING"))
c, data = run_json(make_fixture("n_retired", rows=[r.replace(" | pass", " | retired") for r in CLEAN_ROWS]))
check(c == 0 and data["cases"] == 3, "retired 行不删：仍被统计、退出码 0（实际 %s）" % c)
c, data = run_json(make_fixture("n_global", rows=mutate(0, 1, "global")))
check(c == 0 and data["counts"] == {"ERROR": 0, "WARN": 0, "INFO": 0},
      "scope=global 合法：整个夹具干净放行（实际 %s）" % data["counts"])
# anti_mock 只要不是空/占位就放行（不判"判据够不够强"——那是测试设计判断，机器判不了）
c, data = run_json(make_fixture("n_antimock_short", rows=mutate(0, 4, "直查库")))
check(c == 0 and data["counts"]["ERROR"] == 0,
      "anti_mock 填了短判据（直查库）也放行：不判强弱，退出码 0")

# ------------------------------------------------------------------ 4. 格式旧 ≠ 内容缺
print("== 格式旧 ≠ 内容缺 ==")
OLD_HEADER = "id | scope | method | data | source"
OLD_ROWS = [
    "CAS-001 | card-001 | 等价类 | 金额 0/1 | AC:REQ-1.1",
    "CAS-002 | tc-001 | 状态迁移 | 状态机 5 态 | 发现:tc-001#F-01",
]
root = make_fixture("stale", header=OLD_HEADER, rows=OLD_ROWS)
c, data = run_json(root)
codes = [i["code"] for i in data["items"]]
check(c == 1, "旧格式：退出码 1（实际 %s）" % c)
check("CASES-FORMAT-STALE" in codes, "旧格式：整块报一次 CASES-FORMAT-STALE")
check(codes.count("CASES-FORMAT-STALE") == 1,
      "旧格式：**只报一次**（实际 %d）" % codes.count("CASES-FORMAT-STALE"))
check("CASE-ANTIMOCK-MISSING" not in codes, "旧格式：**不许**同时报 anti_mock 缺（格式旧 ≠ 内容缺）")
check("CASE-LAST-MISSING" not in codes, "旧格式：**不许**同时报 last 缺")
check("CASE-SOURCE-DANGLING" not in codes, "旧格式：还认得的列继续查（来源指针仍核）")
c, data = run_json(root, "--allow-unmigrated")
check(c == 0, "旧格式 + --allow-unmigrated：降级为提示、退出码 0（实际 %s）" % c)
check(data["counts"]["WARN"] == 1, "旧格式 + 迁移期：1 个 WARN")
# 列错序也认得出（按列名映射，不是按位置）：scope/id 对调 → 不许报错
SWAP_HEADER = "scope | id | method | data | anti_mock | source | last"
SWAP_ROWS = ["card-001 | CAS-001 | 等价类 | 金额 0/1 | 直查库 | AC:REQ-1.1 | pass"]
c, data = run_json(make_fixture("swap", header=SWAP_HEADER, rows=SWAP_ROWS))
check(c == 0 and data["counts"]["ERROR"] == 0, "列错序：按列名映射，退出码 0（实际 %s）" % c)
# 块没闭合
expect(make_fixture("unclosed", raw_lib="# 用例库\n\n<!-- machine:cases -->\n%s\n%s\n" % (HEADER, CLEAN_ROWS[0])),
       1, "CASES-BLOCK-UNCLOSED", sev="ERROR")

# ------------------------------------------------------------------ 5. 三态退出码
print("== 三态退出码 ==")
root = make_fixture("nolib_none", lib=False, tc=False)
c, data = run_json(root)
codes = [i["code"] for i in data["items"]]
check(c == 2, "无库无测试卡：退出码 2（实际 %s）" % c)
check(data["ok"] is None, "无库无测试卡：ok=None（不是「没问题」）")
check("CASES-NO-LIB" in codes, "无库：报 CASES-NO-LIB")
check("CASES-MISSING-WITH-TC" not in codes, "无库：没有测试卡时不报「有轮无库」")
_c, sout, _e = run(root)
check("无法检查" in sout, "无库：人读输出明写「无法检查」")
check("不是「没问题」" in sout or "不是" in sout, "无库：人读输出明写「不是没问题」")

root = make_fixture("nolib_tc", lib=False, tc=True)
c, data = run_json(root)
check(c == 1, "有测试卡却无库：退出码 1（实际 %s）" % c)
check("CASES-MISSING-WITH-TC" in [i["code"] for i in data["items"]],
      "有测试卡却无库：报 CASES-MISSING-WITH-TC")

root = make_fixture("noblock", block=False)
c, data = run_json(root)
check(c == 1 and "CASES-NO-BLOCK" in [i["code"] for i in data["items"]],
      "有库无机读块：退出码 1 + CASES-NO-BLOCK")
c, data = run_json(root, "--allow-unmigrated")
check(c == 0 and data["counts"]["WARN"] == 1, "有库无机读块 + 迁移期：降级 WARN、退出码 0")

# 参数面
root = make_fixture("cli")
c, sout, _e = run(root, "--check")
check(c == 0 and "用例 3 条" in sout, "--check：只印计数")
shutil.copytree(os.path.join(root, "project-state"), os.path.join(root, "副本"))
c, data = run_json(root, "--lib", os.path.join(root, "副本", "代码", "测试", "用例库.md"))
check(data["lib"].endswith("用例库.md") and data["cases"] == 3, "--lib：可换用例库路径")
c, _sout, _e = run(root, "--no-such-flag")
check(c == 2, "未知参数：退出码 2")
c, _sout, _e = run(root, "--help")
check(c == 0, "--help：退出码 0")

# ------------------------------------------------------------------ 6. 热区扫查
print("== 热区扫查 ==")
root = make_fixture("hot")
c, sout, _e = run(root, "--hotspots")
check(c == 0, "热区：退出码 0（实际 %s）" % c)
check("repos/app" in sout, "热区：按路径前缀归并（repos/app）")
check("已覆盖 1/2" in sout, "热区：list.py 两条发现项里 1 条已有回归用例")
check("F-02" in sout and "F-03" in sout, "热区：印出尚无回归用例的发现项")
check("锚点未知" in sout, "热区：锚点未知的发现项单独计数、不许并进路径前缀")

root_no = make_fixture("hot_none", tc=True, findings="# 发现项\n\n（还没有机读块）\n")
c, sout, _e = run(root_no, "--hotspots")
check(c == 2, "热区：无任何机读块 → 退出码 2（实际 %s）" % c)
check("未查过" in sout, "热区：印「未查过」而不是「0 个热区」")
check("0 个热区" in sout, "热区：明写「未查过 ≠ 0 个热区」")

# ------------------------------------------------------------------ 7. 编码
print("== 编码 ==")
root = make_fixture("gbk", encoding="gbk")
c, data = run_json(root)
check(c == 0 and data["counts"]["ERROR"] == 0, "GBK 用例库：能读、退出码 0")
c, _sout, serr = run(root, env={"PYTHONIOENCODING": "gbk"})
check(c == 0, "stdout 强指 GBK：不崩、退出码 0")
check("Traceback" not in serr, "stdout 强指 GBK：无 Traceback")

# ------------------------------------------------------------------ 8. 检查器自身变异
print("== 检查器自身变异（反向断言要有牙）==")
MUTATIONS = [
    ("不查 anti_mock", "if a.lower() in ANTIMOCK_EMPTY:", "if False:",
     "m_antimock_empty", "CASE-ANTIMOCK-MISSING"),
    ("不查来源前缀", "if s in EMPTY_OK or head not in SOURCES:", "if False:",
     "m_source_empty", "CASE-SOURCE-MISSING"),
    ("不查列集（格式旧）", "if missing_cols or extra_cols:", "if False:",
     "stale", "CASES-FORMAT-STALE"),
]
SRC = open(CASE_AUDIT, encoding="utf-8").read()
MUT_DIR = os.path.join(TMP, "_mutated")
if os.path.isdir(MUT_DIR):
    shutil.rmtree(MUT_DIR)
os.makedirs(MUT_DIR)
MUT_PATHS = {}
for label, old, new, fixture, code in MUTATIONS:
    check(old in SRC, "变异锚点存在：%s" % label)
    path = os.path.join(MUT_DIR, "mut_%d.py" % len(MUT_PATHS))
    MUT_PATHS[label] = path
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(SRC.replace(old, new, 1))
    p = subprocess.run([sys.executable, "-X", "utf8", path, os.path.join(TMP, fixture), "--json"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        mcodes = [i["code"] for i in json.loads(p.stdout.decode("utf-8", "replace"))["items"]]
    except ValueError:
        mcodes = []
    check(code not in mcodes, "变异「%s」被抓住：%s 失去命中（%s）" % (label, code, mcodes))

# 反向控制：变异不许把干净夹具也弄红
p = subprocess.run([sys.executable, "-X", "utf8", MUT_PATHS["不查 anti_mock"], CLEAN, "--json"],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
check(p.returncode == 0, "变异后干净夹具仍 0（没有把地板打穿）")

# ------------------------------------------------------------------ 清场
# 与 test_recall.py 一致：全绿才清夹具（失败时留着便于复现），避免给工作区留 tmp
if not FAILS:
    shutil.rmtree(TMP, ignore_errors=True)

# ------------------------------------------------------------------ 汇总
print("")
print("断言：%d 通过 / %d 失败" % (len(PASSES), len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  FAIL %s" % f)
    sys.exit(1)
print("全绿。")
