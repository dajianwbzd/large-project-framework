#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mutation_coverage_audit.py — 「变更后一致性」覆盖矩阵的只读检查器（v5.23.0）

为什么有这个脚本
----------------
「变更后一致性」测的是**事情发生之后系统还对不对**：权限被回收了、归属被改到别的组织了、
分享链接时效到期了、派生数据该重算了——**变更本身走通了不等于变更之后全对**。
真正会漏的是「列表刷新了、详情还是旧权限」「分享链接还能直接打开」「导出文件里还是旧归属」
「统计没重算」这些**变更后残留**。

v5.23.0 之前，这类检查**没有任何落纸的覆盖清单**：一张测试卡说「测了权限回收」，
机器无从知道它到底看了哪几个面（列表/详情/直达链接/导出/搜索/通知/统计），
也无从知道**没测的那几个面是「不适用」还是「忘掉了」**——两者在文档里长得一模一样。
把它写成机读块（`machine:mutation`，七列）放进 `测试卡/tc-NNN/变更矩阵.md`，
机器才有东西可查：本脚本就是那个查看器。

三条纪律（本框架踩过的坑，逐条有先例：`case_audit.py` v5.8.0 / `test_scope_audit.py` v5.9.0
/ `browser_resource_audit.py` v5.11.0）
  ① **「有问题」与「查不了」必须分开**：项目根/`测试卡/`/`tc-*` 一个都没有，
     或**全部测试卡都没有 `变更矩阵.md`** → 退出码 **2**、`ok=null`，
     **不是「没问题」**（同 `recall.py` 的退出码纪律）。
  ② **格式旧 ≠ 内容缺**：机读块存在但列头不是那 7 列 → 整块**只报一次**
     `MUT-FORMAT-STALE`（WARN）并**跳过该块的行级检查**——
     升级当天所有老卡一起变红，真检测力反而被淹没。
  ③ **`unknown` 不是「通过」**：`result: unknown` 是「还没回填」，报 ERROR；
     机器只核「声明与自相矛盾」，**核不了「这些面是不是真的测了」**——
     `covered` 是一句自述，不是系统一致性的事实（同「证据自述」口径）。

判定口径（一句话）
------------------
同一 `object` + `event` 组合下，**七面要么被某个 `faces` 覆盖，要么有一行显式写 `faces=na`**；
`result` 回填 `covered` 就必须指到一个真实存在的用例（`CAS-NNN`）；
`partial` / `na` / `accepted` 必须写清原因——**说不清的「不适用」就是漏测的形状**。

用法
----
    python -X utf8 scripts/mutation_coverage_audit.py <项目根>            # 只读体检
    python -X utf8 scripts/mutation_coverage_audit.py <项目根> --json     # 机器可读
    python -X utf8 scripts/mutation_coverage_audit.py <项目根> --check    # 只印计数（门禁用）
    python -X utf8 scripts/mutation_coverage_audit.py --checks            # 列出全部检查码

    退出码：0 干净（允许 WARN）｜ 1 有 ERROR ｜ 2 无法检查（**无法检查 ≠ 通过**，
    **不是「没问题」**）

数据来源
--------
    project-state/测试卡/tc-NNN/tc.md          frontmatter 的 `combo_budget`
    project-state/测试卡/tc-NNN/变更矩阵.md    `machine:mutation` 机读块（七列）
    project-state/代码/测试/用例库.md          `CAS-NNN` 的落点（`cas` 悬空的判据）

    `machine:mutation` 块两种围栏写法都认（同一份数据，不因围栏长什么样而「看不见」）：
    ① `<!-- machine:mutation -->` … `<!-- /machine:mutation -->`（框架统一写法）
    ② ``` 围栏，且围栏内**首行**就是 `machine:mutation`

JSON 结构（`--json`，供 agent / 门禁读）
----------------------------------------
    {"version", "base", "cards", "matrices", "rows", "ok", "exit",
     "errors": [...], "warns": [...], "infos": [...], "counts", "details", "items"}
    `cards` / `matrices` / `rows` 是计数；`details` 是逐行解析结果；`items` 是全部条目。
"""

import json
import os
import re
import sys

VERSION = "5.23.0"

# ---------------------------------------------------------------- 契约常量
# 机读块列头：**一个字都不能差**（7 列）
CANON = ["id", "object", "event", "faces", "cas", "result", "reason"]
CANON_HEADER = "id | object | event | faces | cas | result | reason"

# 变更的四子族（event）：只有这四类，别的一律不认
EVENTS = ["权限与角色回收", "归属与组织变更", "时效到期", "派生数据重算"]

# 变更之后要逐个看过去的七面（faces）
FACES = ["列表", "详情", "直达链接", "导出", "搜索", "通知", "统计"]

# 覆盖结果五选一：unknown 是「还没回填」，**不是通过**
RESULTS = ["covered", "partial", "na", "accepted", "unknown"]
# 这三档必须写原因（写了 `-` / `无` / `n/a` 等于没写）
REASON_REQUIRED = ("partial", "na", "accepted")

MUT_START = "<!-- machine:mutation -->"
MUT_END = "<!-- /machine:mutation -->"
FENCE_TAG = "machine:mutation"

MATRIX_NAME = "变更矩阵.md"
TC_NAME = "tc.md"
TC_FOLDER = "测试卡"
CASLIB_PARTS = ("代码", "测试", "用例库.md")
BUDGET_KEY = "combo_budget"

# 「空 / 占位」的写法（同 case_audit.py：不把这些当内容）
EMPTY_OK = {"", "-", "--"}
# 「原因」格里的空话：写了等于没写
REASON_EMPTY = {"", "-", "--", "无", "n/a", "na", "none", "tbd", "todo", "待定"}

SEV_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
SEV_LABEL = {"ERROR": "错误", "WARN": "告警", "INFO": "提示"}

ID_RE = re.compile(r"^MU-\d{3}$")
NUM_RE = re.compile(r"MU-(\d+)")
CAS_ANY_RE = re.compile(r"CAS-\d{3,}")
TC_DIR_RE = re.compile(r"^tc-\d+$")
FACE_SPLIT_RE = re.compile(r"[,，、]")

# 检查码表（`--checks` 原样打印；报告里只用这些码）
CHECKS = [
    ("MUT-EVENT-UNKNOWN", "ERROR",
     "event 不在四子族里（权限与角色回收 / 归属与组织变更 / 时效到期 / 派生数据重算）"),
    ("MUT-FACE-UNKNOWN", "ERROR",
     "faces 里出现七面之外的名字（含 `all` 拼写错误：`al` / `ALL` 都不算 `all`）"),
    ("MUT-RESULT-UNKNOWN", "ERROR",
     "result = unknown——**未回填不等于通过**：还没测完的格子必须继续红着"),
    ("MUT-RESULT-BAD", "ERROR",
     "result 不在五选一里（covered / partial / na / accepted / unknown）"),
    ("MUT-NO-REASON", "ERROR",
     "result 是 partial / na / accepted 但 reason 空（`-` / `无` / `n/a` 也算空）"),
    ("MUT-CAS-MISSING", "ERROR",
     "result = covered 但 cas = `-`——**说测了却指不到用例**"),
    ("MUT-CAS-DANGLING", "ERROR",
     "cas 不是 `-`，但 `代码/测试/用例库.md` 里找不到这个 `CAS-NNN`（用例库缺失时跳过，记 WARN）"),
    ("MUT-ID-DUP", "ERROR",
     "`MU-NNN` 重复——同一行被复制到两张卡上是最容易掩盖漏测的一种错"),
    ("MUT-ID-ORDER", "ERROR",
     "`MU-NNN` 非升序（追加式清单的读法会失真）"),
    ("MUT-OBJECT-EMPTY", "ERROR",
     "object 为空——**不知道被变更作用的是哪个业务对象**，覆盖矩阵无从对账"),
    ("MUT-ID-BAD", "ERROR",
     "id 为空或不是 `MU-NNN` 形状（3 位数字）——扩展码，契约只写了重复/乱序，"
     "空与畸形 id 若不拦，重复与升序两条检查都会静默失效"),
    ("MUT-MATRIX-MISSING", "WARN",
     "某张 `tc-*` 没有 `变更矩阵.md`——该卡未声明变更矩阵（全部卡都没有时整体退出码 2）"),
    ("MUT-BUDGET-MISSING", "WARN",
     "某张 `tc.md` frontmatter 缺 `combo_budget` 键——**预算没写在卡上，等于没人批过**"
     "（形式对齐 `browser_tab_cap` 的 `BRW-TAB-CAP-UNDECLARED`：缺键只是 WARN）"),
    ("MUT-FACE-GAP", "WARN",
     "同一 object + event 下，某些面既没出现在任何 faces 里、也没有一行显式写 `faces=na`"
     "——**「没测」与「不适用」混为一谈**"),
    ("MUT-CASLIB-MISSING", "WARN",
     "`代码/测试/用例库.md` 不存在——本次**跳过全部 cas 悬空检查**（查不了，不是没悬空）"),
    ("MUT-FORMAT-STALE", "WARN",
     "机读块没闭合 / 没有表头 / 列头不是那 7 列——**格式旧 ≠ 内容缺**：整块只报一次，"
     "跳过该块的行级检查"),
    ("MUT-NO-TESTCARD", "INFO", "`测试卡/` 目录不存在——**无法检查，不是「没问题」**"),
    ("MUT-NO-CARD", "INFO", "`测试卡/` 下没有任何 `tc-*` 目录——**无法检查，不是「没问题」**"),
    ("MUT-NO-MATRIX", "INFO",
     "扫到了测试卡，但没有一张有 `变更矩阵.md`——**无法检查，不是「没问题」**"),
]


def out(text=""):
    sys.stdout.write(text + "\n")


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


def uncomment(value):
    """剥掉 frontmatter 行内注释与包裹引号（模板里的 `# 说明` 是给人看的）。"""
    v = value.strip()
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


def split_cells(line, strip_edges=True, width=None):
    """按 `|` 切格。

    `strip_edges` 是**缺省**的剥边意图，一般由表头写法给出（表头带边就整表带边）。
    不能无条件剥：`| MU-001 | …` 与「首列真的空着」在字面上同形，
    无条件剥会让 **id 为空** 悄悄变成「少一格」，从此这条检查永不触发
    （先例：v5.7.0「空与缺混为一谈」）。

    `width`（表头格数，给了就启用）用来消解**逐行写法与表头不一致**的朽格式：
    只有当「另一种剥边写法」恰好等于表头格数时才切换过去，
    且**优先保持按表头写法切出的结果**——这样首列真空着仍是 7 格、`MUT-ID-BAD` 照旧触发；
    而「表头没写边、数据行写了边」（手写机读块最常见的走样）能正常解析成 7 格。
    """
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


def parse_table(body):
    """机读块 → (表头格子, [(行号, 格子)])。注释行/空行/分隔行都跳过。

    剥边规则**整表统一**：表头带边（`| a | b |`）就整表剥边，表头不带边就整表不剥。
    """
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


def find_mutation_blocks(lines):
    """返回 [(起始行号, [(行号, 原文), …], 是否闭合)]。

    两种围栏写法都认（同一份数据，不许因为围栏长什么样而「看不见」）：
      ① 框架统一的 HTML 注释标记 `<!-- machine:mutation -->` … `<!-- /machine:mutation -->`
      ② ``` 围栏，且围栏内**首行**就是 `machine:mutation`
         （`变更矩阵.md` 是给人读的正文，机读块常被作者写进代码块里）
    """
    blocks = []
    n = len(lines)
    i = 0
    while i < n:
        s = lines[i].strip()
        if s == MUT_START:
            body = []
            j = i + 1
            while j < n and lines[j].strip() != MUT_END:
                body.append((j + 1, lines[j]))
                j += 1
            blocks.append((i + 1, body, j < n))
            i = j + 1
            continue
        if s.startswith("```"):
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and lines[j].strip() == FENCE_TAG:
                body = []
                k = j + 1
                while k < n and not lines[k].strip().startswith("```"):
                    body.append((k + 1, lines[k]))
                    k += 1
                blocks.append((i + 1, body, k < n))
                i = k + 1
                continue
        i += 1
    return blocks


def resolve_base(root):
    """项目根 → 含 `测试卡/` 的那层（与 case_audit.py / test_scope_audit.py 同读法）。"""
    if not root:
        root = os.getcwd()
    root = os.path.abspath(root)
    inner = os.path.join(root, "project-state")
    if os.path.isdir(inner):
        return inner
    if os.path.isdir(os.path.join(root, "代码")) and os.path.isdir(os.path.join(root, "契约")):
        return root
    return root if os.path.isdir(root) else None


def rel(base, path):
    try:
        return os.path.relpath(path, base).replace("\\", "/")
    except ValueError:
        return path


def card_order(name):
    """`tc-001` / `tc-010` 按**数字**排（字典序会把 tc-010 排到 tc-002 前面）。"""
    m = re.search(r"(\d+)$", name)
    return (int(m.group(1)) if m else 0, name)


def scan_cards(base):
    """扫 `测试卡/tc-*/`。返回 [{"name", "dir", "tc", "matrix", "fm"}…]，按序号排序。

    **目录存在就算一张卡**（哪怕 `tc.md` / `变更矩阵.md` 缺失）——
    缺文件本身就是本脚本要报的东西，不能因为文件缺就把它从扫描里漏掉。
    """
    tcdir = os.path.join(base, TC_FOLDER)
    cards = []
    if not os.path.isdir(tcdir):
        return cards
    for name in sorted(os.listdir(tcdir), key=card_order):
        d = os.path.join(tcdir, name)
        if not os.path.isdir(d) or not TC_DIR_RE.match(name):
            continue
        tc_path = os.path.join(d, TC_NAME)
        fm = None
        if os.path.isfile(tc_path):
            fm, _closed = parse_frontmatter(read_text(tc_path))
        cards.append({"name": name, "dir": d, "tc": tc_path,
                      "matrix": os.path.join(d, MATRIX_NAME), "fm": fm})
    return cards


def scan_caslib(base):
    """读用例库里的全部 `CAS-NNN` → (是否找到文件, 编号集合, 绝对路径)。

    只认编号、不认排版：用例库将来换表头也不会让 `cas` 检查失效
    （换排版是 `case_audit.py` 的事，这里只要「这个编号存不存在」）。
    """
    path = os.path.join(base, *CASLIB_PARTS)
    if not os.path.isfile(path):
        return False, set(), path
    return True, set(CAS_ANY_RE.findall(read_text(path))), path


def parse_faces(value):
    """faces 格 → (覆盖到的面集合, 是否声明了整组 na, 七面之外的脏名字列表)。

    - `all` = 七面全测（`ALL` / `al` 不会被当 `all`：拼错就是 `MUT-FACE-UNKNOWN`）
    - `na`  = 整组不适用（**这行的 object + event 组合不再要求面覆盖**）
    - 其余用 `,` / `，` / `、` 分隔；空白不是分隔符（`列表 详情` 会被当脏名字打出来）
    """
    tokens = [t.strip() for t in FACE_SPLIT_RE.split(value or "") if t.strip()]
    faces = set()
    na = False
    bad = []
    for t in tokens:
        if t == "all":
            faces.update(FACES)
        elif t == "na":
            na = True
        elif t in FACES:
            faces.add(t)
        else:
            bad.append(t)
    return faces, na, bad


class Report(object):
    def __init__(self):
        self.items = []
        self.counts = {"ERROR": 0, "WARN": 0, "INFO": 0}

    def add(self, code, sev, where, message):
        self.items.append({"code": code, "severity": sev, "where": where, "message": message})
        self.counts[sev] += 1


def check_cards(base, cards, caslib_exists, cas_ids):
    """核心检查。返回 (report, matrices, rows, groups)。

    rows   = 逐行解析结果（供 `--json` 的 `details`）。
    groups = 跨全部卡的 (object, event) → 面覆盖并集（供 `MUT-FACE-GAP` 判据）。
             覆盖矩阵是**框架级**的：同一个业务对象可能在两张测试卡里各测一半，
             所以并集必须跨卡算，不能一张卡一个并集。
    """
    rep = Report()
    rows = []
    groups = {}
    seen_ids = {}
    seq = []                 # 全局出现顺序的 MU 序号
    order_flagged = False
    matrices = 0

    for card in cards:
        name = card["name"]
        where_tc = rel(base, card["tc"]) if os.path.isfile(card["tc"]) else rel(base, card["dir"])
        where_matrix = rel(base, card["matrix"])

        # ---- 预算键（缺键只是 WARN：默认值不写在卡上，等于没人批过）
        fm = card["fm"]
        budget_declared = bool(fm) and BUDGET_KEY in fm and fm[BUDGET_KEY][0] != ""
        if not budget_declared:
            if os.path.isfile(card["tc"]):
                rep.add("MUT-BUDGET-MISSING", "WARN", where_tc,
                        "frontmatter 缺 `%s` 键——**预算应由项目经理在卡上声明**："
                        "本卡能往变更矩阵里写多少行，是开工前就该批的数字，"
                        "不是写完再补的一句说明（照 `browser_tab_cap` 的先例，缺键记 WARN）"
                        % BUDGET_KEY)
            else:
                rep.add("MUT-BUDGET-MISSING", "WARN", where_tc,
                        "`%s` 不存在，读不到 `%s`——**预算没人批过**（缺 tc.md 本身也是问题，"
                        "但本脚本只核变更矩阵这一面）" % (TC_NAME, BUDGET_KEY))

        # ---- 没有 变更矩阵.md：这张卡没声明变更矩阵
        if not os.path.isfile(card["matrix"]):
            rep.add("MUT-MATRIX-MISSING", "WARN", where_matrix,
                    "这张测试卡没有 `%s`——**变更后一致性没声明覆盖矩阵**："
                    "「测了权限回收」不等于「回收之后七面都看过」" % MATRIX_NAME)
            continue

        matrices += 1
        text = read_text(card["matrix"])
        blocks = find_mutation_blocks(text.splitlines())
        if not blocks:
            rep.add("MUT-FORMAT-STALE", "WARN", where_matrix,
                    "文件里没有 `%s` 机读块——**格式旧 ≠ 内容缺**："
                    "补上机读块（列头 `%s`）后本卡才会进覆盖判据" % (FENCE_TAG, CANON_HEADER))
            continue

        for bstart, body, closed in blocks:
            bwhere = "%s:%d" % (where_matrix, bstart)
            if not closed:
                rep.add("MUT-FORMAT-STALE", "WARN", bwhere,
                        "机读块没有收尾——**格式旧 ≠ 内容缺**：本块跳过行级检查，"
                        "补齐围栏（`%s` 或 ```）后才核覆盖" % MUT_END)
                continue

            header, trows = parse_table(body)
            if header is None:
                rep.add("MUT-FORMAT-STALE", "WARN", bwhere,
                        "机读块里没有表头行——**格式旧 ≠ 内容缺**：本块只报这一次，跳过行级检查")
                continue
            if header != CANON:
                rep.add("MUT-FORMAT-STALE", "WARN", bwhere,
                        "列头不是 7 列（应为 `%s`；实得 `%s`）——**格式旧 ≠ 内容缺**："
                        "本块只报这一次整块级 WARN，**跳过该块的行级检查**（缺列不逐行重复报）"
                        % (CANON_HEADER, " | ".join(header)))
                continue

            idx = {c: k for k, c in enumerate(header)}

            def cell(cells, key):
                k = idx.get(key)
                if k is None or k >= len(cells):
                    return ""
                return cells[k]

            for lineno, cells in trows:
                where = "%s:%d" % (where_matrix, lineno)
                bid = cell(cells, "id").strip()
                obj = cell(cells, "object").strip()
                ev = cell(cells, "event").strip()
                faces_raw = cell(cells, "faces").strip()
                cas = cell(cells, "cas").strip()
                res = cell(cells, "result").strip()
                reason = cell(cells, "reason").strip()

                # ---- id：形状 / 唯一 / 升序
                if not bid:
                    rep.add("MUT-ID-BAD", "ERROR", where,
                            "id 为空——**这一行没有身份**：覆盖矩阵的每一行都要能被 `MU-NNN` 指出来，"
                            "否则「哪一行还没回填」永远对不上账")
                elif not ID_RE.match(bid):
                    rep.add("MUT-ID-BAD", "ERROR", where,
                            "id `%s` 不是 `MU-NNN` 形状（3 位数字）——跨卡对不上号，"
                            "重复与升序两条检查也会跟着静默失效" % bid)
                else:
                    if bid in seen_ids:
                        rep.add("MUT-ID-DUP", "ERROR", where,
                                "id `%s` 与 %s 重复——**全局唯一**（跨全部卡），"
                                "同一行被复制到两张卡上是最容易掩盖漏测的一种错"
                                % (bid, seen_ids[bid]))
                    else:
                        seen_ids[bid] = where
                        n = int(NUM_RE.match(bid).group(1))
                        if seq and n <= seq[-1] and not order_flagged:
                            rep.add("MUT-ID-ORDER", "ERROR", where,
                                    "id 未按出现顺序升序（`%s` 出现在 `MU-%03d` 之后）——"
                                    "追加式清单的读法会失真" % (bid, seq[-1]))
                            order_flagged = True
                        seq.append(n)

                # ---- object：被变更作用的业务对象
                if not obj:
                    rep.add("MUT-OBJECT-EMPTY", "ERROR", where,
                            "object 为空——**不知道被变更作用的是哪个业务对象**："
                            "同一对象在两张卡里各测一半时，空 object 会让并集算错")

                # ---- event：四子族
                if ev not in EVENTS:
                    rep.add("MUT-EVENT-UNKNOWN", "ERROR", where,
                            "event `%s` 不在四子族里——**变更类型必须落在族上**，"
                            "否则「权限回收之后七面全要重看」这条判据无从套用（家族：%s）"
                            % (ev or "空", " / ".join(EVENTS)))

                # ---- faces：七面 / all / na
                covered_faces, na_declared, bad_faces = parse_faces(faces_raw)
                if bad_faces:
                    rep.add("MUT-FACE-UNKNOWN", "ERROR", where,
                            "faces 里有七面之外的名字：%s——**面名对不上就无法核对覆盖**"
                            "（七面：%s；整组不适用写 `na`，七面全测写 `all`）"
                            % ("、".join("`%s`" % b for b in bad_faces), " / ".join(FACES)))

                # ---- result：五选一，unknown 不是通过
                if res == "unknown":
                    rep.add("MUT-RESULT-UNKNOWN", "ERROR", where,
                            "result = unknown——**未回填不等于通过**：这一格还没测完，"
                            "继续保持红色才是诚实的（回填后写 covered 并给 cas）")
                elif res not in RESULTS:
                    rep.add("MUT-RESULT-BAD", "ERROR", where,
                            "result `%s` 不在五选一里（covered / partial / na / accepted / unknown）"
                            "——**结果格写歪了，覆盖统计就是假的**" % (res or "空"))

                # ---- reason：partial / na / accepted 必须说清楚为什么
                if res in REASON_REQUIRED and reason.lower() in REASON_EMPTY:
                    rep.add("MUT-NO-REASON", "ERROR", where,
                            "result = %s 但 reason 空（`-` / `无` / `n/a` 都算空）——"
                            "**说不清的「不适用」就是漏测的形状**：要么补测，"
                            "要么写清为什么这一面在本次变更里不成立" % res)

                # ---- cas：covered 必须指到真实用例
                cas_empty = cas in EMPTY_OK
                if res == "covered" and cas_empty:
                    rep.add("MUT-CAS-MISSING", "ERROR", where,
                            "result = covered 但 cas = `-`——**说测了却指不到用例**："
                            "覆盖矩阵的每一格结论都要能顺着 `CAS-NNN` 回到用例库")
                elif not cas_empty and caslib_exists:
                    if cas not in cas_ids:
                        rep.add("MUT-CAS-DANGLING", "ERROR", where,
                                "cas `%s` 在 `%s` 里找不到——**悬空指针 = 假覆盖**："
                                "`CAS-NNN` 形状写错、或这条用例根本没进用例库"
                                % (cas, rel(base, os.path.join(base, *CASLIB_PARTS))))

                # ---- 原料：同一 object + event 的面覆盖并集
                if obj and ev in EVENTS:
                    slot = groups.setdefault((obj, ev),
                                             {"faces": set(), "na": False, "where": where})
                    slot["faces"].update(covered_faces)
                    if na_declared:
                        slot["na"] = True

                rows.append({"id": bid or "(空)", "object": obj or "(空)", "event": ev or "(空)",
                             "faces": sorted(covered_faces), "faces_na": na_declared,
                             "cas": cas, "result": res, "reason": reason,
                             "where": where, "card": name})

    # ---- MUT-FACE-GAP：同一 object + event 下，没被任何 faces 覆盖、也没整组 na 的面
    for (obj, ev), slot in sorted(groups.items()):
        if slot["na"]:
            continue
        missing = [f for f in FACES if f not in slot["faces"]]
        if missing:
            rep.add("MUT-FACE-GAP", "WARN", slot["where"],
                    "object `%s` + event `%s` 下这些面既没出现在任何 `faces` 里、"
                    "也没有一行显式写 `faces=na`：%s——**「没测」与「不适用」混为一谈**："
                    "要么补测并进 `faces`，要么明说这一组不适用（`na` + 原因）"
                    % (obj, ev, " / ".join(missing)))

    # ---- 用例库缺失：只报一次（查不了 ≠ 没悬空）
    if not caslib_exists:
        rep.add("MUT-CASLIB-MISSING", "WARN",
                rel(base, os.path.join(base, *CASLIB_PARTS)),
                "用例库不存在——本次**跳过全部 cas 悬空检查**（**查不了，不是没悬空**；"
                "无库时 `covered` 只校验了「cas 不是 `-`」这一半）")

    return rep, matrices, rows, groups


def exit_json(base, exit_code, rep, n_cards, n_matrices, rows, reason=""):
    """`--json` 的统一载荷：`errors` / `warns` / `infos` 分开，`details` 给逐行。"""
    payload = {
        "version": VERSION,
        "base": base,
        "cards": n_cards,
        "matrices": n_matrices,
        "rows": len(rows),
        "ok": None if exit_code == 2 else (rep.counts["ERROR"] == 0),
        "exit": exit_code,
        "errors": [it for it in rep.items if it["severity"] == "ERROR"],
        "warns": [it for it in rep.items if it["severity"] == "WARN"],
        "infos": [it for it in rep.items if it["severity"] == "INFO"],
        "counts": rep.counts,
        "details": rows,
        "items": rep.items,
    }
    if reason:
        payload["reason"] = reason
    out(json.dumps(payload, ensure_ascii=False, indent=2))


def print_items(rep):
    for it in sorted(rep.items, key=lambda x: (SEV_ORDER[x["severity"]], x["where"])):
        out("[%-5s] %-24s %s" % (SEV_LABEL[it["severity"]], it["code"], it["where"]))
        out("        %s" % it["message"])


SCOPE_NOTE = ("范围声明：本检查器**只核「声明与自相矛盾」**——变更矩阵里的事件枚举、面枚举、"
              "结果回填、用例指针、id 唯一升序，以及「没测的面有没有被明说成不适用」。")
SCOPE_NOTE2 = ("它**核不了「这些面是不是真的测了」「变更之后系统是不是真的还一致」**："
               "纸面写 `covered` 是一句自述，不是一致性的事实（同「证据自述」口径）——"
               "那要靠用例执行留痕与主控抽查。")


def main(argv):
    root = None
    as_json = False
    check_only = False
    want_checks = False

    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
        elif a == "--check":
            check_only = True
        elif a == "--checks":
            want_checks = True
        elif a in ("-h", "--help"):
            out(__doc__.strip())
            return 0
        elif a.startswith("--"):
            out("未知参数：%s（见 --help）" % a)
            return 2
        else:
            root = a
        i += 1

    if want_checks:
        for code, sev, meaning in CHECKS:
            out("%-24s %-5s %s" % (code, sev, meaning))
        return 0

    base = resolve_base(root)
    if base is None:
        if as_json:
            exit_json(os.path.abspath(root or os.getcwd()), 2, Report(), 0, 0, [],
                      reason="项目根不存在或不可读")
        else:
            out("项目根不存在或不可读：%s" % (root or os.getcwd()))
            out("退出码 2 —— **无法检查 ≠ 通过**（**不是「没问题」**）：路径都没对上，"
                "是「查不了」，不是「查过且干净」")
        return 2

    tcdir = os.path.join(base, TC_FOLDER)
    cards = scan_cards(base)
    rep = Report()

    if not os.path.isdir(tcdir):
        rep.add("MUT-NO-TESTCARD", "INFO", rel(base, tcdir),
                "目录不存在——**无法检查，不是「没问题」**："
                "没有测试卡就没有 `变更矩阵.md`，谈不上覆盖矩阵自洽")
        return report_cannot_check(base, rep, 0, 0, [], as_json, check_only)

    if not cards:
        rep.add("MUT-NO-CARD", "INFO", rel(base, tcdir),
                "`%s/` 下没有任何 `tc-*` 目录——**无法检查，不是「没问题」**" % TC_FOLDER)
        return report_cannot_check(base, rep, 0, 0, [], as_json, check_only)

    caslib_exists, cas_ids, _caslib_path = scan_caslib(base)
    rep, matrices, rows, _groups = check_cards(base, cards, caslib_exists, cas_ids)
    n_cards = len(cards)

    # ⑧ 全都没声明变更矩阵：不是「干净」，是「没法查」
    if matrices == 0:
        rep.add("MUT-NO-MATRIX", "INFO", rel(base, tcdir),
                "扫到 %d 张测试卡，**没有任何一张有 `%s`**——"
                "**无法检查，不是「没问题」**" % (n_cards, MATRIX_NAME))
        return report_cannot_check(base, rep, n_cards, 0, [], as_json, check_only)

    exit_code = 1 if rep.counts["ERROR"] else 0

    if as_json:
        exit_json(base, exit_code, rep, n_cards, matrices, rows)
        return exit_code

    if check_only:
        out("测试卡 %d 张 ｜ 变更矩阵 %d 份 ｜ 矩阵行 %d 行 ｜ ERROR %d / WARN %d（退出码 %d）"
            % (n_cards, matrices, len(rows), rep.counts["ERROR"], rep.counts["WARN"], exit_code))
        return exit_code

    out("== mutation_coverage_audit.py v%s ==" % VERSION)
    out("项目根：%s" % base)
    out("测试卡：%d 张 ｜ 变更矩阵：%d 份 ｜ 矩阵行：%d 行"
        % (n_cards, matrices, len(rows)))
    out("")
    if not rep.items:
        out("无 ERROR / WARN：每一条变更都落在四子族上、回填了结果，"
            "`covered` 都指得到用例，未测的面都明说了不适用。")
        out("（**只证明「填得对」，不证明「变更之后系统真的还一致」**——见下方范围声明。）")
    else:
        print_items(rep)
    out("")
    out(SCOPE_NOTE)
    out(SCOPE_NOTE2)
    out("")
    out("汇总：ERROR %d / WARN %d / INFO %d（扫描 %d 张卡、%d 行）"
        % (rep.counts["ERROR"], rep.counts["WARN"], rep.counts["INFO"], n_cards, len(rows)))
    if rep.counts["ERROR"]:
        out("退出码 1 —— 有 ERROR：打回测试工程师补覆盖矩阵（未回填的格子继续红着，"
            "别拿 `unknown` 当通过）。")
        return 1
    out("退出码 0 —— 干净。")
    return 0


def report_cannot_check(base, rep, n_cards, n_matrices, rows, as_json, check_only):
    """三态里的第三态：**无法检查**。措辞必须念成「查不了」，不许念成「没问题」。"""
    if as_json:
        exit_json(base, 2, rep, n_cards, n_matrices, rows, reason="无法检查")
        return 2
    if check_only:
        out("测试卡 %d 张 ｜ 变更矩阵 %d 份 ｜ ERROR %d / WARN %d（退出码 2 —— 无法检查）"
            % (n_cards, n_matrices, rep.counts["ERROR"], rep.counts["WARN"]))
        return 2
    out("== mutation_coverage_audit.py v%s ==" % VERSION)
    out("项目根：%s" % base)
    out("测试卡：%d 张 ｜ 变更矩阵：%d 份 ｜ 矩阵行：0 行" % (n_cards, n_matrices))
    out("")
    print_items(rep)
    out("")
    out(SCOPE_NOTE)
    out(SCOPE_NOTE2)
    out("")
    out("汇总：ERROR %d / WARN %d / INFO %d（扫描 %d 张卡）"
        % (rep.counts["ERROR"], rep.counts["WARN"], rep.counts["INFO"], n_cards))
    out("退出码 2 —— **无法检查 ≠ 通过**（**不是「没问题」**）：三态里的第三态，"
        "报告里没有「查过且干净」这层意思")
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv))
