#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_scope_audit.py — `scope_audit.py`（探索域检查器，v5.26.0）回归用例

守四件事：
  ① 三态：没探索域 / 无机读块 / 坏列头 / **空表** → 2（空地图 = 无法检查，不是干净）
  ② 族×类型学：族必须是三族之一、类型前缀必须与族匹配、编号必须在范围内（F1~F11 / D1~D6 / E1~E8）
  ③ 判据与证据：判据来源路径必须真实存在（悬空 = ERROR）；判据为空却非「空洞」= WARN；
     **体验族证据必须含 截图/CLS/录屏**
  ④ 三级指针：backlog 每条 WU 的判据指针必须解析到「探索域 S-NNN」或「真实存在的契约文件」，
     指不到/悬空/形式错 = ERROR（防凭空派活）
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

SCOPE_COLS = ("| id | family | kind | target | identity | channel | face | criterion "
              "| risk | evidence | status |")
SCOPE_SEP = "| " + " | ".join(["---"] * 11) + " |"
S_OPEN, S_CLOSE = "<!-- machine:scope -->", "<!-- /machine:scope -->"
B_COLS = "| id | 类型 | 对象 | 判据指针 | 预估token | 来源 | 优先级 | 状态 |"
B_SEP = "| " + " | ".join(["---"] * 8) + " |"
B_OPEN, B_CLOSE = "<!-- machine:backlog -->", "<!-- /machine:backlog -->"


def load_module():
    spec = importlib.util.spec_from_file_location("scope_audit", str(HERE.parent / "scope_audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = load_module()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="scp_")
        self.root = Path(self.tmp)
        (self.root / "project-state" / "契约").mkdir(parents=True)
        # 两个真实存在的契约文件，供判据指针使用
        self.write("project-state/契约/产品需求文档.md", "# PRD\n\nREQ-12 权限：降权后不得查看\n")
        self.write("project-state/契约/EXPERIENCE.md", "# EXPERIENCE\n\nUX-INS-03 提交后给出进度反馈\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def probe(self, sid="S-001", family="负面", kind="F4", target="审批单详情", identity="刚被降权",
              channel="界面", face="直达链接", criterion="契约/产品需求文档.md#REQ-12",
              risk="P0", evidence="截图,DOM", status="待探"):
        return (f"| {sid} | {family} | {kind} | {target} | {identity} | {channel} | {face} "
                f"| {criterion} | {risk} | {evidence} | {status} |")

    def scope(self, rows, header=SCOPE_COLS):
        body = "\n".join([header, SCOPE_SEP] + rows)
        self.write("project-state/契约/探索域.md", f"# 探索域\n\n{S_OPEN}\n{body}\n{S_CLOSE}\n")

    def backlog(self, rows):
        body = "\n".join([B_COLS, B_SEP] + rows)
        self.write("project-state/契约/探索backlog.md", f"# backlog\n\n{B_OPEN}\n{body}\n{B_CLOSE}\n")

    def brow(self, wid="WU-001", ptr="契约/探索域.md#S-001", status="待干"):
        return f"| {wid} | 发现 | 审批单详情 | {ptr} | 150000 | 供给器 | P0 | {status} |"

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = MOD.main(["scope_audit.py", str(self.root), *args])
        return rc, out.getvalue()


class ThreeState(Base):
    def test_no_scope_is_2(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("无法检查", out)

    def test_no_block_is_2(self):
        self.write("project-state/契约/探索域.md", "# 探索域\n\n没有机读块\n")
        rc, _ = self.run_cli()
        self.assertEqual(rc, 2)

    def test_bad_columns_is_2(self):
        self.scope([self.probe()], header="| id | family | target |")
        rc, _ = self.run_cli()
        self.assertEqual(rc, 2)

    def test_empty_scope_is_2(self):
        """空表 = 地图没画，backlog 只能凭空派活 → 2（不是干净）"""
        self.scope([])
        rc, out = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("空", out)


class Clean(Base):
    def test_clean_is_0(self):
        self.scope([self.probe()])
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("ERROR 0", out)

    def test_three_families_ok(self):
        self.scope([
            self.probe("S-001", "负面", "F4"),
            self.probe("S-002", "数据", "D1", target="导出审批单", identity="普通用户",
                       channel="界面", face="导出", criterion="契约/产品需求文档.md#REQ-20",
                       risk="P1", evidence="接口,DOM"),
            self.probe("S-003", "体验", "E1", target="提交审批按钮",
                       criterion="契约/EXPERIENCE.md#UX-INS-03", risk="P2",
                       evidence="CLS,截图"),
        ])
        rc, out = self.run_cli("--json")
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertEqual(data["rows"], 3)
        self.assertEqual(data["families"], {"负面": 1, "数据": 1, "体验": 1})

    def test_hollow_criterion_empty_ok_with_warn(self):
        self.scope([self.probe(criterion="-", status="空洞")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN", out)


class MustBlock(Base):
    def test_id_order(self):
        self.scope([self.probe("S-002"), self.probe("S-001")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-ID-ORDER", out)

    def test_bad_family(self):
        self.scope([self.probe(family="性能")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-FAMILY", out)

    def test_kind_family_mismatch(self):
        """族=负面 却写了体验类 E1 → 串族 ERROR"""
        self.scope([self.probe(family="负面", kind="E1")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-KIND-FAMILY", out)

    def test_kind_out_of_range(self):
        self.scope([self.probe(family="体验", kind="E9", evidence="截图")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-KIND-RANGE", out)

    def test_target_empty(self):
        self.scope([self.probe(target="-")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-TARGET", out)

    def test_criterion_dangling(self):
        self.scope([self.probe(criterion="契约/不存在.md#REQ-9")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-CRITERION-DANGLING", out)

    def test_experience_without_visual_evidence(self):
        """体验族只写接口证据 = 体验问题不可复核 → ERROR"""
        self.scope([self.probe(family="体验", kind="E2", evidence="接口", risk="P1")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-EVIDENCE-VISUAL", out)

    def test_bad_risk(self):
        self.scope([self.probe(risk="P9")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-RISK", out)

    def test_row_width(self):
        self.scope(["| S-001 | 负面 | F4 |"])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-ROW-WIDTH", out)


class BacklogPointers(Base):
    def test_pointer_to_scope_ok(self):
        self.scope([self.probe()])
        self.backlog([self.brow()])
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)

    def test_pointer_missing(self):
        self.scope([self.probe()])
        self.backlog([self.brow(ptr="-")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-BACKLOG-NO-POINTER", out)

    def test_pointer_dangling_probe(self):
        self.scope([self.probe()])
        self.backlog([self.brow(ptr="契约/探索域.md#S-999")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-BACKLOG-DANGLING", out)

    def test_pointer_to_missing_contract(self):
        self.scope([self.probe()])
        self.backlog([self.brow(ptr="契约/缺失契约.md#X-1")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-BACKLOG-CRITERION-DANGLING", out)

    def test_pointer_bad_form(self):
        self.scope([self.probe()])
        self.backlog([self.brow(ptr="某页面")])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("SCP-BACKLOG-POINTER-FORM", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
