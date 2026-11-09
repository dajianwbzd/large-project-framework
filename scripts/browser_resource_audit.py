#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_resource_audit.py — 浏览器共享资源的「取用—归还配对」只读检查器（v5.10.0）

为什么有这个脚本
----------------
2026-09-29 的真实工程事故：**三名队友各自 `session start`、各开 7~15 个 tab**，
把浏览器扩展的 service worker 拖垮，日志里反复出现
`reaping unresponsive browser` / `session removed`。
根因不是谁做错了操作，而是**共享资源没有仲裁**：

    浏览器是**全局唯一的共享资源**（一个扩展、一套登录态、一个 service worker），
    但每个角色都把它当成**自己一个人的资源**在用——
    开的时候没人在册，用完的时候没人归还，**超额与重叠直到把服务打垮才被发现**。

事故之后立的规矩是三条：**声明**（谁在什么时候占）、**预算**（一个会话最多几个 tab）、
**归还**（用完必须交还并把证据写在纸上）。本脚本就是这三条规矩的查看器。

三条纪律（逐条有先例：`case_audit.py` v5.8.0 / `test_scope_audit.py` v5.9.0）
  ① **「有问题」与「查不了」必须分开**：项目根下一张 `card.md`/`tc.md` 都没有 →
     退出码 **2**、`ok=null`，**不是「没问题」**（同 `recall.py`）。
  ② **格式旧 ≠ 内容缺**：机读块存在但列头不是那 7 列 → 整块**只报一次**
     `BRW-FORMAT-STALE`（WARN）并**跳过该块的行级检查**——
     升级当天所有老卡一起变红，真检测力反而被淹没。
  ③ **不稳定的量不进闸门 / 不谎报能力**：只核**声明与配对**，
     **核不了**「会话是不是真的被关掉了」，也**核不了**「tab 是不是真的关干净了」——
     那些只有人（或当时那把 `browser_tabs` 的返回）知道，机器只能看纸面
     （同 v5.10.0 注意事项 50 的「证据自述」口径）。

用法
----
    python -X utf8 scripts/browser_resource_audit.py <项目根>          # 只读体检
    python -X utf8 scripts/browser_resource_audit.py <项目根> --json   # 机器可读
    python -X utf8 scripts/browser_resource_audit.py <项目根> --check  # 只印计数

    退出码：0 干净 ｜ 1 有 ERROR ｜ 2 无法检查（**不是「没问题」**）
"""

import argparse
import json
import os
import re
import sys

VERSION = "5.11.0"

# ---------------------------------------------------------------- 契约常量
# 机读块列头：**一个字都不能差**（7 列）
CANON = ["id", "card", "session", "tabs", "take", "return", "evidence"]
CANON_HEADER = "id | card | session | tabs | take | return | evidence"

START = "<!-- machine:browser -->"
END = "<!-- /machine:browser -->"

# 单会话 tab 上限（与 `project-state/契约/浏览器资源调度规程模板.md` 一致）
DEFAULT_TAB_CAP = 3

# 「空 / 占位」的写法（同 case_audit.py：不把这些当内容）
EMPTY_OK = {"", "-", "--", "无", "n/a", "na", "none", "tbd", "todo", "待定", "[]", "[ ]"}
# 「自述」类证据：不是路径，**不做悬空校验**（机器核不了自述真假）
SELF_REPORT = {"自述", "无证据路径", "口头", "记忆"}

# 卡正文命中这几个词之一 = 这张卡用了浏览器取证 → 必须有取用—归还记录
BROWSER_TOKENS = ["浏览器", "browser", "browser_", "bsk "]
BROWSER_FM_KEY = "browser_tab_cap"

ID_RE = re.compile(r"^BRW-\d{3,}$")
NUM_RE = re.compile(r"BRW-(\d+)")
CARD_DIR_RE = re.compile(r"^card-\d+$")
TC_DIR_RE = re.compile(r"^tc-\d+$")

# YYYY-MM-DD HH:MM 与 YYYY-MM-DDTHH:MM（分钟精度即可）
TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$")
# 长成路径的证据（含 `.md` / 目录分隔符）
PATHISH_RE = re.compile(r"[^\s，,；;（）()]+\.(?:md|txt|log|json|png|jpg|jpeg|webp|gif|html|pdf|csv|yaml|yml)\b",
                        re.IGNORECASE)
# 路径前的常见引导词（剥掉才能拿到真路径）
PATH_LEAD_RE = re.compile(r"^(?:见|证据[:：]|证据文件[:：]|路径[:：]|at|see)[\s:：]*")

SEV_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
SEV_LABEL = {"ERROR": "错误", "WARN": "告警", "INFO": "提示"}
SEV_TAG = {"ERROR": "E", "WARN": "W", "INFO": "I"}


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


def strip_frontmatter(text):
    """剥掉开头 `---` … `---` 的机读 frontmatter，返回正文。

    frontmatter 里的 `browser_tab_cap: 3` **不算「这张卡用了浏览器」**——
    那恰恰是没用的卡也该有的预算声明（规则 9 反过来要它存在）。
    """
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:])
    return text


def strip_blocks_and_fences(text):
    """剥掉机读块**整段**与 ``` 围栏**整段**，返回剩下的正文本体。

    用途：只在「人写的正文」里找 `浏览器` 这类词——
    机读块里的表头、``` 围栏里的示例卡、被注释掉的旧记录都不算「这张卡用了浏览器」。
    """
    kept = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s == START:
            j = i + 1
            while j < len(lines) and lines[j].strip() != END:
                j += 1
            i = j + 1
            continue
        if s.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            i = j + 1
            continue
        kept.append(lines[i])
        i += 1
    return "\n".join(kept)


def strip_html_comments(text):
    """剥掉 `<!-- -->` 注释体（只保留「有浏览器取证」判据的干净输入）。"""
    return re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)


def uses_browser(text):
    """卡（人写正文）里有没有「用了浏览器取证」的痕迹。

    三样都不算：frontmatter（`browser_tab_cap` 是预算声明本身）、
    机读块（取用—归还记录本身）、``` 围栏与 `<!-- -->` 注释（示例与历史）。
    """
    body = strip_html_comments(strip_blocks_and_fences(strip_frontmatter(text)))
    low = body.lower()
    return any(tok in low for tok in BROWSER_TOKENS)


def split_cells(line, strip_edges=True, width=None):
    """按 `|` 切格。

    `strip_edges` 是**缺省**的剥边意图，一般由表头写法给出（表头带边就整表带边）。
    不能无条件剥：`| card-001 | …` 与「首列真的空着」在字面上同形，
    无条件剥会让 **id 为空** 悄悄变成「少一格」，从此这条检查永不触发
    （先例：v5.7.0「空与缺混为一谈」）。

    `width`（表头格数，给了就启用）用来消解**逐行写法与表头不一致**的朽格式：
    只有当「另一种剥边写法」恰好等于表头格数时才切换过去，
    且**优先保持按表头写法切出的结果**——这样 `| | card-001 | … |`（首列真空着）
    仍是 N 格、`BRW-EMPTY-ID` 照旧触发；而「表头没写边、数据行写了边」（手写机读块
    最常见的走样）能正常解析成 N 格，不会被误判成「id 为空 + 整体右移一格」。
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
    # 逐行写法与表头不一致（手写机读块最常见的走样）：
    # 试另一种剥边写法，只有恰好等于表头格数时才采用。
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


def parse_table(body):
    """机读块 → (表头格子, [(行号, 格子)])。注释行/空行/分隔行都跳过。

    剥边规则**整表统一**：表头带边（`| a | b |`）就整表剥边，表头不带边就整表不剥。
    若某一行自带的边与表头写法不一致，以**表头**为准（先例 v5.7.0：
    「首列真的空着」与「行首多一个分隔符」在字面上同形，混判会让 **id 为空**
    这条检查永不触发）。
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


def parse_time(value):
    """`YYYY-MM-DD HH:MM` / `YYYY-MM-DDTHH:MM` → 可比较元组；解析不了返回 None。"""
    m = TIME_RE.match((value or "").strip())
    if not m:
        return None
    try:
        parts = [int(x) for x in m.groups()]
    except ValueError:
        return None
    y, mo, d, h, mi = parts
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return (y, mo, d, h, mi)


def extract_path(value):
    """从证据格子里取「像路径的那一段」；取不到（纯自述/纯结论）返回 None。

    取不到 ⇒ **不校验**（机器核不了自述真假，硬核 = 假阳性发生器）。
    """
    s = (value or "").strip()
    if not s or s in EMPTY_OK or s in SELF_REPORT:
        return None
    m = PATHISH_RE.search(s)
    if not m:
        return None
    p = m.group(0).strip()
    p = PATH_LEAD_RE.sub("", p).strip()
    return p or None


def resolve_base(root):
    """项目根 → 含 `需求卡/` `测试卡/` 的那层（与 case_audit.py / test_scope_audit.py 同读法）。"""
    if not root:
        root = os.getcwd()
    root = os.path.abspath(root)
    inner = os.path.join(root, "project-state")
    if os.path.isdir(inner):
        return inner
    if os.path.isdir(os.path.join(root, "需求卡")) or os.path.isdir(os.path.join(root, "测试卡")):
        return root
    return root if os.path.isdir(root) else None


def scan_cards(base):
    """扫 `需求卡/card-*/card.md` 与 `测试卡/tc-*/tc.md`。

    返回 [(卡名, 类别, 相对路径, 绝对路径, frontmatter)]，需求卡在前、同类按目录名排序。
    """
    cards = []
    for kind, folder, fname, rx in (("card", "需求卡", "card.md", CARD_DIR_RE),
                                    ("tc", "测试卡", "tc.md", TC_DIR_RE)):
        d = os.path.join(base, folder)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name, fname)
            if not os.path.isfile(path):
                continue
            if not rx.match(name):
                continue
            fm, _end = parse_frontmatter(read_text(path))
            cards.append((name, kind, path, fm))
    return cards


def rel(base, path):
    try:
        return os.path.relpath(path, base).replace("\\", "/")
    except ValueError:
        return path


class Report(object):
    def __init__(self):
        self.items = []
        self.counts = {"ERROR": 0, "WARN": 0, "INFO": 0}

    def add(self, code, sev, where, message):
        self.items.append({"code": code, "severity": sev, "where": where, "message": message})
        self.counts[sev] += 1


def check_cards(base, tab_cap):
    """核心检查。返回 (report, cards, rows, spans)。

    rows  = 每行一行的可读摘要（供人眼扫「谁在什么时候占了浏览器、还没还」）。
    spans = 跨全部卡收集到的 (take, return, where) —— 供独占时段重叠判据排序。
    """
    rep = Report()
    cards = scan_cards(base)
    rows = []
    spans = []
    seen_ids = {}
    seq = []              # 全局出现顺序的 BRW 序号（跨全部卡）
    order_flagged = False
    stale_blocks = 0

    for name, kind, path, fm in cards:
        text = read_text(path)
        where_card = rel(base, path)
        card_uses_browser = uses_browser(text)
        cap_declared = bool(fm) and BROWSER_FM_KEY in fm and fm[BROWSER_FM_KEY][0] != ""

        blocks = find_blocks(text.splitlines(), START, END)
        id_rows = []          # 本卡已解析出的 (ID, 行号, 行)
        for bstart, body, closed in blocks:
            bwhere = "%s:%d" % (where_card, bstart)
            if not closed:
                rep.add("BRW-FORMAT-STALE", "WARN", bwhere,
                        "机读块没有 `%s` 收尾——**格式旧 ≠ 内容缺**：本块跳过行级检查，"
                        "补上收尾标记后才核取用—归还配对" % END)
                stale_blocks += 1
                continue

            header, trows = parse_table(body)
            if header is None:
                rep.add("BRW-FORMAT-STALE", "WARN", bwhere,
                        "机读块里没有表头行——**格式旧 ≠ 内容缺**：本块只报这一次，跳过行级检查")
                stale_blocks += 1
                continue
            if header != CANON:
                rep.add("BRW-FORMAT-STALE", "WARN", bwhere,
                        "列头不是 7 列（应为 `%s`；实得 `%s`）——**格式旧 ≠ 内容缺**："
                        "本块只报这一次整块级 WARN，**跳过该块的行级检查**（缺列不逐行重复报）"
                        % (CANON_HEADER, " | ".join(header)))
                stale_blocks += 1
                continue

            idx = {c: k for k, c in enumerate(header)}

            def cell(cells, key):
                k = idx.get(key)
                if k is None or k >= len(cells):
                    return ""
                return cells[k]

            for lineno, cells in trows:
                where = "%s:%d" % (where_card, lineno)
                bid = cell(cells, "id")
                card_col = cell(cells, "card")
                session = cell(cells, "session")
                tabs_raw = cell(cells, "tabs")
                take_raw = cell(cells, "take")
                ret_raw = cell(cells, "return")
                ev_raw = cell(cells, "evidence")

                returned = ret_raw.strip() not in EMPTY_OK
                id_rows.append((bid, where, (bid, card_col, session, tabs_raw,
                                             take_raw, ret_raw, ev_raw)))

                # ---- 规则 1：id 形状 / 唯一 / 升序
                if not bid:
                    rep.add("BRW-EMPTY-ID", "ERROR", where,
                            "id 为空——**取用没有身份**：出了事故没法把日志里的会话对回这张卡")
                else:
                    if not ID_RE.match(bid):
                        rep.add("BRW-EMPTY-ID", "ERROR", where,
                                "id `%s` 不是 `BRW-NNN` 形状（3 位起）——跨卡对不上号" % bid)
                    if bid in seen_ids:
                        rep.add("BRW-ID-DUP", "ERROR", where,
                                "id `%s` 与 %s 重复——**全局唯一**（跨全部卡），"
                                "同一行被复制到两张卡上是最容易掩盖占用的一种错" % (bid, seen_ids[bid]))
                    else:
                        seen_ids[bid] = where
                    m = NUM_RE.match(bid)
                    if m:
                        n = int(m.group(1))
                        if seq and n <= seq[-1] and not order_flagged:
                            rep.add("BRW-ID-ORDER", "ERROR", where,
                                    "id 未按出现顺序升序（`%s` 出现在 `BRW-%03d` 之后）——"
                                    "追加式清单的读法会失真" % (bid, seq[-1]))
                            order_flagged = True
                        seq.append(n)

                # ---- 规则 5：session 不能空（`-` 是占位不是空白：它明说「没记」
                #      而不是「这一格不存在」，报它会把「填错」混进「缺格」）
                if not session.strip():
                    rep.add("BRW-SESSION-EMPTY", "ERROR", where,
                            "session 为空——**不知道是哪个会话占着浏览器**，"
                            "日志里的 `reaping unresponsive browser` 就对不回人")

                # ---- 规则 4：tabs 必须是正整数且不超预算
                tnum = None
                if not re.fullmatch(r"\d+", tabs_raw.strip()):
                    rep.add("BRW-TABS-OVER", "ERROR", where,
                            "tabs `%s` 不是正整数——**连开了几个都不知道**，"
                            "预算也就无从核起（单会话上限 %d）" % (tabs_raw or "空", tab_cap))
                else:
                    tnum = int(tabs_raw.strip())
                    if tnum < 1:
                        rep.add("BRW-TABS-OVER", "ERROR", where,
                                "tabs = 0——开了 0 个 tab 却登记了一次取用（要么是没发生，要么是没记）")
                    elif tnum > tab_cap:
                        rep.add("BRW-TABS-OVER", "ERROR", where,
                                "tabs = %d > 单会话上限 %d——**这就是 2026-09-29 那次事故的形状**："
                                "每人 7~15 个 tab 把扩展 service worker 拖垮"
                                "（上限见 `project-state/契约/浏览器资源调度规程模板.md`）" % (tnum, tab_cap))

                # ---- 规则 2：核心判据——取用有没有归还
                if not returned:
                    rep.add("BRW-NO-RETURN", "ERROR", where,
                            "return 为空/占位——**取用了没归还**：共享资源无仲裁的本体，"
                            "一个会话不还，后面所有人的 `session start` 都在挤同一个 service worker")

                # ---- 规则 3：已归还必须有证据，证据里的路径必须真实存在
                if returned:
                    ev = ev_raw.strip()
                    if ev in EMPTY_OK:
                        rep.add("BRW-NO-EVIDENCE", "ERROR", where,
                                "已归还却没有 evidence——**「还了」只是一句话**："
                                "要有可查的痕迹（会话记录/截图/日志指针，写明路径）")
                    else:
                        p = extract_path(ev)
                        if p:
                            target = os.path.normpath(os.path.join(base, p))
                            if not os.path.exists(target):
                                rep.add("BRW-EVIDENCE-DANGLING", "ERROR", where,
                                        "evidence 指向的路径不存在：`%s`（相对项目根解析）——"
                                        "**悬空指针 = 假登记**" % p)

                # ---- 规则 6 的原料：可比较的占用时段
                if returned:
                    t = parse_time(take_raw)
                    r = parse_time(ret_raw)
                    if t is None or r is None:
                        rep.add("BRW-TIME-BAD", "WARN", where,
                                "take/return 时间解析不了（接受 `YYYY-MM-DD HH:MM` 与 "
                                "`YYYY-MM-DDTHH:MM`，分钟精度）——**该行不参与独占时段重叠排序**"
                                "（只报 WARN，不算 ERROR）")
                    elif r < t:
                        rep.add("BRW-TIME-BAD", "WARN", where,
                                "return 早于 take（`%s` → `%s`）——时段反了，本行不参与重叠排序"
                                % (take_raw, ret_raw))
                    else:
                        spans.append((t, r, where))

                rows.append({"id": bid or "(空)", "card": card_col or name, "session": session or "(空)",
                             "tabs": tabs_raw, "take": take_raw, "return": ret_raw,
                             "evidence": ev_raw, "where": where,
                             "returned": returned,
                             "in_budget": (tnum is not None and 1 <= tnum <= tab_cap)})

        # ---- 规则 7：声明了浏览器取证却没有机读块
        if not blocks and card_uses_browser:
            rep.add("BRW-MISSING-BLOCK", "ERROR", where_card,
                    "卡里写了浏览器取证（命中 `浏览器` / `BrowserSkill` / `bsk ` 之类）"
                    "却**没有 `%s` 机读块**——声明了取用却没有取用—归还记录" % START)

        # ---- 规则 9：用了浏览器却没在卡上声明预算（WARN）
        if card_uses_browser and not cap_declared:
            rep.add("BRW-TAB-CAP-UNDECLARED", "WARN", where_card,
                    "卡用了浏览器但 frontmatter 没有 `%s` 字段——**预算应由项目经理在卡上声明**"
                    "（本脚本按默认上限 %d 核，但默认值不写在卡上就没人知道这一卡批了多少）"
                    % (BROWSER_FM_KEY, tab_cap))

    # ---- 规则 6：跨全部卡——同一时刻全局只允许 1 个会话（独占时段不得重叠）
    # 扫过最大 return：与上一行比是不够的——B 的 return 可能比 A 更早，
    # 那时 C 落在 A 的时段里却躲过了「与 B 比」。
    spans.sort(key=lambda x: (x[0], x[1], x[2]))
    prev = None                 # (take, return, where) —— 迄今 return 最晚的那一段
    for t, r, where in spans:
        if prev is not None and t < prev[1]:
            rep.add("BRW-OVERLAP", "ERROR", where,
                    "独占时段重叠：本行 take `%s` **早于**上一行（%s）的 return `%s`——"
                    "**同一时刻全局只允许 1 个会话**，重叠就是 2026-09-29 那次"
                    "「三名队友各自 start」的形状"
                    % ("%04d-%02d-%02d %02d:%02d" % t, prev[2],
                       "%04d-%02d-%02d %02d:%02d" % prev[1]))
        if prev is None or r > prev[1]:
            prev = (t, r, where)

    return rep, cards, rows, spans


def build_parser():
    p = argparse.ArgumentParser(
        prog="browser_resource_audit.py",
        description="浏览器共享资源的「取用—归还配对」只读检查器（v%s）" % VERSION,
        epilog="退出码：0 干净 ｜ 1 有 ERROR ｜ 2 无法检查（**不是「没问题」**）")
    p.add_argument("root", nargs="?", default=".", help="项目根（默认 .）")
    p.add_argument("--json", action="store_true", help="机器可读输出")
    p.add_argument("--check", action="store_true", help="只印计数（门禁用）")
    p.add_argument("--tab-cap", type=int, default=DEFAULT_TAB_CAP,
                   help="单会话 tab 上限（默认 %d）" % DEFAULT_TAB_CAP)
    return p


SCOPE_NOTE = ("范围声明：本检查器**只核「声明与配对」**——谁在什么时候声明占用了浏览器、"
              "一个会话开了几个 tab、有没有写归还与证据。")
SCOPE_NOTE2 = ("它**核不了「会话是否真的被关掉」**，也**核不了「tab 是否真的关干净」**："
               "纸面写 `return` 不等于浏览器真的还回来了——那要靠当场留证与主控抽查。")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)

    base = resolve_base(args.root)
    if base is None:
        out("项目根不存在或不可读：%s" % (args.root or os.getcwd()))
        out("退出码 2 —— **无法检查，不是「没问题」**")
        return 2

    tab_cap = args.tab_cap if args.tab_cap and args.tab_cap > 0 else DEFAULT_TAB_CAP
    rep, cards, rows, spans = check_cards(base, tab_cap)

    n_cards = len(cards)

    if n_cards == 0:
        if args.json:
            out(json.dumps({
                "version": VERSION, "base": base, "cards": 0, "tab_cap": tab_cap,
                "ok": None, "exit": 2, "counts": rep.counts, "items": rep.items, "rows": [],
            }, ensure_ascii=False, indent=2))
            return 2
        out("== browser_resource_audit.py v%s ==" % VERSION)
        out("项目根：%s" % base)
        out("")
        out("[提示] BRW-NO-CARD  %s  **无法检查，不是「没问题」**："
            % rel(base, os.path.join(base, "需求卡")))
        out("        项目根下**没有任何 `card.md` / `tc.md`**（`需求卡/card-NNN/card.md`、"
            "`测试卡/tc-NNN/tc.md` 都不存在）——")
        out("        没有卡就没有「谁声明用过浏览器」这回事，谈不上「取用—归还配对是否干净」。")
        out("")
        out(SCOPE_NOTE)
        out(SCOPE_NOTE2)
        out("")
        out("browser_resource_audit：ERROR 0 / WARN 0（扫描 0 张卡）")
        out("退出码 2 —— **无法检查，不是「没问题」**")
        return 2

    exit_code = 1 if rep.counts["ERROR"] else 0

    if args.json:
        out(json.dumps({
            "version": VERSION,
            "base": base,
            "cards": n_cards,
            "tab_cap": tab_cap,
            "ok": rep.counts["ERROR"] == 0,
            "exit": exit_code,
            "counts": rep.counts,
            "spans": len(spans),
            "rows": rows,
            "items": rep.items,
        }, ensure_ascii=False, indent=2))
        return exit_code

    if args.check:
        out("卡 %d 张 ｜ 取用记录 %d 行 ｜ ERROR %d / WARN %d（退出码 %d）"
            % (n_cards, len(rows), rep.counts["ERROR"], rep.counts["WARN"], exit_code))
        return exit_code

    out("== browser_resource_audit.py v%s ==" % VERSION)
    out("项目根：%s" % base)
    out("卡：%d 张 ｜ 取用记录：%d 行 ｜ 单会话 tab 上限：%d"
        % (n_cards, len(rows), tab_cap))
    out("")
    if rows:
        out("%-9s %-10s %-12s %-5s %-17s %-17s %s"
            % ("id", "card", "session", "tabs", "take", "return", "evidence"))
        for r in rows:
            out("%-9s %-10s %-12s %-5s %-17s %-17s %s"
                % (r["id"][:9], r["card"][:10], r["session"][:12], r["tabs"][:5],
                   r["take"][:17] or "-", (r["return"][:17] or "-") if r["returned"] else "**未归还**",
                   r["evidence"][:44] or "-"))
        out("")
    if not rep.items:
        out("无 ERROR / WARN：每一条浏览器取用都**声明了身份、会话与 tab 数，并且成对归还**。")
        out("（**只证明「声明与配对没错」，不证明「会话真的关掉了」**——见下方范围声明。）")
    else:
        for it in sorted(rep.items, key=lambda x: (SEV_ORDER[x["severity"]], x["where"])):
            out("[%s] %-24s %s  %s"
                % (SEV_TAG[it["severity"]], it["code"], it["where"], it["message"]))
    out("")
    out(SCOPE_NOTE)
    out(SCOPE_NOTE2)
    out("")
    out("browser_resource_audit：ERROR %d / WARN %d（扫描 %d 张卡）"
        % (rep.counts["ERROR"], rep.counts["WARN"], n_cards))
    if rep.counts["ERROR"]:
        out("退出码 1 —— 有 ERROR：打回项目经理/角色补声明与归还（共享资源无仲裁，"
            "2026-09-29 事故的形状）。")
        return 1
    out("退出码 0 —— 干净。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
