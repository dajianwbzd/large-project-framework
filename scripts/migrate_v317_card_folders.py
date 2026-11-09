#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.17.0 迁移：把老工作区的「平铺卡」转成「卡卷宗（一卡一目录）」。

对应框架变更：需求卡/card-NNN.md → 需求卡/card-NNN/card.md
              思路/<角色>/card-NNN.md → 需求卡/card-NNN/思路-<角色>.md
              测试卡/tc-NNN.md → 测试卡/tc-NNN/tc.md
              （可选 --with-evidence）代码/<角色>/card-NNN/ → 需求卡/card-NNN/证据/<角色>/

设计要点（v3.14.0 路径纪律 + 结构变更纪律）：
- **默认 dry-run**，只报告将做什么，不动任何文件；
- `--apply` 前先把待迁移文件整体备份到 `<项目根>/tmp/v317_migrate_backup_<日期>/`（tmp 是唯一临时区，核对无误后可删）；
- **只移动不改写内容**；目标已存在则跳过并报告（绝不覆盖）；
- 迁移后写 `project-state/迁移记录-v317.md`（old→new 全表），便于人工回滚与复核；
- 非 git 项目同样安全：备份 + 映射表 = 可逆。

用法：
    python migrate_v317_card_folders.py <项目根路径>                     # dry-run
    python migrate_v317_card_folders.py <项目根路径> --apply             # 执行（含备份）
    python migrate_v317_card_folders.py <项目根路径> --apply --with-evidence
    python migrate_v317_card_folders.py <项目根路径> --apply --no-backup  # 跳过备份（不推荐）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import shutil
import sys
from pathlib import Path

CARD_RE = re.compile(r"^card-(\d+)\.md$", re.I)
TC_RE = re.compile(r"^tc-(\d+)\.md$", re.I)


class Plan:
    """一条迁移动作（源 → 目标）。"""

    def __init__(self, src: Path, dst: Path, kind: str) -> None:
        self.src, self.dst, self.kind = src, dst, kind

    def __str__(self) -> str:
        try:
            return f"  [{self.kind}] {self.src.relative_to(_ROOT)}  →  {self.dst.relative_to(_ROOT)}"
        except Exception:
            return f"  [{self.kind}] {self.src}  →  {self.dst}"


_ROOT: Path = Path(".")


def build_plan(root: Path, with_evidence: bool) -> tuple[list[Plan], list[str]]:
    ps = root / "project-state"
    plans: list[Plan] = []
    notes: list[str] = []

    # 1) 需求卡：需求卡/card-NNN.md → 需求卡/card-NNN/card.md
    for f in sorted((ps / "需求卡").glob("card-*.md")) if (ps / "需求卡").is_dir() else []:
        if CARD_RE.match(f.name):
            stem = f.stem  # card-001
            plans.append(Plan(f, ps / "需求卡" / stem / "card.md", "需求卡→卡卷宗"))

    # 2) 测试卡：测试卡/tc-NNN.md → 测试卡/tc-NNN/tc.md
    for f in sorted((ps / "测试卡").glob("tc-*.md")) if (ps / "测试卡").is_dir() else []:
        if TC_RE.match(f.name):
            stem = f.stem  # tc-001
            plans.append(Plan(f, ps / "测试卡" / stem / "tc.md", "测试卡→卡卷宗"))

    # 3) 思路文档：思路/<角色>/card-NNN.md → 卡卷宗内 思路-<角色>.md
    idea_dir = ps / "思路"
    if idea_dir.is_dir():
        for role_dir in sorted(p for p in idea_dir.iterdir() if p.is_dir()):
            role = role_dir.name
            for f in sorted(role_dir.glob("card-*.md")):
                m = CARD_RE.match(f.name)
                if not m:
                    continue
                stem = f.stem
                plans.append(Plan(f, ps / "需求卡" / stem / f"思路-{role}.md", "思路→卡卷宗"))
            for f in sorted(role_dir.glob("tc-*.md")):
                m = TC_RE.match(f.name)
                if not m:
                    continue
                stem = f.stem
                plans.append(Plan(f, ps / "测试卡" / stem / f"思路-{role}.md", "思路→测试卡卷宗"))
            # 通用-/盘点-<主题> 属跨卡内容，按 v3.17.0 留在 思路/<角色>/，不动
    # 4) 可选：按卡证据目录 代码/<角色>/card-NNN/ → 卡卷宗/证据/<角色>/
    if with_evidence:
        code_dir = ps / "代码"
        if code_dir.is_dir():
            for role_dir in sorted(p for p in code_dir.iterdir() if p.is_dir()):
                role = role_dir.name
                for d in sorted(p for p in role_dir.iterdir() if p.is_dir()):
                    if re.match(r"^(card|tc)-\d+$", d.name, re.I):
                        stem = d.name
                        base = "需求卡" if stem.lower().startswith("card-") else "测试卡"
                        plans.append(Plan(d, ps / base / stem / "证据" / role, "证据→卡卷宗"))
        notes.append("--with-evidence：证据目录按 角色 分子目录并入卡卷宗，避免同名冲突（大文件建议核对后转 产物/）")

    return plans, notes


def main() -> int:
    ap = argparse.ArgumentParser(description="v3.17.0 卡卷宗迁移（默认 dry-run）")
    ap.add_argument("project_root", help="项目根目录（含 project-state/）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    ap.add_argument("--with-evidence", action="store_true", help="同时把 代码/<角色>/card-NNN/ 证据目录并入卡卷宗")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    args = ap.parse_args()

    global _ROOT
    root = Path(args.project_root).expanduser().resolve()
    _ROOT = root
    ps = root / "project-state"
    if not ps.is_dir():
        print(f"[错误] 找不到 {ps}（请传入项目根目录，而非 project-state 本身）")
        return 2

    plans, notes = build_plan(root, args.with_evidence)
    print(f"工作区：{root}")
    print(f"计划迁移 {len(plans)} 项：")
    if not plans:
        print("  （无——该工作区可能已是卡卷宗结构）")
    conflicts = [p for p in plans if p.dst.exists()]
    for p in plans:
        mark = "  ⚠ 目标已存在，将跳过" if p.dst.exists() else ""
        print(f"{p}{mark}")
    for n in notes:
        print(f"  注：{n}")

    if not args.apply:
        print("\n[dry-run] 未改动任何文件。加 --apply 执行（会先备份）。")
        return 0

    if not plans:
        print("\n无需迁移：该工作区已是卡卷宗结构（未改动任何文件）。")
        return 0

    # ---- 备份 ----
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = root / "tmp" / f"v317_migrate_backup_{stamp}"
    if not args.no_backup:
        for p in plans:
            if p.src.exists():
                rel = p.src.relative_to(root)
                dst = backup_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if p.src.is_dir():
                    shutil.copytree(p.src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(p.src, dst)
        print(f"\n[备份] {backup_root.relative_to(root)}（核对无误后可删）")

    # ---- 执行 ----
    done, skipped, failed = [], [], []
    for p in plans:
        if p.dst.exists():
            skipped.append(p)
            continue
        try:
            p.dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p.src), str(p.dst))
            done.append(p)
        except Exception as e:  # noqa: BLE001
            failed.append((p, str(e)))
            print(f"[失败] {p.src} → {p.dst}：{e}")

    # ---- 清空空目录（只清理 思路/<角色>/ 下已空的目录，不递归删卡目录）----
    idea_dir = ps / "思路"
    if idea_dir.is_dir():
        for role_dir in sorted(ps_dir for ps_dir in idea_dir.iterdir() if ps_dir.is_dir()):
            try:
                if not any(role_dir.iterdir()):
                    role_dir.rmdir()
                    print(f"[清空目录] {role_dir.relative_to(root)}")
            except OSError:
                pass

    # ---- 迁移记录（已存在则追加，绝不覆盖历史记录）----
    rec = ps / "迁移记录-v317.md"
    stamp_str = f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S}"
    head = ([] if rec.exists() else [
        "# v3.17.0 卡卷宗迁移记录",
        "",
        "> 本文件由 `scripts/migrate_v317_card_folders.py` 写入：记录「平铺卡 → 卡卷宗」的 old→new 映射，供人工复核与回滚。",
        "> 每一节 = 一次迁移执行（追加，不覆盖历史）。",
        "",
    ])
    lines = [
        f"## 迁移执行 {stamp_str}",
        "",
        f"> 工作区：`{root}`　｜　"
        + (f"备份：`{backup_root.relative_to(root)}`" if not args.no_backup else "备份：**已跳过（--no-backup）**"),
        f"> 迁移 {len(done)} 项，跳过 {len(skipped)} 项（目标已存在），失败 {len(failed)} 项。",
        "",
        "| 类型 | 原路径 | 新路径 | 结果 |",
        "|---|---|---|---|",
    ]
    for p in plans:
        res = "已迁移" if p in done else ("跳过（已存在）" if p in skipped else "失败")
        lines.append(
            f"| {p.kind} | `{p.src.relative_to(root)}` | `{p.dst.relative_to(root)}` | {res} |"
        )
    lines += [
        "",
        "### 迁移后复核清单（人工/主控逐条核）",
        "",
        "- [ ] 卡本体就位：`需求卡/*/card.md`、`测试卡/*/tc.md` 数目与迁移前卡数一致",
        "- [ ] 思路文档就位：每张卡的参与角色都有 `思路-<角色>.md`（`思路/<角色>/通用-*`/`盘点-*` 属跨卡内容，**不应**被搬动）",
        "- [ ] 无残留平铺文件：`需求卡/` 与 `测试卡/` 根目录下不再有 `card-NNN.md` / `tc-NNN.md`",
        "- [ ] 引用修正：各卡 frontmatter / 进度 json / 开发说明 / 审核记录里指向旧路径的引用已由主控校正（agent.md「输出存放由主控统一管理」）",
        "- [ ] 路径合规：工作区无新增游离文件（v3.14.0），`tmp/` 备份在确认无误后清理",
        "",
    ]
    if not failed:
        mode = "a" if rec.exists() else "w"
        with rec.open(mode, encoding="utf-8") as fh:
            if mode == "a":
                fh.write("\n---\n\n")
            fh.write("\n".join(head + lines))
        print(f"\n[记录] {rec.relative_to(root)}（含 old→new 映射表与复核清单）")

    print(f"\n完成：迁移 {len(done)}｜跳过 {len(skipped)}｜失败 {len(failed)}")
    if failed:
        print("有失败项，请按上面报错处理后重跑（已迁移项不会重复处理）。")
        return 1
    print("下一步：按《迁移记录》复核清单逐条核，再让各角色的旧路径引用由主控统一校正。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
