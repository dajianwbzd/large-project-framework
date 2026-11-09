#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""case_audit.py — 测试用例资产（用例库）只读检查器（v5.8.0）

为什么有这个脚本
----------------
v5.8.0 之前，黑盒验收的用例是**每轮临时从卡片验收标准反推**出来的：
同一份 AC + 同一份契约 → 每轮产出同一份清单（**每轮一样**）；
全篇没有测试设计方法（等价类/边界值/判定表/状态迁移/正交/场景法/错误推测/变异/穿透），
也没有任何沉淀物（**不全面**）；更没有一条判据要求证明「看到的不是前端 mock 造出来的」
（**被 mock 骗过**）。把它写成字段、放进一个**会长大**的文件
（`project-state/代码/测试/用例库.md`），才有东西可以被机器查——本脚本就是那个查看器。

三条纪律（本框架踩过的坑，逐条有先例）
  ① **「有问题」与「查不了」必须分开**：用例库不存在 → 退出码 **2**、`ok=null`，
     **不是「没问题」**（同 `recall.py` / `task_estimate.py` 的退出码纪律）。
  ② **格式旧 ≠ 内容缺**：列集不是 7 列时报**一次整块级** `CASES-FORMAT-STALE`，
     **不与逐行「字段为空」同时输出**（为修一个假阳性而弄丢真检测力，比假阳性更坏）。
  ③ **不稳定的量不进闸门**：只核「字段齐全 + 指针存在」，**不猜**用例好不好、
     覆盖够不够（那是测试设计判断，机器判不了；方向由项目经理出、范围由主控排）。

用法
----
    python scripts/case_audit.py <项目根>                    # 只读体检，按严重度打印
    python scripts/case_audit.py <项目根> --json             # 机器可读
    python scripts/case_audit.py <项目根> --check            # 只印计数（门禁用）
    python scripts/case_audit.py <项目根> --hotspots         # 只扫「发现项热区」：哪里反复出问题、有没有回归用例
    python scripts/case_audit.py <项目根> --allow-unmigrated # 迁移期：格式旧降级为提示

    退出码：0 干净 ｜ 1 有 ERROR ｜ 2 无法检查（**不是「没问题」**）
"""

import json
import os
import re
import sys

VERSION = "5.8.0"

# ---------------------------------------------------------------- 契约常量
CANON = ["id", "scope", "method", "data", "anti_mock", "source", "last"]
METHODS = ["等价类", "边界值", "判定表", "状态迁移", "正交", "场景法", "错误推测", "变异", "穿透"]
SOURCES = ["AC", "发现", "反馈", "探索", "回归"]
LASTS = ["pass", "fail", "skip", "unknown", "retired"]
ANTIMOCK_EMPTY = {"", "-", "--", "无", "n/a", "na", "none", "无判据", "tbd", "todo", "待定"}
EMPTY_OK = {"", "-", "--"}

CASES_START = "<!-- machine:cases -->"
CASES_END = "<!-- /machine:cases -->"
FIND_START = "<!-- machine:findings -->"
FIND_END = "<!-- /machine:findings -->"

SEV_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
SEV_LABEL = {"ERROR": "错误", "WARN": "告警", "INFO": "提示"}

ID_RE = re.compile(r"^CAS-\d{3,}$")
NUM_RE = re.compile(r"CAS-(\d+)")
CARD_RE = re.compile(r"^card-\d+$")
TC_RE = re.compile(r"^tc-\d+$")
DISC_RE = re.compile(r"^发现:([^#\s]+)#(\S+)$")
ANY_ANCHOR_RE = re.compile(r"(card-\d+|tc-\d+|REQ-\d+(?:\.\d+)?)")


def out(text=""):
    sys.stdout.write(text + "\n")


def read_text(path):
    """UTF-8 优先、GBK 兜底：真实工程里这两种都存在（先例：recall.py）。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def split_cells(line, strip_edges=True):
    """按 `|` 切格。

    `strip_edges` 由**表头行的写法**决定（表头以 `|` 开头才剥行首行尾的分隔符），
    不能无条件剥：` | card-001 | …` 与「首列真的空着」在字面上同形，
    无条件剥会让 **id 为空** 悄悄变成「少一格」，从此这条检查永不触发
    （先例：v5.7.0「空与缺混为一谈」）。
    """
    cells = [c.strip() for c in line.strip().split("|")]
    if strip_edges:
        if cells and cells[0] == "":
            cells.pop(0)
        if cells and cells[-1] == "":
            cells.pop()
    return cells


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
            edges = s.startswith("|")
            continue
        cells = split_cells(s, edges)
        if is_sep_row(cells):
            continue
        rows.append((lineno, cells))
    return header, rows


def resolve_base(root):
    """项目根 → project-state 目录（含 `代码/` 与 `契约/` 的那层）。"""
    if not root:
        root = os.getcwd()
    root = os.path.abspath(root)
    inner = os.path.join(root, "project-state")
    if os.path.isdir(inner):
        return inner
    if os.path.isdir(os.path.join(root, "代码")) and os.path.isdir(os.path.join(root, "契约")):
        return root
    return root if os.path.isdir(root) else None


def scan_findings(base):
    """扫全部 `测试卡/tc-*/发现项.md` 的 machine:findings → {tc: {"ids": [...], "rows": [...]}}。"""
    tcs = {}
    tcdir = os.path.join(base, "测试卡")
    if not os.path.isdir(tcdir):
        return tcs
    for name in sorted(os.listdir(tcdir)):
        path = os.path.join(tcdir, name, "发现项.md")
        if not os.path.isfile(path):
            continue
        lines = read_text(path).splitlines()
        for _start, body, _closed in find_blocks(lines, FIND_START, FIND_END):
            header, rows = parse_table(body)
            idx = {c: k for k, c in enumerate(header or [])}
            for lineno, cells in rows:
                fid = cells[idx["id"]] if "id" in idx and idx["id"] < len(cells) else ""
                if not fid:
                    continue
                sev = cells[idx["severity"]].lower() if "severity" in idx and idx["severity"] < len(cells) else ""
                anchor = cells[idx["anchor"]] if "anchor" in idx and idx["anchor"] < len(cells) else ""
                tcs.setdefault(name, {"ids": [], "rows": []})
                tcs[name]["ids"].append(fid)
                tcs[name]["rows"].append({"id": fid, "severity": sev, "anchor": anchor,
                                          "where": "%s/发现项.md:%d" % (name, lineno)})
    return tcs


def anchor_prefix(anchor):
    """锚点 → 热区键（只取路径的前两段；行号/符号通配一律先剥掉）。"""
    s = (anchor or "").strip()
    if not s or s in ("-", "未知", "n/a", "无"):
        return ""
    s = s.split("#")[0]
    s = re.sub(r":\d+(?:-\d+)?$", "", s)
    s = s.replace("\\", "/").strip("/")
    parts = [p for p in s.split("/") if p not in ("", ".", "..")]
    if not parts:
        return ""
    return "/".join(parts[:2])


class Report(object):
    def __init__(self):
        self.items = []
        self.counts = {"ERROR": 0, "WARN": 0, "INFO": 0}

    def add(self, code, sev, where, message):
        self.items.append({"code": code, "severity": sev, "where": where, "message": message})
        self.counts[sev] += 1


def rel(base, path):
    try:
        return os.path.relpath(path, base).replace("\\", "/")
    except ValueError:
        return path


def find_has_finding(findings, tc, fid):
    if tc not in findings:
        return None
    return any(x == fid or x.startswith(fid + "#") for x in findings[tc]["ids"])


def check_cases(base, lib_path, allow_unmigrated):
    """核心检查。返回 (report, lib_exists, case_count, blocks)。"""
    rep = Report()
    if not os.path.isfile(lib_path):
        tcdir = os.path.join(base, "测试卡")
        tcs = sorted(d for d in (os.listdir(tcdir) if os.path.isdir(tcdir) else [])
                     if os.path.isfile(os.path.join(tcdir, d, "tc.md")))
        rep.add("CASES-NO-LIB", "INFO", rel(base, lib_path),
                "用例库不存在——**无法检查**（退出码 2）。新工程早期属正常，但不得当作「用例没问题」")
        if tcs:
            rep.add("CASES-MISSING-WITH-TC", "ERROR", rel(base, lib_path),
                    "已有系统级测试卡（%s）却没有用例库——用例资产没落盘" % ", ".join(tcs[:5]))
        return rep, False, 0, 0, 0

    lines = read_text(lib_path).splitlines()
    blocks = find_blocks(lines, CASES_START, CASES_END)
    if not blocks:
        sev = "WARN" if allow_unmigrated else "ERROR"
        rep.add("CASES-NO-BLOCK", sev, rel(base, lib_path),
                "用例库在位但找不到 `%s` 机读块——人读表机器读不了" % CASES_START)
        return rep, True, 0, 0, 0

    findings = scan_findings(base)
    known_ids = {}
    seen = []
    order_flagged = False
    total = 0
    with_antimock = 0

    for bstart, body, closed in blocks:
        bwhere = "%s:%d" % (rel(base, lib_path), bstart)
        if not closed:
            rep.add("CASES-BLOCK-UNCLOSED", "ERROR", bwhere,
                    "机读块没有 `%s` 收尾——后面所有内容都会被当成块内" % CASES_END)
        header, rows = parse_table(body)
        if not header:
            rep.add("CASES-FORMAT-STALE", "ERROR", bwhere, "机读块里没有表头行——无法判定列集")
            continue
        idx = {}
        for k, name in enumerate(header):
            idx.setdefault(name, k)
        missing_cols = [c for c in CANON if c not in idx]
        extra_cols = [c for c in header if c not in CANON]
        if missing_cols or extra_cols:
            sev = "WARN" if allow_unmigrated else "ERROR"
            rep.add("CASES-FORMAT-STALE", sev, bwhere,
                    "列集不是 7 列（缺 %s%s）——**格式旧 ≠ 内容缺**：本块只核还认得的列，缺列不逐行重复报"
                    % (",".join(missing_cols) or "无",
                       ("；多出 " + ",".join(extra_cols)) if extra_cols else ""))

        def cell(cells, name):
            k = idx.get(name)
            if k is None or k >= len(cells):
                return None          # 列不存在 ⇒ 交给 CASES-FORMAT-STALE 说一次，不逐行重复
            return cells[k]

        for lineno, cells in rows:
            where = "%s:%d" % (rel(base, lib_path), lineno)
            if len(cells) != len(header):
                rep.add("CASE-FIELD-PIPE", "ERROR", where,
                        "格子数 %d ≠ 表头 %d——字段里混进了 `|`（要多个用 `,` 或 `/`）"
                        % (len(cells), len(header)))
            total += 1

            cid = cell(cells, "id") or ""
            if not cid:
                rep.add("CASE-ID-EMPTY", "ERROR", where, "id 为空——用例身份没有落点")
            else:
                if not ID_RE.match(cid):
                    rep.add("CASE-ID-FORMAT", "WARN", where, "id `%s` 不是 `CAS-NNN` 形状" % cid)
                if cid in known_ids:
                    rep.add("CASE-ID-DUP", "ERROR", where,
                            "id `%s` 与 %s 重复——**只增不减、永不复用**，废弃写 `last=retired` 不删行"
                            % (cid, known_ids[cid]))
                else:
                    known_ids[cid] = where
                m = NUM_RE.match(cid)
                if m:
                    n = int(m.group(1))
                    if seen and n <= seen[-1]:
                        if not order_flagged:
                            rep.add("CASE-ID-ORDER", "WARN", where,
                                    "id 未严格递增（`%s` 出现在 `CAS-%03d` 之后）——追加式清单的读法会失真"
                                    % (cid, seen[-1]))
                            order_flagged = True
                    seen.append(n)

            scope = cell(cells, "scope")
            if scope is not None:
                s = scope.strip()
                if s in EMPTY_OK:
                    rep.add("CASE-SCOPE-MISSING", "ERROR", where,
                            "scope 为空——用例不知道挂在哪张卡/哪个测试轮上")
                elif s.lower() in ("global", "全局"):
                    pass
                elif CARD_RE.match(s):
                    if not os.path.isdir(os.path.join(base, "需求卡", s)):
                        rep.add("CASE-SCOPE-DANGLING", "ERROR", where,
                                "scope `%s` 指向不存在的卡目录（需求卡/%s/）" % (s, s))
                elif TC_RE.match(s):
                    if not os.path.isdir(os.path.join(base, "测试卡", s)):
                        rep.add("CASE-SCOPE-DANGLING", "ERROR", where,
                                "scope `%s` 指向不存在的测试卡目录（测试卡/%s/）" % (s, s))
                else:
                    rep.add("CASE-SCOPE-DANGLING", "ERROR", where,
                            "scope `%s` 既不是 card-NNN / tc-NNN，也不是 global" % s)

            method = cell(cells, "method")
            if method is not None:
                m = method.strip()
                if m in EMPTY_OK:
                    rep.add("CASE-METHOD-MISSING", "ERROR", where,
                            "method 为空——**没写设计方法 = 又回到照着 AC 顺手点一遍**")
                else:
                    toks = [t for t in re.split(r"[+,/、]", m) if t.strip()]
                    unknown = [t for t in toks if not any(t.strip().startswith(x) for x in METHODS)]
                    if unknown:
                        rep.add("CASE-METHOD-UNKNOWN", "WARN", where,
                                "method 里的 `%s` 不在九法内（%s）" % (",".join(unknown), "/".join(METHODS)))

            data = cell(cells, "data")
            if data is not None and data.strip() in EMPTY_OK:
                rep.add("CASE-DATA-MISSING", "ERROR", where,
                        "data 为空——数据怎么造没写，下一轮复跑不了（等于一次性用例）")

            am = cell(cells, "anti_mock")
            if am is not None:
                a = am.strip()
                if a.lower() in ANTIMOCK_EMPTY:
                    rep.add("CASE-ANTIMOCK-MISSING", "ERROR", where,
                            "anti_mock 为空/占位——**这条用例拿什么证明「看到的不是 mock 造出来的」**"
                            "（独立通道复核 / 负向证据 / 环境指纹 / canary 数据，四类至少一类）")
                else:
                    with_antimock += 1

            src = cell(cells, "source")
            if src is not None:
                s = src.strip()
                head = s.split(":", 1)[0].strip() if ":" in s else ""
                if s in EMPTY_OK or head not in SOURCES:
                    rep.add("CASE-SOURCE-MISSING", "ERROR", where,
                            "source `%s` 不是五类来源之一（%s）——用例从哪来没写上，"
                            "失败后就没法把它钉回需求/发现/反馈"
                            % (s or "空", " / ".join(x + ":" for x in SOURCES)))
                else:
                    dm = DISC_RE.match(s)
                    if dm:
                        tc, fid = dm.group(1), dm.group(2)
                        if not os.path.isdir(os.path.join(base, "测试卡", tc)):
                            rep.add("CASE-SOURCE-DANGLING", "ERROR", where,
                                    "source 指向不存在的测试卡 `%s`" % tc)
                        elif find_has_finding(findings, tc, fid) is False:
                            rep.add("CASE-SOURCE-DANGLING", "ERROR", where,
                                    "source `%s#%s` 在 `测试卡/%s/发现项.md` 机读块里找不到——假登记"
                                    % (tc, fid, tc))
                    elif head == "反馈" and not ANY_ANCHOR_RE.search(s):
                        rep.add("CASE-FEEDBACK-NO-ANCHOR", "WARN", where,
                                "`反馈:` 没带任何锚点（card-NNN / tc-NNN / REQ-x.y）——"
                                "用户反复反馈的问题要能被对上号")

            last = cell(cells, "last")
            if last is not None:
                l = last.strip().lower()
                if l in EMPTY_OK:
                    rep.add("CASE-LAST-MISSING", "ERROR", where, "last 为空——跑没跑过、结果如何没回填")
                elif l not in LASTS:
                    rep.add("CASE-LAST-INVALID", "ERROR", where,
                            "last `%s` 不在 %s 内" % (l, "/".join(LASTS)))
                elif l == "unknown":
                    rep.add("CASE-LAST-UNKNOWN", "WARN", where,
                            "last=unknown（未回填）——**不许当 pass 用**")

    return rep, True, total, len(blocks), with_antimock


def hotspots(base, lib_path):
    """发现项热区：哪条路径反复出问题；每条有没有对应的回归用例。"""
    findings = scan_findings(base)
    if not findings:
        out("发现项热区：**未查过**——本轮没有任何 `%s` 机读块。" % FIND_START)
        out("（「未查过」≠「0 个热区」：没有机读块意味着机器看不见发现项，不意味着问题不存在。）")
        return 2

    covered = set()
    if os.path.isfile(lib_path):
        for _s, body, _c in find_blocks(read_text(lib_path).splitlines(), CASES_START, CASES_END):
            header, rows = parse_table(body)
            if not header:
                continue
            idx = {c: k for k, c in enumerate(header)}
            for _lineno, cells in rows:
                if "source" not in idx or idx["source"] >= len(cells):
                    continue
                m = DISC_RE.match(cells[idx["source"]].strip())
                if m:
                    covered.add("%s#%s" % (m.group(1), m.group(2)))

    agg = {}
    unanchored = 0
    for tc in sorted(findings):
        for row in findings[tc]["rows"]:
            key = anchor_prefix(row["anchor"])
            if not key:
                unanchored += 1
                key = "(锚点未知)"
            slot = agg.setdefault(key, {"n": 0, "sev": {}, "ids": []})
            slot["n"] += 1
            slot["sev"][row["severity"] or "?"] = slot["sev"].get(row["severity"] or "?", 0) + 1
            slot["ids"].append((tc, row["id"]))

    sev_cols = ["critical", "required", "nit", "fyi"]
    out("发现项热区（数据源：测试卡/*/发现项.md 的机读块；按发现项数降序）")
    out("")
    out("%-34s %4s  %s  %s" % ("路径前缀", "发现", "  ".join("%-8s" % c for c in sev_cols), "回归用例"))
    for key in sorted(agg, key=lambda k: (-agg[k]["n"], k)):
        slot = agg[key]
        has = sum(1 for tc, fid in slot["ids"] if "%s#%s" % (tc, fid) in covered)
        mark = "已覆盖 %d/%d" % (has, slot["n"])
        if slot["n"] - has:
            mark += "  ← 缺 %d" % (slot["n"] - has)
        out("%-34s %4d  %s  %s"
            % (key[:34], slot["n"],
               "  ".join("%-8s" % slot["sev"].get(c, 0) for c in sev_cols), mark))
    out("")
    if unanchored:
        out("锚点未知/未填的发现项：%d 条——**它们没法归并成热区**（补 `anchor` 才会进上表）。" % unanchored)
    missing = ["%s#%s" % (tc, fid) for tc in sorted(findings) for fid in findings[tc]["ids"]
               if "%s#%s" % (tc, fid) not in covered]
    if missing:
        out("尚无回归用例的发现项：%s" % ", ".join(missing))
        out("→ 这些正是**用例库该长的方向**（`source: 发现:<tc-NNN>#F-xx`）："
            "反复反馈/反复冒出来的问题，先给固定回归用例，再谈轮换探索。")
    else:
        out("所有发现项都已有回归用例。")
    return 0


def main(argv):
    root = None
    lib_opt = None
    as_json = False
    check_only = False
    want_hotspots = False
    allow_unmigrated = False

    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
        elif a == "--check":
            check_only = True
        elif a == "--hotspots":
            want_hotspots = True
        elif a == "--allow-unmigrated":
            allow_unmigrated = True
        elif a == "--lib":
            i += 1
            lib_opt = argv[i] if i < len(argv) else None
        elif a in ("-h", "--help"):
            out(__doc__.strip())
            return 0
        elif a.startswith("--"):
            out("未知参数：%s（见 --help）" % a)
            return 2
        else:
            root = a
        i += 1

    base = resolve_base(root)
    if base is None:
        out("项目根不存在或不可读：%s" % (root or os.getcwd()))
        return 2
    lib_path = os.path.abspath(lib_opt) if lib_opt else os.path.join(base, "代码", "测试", "用例库.md")

    if want_hotspots:
        if as_json:
            fs = scan_findings(base)
            out(json.dumps({"ok": bool(fs), "findings": fs}, ensure_ascii=False, indent=2))
            return 0 if fs else 2
        return hotspots(base, lib_path)

    rep, lib_exists, total, blocks, with_antimock = check_cases(base, lib_path, allow_unmigrated)

    if as_json:
        payload = {
            "version": VERSION,
            "base": base,
            "lib": lib_path,
            "lib_exists": lib_exists,
            "ok": None if not lib_exists else (rep.counts["ERROR"] == 0),
            "exit": (1 if rep.counts["ERROR"] else 2) if not lib_exists
                    else (1 if rep.counts["ERROR"] else 0),
            "blocks": blocks,
            "cases": total,
            "counts": rep.counts,
            "items": rep.items,
        }
        out(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload["exit"]

    if not lib_exists:
        for it in rep.items:
            out("[%s] %-24s %s  %s"
                % (SEV_LABEL[it["severity"]], it["code"], it["where"], it["message"]))
        out("")
        out("汇总：ERROR %d / WARN %d / INFO %d"
            % (rep.counts["ERROR"], rep.counts["WARN"], rep.counts["INFO"]))
        if rep.counts["ERROR"]:
            out("退出码 1 —— 有 ERROR：已有系统级测试卡却没有用例库，测试用例资产没落盘（v5.8.0）。")
            return 1
        out("退出码 2 —— **无法检查，不是「没问题」**：用例库还没建（%s）" % rel(base, lib_path))
        out("建库：复制 `代码/测试/用例库模板.md` → `代码/测试/用例库.md`（v5.8.0 起用例资产的唯一落点）。")
        return 2

    if check_only:
        out("用例库 %s：用例 %d 条，ERROR %d / WARN %d（退出码 %d）"
            % (rel(base, lib_path), total, rep.counts["ERROR"], rep.counts["WARN"],
               1 if rep.counts["ERROR"] else 0))
        return 1 if rep.counts["ERROR"] else 0

    out("== case_audit.py v%s ==" % VERSION)
    out("项目根：%s" % base)
    out("用例库：%s（%d 段机读块，%d 条用例）" % (rel(base, lib_path), blocks, total))
    out("")
    if not rep.items:
        out("无 ERROR / WARN：字段齐全、指针存在。（**只证明「填得对」，不证明「测得全」**——"
            "覆盖方向由项目经理出、范围由主控排，本脚本不判。）")
    else:
        for it in sorted(rep.items, key=lambda x: (SEV_ORDER[x["severity"]], x["where"])):
            out("[%-5s] %-24s %s" % (SEV_LABEL[it["severity"]], it["code"], it["where"]))
            out("        %s" % it["message"])
    out("")
    out("汇总：ERROR %d / WARN %d / INFO %d ｜ 带反 mock 判据的用例 %d/%d"
        % (rep.counts["ERROR"], rep.counts["WARN"], rep.counts["INFO"], with_antimock, total))
    if rep.counts["ERROR"]:
        out("退出码 1 —— 有 ERROR：打回测试工程师补齐（前置检查第 18 项）。")
        return 1
    out("退出码 0 —— 干净。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv))
