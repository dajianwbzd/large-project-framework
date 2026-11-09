#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_explore_loop_audit.py — 探索→修复卡→评估→归档 闭环检查器回归（v5.27.0）

守五件事：
  ① 三态：既没有探索域也没有探索修复卡 → 2（没做过探索闭环，2 ≠ 没问题）；
     有探索域但零卡 → 0（探索过、没发现问题）
  ② 识别：只有 `source` 指向探索（S-NNN / 探索#…）的卡才算探索修复卡，普通卡不误报
  ③ 必填：anchor/entry 不得同时为空；status 必须落在现有五状态
  ④ 评估：done 的探索卡必须 disposition ∈ {card,tc,archive}；card/tc 的 ref 必须真实存在；
     archive 必须写 note（≥6 字）
  ⑤ 指针：source 里的 S-NNN 必须在地图里存在（悬空 = ERROR）
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


def load_module():
    spec = importlib.util.spec_from_file_location(
        "explore_loop_audit", str(HERE.parent / "explore_loop_audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = load_module()

S_OPEN, S_CLOSE = "<!-- machine:scope -->", "<!-- /machine:scope -->"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eloop_")
        self.root = Path(self.tmp)
        (self.root / "project-state" / "契约").mkdir(parents=True)
        (self.root / "project-state" / "需求卡").mkdir(parents=True)
        (self.root / "project-state" / "测试卡").mkdir(parents=True)
        self.write_scope(["S-001", "S-002"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def write_scope(self, ids):
        rows = "\n".join(
            f"| {i} | 负面 | F4 | 审批单详情 | 刚被降权 | 界面 | 详情 | 契约/PRD.md#REQ-1 | P0 | 截图 | 已探 |".replace(
                " | 已探 |", " | 已探 |")
            for i in ids)
        # 注意：探索域真实列头是 11 列（id|family|kind|target|identity|channel|face|criterion|risk|evidence|status）
        rows = "\n".join(
            f"| {i} | 负面 | F4 | 审批单详情 | 刚被降权 | 界面 | 详情 | 契约/PRD.md#REQ-1 | P0 | 截图 | 已探 |"
            for i in ids)
        self.write("project-state/契约/探索域.md",
                   f"# 探索域\n\n{S_OPEN}\n| id | family | kind | target | identity | channel | face | criterion | risk | evidence | status |\n"
                   f"| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n{rows}\n{S_CLOSE}\n")

    def card(self, cid="card-001", source="探索#S-001", status="todo",
             anchor="repos/app/web/app.js:983", entry="搜索框输入 <img>",
             disposition="", disposition_ref="", disposition_note=""):
        fm = (
            "---\n"
            f"id: {cid}\n"
            "title: 修复搜索框 XSS\n"
            'req: ""\n'
            f"source: {source}\n"
            f"anchor: {anchor}\n"
            f"entry: {entry}\n"
            f"status: {status}\n"
            f"disposition: {disposition}\n"
            f"disposition_ref: {disposition_ref}\n"
            f"disposition_note: {disposition_note}\n"
            "---\n\n# 卡\n"
        )
        self.write(f"project-state/需求卡/{cid}/card.md", fm)
        return cid

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = MOD.main(["explore_loop_audit.py", str(self.root), *args])
        return rc, out.getvalue()


class States(Base):
    def test_no_explore_anywhere_is_2(self):
        shutil.rmtree(self.root / "project-state" / "契约", ignore_errors=True)
        rc, out = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("无法检查", out)

    def test_scope_but_no_cards_is_0(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("探索修复卡 0", out)

    def test_plain_requirement_card_not_flagged(self):
        self.card(cid="card-002", source="", status="done")   # 普通卡
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("探索修复卡 0", out)


class Chain(Base):
    def test_todo_card_ok(self):
        self.card(status="todo")
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)

    def test_anchor_entry_both_empty(self):
        self.card(status="todo", anchor="-", entry="-")
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("EL-ANCHOR-MISSING", out)

    def test_anchor_only_is_ok(self):
        self.card(status="todo", anchor="repos/x.py:10", entry="-")
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)

    def test_bad_status(self):
        self.card(status=" архивировано")
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("EL-STATUS", out)

    def test_done_without_disposition(self):
        self.card(status="done")
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("EL-DISPOSITION-MISSING", out)

    def test_done_disposition_card_missing_target(self):
        self.card(status="done", disposition="card", disposition_ref="card-009")
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("EL-DISPOSITION-SINK", out)

    def test_done_disposition_card_ok(self):
        (self.root / "project-state" / "需求卡" / "card-009").mkdir(parents=True)
        self.card(status="done", disposition="card", disposition_ref="card-009")
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("沉需求卡 1", out)

    def test_done_disposition_tc_ok(self):
        (self.root / "project-state" / "测试卡" / "tc-003").mkdir(parents=True)
        self.card(status="done", disposition="tc", disposition_ref="tc-003")
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)

    def test_disposition_bad_value(self):
        self.card(status="done", disposition="需求卡", disposition_ref="card-009")
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("EL-DISPOSITION-VALUE", out)

    def test_archive_requires_note(self):
        self.card(status="done", disposition="archive", disposition_note="短")
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("EL-DISPOSITION-NOTE", out)

    def test_archive_with_note_ok(self):
        self.card(status="done", disposition="archive",
                  disposition_note="一次性配置错误，已由部署脚本覆盖，无需回归")
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("只归档 1", out)

    def test_probe_dangling(self):
        self.card(source="探索#S-999", status="todo")
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("EL-PROBE-DANGLING", out)

    def test_json_payload(self):
        self.card(status="done", disposition="archive",
                  disposition_note="一次性问题，无需沉淀")
        rc, out = self.run_cli("--json")
        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(data["explore_cards"], 1)
        self.assertEqual(data["disposed"], 1)
        self.assertEqual(data["pending_eval"], 0)
        self.assertTrue(data["coverage"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
