#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""regression_audit.py — 回归防线只读检查器（v5.28.0：修过的 bug 不许二进宫）

为什么有这个脚本
----------------
用户 2026-11-08 点出真实工程的痛点：「反复修改的情况下，新的一个版本会出现两个版本前
的问题」——**修过的 bug 在后续迭代中复发**。框架此前的相关资产散在三处、每处都不闭环：

  · 用例库（v5.8.0）有「固定回归基线」的说法，但它只是测试工程师 §15.2 的角色纪律，
    `last=fail` / `last=unknown` **不阻断任何收口**——基线挂了没人拦；
  · 修复卡（v5.7.0）有 `anchor`/`entry` 定位锚点，但置 done 没有任何条款要求
    「用例库里存在一条能复现该 bug 的哨兵用例」——**修复闭环不留防线**，bug 修完即裸奔；
  · 极速修复（v5.12.0）台账 `verified=是` 也不看有没有留哨兵——快档修过的问题同样裸奔。

v5.28.0 四条硬线（对应四个缺口，每条都有检查码）：

  线 1 哨兵绑定（test-on-fix）——修复卡置 done 的前提：`reg_case` 指向真实存在、
       修复后 `last=pass` 的 CAS；无法自动化复现的写 `reg_case: -` + `reg_note` 豁免。
       检查码 `SENTINEL-*`。
  线 2 基线全绿（绑收口关卡）——基线哨兵集 = `source` 前缀 ∈ {发现:,反馈:,回归:,探索:}
       且非 retired 的 CAS。`fail` 未处置 = ERROR；`unknown` = WARN（收口报告必须写明）；
       `deliverable` / `ship` 轮收口时哨兵集必须全 `pass`（接 #53）。
       检查码 `BASELINE-*`。
  线 3 二进宫处置——哨兵 `last=fail` = 旧 bug 复现 = 当轮最高优先级：必须建修复卡
       `source: 回归:CAS-NNN`（修好后复用同一条 CAS，`last` 翻回 pass）。
       检查码 `BASELINE-FAIL-*`。
  线 4 极速修复的债——台账行 `verified=是` 仍无哨兵 = ERROR（修过了连一条哨兵都没留）；
       `verified=否` = WARN 清单（下一轮正常测试的靶子，极速里省下的测试不得当成
       「已经测过」）。检查码 `TURBO-SENTINEL-*`。

`--impact`（碰撞复验，诚实边界内做提示不做强拦）：给定 anchor 路径（或修复卡号），
列出**同文件**的历史发现项（含已 fixed）与各自哨兵——修 B 前先看 A 的哨兵还在不在。
强制线落在人工对照：测试工程师 targeted 轮必须跑碰撞哨兵集，测试卡覆盖矩阵留痕。

诚实边界（本脚本核不了的，报告不得读成「没问题」）
  ① 这条用例是不是**真能复现那个 bug**——用例质量归反 mock 判据与测试工程师的专业判断；
  ② `last=pass` 是不是**真在新构建上跑出来的**——归证据留痕与三路验收；
  ③ 极速轮次 `verified=否` 期间防线敞开——债显式记 WARN 清单，不许沉默。

用法
----
    python -X utf8 scripts/regression_audit.py <项目根>                  # 只读体检
    python -X utf8 scripts/regression_audit.py <项目根> --json           # 机器可读
    python -X utf8 scripts/regression_audit.py <项目根> --impact <路径|card-NNN>

    退出码：0 干净 ｜ 1 有 ERROR ｜ 2 无法检查（**不是「没问题」**）
    （用例库 `project-state/代码/测试/用例库.md` 不存在 = 2——哨兵无处落，谈不上「干净」。）
"""

import json
import os
import re
import sys

VERSION = "5.28.0"

# ---------------------------------------------------------------- 契约常量
CASES_LIB = os.path.join("代码", "测试", "用例库.md")
CASES_START = "<!-- machine:cases -->"
CASES_END = "<!-- /machine:cases -->"
FIND_START = "<!-- machine:findings -->"
FIND_END = "<!-- /machine:findings -->"
TURBO_LEDGER = os.path.join("问题", "极速修复台账.md")
TURBO_OPEN_RE = re.compile(r"<!--\s*machine:turbo\s*-->")
TURBO_CLOSE_RE = re.compile(r"<!--\s*/machine:turbo\s*-->")

CASE_COLS = ["id", "scope", "method", "data", "anti_mock", "source", "last"]
FIND_COLS = ["id", "severity", "owner", "ref", "state", "anchor", "entry"]
TURBO_COLS = ["id", "at", "source", "basis", "symptom", "files", "depth",
              "evidence", "pack", "exit", "exit_reason", "verified"]

LASTS = ["pass", "fail", "skip", "unknown", "retired"]
# 基线哨兵集的 source 前缀（`AC:` 是需求基线，不属"修过的问题"防线；retired 已作废不算）
BASELINE_PREFIX = ("发现", "反馈", "回归", "探索")

CAS_RE = re.compile(r"^CAS-\d{3,}$")
CAS_ANY = re.compile(r"CAS-\d{3,}")
SRC_RE = re.compile(r"^(AC|发现|反馈|回归|探索):(.*)$", re.S)
CARD_NAME_RE = re.compile(r"^card-\d+$")

OPEN_STATUSES = ("todo", "in_progress", "reviewing", "blocked")
CLOSED_STATUSES = ("done", "reviewing")      # 置 done 前提：done 与其后的 reviewing 都受哨兵线管
EMPTY_SRC = ("", "-", "--", "''", '""')

SEV_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
SEV_LABEL = {"ERROR": "错误", "WARN": "告警", "INFO": "提示"}


def out(text=""):
    sys.stdout.write(text + "\n")


def read_text(path):
    """UTF-8 优先、GBK 兜底（先例：recall.py / case_audit.py / test_scope_audit.py）。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def split_cells(line, strip_edges=True):
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
    """返回 [(起始行号, [(行号, 原文), …], 是否闭合)]——只认整行完全匹配的围栏（先例：case_audit.py）。"""
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


def rows_by_header(header, rows, cols):
    """按表头名取列 → [(行号, {列名: 值})]；列名不在表头里 = 取不到（返回 None 占位）。"""
    out_rows = []
    for lineno, cells in rows:
        d = {}
        for name in cols:
            if name in header:
                idx = header.index(name)
                d[name] = cells[idx] if idx < len(cells) else ""
            else:
                d[name] = None
        out_rows.append((lineno, d))
    return out_rows


def uncomment(value):
    """剥掉 frontmatter 行内注释与包裹引号（先例：test_scope_audit.py）。"""
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
    """`---` 之间的键值 → ({键: 值}, 闭合行号)。无 frontmatter 返回 (None, 0)。"""
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
        fm[k.strip()] = uncomment(v)
    return None, 0


def resolve_base(root):
    """项目根 → project-state 目录（先例：case_audit.py）。"""
    if not root:
        root = os.getcwd()
    root = os.path.abspath(root)
    inner = os.path.join(root, "project-state")
    if os.path.isdir(inner):
        return inner
    if os.path.isdir(os.path.join(root, "需求卡")):
        return root
    return root if os.path.isdir(root) else None


def rel(base, path):
    try:
        return os.path.relpath(path, base).replace("\\", "/")
    except ValueError:
        return path


def norm_anchor(a):
    """anchor → 参与碰撞比对的文件路径（剥行号/符号后缀；`未知`/空 = 不参与）。"""
    a = (a or "").strip().replace("\\", "/")
    if not a or a in ("未知", "-", "--"):
        return ""
    a = a.split("#", 1)[0].strip()
    a = re.sub(r":\d+$", "", a).strip()
    while a.startswith("./"):
        a = a[2:]
    return a.rstrip("/")


class Report(object):
    def __init__(self):
        self.items = []
        self.counts = {"ERROR": 0, "WARN": 0, "INFO": 0}

    def add(self, code, sev, where, message):
        self.items.append({"code": code, "severity": sev, "where": where, "message": message})
        self.counts[sev] += 1


# ---------------------------------------------------------------- 数据加载
def load_cases(base):
    """读用例库机读块 → {"cases": {id: 行}, "where": 相对路径}；用例库不存在 → None。"""
    path = os.path.join(base, CASES_LIB)
    if not os.path.isfile(path):
        return None
    txt = read_text(path)
    lines = txt.splitlines()
    blocks = find_blocks(lines, CASES_START, CASES_END)
    if not blocks or not blocks[0][2]:
        return {"cases": None, "where": rel(base, path)}
    _start, body, _closed = blocks[0]
    header, raw_rows = parse_table(body)
    if header is None or not all(c in (header or []) for c in ("id", "source", "last")):
        return {"cases": None, "where": rel(base, path)}
    cases = {}
    for lineno, d in rows_by_header(header, raw_rows, CASE_COLS):
        cid = (d["id"] or "").strip()
        if not cid:
            continue
        cases[cid] = {
            "scope": (d["scope"] or "").strip(),
            "source": (d["source"] or "").strip(),
            "last": (d["last"] or "").strip(),
            "line": lineno,
        }
    return {"cases": cases, "where": rel(base, path)}


def load_findings(base):
    """扫全部 `测试卡/tc-*/发现项.md` 的 machine:findings → [ {tc,id,state,anchor,…} ]。"""
    findings = []
    tcdir = os.path.join(base, "测试卡")
    if not os.path.isdir(tcdir):
        return findings
    for name in sorted(os.listdir(tcdir)):
        path = os.path.join(tcdir, name, "发现项.md")
        if not os.path.isfile(path):
            continue
        lines = read_text(path).splitlines()
        for _start, body, closed in find_blocks(lines, FIND_START, FIND_END):
            if not closed:
                continue
            header, raw_rows = parse_table(body)
            if header is None:
                continue
            for _lineno, d in rows_by_header(header, raw_rows, FIND_COLS):
                fid = (d["id"] or "").strip()
                if not fid:
                    continue
                findings.append({
                    "tc": name,
                    "id": fid,
                    "severity": (d["severity"] or "").strip(),
                    "state": (d["state"] or "").strip(),
                    "anchor": (d["anchor"] or "").strip(),
                    "entry": (d["entry"] or "").strip(),
                    "where": rel(base, path),
                })
    return findings


def load_cards(base):
    """扫 `需求卡/card-*/card.md` frontmatter → [修复卡]（source 或 anchor 非空者）。"""
    cards = []
    cdir = os.path.join(base, "需求卡")
    if not os.path.isdir(cdir):
        return cards
    for name in sorted(os.listdir(cdir)):
        path = os.path.join(cdir, name, "card.md")
        if not os.path.isfile(path):
            continue
        fm, _end = parse_frontmatter(read_text(path))
        if fm is None:
            continue
        src = (fm.get("source") or "").strip()
        anc = (fm.get("anchor") or "").strip()
        if src in EMPTY_SRC and anc in EMPTY_SRC:
            continue  # 普通需求卡：不属修复卡（v5.7.0 口径：source/anchor 非空即修复卡）
        cards.append({
            "card": name,
            "status": (fm.get("status") or "").strip().lower(),
            "source": src,
            "anchor": anc,
            "entry": (fm.get("entry") or "").strip(),
            "reg_case": (fm.get("reg_case") or "").strip(),
            "reg_note": (fm.get("reg_note") or "").strip(),
            "where": rel(base, path),
        })
    return cards


def load_tcs(base):
    """扫 `测试卡/tc-*/tc.md` frontmatter（供 deliverable/ship 收口门）。"""
    tcs = []
    tcdir = os.path.join(base, "测试卡")
    if not os.path.isdir(tcdir):
        return tcs
    for name in sorted(os.listdir(tcdir)):
        path = os.path.join(tcdir, name, "tc.md")
        if not os.path.isfile(path):
            continue
        fm, _end = parse_frontmatter(read_text(path))
        if fm is None:
            continue
        tcs.append({
            "tc": name,
            "status": (fm.get("status") or "").strip().lower(),
            "delivery_use": (fm.get("delivery_use") or "").strip().lower(),
            "cadence_round": (fm.get("cadence_round") or "").strip().lower(),
            "where": rel(base, path),
        })
    return tcs


def load_turbo(base):
    """读极速修复台账 machine:turbo → {"rows": [...]}；台账不存在 → None；块缺失 → "noblock"。"""
    path = os.path.join(base, TURBO_LEDGER)
    if not os.path.isfile(path):
        return None
    txt = read_text(path)
    mo = TURBO_OPEN_RE.search(txt)
    mc = TURBO_CLOSE_RE.search(txt, mo.end()) if mo else None
    if not mo or not mc:
        return "noblock"          # 块缺失归 framework_lint K 组管，本脚本不重复报
    # TURBO 围栏允许正则空白（先例 framework_lint.py），不走精确 find_blocks，按行号切
    lines = txt.splitlines()
    open_line = txt[:mo.start()].count("\n")
    close_line = txt[:mc.start()].count("\n")
    body = [(k + 1, lines[k]) for k in range(open_line + 1, close_line)]
    header, raw_rows = parse_table(body)
    if header is None:
        return {"rows": []}
    rows = []
    for lineno, d in rows_by_header(header, raw_rows, TURBO_COLS):
        if not (d["id"] or "").strip():
            continue
        d["line"] = lineno
        rows.append(d)
    return {"rows": rows}


def baseline_of(cases):
    """基线哨兵集：source 前缀 ∈ {发现,反馈,回归,探索} 且非 retired → [(cid, row)]。"""
    bl = []
    for cid in sorted(cases):
        row = cases[cid]
        m = SRC_RE.match(row["source"] or "")
        if m and m.group(1) in BASELINE_PREFIX and row["last"] != "retired":
            bl.append((cid, row))
    return bl


# ---------------------------------------------------------------- 四条检查线
def check_sentinels(rep, cards, cases):
    """线 1 哨兵绑定：done/reviewing 的修复卡必须有真实存在、last=pass 的哨兵 CAS。

    返回 reg_flagged（已由哨兵线报过的 CAS 集合，供基线线去重）。
    """
    reg_flagged = set()
    gated = [c for c in cards if c["status"] in CLOSED_STATUSES]
    for c in gated:
        rc = c["reg_case"]
        if rc == "":
            rep.add("SENTINEL-MISSING", "ERROR", c["where"],
                    "**修复卡 `%s` 已 `%s` 却没有哨兵用例**（`reg_case` 空）——"
                    "修过的 bug 不留哨兵，后续迭代必然二进宫（用户 2026-11-08 的原话就是这条痛）。"
                    "补法：在 `代码/测试/用例库.md` 加一条能复现本 bug 的 CAS（`source` 按来源写 "
                    "`发现:` / `反馈:` / `探索:`），修复后跑一遍回填 `last=pass`，再把 CAS 号填进本卡 "
                    "`reg_case`；确实无法自动化复现的面（真人/声学/物理）写 `reg_case: -` 并在 "
                    "`reg_note` 写明豁免依据"
                    % (c["card"], c["status"]))
            continue
        if rc == "-":
            if not c["reg_note"]:
                rep.add("SENTINEL-WAIVER-EMPTY", "ERROR", c["where"],
                        "**修复卡 `%s` 用 `reg_case: -` 豁免哨兵却没有 `reg_note`**——"
                        "豁免必须写明为什么无法自动化复现（真人体验/声学/物理面），"
                        "空豁免 = 没有豁免，等于修完裸奔" % c["card"])
            continue
        if not CAS_RE.match(rc):
            rep.add("SENTINEL-FORMAT", "ERROR", c["where"],
                    "**修复卡 `%s` 的 `reg_case: %s` 不是 `CAS-NNN` 形状**——"
                    "哨兵指针必须指向用例库机读块里的真实用例" % (c["card"], rc))
            continue
        row = cases.get(rc)
        if row is None:
            rep.add("SENTINEL-DANGLING", "ERROR", c["where"],
                    "**修复卡 `%s` 的哨兵 `%s` 在用例库机读块里不存在**——"
                    "指不到的哨兵等于没有哨兵（同 v4.4.0「假保护」判据）"
                    % (c["card"], rc))
            reg_flagged.add(rc)
            continue
        if row["last"] != "pass":
            # 已有开着的回归修复卡在跟（source 含 回归:<CAS>）= 二进宫处置中，老卡不重复打
            open_reg = [x for x in cards
                        if ("回归:%s" % rc) in (x["source"] or "")
                        and x["status"] in OPEN_STATUSES]
            if open_reg:
                continue
            if row["last"] == "retired":
                rep.add("SENTINEL-RETIRED", "WARN", c["where"],
                        "**修复卡 `%s` 的哨兵 `%s` 已作废（retired）**——"
                        "作废哨兵不设防：功能还在就要换一条新哨兵并更新 `reg_case`；"
                        "功能确已下线，在 `reg_note` 写明依据" % (c["card"], rc))
            else:
                rep.add("SENTINEL-NOT-PASS", "ERROR", c["where"],
                        "**修复卡 `%s` 已 `%s`，但哨兵 `%s` 的 `last=%s` ≠ pass**——"
                        "哨兵必须在**修复后的构建**上跑绿并回填；没回绿就说修好了，"
                        "正是「修过的 bug 又回来了」的机器形状（`unknown` = 没回填，"
                        "不许顺手写 pass）" % (c["card"], c["status"], rc, row["last"] or "空"))
            reg_flagged.add(rc)
    return reg_flagged


def check_baseline(rep, cases, cards, reg_flagged, lib_where):
    """线 2 + 线 3：基线哨兵集 fail 必须有卡在跟（二进宫处置）；unknown 必须显式。"""
    bl = baseline_of(cases)
    for cid, row in bl:
        if row["last"] != "fail" or cid in reg_flagged:
            continue
        # 有开着的回归修复卡（source 含 回归:<CAS>）在跟 = 处置中，不打
        if any(("回归:%s" % cid) in (c["source"] or "") and c["status"] in OPEN_STATUSES
               for c in cards):
            continue
        referenced = [c for c in cards
                      if ("回归:%s" % cid) in (c["source"] or "") or c["reg_case"] == cid]
        if referenced:
            rep.add("BASELINE-FAIL-CARD-DONE", "ERROR", lib_where,
                    "**哨兵 `%s` 复挂（last=fail）而关联的修复卡都已收口**——"
                    "旧 bug 复现了：按**线 3**建修复卡 `source: 回归:%s`，"
                    "写清归因「哪个后续改动把它弄坏的」，修好后复用同一条 CAS 回填 pass"
                    % (cid, cid))
        else:
            rep.add("BASELINE-FAIL-NO-CARD", "ERROR", lib_where,
                    "**哨兵 `%s` 挂了（last=fail）却没有任何修复卡在跟**——"
                    "回归失败不许「顺手改掉不记」：建修复卡 `source: 回归:%s`，"
                    "`anchor`/`entry` 必填，当轮最高优先级" % (cid, cid))
    unknown = [cid for cid, row in bl if row["last"] == "unknown"]
    if unknown:
        shown = ", ".join(unknown[:8]) + ("…" if len(unknown) > 8 else "")
        rep.add("BASELINE-UNKNOWN", "WARN", "用例库",
                "**基线哨兵集有 %d 条 `last=unknown`（未回填）**：%s——"
                "`unknown` 是合法值，但**收口报告必须写明**（同 #53 `depth_note` 哲学："
                "没跑就是没跑，不许沉默，也不许顺手写 pass）" % (len(unknown), shown))
    return bl


def check_ship_gate(rep, tcs, bl):
    """线 2 的收口门：deliverable / ship 轮（已跑完的）基线哨兵必须全 pass（接 #53）。"""
    bl_map = dict(bl)
    for t in tcs:
        is_ship = t["delivery_use"] == "deliverable" or t["cadence_round"].startswith("ship")
        if not is_ship or t["status"] not in CLOSED_STATUSES:
            continue
        not_green = [cid for cid, row in bl if row["last"] != "pass"]
        if not_green:
            shown = ", ".join(not_green[:8]) + ("…" if len(not_green) > 8 else "")
            rep.add("BASELINE-SHIP-NOT-GREEN", "ERROR", t["where"],
                    "**`%s` 是交付/上线轮（`delivery_use=%s` / `cadence_round=%s`）且已收口，"
                    "但基线哨兵集没有全绿**：%s——交付包/上线前一轮必须 `full` 且哨兵全 `pass`"
                    "（skip/unknown/fail 都不行，接验收标准 #53）；这些面交给用户 = "
                    "把「用户当发现者」（#63 口径）"
                    % (t["tc"], t["delivery_use"] or "-", t["cadence_round"] or "-", shown))


def check_turbo(rep, turbo, cases):
    """线 4 极速修复的债：verified=是 必须有哨兵且 pass；verified=否 = 显式记账的 WARN。"""
    if turbo is None:
        rep.add("TURBO-NOT-USED", "INFO", "极速修复台账",
                "没有 `project-state/问题/极速修复台账.md`——本项目未用过极速修复模式，"
                "线 4 无对象（INFO，不是「没查」）")
        return
    if turbo == "noblock":
        return          # 块缺失由 framework_lint K 组报，本脚本不重复
    debt = []
    for r in turbo["rows"]:
        rid = (r["id"] or "").strip()
        verified = (r["verified"] or "").strip()
        basis = (r["basis"] or "").strip()
        where = "问题/极速修复台账.md:%s" % r.get("line", "?")
        cas_ids = CAS_ANY.findall(basis)
        if verified == "是":
            if not cas_ids:
                rep.add("TURBO-SENTINEL-MISSING", "ERROR", where,
                        "**极速修复行 `%s` 已独立复验（verified=是）却没留哨兵**（`basis` 里无 "
                        "CAS-NNN）——极速可以省建卡，不能省「修过的问题留下防复发用例」："
                        "在用例库补一条 CAS（`source: 反馈:<来源锚点>`），跑绿回填 pass，"
                        "把 CAS 号追加进本行 `basis`（v5.28.0 起 basis 允许 `CAS-NNN` 段）" % rid)
            else:
                for cid in cas_ids:
                    row = cases.get(cid)
                    if row is None:
                        rep.add("TURBO-SENTINEL-DANGLING", "ERROR", where,
                                "**极速修复行 `%s` 的哨兵 `%s` 在用例库里不存在**——"
                                "指不到的哨兵等于没有哨兵" % (rid, cid))
                    elif row["last"] != "pass":
                        rep.add("TURBO-SENTINEL-NOT-PASS", "ERROR", where,
                                "**极速修复行 `%s` 已 verified=是，但哨兵 `%s` 的 `last=%s` ≠ "
                                "pass**——复验通过就该把哨兵跑绿回填" % (rid, cid, row["last"]))
        elif verified == "否":
            debt.append(rid or "?")
    if debt:
        shown = ", ".join(debt[:8]) + ("…" if len(debt) > 8 else "")
        rep.add("TURBO-SENTINEL-DEBT", "WARN", "问题/极速修复台账.md",
                "**极速修复有 %d 行尚未独立复验（verified=否）**：%s——极速里省下的测试"
                "不得当成「已经测过」（v5.12.0 口径）：这些行就是**下一轮正常测试的靶子**，"
                "下一轮把对应面纳入 `targeted` 并补哨兵，跑绿后把 verified 翻成「是」"
                % (len(debt), shown))


# ---------------------------------------------------------------- --impact
def match_anchor(target, cand):
    """碰撞比对：归一化后相等，或互为路径尾部（容忍 `repos/` 前缀差异）。"""
    if not target or not cand:
        return False
    if target == cand:
        return True
    return cand.endswith("/" + target) or target.endswith("/" + cand)


def run_impact(base, target_arg):
    """同文件历史发现项 + 碰撞哨兵集（提示工具，退出码只有 0 / 2）。"""
    lib = load_cases(base)
    if lib is None or lib["cases"] is None:
        out("== regression_audit.py --impact v%s ==" % VERSION)
        out("用例库（%s）不存在或机读块不可读——无法检查，不是「没问题」。" % CASES_LIB)
        return 2
    cases = lib["cases"]
    target = ""
    origin = "命令行"
    if target_arg and CARD_NAME_RE.match(target_arg.strip()):
        cpath = os.path.join(base, "需求卡", target_arg.strip(), "card.md")
        if os.path.isfile(cpath):
            fm, _ = parse_frontmatter(read_text(cpath))
            target = norm_anchor((fm or {}).get("anchor") or "")
            origin = "%s 的 anchor" % target_arg.strip()
        if not target:
            out("卡 `%s` 不存在或没有可解析的 anchor——无法定位。" % target_arg)
            return 2
    else:
        target = norm_anchor(target_arg or "")
    if not target:
        out("没有可参与碰撞比对的路径（`未知`/空不算）。")
        return 2

    findings = load_findings(base)
    cards = load_cards(base)
    hit_findings = [f for f in findings if match_anchor(target, norm_anchor(f["anchor"]))]
    hit_cards = [c for c in cards if match_anchor(target, norm_anchor(c["anchor"]))]

    out("== regression_audit.py --impact v%s ==" % VERSION)
    out("项目根：%s" % base)
    out("目标路径：%s（来自%s）" % (target, origin))
    out("")
    out("同文件历史发现项：%d 条" % len(hit_findings))
    sentinel_ids = []
    for f in hit_findings:
        out("  %s#%s  state=%s  severity=%s" % (f["tc"], f["id"], f["state"] or "?", f["severity"] or "?"))
        out("    anchor: %s" % (f["anchor"] or "-"))
        for cid in sorted(cases):
            row = cases[cid]
            if ("发现:%s#%s" % (f["tc"], f["id"])) in (row["source"] or ""):
                tag = "" if row["last"] == "pass" else "   ← 未绿，先跑"
                out("    → 哨兵 %s  last=%s  source=%s%s" % (cid, row["last"], row["source"], tag))
                if row["last"] != "retired":
                    sentinel_ids.append(cid)
    if hit_cards:
        out("")
        out("同文件修复卡：%d 张" % len(hit_cards))
        for c in hit_cards:
            out("  %s  status=%s  reg_case=%s%s" % (
                c["card"], c["status"] or "?", c["reg_case"] or "-",
                ("（哨兵 last=%s）" % cases[c["reg_case"]]["last"]) if c["reg_case"] in cases else ""))
    out("")
    if sentinel_ids:
        out("【碰撞哨兵集】%s" % ", ".join(sorted(set(sentinel_ids))))
        out("  本轮 targeted 至少把上面这些哨兵跑一遍（覆盖矩阵留痕）——")
        out("  这个文件历史上修过 %d 个问题，改它之前先确认没把它们改坏。" % len(set(sentinel_ids)))
    else:
        out("该路径没有已知哨兵——若本次改动与历史修复同文件，仍建议补一条。")
    out("")
    out("诚实边界：本工具只做「登记过的锚点」的同文件比对；改的是**新文件**或发现项没写")
    out("anchor 时，它看不见——影响面判断仍是测试工程师的专业判断（#63 诚实边界同款）。")
    return 0


# ---------------------------------------------------------------- 汇总输出
def main(argv):
    root = None
    as_json = False
    impact_arg = None

    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
        elif a == "--impact":
            i += 1
            impact_arg = argv[i] if i < len(argv) else ""
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

    if impact_arg is not None:
        return run_impact(base, impact_arg)

    lib = load_cases(base)
    rep = Report()
    if lib is None:
        if as_json:
            out(json.dumps({"version": VERSION, "base": base, "ok": None, "exit": 2,
                            "counts": rep.counts, "items": [],
                            "reason": "用例库不存在——无法检查，不是「没问题」"},
                           ensure_ascii=False, indent=2))
        else:
            out("== regression_audit.py v%s ==" % VERSION)
            out("项目根：%s" % base)
            out("")
            out("[提示] CASES-LIB-MISSING  %s" % CASES_LIB)
            out("        用例库不存在——哨兵无处落，回归防线无从核起。")
            out("        **无法检查，不是「没问题」**（同 case_audit / test_scope_audit 口径）；")
            out("        落地方式见 `project-state/代码/测试/用例库模板.md`（v5.8.0）。")
            out("")
            out("退出码 2 —— 无法检查 ≠ 没问题")
        return 2

    cases = lib["cases"] or {}
    if lib["cases"] is None:
        rep.add("CASES-BLOCK-MISSING", "ERROR", lib["where"],
                "用例库存在但没有可读的 `machine:cases` 机读块（缺块/未闭合/列头缺 "
                "id|source|last）——哨兵核验无据（块写法见用例库模板 §3）")

    cards = load_cards(base)
    tcs = load_tcs(base)
    findings = load_findings(base)
    turbo = load_turbo(base)

    reg_flagged = check_sentinels(rep, cards, cases) if cases else set()
    bl = check_baseline(rep, cases, cards, reg_flagged, lib["where"]) if cases else []
    if cases:
        check_ship_gate(rep, tcs, bl)
    check_turbo(rep, turbo, cases)

    exit_code = 1 if rep.counts["ERROR"] else 0
    bl_stats = {k: 0 for k in LASTS}
    for _cid, row in bl:
        bl_stats[row["last"] if row["last"] in LASTS else "unknown"] += 1

    if as_json:
        out(json.dumps({
            "version": VERSION, "base": base,
            "ok": rep.counts["ERROR"] == 0, "exit": exit_code,
            "counts": rep.counts, "items": rep.items,
            "cases": len(cases), "findings": len(findings),
            "fix_cards": len(cards),
            "baseline": {"total": len(bl), **bl_stats},
            "turbo": (None if turbo is None else
                      ("noblock" if turbo == "noblock" else len(turbo["rows"]))),
        }, ensure_ascii=False, indent=2))
        return exit_code

    out("== regression_audit.py v%s ==" % VERSION)
    out("项目根：%s" % base)
    out("用例库：%d 条用例 ｜ 基线哨兵集 %d 条（pass %d / fail %d / unknown %d / skip %d）"
        % (len(cases), len(bl), bl_stats["pass"], bl_stats["fail"],
           bl_stats["unknown"], bl_stats["skip"]))
    out("修复卡 %d 张（done/reviewing %d 张受哨兵线管）｜ 发现项 %d 条 ｜ 极速台账 %s"
        % (len(cards), len([c for c in cards if c["status"] in CLOSED_STATUSES]),
           len(findings),
           "未使用" if turbo is None else ("块缺失(K组管)" if turbo == "noblock"
                                          else "%d 行" % len(turbo["rows"]))))
    out("")
    if not rep.counts["ERROR"] and not rep.counts["WARN"]:
        out("无 ERROR / WARN：哨兵线、基线线、二进宫处置、极速债四条线都干净。")
        out("（**只证明「登记在、指针真、回填了」**——用例质量与回填真实性不归本脚本，")
        out("  见文件头诚实边界。）")
        if rep.items:
            out("")
            for it in rep.items:
                out("[%-5s] %-24s %s" % (SEV_LABEL[it["severity"]], it["code"], it["where"]))
                out("        %s" % it["message"])
    else:
        for it in sorted(rep.items, key=lambda x: (SEV_ORDER[x["severity"]], x["where"])):
            out("[%-5s] %-24s %s" % (SEV_LABEL[it["severity"]], it["code"], it["where"]))
            out("        %s" % it["message"])
    out("")
    out("汇总：ERROR %d / WARN %d / INFO %d"
        % (rep.counts["ERROR"], rep.counts["WARN"], rep.counts["INFO"]))
    if rep.counts["ERROR"]:
        out("退出码 1 —— 有 ERROR：回归防线有缺口，收口前必须清零（验收标准 #68）。")
        return 1
    out("退出码 0 —— 干净。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv))
