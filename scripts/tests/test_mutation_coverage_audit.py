#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_mutation_coverage_audit.py — `mutation_coverage_audit.py` 的回归用例（纯 stdlib / unittest）

为什么有这个文件
----------------
`mutation_coverage_audit.py` 是 v5.23.0 新增的「变更后一致性」覆盖矩阵检查器。它同时要守住三件
**互相拉扯**的事，任何一件松掉，闸门就变成摆设：

  ① **能拦**：事件不在四子族、面名写错、`unknown` 当通过、`covered` 指不到用例、`MU-NNN`
     重复乱序、`object` 空——每一条都必须真的让退出码变成 1（**变异对拍**：没有植入违例的断言
     就是假保护）；
  ② **不乱拦**：干净矩阵必须退出码 0；缺预算键、面没测全、用例库还没建，只许报 WARN——
     **误报的代价是训练人忽略这条闸门**（同 `test_git_audit.py` 的反向对拍纪律）；
  ③ **三态不能混**：项目根不存在 / 没有 `测试卡/` / 没有 `tc-*` / 全部卡都没有 `变更矩阵.md`
     → 退出码 **2**，绝不能被当成 0 或 1——**「查不了」不是「没问题」**。

夹具全部现场合成（`tempfile.mkdtemp`），**不读仓库真实数据、不写仓库**——仓库里暂时没有
`测试卡/tc-*`，直接跑本脚本必然是 2（这是三态的实测，见下方「已知」）。

覆盖（15 组，编号与需求契约一一对应）
------------------------------------
     1 项目根不存在                        → 2
     2 没有 `测试卡/` 目录                 → 2
     3 有 `测试卡/` 但没有任何 `tc-*`      → 2
     4 全部卡都没有 `变更矩阵.md`          → 2（并带逐卡 `MUT-MATRIX-MISSING`）
     5 干净矩阵（covered + 用例存在）      → 0
     6 `event` 非法                        → 1 + `MUT-EVENT-UNKNOWN`
     7 `faces` 有七面之外的名字            → 1 + `MUT-FACE-UNKNOWN`
     8 `result=unknown`                    → 1 + `MUT-RESULT-UNKNOWN`
     9 `result=partial` 但 reason 空       → 1 + `MUT-NO-REASON`
    10 `result=covered` 但 `cas=-`         → 1 + `MUT-CAS-MISSING`
    11 `cas` 不在用例库                    → 1 + `MUT-CAS-DANGLING`
    12 重复 `MU-001`                       → 1 + `MUT-ID-DUP`
    13 缺 `combo_budget`                   → WARN `MUT-BUDGET-MISSING`，退出码仍 0
    14 七面只声明 2 面又无 `faces=na`      → WARN `MUT-FACE-GAP`，退出码仍 0
    15 `--json` 可 `json.loads` 且 `exit` 正确

另加四组**同源加固**（契约只说「至少 14」）：
    16 用例库不存在 → 只 WARN `MUT-CASLIB-MISSING`，**不**误报 `MUT-CAS-DANGLING`
    17 `MU-NNN` 乱序 / `result` 非法 / `object` 空 / 七列旧表头 → 各自的码
    18 两种围栏写法（HTML 注释标记 / ``` 围栏内 `machine:mutation`）都认
    19 编码与控制台：GBK 矩阵文件可读；`PYTHONIOENCODING=gbk` 下不崩、不出 Traceback

用法
----
    python -X utf8 -m unittest discover -s scripts/tests -p "test_mutation_coverage_audit.py" -v
    python -X utf8 scripts/tests/test_mutation_coverage_audit.py        # 等价（自带 main）
    MUT_AUDIT_KEEP_FIXTURE=1 …                                          # 失败时保留临时树便于排查
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_script():
    """兼容两种布局：本文件在 `scripts/tests/` 下（仓库现状），或被单独复制到 `scripts/`。"""
    for base in (os.path.join(HERE, os.pardir), HERE):
        cand = os.path.abspath(os.path.join(base, "mutation_coverage_audit.py"))
        if os.path.isfile(cand):
            return cand
    return os.path.abspath(os.path.join(HERE, os.pardir, "mutation_coverage_audit.py"))


SCRIPT = _find_script()

try:                                    # 直接 import 只用来核对版本号与常量，跑判据一律走子进程
    sys.path.insert(0, os.path.dirname(SCRIPT))
    from mutation_coverage_audit import VERSION as SCRIPT_VERSION  # noqa: E402
    from mutation_coverage_audit import CANON, EVENTS, FACES       # noqa: E402
except Exception:                       # pragma: no cover - 脚本被删/语法错时由 subprocess 报错
    SCRIPT_VERSION = None
    CANON = ["id", "object", "event", "faces", "cas", "result", "reason"]
    EVENTS = ["权限与角色回收", "归属与组织变更", "时效到期", "派生数据重算"]
    FACES = ["列表", "详情", "直达链接", "导出", "搜索", "通知", "统计"]

HEADER = "| " + " | ".join(CANON) + " |"
SEP = "| " + " | ".join(["---"] * len(CANON)) + " |"

# 干净夹具的默认两行：两条 covered、都指得到用例、七面全测
CLEAN_ROWS = [
    "| MU-001 | 审批单 | 权限与角色回收 | all | CAS-001 | covered | - |",
    "| MU-002 | 分享链接 | 时效到期 | all | CAS-002 | covered | - |",
]


def row(mu="MU-001", obj="审批单", event="权限与角色回收", faces="all",
        cas="CAS-001", result="covered", reason="-"):
    """拼一行机读块（列顺序与 `CANON` 一致）。参数名不用 `id` / `object`，避免遮蔽内建。"""
    return "| %s | %s | %s | %s | %s | %s | %s |" % (mu, obj, event, faces, cas, result, reason)


class MutationCoverageAuditTest(unittest.TestCase):
    """一棵临时项目树 = 一个用例；`tearDown` 清场（`MUT_AUDIT_KEEP_FIXTURE=1` 时保留）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mut_audit_")
        self.root = os.path.join(self.tmp, "proj")
        self.base = os.path.join(self.root, "project-state")
        os.makedirs(self.base)

    def tearDown(self):
        if os.environ.get("MUT_AUDIT_KEEP_FIXTURE"):
            sys.stderr.write("夹具保留（MUT_AUDIT_KEEP_FIXTURE）：%s\n" % self.tmp)
            return
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具
    def write(self, rel_path, text, encoding="utf-8"):
        path = os.path.join(self.root, *rel_path.split("/"))
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
        return path

    def write_tc(self, name="tc-001", budget="6"):
        """`tc.md` frontmatter；`budget=None` = 故意不写 `combo_budget`（MUT-BUDGET-MISSING 夹具）。"""
        lines = ["---", "card: %s" % name, "test_depth: full"]
        if budget is not None:
            lines.append("combo_budget: %s" % budget)
        lines.extend(["---", "", "# 测试卡 %s" % name, ""])
        self.write("project-state/测试卡/%s/tc.md" % name, "\n".join(lines))

    def matrix_text(self, rows, header=None, sep=None, tag=True):
        parts = ["# 变更矩阵", "", "```"]
        if tag:
            parts.append("machine:mutation")
        parts.append(header or HEADER)
        parts.append(sep or SEP)
        parts.extend(rows)
        parts.extend(["```", ""])
        return "\n".join(parts)

    def write_matrix(self, rows=None, name="tc-001", header=None, sep=None, tag=True,
                     encoding="utf-8"):
        text = self.matrix_text(CLEAN_ROWS if rows is None else rows,
                                header=header, sep=sep, tag=tag)
        return self.write("project-state/测试卡/%s/变更矩阵.md" % name, text, encoding=encoding)

    def write_matrix_html(self, rows=None, name="tc-001"):
        """另一种围栏写法：框架统一的 HTML 注释标记（v5.23.0 两种都认）。"""
        parts = ["# 变更矩阵", "",
                 "<!-- machine:mutation -->", HEADER, SEP]
        parts.extend(CLEAN_ROWS if rows is None else rows)
        parts.extend(["<!-- /machine:mutation -->", ""])
        return self.write("project-state/测试卡/%s/变更矩阵.md" % name, "\n".join(parts))

    def write_lib(self, ids=("CAS-001", "CAS-002")):
        lines = ["# 用例库", ""]
        lines.extend("- %s 回归：变更后列表/详情仍正确" % i for i in ids)
        lines.append("")
        return self.write("project-state/代码/测试/用例库.md", "\n".join(lines))

    # ------------------------------------------------------------ 跑脚本
    def run_audit(self, *args, **kwargs):
        """真起子进程（`python -X utf8`），返回 (退出码, stdout, stderr) 全按 UTF-8 解码。"""
        target = kwargs.pop("root", self.root)
        env_extra = kwargs.pop("env", None) or {}
        cmd = [sys.executable, "-X", "utf8", SCRIPT, target] + list(args)
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env.update(env_extra)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))

    def run_json(self, *args, **kwargs):
        code, stdout, stderr = self.run_audit("--json", *args, **kwargs)
        try:
            payload = json.loads(stdout)
        except ValueError:
            self.fail("--json 输出不是合法 JSON：\n%s\nstderr:\n%s" % (stdout, stderr))
        return code, payload, stdout, stderr

    # ------------------------------------------------------------ 断言
    def expect(self, want_exit, stdout, stderr, want_codes=(), absent=()):
        text = stdout + stderr

        def _fail(msg):
            self.fail("%s\n---- stdout ----\n%s\n---- stderr ----\n%s" % (msg, stdout, stderr))

        if STDOUT_EXIT_PREFIX not in stdout and "退出码" not in stdout:
            _fail("人读报告里没有结论行（退出码 …）")
        for code in want_codes:
            if code not in stdout:
                _fail("期望报告里出现检查码 %s，实际没有" % code)
        for code in absent:
            if code in stdout:
                _fail("不应出现检查码 %s（误报）" % code)

    def assert_exit(self, got, want, stdout, stderr, want_codes=(), absent=()):
        self.expect(want, stdout, stderr, want_codes=want_codes, absent=absent)
        if got != want:
            self.fail("退出码应为 %d，实得 %d\n---- stdout ----\n%s\n---- stderr ----\n%s"
                      % (want, got, stdout, stderr))

    def codes_of(self, payload):
        return set(it["code"] for it in payload["items"])

    # ============================================================ 1–4 三态里的第三态
    def test_01_missing_project_root(self):
        code, stdout, stderr = self.run_audit(root=os.path.join(self.tmp, "no-such-root"))
        self.assert_exit(code, 2, stdout, stderr)
        self.assertIn("无法检查 ≠ 通过", stdout, "三态报告必须字面写明「无法检查 ≠ 通过」\n%s" % stdout)
        self.assertIn("不是「没问题」", stdout)

    def test_02_missing_testcard_dir(self):
        os.rmdir(self.base)                      # project-state 存在，但没有 测试卡/
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 2, stdout, stderr, want_codes=("MUT-NO-TESTCARD",))
        self.assertIn("无法检查 ≠ 通过", stdout, "退出码 2 的结论行不能读成「干净」\n%s" % stdout)

    def test_03_no_tc_dirs(self):
        self.write("project-state/测试卡/测试卡模板.md", "# 模板不是卡\n")
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 2, stdout, stderr, want_codes=("MUT-NO-CARD",))
        self.assertIn("无法检查 ≠ 通过", stdout)

    def test_04_all_cards_without_matrix(self):
        self.write_tc("tc-001")
        self.write_tc("tc-002")
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 2, stdout, stderr,
                         want_codes=("MUT-MATRIX-MISSING", "MUT-NO-MATRIX"))
        self.assertEqual(stdout.count("MUT-MATRIX-MISSING"), 2,
                         "两张卡都应各报一次 MUT-MATRIX-MISSING\n%s" % stdout)
        self.assertIn("无法检查 ≠ 通过", stdout)

    # ============================================================ 5 干净矩阵 → 0
    def test_05_clean_matrix(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix()
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 0, stdout, stderr)
        self.assertIn("退出码 0", stdout)
        self.assertIn("矩阵行：2 行", stdout)

    # ============================================================ 6–12 每条 ERROR 各一发
    def test_06_event_unknown(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix([row(event="权限回收")])          # 少了「与角色」三个字
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 1, stdout, stderr, want_codes=("MUT-EVENT-UNKNOWN",))

    def test_07_face_unknown(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix([row(faces="列表,弹窗")])          # 弹窗不是七面之一
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 1, stdout, stderr, want_codes=("MUT-FACE-UNKNOWN",))

    def test_08_result_unknown(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix([row(result="unknown", cas="CAS-001")])
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 1, stdout, stderr, want_codes=("MUT-RESULT-UNKNOWN",))

    def test_09_partial_without_reason(self):
        self.write_tc()
        self.write_lib()
        for empty in ("-", "无", "n/a", ""):
            self.write_matrix([row(faces="all", result="partial", cas="CAS-001", reason=empty)])
            code, stdout, stderr = self.run_audit()
            self.assert_exit(code, 1, stdout, stderr, want_codes=("MUT-NO-REASON",))

    def test_10_covered_without_cas(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix([row(cas="-")])
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 1, stdout, stderr, want_codes=("MUT-CAS-MISSING",))

    def test_11_cas_dangling(self):
        self.write_tc()
        self.write_lib(ids=("CAS-001",))
        self.write_matrix([row(cas="CAS-999")])
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 1, stdout, stderr,
                         want_codes=("MUT-CAS-DANGLING",), absent=("MUT-CASLIB-MISSING",))

    def test_12_id_duplicate(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix([row(mu="MU-001"), row(mu="MU-001", obj="报销单")])
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 1, stdout, stderr, want_codes=("MUT-ID-DUP",))

    # ============================================================ 13–14 WARN 不许改退出码
    def test_13_budget_missing_is_only_warn(self):
        self.write_tc(budget=None)               # 故意不写 combo_budget
        self.write_lib()
        self.write_matrix()
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 0, stdout, stderr, want_codes=("MUT-BUDGET-MISSING",))
        self.assertIn("WARN 1", stdout, "缺预算键应只记 1 条 WARN\n%s" % stdout)

    def test_14_face_gap_is_only_warn(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix([row(faces="列表,详情")])   # 七面只声明 2 面，又没有 faces=na
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 0, stdout, stderr, want_codes=("MUT-FACE-GAP",))
        for face in ("直达链接", "导出", "搜索", "通知", "统计"):
            self.assertIn(face, stdout, "MUT-FACE-GAP 应点出缺哪一面：%s\n%s" % (face, stdout))

    # ============================================================ 15 --json
    def test_15_json_payload(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix()
        code, payload, stdout, stderr = self.run_json()
        self.assertEqual(code, 0, stdout + stderr)
        for key in ("version", "base", "cards", "matrices", "rows", "ok", "exit",
                    "errors", "warns", "infos", "counts", "details", "items"):
            self.assertIn(key, payload, "--json 缺字段 %s" % key)
        self.assertEqual(payload["exit"], 0)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["cards"], 1)
        self.assertEqual(payload["matrices"], 1)
        self.assertEqual(payload["rows"], 2)
        self.assertEqual(len(payload["details"]), 2)
        self.assertEqual(payload["errors"], [])
        if SCRIPT_VERSION is not None:
            self.assertEqual(payload["version"], SCRIPT_VERSION,
                             "报告版本号应与脚本模块级 VERSION 一致")

    # ============================================================ 16–19 同源加固
    def test_16_caslib_missing_skips_dangling(self):
        self.write_tc()
        # 故意不写用例库：cas 非空但查不了 → 只 WARN，不许报悬空
        self.write_matrix()
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 0, stdout, stderr,
                         want_codes=("MUT-CASLIB-MISSING",), absent=("MUT-CAS-DANGLING",))

    def test_17_id_order_result_bad_object_empty_and_stale_header(self):
        self.write_tc()
        self.write_lib()
        # 乱序 + result 非法 + object 空（三发一起，各认各的码）
        self.write_matrix([
            row(mu="MU-002", obj="审批单"),
            row(mu="MU-001", obj="", result="pass"),
        ])
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 1, stdout, stderr,
                         want_codes=("MUT-ID-ORDER", "MUT-RESULT-BAD", "MUT-OBJECT-EMPTY"))

        # 七列旧表头（6 列）：整块只报一次 WARN，跳过行级检查，退出码仍是 0
        stale_header = "| id | object | event | faces | cas | result |"
        stale_sep = "| --- | --- | --- | --- | --- | --- |"
        self.write_matrix(rows=[], header=stale_header, sep=stale_sep)
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 0, stdout, stderr, want_codes=("MUT-FORMAT-STALE",))
        self.assertIn("MUT-FORMAT-STALE", stdout)

    def test_18_both_fence_styles_parse(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix_html()
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 0, stdout, stderr, absent=("MUT-FORMAT-STALE",))
        self.assertIn("矩阵行：2 行", stdout)

        # 围栏里没有 machine:mutation 标记 → 看不见机读块，记 WARN MUT-FORMAT-STALE
        self.write_matrix(CLEAN_ROWS, tag=False)
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 0, stdout, stderr, want_codes=("MUT-FORMAT-STALE",))

    def test_19_gbk_fixture_and_gbk_console(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix(encoding="gbk")        # 真实工程里 GBK 的卡确实存在
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 0, stdout, stderr)

        code, stdout, stderr = self.run_audit(env={"PYTHONIOENCODING": "gbk"})
        self.assert_exit(code, 0, stdout, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertIn("退出码 0", stdout)         # reconfigure 到 UTF-8 后中文照样完整

    def test_20_json_exit_code_on_error(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix([row(event="不存在的族")])
        code, payload, stdout, stderr = self.run_json()
        self.assertEqual(code, 1, stdout + stderr)
        self.assertEqual(payload["exit"], 1)
        self.assertIs(payload["ok"], False)
        self.assertIn("MUT-EVENT-UNKNOWN", [it["code"] for it in payload["errors"]])

    def test_21_cannot_check_json_is_exit_2(self):
        self.write_tc("tc-001")
        self.write_tc("tc-002")                  # 都没矩阵
        code, payload, stdout, stderr = self.run_json()
        self.assertEqual(code, 2, stdout + stderr)
        self.assertEqual(payload["exit"], 2)
        self.assertIsNone(payload["ok"], "无法检查时 ok 必须是 null，不能是 true")
        self.assertIn("MUT-NO-MATRIX", [it["code"] for it in payload["infos"]])

    def test_22_check_mode_and_checks_table(self):
        self.write_tc()
        self.write_lib()
        self.write_matrix()
        code, stdout, stderr = self.run_audit("--check")
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(stdout.count("\n"), 1, "--check 只印一行（门禁用）\n%s" % stdout)
        self.assertIn("退出码 0", stdout)

        code, stdout, stderr = self.run_audit("--checks")
        self.assertEqual(code, 0, stdout + stderr)
        for code_name in ("MUT-EVENT-UNKNOWN", "MUT-FACE-UNKNOWN", "MUT-RESULT-UNKNOWN",
                          "MUT-RESULT-BAD", "MUT-NO-REASON", "MUT-CAS-MISSING",
                          "MUT-CAS-DANGLING", "MUT-ID-DUP", "MUT-ID-ORDER",
                          "MUT-OBJECT-EMPTY", "MUT-MATRIX-MISSING", "MUT-BUDGET-MISSING",
                          "MUT-FACE-GAP", "MUT-CASLIB-MISSING"):
            self.assertIn(code_name, stdout, "--checks 表缺 %s" % code_name)

    def test_23_one_card_missing_matrix_is_warn(self):
        self.write_tc("tc-001")
        self.write_tc("tc-002")
        self.write_lib()
        self.write_matrix(name="tc-001")
        code, stdout, stderr = self.run_audit()
        self.assert_exit(code, 0, stdout, stderr, want_codes=("MUT-MATRIX-MISSING",))
        self.assertIn("测试卡：2 张", stdout)
        self.assertIn("变更矩阵：1 份", stdout)


STDOUT_EXIT_PREFIX = "退出码 "


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    unittest.main(verbosity=2)
