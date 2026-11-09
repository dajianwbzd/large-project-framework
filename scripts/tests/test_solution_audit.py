#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""solution_audit.py 的回归用例（v5.16.0「方案视图区」补丁）

覆盖九组：
  1 干净不误报（完整模式齐备 → exit 0 且零条目）
  2 单点变异（删目录 / 子目录 / README / 台账 / 登记行 / 标注 / 图未登记 / 根下游离件）
  3 成对反向（完整模式必建 vs 快速模式未建 = 无法检查）
  4 注释 / 散文伪命中（只在正文提到文件名，不算登记）
  5 大小写与空白（Windows 大小写不敏感；台账单元格带反引号与空格）
  6 格式旧 ≠ 内容缺（五列表头只报 WARN，不误判缺登记）
  7 三态退出码 0 / 1 / 2（含 root 传 project-state 的归一）
  8 顺序与编码（GBK 台账与 GBK 原件）
  9 检查器自身变异（把错误码字符串改掉后必须失去检测力）

全绿才清场；失败保留夹具便于排查。
"""

import json
import os
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "solution_audit.py")

sys.path.insert(0, os.path.dirname(SCRIPT))
from solution_audit import VERSION as SCRIPT_VERSION  # noqa: E402
TMP = os.path.join(HERE, "_fixture_sol_tmp")

PASS = 0
FAILS = []

README_MARKS = (
    "方案视图区",
    "原始资料",
    "产品稿",
    "原型图",
    "真源",
    "最后刷新",
)


def check(cond, label):
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(label)
        sys.stdout.write("FAIL: %s\n" % label)


def write(path, text, encoding="utf-8"):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding=encoding, newline="\n") as fh:
        fh.write(text)


def config(mode="full", with_area=True):
    cfg = {
        "schema_version": "1",
        "mode": mode,
        "paths": {
            "project_state": "project-state",
            "layout": {"开发记忆区": "project-state"},
            "根文件": {"framework": "agent.md", "entry": ""},
        },
    }
    if with_area:
        cfg["paths"]["方案"] = "方案"
    return cfg


LEDGER_HEADER = (
    "| 文件名 | 收到时间 | 来源 | 类型 | 已消化到哪 | 备注 |\n"
    "|---|---|---|---|---|---|\n"
)
LEDGER_ROW = "| 客户方案v1.docx | 2026-10-23 | 客户 | 客户方案 | project-state/需求卡/REQ-001.md | - |\n"
PROTO_HEADER = "| 页面/屏 | 档位 | 导出日期 | 源路径 | 备注 |\n|---|---|---|---|---|\n"
PROTO_ROW = (
    "| 登录 | 375 | 2026-10-23 | project-state/契约/UI资源/prototypes/prototype.spec.json | 低保真 |\n"
)
DRAFT_MARKS = "导出日期：2026-10-23\n源路径：project-state/契约/产品需求文档.md\n\n# PRD 摘要\n"


def make_project(
    name,
    mode="full",
    area=True,
    subdirs=("原始资料", "产品稿", "原型图"),
    readme=True,
    ledger=True,
    ledger_text=None,
    origin_files=("客户方案v1.docx",),
    drafts=None,
    images=None,
    proto_ledger=True,
    proto_ledger_text=None,
    stray=None,
    cfg=True,
    area_registered=True,
    ledger_encoding="utf-8",
    origin_encoding="utf-8",
):
    root = os.path.join(TMP, name)
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(os.path.join(root, "project-state"), exist_ok=True)
    if cfg:
        write(
            os.path.join(root, "project-state", "config.json"),
            json.dumps(config(mode, area_registered), ensure_ascii=False, indent=2),
        )
    if not area:
        return root

    sol = os.path.join(root, "方案")
    for sub in subdirs:
        os.makedirs(os.path.join(sol, sub), exist_ok=True)

    if readme:
        body = "# 方案视图区（方案/）\n\n" + "\n".join("- %s" % m for m in README_MARKS) + "\n"
        write(os.path.join(sol, "README.md"), body)

    for fname in origin_files:
        write(os.path.join(sol, "原始资料", fname), "客户提供的原始文件示例内容\n", origin_encoding)

    if ledger:
        text = ledger_text if ledger_text is not None else LEDGER_HEADER + LEDGER_ROW
        write(os.path.join(sol, "原始资料", "清单.md"), text, ledger_encoding)

    for dname, body in (drafts or {"PRD摘要.md": DRAFT_MARKS}).items():
        write(os.path.join(sol, "产品稿", dname), body)

    for iname in images or ("登录@375.png",):
        write(os.path.join(sol, "原型图", iname), "PNG-PLACEHOLDER\n")

    if proto_ledger:
        text = proto_ledger_text if proto_ledger_text is not None else PROTO_HEADER + PROTO_ROW
        write(os.path.join(sol, "原型图", "清单.md"), text)

    if stray:
        for sname in stray:
            write(os.path.join(sol, sname), "stray\n")
    return root


def run(root, *args, script=None):
    return subprocess.run(
        [sys.executable, "-X", "utf8", script or SCRIPT, root] + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=os.path.dirname(SCRIPT),
    )


def run_json(root, *args, script=None):
    proc = run(root, "--json", *args, script=script)
    try:
        return proc, json.loads(proc.stdout)
    except ValueError:
        return proc, None


def codes_of(data):
    if not data:
        return set()
    return {item.get("code") for item in data.get("items", [])}


def expect(root, want_exit, want_codes=(), absent_codes=(), label="", script=None):
    proc, data = run_json(root, script=script)
    code_set = codes_of(data)
    ok_exit = proc.returncode == want_exit
    ok_codes = all(c in code_set for c in want_codes)
    ok_absent = all(c not in code_set for c in absent_codes)
    if not (ok_exit and ok_codes and ok_absent):
        sys.stdout.write(
            "  got exit=%s codes=%s\n  want exit=%s codes=%s absent=%s\n"
            % (proc.returncode, sorted(code_set), want_exit, list(want_codes), list(absent_codes))
        )
    check(ok_exit, "%s：退出码应为 %s（实际 %s）" % (label, want_exit, proc.returncode))
    check(ok_codes, "%s：应含 %s（实际 %s）" % (label, list(want_codes), sorted(code_set)))
    check(ok_absent, "%s：不应含 %s（实际 %s）" % (label, list(absent_codes), sorted(code_set)))
    return data


def group_clean():
    root = make_project("clean")
    data = expect(root, 0, label="1 干净完整模式", absent_codes=("SOL-MISSING",))
    check(data is not None and data.get("counts", {}).get("ERROR") == 0, "1 干净：ERROR 必须为 0")
    check(data is not None and data.get("ok") is True, "1 干净：ok 必须为 True")
    check(data is not None and data.get("version") == SCRIPT_VERSION,
          "1 干净：version 应为 %s" % SCRIPT_VERSION)
    # 快速模式但已建好 → 同样干净
    quick = make_project("clean_quick", mode="quick")
    expect(quick, 0, label="1 快速模式已建", absent_codes=("SOL-NOT-ENABLED",))


def group_mutations():
    expect(
        make_project("mut_no_area", area=False),
        1,
        ("SOL-MISSING",),
        label="2 删整个方案目录",
    )
    no_sub = make_project("mut_no_sub", subdirs=("原始资料", "产品稿"))
    shutil.rmtree(os.path.join(no_sub, "方案", "原型图"))
    expect(no_sub, 1, ("SOL-SUBDIR",), label="2 缺原型图子目录")
    expect(
        make_project("mut_no_readme", readme=False),
        1,
        ("SOL-README",),
        label="2 缺 README",
    )
    expect(
        make_project("mut_no_ledger", ledger=False),
        1,
        ("SOL-NO-LEDGER",),
        label="2 缺原始资料台账",
    )
    expect(
        make_project("mut_unregistered", ledger=True, ledger_text=LEDGER_HEADER),
        1,
        ("SOL-LEDGER-MISSING",),
        label="2 原件未登记",
    )
    expect(
        make_project("mut_export", drafts={"PRD摘要.md": "# 摘要\n只有正文，没有标注\n"}),
        1,
        ("SOL-EXPORT-UNMARKED",),
        label="2 导出件缺标注",
    )
    expect(
        make_project("mut_proto", images=("登录@375.png", "订单@1440.png")),
        0,
        ("SOL-PROTO-UNLISTED",),
        label="2 图未登记（WARN 不拦）",
    )
    expect(
        make_project("mut_stray", stray=("临时.txt",)),
        0,
        ("SOL-ROOT-STRAY",),
        label="2 根下游离件",
    )
    expect(
        make_project("mut_thin_readme", readme=False),
        1,
        ("SOL-README",),
        label="2 README 缺失优先于内容过薄",
    )


def group_pairs():
    missing_full = make_project("pair_full", area=False, mode="full")
    expect(missing_full, 1, ("SOL-MISSING",), label="3 完整模式未建")
    missing_quick = make_project("pair_quick", area=False, mode="quick")
    proc, data = run_json(missing_quick)
    check(proc.returncode == 2, "3 快速模式未建：退出码应为 2（实际 %s）" % proc.returncode)
    check(data is not None and data.get("ok") is None, "3 快速模式未建：ok 必须为 None（无法检查）")
    check("SOL-NOT-ENABLED" in codes_of(data), "3 快速模式未建：应报 SOL-NOT-ENABLED")
    # 成对反向：同一个缺件在完整模式是 ERROR、在快速模式是 WARN 且不拦
    quick_bad = make_project("pair_quick_bad", mode="quick", ledger=False)
    proc2, data2 = run_json(quick_bad)
    check(proc2.returncode == 0, "3 快速模式缺台账：不应拦成 1（实际 %s）" % proc2.returncode)
    check("SOL-NO-LEDGER" in codes_of(data2), "3 快速模式缺台账：应记 WARN")
    full_bad = make_project("pair_full_bad", mode="full", ledger=False)
    expect(full_bad, 1, ("SOL-NO-LEDGER",), label="3 完整模式缺台账")


def group_prose():
    # 只在正文散文里提到文件名 → 不算登记（登记只认表格数据行）
    prose = "# 清单\n\n客户给过 客户方案v1.docx 一份，稍后补登记。\n"
    expect(
        make_project("prose", ledger_text=prose),
        1,
        ("SOL-LEDGER-MISSING",),
        label="4 散文提到文件不算登记",
    )
    # 占位行（尖括号 / 示例）不算台账悬空
    placeholder = LEDGER_HEADER + "| <文件名> |  |  |  |  |  |\n| 示例：xxx.docx | - | - | - | - | - |\n"
    expect(
        make_project("prose_placeholder", origin_files=(), ledger_text=placeholder),
        0,
        label="4 占位行不误报悬空",
        absent_codes=("SOL-LEDGER-DANGLING",),
    )


def group_case():
    # 文件是 Login@375.PNG，清单写 login / 375 → 大小写与 @ 拆格都能认出
    root = make_project(
        "case",
        images=("Login@375.PNG",),
        proto_ledger_text=PROTO_HEADER + "| Login | 375 | 2026-10-23 | 设计稿 | 低保真 |\n",
    )
    expect(root, 0, label="5 图名大小写", absent_codes=("SOL-PROTO-UNLISTED",))
    # 台账单元格带反引号与空白
    ledger = LEDGER_HEADER + "|   `客户方案v1.docx`   | 2026-10-23 | 客户 | 客户方案 | 未消化 | - |\n"
    expect(
        make_project("case_spaces", ledger_text=ledger),
        0,
        label="5 台账单元格带反引号空格",
        absent_codes=("SOL-LEDGER-MISSING", "SOL-LEDGER-DANGLING"),
    )
    # 原件大小写不同但台账写原样 → 不应报悬空
    ledger2 = LEDGER_HEADER + "| 客户方案V1.DOCX | 2026-10-23 | 客户 | 客户方案 | 未消化 | - |\n"
    expect(
        make_project("case_origin", ledger_text=ledger2),
        0,
        label="5 原件名大小写",
        absent_codes=("SOL-LEDGER-DANGLING", "SOL-LEDGER-MISSING"),
    )


def group_format():
    # 五列表头（旧格式）：只报 WARN，登记行仍算数
    old = (
        "| 文件名 | 收到时间 | 来源 | 类型 | 备注 |\n"
        "|---|---|---|---|---|\n"
        "| 客户方案v1.docx | 2026-10-23 | 客户 | 客户方案 | - |\n"
    )
    data = expect(
        make_project("fmt_old", ledger_text=old),
        0,
        ("SOL-LEDGER-HEADER",),
        absent_codes=("SOL-LEDGER-MISSING",),
        label="6 五列表头只报 WARN",
    )
    check(data is not None and data.get("counts", {}).get("ERROR") == 0, "6 五列表头：ERROR 必须为 0")


def group_three_state():
    root = make_project("state_no_cfg", cfg=False)
    proc, data = run_json(root)
    check(proc.returncode == 2, "7 无 config.json：退出码 2（实际 %s）" % proc.returncode)
    check(data is not None and data.get("ok") is None, "7 无 config.json：ok 为 None")
    # 传 project-state 也要能归一
    good = make_project("state_normalize")
    expect(os.path.join(good, "project-state"), 0, label="7 root 传 project-state 归一")
    # --check 只印一行
    proc2 = run(good, "--check")
    lines = [ln for ln in proc2.stdout.splitlines() if ln.strip()]
    check(len(lines) == 1, "7 --check 应只印一行（实际 %d 行）" % len(lines))
    check("ERROR 0" in proc2.stdout, "7 --check 应印 ERROR 0")
    # 不存在的根
    proc3 = run(os.path.join(TMP, "no_such_root"))
    check(proc3.returncode == 2, "7 根不存在：退出码 2（实际 %s）" % proc3.returncode)


def group_encoding():
    root = make_project("enc_gbk", ledger_encoding="gbk", origin_encoding="gbk")
    expect(root, 0, label="8 GBK 台账与 GBK 原件", absent_codes=("SOL-LEDGER-MISSING",))
    # GBK 台账里写 GBK 文件名 + 文件是 UTF-8 → 仍需按名字匹配
    root2 = make_project("enc_gbk2", ledger_encoding="gbk")
    expect(root2, 0, label="8 GBK 台账 / UTF-8 原件", absent_codes=("SOL-LEDGER-MISSING",))


def group_self_mutation():
    mut_dir = os.path.join(TMP, "mut_script")
    os.makedirs(mut_dir, exist_ok=True)
    mutated = os.path.join(mut_dir, "solution_audit_mut.py")
    with open(SCRIPT, "r", encoding="utf-8") as fh:
        source = fh.read()
    check("SOL-MISSING" in source, "9 源码里应有 SOL-MISSING")
    with open(mutated, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(source.replace("SOL-MISSING", "SOL-MISSINGX"))
    root = make_project("mut_target", area=False)
    proc, data = run_json(root, script=mutated)
    check(proc.returncode == 1, "9 变异版仍应判 ERROR（实际 %s）" % proc.returncode)
    check("SOL-MISSING" not in codes_of(data), "9 变异版必须失去 SOL-MISSING 检测力")
    check("SOL-MISSINGX" in codes_of(data), "9 变异版应报成 SOL-MISSINGX")


def main():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP, exist_ok=True)
    check(os.path.isfile(SCRIPT), "被测脚本存在")
    groups = (
        group_clean,
        group_mutations,
        group_pairs,
        group_prose,
        group_case,
        group_format,
        group_three_state,
        group_encoding,
        group_self_mutation,
    )
    for fn in groups:
        sys.stdout.write("\n== %s ==\n" % fn.__name__)
        fn()
    sys.stdout.write("\n" + "-" * 58 + "\n")
    sys.stdout.write("PASS %d / FAIL %d\n" % (PASS, len(FAILS)))
    if FAILS:
        sys.stdout.write("失败项：\n")
        for item in FAILS:
            sys.stdout.write("  - %s\n" % item)
        sys.stdout.write("夹具保留在 %s\n" % TMP)
        return 1
    shutil.rmtree(TMP)
    sys.stdout.write("全绿，夹具已清场。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
