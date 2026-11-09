#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方案视图区检查器（v5.16.0「方案视图区」补丁）

检查项目根的 ``方案/``（原始资料 / 产品稿 / 原型图）是否按纪律落地：
  * 完整模式：方案/ 与三子目录、README.md、原始资料/清单.md 必须存在
  * 原始资料/ 下的实体文件必须在 清单.md 登记一行
  * 产品稿/*.md 必须带「导出日期」「源路径」双标注
  * 原型图/ 下的图片必须在 原型图/清单.md 登记
  * 快速模式未建 方案/ → INFO + 退出码 2（无法检查，**不是「没问题」**）

设计口径（与框架一致）：
  * 方案/ 是**导出视图 + 原始件归档**，不是第二事实源；本器只核结构与标注，
    不核导出件内容是否与真源一致、不核图是否最新、不核客户是否收到。
  * 三态退出码：0 干净 ｜ 1 有 ERROR ｜ 2 无法检查（**不是「没问题」**）。

用法::

    python -X utf8 scripts/solution_audit.py <项目根> [--json] [--check]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

VERSION = "5.16.0"

DEFAULT_DIR = "方案"
SUBDIRS = ("原始资料", "产品稿", "原型图")
LEDGER_REL = os.path.join("原始资料", "清单.md")
PROTO_LEDGER_REL = os.path.join("原型图", "清单.md")
LEDGER_NAME = "清单.md"
SKIP_NAMES = {"README.md", "清单.md", ".gitkeep", ".keep", ".DS_Store", "Thumbs.db"}
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif")

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"
SEV_ORDER = {ERROR: 0, WARN: 1, INFO: 2}
SEV_TAG = {ERROR: "E", WARN: "W", INFO: "I"}

EXPORT_MARKS = ("导出日期", "源路径")
README_MARKS = ("原始资料", "产品稿", "原型图", "真源", "最后刷新")
LEDGER_HEADER_MIN_CELLS = 6

SCOPE_NOTE = "范围声明：只核 方案/ 的目录结构、索引/台账存在性与导出件标注齐全度。"
SCOPE_NOTE2 = "核不了：导出件内容是否与真源一致、截图是否为最新一轮、是否已发客户、原始资料是否被完整消化。"


def _reconfigure() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def out(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def read_text(path: str):
    """UTF-8(-sig) → UTF-8 → GBK 兜底；读不到返回 None。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


class Reporter(object):
    def __init__(self) -> None:
        self.items = []

    def add(self, code: str, sev: str, where: str, msg: str) -> None:
        self.items.append({"code": code, "sev": sev, "where": where, "msg": msg})

    def count(self, sev: str) -> int:
        return sum(1 for it in self.items if it["sev"] == sev)

    @property
    def counts(self) -> dict:
        return {sev: self.count(sev) for sev in (ERROR, WARN, INFO)}

    @property
    def ok(self):
        return self.count(ERROR) == 0


def resolve_base(root: str) -> str:
    """接受项目根，也接受 .../project-state（自动归一）。"""
    base = os.path.abspath(root)
    if os.path.basename(base.rstrip("\\/")) == "project-state":
        parent = os.path.dirname(base)
        if os.path.isdir(parent):
            return parent
    return base


def load_cfg(base: str):
    cfg_path = os.path.join(base, "project-state", "config.json")
    text = read_text(cfg_path)
    if text is None:
        return None, "读不到 %s" % cfg_path
    try:
        cfg = json.loads(text)
    except ValueError as exc:
        return None, "解析失败 %s: %s" % (cfg_path, exc)
    if not isinstance(cfg, dict):
        return None, "config.json 顶层不是对象"
    return cfg, ""


def strip_registry_value(value) -> str:
    """注册表值 → 纯相对路径（剥 '#' 注释与 '——' 说明，去掉外层斜杠/反斜杠）。"""
    if not isinstance(value, str):
        return ""
    text = value.split("#")[0].split("——")[0].strip()
    text = text.strip("`").strip()
    text = text.replace("\\", "/").strip("/")
    return text


def table_rows(text: str):
    """解析 markdown 表格数据行（跳过表头与分隔行），返回 [[cell, ...], ...]。"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        if all(set(c) <= set("-: ") and c for c in cells):
            continue
        if cells and cells[0] in ("文件名", "页面/屏", "页面", "文件"):
            continue
        rows.append(cells)
    return rows


def is_placeholder(name: str) -> bool:
    bare = name.strip().strip("`").strip()
    if not bare:
        return True
    if bare.startswith("<") or "示例" in bare or bare.startswith("（"):
        return True
    return False


def iter_files(directory: str):
    """递归列出目录下所有文件（不跟随符号链接目录），返回绝对路径列表。"""
    result = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            result.append(os.path.join(dirpath, name))
    return sorted(result)


def find_case_insensitive(directory: str, name: str):
    """目录下按文件名不区分大小写查找；找不到返回 None。Windows 上大小写不该造成假失败。"""
    candidate = os.path.join(directory, name)
    if os.path.isfile(candidate):
        return candidate
    try:
        lowered = name.lower()
        for entry in os.listdir(directory):
            if entry.lower() == lowered:
                full = os.path.join(directory, entry)
                if os.path.isfile(full):
                    return full
    except OSError:
        pass
    return None


def rel_to(base: str, path: str) -> str:
    try:
        return os.path.relpath(path, base).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")


def proto_registered(name: str, rel_in: str, proto_lower: str, rows) -> bool:
    """原型图是否已在 清单.md 登记。

    认三种写法（都按大小写不敏感）：
      1) 整份文件名直接出现在清单正文（含带路径写法）
      2) 文件名形如 ``<页面>@<档位>.png``：清单里同一行同时有「页面」与「档位」两格
      3) 文件名无 ``@``：去扩展名的名字出现在清单正文
    """
    if name.lower() in proto_lower or rel_in.lower() in proto_lower:
        return True
    stem = os.path.splitext(name)[0]
    if "@" not in stem:
        return stem.lower() in proto_lower
    page, width = stem.rsplit("@", 1)
    page_l = page.strip().lower()
    width_l = width.strip().lower()
    if width_l.endswith("px"):
        width_l = width_l[:-2]
    for row in rows:
        cells = [c.strip().strip("`").lower() for c in row]
        if page_l not in cells:
            continue
        widths = [c[:-2] if c.endswith("px") else c for c in cells]
        if width_l in widths:
            return True
    return False


def audit(base: str, cfg: dict):
    """返回 (Reporter, ok)；ok 为 None 表示「无法检查」。"""
    r = Reporter()

    paths = cfg.get("paths")
    sol_rel = DEFAULT_DIR
    if isinstance(paths, dict):
        sol_rel = strip_registry_value(paths.get("方案")) or DEFAULT_DIR
    sol_rel = sol_rel or DEFAULT_DIR
    sol_abs = os.path.join(base, sol_rel)

    mode = cfg.get("mode")
    mode = mode if mode in ("full", "quick") else "quick"
    full = mode == "full"

    def severity_for_full(full_sev: str, quick_sev: str) -> str:
        return full_sev if full else quick_sev

    if not os.path.isdir(sol_abs):
        if full:
            r.add(
                "SOL-MISSING",
                ERROR,
                sol_rel,
                "完整模式必须建 %s/（原始资料 / 产品稿 / 原型图）——完整模式有产品稿与原型交付，"
                "用户需要能随时看；缺目录等于方案视图区未落地（v5.16.0 方案视图区）" % sol_rel,
            )
            return r, True
        r.add(
            "SOL-NOT-ENABLED",
            INFO,
            sol_rel,
            "快速模式未建方案视图区（默认按需建）——本次判为「无法检查」，不是「没问题」",
        )
        return r, None

    # 1) 三子目录
    for sub in SUBDIRS:
        if not os.path.isdir(os.path.join(sol_abs, sub)):
            sev = severity_for_full(ERROR, WARN)
            r.add(
                "SOL-SUBDIR",
                sev,
                rel_to(base, os.path.join(sol_abs, sub)),
                "缺子目录 %s/（三子目录固定：原始资料 / 产品稿 / 原型图）" % sub,
            )

    # 2) 根级索引 README.md
    readme_abs = os.path.join(sol_abs, "README.md")
    readme_text = read_text(readme_abs) if os.path.isfile(readme_abs) else None
    if readme_text is None:
        r.add(
            "SOL-README",
            severity_for_full(ERROR, WARN),
            rel_to(base, readme_abs),
            "缺方案视图区索引 README.md（要写三子目录用途、真源指针、最后刷新人与日期）",
        )
    else:
        missing = [m for m in README_MARKS if m not in readme_text]
        if missing:
            r.add(
                "SOL-README-THIN",
                WARN,
                rel_to(base, readme_abs),
                "README.md 缺关键要素：%s（索引要能让人一眼看懂三子目录用途与真源指针）"
                % "、".join(missing),
            )

    # 3) 根下只放索引
    if os.path.isdir(sol_abs):
        for name in sorted(os.listdir(sol_abs)):
            if name in SUBDIRS or name == "README.md":
                continue
            r.add(
                "SOL-ROOT-STRAY",
                WARN,
                rel_to(base, os.path.join(sol_abs, name)),
                "方案/ 根下只允许 README.md 与三个子目录；其余请归入子目录（避免又出一层随手放）",
            )

    # 4) 原始资料：清单.md + 逐文件登记
    ledger_abs = os.path.join(sol_abs, LEDGER_REL)
    ledger_text = read_text(ledger_abs) if os.path.isfile(ledger_abs) else None
    if ledger_text is None:
        r.add(
            "SOL-NO-LEDGER",
            severity_for_full(ERROR, WARN),
            rel_to(base, ledger_abs),
            "缺原始资料台账 %s（六列：文件名 | 收到时间 | 来源 | 类型 | 已消化到哪 | 备注）"
            % LEDGER_REL.replace("\\", "/"),
        )
    else:
        rows = table_rows(ledger_text)
        header_cells = 0
        for line in ledger_text.splitlines():
            line = line.strip()
            if line.startswith("|") and "文件名" in line:
                header_cells = len([c for c in line.strip("|").split("|")])
                break
        if header_cells and header_cells < LEDGER_HEADER_MIN_CELLS:
            r.add(
                "SOL-LEDGER-HEADER",
                WARN,
                rel_to(base, ledger_abs),
                "台账表头只有 %d 列，应为 6 列（文件名 | 收到时间 | 来源 | 类型 | 已消化到哪 | 备注）"
                % header_cells,
            )
        registered = set()
        ledger_dir = os.path.join(sol_abs, "原始资料")
        for cells in rows:
            if not cells:
                continue
            name = cells[0].strip().strip("`").strip()
            if is_placeholder(name):
                continue
            registered.add(name.lower())
            if find_case_insensitive(ledger_dir, name) is None:
                r.add(
                    "SOL-LEDGER-DANGLING",
                    WARN,
                    rel_to(base, ledger_abs),
                    "台账登记了但原件不在 %s 下：%s（缺件请在备注列写明「客户未给/已删」，或删除该行）"
                    % ("原始资料", name),
                )
        src_dir = os.path.join(sol_abs, "原始资料")
        if os.path.isdir(src_dir):
            for path in iter_files(src_dir):
                name = os.path.basename(path)
                if name in SKIP_NAMES or name.startswith("."):
                    continue
                rel_in = rel_to(src_dir, path)
                if name.lower() in registered or rel_in.lower() in registered:
                    continue
                r.add(
                    "SOL-LEDGER-MISSING",
                    severity_for_full(ERROR, WARN),
                    rel_to(base, path),
                    "原件未登记进原始资料/清单.md（原件进目录必须同步登记一行）",
                )

    # 5) 产品稿：导出件双标注
    draft_dir = os.path.join(sol_abs, "产品稿")
    if os.path.isdir(draft_dir):
        for path in iter_files(draft_dir):
            name = os.path.basename(path)
            if name in SKIP_NAMES or name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in (".md", ".txt"):
                continue
            text = read_text(path) or ""
            missing = [m for m in EXPORT_MARKS if m not in text]
            if missing:
                r.add(
                    "SOL-EXPORT-UNMARKED",
                    severity_for_full(ERROR, WARN),
                    rel_to(base, path),
                    "导出件缺标注：%s（每份导出件必须写「导出日期：YYYY-MM-DD」「源路径：…」，"
                    "真源改了就重新导出覆盖）" % "、".join(missing),
                )

    # 6) 原型图：清单登记
    proto_dir = os.path.join(sol_abs, "原型图")
    if os.path.isdir(proto_dir):
        proto_ledger_abs = os.path.join(proto_dir, LEDGER_NAME)
        proto_text = read_text(proto_ledger_abs) if os.path.isfile(proto_ledger_abs) else None
        images = [
            p
            for p in iter_files(proto_dir)
            if os.path.splitext(p)[1].lower() in IMAGE_EXT
            and os.path.basename(p) not in SKIP_NAMES
        ]
        if images and proto_text is None:
            r.add(
                "SOL-PROTO-NO-LEDGER",
                severity_for_full(ERROR, WARN),
                rel_to(base, proto_ledger_abs),
                "原型图/ 下有 %d 张图但缺 清单.md（页面 | 档位 | 导出日期 | 源路径 | 备注）" % len(images),
            )
        elif proto_text is not None:
            proto_lower = proto_text.lower()
            proto_rows = table_rows(proto_text)
            for path in images:
                name = os.path.basename(path)
                rel_in = rel_to(proto_dir, path)
                if proto_registered(name, rel_in, proto_lower, proto_rows):
                    continue
                r.add(
                    "SOL-PROTO-UNLISTED",
                    WARN,
                    rel_to(base, path),
                    "图未登记进原型图/清单.md（页面/档位/导出日期/源路径）——无主图，用户分不清哪张是最新",
                )

    return r, r.ok


def emit_report(r: Reporter, base: str, sol: str, ok, as_json: bool) -> int:
    counts = r.counts
    if as_json:
        payload = {
            "version": VERSION,
            "base": base,
            "dir": sol,
            "ok": ok,
            "counts": counts,
            "items": r.items,
            "scope": [SCOPE_NOTE, SCOPE_NOTE2],
        }
        out(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if ok is None:
            head = "方案视图区：无法检查（不是「没问题」）"
        elif counts[ERROR]:
            head = "方案视图区：有 ERROR"
        else:
            head = "方案视图区：干净"
        out("=" * 62)
        out("%s — %s" % (head, os.path.join(base, sol).replace("\\", "/")))
        out("=" * 62)
        for item in sorted(r.items, key=lambda it: (SEV_ORDER[it["sev"]], it["code"])):
            out("  [%s] %-22s %s" % (SEV_TAG[item["sev"]], item["code"], item["where"]))
            out("        %s" % item["msg"])
        out("-" * 62)
        out(
            "汇总：ERROR %d ｜ WARN %d ｜ INFO %d"
            % (counts[ERROR], counts[WARN], counts[INFO])
        )
        out(SCOPE_NOTE)
        out(SCOPE_NOTE2)

    if ok is None:
        return 2
    return 1 if counts[ERROR] else 0


def main(argv=None) -> int:
    _reconfigure()
    parser = argparse.ArgumentParser(
        description="方案视图区检查器（结构 / 台账 / 导出标注）",
        epilog="退出码：0 干净 ｜ 1 有 ERROR ｜ 2 无法检查（**不是「没问题」**）",
    )
    parser.add_argument("root", nargs="?", default=os.getcwd(), help="项目根（缺省当前目录；也可给 project-state）")
    parser.add_argument("--json", action="store_true", help="机读输出")
    parser.add_argument("--check", action="store_true", help="只印一行计数（收口门禁用）")
    args = parser.parse_args(argv)

    base = resolve_base(args.root)
    if not os.path.isdir(base):
        out("无法检查：项目根不存在 %s" % base)
        return 2

    cfg, err = load_cfg(base)
    if cfg is None:
        if args.json:
            out(json.dumps({"ok": None, "error": err}, ensure_ascii=False, indent=2))
        else:
            out("方案视图区：无法检查（不是「没问题」）——%s" % err)
            out("  该检查器要求项目里有 project-state/config.json；框架根与项目根都应有该文件。")
        return 2

    r, ok = audit(base, cfg)
    counts = r.counts
    sol_rel = DEFAULT_DIR
    paths = cfg.get("paths")
    if isinstance(paths, dict):
        sol_rel = strip_registry_value(paths.get("方案")) or DEFAULT_DIR

    if args.check:
        tag = "无法检查" if ok is None else ("有 ERROR" if counts[ERROR] else "干净")
        out(
            "方案视图区(%s)：%s ｜ ERROR %d / WARN %d / INFO %d"
            % (sol_rel, tag, counts[ERROR], counts[WARN], counts[INFO])
        )
        if ok is None:
            return 2
        return 1 if counts[ERROR] else 0

    return emit_report(r, base, sol_rel, ok, args.json)


if __name__ == "__main__":
    sys.exit(main())
