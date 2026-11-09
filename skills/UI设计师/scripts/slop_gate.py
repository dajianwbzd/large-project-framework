#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""slop_gate.py —— 反 AI 默认风格门禁（零依赖）

规则**不在本文件里**：本脚本每次运行都现读 `../slop-gates.md` 的规则表，
取「该查哪几条 + 哪几条是 Floor」。判据实现在下面的 `_check_*` 函数里。

口径（与 Hallmark `gates.md` + `eval/score.mjs` 的分工一致）：
  规则正文（人读、可评审）→ slop-gates.md
  判据实现（机器执行）    → 本文件 `_check_<SG-nn>`
  集合与 Floor 标记        → 每次从 md 现读（所以「规则集合」永远不会漂移）

退出码：
  0  无违例（advisory / 豁免 / 跳过 不影响退出码，但都会列出来）
  1  有违例（`可判` 档规则命中）
  2  **无法完整检查**：用法错 / 读不到规则表 / 表里有规则而代码没实现（漂移）

用法：
  python -X utf8 slop_gate.py 契约/UI资源/directions [--floor-only] [--json]
  python -X utf8 slop_gate.py 契约/DESIGN.md 契约/UI资源/designs
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys

RULES_MD = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "slop-gates.md")
)
TARGET_EXTS = (".html", ".htm", ".css", ".md")
TEXT_RULES = ("SG-06", "SG-08")  # 文本类规则只对 html/css 生效（md 里可能是在描述规则）
FLOOR_MARKS = ("✅", "是", "Y", "y", "TRUE", "true", "yes")
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "dist", "build"}


class UsageError(Exception):
    pass


class RulesError(Exception):
    pass


# ---------------------------------------------------------------- 颜色工具


NAMED_COLORS = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "orange": (255, 165, 0),
    "purple": (128, 0, 128),
    "indigo": (75, 0, 130),
    "violet": (238, 130, 238),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "silver": (192, 192, 192),
    "whitesmoke": (245, 245, 245),
    "transparent": None,
}

_COLOR_RE = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|(?:rgba?|hsla?)\([^()]*\)", re.IGNORECASE
)


def _hex_to_rgb(tok):
    h = tok[1:]
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h[:3])
    elif len(h) in (6, 8):
        h = h[:6]
    else:
        return None
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return None


def _num(part, scale=255.0):
    part = part.strip()
    if part.endswith("%"):
        try:
            return float(part[:-1]) / 100.0 * scale
        except ValueError:
            return None
    try:
        return float(part)
    except ValueError:
        return None


def parse_color(tok):
    """'#abc' / 'rgb(1,2,3)' / 'hsl(250,60%,50%)' / 'white' → (r,g,b) 或 None"""
    if not tok:
        return None
    t = tok.strip().rstrip(";").lower()
    if t in NAMED_COLORS:
        return NAMED_COLORS[t]
    if t.startswith("#"):
        return _hex_to_rgb(t)
    m = re.match(r"rgba?\(([^)]*)\)$", t)
    if m:
        parts = [p for p in re.split(r"[,/\s]+", m.group(1).strip()) if p]
        if len(parts) < 3:
            return None
        rgb = [_num(parts[0]), _num(parts[1]), _num(parts[2])]
        if any(v is None for v in rgb):
            return None
        return tuple(int(max(0, min(255, round(v)))) for v in rgb)
    m = re.match(r"hsla?\(([^)]*)\)$", t)
    if m:
        parts = [p for p in re.split(r"[,/\s]+", m.group(1).strip()) if p]
        if len(parts) < 3:
            return None
        try:
            h = float(parts[0].rstrip("deg")) % 360.0
            s = float(parts[1].rstrip("%")) / 100.0
            l = float(parts[2].rstrip("%")) / 100.0
        except ValueError:
            return None
        return _hsl_to_rgb(h, s, l)
    return None


def _hsl_to_rgb(h, s, l):
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    mm = l - c / 2
    seg = int(h // 60) % 6
    table = {
        0: (c, x, 0.0),
        1: (x, c, 0.0),
        2: (0.0, c, x),
        3: (0.0, x, c),
        4: (x, 0.0, c),
        5: (c, 0.0, x),
    }
    r, g, b = table[seg]
    return tuple(int(max(0, min(255, round((v + mm) * 255)))) for v in (r, g, b))


def rgb_to_hsl(rgb):
    r, g, b = [v / 255.0 for v in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2.0
    d = mx - mn
    if d == 0:
        return 0.0, 0.0, l
    s = d / (1 - abs(2 * l - 1)) if abs(1 - abs(2 * l - 1)) > 1e-9 else 0.0
    if mx == r:
        h = 60 * (((g - b) / d) % 6)
    elif mx == g:
        h = 60 * ((b - r) / d + 2)
    else:
        h = 60 * ((r - g) / d + 4)
    return h % 360.0, s, l


# ---------------------------------------------------------------- 规则表读取


def read_rules(path=RULES_MD):
    if not os.path.isfile(path):
        raise RulesError("读不到规则表：%s" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise RulesError("读规则表失败：%s（%s）" % (path, exc))

    rules = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        m = re.search(r"SG-\d{2}", cells[0])
        if not m:
            continue
        if len(cells) < 5:
            raise RulesError("规则表列数不对（%s）：%s" % (m.group(0), line[:120]))
        rid = m.group(0).upper()
        tier_cell = cells[3]
        if "半可判" in tier_cell:
            tier = "半可判"
        elif "可判" in tier_cell:
            tier = "可判"
        elif "人判" in tier_cell:
            tier = "人判"
        else:
            raise RulesError("规则 %s 的「档」列无法识别：%s" % (rid, tier_cell))
        floor = any(mark in cells[4] for mark in FLOOR_MARKS)
        rules.append(
            {"id": rid, "name": cells[1], "criteria": cells[2], "tier": tier, "floor": floor}
        )
    if not rules:
        raise RulesError("规则表里没解析出任何规则（应以 `| `SG-01` |` 开头）：%s" % path)
    return rules


# ---------------------------------------------------------------- 文件收集


def collect_targets(paths):
    targets = []
    for p in paths:
        ap = os.path.abspath(p)
        if os.path.isdir(ap):
            for root, dirs, files in os.walk(ap):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                for f in sorted(files):
                    if f.lower().endswith(TARGET_EXTS):
                        targets.append(os.path.join(root, f))
        elif os.path.isfile(ap):
            if ap.lower().endswith(TARGET_EXTS):
                targets.append(ap)
        else:
            raise UsageError("路径不存在：%s" % p)
    return targets


# ---------------------------------------------------------------- 文本预处理

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_EXEMPT_RE = re.compile(
    r"<!--\s*slop-gate\s*:\s*(off|on)\b(.*?)-->", re.IGNORECASE | re.DOTALL
)
# 标记里的规则 ID 允许写成 `SG-02` / `[SG-02]` / `SG-02,SG-05`，后面可以跟理由文字
_EXEMPT_ID_RE = re.compile(r"SG-\d{2}", re.IGNORECASE)


def blank_comments(raw):
    """把注释换成同长空格（保留换行）——行号/偏移不变，注释里的字样不会误伤规则。"""

    def repl(m):
        seg = m.group(0)
        return re.sub(r"[^\n]", " ", seg)

    out = _HTML_COMMENT_RE.sub(repl, raw)
    out = _CSS_COMMENT_RE.sub(repl, out)
    return out


def exemptions(raw):
    """解析豁免区间 → [(start, end, scope_or_None)]，scope=None 表示全部规则。"""
    intervals = []
    open_at = None
    scope = None
    for m in _EXEMPT_RE.finditer(raw):
        kind = m.group(1).lower()
        ids = {x.upper() for x in _EXEMPT_ID_RE.findall(m.group(2))}
        if kind == "off":
            if open_at is None:
                open_at = m.end()
                scope = set(ids) if ids else None
        else:  # on
            if open_at is None:
                continue
            intervals.append([open_at, m.start(), scope])
            open_at = None
            scope = None
    if open_at is not None:
        intervals.append([open_at, len(raw), scope])
    return intervals


class FileCtx(object):
    def __init__(self, path, rel):
        self.path = path
        self.rel = rel
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                self.raw = fh.read()
        except OSError as exc:
            raise UsageError("读取文件失败：%s（%s）" % (path, exc))
        self.body = blank_comments(self.raw)
        self._nl = [i for i, ch in enumerate(self.body) if ch == "\n"]
        self.intervals = exemptions(self.raw)
        self.exempt_used = []
        self.exempt_rejected = []

    @property
    def is_md(self):
        return self.path.lower().endswith(".md")

    def line(self, pos):
        return bisect.bisect_left(self._nl, pos) + 1

    def snippet(self, pos, width=60):
        line = self.line(pos)
        start = self._nl[line - 2] + 1 if line >= 2 else 0
        end = self._nl[line - 1] if line - 1 < len(self._nl) else len(self.body)
        return self.body[start:end].strip()[:width]

    def exemption_for(self, rid, pos):
        """返回 None=不豁免；'exempt'=被豁免；'rejected'=想豁免 Floor 规则。"""
        for start, end, scope in self.intervals:
            if start <= pos < end:
                if scope is not None and rid not in scope:
                    continue
                return "exempt"
        return None


# ---------------------------------------------------------------- 各规则判据


def _px_values(text):
    vals = []
    for m in re.finditer(r"(-?\d+(?:\.\d+)?)px\b", text):
        vals.append(float(m.group(1)))
    return vals


def _gradient_args(body):
    """返回 [(start_pos, arg_text)]，支持括号嵌套。"""
    out = []
    for m in re.finditer(r"(?:repeating-)?(?:linear|radial|conic)-gradient\s*\(", body, re.IGNORECASE):
        i = m.end()
        depth = 1
        start = i
        while i < len(body) and depth > 0:
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
            i += 1
        out.append((m.start(), body[start : i - 1]))
    return out


def _check_sg01(body, ctx):
    """紫/靛蓝渐变成灾：渐变里紫蓝色标 > 50% 且渐变 ≥ 2 处。"""
    hits, unresolved, grad_n, stops_n, purple_n = [], 0, 0, 0, 0
    first_pos = None
    for pos, args in _gradient_args(body):
        colors = _COLOR_RE.findall(args)
        if not colors:
            continue
        grad_n += 1
        for tok in colors:
            rgb = parse_color(tok)
            if rgb is None:
                unresolved += 1
                continue
            stops_n += 1
            hue, sat, _l = rgb_to_hsl(rgb)
            if 240.0 <= hue <= 290.0 and sat >= 0.15:
                purple_n += 1
                if first_pos is None:
                    first_pos = pos
    if grad_n >= 2 and stops_n > 0 and purple_n / float(stops_n) > 0.5:
        hits.append(
            {
                "pos": first_pos or 0,
                "detail": "渐变 %d 处、色标 %d 个，其中紫蓝色标 %d 个（%.0f%% > 50%%）"
                % (grad_n, stops_n, purple_n, 100.0 * purple_n / stops_n),
            }
        )
    return hits, unresolved


def _check_sg02(body, ctx):
    """一切圆角：去重 ≥3 档、最小档 ≥8px、且无 ≤4px 档。"""
    hits, unresolved = [], 0
    values = []
    first_pos = None
    for m in re.finditer(r"border-radius\s*:\s*([^;}]+)", body, re.IGNORECASE):
        decl = m.group(1)
        if "var(" in decl:
            unresolved += 1
            continue
        if "%" in decl:
            # 百分比圆角（如 50%）是「容器级」破格，不计入 px 档位但记痕迹
            unresolved += 1
            continue
        px = _px_values(decl)
        if not px:
            continue
        for v in px:
            values.append(v)
        if first_pos is None:
            first_pos = m.start()
    distinct = sorted({round(v, 3) for v in values})
    if len(distinct) >= 3 and distinct[0] >= 8.0 and not any(v <= 4.0 for v in distinct):
        hits.append(
            {
                "pos": first_pos or 0,
                "detail": "圆角档位 %s —— 全部容器都圆、且没有一档是硬边（≤4px）"
                % ", ".join("%gpx" % v for v in distinct),
            }
        )
    return hits, unresolved


def _check_sg03(body, ctx):
    """千篇一律 hero + 卡片网格（半可判：只给线索）。"""
    hits, unresolved = [], 0
    hero = re.search(r"hero", body, re.IGNORECASE)
    if not hero:
        return hits, unresolved
    counts = {}
    for m in re.finditer(r"class\s*=\s*[\"']([^\"']+)[\"']", body, re.IGNORECASE):
        for tok in m.group(1).split():
            counts[tok] = counts.get(tok, 0) + 1
    if not counts:
        return hits, unresolved
    top_tok, top_n = max(counts.items(), key=lambda kv: kv[1])
    if top_n >= 6:
        hits.append(
            {
                "pos": hero.start(),
                "detail": "出现 hero，且 class `%s` 重复 %d 次 —— 是否千篇一律需人看（列表/表格也合理）"
                % (top_tok, top_n),
            }
        )
    return hits, unresolved


def _check_sg04(body, ctx):
    """超大内边距：padding 声明 ≥4 处，且 ≥96px 的占 70% 以上。"""
    hits, unresolved = [], 0
    decls, big = 0, 0
    first_pos = None
    for m in re.finditer(r"padding(?:-(?:top|right|bottom|left))?\s*:\s*([^;}]+)", body, re.IGNORECASE):
        decl = m.group(1)
        if "var(" in decl:
            unresolved += 1
            continue
        px = _px_values(decl)
        if not px:
            continue
        decls += 1
        if max(px) >= 96.0:
            big += 1
            if first_pos is None:
                first_pos = m.start()
    if decls >= 4 and big / float(decls) >= 0.7:
        hits.append(
            {
                "pos": first_pos or 0,
                "detail": "padding 声明 %d 处，其中 ≥96px 的 %d 处（%.0f%%）—— 内容被留白稀释"
                % (decls, big, 100.0 * big / decls),
            }
        )
    return hits, unresolved


def _shadow_blur(decl):
    decl = re.sub(r"\binset\b", " ", decl, flags=re.IGNORECASE)
    px = _px_values(decl)
    if not px:
        return None
    if len(px) >= 3:
        return px[2]
    if len(px) == 2:
        return 0.0
    return 0.0


def _check_sg05(body, ctx):
    """重度阴影：box-shadow 声明 ≥3 处，且每一处模糊半径都 ≥24px。"""
    hits, unresolved = [], 0
    decls, blurs = 0, []
    first_pos = None
    for m in re.finditer(r"box-shadow\s*:\s*([^;}]+)", body, re.IGNORECASE):
        decl = m.group(1).strip()
        if decl.lower().startswith("none"):
            continue
        if "var(" in decl:
            unresolved += 1
            continue
        blur = _shadow_blur(decl)
        if blur is None:
            unresolved += 1
            continue
        decls += 1
        blurs.append(blur)
        if first_pos is None:
            first_pos = m.start()
    if decls >= 3 and all(b >= 24.0 for b in blurs):
        hits.append(
            {
                "pos": first_pos or 0,
                "detail": "box-shadow %d 处，模糊半径全部 ≥24px（最小 %gpx）—— 所有层级都靠大阴影"
                % (decls, min(blurs)),
            }
        )
    return hits, unresolved


PLACEHOLDER_MARKERS = (
    "lorem ipsum",
    "示例文本",
    "示例内容",
    "待补充",
    "待填写",
    "your title here",
    "todo:",
    "todo：",
    "你的标题",
    "此处放图",
)


def _check_sg06(body, ctx):
    """占位文案（可判）。md 文件跳过（可能是在描述规则）。"""
    hits, unresolved = [], 0
    if ctx.is_md:
        return hits, unresolved
    low = body.lower()
    for marker in PLACEHOLDER_MARKERS:
        idx = low.find(marker)
        while idx != -1:
            hits.append({"pos": idx, "detail": "命中占位标记「%s」" % marker})
            idx = low.find(marker, idx + 1)
    return hits, unresolved


def _check_sg07(body, ctx):
    """一切居中（半可判）：center 声明 ≥5 处且占全部 text-align 的 80% 以上。"""
    hits, unresolved = [], 0
    total, center, first_pos = 0, 0, None
    for m in re.finditer(r"text-align\s*:\s*([^;}]+)", body, re.IGNORECASE):
        total += 1
        if "center" in m.group(1).lower():
            center += 1
            if first_pos is None:
                first_pos = m.start()
    if total >= 5 and center / float(total) >= 0.8:
        hits.append(
            {
                "pos": first_pos or 0,
                "detail": "text-align 声明 %d 处，其中 center %d 处（%.0f%%）—— 对齐方式是否被思考过"
                % (total, center, 100.0 * center / total),
            }
        )
    return hits, unresolved


_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]"
)


def _check_sg08(body, ctx):
    """emoji 当图标（半可判）：emoji 出现 ≥3 个。"""
    hits, unresolved = [], 0
    if ctx.is_md:
        return hits, unresolved
    found = list(_EMOJI_RE.finditer(body))
    if len(found) >= 3:
        hits.append(
            {
                "pos": found[0].start(),
                "detail": "emoji 字符 %d 个（如 %s）—— 可能是卖萌，也可能是没有图标体系"
                % (len(found), " ".join(m.group(0) for m in found[:5])),
            }
        )
    return hits, unresolved


CHECKS = {
    "SG-01": _check_sg01,
    "SG-02": _check_sg02,
    "SG-03": _check_sg03,
    "SG-04": _check_sg04,
    "SG-05": _check_sg05,
    "SG-06": _check_sg06,
    "SG-07": _check_sg07,
    "SG-08": _check_sg08,
}


# ---------------------------------------------------------------- 主流程


def run(paths, floor_only=False, rules_path=RULES_MD):
    rules = read_rules(rules_path)
    selected = [r for r in rules if (r["floor"] if floor_only else True)]
    ids = [r["id"] for r in rules]

    missing_impl = [r for r in selected if r["id"] not in CHECKS]
    orphan_impl = [rid for rid in CHECKS if rid not in ids]

    targets = collect_targets(paths)
    ctxs = [FileCtx(p, os.path.relpath(p)) for p in targets]

    violations, advisories, manual, unresolved_total = [], [], [], 0
    exempt_notes, rejected_notes = [], []

    for rule in selected:
        rid = rule["id"]
        fn = CHECKS.get(rid)
        if fn is None:
            continue
        if rule["tier"] == "人判":
            manual.append({"rule": rid, "name": rule["name"]})
            continue
        for ctx in ctxs:
            if rid in TEXT_RULES and ctx.is_md:
                continue
            hits, unresolved = fn(ctx.body, ctx)
            unresolved_total += unresolved
            for hit in hits:
                record = {
                    "rule": rid,
                    "name": rule["name"],
                    "file": ctx.rel,
                    "line": ctx.line(hit["pos"]),
                    "snippet": ctx.snippet(hit["pos"]),
                    "detail": hit["detail"],
                }
                verdict = ctx.exemption_for(rid, hit["pos"])
                if verdict == "exempt":
                    if rule["floor"]:
                        # Floor 规则不接受行内豁免：留痕被拒，违例照算（否则豁免=绕过门禁）
                        ctx.exempt_rejected.append((rid, record["line"]))
                    else:
                        ctx.exempt_used.append((rid, record["line"]))
                        continue
                if rule["tier"] == "可判":
                    violations.append(record)
                else:
                    advisories.append(record)

    for ctx in ctxs:
        for rid, line in ctx.exempt_used:
            exempt_notes.append({"rule": rid, "file": ctx.rel, "line": line})
        for rid, line in ctx.exempt_rejected:
            rejected_notes.append({"rule": rid, "file": ctx.rel, "line": line})

    if missing_impl or orphan_impl:
        status = "drift"
    elif violations:
        status = "fail"
    else:
        status = "pass"

    return {
        "rules_file": os.path.abspath(rules_path),
        "rules_total": len(rules),
        "rules_selected": [r["id"] for r in selected],
        "floor": [r["id"] for r in rules if r["floor"]],
        "mode": "floor-only" if floor_only else "full",
        "targets": [c.rel for c in ctxs],
        "violations": violations,
        "advisories": advisories,
        "manual": manual,
        "exemptions": exempt_notes,
        "rejected_exemptions": rejected_notes,
        "unresolved": unresolved_total,
        "missing_impl": [r["id"] for r in missing_impl],
        "orphan_impl": orphan_impl,
        "status": status,
        "exit": 2 if status == "drift" else (1 if violations else 0),
    }


def render(result):
    lines = []
    lines.append("反 AI 默认风格门禁 · 规则源 %s" % result["rules_file"])
    lines.append(
        "规则 %d 条（Floor %d 条：%s）｜模式 %s｜目标 %d 个文件"
        % (
            result["rules_total"],
            len(result["floor"]),
            ", ".join(result["floor"]),
            result["mode"],
            len(result["targets"]),
        )
    )
    lines.append("")

    if result["missing_impl"]:
        lines.append(
            "[ERROR] 规则表里有规则但本脚本没实现：%s —— 无法完整检查（补 `_check_*` 或从表里删）"
            % ", ".join(result["missing_impl"])
        )
    if result["orphan_impl"]:
        lines.append(
            "[WARN] 脚本实现了规则表里没有的规则：%s（不参与判定，但表是唯一来源，请对齐）"
            % ", ".join(result["orphan_impl"])
        )

    for v in result["violations"]:
        lines.append("[VIOLATION] %s %s  (%s:%d)" % (v["rule"], v["name"], v["file"], v["line"]))
        lines.append("  %s" % v["detail"])
        lines.append("  行内：%s" % v["snippet"])
    for a in result["advisories"]:
        lines.append("[ADVISORY]  %s %s  (%s:%d)" % (a["rule"], a["name"], a["file"], a["line"]))
        lines.append("  %s" % a["detail"])
    for m in result["manual"]:
        lines.append("[人判] %s %s —— 本脚本不表态，须人看" % (m["rule"], m["name"]))
    for e in result["exemptions"]:
        lines.append("[豁免] %s (%s:%d) —— 豁免≠没命中，报告里要单列理由" % (e["rule"], e["file"], e["line"]))
    for r in result["rejected_exemptions"]:
        lines.append("[豁免被拒] %s (%s:%d) —— Floor 规则不接受行内豁免" % (r["rule"], r["file"], r["line"]))
    if result["unresolved"]:
        lines.append(
            "[跳过] %d 处取值无法求值（var()/%/非 px）—— **跳过不算通过**" % result["unresolved"]
        )

    lines.append("")
    verdict = {
        "pass": "通过",
        "fail": "未过（先改，或按 slop-gates.md 的豁免规范留痕）",
        "drift": "无法判定：规则表与实现漂移（退出码 2）",
    }[result["status"]]
    lines.append(
        "结论：VIOLATION %d / ADVISORY %d / 豁免 %d / 跳过 %d → %s"
        % (
            len(result["violations"]),
            len(result["advisories"]),
            len(result["exemptions"]),
            result["unresolved"],
            verdict,
        )
    )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="反 AI 默认风格门禁（规则来自 slop-gates.md，每次现读）"
    )
    ap.add_argument("paths", nargs="+", help="文件或目录（目录会递归扫 .html/.css/.md）")
    ap.add_argument("--floor-only", action="store_true", help="只跑 Floor 子集（最小闸门）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--rules", default=RULES_MD, help="规则表路径（默认同目录的 slop-gates.md）")
    args = ap.parse_args(argv)

    try:
        result = run(args.paths, floor_only=args.floor_only, rules_path=args.rules)
    except RulesError as exc:
        sys.stderr.write("[ERROR] %s\n" % exc)
        return 2
    except UsageError as exc:
        sys.stderr.write("[ERROR] %s\n" % exc)
        return 2

    if args.json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(render(result) + "\n")
    return result["exit"]


if __name__ == "__main__":
    sys.exit(main())
