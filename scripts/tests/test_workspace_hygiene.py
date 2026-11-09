#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workspace_hygiene.py 的回归测试（v4.4.0 补）。

为什么这个测试必须存在：本脚本会**真删/真搬**客户工作区里的文件，而它对外承诺的
「默认只报告」「证据永不清理」是**安全承诺**。承诺一旦与代码脱钩，就是假保护——
比没有保护更危险（人会放心地加 --apply）。所以两条承诺各有一个会变红的断言。

覆盖：
- P0-1  `--archive` / `--archive-delivery` 不带 `--apply` 时**不得**搬动任何文件。
- P0-2  `证据/` 下的任何东西（含 `__pycache__` / `Thumbs.db`）在 `--apply` 下必须存活。
- P0-3  `project-state/` 正式产出区递归硬保护（子目录内 `__pycache__` / 备份件必须存活），
        但其**根级**散落的一次性中间件仍必须可清（§9.9 能力不能被一刀切保护废掉）。
-       Windows GBK 控制台下 `--write` 不得 UnicodeEncodeError 崩掉。
- 分级授权：L1/L2/L2.5 删、L3/L4 归档、在飞资产保留。

用法：python scripts/tests/test_workspace_hygiene.py
退出码 0=全过；1=有失败（会把失败项打出来）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parents[1]
SCRIPT = Path(os.environ.get("HYG_SCRIPT") or (FRAMEWORK / "scripts" / "workspace_hygiene.py"))
ROOT = Path(tempfile.mkdtemp(prefix="hyg_test_"))
OLD = dt.datetime.now() - dt.timedelta(days=40)

fails: list = []
passes: list = []


def check(cond, label, detail=""):
    (passes if cond else fails).append(label)
    tail = f"  -- {detail}" if detail and not cond else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{tail}")


def touch(p: Path, text: str, old: bool = False):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if old:
        os.utime(p, (OLD.timestamp(), OLD.timestamp()))


def build():
    """搭一个最小框架工作区：每类候选都放一个，便于逐级验证。"""
    if ROOT.exists():
        shutil.rmtree(ROOT, ignore_errors=True)
    (ROOT / "project-state").mkdir(parents=True)
    # L1 垃圾（应删）
    touch(ROOT / "__pycache__" / "a.pyc", "x")
    # L2 tmp：超龄应删 / 在飞应留
    touch(ROOT / "tmp" / "old_dump.txt", "x", old=True)
    touch(ROOT / "tmp" / "backup_部署分片.zip", "x", old=True)
    # L2.5 根级一次性中间件（应删）
    touch(ROOT / "_20260101_scratch.ps1", "x")
    # L3 产物同族：旧版应归档，最新版留原位
    touch(ROOT / "产物" / "app-v1.0.zip", "x", old=True)
    touch(ROOT / "产物" / "app-v2.0.zip", "x")
    # L4 交付：旧版应归档且被认出「已发出副本」；发出记录是审计凭证
    touch(ROOT / "交付" / "方案书-v1.0.zip", "x", old=True)
    touch(ROOT / "交付" / "方案书-v2.0.zip", "x")
    touch(ROOT / "交付" / "发出记录.md", "2026-09-01 发出 方案书-v1.0.zip 给甲方，已回执\n")
    # P0-2 关键夹具：证据/ 里的垃圾与日志必须全部存活
    touch(ROOT / "需求卡" / "card-001" / "证据" / "__pycache__" / "t.pyc", "x")
    touch(ROOT / "需求卡" / "card-001" / "证据" / "运行日志.txt", "关键证据")
    touch(ROOT / "需求卡" / "card-001" / "证据" / "Thumbs.db", "x")
    touch(ROOT / "测试卡" / "tc-001" / "证据" / "__pycache__" / "u.pyc", "x")
    # P0-3 关键夹具：project-state/ 是正式产出区，除「根级散落一次性中间件」外一律硬保护
    touch(ROOT / "project-state" / "进度" / "__pycache__" / "x.pyc", "x")       # 原实现递归下钻会当垃圾删
    touch(ROOT / "project-state" / "进度" / "overall.json.tmp", "写入中")        # 正式产物的写入中临时件
    touch(ROOT / "project-state" / "契约" / "card-001.contract.bak", "备份")     # 正式产物的备份件
    touch(ROOT / "project-state" / "状态快照.json", "{}")                        # 新增正式产出（不在手工白名单）
    touch(ROOT / "project-state" / "_20260911_tmp.out", "x")                     # §9.9：根级散落中间件仍要能清


def build_registered():
    """项目自定域名（config paths 注册表）：默认 交付//产物/ 不存在时也不得漏判或写错域。"""
    if ROOT.exists():
        shutil.rmtree(ROOT, ignore_errors=True)
    (ROOT / "project-state").mkdir(parents=True)
    (ROOT / "project-state" / "config.json").write_text(
        json.dumps({"paths": {"产物": "内部产物/", "交付": "客户交付/"}}, ensure_ascii=False),
        encoding="utf-8")
    touch(ROOT / "内部产物" / "app-v1.0.zip", "x", old=True)
    touch(ROOT / "内部产物" / "app-v2.0.zip", "x")
    touch(ROOT / "客户交付" / "方案书-v1.0.zip", "x", old=True)
    touch(ROOT / "客户交付" / "方案书-v2.0.zip", "x")
    touch(ROOT / "客户交付" / "发出记录.md", "2026-09-01 发出 方案书-v1.0.zip 给甲方，已回执\n")


def run(*extra, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), str(ROOT), *extra],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def main() -> int:
    print("=" * 72)
    print(f"被测脚本：{SCRIPT}")
    print(f"沙箱：{ROOT}")
    print("=" * 72)

    print("\n测试 A：--apply --archive --archive-delivery（全量执行）")
    build()
    p = run("--apply", "--archive", "--archive-delivery", "--json")
    check(p.returncode == 0, "退出码 0", (p.stderr or "")[-400:])
    try:
        r = json.loads(p.stdout)
    except ValueError:
        check(False, "--json 可解析", (p.stdout or "")[:300])
        r = {}
    check(not (ROOT / "__pycache__").exists(), "L1 根级 __pycache__ 已删")
    check(not (ROOT / "tmp" / "old_dump.txt").exists(), "L2 超龄 tmp 已删")
    check((ROOT / "tmp" / "backup_部署分片.zip").exists(), "L2 在飞 tmp（backup/部署分片）保留")
    check(not (ROOT / "_20260101_scratch.ps1").exists(), "L2.5 一次性中间件已删")
    check(not (ROOT / "产物" / "app-v1.0.zip").exists()
          and (ROOT / "产物" / "归档" / "app-v1.0.zip").exists(), "L3 产物旧版已归档")
    check((ROOT / "产物" / "app-v2.0.zip").exists(), "L3 产物最新版留原位")
    check(not (ROOT / "交付" / "方案书-v1.0.zip").exists()
          and bool(list((ROOT / "交付" / "归档").rglob("方案书-v1.0.zip"))), "L4 交付旧版已归档")
    check((ROOT / "交付" / "发出记录.md").exists(), "L4 发出记录.md 在位（审计链）")
    print("  -- P0-2 硬保护 --")
    check((ROOT / "需求卡" / "card-001" / "证据" / "__pycache__" / "t.pyc").exists(),
          "证据/__pycache__/t.pyc 存活（P0-2）")
    check((ROOT / "需求卡" / "card-001" / "证据" / "运行日志.txt").exists(), "证据/运行日志.txt 存活")
    check((ROOT / "需求卡" / "card-001" / "证据" / "Thumbs.db").exists(), "证据/Thumbs.db 存活")
    check((ROOT / "测试卡" / "tc-001" / "证据" / "__pycache__" / "u.pyc").exists(),
          "测试卡 证据/__pycache__ 存活")
    skipped = {p.replace("\\", "/") for p, _ in r.get("protected_skipped", [])}
    check("需求卡/card-001/证据" in skipped and "测试卡/tc-001/证据" in skipped,
          f"报告披露两处硬保护目录（实际 {sorted(skipped)}）")

    print("  -- P0-3 project-state/ 正式产出区硬保护 --")
    check((ROOT / "project-state" / "进度" / "__pycache__" / "x.pyc").exists(),
          "project-state/进度/__pycache__/x.pyc 存活（递归下钻不再删正式产出区）")
    check((ROOT / "project-state" / "进度" / "__pycache__").exists(),
          "project-state/进度/__pycache__ 目录存活")
    check((ROOT / "project-state" / "进度" / "overall.json.tmp").exists(),
          "project-state/进度/overall.json.tmp 存活（正式产物写入中临时件）")
    check((ROOT / "project-state" / "契约" / "card-001.contract.bak").exists(),
          "project-state/契约/card-001.contract.bak 存活（正式产物备份件）")
    check((ROOT / "project-state" / "状态快照.json").exists(),
          "project-state/状态快照.json 存活（新增正式产出，不靠手工白名单）")
    check(not (ROOT / "project-state" / "_20260911_tmp.out").exists(),
          "§9.9：project-state 根级散落一次性中间件已删（能力未被废掉）")

    cand = {x["path"].replace("\\", "/") for k in ("junk", "litter", "litter_logs") for x in r.get(k, [])}
    cand |= {s.replace("\\", "/") for s in r.get("stray_scripts", [])}
    protected_targets = {
        "project-state/进度/__pycache__/x.pyc",
        "project-state/进度/overall.json.tmp",
        "project-state/契约/card-001.contract.bak",
        "project-state/状态快照.json",
    }
    check(not (protected_targets & cand),
          f"project-state 正式产出均未被列入任何清理候选（误入 {sorted(protected_targets & cand)}）")
    check("project-state/状态快照.json" in skipped,
          f"project-state 根级正式产出被披露为 hard-protected（实际 {sorted(skipped)}）")
    check("project-state" in skipped,
          "project-state 整区被披露为 hard-protected（iter_junk 剪枝）")
    check("project-state/_20260911_tmp.out" in cand
          and "project-state/_20260911_tmp.out" not in skipped,
          "§9.9 允许清理的根级中间件**在**候选内且**不**被误列硬保护")

    print("\n测试 B：只给 --archive-delivery / --archive，**不给 --apply**（P0-1 回归）")
    build()
    p = run("--archive-delivery", "--json")
    check(p.returncode == 0, "退出码 0", (p.stderr or "")[-400:])
    check((ROOT / "交付" / "方案书-v1.0.zip").exists(), "P0-1 交付旧版未被搬走")
    check(not (ROOT / "交付" / "归档").exists(), "P0-1 未创建 交付/归档/")
    run("--archive")
    check((ROOT / "产物" / "app-v1.0.zip").exists(), "P0-1 产物旧版未被搬走")
    check(not (ROOT / "产物" / "归档").exists(), "P0-1 未创建 产物/归档/")
    p3 = run()
    check("只报告（dry-run）" in p3.stdout, "报告模式显示 dry-run")
    check("执行清理" not in p3.stdout, "报告未显示「执行清理」")
    check((ROOT / "__pycache__").exists(), "dry-run 下垃圾原样保留")

    print("\n测试 C：Windows GBK 控制台 --write 不崩")
    env = dict(os.environ, PYTHONIOENCODING="gbk")
    p = run("--write", env=env)
    check(p.returncode == 0, "GBK 环境退出码 0", (p.stderr or "")[-500:])
    check("UnicodeEncodeError" not in (p.stderr or ""), "stderr 无 UnicodeEncodeError")
    out_md = ROOT / "project-state" / f"整洁盘点-{dt.datetime.now():%Y%m%d}.md"
    check(out_md.exists(), "整洁盘点 .md 已写出")
    if out_md.exists():
        t = out_md.read_text(encoding="utf-8")
        check("硬保护命中" in t, "报告含硬保护命中披露段")
        check("`发出记录.md` 在位" in t, "报告含审计链状态")
        check("✅" not in t and "⚠" not in t, "报告已去除 GBK 不安全符号")

    print("\n测试 D：config.json 注册自定域名（交付产物/部署产物 类项目）生效")
    build_registered()
    p = run("--apply", "--archive", "--archive-delivery", "--json")
    check(p.returncode == 0, "退出码 0", (p.stderr or "")[-400:])
    try:
        r2 = json.loads(p.stdout or "{}")
    except ValueError:
        r2 = {}
    check(r2.get("_art_name") == "内部产物" and r2.get("_dl_name") == "客户交付",
          f"域名解析自 config（实际 art={r2.get('_art_name')} dl={r2.get('_dl_name')}）")
    check(r2.get("delivery_record") is True, f"注册交付区审计链在位（实际 {r2.get('delivery_record')}）")
    check((ROOT / "客户交付" / "发出记录.md").exists(), "注册域 发出记录.md 存活（审计凭证硬保护）")
    check(not (ROOT / "内部产物" / "app-v1.0.zip").exists()
          and (ROOT / "内部产物" / "归档" / "app-v1.0.zip").exists(), "L3 旧版归档进注册产物区")
    check(bool(list((ROOT / "客户交付" / "归档").rglob("方案书-v1.0.zip"))), "L4 旧版归档进注册交付区")
    check(not (ROOT / "产物").exists() and not (ROOT / "交付").exists(),
          "默认名目录未被误建（未写错域）")

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
    sys.exit(code)
