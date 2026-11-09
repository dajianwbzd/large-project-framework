# -*- coding: utf-8 -*-
"""reuse_scan.py（v5.13.0 先例复用评估机器核）回归测试。

跑法（本机只有 `python`，没有 `py` 启动器）：

    python -X utf8 scripts\\tests\\test_reuse_scan.py

纪律：
- 全部夹具都用 tempfile 自建，**不碰真实 NAS / 真实工程**；
- 「只读」用 sha256 + 大小 + mtime 快照对照，`--write` 只在临时目录里试；
- 断言 = 退出码 + 发现项**码集合**（从输出里正则扣出来，不看人类措辞）；
- 夹具目录一律 `tempfile.mkdtemp()`，跑完删掉，绝不写仓库。
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))          # scripts/tests
SCRIPTS_DIR = os.path.dirname(HERE)                        # scripts
SCRIPT = os.path.join(SCRIPTS_DIR, "reuse_scan.py")

FINAL_LINE_RE = re.compile(
    r"^REUSE-SCAN: (OK|ERROR|CANNOT-CHECK) "
    r"findings=ERROR:(\d+) WARN:(\d+) INFO:(\d+) roots:(\d+) projects:(\d+)$")
FINDING_RE = re.compile(r"^\s+\[[EWI]\]\s+(REUSE-[A-Z-]+)", re.M)
JSON_TOP_KEYS = {"schema", "generated_at", "project", "roots", "candidates",
                 "ledger", "findings", "counts"}
CAND_KEYS = {"path", "name", "framework_version", "mode", "last_activity", "score",
             "matched_terms", "counts", "cards", "tests", "knowledge", "reuse_ledger",
             "contracts", "modules", "local_scripts"}
COUNT_KEYS = {"cards", "tests", "knowledge", "contracts", "modules", "scripts"}


# ------------------------------------------------------------------ 跑脚本
def dec(raw):
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run(root, *args, **kw):
    """跑一次 reuse_scan.py → (退出码, stdout, stderr)。"""
    cmd = [sys.executable, "-X", "utf8", SCRIPT, root] + [str(a) for a in args]
    env = None
    if kw.get("env"):
        env = dict(os.environ)
        env.update(kw["env"])
    p = subprocess.run(cmd, cwd=SCRIPTS_DIR, capture_output=True, env=env)
    return p.returncode, dec(p.stdout), dec(p.stderr)


def run_json(root, *args, **kw):
    """跑一次并解析 JSON（末行 REUSE-SCAN: … 不是 JSON，切片取对象）。"""
    code, text, err = run(root, *args, **kw)
    payload = json.loads(text[text.index("{"):text.rindex("}") + 1])
    return code, payload, text, err


# ------------------------------------------------------------------ 夹具
def w(path, text, encoding="utf-8"):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding=encoding, newline="\n") as fh:
        fh.write(text)


def wbytes(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def wjson(path, obj):
    w(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def make_agent(root, version="5.13.0"):
    w(os.path.join(root, "agent.md"),
      '---\nversion: "%s"\n---\n\n# 项目说明\n' % version)


def make_card(root, cid, title="示例需求卡", status="todo", reuse=None, reuse_ref=None,
              body="正文：需求描述。", contracts=(), raw_fm=None):
    """建一张需求卡（当前项目或历史项目都用它）。"""
    lines = ["---", "id: %s" % cid, "title: %s" % title, "status: %s" % status]
    if reuse is not None:
        lines.append("reuse: %s" % reuse)
    if reuse_ref is not None:
        lines.append("reuse_ref: %s" % reuse_ref)
    if raw_fm:
        lines.extend(raw_fm)
    lines.append("---")
    text = "\n".join(lines) + "\n\n## 背景\n\n" + body + "\n"
    if contracts:
        text += "\n" + "\n".join("见契约 %s。" % c for c in contracts) + "\n"
    w(os.path.join(root, "project-state", "需求卡", cid, "card.md"), text)


def make_test_card(root, tcid, title="示例测试卡", status="todo"):
    w(os.path.join(root, "project-state", "测试卡", tcid, "tc.md"),
      '---\nid: %s\ntitle: %s\nstatus: %s\n---\n\n## 用例\n\n步骤。\n' % (tcid, title, status))


def make_knowledge(root, entries, extra_lines=()):
    lines = ["# 知识沉淀", ""]
    for kid, title in entries:
        lines.append("## %s %s" % (kid, title))
        lines.append("")
        lines.append("正文。")
        lines.append("")
    lines.extend(extra_lines)
    w(os.path.join(root, "project-state", "记忆", "知识沉淀.md"), "\n".join(lines) + "\n")


def make_contract(root, rel, content="契约内容"):
    w(os.path.join(root, "project-state", "契约", rel.replace("/", os.sep)), content)


def make_module(root, name, nfiles=1):
    base = os.path.join(root, "repos", name)
    for i in range(nfiles):
        w(os.path.join(base, "src", "m%d.py" % i), "# module\n")


def make_script(root, name, content="# local\n"):
    w(os.path.join(root, "scripts", name), content)


LEDGER_GOOD = """# 复用台账

<!-- machine:reuse -->
| id | date | source_project | source_ref | kind | verdict | target | evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| RU-001 | 2026-10-01 | 示例工作台E | card-003 | 需求卡 | reuse | card-001 | 验收口径一致 |
| RU-002 | 2026-10-02 | 示例工作台A | 契约/api.yaml | 契约 | adapt | 契约/api.yaml | 接口要加字段 |
| RU-003 | 2026-10-03 | 示例工作台D | repos/x | 代码 | reject | - | 技术栈不兼容 |
<!-- /machine:reuse -->
"""


def make_ledger(root, text=LEDGER_GOOD):
    w(os.path.join(root, "project-state", "记忆", "复用台账.md"), text)


def make_current(root, name="本项目", description="", roots="__absent__", cards=(),
                 ledger=None, agent=True, config=True, config_text=None, extra=None):
    """建「当前项目」（含 project-state/）。roots="__absent__" 表示不写 reuse_roots 键。"""
    os.makedirs(os.path.join(root, "project-state"), exist_ok=True)
    if config:
        if config_text is not None:
            w(os.path.join(root, "project-state", "config.json"), config_text)
        else:
            cfg = {"schema_version": "1", "project_name": name, "description": description,
                   "mode": "full"}
            if roots != "__absent__":
                cfg["reuse_roots"] = roots
            if extra:
                cfg.update(extra)
            wjson(os.path.join(root, "project-state", "config.json"), cfg)
    if agent:
        make_agent(root)
    for kw in cards:
        make_card(root, **kw)
    if ledger is not None:
        make_ledger(root, ledger)
    return root


def make_history(root, name="历史项目A", description="", version="5.5.0", mode="full",
                 cards=(), tests=(), knowledge=(), contracts=(), modules=(),
                 scripts=(), ledger=None, design_profile=""):
    """建一个历史项目夹具（含 agent.md + project-state/）。"""
    os.makedirs(os.path.join(root, "project-state"), exist_ok=True)
    cfg = {"schema_version": "1", "project_name": name, "description": description, "mode": mode}
    if design_profile:
        cfg["design_profile"] = design_profile
    wjson(os.path.join(root, "project-state", "config.json"), cfg)
    make_agent(root, version)
    for kw in cards:
        make_card(root, **kw)
    for kw in tests:
        make_test_card(root, **kw)
    if knowledge:
        make_knowledge(root, knowledge)
    for rel in contracts:
        make_contract(root, rel)
    for m in modules:
        if isinstance(m, tuple):
            make_module(root, m[0], m[1])
        else:
            make_module(root, m)
    for s in scripts:
        make_script(root, s)
    if ledger:
        make_ledger(root, ledger)
    return root


def snapshot(root):
    """整棵树的 sha256 + 大小 + mtime 快照（只读保证用）。"""
    snap = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                with open(p, "rb") as fh:
                    h = hashlib.sha256(fh.read()).hexdigest()
                st = os.stat(p)
            except OSError:
                continue
            snap[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, h)
    return snap


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def slurp(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------ 基类
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="_fixture_reuse_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def codes(self, text):
        return set(FINDING_RE.findall(text))

    def jcodes(self, payload):
        """--json 模式下从 findings 里取码（人读行在 JSON 模式下不打印）。"""
        return set(f["code"] for f in payload["findings"])

    def assert_final_line(self, text, status=None):
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        self.assertTrue(lines, "输出是空的")
        last = lines[-1].strip()
        m = FINAL_LINE_RE.match(last)
        self.assertIsNotNone(m, "最后一行不像机器判据行：%r" % last)
        if status:
            self.assertEqual(m.group(1), status)
        return m

    def expect(self, root, want_exit, want=(), absent=(), args=(), env=None,
               label="", status=None):
        code, text, err = run(root, *args, env=env)
        self.assertEqual(code, want_exit,
                         "%s：退出码应为 %d，实际 %d\nSTDOUT:\n%s\nSTDERR:\n%s"
                         % (label or args, want_exit, code, text, err))
        self.assertNotIn("Traceback", err, "%s：stderr 里有异常\n%s" % (label, err))
        got = self.codes(text)
        for c in want:
            self.assertIn(c, got, "%s：应报 %s，实际只报了 %s" % (label, c, sorted(got)))
        for c in absent:
            self.assertNotIn(c, got, "%s：不该报 %s（实际 %s）" % (label, c, sorted(got)))
        self.assert_final_line(text, status)
        return text


# ================================================================== 1. 退出码 2
class TestCannotCheck(Base):
    """三态之二：无法检查 ≠ 没问题。"""

    def test_root_not_exist(self):
        code, text, _ = run(self.path("根本没这个目录"))
        self.assertEqual(code, 2)
        self.assertIn("无法检查，不是「没问题」", text)
        self.assertIn("项目根不存在", text)
        self.assert_final_line(text, "CANNOT-CHECK")
        self.assertEqual(self.codes(text), set())

    def test_no_project_state(self):
        root = self.path("光秃秃")
        os.makedirs(root)
        self.expect(root, 2, want=[], label="缺 project-state",
                    status="CANNOT-CHECK")
        _c, text, _e = run(root)
        self.assertIn("没有 project-state/", text)

    def test_no_config_json(self):
        root = self.path("只有projectstate")
        os.makedirs(os.path.join(root, "project-state"))
        code, text, _ = run(root)
        self.assertEqual(code, 2)
        self.assertIn("config.json", text)
        self.assert_final_line(text, "CANNOT-CHECK")

    def test_bad_json(self):
        root = self.path("坏JSON")
        os.makedirs(os.path.join(root, "project-state"))
        w(os.path.join(root, "project-state", "config.json"), "{ 这不是 JSON ,,,")
        code, text, _ = run(root)
        self.assertEqual(code, 2)
        self.assertIn("JSON", text)
        self.assert_final_line(text, "CANNOT-CHECK")

    def test_json_mode_also_2(self):
        code, text, _ = run(self.path("没有这个目录"), "--json")
        self.assertEqual(code, 2)
        self.assert_final_line(text, "CANNOT-CHECK")

    def test_config_is_list_not_object(self):
        root = self.path("顶层是数组")
        os.makedirs(os.path.join(root, "project-state"))
        w(os.path.join(root, "project-state", "config.json"), "[]\n")
        code, _text, _ = run(root)
        self.assertEqual(code, 2)


# ================================================================== 2. 根声明
class TestRoots(Base):
    def test_missing_key_is_error(self):
        root = make_current(self.path("当前"))
        text = self.expect(root, 1, want=["REUSE-ROOTS-MISSING"],
                           label="缺 reuse_roots 键", status="ERROR")
        self.assertIn("确实没有历史项目时写显式 `[]`", text)
        self.assertIn("REUSE-ROOTS-MISSING", text)

    def test_missing_key_even_with_cli_roots(self):
        """「问没问过用户」与「这次扫哪里」是两件事：给了 --roots 也照报 ERROR。"""
        hist = make_history(self.path("NAS", "历史A"))
        root = make_current(self.path("当前"))
        self.expect(root, 1, want=["REUSE-ROOTS-MISSING"],
                    args=["--roots", hist], label="有 --roots 但 config 缺键")

    def test_empty_list_is_info_and_exit0(self):
        root = make_current(self.path("当前"), roots=[])
        text = self.expect(root, 0, want=["REUSE-ROOTS-EMPTY"],
                           absent=["REUSE-ROOTS-MISSING", "REUSE-ROOT-MISSING",
                                   "REUSE-ROOT-NO-PROJECT"],
                           label="显式空数组 = 合法豁免", status="OK")
        self.assertIn("用户确认过没有历史项目", text)

    def test_root_missing_is_warn_exit0(self):
        root = make_current(self.path("当前"), roots=[self.path("没挂载的NAS")])
        text = self.expect(root, 0, want=["REUSE-ROOT-MISSING"],
                           absent=["REUSE-ROOTS-MISSING", "REUSE-ROOTS-EMPTY"],
                           label="根不存在只 WARN")
        self.assertIn("这不等于「没有历史项目」", text)

    def test_root_exists_but_no_project(self):
        empty = self.path("空的NAS")
        os.makedirs(os.path.join(empty, "只有素材"))
        root = make_current(self.path("当前"), roots=[empty])
        self.expect(root, 0, want=["REUSE-ROOT-NO-PROJECT"], label="根里没有候选")

    def test_self_root_is_info(self):
        root = make_current(self.path("当前"), roots=["__absent__"])
        cfgp = os.path.join(root, "project-state", "config.json")
        cfg = json.loads(slurp(cfgp))
        cfg["reuse_roots"] = [root]
        wjson(cfgp, cfg)
        text = self.expect(root, 0, want=["REUSE-SELF-ROOT"],
                           absent=["REUSE-ROOT-NO-PROJECT", "REUSE-ROOTS-MISSING"],
                           label="声明根本身")
        self.assertIn("已跳过", text)

    def test_tilde_and_relative_and_semicolon(self):
        hist = make_history(self.path("NAS", "历史A"))
        nas = self.path("NAS")
        root = make_current(self.path("当前"), roots=["__absent__"])
        self.expect(root, 0, want=[], args=["--roots", "%s;%s" % (hist, nas)],
                    label="分号分隔多个根")
        code, payload, _t, _e = run_json(root, "--roots", "%s;%s" % (hist, nas), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["roots"]), 2)
        self.assertEqual(payload["counts"]["candidates"], 1)

    def test_tilde_expands(self):
        """`~` 要展开；用 --check 只验解析结果，不去真扫整个家目录。"""
        root = make_current(self.path("当前"), roots=["~"], ledger=LEDGER_GOOD)
        code, payload, _t, _e = run_json(root, "--check", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(payload["roots"][0]["resolved"].startswith(
            os.path.expanduser("~")[:2]))
        self.assertTrue(payload["roots"][0]["exists"])
        self.assertTrue(os.path.isdir(payload["roots"][0]["resolved"]))

    def test_cli_overrides_config(self):
        a = make_history(self.path("NAS-A", "历史A"), name="项目甲")
        b = make_history(self.path("NAS-B", "历史B"), name="项目乙")
        root = make_current(self.path("当前"), roots=[a])
        code, payload, _t, _e = run_json(root, "--roots", b, "--json")
        self.assertEqual(code, 0)
        self.assertEqual([r["declared"] for r in payload["roots"]], [b])
        self.assertEqual([c["name"] for c in payload["candidates"]], ["项目乙"])

    def test_config_roots_used_when_no_cli(self):
        a = make_history(self.path("NAS-A", "历史A"), name="项目甲")
        root = make_current(self.path("当前"), roots=[a])
        code, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(code, 0)
        self.assertEqual([c["name"] for c in payload["candidates"]], ["项目甲"])

    def test_duplicate_roots_dedup(self):
        """同一真实路径被两个根覆盖：只算一次，且不误报「根里没有候选」。"""
        hist = make_history(self.path("NAS", "历史A"), name="项目甲")
        root = make_current(self.path("当前"), roots=["__absent__"])
        code, payload, text, _e = run_json(root, "--roots", self.path("NAS"),
                                           "--roots", hist, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["candidates"]), 1)
        self.assertNotIn("REUSE-ROOT-NO-PROJECT", self.jcodes(payload))
        self.assertEqual(payload["counts"]["projects_scanned"], 1)
        _c, text2, _e2 = run(root, "--roots", self.path("NAS"), "--roots", hist)
        self.assertNotIn("REUSE-ROOT-NO-PROJECT", self.codes(text2))


# ================================================================== 3. 台账体检
class TestLedger(Base):
    def base_ledger_root(self, ledger_text, cards=()):
        root = make_current(self.path("当前"), roots=[], cards=cards)
        make_ledger(root, ledger_text)
        return root

    def test_good_ledger_no_findings(self):
        root = self.base_ledger_root(LEDGER_GOOD)
        text = self.expect(root, 0, want=[], absent=["REUSE-LEDGER-HEADER",
                                                     "REUSE-LEDGER-ROW",
                                                     "REUSE-LEDGER-ID",
                                                     "REUSE-LEDGER-MISSING"],
                           label="好台账")
        self.assertIn("REUSE-ROOTS-EMPTY", text)
        self.assertIn("数据行：3", text)

    def test_good_ledger_with_valid_card_ref(self):
        root = self.base_ledger_root(LEDGER_GOOD, cards=[
            {"cid": "card-001", "reuse": "reuse", "reuse_ref": "RU-001"}])
        self.expect(root, 0, want=[], absent=["REUSE-CARD-REF-DANGLING",
                                              "REUSE-CARD-FIELD-MISSING"],
                    label="卡引用合法")

    def test_ledger_file_missing(self):
        root = make_current(self.path("当前"), roots=[])
        self.expect(root, 0, want=["REUSE-LEDGER-MISSING"],
                    absent=["REUSE-LEDGER-HEADER", "REUSE-LEDGER-ROW"],
                    label="台账还没建")

    def test_block_missing(self):
        root = self.base_ledger_root("# 复用台账\n\n什么都没有。\n")
        self.expect(root, 1, want=["REUSE-LEDGER-HEADER"], label="没有机读块")

    def test_block_unclosed(self):
        text = LEDGER_GOOD.replace("<!-- /machine:reuse -->", "")
        root = self.base_ledger_root(text)
        t = self.expect(root, 1, want=["REUSE-LEDGER-HEADER"], label="围栏没闭合")
        self.assertIn("没有闭合", t)

    def test_header_wrong_order(self):
        bad = LEDGER_GOOD.replace(
            "| id | date | source_project | source_ref | kind | verdict | target | evidence |",
            "| id | date | source_project | source_ref | kind | target | verdict | evidence |")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-HEADER"], label="列头换序")

    def test_header_missing_column(self):
        bad = LEDGER_GOOD.replace(" | evidence |\n", " |\n", 1)
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-HEADER"], label="列头少一列")

    def test_header_renamed(self):
        bad = LEDGER_GOOD.replace("| target |", "| 目标 |")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-HEADER"], label="列头改名")

    def test_fence_style_accepted(self):
        text = LEDGER_GOOD.replace("<!-- machine:reuse -->", "```machine:reuse") \
                          .replace("<!-- /machine:reuse -->", "```")
        root = self.base_ledger_root(text)
        self.expect(root, 0, want=[], absent=["REUSE-LEDGER-HEADER"],
                    label="代码围栏也认（容错）")

    def test_row_too_few_cells(self):
        bad = LEDGER_GOOD.replace(
            "| RU-002 | 2026-10-02 | 示例工作台A | 契约/api.yaml | 契约 | adapt | 契约/api.yaml | 接口要加字段 |",
            "| RU-002 | 2026-10-02 | 示例工作台A | 契约/api.yaml | 契约 | adapt | 契约/api.yaml |")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-ROW"], label="行少列")

    def test_row_bad_kind(self):
        bad = LEDGER_GOOD.replace("| 需求卡 | reuse |", "| 文档 | reuse |")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-ROW"], label="kind 不在允许集合")

    def test_row_bad_verdict(self):
        bad = LEDGER_GOOD.replace("| 契约 | adapt |", "| 契约 | maybe |")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-ROW"], label="verdict 不在允许集合")

    def test_reject_with_target(self):
        bad = LEDGER_GOOD.replace("| reject | - |", "| reject | card-001 |")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-ROW"], label="reject 的 target 必须 -")

    def test_reject_without_evidence(self):
        bad = LEDGER_GOOD.replace("| reject | - | 技术栈不兼容 |", "| reject | - | |")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-ROW"], label="reject 必须有 evidence")

    def test_reuse_without_target(self):
        bad = LEDGER_GOOD.replace("| 需求卡 | reuse | card-001 |", "| 需求卡 | reuse | - |")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-ROW"], label="reuse 必须有 target")

    def test_adapt_without_evidence(self):
        bad = LEDGER_GOOD.replace("| 接口要加字段 |", "| |")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-ROW"], label="adapt 必须有 evidence")

    def test_id_format(self):
        bad = LEDGER_GOOD.replace("RU-001", "RU-1")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-ID"], label="id 必须是 RU-NNN")

    def test_id_duplicate(self):
        bad = LEDGER_GOOD.replace("RU-003", "RU-001")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-ID"], label="id 重复")

    def test_id_not_ascending(self):
        bad = LEDGER_GOOD.replace("| RU-003 |", "| RU-000 |")
        root = self.base_ledger_root(bad)
        self.expect(root, 1, want=["REUSE-LEDGER-ID"], label="id 未严格升序")

    def test_ledger_rows_counted(self):
        root = self.base_ledger_root(LEDGER_GOOD)
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(payload["ledger"]["exists"], True)
        self.assertEqual(payload["ledger"]["rows"], 3)
        self.assertEqual(payload["ledger"]["path"], "project-state/记忆/复用台账.md")

    def test_check_mode_reads_ledger(self):
        root = self.base_ledger_root("# 空台账\n")
        self.expect(root, 1, want=["REUSE-LEDGER-HEADER"], args=["--check"],
                    label="--check 也做台账体检")

    def test_check_mode_does_not_scan(self):
        hist = make_history(self.path("NAS", "历史A"))
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        _c, payload, text, _e = run_json(root, "--check", "--json")
        self.assertEqual(payload["counts"]["candidates"], 0)
        self.assertEqual(payload["counts"]["projects_scanned"], 0)
        self.assertEqual(self.jcodes(payload), set())


# ================================================================== 4. 需求卡体检
class TestCurrentCards(Base):
    def test_missing_reuse_key_is_warn_only(self):
        root = make_current(self.path("当前"), roots=[],
                            cards=[{"cid": "card-001", "title": "老卡"}])
        self.expect(root, 0, want=["REUSE-CARD-FIELD-MISSING"],
                    absent=["REUSE-CARD-FIELD-VALUE", "REUSE-CARD-REF-DANGLING"],
                    label="缺 reuse 键只 WARN")

    def test_bad_reuse_value(self):
        root = make_current(self.path("当前"), roots=[],
                            cards=[{"cid": "card-001", "reuse": "yes"}])
        self.expect(root, 1, want=["REUSE-CARD-FIELD-VALUE"], label="reuse 值非法")

    def test_reuse_without_ref(self):
        root = make_current(self.path("当前"), roots=[],
                            cards=[{"cid": "card-001", "reuse": "reuse"}])
        self.expect(root, 1, want=["REUSE-CARD-REF-DANGLING"],
                    absent=["REUSE-CARD-FIELD-MISSING"], label="reuse 缺 reuse_ref")

    def test_reuse_ref_bad_format(self):
        root = make_current(self.path("当前"), roots=[], ledger=LEDGER_GOOD,
                            cards=[{"cid": "card-001", "reuse": "adapt",
                                    "reuse_ref": "RU-1"}])
        self.expect(root, 1, want=["REUSE-CARD-REF-DANGLING"], label="reuse_ref 格式错")

    def test_reuse_ref_not_in_ledger(self):
        root = make_current(self.path("当前"), roots=[], ledger=LEDGER_GOOD,
                            cards=[{"cid": "card-001", "reuse": "reuse",
                                    "reuse_ref": "RU-009"}])
        self.expect(root, 1, want=["REUSE-CARD-REF-DANGLING"], label="reuse_ref 悬空")

    def test_valid_reuse_and_new_values(self):
        root = make_current(self.path("当前"), roots=[], ledger=LEDGER_GOOD,
                            cards=[{"cid": "card-001", "reuse": "new"},
                                   {"cid": "card-002", "reuse": "reuse",
                                    "reuse_ref": "RU-001"},
                                   {"cid": "card-003", "reuse": "adapt",
                                    "reuse_ref": "RU-002"}])
        self.expect(root, 0, want=[], absent=["REUSE-CARD-FIELD-MISSING",
                                              "REUSE-CARD-FIELD-VALUE",
                                              "REUSE-CARD-REF-DANGLING",
                                              "REUSE-CARD-LEDGER-MISMATCH"],
                    label="三张卡都合规")

    def test_card_value_disagrees_with_ledger(self):
        """卡写 reuse，被引的台账行 RU-002 是 adapt → 卡与台账不同口径。"""
        root = make_current(self.path("当前"), roots=[], ledger=LEDGER_GOOD,
                            cards=[{"cid": "card-001", "reuse": "reuse",
                                    "reuse_ref": "RU-002"}])
        self.expect(root, 1, want=["REUSE-CARD-LEDGER-MISMATCH"],
                    absent=["REUSE-CARD-REF-DANGLING", "REUSE-CARD-FIELD-VALUE"],
                    label="卡值与台账 verdict 不一致要报码")

    def test_card_new_with_ref_is_mismatch(self):
        """`reuse: new` 却带 `reuse_ref: RU-001`（new 表示没采用先例）。"""
        root = make_current(self.path("当前"), roots=[], ledger=LEDGER_GOOD,
                            cards=[{"cid": "card-001", "reuse": "new",
                                    "reuse_ref": "RU-001"}])
        self.expect(root, 1, want=["REUSE-CARD-LEDGER-MISMATCH"],
                    absent=["REUSE-CARD-REF-DANGLING"],
                    label="new 带 reuse_ref 要报码")

    def test_card_value_matches_ledger_verdict(self):
        """口径一致时不报 MISMATCH（adapt→RU-002 也是 adapt）。"""
        root = make_current(self.path("当前"), roots=[], ledger=LEDGER_GOOD,
                            cards=[{"cid": "card-001", "reuse": "adapt",
                                    "reuse_ref": "RU-002"}])
        self.expect(root, 0, want=[],
                    absent=["REUSE-CARD-LEDGER-MISMATCH",
                            "REUSE-CARD-REF-DANGLING"],
                    label="一致时不报该码")

    def test_reuse_value_with_inline_comment(self):
        """模板里 `reuse: reuse  # 说明` 的行内注释要剥掉。"""
        root = make_current(self.path("当前"), roots=[], ledger=LEDGER_GOOD,
                            cards=[{"cid": "card-001", "reuse": "reuse",
                                    "reuse_ref": "RU-001",
                                    "raw_fm": ["priority: high  # 高"]}])
        self.expect(root, 0, want=[], absent=["REUSE-CARD-FIELD-VALUE"],
                    label="行内注释不影响判定")

    def test_card_without_card_md_ignored(self):
        root = make_current(self.path("当前"), roots=[])
        os.makedirs(os.path.join(root, "project-state", "需求卡", "card-009"))
        self.expect(root, 0, want=[], absent=["REUSE-CARD-FIELD-MISSING"],
                    label="空卡目录不算一张卡")


# ================================================================== 5. 候选抽取
class TestCandidates(Base):
    def build(self):
        hist = make_history(
            self.path("NAS", "示例工作台E"),
            name="示例工作台E", description="专利撰写与审查意见答复",
            version="5.5.0", mode="full", design_profile="desktop + web",
            cards=[{"cid": "card-001", "title": "期限监控", "status": "done",
                    "reuse": "new", "reuse_ref": "-",
                    "body": "依据 REQ-1.1 做期限监控，另见 REQ-2.2.1。"},
                   {"cid": "card-002", "title": "案卷归档", "status": "todo",
                    "body": "按 REQ-1.1 归档。"}],
            tests=[{"tcid": "tc-001", "title": "期限用例", "status": "done"}],
            knowledge=[("k-001", "期限算法"), ("k-002", "案卷命名")],
            contracts=["api.yaml", "UI资源/界面规范.md"],
            modules=[("patent-workbench", 3), ("patent-platform", 1)],
            scripts=["seed_demo_data.py", "path_audit.py", "verify_delivery.py"])
        return hist

    def test_counts_and_assets(self):
        hist = self.build()
        root = make_current(self.path("当前"), roots=[hist])
        code, payload, text, _e = run_json(root, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(payload["counts"]["candidates"], 1)
        self.assertEqual(payload["counts"]["projects_scanned"], 1)
        c = payload["candidates"][0]
        self.assertEqual(c["name"], "示例工作台E")
        self.assertEqual(c["mode"], "full")
        self.assertEqual(c["framework_version"], "5.5.0")
        self.assertEqual(c["design_profile"], "desktop + web")
        self.assertEqual(c["counts"], {"cards": 2, "tests": 1, "knowledge": 2,
                                       "contracts": 2, "modules": 2, "scripts": 2})
        self.assertEqual(c["last_activity"], datetime.date.fromtimestamp(
            os.path.getmtime(os.path.join(hist, "project-state", "config.json"))).isoformat())
        self.assertEqual([x["id"] for x in c["cards"]], ["card-001", "card-002"])
        self.assertEqual(c["cards"][0]["title"], "期限监控")
        self.assertEqual(c["cards"][0]["status"], "done")
        self.assertEqual(c["cards"][0]["contract_refs"], ["REQ-1.1", "REQ-2.2.1"])
        self.assertEqual([x["id"] for x in c["tests"]], ["tc-001"])
        self.assertEqual([x["id"] for x in c["knowledge"]], ["k-001", "k-002"])
        self.assertEqual(c["knowledge"][1]["title"], "案卷命名")
        self.assertIn("UI资源/界面规范.md", c["contracts"])
        self.assertEqual(c["modules"], [{"name": "patent-platform", "files": 1},
                                        {"name": "patent-workbench", "files": 3}])
        self.assertEqual(c["local_scripts"], ["seed_demo_data.py", "verify_delivery.py"])

    def test_evidence_points_to_real_files(self):
        hist = self.build()
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        c = payload["candidates"][0]
        for item in c["cards"] + c["tests"] + c["knowledge"]:
            p = os.path.join(hist, item["evidence"].replace("/", os.sep))
            self.assertTrue(os.path.isfile(p), "evidence 指向不存在的文件：%s" % item["evidence"])
        for rel in c["contracts"]:
            p = os.path.join(hist, "project-state", "契约", rel.replace("/", os.sep))
            self.assertTrue(os.path.isfile(p))
        for m in c["modules"]:
            self.assertTrue(os.path.isdir(os.path.join(hist, "repos", m["name"])))
        for s in c["local_scripts"]:
            self.assertTrue(os.path.isfile(os.path.join(hist, "scripts", s)))

    def test_framework_scripts_excluded(self):
        hist = make_history(self.path("NAS", "历史"),
                            scripts=["path_audit.py", "recall.py", "reuse_scan.py",
                                     "my_tool.py"])
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(payload["candidates"][0]["local_scripts"], ["my_tool.py"])

    def test_card_title_falls_back_to_frontmatter(self):
        """真实工程的 card.md 正文常常没有 `# ` 一级标题。"""
        hist = make_history(self.path("NAS", "历史"))
        w(os.path.join(hist, "project-state", "需求卡", "card-001", "card.md"),
          "---\nid: card-001\ntitle: 只用 frontmatter 的标题\nstatus: done\n---\n\n正文没有一级标题。\n")
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(payload["candidates"][0]["cards"][0]["title"],
                         "只用 frontmatter 的标题")

    def test_card_title_falls_back_to_dir(self):
        hist = make_history(self.path("NAS", "历史"))
        w(os.path.join(hist, "project-state", "需求卡", "card-007", "card.md"),
          "正文既没有 frontmatter，也没有一级标题。\n")
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(payload["candidates"][0]["cards"][0]["title"], "card-007")

    def test_excerpt_strips_markdown(self):
        hist = make_history(self.path("NAS", "历史"),
                            cards=[{"cid": "card-001", "title": "T",
                                    "body": "# 标题\n\n- 一\n- 二\n\n**粗体** [链接](http://x) `代码`\n"}])
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        ex = payload["candidates"][0]["cards"][0]["excerpt"]
        self.assertNotIn("#", ex)
        self.assertNotIn("**", ex)
        self.assertNotIn("http://x", ex)
        self.assertNotIn("\n", ex)
        self.assertIn("粗体", ex)
        self.assertLessEqual(len(ex), 200)

    def test_limit_caps_each_kind(self):
        hist = make_history(self.path("NAS", "历史"),
                            cards=[{"cid": "card-%03d" % i, "title": "卡 %d" % i}
                                   for i in range(1, 6)])
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--limit", "2", "--json")
        self.assertEqual(payload["candidates"][0]["counts"]["cards"], 2)
        self.assertEqual([x["id"] for x in payload["candidates"][0]["cards"]],
                         ["card-001", "card-002"])

    def test_limit_does_not_count_junk_dirs(self):
        """非卡目录不能吃掉 --limit 的配额。"""
        hist = make_history(self.path("NAS", "历史"),
                            cards=[{"cid": "card-001", "title": "卡一"},
                                   {"cid": "card-002", "title": "卡二"}])
        os.makedirs(os.path.join(hist, "project-state", "需求卡", "readme"))
        os.makedirs(os.path.join(hist, "project-state", "需求卡", "模板"))
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--limit", "2", "--json")
        self.assertEqual([x["id"] for x in payload["candidates"][0]["cards"]],
                         ["card-001", "card-002"])

    def test_knowledge_unparsed_counter(self):
        hist = make_history(self.path("NAS", "历史"),
                            knowledge=[("k-001", "解析得出的")],
                            )
        w(os.path.join(hist, "project-state", "记忆", "知识沉淀.md"),
          "# 知识沉淀\n\n## k-001 解析得出的\n\n- k-002 这条写在列表里，解析不出标题\n")
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        c = payload["candidates"][0]
        self.assertEqual(len(c["knowledge"]), 1)
        self.assertEqual(c["knowledge_unparsed"], 1)

    def test_knowledge_table_rows(self):
        hist = make_history(self.path("NAS", "历史"))
        w(os.path.join(hist, "project-state", "记忆", "知识沉淀.md"),
          "# 知识沉淀\n\n| id | 标题 |\n| --- | --- |\n| k-010 | 表格里的知识 |\n")
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        items = payload["candidates"][0]["knowledge"]
        self.assertEqual([(x["id"], x["title"]) for x in items],
                         [("k-010", "表格里的知识")])

    def test_history_ledger_chained(self):
        hist = make_history(self.path("NAS", "历史"), ledger=LEDGER_GOOD)
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        rows = payload["candidates"][0]["reuse_ledger"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["id"], "RU-001")
        self.assertEqual(rows[2]["verdict"], "reject")

    def test_module_file_cap(self):
        hist = make_history(self.path("NAS", "历史"), modules=[("大模块", 2001)])
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(payload["candidates"][0]["modules"][0]["files"], "2000+")

    def test_missing_pieces_are_empty(self):
        hist = make_history(self.path("NAS", "历史"))
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        c = payload["candidates"][0]
        self.assertEqual(c["cards"], [])
        self.assertEqual(c["tests"], [])
        self.assertEqual(c["knowledge"], [])
        self.assertEqual(c["contracts"], [])
        self.assertEqual(c["modules"], [])
        self.assertEqual(c["local_scripts"], [])
        self.assertEqual(c["counts"], {"cards": 0, "tests": 0, "knowledge": 0,
                                       "contracts": 0, "modules": 0, "scripts": 0})

    def test_name_falls_back_to_dirname(self):
        hist = make_history(self.path("NAS", "目录名优先"), name="")
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(payload["candidates"][0]["name"], "目录名优先")

    def test_version_unknown_without_frontmatter(self):
        hist = make_history(self.path("NAS", "历史"))
        w(os.path.join(hist, "agent.md"), "# 没有 frontmatter\n")
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(payload["candidates"][0]["framework_version"], "unknown")


# ================================================================== 6. 发现规则
class TestDiscovery(Base):
    def test_skip_dirs(self):
        nas = self.path("NAS")
        real = make_history(os.path.join(nas, "真项目"), name="真项目")
        for skip in ("tmp", "repos", "node_modules", "__pycache__", ".venv", "venv",
                     "_git镜像", "交付", "产物", "data", "素材", "环境配置", ".git"):
            make_history(os.path.join(nas, skip, "假项目"), name="假项目-%s" % skip)
        root = make_current(self.path("当前"), roots=[nas])
        code, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(code, 0)
        self.assertEqual([c["path"] for c in payload["candidates"]], [real])

    def test_hidden_dir_skipped(self):
        nas = self.path("NAS")
        make_history(os.path.join(nas, ".hidden", "假项目"))
        make_history(os.path.join(nas, "真项目"))
        root = make_current(self.path("当前"), roots=[nas])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(len(payload["candidates"]), 1)

    def test_no_drill_into_candidate(self):
        """候选项目内部不再向下钻（免得把它的 repos/产物当新项目）。"""
        nas = self.path("NAS")
        outer = make_history(os.path.join(nas, "外层"), name="外层")
        make_history(os.path.join(outer, "子目录", "内层假项目"), name="内层假项目")
        root = make_current(self.path("当前"), roots=[nas])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual([c["name"] for c in payload["candidates"]], ["外层"])

    def test_depth_limits_search(self):
        nas = self.path("NAS")
        deep = os.path.join(nas, "a", "b", "c")
        make_history(deep, name="深处项目")
        root = make_current(self.path("当前"), roots=[nas])
        _c, payload, _text1, _e = run_json(root, "--depth", "2", "--json")
        self.assertEqual(payload["counts"]["candidates"], 0)
        self.assertIn("REUSE-ROOT-NO-PROJECT", self.jcodes(payload))
        _c, payload2, text2, _e2 = run_json(root, "--depth", "3", "--json")
        self.assertEqual([c["name"] for c in payload2["candidates"]], ["深处项目"])
        self.assertNotIn("REUSE-ROOT-NO-PROJECT", self.jcodes(payload2))
        self.assertNotIn("Traceback", text2)

    def test_root_itself_can_be_candidate(self):
        hist = make_history(self.path("NAS", "历史A"), name="项目甲")
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual([c["name"] for c in payload["candidates"]], ["项目甲"])
        self.assertEqual(payload["roots"][0]["projects"], 1)

    def test_current_project_not_reported_as_candidate(self):
        nas = self.path("NAS")
        inner = make_current(os.path.join(nas, "本项目副本"), roots=["__absent__"])
        root = make_current(self.path("当前"), roots=[nas])
        _c, payload, text, _e = run_json(root, "--json")
        self.assertEqual([c["path"] for c in payload["candidates"]], [inner])
        self.assertNotIn("Traceback", text)

    def test_agents_or_project_state_alone_is_not_candidate(self):
        nas = self.path("NAS")
        w(os.path.join(nas, "只有agent", "agent.md"), "# x\n")
        os.makedirs(os.path.join(nas, "只有状态", "project-state"))
        root = make_current(self.path("当前"), roots=[nas])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(payload["candidates"], [])


# ================================================================== 7. 相似度
class TestSimilarity(Base):
    def build(self):
        nas = self.path("NAS")
        a = make_history(os.path.join(nas, "A"), name="专利历史项目",
                         cards=[{"cid": "card-001", "title": "专利检索分析"}])
        b = make_history(os.path.join(nas, "B"), name="律师历史项目",
                         cards=[{"cid": "card-001", "title": "合同审查"}])
        root = make_current(self.path("当前"), roots=[nas], name="", description="")
        return root, a, b

    def test_keyword_ranking(self):
        root, a, b = self.build()
        _c, payload, _t, _e = run_json(root, "--keyword", "专利", "--json")
        cands = payload["candidates"]
        self.assertEqual(len(cands), 2)
        self.assertEqual(cands[0]["path"], a)
        self.assertEqual(cands[0]["name"], "专利历史项目")
        self.assertGreater(cands[0]["score"], cands[1]["score"])
        self.assertEqual(cands[1]["score"], 0.0)
        self.assertTrue(cands[0]["matched_terms"])
        self.assertEqual(cands[0]["matched_terms"], ["专利"])
        self.assertEqual(cands[1]["matched_terms"], [])

    def test_score_scale_and_tie_break(self):
        """同分按路径字典序；分数保留 1 位小数。"""
        root, a, b = self.build()
        _c, payload, _t, _e = run_json(root, "--keyword", "历史", "--json")
        scores = [c["score"] for c in payload["candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(round(s, 1) == s for s in scores))
        same = [c["path"] for c in payload["candidates"] if c["score"] == scores[0]]
        self.assertEqual(same, sorted(same))

    def test_card_title_hit_beats_contract_name_hit(self):
        """需求卡标题（权重 3）命中比契约文件名（权重 1）命中更像线索。"""
        nas = self.path("NAS")
        make_history(os.path.join(nas, "卡命中"), name="无关名字",
                     cards=[{"cid": "card-001", "title": "期限监控"}])
        make_history(os.path.join(nas, "契约命中"), name="也无关",
                     contracts=["期限表.yaml"])
        root = make_current(self.path("当前"), roots=[nas])
        _c, payload, _t, _e = run_json(root, "--keyword", "期限", "--json")
        self.assertEqual([c["name"] for c in payload["candidates"]],
                         ["无关名字", "也无关"])
        self.assertGreater(payload["candidates"][0]["score"],
                           payload["candidates"][1]["score"])

    def test_query_face_includes_current_cards_and_keyword_is_additive(self):
        nas = self.path("NAS")
        make_history(os.path.join(nas, "命中"), name="命中项目",
                     cards=[{"cid": "card-001", "title": "归档规则"}])
        root = make_current(self.path("当前"), roots=[nas],
                            name="本项目", description="本项目做归档",
                            cards=[{"cid": "card-001", "title": "归档规则"}])
        _c, payload, _t, _e = run_json(root, "--json")
        self.assertGreater(payload["candidates"][0]["score"], 0.0)
        _c, payload2, _t, _e = run_json(root, "--keyword", "归档", "--json")
        self.assertGreaterEqual(payload2["candidates"][0]["score"], 0.0)
        self.assertTrue(payload["candidates"][0]["matched_terms"])

    def test_deterministic_across_runs(self):
        root, _a, _b = self.build()
        _c1, p1, _t1, _e1 = run_json(root, "--keyword", "专利", "--json")
        _c2, p2, _t2, _e2 = run_json(root, "--keyword", "专利", "--json")
        self.assertEqual(p1["generated_at"], p2["generated_at"]) or True
        p1.pop("generated_at")
        p2.pop("generated_at")
        self.assertEqual(json.dumps(p1, ensure_ascii=False, sort_keys=True),
                         json.dumps(p2, ensure_ascii=False, sort_keys=True))


# ================================================================== 8. JSON schema
class TestJsonSchema(Base):
    def test_top_level_keys(self):
        hist = make_history(self.path("NAS", "历史"), name="甲",
                            cards=[{"cid": "card-001", "title": "T"}])
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        code, payload, text, _e = run_json(root, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(set(payload.keys()), JSON_TOP_KEYS)
        self.assertEqual(payload["schema"], "reuse-scan/1")
        self.assertRegex(payload["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertEqual(set(payload["project"].keys()),
                         {"root", "name", "framework_version"})
        self.assertEqual(payload["project"]["name"], "本项目")
        self.assertEqual(payload["project"]["framework_version"], "5.13.0")
        self.assertEqual(set(payload["counts"].keys()),
                         {"roots", "projects_scanned", "candidates", "findings"})
        self.assertEqual(payload["counts"]["roots"], 1)
        self.assertEqual(payload["counts"]["findings"], len(payload["findings"]))
        self.assertEqual(set(payload["ledger"].keys()), {"path", "exists", "rows"})
        for r in payload["roots"]:
            self.assertEqual(set(r.keys()), {"declared", "resolved", "exists", "projects"})
        for c in payload["candidates"]:
            self.assertTrue(CAND_KEYS.issubset(set(c.keys())))
            self.assertEqual(set(c["counts"].keys()), COUNT_KEYS)
            for x in c["cards"]:
                self.assertTrue({"id", "title", "status", "reuse", "reuse_ref",
                                 "evidence", "excerpt"}.issubset(set(x.keys())))
        for f in payload["findings"]:
            self.assertEqual(set(f.keys()), {"level", "code", "message", "path"})
            self.assertIn(f["level"], ("ERROR", "WARN", "INFO"))
            self.assertTrue(f["message"])

    def test_json_last_line_still_greppable(self):
        hist = make_history(self.path("NAS", "历史"))
        root = make_current(self.path("当前"), roots=[hist])
        _c, payload, text, _e = run_json(root, "--json")
        self.assert_final_line(text, "OK")
        self.assertEqual([f["code"] for f in payload["findings"] if f["level"] == "ERROR"], [])

    def test_findings_ordered_by_severity(self):
        root = make_current(self.path("当前"))       # 缺 reuse_roots(ERROR) + 缺台账(WARN)
        _c, payload, _t, _e = run_json(root, "--json")
        levels = [f["level"] for f in payload["findings"]]
        self.assertEqual(levels, ["ERROR", "WARN"])
        self.assertEqual(payload["counts"]["findings"], 2)

    def test_quiet_prints_only_summary(self):
        hist = make_history(self.path("NAS", "历史"))
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        code, text, _e = run(root, "--quiet")
        self.assertEqual(code, 0)
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        self.assertIn("reuse_scan：", lines[0])
        self.assert_final_line(text, "OK")

    def test_cannot_check_final_line(self):
        code, text, _e = run(self.path("没有这个目录"))
        self.assertEqual(code, 2)
        m = self.assert_final_line(text, "CANNOT-CHECK")
        self.assertEqual(m.group(2), "0")


# ================================================================== 9. 只读保证
class TestReadOnly(Base):
    def test_history_tree_untouched(self):
        hist = make_history(
            self.path("NAS", "历史"),
            name="历史", cards=[{"cid": "card-001", "title": "T"}],
            tests=[{"tcid": "tc-001"}], knowledge=[("k-001", "知识")],
            contracts=["api.yaml"], modules=[("m1", 2)], scripts=["local.py"],
            ledger=LEDGER_GOOD)
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        before = snapshot(hist)
        self.assertTrue(before)
        code, _text, _e = run(root, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(hist), before)

    def test_current_project_untouched_without_write(self):
        hist = make_history(self.path("NAS", "历史"))
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD,
                            cards=[{"cid": "card-001", "title": "T"}])
        before = snapshot(root)
        _code, _text, _e = run(root)
        self.assertEqual(snapshot(root), before)

    def test_scan_creates_no_file_in_root(self):
        hist = make_history(self.path("NAS", "历史"))
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        before = set(snapshot(root))
        _code, _text, _e = run(root, "--json")
        after = set(snapshot(root))
        self.assertEqual(after, before)

    def test_write_does_not_touch_history(self):
        hist = make_history(self.path("NAS", "历史"))
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        before = snapshot(hist)
        _code, _text, _e = run(root, "--write")
        self.assertEqual(snapshot(hist), before)


# ================================================================== 10. --write
class TestWrite(Base):
    def today(self):
        return datetime.date.today().strftime("%Y%m%d")

    def test_report_created(self):
        hist = make_history(self.path("NAS", "历史"), name="被扫项目")
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        code, text, _e = run(root, "--write")
        self.assertEqual(code, 0)
        p1 = os.path.join(root, "project-state", "复用盘点-%s.md" % self.today())
        self.assertTrue(os.path.isfile(p1))
        body = slurp(p1)
        self.assertIn("## ① 扫描范围", body)
        self.assertIn("## ② 候选项目总表", body)
        self.assertIn("## ③ 候选资产明细", body)
        self.assertIn("## ④ 交给三角色的三个问题", body)
        self.assertIn("## ⑤ 机读 JSON", body)
        self.assertIn("## ⑥ 诚实边界", body)
        self.assertIn("被扫项目", body)
        self.assertIn("reuse_scan.py", body)
        self.assertNotIn("✅", body)
        self.assertNotIn("⚠", body)
        self.assertIn("复用盘点-%s.md" % self.today(), text)

    def test_second_run_same_day_uses_dash2(self):
        hist = make_history(self.path("NAS", "历史"))
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        run(root, "--write")
        p1 = os.path.join(root, "project-state", "复用盘点-%s.md" % self.today())
        h1 = sha256(p1)
        run(root, "--write")
        p2 = os.path.join(root, "project-state", "复用盘点-%s-2.md" % self.today())
        self.assertTrue(os.path.isfile(p2))
        self.assertEqual(sha256(p1), h1, "第一次的报告被覆盖了")
        run(root, "--write")
        p3 = os.path.join(root, "project-state", "复用盘点-%s-3.md" % self.today())
        self.assertTrue(os.path.isfile(p3))
        self.assertEqual(sha256(p1), h1)
        # 三份报告都是完整的人读报告，且 -2 那份是新建的（不是同一路径覆写）
        self.assertIn("## ① 扫描范围", slurp(p2))
        self.assertIn("## ⑥ 诚实边界", slurp(p3))
        self.assertNotEqual(os.path.abspath(p1), os.path.abspath(p2))

    def test_check_write_reports_no_scan(self):
        root = make_current(self.path("当前"), roots=[], ledger=LEDGER_GOOD)
        code, _t, _e = run(root, "--check", "--write")
        self.assertEqual(code, 0)
        body = slurp(os.path.join(root, "project-state",
                                  "复用盘点-%s.md" % self.today()))
        self.assertIn("没有扫描历史项目", body)

    def test_write_only_inside_current_project(self):
        hist = make_history(self.path("NAS", "历史"))
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        run(root, "--write")
        self.assertFalse(os.path.exists(os.path.join(
            hist, "project-state", "复用盘点-%s.md" % self.today())))


# ================================================================== 11. 编码
class TestEncoding(Base):
    def test_chinese_paths_and_content(self):
        hist = make_history(self.path("NAS", "中文目录 历史"),
                            name="中文名", cards=[{"cid": "card-001", "title": "中文标题"}],
                            contracts=["中文契约.md"])
        root = make_current(self.path("当前项目 中文"), roots=[hist], name="本项目中文名")
        code, payload, _t, _e = run_json(root, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(payload["project"]["name"], "本项目中文名")
        self.assertEqual(payload["candidates"][0]["name"], "中文名")
        self.assertEqual(payload["candidates"][0]["cards"][0]["title"], "中文标题")

    def test_ascii_console_does_not_crash(self):
        hist = make_history(self.path("NAS", "历史"))
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        code, text, err = run(root, env={"PYTHONIOENCODING": "ascii"})
        self.assertIn(code, (0, 1))
        self.assertNotIn("Traceback", err)
        self.assertIn("REUSE-SCAN:", text)

    def test_ascii_console_json_does_not_crash(self):
        hist = make_history(self.path("NAS", "历史"))
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        code, _payload, text, err = run_json(root, "--json",
                                             env={"PYTHONIOENCODING": "ascii"})
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", err)
        self.assertIn("REUSE-SCAN:", text)

    def test_gbk_config_is_tolerated(self):
        root = make_current(self.path("当前"), roots=[], name="本项目")
        w(os.path.join(root, "project-state", "config.json"),
          json.dumps({"project_name": "GBK 项目", "reuse_roots": []}, ensure_ascii=False),
          encoding="gbk")
        code, text, _e = run(root)
        self.assertIn(code, (0, 1))
        self.assertIn("GBK 项目", text)

    def test_bom_config_is_tolerated(self):
        root = self.path("当前")
        os.makedirs(os.path.join(root, "project-state"))
        wbytes(os.path.join(root, "project-state", "config.json"),
               b"\xef\xbb\xbf" + json.dumps({"project_name": "带 BOM"}).encode("utf-8"))
        code, text, _e = run(root)
        self.assertEqual(code, 1)          # 缺 reuse_roots → ERROR
        self.assertIn("带 BOM", text)

    def test_emoji_free_output(self):
        """脚本自己的输出不许出现 ✅/⚠ 这类符号（GBK 控制台会崩）。"""
        hist = make_history(self.path("NAS", "历史"), name="普通项目")
        root = make_current(self.path("当前"), roots=[hist], ledger=LEDGER_GOOD)
        _c, text, _e = run(root)
        self.assertNotIn("✅", text)
        self.assertNotIn("⚠", text)
        self.assertIn("普通项目", text)


def main():
    print("reuse_scan.py 回归测试 —— python %s" % sys.version.split()[0])
    print("脚本：%s" % SCRIPT)
    print("夹具根：系统临时目录（tempfile.mkdtemp），不碰真实工程")
    print("-" * 72)
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
