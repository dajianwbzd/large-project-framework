#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.18.0 路径盘点（path_audit）：扫描项目根，报告游离条目 / tmp 残留 / 大文件 / 各域体积。

设计原则：
- **只报告，不改动**——归位属于结构变更，由主控裁决后执行（agent.md「统一路径管理」）。
- 判据 = `config.json` 的 `paths`（域注册表）+ `根文件`（根级白名单）；未登记即游离。
- **tmp/ 只提示不清理**：tmp 里可能躺着在飞任务的分片/备份（真实案例：待用户扫码期间的部署分片），
  清单里明确标注「确认无在飞任务后再清」，避免误删。

用法：
    python path_audit.py <项目根>                 # 人类可读报告
    python path_audit.py <项目根> --json          # 机器可读
    python path_audit.py <项目根> --write         # 同时写 project-state/路径盘点-<日期>.md
    python path_audit.py <项目根> --max-mb 20     # 大文件阈值（默认 20MB）
    python path_audit.py <项目根> --tmp-age-days 3  # tmp 超龄阈值天数（默认 3.0，直接决定 tmp 是否判为超龄）
    python path_audit.py <项目根> --no-size       # 跳过体积统计（慢盘兜底，见下）

**为什么会有 `--no-size`（2026-09-16 实测）**：体积统计 `du()` 对每个根条目做 `rglob("*")` + 逐文件 `stat()`，
在逐文件 stat 约 0.18s 的文件系统上（本项目实测：全树约 5000 个文件，纯计数就花 908.7s），
`du()` 会让整个盘点**分钟级挂起且中途无任何输出**，闸门跑不完。
`--no-size` 只跳「体积 / 大文件」两项，**游离条目、应归位、域白名单、tmp 残留的判据一字不动**——
即：闸门要判的东西全在，只是不再顺带称重。**默认不开**（默认行为与判别结果不变）。

退出码：0 正常 / 2 不是框架工作区（<项目根> 下没有 project-state/）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

FRAMEWORK_ROOT_ENTRIES = {"agent.md", "skills", "scripts", "project-state", "tmp"}
JUNK_PATTERNS = ("__pycache__", ".DS_Store", "Thumbs.db", "~$")
JUNK_SUFFIX = (".pyc", ".pyo", ".log", ".tmp", ".bak", ".out")

# 慢盘兜底：--no-size 时置 True，du() 直接返回哨兵 -1，human() 把它显示成「未统计」。
# 判据（游离 / 应归位 / tmp 残留）不依赖体积，故闸门语义不受影响。
SKIP_SIZE = False
SIZE_UNKNOWN = -1


def du(path: Path) -> int:
    if SKIP_SIZE:
        return SIZE_UNKNOWN
    if path.is_file():
        return path.stat().st_size
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def human(n: int) -> str:
    if n < 0:
        return "未统计"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def _path_of(val: str) -> str:
    """从登记值里取出路径：支持 `路径  # 说明` 与 `路径 —— 说明` 两种写法（说明不参与解析）。"""
    v = val.strip()
    if "#" in v:
        v = v.split("#")[0]
    parts = v.split()
    v = parts[0] if parts else ""
    return v.strip("`\"'/")


def load_registry(root: Path) -> tuple[set[str], dict[str, str], dict, dict[str, str]]:
    """返回（已登记的根级名字集合, 名字→来源说明, 原始 config）。
    兼容两种 schema：`根文件` 写在顶层，或写在 `paths` 内（v3.14.0 旧写法，真实项目在用）。"""
    cfg_path = root / "project-state" / "config.json"
    cfg = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = {}
    names: set[str] = set(FRAMEWORK_ROOT_ENTRIES)
    why: dict[str, str] = {n: "框架文件（按根解析相对路径）" for n in FRAMEWORK_ROOT_ENTRIES}
    key_hints: dict[str, str] = {}

    paths = cfg.get("paths") or {}
    root_files: dict = {}
    if isinstance(cfg.get("根文件"), dict):
        root_files.update(cfg["根文件"])
    if isinstance(paths.get("根文件"), dict):
        root_files.update(paths["根文件"])

    for key, val in paths.items():
        if key.startswith("_") or key == "根文件":
            continue
        for v in (val if isinstance(val, list) else [val]):
            if not isinstance(v, str):
                continue
            head = _path_of(v).split("/")[0]
            if head:
                names.add(head)
                why.setdefault(head, f"paths.{key} 登记域")
    for key, val in root_files.items():
        if key.startswith("_") or not isinstance(val, str):
            continue
        head = _path_of(val).split("/")[0]
        if head:
            names.add(head)
            why.setdefault(head, f"根文件.{key}（白名单）")
            key_hints[head] = key
    return names, why, cfg, key_hints


# 「登记了也不该留根」的类别判据（v3.18.0：交付件/文档/脚本/日志各有其域）
MISPLACED = [
    ((".zip", ".7z", ".rar", ".tar", ".gz", ".tgz"), "对外交付件 → `交付/`（内部构建包 → `产物/`）"),
    ((".md", ".docx", ".pdf", ".xlsx", ".pptx"), "项目文档 → `project-state/契约/`；对外文档 → `交付/`"),
    ((".py", ".ps1", ".js", ".sh", ".bat"), "工具脚本 → `scripts/`；一次性脚本 → `tmp/`"),
    ((".log", ".out", ".tmp", ".bak", ".dump", ".sqlite"), "临时/日志 → `tmp/`；长期证据 → 对应域"),
]
ROOT_FILE_ALLOW = {"agent.md", "readme.md", "license", "license.txt", ".gitignore", ".gitattributes"}
LAUNCHER_HINT = ("启动", "start", "run", "launch", "一键")
LAUNCHER_KEYS = {"entry", "入口", "启动器", "launcher"}


def misplacement(name: str, key_hint: str = "") -> str | None:
    """判断「已登记但按白名单不属于根级」的条目；**面向人的入口（启动器）豁免**——白名单第②类，必须在根。"""
    low = name.lower()
    if low in ROOT_FILE_ALLOW or low.startswith(("readme", "license", "changelog")):
        return None
    if key_hint in LAUNCHER_KEYS or any(h in name for h in LAUNCHER_HINT):
        return None
    for exts, advice in MISPLACED:
        if low.endswith(exts):
            return advice
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="路径盘点（只报告，不改动）")
    ap.add_argument("project_root")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true", help="同时写 project-state/路径盘点-<日期>.md")
    ap.add_argument("--max-mb", type=float, default=20.0)
    ap.add_argument("--tmp-age-days", type=float, default=3.0)
    ap.add_argument("--no-size", action="store_true",
                    help="跳过体积统计（慢盘兜底：只判游离/应归位/tmp 残留，不判体积与大文件）")
    args = ap.parse_args()

    global SKIP_SIZE
    SKIP_SIZE = args.no_size

    root = Path(args.project_root).expanduser().resolve()
    if not (root / "project-state").is_dir():
        print(f"[错误] {root} 下没有 project-state/（不像框架工作区）")
        return 2

    registered, why, cfg, root_key_hint = load_registry(root)
    now = _dt.datetime.now()
    stray, domains, junk = [], [], []
    misplaced_registered: list[dict] = []
    big: list[tuple[str, int]] = []

    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name in {".git", ".gitignore", ".gitattributes"}:
            continue
        size = du(entry)
        is_junk = any(p in entry.name for p in JUNK_PATTERNS) or entry.name.endswith(JUNK_SUFFIX)
        if entry.name in registered or entry.name in FRAMEWORK_ROOT_ENTRIES:
            domains.append({"name": entry.name, "kind": "dir" if entry.is_dir() else "file",
                            "size": size, "why": why.get(entry.name, "标准域"), "junk": is_junk})
            if entry.is_file():
                advice = misplacement(entry.name, root_key_hint.get(entry.name, ""))
                if advice:
                    misplaced_registered.append({"name": entry.name, "size": size, "advice": advice})
        else:
            stray.append({"name": entry.name, "kind": "dir" if entry.is_dir() else "file", "size": size})
        if size > args.max_mb * 1024 * 1024:
            big.append((entry.name, size))

    # tmp/ 内容（只提示）
    tmp = root / "tmp"
    tmp_items: list[dict] = []
    if tmp.is_dir():
        for item in sorted(tmp.iterdir(), key=lambda p: p.name):
            if item.name in {".gitkeep"}:
                continue
            try:
                age = (now - _dt.datetime.fromtimestamp(item.stat().st_mtime)).days
            except OSError:
                age = -1
            tmp_items.append({"name": item.name, "size": du(item), "age_days": age})

    # 游离文件里的"典型肇事者"模式提示
    suspects = [s for s in stray if s["name"].endswith((".py", ".sh", ".bat", ".log", ".txt", ".json", ".out"))]

    report = {
        "workspace": str(root),
        "generated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "stray": stray, "misplaced_registered": misplaced_registered, "domains": domains, "tmp": tmp_items,
        "big_files": [{"name": n, "size": s} for n, s in big],
        "junk_hits": [d["name"] for d in domains if d["junk"]] + [s["name"] for s in stray if any(p in s["name"] for p in JUNK_PATTERNS)],
        "profile": cfg.get("profile"), "mode": cfg.get("mode"),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    L = []
    L.append(f"# 路径盘点 · {root.name}")
    L.append("")
    L.append(f"> 时间：{report['generated']}　模式：{cfg.get('mode','?')}／档位：{cfg.get('profile','?')}")
    L.append("> 判据：`config.json` 的 `paths`（域注册表）+ `根文件`（根级白名单）。**只报告不改动**——归位由主控裁决。")
    if SKIP_SIZE:
        L.append("> ⚠ 体积未统计（`--no-size`）：本次只判「游离 / 应归位 / tmp 残留」，"
                 "**不判体积与大文件**（判据不依赖体积，故闸门结论等价）。")
    L.append("")
    L.append(f"## 一、游离条目（未登记，需处置）：{len(stray)} 项")
    if stray:
        L.append("")
        L.append("| 条目 | 类型 | 体积 | 建议处置 |")
        L.append("|---|---|---|---|")
        for s in stray:
            kind = "目录" if s["kind"] == "dir" else "文件"
            if s["name"].endswith((".js", ".py", ".sh", ".ps1")):
                advice = "脚本 → `tmp/`（一次性）或对应域；长期工具进 `scripts/`"
            elif s["name"].endswith((".zip", ".tar", ".gz", ".7z")):
                advice = "**对外交付件 → `交付/`**（并登记 `交付/发出记录.md`）；内部构建包 → `产物/`"
            elif s["name"].endswith((".md", ".docx", ".pdf", ".xlsx")):
                advice = "项目文档 → `project-state/契约/`；对外文档 → `交付/`"
            else:
                advice = "归位到对应域 / 移 `tmp/` / 登记 `待办/自然人.md` 请用户裁决"
            L.append(f"| `{s['name']}` | {kind} | {human(s['size'])} | {advice} |")
    else:
        L.append("")
        L.append("（无——根目录全部在白名单内 ✅）")
    if suspects:
        L.append("")
        L.append(f"> ⚠ 其中看起来像临时/中间产物的：{'、'.join('`'+s['name']+'`' for s in suspects)}")
    L.append("")
    L.append(f"## 二、已登记但应归位（**登记 ≠ 归位**，v3.18.0）：{len(misplaced_registered)} 项")
    if misplaced_registered:
        L.append("")
        L.append("| 条目 | 体积 | 应归位到 |")
        L.append("|---|---|---|")
        for m in misplaced_registered:
            L.append(f"| `{m['name']}` | {human(m['size'])} | {m['advice']} |")
        L.append("")
        L.append("> 这些条目**已在 config.json 登记**，但按 v3.18.0 白名单四类**不属于根级**（交付件/文档/脚本/日志各有其域）——"
                 "登记只回答「它在哪、为何在这」，**不构成留在根上的理由**。")
    else:
        L.append("")
        L.append("（无 ✅）")
    L.append("")
    L.append("## 三、白名单域（登记在册）")
    L.append("")
    L.append("| 条目 | 类型 | 体积 | 依据 |")
    L.append("|---|---|---|---|")
    for d in sorted(domains, key=lambda x: -x["size"]):
        L.append(f"| `{d['name']}` | {'目录' if d['kind']=='dir' else '文件'} | {human(d['size'])} | {d['why']} |")
    if report["junk_hits"]:
        L.append("")
        L.append(f"⚠ 疑似垃圾（`__pycache__`/缓存/编辑器临时文件）：{'、'.join('`'+j+'`' for j in report['junk_hits'])}")
    L.append("")
    L.append(f"## 四、tmp/ 残留（{len(tmp_items)} 项，**只提示不清理**）")
    L.append("")
    if tmp_items:
        L.append("| 条目 | 体积 | 最后修改（天前） |")
        L.append("|---|---|---|")
        for t in sorted(tmp_items, key=lambda x: -x["size"]):
            age = "?" if t["age_days"] < 0 else str(t["age_days"])
            flag = " ← 超过阈值，可评估清理" if t["age_days"] > args.tmp_age_days else ""
            L.append(f"| `{t['name']}` | {human(t['size'])} | {age}{flag} |")
        L.append("")
        L.append("> **清 tmp 前必须先确认无在飞任务**（部署分片、回滚备份、便携运行时可能仍在使用）；")
        L.append("> 正式证据不得只在 tmp（v3.14.0）；清扫时按「归位 / 转正到产物 / 删除」三类处置，删除前报用户。")
    else:
        L.append("")
        L.append("（空 ✅）")
    if big:
        L.append("")
        L.append(f"## 五、大文件（>{args.max_mb:g}MB）")
        L.append("")
        for n, s in sorted(big, key=lambda x: -x[1]):
            L.append(f"- `{n}` — {human(s)}")
    L.append("")
    L.append("## 处置三件套（主控执行）")
    L.append("")
    L.append("1. **归位**：游离条目进对应域（对外件→`交付/`、内部件→`产物/`、文档→`project-state/契约/`、工具→`scripts/`）。")
    L.append("2. **移 tmp/**：确认是一次性中间产物的移 `tmp/` 并命名 `_YYYYMMDD_<用途>.<ext>`。")
    L.append("3. **请用户裁决**：无法判定的登记 `project-state/待办/自然人.md`，不擅自删除。")
    L.append("")
    L.append("> 结构变更纪律（v3.14.0）：多项目拆分、跨域大搬运属结构变更，**先出方案报用户确认再执行**。")

    text = "\n".join(L)
    print(text)
    if args.write:
        out = root / "project-state" / f"路径盘点-{now:%Y%m%d}.md"
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\n[已写入] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
