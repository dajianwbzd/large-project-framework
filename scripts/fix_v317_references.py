#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.17.0 迁移配套：批量校正工作区里的旧路径引用（卡卷宗迁移的收尾步骤）。

`migrate_v317_card_folders.py` 只搬文件、不改内容；本脚本负责把**引用**改对：

| 旧路径 | 新路径 |
|---|---|
| `需求卡/card-NNN.md` | `需求卡/card-NNN/card.md` |
| `思路/<角色>/card-NNN.md` | `需求卡/card-NNN/思路-<角色>.md` |
| `代码/<角色>/card-NNN/…` | `需求卡/card-NNN/证据/<角色>/…` |
| `测试卡/tc-NNN.md` | `测试卡/tc-NNN/tc.md` |

两类引用分开处理：
1. **文档类**（`.md` / `.json`）——按上表改写；跳过 `迁移记录-v317.md`（它本身就是 old→new 映射表）与 `tmp/`。
2. **脚本类**（`.js` / `.py` / `.sh`）——除字面量路径外，还要处理 `Path(__file__).resolve().parents[N]`：
   脚本被搬动后层级变了，`N` 必须同步改，否则"跑得通但算错目录"。做法是按《迁移记录-v317.md》反推脚本原位置，
   求出解析结果等价的新 `N`；若旧值本就失配（常见：脚本原在 `tmp/card0NN/` 下写的层级，被复制进证据目录后没改），
   则按脚本真实意图（**含 `project-state/` 的项目根**）修正，并明确报告。
3. **历史记录类**（`.txt`）——**不改**：它们是当时的原始输出记录，按《迁移记录-v317.md》的映射表解读即可。

用法：
    python fix_v317_references.py <项目根路径>            # dry-run（默认）
    python fix_v317_references.py <项目根路径> --apply    # 执行（先备份被改文件到 tmp/）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import shutil
import sys
from pathlib import Path

RULES: list[tuple[re.Pattern[str], str]] = [
    # 代码/<角色>/card-NNN → 卡卷宗证据（先做，避免与下面规则交叉）
    (re.compile(r"代码/([^/\s`，、）)]+)/card-(\d{3})"), r"需求卡/card-\2/证据/\1"),
    # 代码/测试/件 → card-001 证据（该项目特有的历史遗留目录名，可按需删除本行）
    (re.compile(r"代码/测试/件"), r"需求卡/card-001/证据/测试/件"),
    # 思路/<角色>/card-NNN.md → 卡卷宗内思路
    (re.compile(r"思路/([^/\s`]+)/card-(\d{3})\.md"), r"需求卡/card-\2/思路-\1.md"),
    # 需求卡/card-NNN.md → 卡本体
    (re.compile(r"需求卡/card-(\d{3})\.md"), r"需求卡/card-\1/card.md"),
    # 测试卡/tc-NNN.md → 测试卡本体
    (re.compile(r"测试卡/tc-(\d{3})\.md"), r"测试卡/tc-\1/tc.md"),
]
PARENTS = re.compile(r"parents\[(\d+)\]")
DOC_EXT = {".md", ".json"}
SCRIPT_EXT = {".js", ".py", ".sh"}
SKIP_NAMES = {"迁移记录-v317.md"}


def load_mapping(root: Path) -> list[tuple[Path, Path]]:
    """从《迁移记录-v317.md》解析「dir 级 old→new」映射（供脚本 parents[N] 反推用）。"""
    rec = root / "project-state" / "迁移记录-v317.md"
    out: list[tuple[Path, Path]] = []
    if not rec.is_file():
        return out
    for line in rec.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        old, new = cells[1].replace("\\", "/"), cells[2].replace("\\", "/")
        if old.startswith("project-state") and new.startswith("project-state"):
            out.append((root / old, root / new))
    return out


def sub_keep_mappings(pattern: re.Pattern[str], rep: str, s: str) -> tuple[str, int]:
    """按规则改写，但**跳过"旧路径 → 新路径"这种映射写法里的左侧**——
    迁移记录 / 变更日志里的 old→new 说明是权威描述，改写会让它自相矛盾。"""
    out, last, hits = [], 0, 0
    for m in pattern.finditer(s):
        tail = s[m.end():m.end() + 8].lstrip("/` ").lstrip()     # 允许尾随的 `/` 与反引号/空格
        if tail.startswith("→"):
            continue
        out.append(s[last:m.start()])
        out.append(m.expand(rep))
        last = m.end()
        hits += 1
    out.append(s[last:])
    return "".join(out), hits


def main() -> int:
    ap = argparse.ArgumentParser(description="v3.17.0 旧路径引用批量校正（默认 dry-run）")
    ap.add_argument("project_root")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    ps = root / "project-state"
    if not ps.is_dir():
        print(f"[错误] 找不到 {ps}（请传入项目根目录）")
        return 2
    mapping = load_mapping(root)
    print(f"工作区：{root}\n目录级映射：{len(mapping)} 条")

    targets = [f for f in ps.rglob("*") if f.is_file() and f.suffix in DOC_EXT | SCRIPT_EXT]
    targets += [f for f in root.glob("*.md") if f.is_file()]

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = root / "tmp" / f"v317_refs_backup_{stamp}"
    changed = blobs = 0
    notes: list[str] = []

    for f in sorted(targets):
        if f.name in SKIP_NAMES or "tmp" in f.relative_to(root).parts:
            continue
        try:
            s = orig = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for pat, rep in RULES:
            s, _ = sub_keep_mappings(pat, rep, s)

        if f.suffix in SCRIPT_EXT:
            old_file = next((od / f.relative_to(nd) for od, nd in mapping if nd in f.parents), None)
            if old_file is not None:
                p = f.resolve()
                for n_old in sorted({int(x) for x in PARENTS.findall(s)}, reverse=True):
                    try:
                        target = old_file.resolve().parents[n_old]
                    except IndexError:
                        target = None
                    # 幂等守卫：当前层级若已满足意图（解析到项目根，或已等于迁移前目标），不动
                    cur = p.parents[n_old] if n_old < len(p.parents) else None
                    if cur is not None and ((cur / "project-state").is_dir() or (target is not None and cur == target)):
                        continue
                    n_new = next((c for c in range(12)
                                  if c < len(p.parents) and target is not None and p.parents[c] == target), None)
                    if n_new is None:
                        n_new = next((c for c in range(12)
                                      if c < len(p.parents) and (p.parents[c] / "project-state").is_dir()), None)
                        if n_new is None:
                            notes.append(f"⚠ {f.relative_to(root)}: parents[{n_old}] 无法确定项目根层级，未改")
                            continue
                        notes.append(f"ℹ {f.relative_to(root)}: parents[{n_old}] 旧值本就失配 → 按项目根修正为 parents[{n_new}]")
                    if n_new != n_old:
                        s = s.replace(f"parents[{n_old}]", f"parents[{n_new}]")

        if s != orig:
            changed += 1
            blobs += sum(1 for a, b in zip(orig.splitlines(), s.splitlines()) if a != b)
            print(f"[改] {f.relative_to(root)}")
            if args.apply:
                if not args.no_backup:
                    dst = backup / f.relative_to(root)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)
                f.write_text(s, encoding="utf-8")

    print(f"\n{'已写入' if args.apply else 'dry-run'}：{changed} 个文件")
    if args.apply and changed and not args.no_backup:
        print(f"[备份] {backup.relative_to(root)}（核对无误后可删）")
    for n in notes:
        print("  " + n)
    if not args.apply and changed:
        print("\n提示：.txt 历史记录不改（按《迁移记录-v317.md》映射解读）；确认后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
