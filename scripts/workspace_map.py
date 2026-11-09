#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workspace_map.py — 工作区地图（文件面冷启动 + 登记对账，v1.0.0）

为什么有这个脚本
----------------
recall.py（v5.6.0）解决的是**状态面**冷启动（"项目到哪了"：卡 frontmatter / 覆盖矩阵 /
发现项）；本脚本补的是**文件面**（"工作区里有什么文件、每个是什么、登记与磁盘对不对得上"）。
痛点实测见 recall.py 文档：真实项目冷启动吃掉总输入 38.2%、网络盘单次目录遍历 10–17 秒
——LLM 靠 ls/grep 摸工作区这条路成本已被钉死。

形态（Obsidian 分层目录页 × aider 预算控制的合体，聚合派生不登记）：
  - 磁盘事实（os.walk 单次遍历）
  - + 既有登记的语义（契约/上下文索引.md 的 L0 摘要行、目录用途段〔v5.18.0 项目自定域〕、
    方案/两份清单、md frontmatter title）
  - → 工作区地图（机器 JSON + 人读目录页两份，同一数据源派生）

权威分层（与 recall.py 同一根线）
--------------------------------
- **磁盘事实** 唯一真相源 = 文件系统本身
- **语义登记** 唯一真相源 = 契约/上下文索引.md 与方案/清单（本脚本只读聚合，不改一字）
- **地图** = 派生物：允许存在，但对账口径以真相源为准并报差异（drift）；**不许手改**，
  手改会在下次刷新被覆盖。

三条不许（对齐 02_方法论 M6）
  1. 地图不当真源——l0 是指针与摘要，内容以原文件为准；
  2. 不许人工编辑地图文件；
  3. unknown 占比低 ≠ 工作区健康——它只说明"登记过的占比"。

用法
----
  python -X utf8 scripts/workspace_map.py <项目根> [--json] [--check] [--write] [--out DIR]
  退出码（--check）：0 = 干净；1 = 有 ERROR 级漂移；2 = 无法检查（缺 project-state 或索引，
  **不是"没问题"**）。
"""

import argparse
import fnmatch
import json
import os
import re
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- 常量注册表

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".obsidian", ".zcode", ".idea", ".vscode",
             ".pytest_cache", ".ruff_cache", ".mypy_cache",
             ".mimosa"}  # v5.18.0：安全扫描器工具状态目录（单会话可积累 10 万+ 文件，进地图只会刷屏）
SKIP_FILES = {".DS_Store", "Thumbs.db"}

# 目录用途注册表（来自 agent.md 工作区布局与各域契约，路径相对项目根）
DIR_PURPOSE = {
    "project-state": "共享工作区：全部角色产出的正式落盘区（结构由主控裁决）",
    "project-state/契约": "先确认后开发：全部契约文档，语义真源区（须登记上下文索引）",
    "project-state/需求卡": "一需求一目录的卡卷宗（card.md+思路+验收记录+证据）",
    "project-state/测试卡": "一次系统级测试活动一目录（tc.md+覆盖矩阵+发现项+证据）",
    "project-state/进度": "每角色一 json 的进度与账本（派生物，以卡为准）",
    "project-state/问题": "CR、审核记录与事故单证",
    "project-state/记忆": "角色档案、知识沉淀与各类台账",
    "project-state/思路": "跨卡思路文档（卡内的随卡卷宗走）",
    "project-state/待办": "待办与自然人依赖（夜间编排的地雷清单）",
    "project-state/复盘": "复盘台账与行动项（按迭代触发）",
    "project-state/代码": "代码级角色的代码产出区",
    "project-state/产物": "内部产物与旧版归档",
    "交付": "客户交互区：只允许当前有效件+发出记录.md+归档/",
    "方案": "方案视图区：原始资料（唯一归档点）/产品稿/原型图（导出视图不改真源）",
    "方案/原始资料": "客户给的方案/参考原件，唯一实体归档点（只进不改）",
    "方案/产品稿": "产品经理从 PRD 导出的人读件（带导出日期+源路径）",
    "方案/原型图": "UI 导出的低保真/高保真图（带导出日期+源路径）",
    "tmp": "一次性中间文件（收口即清，超龄由 workspace_hygiene 处置）",
    "scripts": "框架自检与工具脚本",
    "scripts/tests": "各工具脚本的回归用例（改脚本必先跑）",
    "skills": "12 角色 SKILL 定义（框架侧资产，随框架走）",
}
DIR_WHEN = {
    "project-state/契约": "开工/验收/改契约前",
    "project-state/需求卡": "查某需求的状态、AC 与证据时",
    "project-state/进度": "对账进度与成本时（以卡为准）",
    "交付": "发出对外件与核对发出记录时",
    "方案/原始资料": "回看客户原件时",
}

# 项目根白名单（游离物判定豁免）
ROOT_KNOWN = {
    "agent.md": "多角色协作框架主控定义（工作区根标志）",
    "README.md": "项目说明",
    "启动.exe": "面向人的启动器（必须在根；形态由用户需求/所属域 skill 定义）",
    "启动.bat": "面向人的启动器（必须在根；形态由用户需求/所属域 skill 定义）",
    "工作区地图.md": "用户层导航页：什么在哪（本脚本生成物，勿手改，刷新即覆盖）",
}

# 上下文索引各段 → 基目录（相对项目根；None = 按名字双解析）
SECTION_BASE = {
    "契约": ["project-state/契约"],
    "工作区流程文件": ["project-state", ""],  # 空基目录 = 项目根（方案/ 等在根）
    "记忆": ["project-state/记忆"],
    "问题": ["project-state/问题"],
    "思路": ["project-state/思路"],
    "进度": ["project-state/进度"],
    "复盘": ["project-state/复盘"],
    "需求卡": None,
    "交付": ["交付"],
    "代码": ["代码", "project-state/代码"],
}
# 只对这些域做"磁盘有但没登记"的反向对账（索引承诺最强的是契约域；卡卷宗是运行态不反查）
REVERSE_CHECK_DIRS = ["project-state/契约"]

RUNTIME_MARK = re.compile(r"【运行")  # 【运行】/【运行·按需建】→ 开工前不存在属正常


# ---------------------------------------------------------------- 工具函数

def est_tokens(text):
    """粗估 token：ASCII 约 4 字符/token，CJK 约 1.5 字符/token。"""
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return int(ascii_n / 4 + (len(text) - ascii_n) / 1.5)


def clean_name(raw):
    """登记名清洗：去 **bold**、（vX.Y.Z …）版本尾注、空白；返回 (名字, 是否运行态)。"""
    name = raw.strip().strip("`").replace("**", "")
    runtime = bool(RUNTIME_MARK.search(name))
    name = re.sub(r"【[^】]*】", "", name)
    name = re.sub(r"（v[\d.][^）]*）$", "", name).strip()
    return name, runtime


def clean_l0(raw):
    """L0 摘要清洗：去 markdown 强调与链接，压到 120 字。"""
    t = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", raw)      # [[a|b]] → b
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)                  # [[a]] → a
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)             # [a](b) → a
    t = t.replace("**", "").replace("`", "").strip()
    return t[:120]


def read_head(path, limit=4096):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def frontmatter_title(path):
    head = read_head(path, 2048)
    m = re.match(r"^---\s*\n(.*?\n)?title:\s*(.+)", head, re.S)
    if m:
        return m.group(2).strip().strip("\"'")[:80]
    return None


# ---------------------------------------------------------------- 登记解析

def parse_context_index(index_path):
    """解析 契约/上下文索引.md 全部表格行 → [{name, l0, section, runtime, glob}]。"""
    entries = []
    section = ""
    for line in read_head(index_path, 200000).splitlines():
        m = re.match(r"^##\s+([^\s（(]+)", line)
        if m:
            section = m.group(1).rstrip("/")
            continue
        if not line.strip().startswith("|"):
            continue
        esc = "\x00"  # 表格内转义竖线 \| 先藏起，避免被当列分隔劈断单元格
        cells = [c.strip().replace(esc, "|") for c in
                 line.strip().strip("|").replace("\\|", esc).split("|")]
        if len(cells) < 2 or set(cells[0]) <= {"-", " ", ":"}:
            continue
        runtime = False
        names = []
        for part in re.split(r"[、]", cells[0]):  # 一格多名用「、」枚举；版本尾注内的「；，」不拆
            name, rt = clean_name(part)
            runtime = runtime or rt
            if name and name not in ("文档", "文件", "条目", "登记", "目录"):
                names.append(name)
        for name in names:
            entries.append({
                "name": name,
                "l0": clean_l0(cells[1]) if len(cells) > 1 else "",
                "section": section,
                "runtime": runtime,
                "glob": any(ch in name for ch in "*<>{}"),
            })
    return entries


def parse_solution_ledgers(root):
    """方案/原始资料/清单.md 与 原型图/清单.md → {相对方案/的文件名: 摘要}。"""
    led = {}
    for rel in ("方案/原始资料/清单.md", "方案/原型图/清单.md"):
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            continue
        area = rel.split("/")[1]
        for line in read_head(p, 60000).splitlines():
            if not line.strip().startswith("|"):
                continue
            esc = "\x00"  # 同 parse_context_index：先藏转义竖线再切列
            cells = [c.strip().replace(esc, "|") for c in
                     line.strip().strip("|").replace("\\|", esc).split("|")]
            if len(cells) < 2 or set(cells[0]) <= {"-", " ", ":"}:
                continue
            fname = cells[0].replace("**", "").strip()
            if not fname or fname in ("文件名", "页面", "文件", "条目"):
                continue
            bits = [c for c in cells[1:4] if c]
            led["方案/%s/%s" % (area, fname)] = "方案·%s：%s" % (area, "／".join(bits)[:100])
    return led


def parse_dir_purposes(index_path):
    """解析 上下文索引.md「目录用途」段 → {目录路径(相对项目根): 用途}（v5.18.0）。

    项目自定域（如 交付产物/、部署产物/、环境配置/ 的子域）的用途由项目自己在索引里
    登记一行的域级语义；框架标准目录仍以本脚本 DIR_PURPOSE 为准。display-only：
    只喂 build_tree 的目录树用途列，不参与登记↔磁盘对账（对账语义仍走 parse_context_index）。
    """
    out = {}
    if not os.path.isfile(index_path):
        return out
    section = ""
    for line in read_head(index_path, 200000).splitlines():
        m = re.match(r"^##\s+([^\s（(]+)", line)
        if m:
            section = m.group(1).rstrip("/")
            continue
        if section != "目录用途" or not line.strip().startswith("|"):
            continue
        esc = "\x00"
        cells = [c.strip().replace(esc, "|") for c in
                 line.strip().strip("|").replace("\\|", esc).split("|")]
        if len(cells) < 2 or set(cells[0]) <= {"-", " ", ":"}:
            continue
        name = cells[0].replace("**", "").strip().strip("`").rstrip("/")
        name = re.sub(r"【[^】]*】", "", name).strip()
        if not name or name in ("目录", "域", "路径"):
            continue
        purpose = cells[1].strip()
        if purpose:
            out[name] = purpose
    return out


# ---------------------------------------------------------------- 扫描

def scan_disk(root):
    """单次遍历 → files=[{rel, bytes, mtime}], dirs=[rel]。跳过噪声目录。"""
    files, dirs = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir != ".":
            dirs.append(rel_dir)
        for fn in sorted(filenames):
            if fn in SKIP_FILES or fn.endswith((".pyc",)):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append({
                "rel": (rel_dir + "/" + fn) if rel_dir != "." else fn,
                "bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return files, dirs


# ---------------------------------------------------------------- 语义聚合

def domain_of(rel):
    top = rel.split("/")[0]
    if top == "project-state":
        return "/".join(rel.split("/")[:2])
    return top


def pat_translate(p):
    """索引占位符 → fnmatch 通配：<页面>/<日期> → *，{a/b} 枚举 → *。"""
    p = re.sub(r"<[^>]+>", "*", p)
    p = re.sub(r"\{[^}]*\}", "*", p)
    return p


def classify(root, files, dirs, index_entries, solution_ledgers):
    """给每个文件挂 domain/kind/l0；返回 (files, unknown, drift)。"""
    file_set = {f["rel"] for f in files}
    dir_set = set(dirs)
    exact = {}          # 磁盘路径 → 登记条目
    glob_regs = []      # [(候选 pattern 列表, 条目, 是否目录登记)]
    drift = []
    for e in index_entries:
        bases = SECTION_BASE.get(e["section"])
        if bases is None:  # 卡卷宗等运行态段：只做语义参考，不做对账
            continue
        is_dir = e["name"].endswith("/")
        cands = []
        for base in bases:
            cands.append((base + "/" + e["name"]) if base else e["name"])
            if "/" in e["name"]:  # 登记名自带域前缀时会拼出 问题/问题/…，补"去首段"变体
                tail = e["name"].split("/", 1)[1]
                cands.append((base + "/" + tail) if base else tail)
        if e["name"].startswith(("project-state/", "方案/", "交付/", "代码/")):
            cands.append(e["name"])  # 登记名已是项目根相对路径
        cands = list(dict.fromkeys(c.rstrip("/") for c in cands))  # 去重保序
        pool = dir_set if is_dir else file_set
        if e["glob"] or is_dir:
            glob_regs.append((cands, e, is_dir))
            if not e["runtime"]:
                if not any(fnmatch.fnmatch(p, pat_translate(c)) for c in cands for p in pool):
                    drift.append({"type": "registered_missing_glob", "severity": "warn",
                                  "path": e["name"],
                                  "detail": "索引登记（%s 段）未匹配到任何%s" % (
                                      e["section"], "目录" if is_dir else "文件")})
        else:
            hit = next((c for c in cands if c in pool), cands[0])
            exact[hit] = e
            if hit not in pool and not e["runtime"]:
                drift.append({"type": "registered_missing", "severity": "error",
                              "path": hit,
                              "detail": "索引登记（%s 段）但磁盘不存在，且非【运行】" % e["section"]})

    # 反向：契约域磁盘有但没登记（备份/中间件除外——那是整洁问题，单独升级见 loose_file）
    backup_pat = re.compile(r"\.bak|\.out$|\.tmp$|^_", re.I)
    registered_patterns = list(exact.keys()) + [pat_translate(c) for cands, _, _ in glob_regs for c in cands]
    for f in files:
        rel = f["rel"]
        base_name = os.path.basename(rel)
        parent = os.path.dirname(rel)
        if backup_pat.search(base_name):
            f["_backup"] = True
            if parent in ("", "project-state"):  # 三层根（框架根/项目根/project-state 根）零游离
                drift.append({"type": "loose_file", "severity": "error", "path": rel,
                              "detail": "顶层游离备份/中间件（agent.md：框架根/项目根/project-state 根零游离文件；处置走 path_audit/workspace_hygiene）"})
            elif parent in ("scripts", "skills") or parent.startswith(("scripts/", "skills/")):
                drift.append({"type": "loose_file", "severity": "warn", "path": rel,
                              "detail": "框架侧目录内的游离备份件（同零游离纪律，域内降级 warn）"})
            continue
        if parent in REVERSE_CHECK_DIRS and \
                not any(rel == p or fnmatch.fnmatch(rel, p) for p in registered_patterns):
            drift.append({"type": "unregistered_contract", "severity": "error",
                          "path": rel, "detail": "契约域文件未登记进 上下文索引.md（索引承诺 L0 全汇总）"})

    # 文件级语义挂接
    fm_cache = {}
    for f in files:
        rel = f["rel"]
        f["domain"] = domain_of(rel)
        base = os.path.basename(rel)
        if f.pop("_backup", False):
            f["l0"], f["l0_source"], f["kind"] = None, None, "备份中间件"
            continue
        l0, src, kind = None, None, "文件"
        if rel == "project-state/进度/工作区地图.json":
            l0, src, kind = "工作区地图机器版（本脚本生成物，派发简报引用）", "约定", "机读块"
        e = exact.get(rel)
        if e is None:
            for cands, ge, _ in glob_regs:
                if any(fnmatch.fnmatch(rel, pat_translate(c)) for c in cands):
                    e = ge
                    break
        if e:
            l0, src = e["l0"] or None, "上下文索引"
        if l0 is None and rel in solution_ledgers:
            l0, src = solution_ledgers[rel], "方案清单"
        if l0 is None and rel in ROOT_KNOWN:
            l0, src, kind = ROOT_KNOWN[rel], "根白名单", "根级文件"
        if l0 is None and base in ("README.md",):
            l0, src, kind = "%s 的目录页/索引" % os.path.dirname(rel) or "根", "约定", "索引"
        if l0 is None and base in ("card.md", "tc.md"):
            if rel not in fm_cache:
                fm_cache[rel] = frontmatter_title(os.path.join(root, rel))
            t = fm_cache[rel]
            l0 = ("卡：%s" % t) if t else ("需求卡卷宗" if base == "card.md" else "测试卡卷宗")
            src, kind = "frontmatter", "卡卷宗"
        if l0 is None and rel.endswith((".json",)):
            head = read_head(os.path.join(root, rel), 512)
            kind = "机读块" if "<!-- machine:" in head else "数据"
        if l0 is None and rel.endswith((".csv",)):
            kind, l0, src = "台账", None, None  # 台账有名无义不算 unknown？——算，语义靠列头，不猜
        if l0 is None and "模板" in base:
            kind = "模板"
        if rel.endswith((".py", ".js", ".ts", ".mjs", ".bat", ".sh", ".ps1")):
            kind = "脚本"
        f["l0"], f["l0_source"], f["kind"] = l0, src, kind

    noise_domains = ("tmp", "scripts", "skills", ".git")  # 前缀匹配：框架侧/临时区不进 unknown
    unknown = [f["rel"] for f in files
               if f["l0"] is None
               and not f["domain"].startswith(noise_domains)
               and f["kind"] not in ("卡卷宗", "脚本", "机读块", "数据", "模板", "索引")]
    return files, unknown, drift


# ---------------------------------------------------------------- 渲染

def build_tree(files, dirs, dir_purposes=None):
    extra = dir_purposes or {}

    def purpose_of(d):
        # 取用顺序（v5.18.0）：项目侧 exact → 框架 DIR_PURPOSE → 项目侧最近祖先 → skills 兜底
        if d in extra:
            return extra[d]
        if d in DIR_PURPOSE:
            return DIR_PURPOSE[d]
        parts = d.split("/")
        for i in range(len(parts) - 1, 0, -1):
            anc = "/".join(parts[:i])
            if anc in extra:
                return extra[anc]
        if d.startswith("skills/"):
            return "角色 SKILL 与配套（框架侧资产，随框架走）"
        return None

    tree = []
    for d in sorted(set(dirs)):
        depth = d.count("/")
        if depth > 1 and not d.startswith("方案"):
            continue  # 人读版只到两层（方案区多露一层）
        n = sum(1 for f in files if f["rel"].startswith(d + "/"))
        sub = sum(1 for x in dirs if x.startswith(d + "/") and x.count("/") == depth + 1)
        tree.append({"path": d, "purpose": purpose_of(d), "files": n, "subdirs": sub})
    return tree


def render_human(root, data, out_dir):
    lines = [
        "# 工作区地图（%s）" % os.path.basename(root),
        "",
        "> **用户可见层**（给你看的）：`交付/` 发给客户的东西、`方案/` 产品稿与原型图、`工作区地图.md` 本页、面向人的入口件；",
        "> `project-state/` 是框架层（主控与角色的工作区），找具体文件先查下面目录地图。",
        "> 派生物：由 `scripts/workspace_map.py` 生成，**不许手改**（下次刷新会被覆盖）；",
        "> 内容以原文件为准，`l0` 只是指针与摘要。生成时间 %s，耗时 %.1fs。" % (data["generated_at"], data["elapsed_s"]),
        "",
        "## 目录地图（先看我，再往下翻）",
        "",
        "| 目录 | 里面有什么 | 什么时候看 | 文件数 |",
        "|---|---|---|---|",
    ]
    for t in data["tree"]:
        lines.append("| `%s` | %s | %s | %d |" % (
            t["path"], t["purpose"] or "（未登记用途——项目自定域在 上下文索引「目录用途」段登记，框架标准目录该补 DIR_PURPOSE）",
            DIR_WHEN.get(t["path"], "按需"), t["files"]))
    lines += ["", "## 登记对账（drift）", ""]
    if data["drift"]:
        lines += ["| 级别 | 类型 | 路径 | 说明 |", "|---|---|---|---|"]
        for d in data["drift"]:
            lines.append("| %s | %s | `%s` | %s |" % (d["severity"], d["type"], d["path"], d["detail"]))
    else:
        lines.append("（无漂移：登记与磁盘对得上）")
    lines += ["", "## 未登记语义的文件（unknown，%d 个）" % len(data["unknown"]), ""]
    lines.append("unknown ≠ 没问题——只说明这些文件没有任何登记语义（详见 02_方法论 M6-3）。")
    for u in data["unknown"][:30]:
        lines.append("- `%s`" % u)
    if len(data["unknown"]) > 30:
        lines.append("- …其余 %d 个见 JSON" % (len(data["unknown"]) - 30))
    lines += [
        "",
        "## 给 AI 的用法",
        "",
        "1. 先读本地图定位（目录地图 + drift + unknown），再按需打开原文件——**不要平铺全量**（上下文规范）。",
        "2. 机器版：`--json` 输出或 `工作区地图.json`（files[] 含每文件 domain/kind/l0）。",
        "3. 地图滞后自查：`--check` 会在地图 mtime 早于最新文件时报 WARN。",
        "",
    ]
    out = os.path.join(out_dir, "工作区地图.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out


# ---------------------------------------------------------------- 主流程

def build_map(root, budget):
    t0 = time.time()
    ps = os.path.join(root, "project-state")
    files, dirs = scan_disk(root)
    index_path = os.path.join(ps, "契约", "上下文索引.md")
    index_entries = parse_context_index(index_path) if os.path.isfile(index_path) else []
    dir_purposes = parse_dir_purposes(index_path)
    solution_ledgers = parse_solution_ledgers(root)
    files, unknown, drift = classify(root, files, dirs, index_entries, solution_ledgers)
    summary_fields = {"root", "generated_at", "elapsed_s", "counts", "tree", "drift",
                      "unknown", "usage"}
    data = {
        "root": root,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 2),
        "counts": {
            "files": len(files), "dirs": len(dirs),
            "index_entries": len(index_entries),
            "unknown": len(unknown),
            "drift_error": sum(1 for d in drift if d["severity"] == "error"),
            "drift_warn": sum(1 for d in drift if d["severity"] == "warn"),
        },
        "tree": build_tree(files, dirs, dir_purposes),
        "files": [{k: f[k] for k in ("rel", "bytes", "mtime", "domain", "kind", "l0", "l0_source")}
                  for f in files],
        "unknown": unknown,
        "drift": drift,
        "usage": "先读地图定位再定向打开原文件；地图是派生物不许手改；unknown≠没问题",
    }
    full = json.dumps(data, ensure_ascii=False, indent=1)
    summary = json.dumps({k: v for k, v in data.items() if k in summary_fields and k != "files"},
                         ensure_ascii=False, indent=1)
    data["counts"]["est_tokens_full"] = est_tokens(full)
    data["counts"]["est_tokens_summary"] = est_tokens(summary)
    data["_budget_note"] = ("摘要 %d token（预算 %d）；完整 files[] 明细 %d token，超预算时按域下钻"
                            % (data["counts"]["est_tokens_summary"], budget,
                               data["counts"]["est_tokens_full"]))
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="工作区地图：文件面冷启动 + 登记对账（派生物，不许手改）")
    ap.add_argument("project_root", help="项目根（含 project-state/）")
    ap.add_argument("--json", action="store_true", help="输出完整地图 JSON")
    ap.add_argument("--check", action="store_true", help="门禁模式：只报漂移与计数")
    ap.add_argument("--write", action="store_true", help="落盘 工作区地图.json 与人读版 md")
    ap.add_argument("--out", default=None, help="落盘目录（默认 project-state/进度/）")
    ap.add_argument("--budget", type=int, default=4000, help="摘要 token 预算（默认 4000）")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.project_root)
    ps = os.path.join(root, "project-state")
    if not os.path.isdir(ps):
        print("✗ 无法检查：缺 project-state/（不是框架项目根）：%s" % root, file=sys.stderr)
        return 2
    index_path = os.path.join(ps, "契约", "上下文索引.md")
    if not os.path.isfile(index_path):
        print("✗ 无法检查：缺 %s（对账依据不存在）" % index_path, file=sys.stderr)
        return 2

    data = build_map(root, args.budget)
    c = data["counts"]

    if args.json:
        payload = dict(data)
        payload.pop("_budget_note", None)
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0

    print("工作区地图：%s" % root)
    print("  文件 %d 个 / 目录 %d 个 / 索引登记 %d 条 | 生成 %.1fs"
          % (c["files"], c["dirs"], c["index_entries"], data["elapsed_s"]))
    print("  摘要 ≈%d token（预算 %d）| files 明细 ≈%d token（超预算按下钻/JSON 取明细）"
          % (c["est_tokens_summary"], args.budget, c["est_tokens_full"]))
    if data["drift"]:
        print("  登记对账：ERROR %d / WARN %d" % (c["drift_error"], c["drift_warn"]))
        for d in data["drift"][:15]:
            print("    [%s] %s %s — %s" % (d["severity"], d["type"], d["path"], d["detail"]))
        if len(data["drift"]) > 15:
            print("    …其余 %d 条见 --json" % (len(data["drift"]) - 15))
    else:
        print("  登记对账：登记与磁盘一致（零漂移）")
    print("  unknown（无登记语义）：%d 个%s" % (c["unknown"],
          ("，例如 " + "、".join("`%s`" % u for u in data["unknown"][:5])) if data["unknown"] else ""))

    if args.write:
        # 人读版默认落项目根（用户层导航页）；机器版默认落 进度/；--out 统一覆盖两处（沙箱用）
        md_dir = args.out or root
        out_dir = args.out or os.path.join(ps, "进度")
        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(md_dir, exist_ok=True)
        jpath = os.path.join(out_dir, "工作区地图.json")
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in data.items() if k != "_budget_note"}, f,
                      ensure_ascii=False, indent=1)
        mpath = render_human(root, data, md_dir)
        print("  已落盘：%s / %s" % (jpath, mpath))

    if args.check:
        return 1 if c["drift_error"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
