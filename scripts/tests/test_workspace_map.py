#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_workspace_map.py — workspace_map 回归（v0.1 演示版）

钉住的承诺（改坏即红）：
  1. registered_missing / unregistered_contract 双向漂移都能抓（H2 的机械证明）
  2. loose_file：根级游离备份 = ERROR（agent.md 根目录零游离文件）
  3. 【运行】登记缺失不算漂移（存在性图例 v4.5.0 的语义）
  4. 语义聚合四来源：上下文索引 / 方案清单 / frontmatter(card.md) / 根白名单
  5. --check 退出码三态：0 干净 / 1 有 ERROR / 2 无法检查
  6. 目录登记（名尾带 /）按目录存在性对账，不误报
  7. .mimosa 工具状态目录整体跳过，不进 files/unknown/tree（v5.18.0）
  8. 索引「目录用途」段给项目自定域挂用途，子目录继承最近祖先（v5.18.0）
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace_map.py")

INDEX_MD = """# 上下文索引

## 契约/（先确认后开发）

| 文档 | L0 摘要 | 建议深度 |
|------|-----------------|---------|
| 产品需求文档.md | **需求唯一事实源**：目标/范围 | L2 |
| 缺失文档.md | 登记了但磁盘没有 | L1 |
| 用例库模板.md | 用例库骨架（7 列机读块 id\\|casename\\|steps） | L1 |
| UI资源/directions/*.html【运行】 | 视觉方向稿 | L2 |
| 架构图.json（v3.15.0） | 可验证架构契约 | L2 |

## 问题/

| 文档 | L0 摘要 | 建议深度 |
|------|-----------------|---------|
| 问题/审核记录.md | 他律穿透审核记录 | L1 |
| 问题/变更申请单/ | 破坏性变更申请单 | L2 |

## 目录用途（项目侧域级登记）

| 目录 | 用途 | 什么时候看 |
|---|---|---|
| `素材自定义/` | 客户给的素材（项目自定域） | 按需 |
"""


def run(args):
    return subprocess.run([sys.executable, "-X", "utf8", SCRIPT] + args,
                          capture_output=True, text=True, encoding="utf-8")


class TestWorkspaceMap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="wmap_test_")
        root = pathlib.Path(cls.root)
        ps = root / "project-state"
        # 契约域：登记的 + 未登记的
        (ps / "契约").mkdir(parents=True)
        (ps / "契约" / "产品需求文档.md").write_text("# PRD", encoding="utf-8")
        (ps / "契约" / "架构图.json").write_text('{"components": []}', encoding="utf-8")
        (ps / "契约" / "用例库模板.md").write_text("# 用例库模板", encoding="utf-8")
        (ps / "契约" / "未登记新契约.md").write_text("# new", encoding="utf-8")
        (ps / "契约" / "上下文索引.md").write_text(INDEX_MD, encoding="utf-8")
        # 问题域：目录登记（存在）+ 文件登记（存在）
        (ps / "问题" / "变更申请单").mkdir(parents=True)
        (ps / "问题" / "审核记录.md").write_text("| 审核 |", encoding="utf-8")
        # 卡卷宗 frontmatter 语义
        (ps / "需求卡" / "card-001").mkdir(parents=True)
        (ps / "需求卡" / "card-001" / "card.md").write_text(
            "---\ntitle: 登录失效修复\nstatus: todo\n---\n# card-001\n", encoding="utf-8")
        # 方案区 + 清单语义
        (root / "方案" / "原始资料").mkdir(parents=True)
        (root / "方案" / "原始资料" / "客户方案v1.pdf").write_bytes(b"%PDF-1.4")
        (root / "方案" / "原始资料" / "清单.md").write_text(
            "| 文件名 | 收到时间 | 来源 | 类型 |\n|---|---|---|---|\n"
            "| 客户方案v1.pdf | 2026-10-01 | 客户A | pdf |\n", encoding="utf-8")
        (root / "agent.md").write_text("# 主控", encoding="utf-8")
        # 根级游离备份（硬纪律违规）+ 框架侧目录备份（降级 warn）
        (root / "agent.md.bak-20261001").write_text("# old", encoding="utf-8")
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "tool.py.bak-20261001").write_text("# old", encoding="utf-8")
        cls.out = str(root / "tmp_out")

        # v5.18.0：工具状态目录（应整体跳过）+ 项目自定域（目录用途段登记）
        mimosa_state = pathlib.Path(cls.root, ".mimosa", "hook-state")
        mimosa_state.mkdir(parents=True)
        (mimosa_state / "x.source").write_text("state", encoding="utf-8")
        custom = pathlib.Path(cls.root, "素材自定义", "子目录")
        custom.mkdir(parents=True)
        (custom / "a.txt").write_text("asset", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def map_data(self):
        r = run([self.root, "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_a_bidirectional_drift(self):
        """双向漂移：登记缺失 + 契约域未登记，都要抓到。"""
        d = self.map_data()
        types = {(x["type"], x["path"], x["severity"]) for x in d["drift"]}
        self.assertIn(("registered_missing", "project-state/契约/缺失文档.md", "error"), types)
        self.assertIn(("unregistered_contract", "project-state/契约/未登记新契约.md", "error"), types)
        self.assertIn(("loose_file", "agent.md.bak-20261001", "error"), types)
        # 框架侧目录的备份件降级 warn（隔离验收点名：不可见 = 漏报）
        self.assertIn(("loose_file", "scripts/tool.py.bak-20261001", "warn"), types)
        # 修好即绿的反面：登记且存在的文档不算漂移
        self.assertNotIn(("registered_missing", "project-state/契约/产品需求文档.md", "error"), types)

    def test_g_escaped_pipe_l0(self):
        """表格内转义竖线 \\| 不劈断单元格：l0 完整且无尾随反斜杠。"""
        d = self.map_data()
        by = {f["rel"]: f for f in d["files"]}
        f = by["project-state/契约/用例库模板.md"]
        self.assertEqual(f["l0_source"], "上下文索引")
        self.assertIn("7 列机读块", f["l0"])
        self.assertIn("|", f["l0"], "转义竖线应还原为字面竖线：l0=%r" % f["l0"])
        self.assertFalse(f["l0"].endswith("\\"), f["l0"])

    def test_b_runtime_legend_respected(self):
        """【运行】登记磁盘缺失 ≠ 漂移（存在性图例）。"""
        d = self.map_data()
        paths = [x["path"] for x in d["drift"]]
        self.assertFalse(any("directions" in p for p in paths), paths)

    def test_c_dir_registration_no_false_positive(self):
        """目录登记（问题/变更申请单/ 存在）不误报。"""
        d = self.map_data()
        paths = [x["path"] for x in d["drift"]]
        self.assertFalse(any("变更申请单" in p for p in paths), paths)

    def test_d_semantic_sources(self):
        """四来源：索引 L0 / 方案清单 / card frontmatter / 根白名单。"""
        d = self.map_data()
        by = {f["rel"]: f for f in d["files"]}
        self.assertEqual(by["project-state/契约/产品需求文档.md"]["l0_source"], "上下文索引")
        self.assertIn("需求唯一事实源", by["project-state/契约/产品需求文档.md"]["l0"])
        self.assertEqual(by["方案/原始资料/客户方案v1.pdf"]["l0_source"], "方案清单")
        self.assertEqual(by["project-state/需求卡/card-001/card.md"]["kind"], "卡卷宗")
        self.assertIn("登录失效修复", by["project-state/需求卡/card-001/card.md"]["l0"])
        self.assertEqual(by["agent.md"]["l0_source"], "根白名单")
        # 架构图.json（v3.15.0）版本尾注剥掉后正确命中
        self.assertEqual(by.get("project-state/契约/架构图.json", {}).get("l0_source"), "上下文索引")

    def test_e_check_exit_codes(self):
        """--check 三态：有问题=1；非项目根=2。"""
        self.assertEqual(run([self.root, "--check"]).returncode, 1)
        self.assertEqual(run([os.path.join(self.root, "方案"), "--check"]).returncode, 2)

    def test_f_write_artifacts(self):
        """--write 落盘 JSON+人读版，且目录地图含方案/原始资料行（H3 的机械面）。"""
        run([self.root, "--write", "--out", self.out])
        j = os.path.join(self.out, "工作区地图.json")
        md = [f for f in os.listdir(self.out) if f.endswith(".md") and f.startswith("工作区地图")]
        self.assertTrue(os.path.isfile(j) and md, os.listdir(self.out or "."))
        d = json.loads(pathlib.Path(j).read_text(encoding="utf-8"))
        self.assertIn("方案/原始资料", [t["path"] for t in d["tree"]])
        text = pathlib.Path(self.out, md[0]).read_text(encoding="utf-8")
        self.assertIn("不许手改", text)
        self.assertIn("原始资料", text)


    def test_h_mimosa_skipped(self):
        """v5.18.0：.mimosa 工具状态目录整体跳过（不进 files/unknown/tree）。"""
        d = self.map_data()
        rels = [f["rel"] for f in d["files"]]
        self.assertFalse(any(r.startswith(".mimosa/") for r in rels))
        self.assertFalse(any(t["path"].startswith(".mimosa") for t in d["tree"]))
        self.assertFalse(any(u.startswith(".mimosa/") for u in d["unknown"]))

    def test_i_dir_purpose_section(self):
        """v5.18.0：索引「目录用途」段给项目自定域挂用途；子目录继承最近祖先。"""
        d = self.map_data()
        by = {t["path"]: t for t in d["tree"]}
        self.assertEqual(by.get("素材自定义", {}).get("purpose"), "客户给的素材（项目自定域）")
        self.assertEqual(by.get("素材自定义/子目录", {}).get("purpose"), "客户给的素材（项目自定域）")

if __name__ == "__main__":
    unittest.main(verbosity=2)
