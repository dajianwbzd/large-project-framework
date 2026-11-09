#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git_audit.py 的回归测试 + 变异对拍 + 反向对拍（2026-09-19 补，源起两处实测缺陷）。

为什么这个文件必须存在：
    `scripts/tests/` 下其余检查器都有回归测试，**唯独 git_audit.py 没有**。而它同时踩了两个坑，
    两个都不是"代码写错了"，是"判据/输出没有判别力与自保力"：

    ① `commit_pattern` 只承认**单个**卡标记且在信息末尾 ⇒ 按批次写的多卡并列提交
       （`[card-043,card-044,card-045,card-046]`）被判"缺卡片标记"——**6 条合规提交被误报**。
       误报的代价不是"多看一眼"，是会训练人忽略这条闸门。
    ② `GIT-006b` 把命中行正文截 130 字符**原样**打印，而框架纪律要求自检输出写进 `运行日志.md`
       ⇒ **跑一次自检就把凭据明文抄进日志**，密钥卫生审计自身成了泄漏路径。

    两条测试纪律（与 test_style_audit.py 同源）：
    **变异对拍**：每条断言都要有一个"证明它能拦"的植入违例——否则就是假保护。
    **反向对拍**：每条断言还要有一个"证明它不乱拦"的合规样例——否则闸门会因误报被关掉。
    本文件额外的第三条：**同源对拍**——`commit_pattern` 在框架内置默认与项目
    `契约/Git工作流.md` 机读块里各存一份，改一处漏一处就是本缺陷的复发形态，
    故两者必须对同一批用例给出同一结论。

用法：python scripts/tests/test_git_audit.py [项目根]
退出码：0 全过；1 有失败（逐条打印失败项）。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
FRAMEWORK = HERE.parents[2]
FAILS = []


def load_git_audit():
    spec = importlib.util.spec_from_file_location("git_audit_under_test", FRAMEWORK / "scripts" / "git_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(name, got, want, note=""):
    if got == want:
        print("  OK   %s" % name)
    else:
        FAILS.append("%s：got=%r want=%r %s" % (name, got, want, note))
        print("  FAIL %s：got=%r want=%r %s" % (name, got, want, note))


# 合规样例（反向对拍用：闸门不许拦这些）
COMPLIANT = [
    "chore(ws): 批次收口 [card-043,card-044,card-045,card-046]",
    "feat(web): 商城 hash 状态修复 [card-040]",
    "test(tc): 系统级 11 维度覆盖 [tc-001]",
    "chore(ws): 混标归档 [card-047,tc-002]",
    "fix(api): 详情页刷空 [card-048]",
    "hotfix(prod): 回滚点恢复 [hotfix-20260915a]",
]
# 违例样例（变异对拍用：闸门必须拦）
VIOLATIONS = [
    "chore(ws): 忘了写卡片标记",
    "chore(ws): 标记不在末尾 [card-047]（补充说明）",
    "chore(ws): 逗号后带空格 [card-047, card-048]",
    "chore(ws): 标记格式不对 (card-047)",
]


def test_commit_pattern(pat_text, label):
    pat = re.compile(pat_text)
    print("[%s]" % label)
    for t in COMPLIANT:
        check("合规不被拦 | " + t[:42], bool(pat.match(t)), True)
    for t in VIOLATIONS:
        check("违例被拦下 | " + t[:42], bool(pat.match(t)), False)


def test_mutation(ga):
    """变异对拍：把判据还原成修复前的单卡版，多卡样例必须变红——否则本测试没有判别力。"""
    print("[变异对拍] 单卡版判据必须拦下多卡提交")
    single = r"^(feat|fix|refactor|docs|test|chore|hotfix)\([^)]+\): .+(\[card-\d{3,}\]|\[tc-\d{3,}\]|\[hotfix-[\w.-]+\])$"
    check("旧判据拦下多卡提交（证明测试有判别力）",
          bool(re.match(single, COMPLIANT[0])), False)
    check("旧判据仍放过单卡提交", bool(re.match(single, COMPLIANT[1])), True)


def test_redaction(ga):
    print("[脱敏] GIT-006b 输出不得回显命中正文")
    secret = "s3cr3t-dev-value"
    hit = "src/login.js:42:  const password = '%s';" % secret
    out = ga.redact_secret_hit(hit, ga.DEFAULT_MODEL["secret_regex"])
    check("脱敏输出不含明文", secret in out, False)
    check("脱敏输出保留定位（文件:行号）", out.startswith("src/login.js:42"), True)
    # 变异对拍：修复前的写法（原样截断）确实会泄露——证明上面那条断言不是空的
    leaked = ga.one_line(hit, 130)
    check("阳性对照：旧写法确实泄露", secret in leaked, True)
    # 边界：畸形行不得抛异常、也不得回显
    check("畸形行安全兜底", secret in ga.redact_secret_hit(secret, ga.DEFAULT_MODEL["secret_regex"]), False)


def test_same_source(ga, root):
    """同源对拍：项目契约机读块与框架内置默认必须同结论。"""
    p = Path(root) / "project-state" / "契约" / "Git工作流.md"
    if not p.is_file():
        print("[同源对拍] 跳过：%s 不存在" % p)
        return
    m = re.search(r"```json git-workflow-model\s*\n(.*?)```", p.read_text(encoding="utf-8"), re.S)
    check("契约机读块存在且可解析", bool(m), True)
    if not m:
        return
    try:
        model = json.loads(m.group(1))
    except Exception as e:  # noqa: BLE001
        check("契约机读块 JSON 可解析", False, True, str(e))
        return
    print("[同源对拍] 契约 commit_pattern vs 框架内置默认")
    for t in COMPLIANT:
        check("契约：合规不被拦 | " + t[:38], bool(re.match(model["commit_pattern"], t)), True)
    for t in VIOLATIONS:
        check("契约：违例被拦下 | " + t[:38], bool(re.match(model["commit_pattern"], t)), False)
    check("两处判据对合规样例同结论",
          [bool(re.match(model["commit_pattern"], t)) for t in COMPLIANT],
          [bool(re.match(ga.DEFAULT_MODEL["commit_pattern"], t)) for t in COMPLIANT])


def main():
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else FRAMEWORK
    ga = load_git_audit()
    test_commit_pattern(ga.DEFAULT_MODEL["commit_pattern"], "内置默认判据")
    test_mutation(ga)
    test_redaction(ga)
    test_same_source(ga, root)
    print("")
    if FAILS:
        print("失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
