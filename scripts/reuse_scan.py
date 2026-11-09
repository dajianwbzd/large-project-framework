# -*- coding: utf-8 -*-
"""reuse_scan.py —— v5.13.0「先例复用评估」的机器核（只读）。

为什么有这个脚本
----------------
项目开工初期最容易犯的错，是**闭着眼睛从零造**：主控没问过用户「以前做过类似的吗、
历史项目放在哪些根目录」，团队就直接开工，于是同一个坑踩第二遍。本脚本把这件事拆成
两半：人能答的部分（以前有没有做过）由主控问用户，答案写进本项目
`project-state/config.json` 的 `reuse_roots`；机器能核的部分由本脚本做——
**只读**扫描那些根，列出可复用资产候选（历史项目的需求卡 / 测试卡 / 知识沉淀 /
契约 / 代码模块 / 项目自建脚本），交给项目经理、产品经理、架构师三个角色判定
「复用 / 改造复用 / 不复用」，判定结果落 `project-state/记忆/复用台账.md` 的
`<!-- machine:reuse -->` 机读块，每张新需求卡再在 frontmatter 里写 `reuse` / `reuse_ref`。

三条纪律
--------
1. **只读**：历史项目一个字节都不改，不新建、不删除、不 touch。`--write` 只写**当前**
   项目自己的 `project-state/复用盘点-<YYYYMMDD>.md`。
2. **相似度只是线索，不是结论**：本脚本用纯字符 n-gram 加权重合度排序（确定、可解释、
   无黑箱、无网络、无模型），它**不判定复用价值**，也不做语义理解；判谁复用是三角色的事。
3. **三态退出码**：`2` 不是「没问题」，是「**无法检查**」（项目根不对 / 缺 config）。

用法
----
    python -X utf8 scripts/reuse_scan.py [项目根] --roots \\\\nas\\work --roots "E:\\old"
    python -X utf8 scripts/reuse_scan.py --json          # 机器可读（仅 JSON）
    python -X utf8 scripts/reuse_scan.py --check         # 只做「声明与台账体检」，不扫历史
    python -X utf8 scripts/reuse_scan.py --write         # 另写人读报告（绝不覆盖同名）
    python -X utf8 scripts/reuse_scan.py --quiet         # 只打汇总与最终判据行

    退出码：0 完成且无 ERROR ｜ 1 有 ERROR ｜ 2 无法检查（**不是「没问题」**）
    末行：  REUSE-SCAN: <OK|ERROR|CANNOT-CHECK> findings=ERROR:n WARN:n INFO:n roots:n projects:n

机读台账列头（**一个字都不能差**，8 列且顺序固定）
--------------------------------------------------
    id | date | source_project | source_ref | kind | verdict | target | evidence
"""

import argparse
import datetime
import json
import os
import re
import sys

VERSION = "5.13.0"
SCHEMA = "reuse-scan/1"

# ---------------------------------------------------------------- 契约常量
# 复用台账机读块列头：**一个字都不能差**（8 列且顺序一致）
CANON = ["id", "date", "source_project", "source_ref", "kind", "verdict", "target", "evidence"]
CANON_HEADER = " | ".join(CANON)

# 机读块围栏（与先例 browser_resource_audit.py 同一约定：HTML 注释围栏）
START = "<!-- machine:reuse -->"
END = "<!-- /machine:reuse -->"

KINDS = ["需求卡", "测试卡", "契约", "组件", "代码", "脚本", "知识"]
VERDICTS = ["reuse", "adapt", "reject"]

LEDGER_REL = os.path.join("project-state", "记忆", "复用台账.md")
CONFIG_REL = os.path.join("project-state", "config.json")

# 框架既有脚本白名单：`scripts/` 下不属于这份清单的 `.py` 才是「项目自建脚本」，
# 它们才是「可复用组件 / 工具」的证据（框架脚本每个项目都有，证明不了什么）。
FRAMEWORK_SCRIPTS = {
    "browser_resource_audit", "case_audit", "db_audit", "fix_v317_references",
    "framework_lint", "git_audit", "guide_audit", "migrate",
    "migrate_v317_card_folders", "path_audit", "prompt_regression", "recall",
    "retro_calibrate", "style_audit", "task_estimate", "test_scope_audit",
    "workspace_hygiene", "reuse_scan",
}

# 候选发现时跳过的目录名：产物、镜像、依赖、临时区——钻进去只会扫出一堆假项目。
SKIP_DIRS = {
    "tmp", ".git", "node_modules", "__pycache__", ".venv", "venv",
    "_git镜像", "交付", "产物", "repos", "data", "素材", "环境配置",
}

SEV_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
SEV_LABEL = {"ERROR": "错误", "WARN": "告警", "INFO": "提示"}
SEV_TAG = {"ERROR": "E", "WARN": "W", "INFO": "I"}

ID_RE = re.compile(r"^RU-\d{3}$")
KID_RE = re.compile(r"^k-[0-9A-Za-z][0-9A-Za-z_-]*$")
CARD_DIR_RE = re.compile(r"^card-\d+$")
TC_DIR_RE = re.compile(r"^tc-\d+$")
REQ_RE = re.compile(r"REQ-\d+(?:\.\d+)+")
H1_RE = re.compile(r"^#\s+(.*)$")
KHEAD_RE = re.compile(r"^#{1,6}\s+(k-[0-9A-Za-z][0-9A-Za-z_-]*)\s*(.*)$")
CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
ASCII_TOK_RE = re.compile(r"[a-z0-9][a-z0-9_+#.-]*")

EXCERPT_LEN = 200
MATCHED_TERM_CAP = 12
FILE_COUNT_CAP = 2000

# 权重表（可解释：谁更像「同类需求」）
W_CARD = 3
W_KNOWLEDGE = 2
W_PROJECT = 2
W_CONTRACT = 1
W_MODULE = 1
MAX_FACET_W = W_CARD          # 候选侧最高权重：查询词全命中需求卡标题才算 100.0 分

SCOPE_NOTES = [
    "只读扫描：历史项目一个字节都没改，本脚本也不写入任何历史项目。",
    "相似度只是**线索**，不是结论：纯字符 n-gram 加权重合度，不做语义理解、不联网、不调模型。",
    "本脚本**不判定复用价值**：判「复用 / 改造复用 / 不复用」是项目经理、产品经理、架构师的事。",
]


def out(text=""):
    """打印一行。控制台编码异常时降级，绝不因为一个字符崩掉整次体检。"""
    line = text + "\n"
    try:
        sys.stdout.write(line)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(line.encode(enc, "backslashreplace").decode(enc, "replace"))


def read_text(path):
    """UTF-8 优先、GBK 兜底：真实工程里这两种都存在（先例：recall.py / case_audit.py）。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def uncomment(value):
    """剥掉 frontmatter 行内注释与包裹引号（模板里的 `# 说明` 是给人看的）。"""
    v = (value or "").strip()
    if v[:1] in ("'", '"'):
        q = v[0]
        j = v.find(q, 1)
        return (v[1:j] if j > 0 else v[1:]).strip()
    m = re.search(r"\s#", v)
    if m:
        v = v[:m.start()]
    return v.strip()


def parse_frontmatter(text):
    """读 `---` 之间的键值 → ({键: (值, 行号)}, 闭合行号)。无 frontmatter 返回 (None, 0)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, 0
    fm = {}
    for i in range(1, len(lines)):
        s = lines[i].strip()
        if s == "---":
            return fm, i + 1
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        fm[k.strip()] = (uncomment(v), i + 1)
    return None, 0          # frontmatter 没闭合：当「读不到」


def strip_frontmatter(text):
    """剥掉开头 `---` … `---` 的机读 frontmatter，返回正文。"""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:])
    return text


def split_cells(line, strip_edges=True, width=None):
    """按 `|` 切格（先例 browser_resource_audit.py：剥边规则整表统一，逐行走样才回退）。"""
    s = line.strip()
    parts = [c.strip() for c in s.split("|")]
    raw = list(parts)
    if strip_edges and raw and raw[0] == "":
        raw.pop(0)
    if strip_edges and raw and raw[-1] == "":
        raw.pop()
    if width is None or len(raw) == width:
        return raw
    alt = list(parts)
    if not strip_edges and alt and alt[0] == "":
        alt.pop(0)
    if not strip_edges and alt and alt[-1] == "":
        alt.pop()
    return alt if len(alt) == width else raw


def is_sep_row(cells):
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def find_blocks(lines, start, end):
    """返回 [(起始行号, [(行号, 原文), …], 是否闭合)]——只认整行完全匹配的围栏。"""
    blocks = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == start:
            body = []
            j = i + 1
            while j < len(lines) and lines[j].strip() != end:
                body.append((j + 1, lines[j]))
                j += 1
            blocks.append((i + 1, body, j < len(lines)))
            i = j + 1
        else:
            i += 1
    return blocks


def find_reuse_block(lines):
    """找 `machine:reuse` 机读块 → (body, 是否闭合, 写法)。

    支持两种写法：HTML 注释围栏（框架既有约定，先例 browser_resource_audit.py）优先，
    其次 ```machine:reuse 代码围栏（容错，免得手写台账的人用错围栏就整份作废）。
    """
    blocks = find_blocks(lines, START, END)
    if blocks:
        _, body, closed = blocks[0]
        return body, closed, "html"
    i = 0
    while i < len(lines):
        if lines[i].strip() == "```machine:reuse":
            body = []
            j = i + 1
            while j < len(lines) and lines[j].strip() != "```":
                body.append((j + 1, lines[j]))
                j += 1
            return body, j < len(lines), "fence"
        i += 1
    return None, False, None


def parse_table(body):
    """机读块 → (表头格子, [(行号, 格子)])。注释行/空行/分隔行都跳过。"""
    header = None
    edges = True
    rows = []
    for lineno, raw in body:
        s = raw.strip()
        if not s or s.startswith("<!--"):
            continue
        if header is None:
            header = split_cells(s, True)
            edges = s.startswith("|") or s.endswith("|")
            continue
        cells = split_cells(s, edges, width=len(header))
        if is_sep_row(cells):
            continue
        rows.append((lineno, cells))
    return header, rows


class Report:
    """发现项账本：ERROR / WARN / INFO 三级 + 稳定码。"""

    def __init__(self):
        self.items = []
        self.counts = {"ERROR": 0, "WARN": 0, "INFO": 0}

    def add(self, code, level, message, path=""):
        self.counts[level] += 1
        self.items.append({"level": level, "code": code, "message": message, "path": path})

    def sorted_items(self):
        return sorted(self.items, key=lambda x: (SEV_ORDER[x["level"]], x["code"], x["path"]))


# ---------------------------------------------------------------- 关键词与相似度
def tokenize(text):
    """纯字符 n-gram：ASCII 按小写词切分，中文按字符二元组（bigram）。

    确定的、可解释的、无黑箱的——同样的输入永远给出同样的输出。
    """
    toks = []
    t = text or ""
    low = t.lower()
    for w in ASCII_TOK_RE.findall(low):
        if len(w) >= 2:
            toks.append(w)
    for run in CJK_RUN_RE.findall(t):
        if len(run) == 1:
            toks.append(run)
            continue
        for i in range(len(run) - 1):
            toks.append(run[i:i + 2])
    return toks


def add_terms(bucket, text, weight):
    """把一段文本的词元并入候选侧「文本面」；同一词元取最大权重（出现多次不重复加分）。"""
    for tok in tokenize(text):
        old = bucket.get(tok)
        if old is None or weight > old:
            bucket[tok] = weight


def add_query(bucket, text):
    """把一段文本的词元并入「查询面」（查询面是集合：同一个词只问一次）。"""
    bucket.update(tokenize(text))


def candidate_terms(cand):
    """候选项目的「文本面」→ {词元: 权重}。

    权重就是规格里那五个数：需求卡标题 3、知识沉淀 2、项目名/description 2、
    契约文件名 1、代码模块名 1；同一词元落在多处时取最大权重。这样
    「命中一张需求卡标题」比「命中一个契约文件名」更像一条可复用线索。
    """
    terms = {}
    add_terms(terms, cand.get("name") or "", W_PROJECT)
    add_terms(terms, cand.get("_description") or "", W_PROJECT)
    for c in cand["cards"]:
        add_terms(terms, c["title"], W_CARD)
    for k in cand["knowledge"]:
        add_terms(terms, k["title"], W_KNOWLEDGE)
    for name in cand["contracts"]:
        add_terms(terms, name, W_CONTRACT)
    for m in cand["modules"]:
        add_terms(terms, m["name"], W_MODULE)
    return terms


def similarity(query, terms):
    """加权重合度 → (score, matched_terms)；同分由调用方按路径字典序兜底。

    纯字符 n-gram，确定的、可解释的、没有黑箱：

        score = 100 × Σ(命中词元在**候选侧**的权重) ÷ (查询词元数 × 3)

    分母里的 3 = 候选侧能拿到的最高权重（需求卡标题）：查询词**全部**命中
    需求卡标题才是 100.0 分；只命中契约文件名只有 1/3 的分。
    """
    q = set(query.keys()) if isinstance(query, dict) else set(query)
    if not q or not terms:
        return 0.0, []
    hits = [(terms[t], t) for t in q if t in terms]
    if not hits:
        return 0.0, []
    score = round(100.0 * sum(w for w, _ in hits) / (len(q) * MAX_FACET_W), 1)
    hits.sort(key=lambda x: (-x[0], x[1]))
    return score, [t for _, t in hits[:MATCHED_TERM_CAP]]


# ---------------------------------------------------------------- 只读抽取
def load_json(path):
    """读 JSON → (dict 或 None, 错误说明)。BOM 与 GBK 都兜底。"""
    if not os.path.isfile(path):
        return None, "文件不存在"
    try:
        text = read_text(path)
    except OSError as exc:
        return None, "读不了：%s" % exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, "JSON 解析失败：%s" % exc
    if not isinstance(data, dict):
        return None, "JSON 顶层不是对象"
    return data, ""


def frontmatter_version(path):
    """从 `agent.md` 的 frontmatter 取 `version: "X.Y.Z"`；取不到记 unknown。"""
    if not os.path.isfile(path):
        return "unknown"
    try:
        text = read_text(path)
    except OSError:
        return "unknown"
    fm, _ = parse_frontmatter(text)
    if not fm or "version" not in fm:
        return "unknown"
    return fm["version"][0].strip() or "unknown"


def make_excerpt(body, n=EXCERPT_LEN):
    """正文前 n 字：压掉 markdown 记号与换行，只留人话。"""
    t = re.sub(r"```.*?```", " ", body or "", flags=re.DOTALL)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.DOTALL)
    t = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", t, flags=re.M)
    t = re.sub(r"^[ \t]*[-*+][ \t]+", "", t, flags=re.M)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[*_`>|]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n]


def is_candidate_dir(path):
    """候选项目 = 同时含 `agent.md` 与 `project-state/` 的目录。"""
    return os.path.isfile(os.path.join(path, "agent.md")) and \
        os.path.isdir(os.path.join(path, "project-state"))


def walk_candidates(root, depth, skip_paths):
    """在 root 下 depth 层内找候选项目；**一旦某目录判定为候选就不再向下钻**。"""
    found = []
    queue = [(root, 0)]
    while queue:
        cur, d = queue.pop(0)
        real = os.path.realpath(cur)
        if real in skip_paths:
            found.append((cur, True))
            continue
        if is_candidate_dir(cur):
            found.append((cur, False))
            continue                    # 候选项目：既不向下钻，也不进它的 repos/产物
        if d >= depth:
            continue
        for name in listdir(cur):
            if name in SKIP_DIRS or name.startswith("."):
                continue
            sub = os.path.join(cur, name)
            if os.path.isdir(sub) and not os.path.islink(sub):
                queue.append((sub, d + 1))
    return found


def count_files(path, cap=FILE_COUNT_CAP):
    """数文件个数，封顶：单模块最多数 cap 个，超过记 `2000+`（别把整仓遍历到爆）。"""
    n = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".git", "node_modules", ".venv", "venv")]
        n += len(filenames)
        if n > cap:
            return "%d+" % cap
    return n


def read_cards(proj, limit):
    """需求卡：`project-state/需求卡/card-*/card.md`。"""
    cdir = os.path.join(proj, "project-state", "需求卡")
    items = []
    names = [n for n in listdir(cdir) if CARD_DIR_RE.match(n)][:limit]
    for name in names:
        p = os.path.join(cdir, name, "card.md")
        if not os.path.isfile(p):
            continue
        rel = "project-state/需求卡/%s/card.md" % name
        try:
            text = read_text(p)
        except OSError:
            continue
        fm, _ = parse_frontmatter(text)
        fm = fm or {}
        body = strip_frontmatter(text)
        title = ""
        for line in body.splitlines():
            m = H1_RE.match(line.strip())
            if m:
                title = m.group(1).strip()
                break
        if not title:
            title = (fm.get("title", ("", 0))[0] or "").strip()
        if not title:
            title = name
        refs = []
        for r in REQ_RE.findall(body):
            if r not in refs:
                refs.append(r)
        items.append({
            "id": name,
            "title": title,
            "status": (fm.get("status", ("", 0))[0] or "").strip(),
            "reuse": (fm.get("reuse", ("", 0))[0] or "").strip(),
            "reuse_ref": (fm.get("reuse_ref", ("", 0))[0] or "").strip() or "-",
            "contract_refs": refs,
            "evidence": rel,
            "excerpt": make_excerpt(body),
        })
    return items


def read_tests(proj, limit):
    """测试卡：`project-state/测试卡/tc-*/tc.md`。"""
    tdir = os.path.join(proj, "project-state", "测试卡")
    items = []
    names = [n for n in listdir(tdir) if TC_DIR_RE.match(n)][:limit]
    for name in names:
        p = os.path.join(tdir, name, "tc.md")
        if not os.path.isfile(p):
            continue
        try:
            text = read_text(p)
        except OSError:
            continue
        fm, _ = parse_frontmatter(text)
        fm = fm or {}
        body = strip_frontmatter(text)
        title = ""
        for line in body.splitlines():
            m = H1_RE.match(line.strip())
            if m:
                title = m.group(1).strip()
                break
        if not title:
            title = (fm.get("title", ("", 0))[0] or "").strip() or name
        items.append({
            "id": name,
            "title": title,
            "status": (fm.get("status", ("", 0))[0] or "").strip(),
            "evidence": "project-state/测试卡/%s/tc.md" % name,
        })
    return items


def read_knowledge(proj, limit):
    """知识沉淀：`### k-001 标题` 与表格行两种写法都兼容；解析不到的计入 unparsed。

    真实工程里用的是 `## k-001 主题`（不止 `###`），所以按 `#{1,6}` 认。
    """
    p = os.path.join(proj, "project-state", "记忆", "知识沉淀.md")
    if not os.path.isfile(p):
        return [], 0
    try:
        text = read_text(p)
    except OSError:
        return [], 0
    rel = "project-state/记忆/知识沉淀.md"
    items = []
    seen_ids = set()
    unparsed = 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = KHEAD_RE.match(s)
        if m:
            kid, title = m.group(1), m.group(2).strip()
            if kid not in seen_ids:
                seen_ids.add(kid)
                items.append({"id": kid, "title": title, "evidence": rel})
            continue
        if s.startswith("|"):
            cells = split_cells(s, True)
            if not cells or is_sep_row(cells):
                continue
            if cells[0].lower() in ("id", "编号", "标识"):
                continue
            for cell in cells:
                if KID_RE.match(cell):
                    idx = cells.index(cell)
                    title = cells[idx + 1] if idx + 1 < len(cells) else ""
                    if cell not in seen_ids:
                        seen_ids.add(cell)
                        items.append({"id": cell, "title": title, "evidence": rel})
                    break
            continue
        if s[:1] in ("#", "-", "*", "+") and re.search(r"\bk-[0-9A-Za-z]", s):
            unparsed += 1
    return items[:limit], unparsed


def read_contracts(proj, limit):
    """契约清单：`project-state/契约/` 下所有文件名（含子目录相对路径，只要名字）。"""
    cdir = os.path.join(proj, "project-state", "契约")
    if not os.path.isdir(cdir):
        return []
    names = []
    for dirpath, dirnames, filenames in os.walk(cdir):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            relp = os.path.relpath(full, cdir).replace(os.sep, "/")
            names.append(relp)
    names.sort()
    return names[:limit]


def read_modules(proj, limit):
    """代码模块：`repos/` 下一层目录名 + 每模块文件数（封顶 2000+）。"""
    rdir = os.path.join(proj, "repos")
    if not os.path.isdir(rdir):
        return []
    items = []
    names = [n for n in listdir(rdir) if os.path.isdir(os.path.join(rdir, n))][:limit]
    for name in names:
        items.append({"name": name, "files": count_files(os.path.join(rdir, name))})
    return items


def read_local_scripts(proj, limit):
    """项目自建脚本：`scripts/` 下不属于框架既有脚本清单的 `.py`。"""
    sdir = os.path.join(proj, "scripts")
    if not os.path.isdir(sdir):
        return []
    names = []
    for fn in listdir(sdir):
        if not fn.endswith(".py"):
            continue
        if os.path.splitext(fn)[0] in FRAMEWORK_SCRIPTS:
            continue
        names.append(fn)
    return names[:limit]


def read_history_ledger(proj):
    """历史项目自己的 `machine:reuse` 行——「链式复用」证据，可直接继承。"""
    p = os.path.join(proj, LEDGER_REL)
    if not os.path.isfile(p):
        return []
    try:
        lines = read_text(p).splitlines()
    except OSError:
        return []
    body, _, _ = find_reuse_block(lines)
    if not body:
        return []
    header, rows = parse_table(body)
    if not header:
        return []
    out_rows = []
    for lineno, cells in rows:
        row = {"line": lineno}
        for i, key in enumerate(header):
            row[key] = cells[i] if i < len(cells) else ""
        out_rows.append(row)
    return out_rows


def extract_candidate(path, limit):
    """抽取一个候选历史项目的元信息与六类资产（**只读**）。"""
    cfg, _err = load_json(os.path.join(path, CONFIG_REL))
    cfg = cfg or {}
    name = (cfg.get("project_name") or "").strip() or os.path.basename(path.rstrip("\\/"))
    ver = frontmatter_version(os.path.join(path, "agent.md"))
    last = "unknown"
    cfg_path = os.path.join(path, CONFIG_REL)
    if os.path.isfile(cfg_path):
        try:
            last = datetime.date.fromtimestamp(os.path.getmtime(cfg_path)).isoformat()
        except OSError:
            last = "unknown"
    cards = read_cards(path, limit)
    tests = read_tests(path, limit)
    knowledge, unparsed = read_knowledge(path, limit)
    contracts = read_contracts(path, limit)
    modules = read_modules(path, limit)
    scripts = read_local_scripts(path, limit)
    cand = {
        "path": path,
        "name": name,
        "framework_version": ver,
        "mode": (cfg.get("mode") or "unknown"),
        "last_activity": last,
        "score": 0.0,
        "matched_terms": [],
        "counts": {
            "cards": len(cards),
            "tests": len(tests),
            "knowledge": len(knowledge),
            "contracts": len(contracts),
            "modules": len(modules),
            "scripts": len(scripts),
        },
        "knowledge_unparsed": unparsed,
        "cards": cards,
        "tests": tests,
        "knowledge": knowledge,
        "reuse_ledger": read_history_ledger(path),
        "contracts": contracts,
        "modules": modules,
        "local_scripts": scripts,
        "_description": (cfg.get("description") or ""),
        "design_profile": (cfg.get("design_profile") or ""),
    }
    return cand


# ---------------------------------------------------------------- 台账与声明体检
def read_ledger(base, rep):
    """读当前项目的复用台账 → {path, exists, rows, ids}；顺带产出台账类发现项。"""
    path = os.path.join(base, LEDGER_REL)
    rel = "project-state/记忆/复用台账.md"
    info = {"path": rel, "exists": False, "rows": [], "ids": set(), "verdicts": {}}
    if not os.path.isfile(path):
        rep.add("REUSE-LEDGER-MISSING", "WARN",
                "本项目还没有 `project-state/记忆/复用台账.md`：三角色的"
                "「复用 / 改造复用 / 不复用」判定与证据无处落账，无法核查。"
                "请在首次复用评估后建这份台账（含 machine:reuse 机读块）。", rel)
        return info
    info["exists"] = True
    try:
        lines = read_text(path).splitlines()
    except OSError as exc:
        rep.add("REUSE-LEDGER-HEADER", "ERROR",
                "复用台账读不了（%s），无法核对机读块。" % exc, rel)
        return info

    body, closed, style = find_reuse_block(lines)
    if body is None:
        rep.add("REUSE-LEDGER-HEADER", "ERROR",
                "复用台账里找不到 `machine:reuse` 机读块：既没有 `%s` … `%s` 围栏，"
                "也没有 ```machine:reuse 围栏。列头必须是这 8 列且顺序一致：%s"
                % (START, END, CANON_HEADER), rel)
        return info
    if not closed:
        rep.add("REUSE-LEDGER-HEADER", "ERROR",
                "`machine:reuse` 机读块没有闭合围栏（%s / ```），整块作废。"
                "列头必须是这 8 列且顺序一致：%s"
                % (END if style == "html" else "```", CANON_HEADER), rel)
        return info

    header, rows = parse_table(body)
    if header != CANON:
        rep.add("REUSE-LEDGER-HEADER", "ERROR",
                "复用台账 machine:reuse 列头不是约定的 8 列（应**恰好**为：%s；"
                "实际：%s）。列头是机器的接口，改名、缺列、换序都会让台账核不了。"
                % (CANON_HEADER, " | ".join(header or []) or "（空）"), rel)
        return info

    parsed = []
    prev_id = ""
    for lineno, cells in rows:
        row = {k: (cells[i] if i < len(cells) else "") for i, k in enumerate(CANON)}
        rid = row["id"]
        if not ID_RE.match(rid):
            rep.add("REUSE-LEDGER-ID", "ERROR",
                    "第 %d 行 id `%s` 不合规：必须是 `RU-NNN`（三位数字，如 RU-001）。"
                    % (lineno, rid), rel)
        elif rid in info["ids"]:
            rep.add("REUSE-LEDGER-ID", "ERROR",
                    "第 %d 行 id `%s` 重复：台账 id 必须唯一（重复 id 会让需求卡的"
                    "reuse_ref 指到两条记录上）。" % (lineno, rid), rel)
        elif prev_id and rid <= prev_id:
            rep.add("REUSE-LEDGER-ID", "ERROR",
                    "第 %d 行 id `%s` 没有严格升序（上一行是 %s）：台账按 id 升序排，"
                    "便于人工核对与 diff。" % (lineno, rid, prev_id), rel)
        if ID_RE.match(rid) and rid not in info["ids"]:
            info["ids"].add(rid)
            prev_id = rid

        if len(cells) != len(CANON):
            rep.add("REUSE-LEDGER-ROW", "ERROR",
                    "第 %d 行有 %d 列，应为 %d 列（%s）。缺列会让 target/evidence "
                    "悄悄错位，是台账最常见的朽格式。"
                    % (lineno, len(cells), len(CANON), CANON_HEADER), rel)
            parsed.append(row)
            continue

        kind, verdict, target, evidence = row["kind"], row["verdict"], row["target"], row["evidence"]
        if ID_RE.match(rid):
            info["verdicts"][rid] = verdict
        if kind not in KINDS:
            rep.add("REUSE-LEDGER-ROW", "ERROR",
                    "第 %d 行 kind `%s` 不在允许集合 {%s} 内。" % (lineno, kind, "、".join(KINDS)), rel)
        if verdict not in VERDICTS:
            rep.add("REUSE-LEDGER-ROW", "ERROR",
                    "第 %d 行 verdict `%s` 不在允许集合 {%s} 内。" % (lineno, verdict, "/".join(VERDICTS)), rel)
        elif verdict == "reject":
            if target != "-":
                rep.add("REUSE-LEDGER-ROW", "ERROR",
                        "第 %d 行 verdict=reject 时 target 必须是 `-`（不复用没有目标），"
                        "实际是 `%s`。" % (lineno, target), rel)
            if not evidence.strip():
                rep.add("REUSE-LEDGER-ROW", "ERROR",
                        "第 %d 行 verdict=reject 时 evidence 必须非空：不复用的理由"
                        "（哪里不适用）同样是证据。" % lineno, rel)
        else:
            if not target.strip() or target.strip() == "-":
                rep.add("REUSE-LEDGER-ROW", "ERROR",
                        "第 %d 行 verdict=%s 时 target 必须非空且不为 `-`（要落到本项目的"
                        "哪个需求卡/契约/模块），实际是 `%s`。" % (lineno, verdict, target), rel)
            if not evidence.strip():
                rep.add("REUSE-LEDGER-ROW", "ERROR",
                        "第 %d 行 verdict=%s 时 evidence 必须非空（来源与判定依据）。"
                        % (lineno, verdict), rel)
        parsed.append(row)
    info["rows"] = parsed
    return info


def check_current_cards(base, rep, ledger):
    """当前项目的需求卡：`reuse` / `reuse_ref` 与台账对得上吗。"""
    ledger_ids = ledger.get("ids") or set()
    ledger_verdicts = ledger.get("verdicts") or {}
    cdir = os.path.join(base, "project-state", "需求卡")
    if not os.path.isdir(cdir):
        return 0
    n = 0
    for name in listdir(cdir):
        if not CARD_DIR_RE.match(name):
            continue
        p = os.path.join(cdir, name, "card.md")
        if not os.path.isfile(p):
            continue
        n += 1
        rel = "project-state/需求卡/%s/card.md" % name
        try:
            text = read_text(p)
        except OSError:
            continue
        fm, _ = parse_frontmatter(text)
        fm = fm or {}
        if "reuse" not in fm:
            rep.add("REUSE-CARD-FIELD-MISSING", "WARN",
                    "需求卡 %s 的 frontmatter 缺 `reuse` 键：先例复用评估是开工前置，"
                    "每张新卡都要写 `reuse: new|reuse|adapt`（复用/改造复用时还要写 "
                    "`reuse_ref: RU-NNN`）。" % name, rel)
            continue
        val = fm["reuse"][0].strip()
        if val not in ("new", "reuse", "adapt"):
            rep.add("REUSE-CARD-FIELD-VALUE", "ERROR",
                    "需求卡 %s 的 `reuse: %s` 非法：只能取 `new` / `reuse` / `adapt`。"
                    % (name, val), rel)
            continue
        ref = (fm.get("reuse_ref", ("", 0))[0] or "").strip()
        if val == "new":
            if ID_RE.match(ref):
                rep.add("REUSE-CARD-LEDGER-MISMATCH", "ERROR",
                        "需求卡 %s 写 `reuse: new`（新造）却带着 `reuse_ref: %s`：new 表示"
                        "没有采用任何先例，`reuse_ref` 必须是 `-`——两者同时出现，说明卡与"
                        "台账不是同一次判定的结果。" % (name, ref), rel)
            continue
        if not ID_RE.match(ref):
            rep.add("REUSE-CARD-REF-DANGLING", "ERROR",
                    "需求卡 %s 声明 `reuse: %s`，但 `reuse_ref` 是 `%s`：复用/改造复用"
                    "必须指向台账 id（`RU-NNN`），否则无从核查复用依据。"
                    % (name, val, ref or "（空）"), rel)
        elif ref not in ledger_ids:
            rep.add("REUSE-CARD-REF-DANGLING", "ERROR",
                    "需求卡 %s 的 `reuse_ref: %s` 在 `复用台账.md` 的 machine:reuse "
                    "块里不存在（悬空引用）。" % (name, ref), rel)
        else:
            lv = ledger_verdicts.get(ref, "")
            if lv and lv != val:
                rep.add("REUSE-CARD-LEDGER-MISMATCH", "ERROR",
                        "需求卡 %s 写 `reuse: %s`，但它引用的台账行 `%s` 的 verdict 是 "
                        "`%s`：卡的判定必须与台账同口径（先有台账行、后有卡字段；要改判定"
                        "就两处一起改，不能只改一处）。" % (name, val, ref, lv), rel)
    return n


# ---------------------------------------------------------------- 主流程
def norm(p):
    return os.path.normpath(os.path.abspath(p))


def expand_root(value):
    v = (value or "").strip()
    if not v:
        return ""
    v = os.path.expanduser(v)
    v = os.path.expandvars(v)
    return v


def split_root_values(values):
    """`--roots` 的值里允许用 `;` 分隔多个根。"""
    roots = []
    for v in values or []:
        for piece in (v or "").split(";"):
            piece = piece.strip()
            if piece:
                roots.append(piece)
    return roots


def declared_roots(args, cfg):
    """命令行优先，其次 config 的 reuse_roots。"""
    if args.roots:
        return split_root_values(args.roots), "命令行"
    raw = cfg.get("reuse_roots")
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()], "config"
    return [], "config"


def build_parser():
    ap = argparse.ArgumentParser(
        prog="reuse_scan.py",
        description="v5.13.0 先例复用评估机器核：只读扫描 reuse_roots 下的历史项目，"
                    "列出可复用资产候选（不判定复用价值）。",
        epilog="退出码：0 完成且无 ERROR ｜ 1 有 ERROR ｜ 2 无法检查（**不是「没问题」**）",
    )
    ap.add_argument("root", nargs="?", default=".",
                    help="项目根（含 project-state/ 的目录），缺省为当前目录")
    ap.add_argument("--roots", action="append", default=None, metavar="R",
                    help="覆盖 config 的 reuse_roots；可重复传参，单个值内可用 ; 分隔多个根")
    ap.add_argument("--keyword", action="append", default=None, metavar="K",
                    help="追加查询关键词；可重复。缺省时自动取项目名 + description + 现有需求卡标题")
    ap.add_argument("--limit", type=int, default=200, metavar="N",
                    help="每个历史项目每类资产最多抽取的条目数（默认 200）")
    ap.add_argument("--depth", type=int, default=3, metavar="N",
                    help="在每个声明的根下向下搜索候选项目的最大层数（默认 3）")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON（不再打人读正文）")
    ap.add_argument("--write", action="store_true",
                    help="另写人读报告 project-state/复用盘点-<YYYYMMDD>.md（同日重复运行自动 -2/-3）")
    ap.add_argument("--check", action="store_true",
                    help="只做「声明与台账体检」，不扫历史项目（快）")
    ap.add_argument("--quiet", action="store_true", help="只打汇总与最终判据行")
    return ap


def final_line(status, rep, n_roots, n_projects):
    return "REUSE-SCAN: %s findings=ERROR:%d WARN:%d INFO:%d roots:%d projects:%d" % (
        status, rep.counts["ERROR"], rep.counts["WARN"], rep.counts["INFO"], n_roots, n_projects)


def pick_report_path(base, day):
    """同日重复运行自动改名 -2、-3……**绝不覆盖已存在的报告**。"""
    d = os.path.join(base, "project-state")
    n = 1
    while True:
        suffix = "" if n == 1 else "-%d" % n
        p = os.path.join(d, "复用盘点-%s%s.md" % (day, suffix))
        if not os.path.exists(p):
            return p
        n += 1


def build_report(base, args, cfg, proj_name, proj_ver, roots_info, candidates,
                 ledger, rep, query, query_terms, generated_at, check_only):
    day = datetime.date.today().strftime("%Y%m%d")
    L = []
    L.append("# 先例复用盘点-%s（v%s）" % (day, VERSION))
    L.append("")
    L.append("- 项目根：`%s`" % base)
    L.append("- 项目名：%s" % (proj_name or "（config 里 project_name 为空）"))
    L.append("- 框架版本：%s" % proj_ver)
    L.append("- 生成时间：%s" % generated_at)
    L.append("- 生成命令：`%s`" % " ".join(args._argv))
    L.append("- 查询面词元：%d 个（来源：%s）" % (len(query_terms), query))
    L.append("")
    L.append("## ① 扫描范围（声明的根）")
    L.append("")
    L.append("| 声明的根 | 解析后 | 是否存在 | 扫到的候选项目 |")
    L.append("| --- | --- | --- | --- |")
    if roots_info:
        for r in roots_info:
            L.append("| `%s` | `%s` | %s | %d |"
                     % (r["declared"], r["resolved"], "是" if r["exists"] else "**否**", r["projects"]))
    else:
        L.append("| （无） | - | - | - |")
    L.append("")
    if check_only:
        L.append("本次为 `--check`：只做「声明与台账体检」，**没有扫描历史项目**。")
        L.append("")
    L.append("## ② 候选项目总表（按相似度降序）")
    L.append("")
    L.append("| 相似度 | 项目 | 模式 | 框架版本 | 最后活动 | 需求卡 | 测试卡 | 知识沉淀 | 契约 | 代码模块 | 自建脚本 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    if candidates:
        for c in candidates:
            k = c["counts"]
            L.append("| %s | %s | %s | %s | %s | %d | %d | %d | %d | %d | %d |"
                     % (c["score"], c["name"], c["mode"], c["framework_version"], c["last_activity"],
                        k["cards"], k["tests"], k["knowledge"], k["contracts"], k["modules"], k["scripts"]))
    else:
        L.append("| - | （无候选） | - | - | - | - | - | - | - | - | - |")
    L.append("")
    L.append("## ③ 候选资产明细（按相似度降序）")
    L.append("")
    if not candidates:
        L.append("没有候选历史项目。**这与「没有历史项目」不是一回事**——先看上面的根是否存在。")
        L.append("")
    for i, c in enumerate(candidates, 1):
        L.append("### %d. %s（相似度 %s ｜ `%s`）" % (i, c["name"], c["score"], c["path"]))
        L.append("")
        L.append("- 模式：%s ｜ 框架版本：%s ｜ 最后活动：%s"
                 % (c["mode"], c["framework_version"], c["last_activity"]))
        L.append("- 命中词：%s" % ("、".join(c["matched_terms"]) if c["matched_terms"] else "（无）"))
        if c.get("knowledge_unparsed"):
            L.append("- 知识沉淀里有 %d 条**没解析出来**（格式不在这两种之内：`### k-001 标题`、表格行）。"
                     % c["knowledge_unparsed"])
        L.append("- 需求卡（%d）" % len(c["cards"]))
        for x in c["cards"]:
            L.append("  - %s 《%s》（状态 %s ｜ reuse %s%s）— `%s`"
                     % (x["id"], x["title"], x["status"] or "-", x["reuse"] or "-",
                        "→%s" % x["reuse_ref"] if x["reuse_ref"] != "-" else "",
                        x["evidence"]))
        L.append("- 测试卡（%d）" % len(c["tests"]))
        for x in c["tests"]:
            L.append("  - %s 《%s》（状态 %s）— `%s`" % (x["id"], x["title"], x["status"] or "-", x["evidence"]))
        L.append("- 知识沉淀（%d）" % len(c["knowledge"]))
        for x in c["knowledge"]:
            L.append("  - %s %s — `%s`" % (x["id"], x["title"] or "（无标题）", x["evidence"]))
        L.append("- 契约（%d）" % len(c["contracts"]))
        for x in c["contracts"]:
            L.append("  - `project-state/契约/%s`" % x)
        L.append("- 代码模块（%d）" % len(c["modules"]))
        for x in c["modules"]:
            L.append("  - `repos/%s`（文件数 %s）" % (x["name"], x["files"]))
        L.append("- 项目自建脚本（%d，框架既有脚本不计）" % len(c["local_scripts"]))
        for x in c["local_scripts"]:
            L.append("  - `scripts/%s`" % x)
        if c["reuse_ledger"]:
            L.append("- 它自己的复用台账 machine:reuse 行（%d 行，链式复用证据，可直接继承）"
                     % len(c["reuse_ledger"]))
            for row in c["reuse_ledger"][:20]:
                L.append("  - %s ｜ %s ｜ %s ｜ %s"
                         % (row.get("id", ""), row.get("kind", ""), row.get("verdict", ""),
                            row.get("evidence", "")))
        L.append("")
    L.append("## ④ 交给三角色的三个问题")
    L.append("")
    L.append("- **产品经理（需求侧）**：历史项目里哪几张需求卡的验收口径可以直接搬？"
             "搬之前哪条业务前提已经变了（用户、渠道、合规口径）？")
    L.append("- **架构师（技术侧）**：哪些契约 / 组件 / 代码模块能直接复用或改造复用？"
             "接口是否兼容、迁移成本与重写成本各是多少？")
    L.append("- **项目经理（记账）**：每个候选的判定（`reuse` / `adapt` / `reject`）与证据"
             "是否已经落进 `project-state/记忆/复用台账.md` 的 machine:reuse 块，"
             "并在新需求卡 frontmatter 写了 `reuse` / `reuse_ref`？")
    L.append("")
    L.append("## ⑤ 机读 JSON")
    L.append("")
    L.append("```")
    L.append("python -X utf8 scripts/reuse_scan.py %s --roots \"...\" --json" % base)
    L.append("```")
    L.append("")
    L.append("## ⑥ 诚实边界")
    L.append("")
    for note in SCOPE_NOTES:
        L.append("- %s" % note)
    L.append("- 报告日期同名时**绝不覆盖**：第二次运行会写成 `-2`、`-3`。")
    L.append("")
    return day, "\n".join(L) + "\n"


def main(argv=None):
    args = build_parser().parse_args(argv)
    args._argv = ([os.path.basename(sys.argv[0] or "reuse_scan.py")] + list(argv)) \
        if argv is not None else list(sys.argv)
    base = norm(args.root or ".")
    generated_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    rep = Report()

    # ---- 三态之二：无法检查（**不是「没问题」**） ----
    if not os.path.isdir(base):
        out("== reuse_scan.py v%s ==" % VERSION)
        out("[错误] 项目根不存在：%s" % base)
        out("")
        out("退出码 2 —— **无法检查，不是「没问题」**")
        out(final_line("CANNOT-CHECK", rep, 0, 0))
        return 2
    if not os.path.isdir(os.path.join(base, "project-state")):
        out("== reuse_scan.py v%s ==" % VERSION)
        out("[错误] %s 下没有 project-state/（不像框架工作区）" % base)
        out("")
        out("退出码 2 —— **无法检查，不是「没问题」**")
        out(final_line("CANNOT-CHECK", rep, 0, 0))
        return 2
    cfg, cfg_err = load_json(os.path.join(base, CONFIG_REL))
    if cfg is None:
        out("== reuse_scan.py v%s ==" % VERSION)
        out("[错误] %s 读不了或解析失败：%s" % (CONFIG_REL, cfg_err))
        out("")
        out("退出码 2 —— **无法检查，不是「没问题」**")
        out(final_line("CANNOT-CHECK", rep, 0, 0))
        return 2

    proj_name = (cfg.get("project_name") or "").strip()
    proj_ver = frontmatter_version(os.path.join(base, "agent.md"))

    # ---- 声明体检：reuse_roots（「没问过」≠「没有历史项目」） ----
    has_key = "reuse_roots" in cfg
    raw_roots = cfg.get("reuse_roots")
    if not has_key:
        rep.add("REUSE-ROOTS-MISSING", "ERROR",
                "`project-state/config.json` 里没有 `reuse_roots` 键：**没有问过用户"
                "「以前有没有做过类似的东西、历史项目放在哪些根目录」**，"
                "这与「没有历史项目」不是一回事。修复：问过用户后把答案写进 "
                "`reuse_roots`（字符串数组，允许 `~` 与绝对/相对路径）；确实没有历史项目时"
                "写显式 `[]`，并在项目计划里注明判断依据。",
                CONFIG_REL.replace(os.sep, "/"))
    elif isinstance(raw_roots, list) and not raw_roots:
        rep.add("REUSE-ROOTS-EMPTY", "INFO",
                "`reuse_roots` 是显式 `[]`：用户确认过没有历史项目，本次豁免（合法）。",
                CONFIG_REL.replace(os.sep, "/"))

    roots_declared, roots_source = declared_roots(args, cfg)
    # 声明根 == 当前项目根：跳过并记 INFO（扫自己等于自我循环）
    self_real = os.path.realpath(base)
    roots_info = []
    active_roots = []
    for r in roots_declared:
        resolved = norm(expand_root(r))
        exists = os.path.isdir(resolved)
        info = {"declared": r, "resolved": resolved, "exists": exists, "projects": 0, "active": False}
        roots_info.append(info)
        if exists and os.path.realpath(resolved) == self_real:
            rep.add("REUSE-SELF-ROOT", "INFO",
                    "声明的根 `%s` 就是当前项目根本身，已跳过（不扫自己）。" % resolved, resolved)
            continue
        if not exists:
            rep.add("REUSE-ROOT-MISSING", "WARN",
                    "声明的根在本机不存在：`%s`（NAS 可能没挂载/盘符变了）。"
                    "**这不等于「没有历史项目」**——先确认根挂上了再下结论。" % resolved, resolved)
            continue
        info["active"] = True
        active_roots.append((r, resolved, info))

    # ---- 台账体检 + 当前项目卡体检 ----
    ledger = read_ledger(base, rep)
    n_cur_cards = check_current_cards(base, rep, ledger)

    # ---- 查询面（集合：--keyword 是**追加**，不替换项目自身的上下文） ----
    query_terms = set()
    if proj_name:
        add_query(query_terms, proj_name)
    add_query(query_terms, cfg.get("description") or "")
    cur_cards = read_cards(base, args.limit)
    for c in cur_cards:
        add_query(query_terms, c["title"])
    if args.keyword:
        for k in args.keyword:
            add_query(query_terms, k)
        query_src = "命令行 --keyword" if not cur_cards and not proj_name else "命令行 --keyword + 项目名/description + 现有需求卡标题"
    else:
        query_src = "自动（项目名 + description + 现有需求卡标题）"

    # ---- 扫历史（--check 不扫） ----
    candidates = []
    n_projects = 0
    seen_real = {}
    self_skipped = []
    if not args.check:
        for declared, resolved, rinfo in active_roots:
            found = walk_candidates(resolved, max(0, args.depth), {self_real})
            for path, is_self in found:
                real = os.path.realpath(path)
                if is_self or real == self_real:
                    if path not in self_skipped:
                        self_skipped.append(path)
                        rep.add("REUSE-SELF-ROOT", "INFO",
                                "扫描时遇到当前项目根 `%s`，已跳过（不把本项目当历史项目）。" % path, path)
                    continue
                rinfo["projects"] += 1     # 本根覆盖到的候选数（含已被别的根先扫到的）
                if real in seen_real:
                    continue               # 同一真实路径只算一次（candidates / n_projects 去重）
                seen_real[real] = True
                n_projects += 1
                candidates.append(extract_candidate(path, args.limit))
        for r in roots_info:
            if r["active"] and r["projects"] == 0:
                rep.add("REUSE-ROOT-NO-PROJECT", "WARN",
                        "声明的根 `%s` 存在，但 %d 层内没扫到任何候选项目"
                        "（候选项目 = 同时含 `agent.md` 与 `project-state/` 的目录）。"
                        "先确认路径写对了、深度够不够，再谈「没有可复用的东西」。"
                        % (r["resolved"], args.depth), r["resolved"])
        for c in candidates:
            score, terms = similarity(query_terms, candidate_terms(c))
            c["score"] = score
            c["matched_terms"] = terms
        candidates.sort(key=lambda c: (-c["score"], c["path"]))

    # ---- 输出 ----
    exit_code = 1 if rep.counts["ERROR"] else 0
    status = "ERROR" if exit_code == 1 else "OK"
    findings_json = rep.sorted_items()
    counts_json = {
        "roots": len([r for r in roots_info if r["exists"]]),
        "projects_scanned": n_projects,
        "candidates": len(candidates),
        "findings": len(findings_json),
    }
    if args.json:
        payload = {
            "schema": SCHEMA,
            "generated_at": generated_at,
            "project": {"root": base, "name": proj_name, "framework_version": proj_ver},
            "roots": [{"declared": r["declared"], "resolved": r["resolved"],
                       "exists": r["exists"], "projects": r["projects"]} for r in roots_info],
            "candidates": [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates],
            "ledger": {"path": ledger["path"], "exists": ledger["exists"], "rows": len(ledger["rows"])},
            "findings": findings_json,
            "counts": counts_json,
        }
        out(json.dumps(payload, ensure_ascii=False, indent=2))
        out(final_line(status, rep, counts_json["roots"], n_projects))
        return exit_code

    report_rel = ""
    if args.write:
        day, text = build_report(base, args, cfg, proj_name, proj_ver, roots_info,
                                 candidates, ledger, rep, query_src, query_terms,
                                 generated_at, args.check)
        rp = pick_report_path(base, day)
        try:
            with open(rp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            report_rel = os.path.relpath(rp, base).replace(os.sep, "/")
        except OSError as exc:
            report_rel = ""
            sys.stderr.write("[警告] 报告写不了（%s）：%s\n" % (rp, exc))

    if args.quiet:
        out("reuse_scan：ERROR %d / WARN %d / INFO %d（根 %d ｜ 候选项目 %d%s）"
            % (rep.counts["ERROR"], rep.counts["WARN"], rep.counts["INFO"],
               counts_json["roots"], len(candidates),
               " ｜ 报告 %s" % report_rel if report_rel else ""))
        out(final_line(status, rep, counts_json["roots"], n_projects))
        return exit_code

    out("== reuse_scan.py v%s ==" % VERSION)
    out("项目根：%s" % base)
    out("项目名：%s ｜ 框架版本：%s" % (proj_name or "（空）", proj_ver))
    out("查询面：%s 词元 %d 个 ｜ 根来源：%s" % (query_src, len(query_terms), roots_source))
    if args.check:
        out("模式：--check（只做声明与台账体检，**不扫历史项目**）")
    out("")
    out("[声明的根]")
    if roots_info:
        for r in roots_info:
            out("  %s  %s  候选 %d" % ("存在" if r["exists"] else "**不存在**", r["resolved"], r["projects"]))
    else:
        out("  （一个都没声明）")
    out("")
    if not args.check:
        out("[候选历史项目 按相似度降序]")
        if candidates:
            out("  %-8s %-28s %-7s %-9s %-11s %5s %5s %5s %5s %5s %5s"
                % ("相似度", "项目", "模式", "框架版本", "最后活动",
                   "需求卡", "测试卡", "知识", "契约", "模块", "脚本"))
            for c in candidates:
                k = c["counts"]
                out("  %-8s %-28s %-7s %-9s %-11s %5d %5d %5d %5d %5d %5d"
                    % (c["score"], c["name"][:28], c["mode"][:7], c["framework_version"][:9],
                       c["last_activity"], k["cards"], k["tests"], k["knowledge"],
                       k["contracts"], k["modules"], k["scripts"]))
        else:
            out("  （没有候选——先看上一条：根是不是不存在 / 路径写错了）")
        out("")
    out("[发现项]")
    if findings_json:
        for it in findings_json:
            out("  [%s] %-24s %s" % (SEV_TAG[it["level"]], it["code"], it["path"]))
            out("      %s" % it["message"])
    else:
        out("  无：根声明齐全，台账与需求卡对得上。")
    out("")
    out("复用台账：%s（存在：%s ｜ 数据行：%d）｜ 当前项目需求卡：%d 张"
        % (ledger["path"], "是" if ledger["exists"] else "否", len(ledger["rows"]), n_cur_cards))
    if report_rel:
        out("报告：%s" % report_rel)
    out("")
    out("reuse_scan：ERROR %d / WARN %d / INFO %d（根 %d ｜ 候选项目 %d）"
        % (rep.counts["ERROR"], rep.counts["WARN"], rep.counts["INFO"],
           counts_json["roots"], len(candidates)))
    if exit_code == 1:
        out("退出码 1 —— 有 ERROR：先把上面每一条修掉，再谈复用判定"
            "（缺 reuse_roots 就回去问用户，台账列头/行/悬空引用就回去补账）。")
    else:
        out("退出码 0 —— 完成且无 ERROR。**这不等于「该复用的都复用了」**："
            "复用价值由项目经理/产品经理/架构师判定，见下方边界。")
    out("")
    for note in SCOPE_NOTES:
        out("- %s" % note)
    out("")
    out(final_line(status, rep, counts_json["roots"], n_projects))
    return exit_code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
