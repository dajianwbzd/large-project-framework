#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""design_lint.py — DESIGN.md 设计契约的零依赖校验器（v3.20.0）

定位：`@google/design.md lint` 的**零依赖兜底**。官方 CLI 能力更全（含 diff / export），
优先用它；本脚本用于没有 Node 环境、或 npm 镜像不可达时，让设计契约仍然**可被机器校验**——
设计门禁不因工具缺失而失效（对齐框架既有"零依赖可视化兜底" arch_mermaid.py 的做法）。

用法：
    python design_lint.py <DESIGN.md>            # 人类可读输出
    python design_lint.py <DESIGN.md> --json     # 结构化输出（与官方 lint 同构：findings + summary）
    python design_lint.py <目录> --json           # 校验目录下所有 DESIGN.md

退出码：0 = 无 error；1 = 有 error（可作 CI / done 门禁）；2 = 用法或读取错误

校验项（对应官方 lint 的主要规则）：
    structure          frontmatter 存在且被 --- 包裹
    invalid-color      色值非法（支持 hex / rgb / hsl / oklch / 常用颜色名）
    missing-primary    未定义 primary 色
    broken-ref         {a.b.c} 引用不存在的令牌
    invalid-subtoken   components.* 下出现未识别的子令牌
    orphaned-tokens    令牌已定义但无任何组件引用
    contrast-ratio     组件 textColor/backgroundColor 对比度低于 WCAG AA
    section-order      正文章节顺序不符合规范（顺序锁定，可省略）
    missing-section    缺少必需章节（Overview / Colors）
"""

from __future__ import annotations

import json
import os
import re
import sys

# ---------------------------------------------------------------- 常量

# 组件允许的子令牌（与 @google/design.md 的报错信息一致）
VALID_SUBTOKENS = {
    "backgroundColor", "textColor", "typography",
    "rounded", "padding", "size", "height", "width",
}

# 正文章节的规范顺序（可省略，但出现时顺序锁定）
CANONICAL_SECTIONS = [
    "overview", "colors", "typography", "layout",
    "elevation & depth", "shapes", "components", "do's and don'ts",
]
REQUIRED_SECTIONS = ["overview", "colors"]

# 令牌分组（frontmatter 顶层）
TOKEN_GROUPS = ["colors", "typography", "rounded", "spacing"]

NAMED_COLORS = {
    "black", "white", "red", "green", "blue", "gray", "grey",
    "transparent", "currentcolor",
}

# 官方 lint 对"规范色角色"豁免 orphaned-tokens：这些名字即使未被组件引用也不报孤儿。
# 名单由**实测官方 CLI 反推**得到（@google/design.md v0.4.0，2026-09-10 逐个探测）：
#   exempt: primary / secondary / tertiary / error / surface / background / outline
#   orphan: onSurface / surfaceVariant / surfaceTint / outlineVariant / inverseSurface /
#           text / focus / neutral / brand / link / divider / overlay / disabled / …
# 为什么必须补这份名单：兜底工具与官方**同判**是本脚本的立身之本（见文件头"定位"）。
# 缺了它，兜底会把 surface/background/error 等误报为孤儿 → 比官方更严 → 下游按兜底"修"契约，
# 反而把正确的设计改坏（"用错判据比不判更危险"）。名单变化时须重新反推。
ORPHAN_EXEMPT_COLORS = {
    "primary", "secondary", "tertiary", "error", "surface", "background", "outline",
}

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
FUNC_RE = re.compile(r"^(?:rgb|rgba|hsl|hsla|oklch|oklab|lab|lch|color)\s*\(", re.I)
REF_RE = re.compile(r"\{([A-Za-z0-9_.\-]+)\}")


# ---------------------------------------------------------------- 极简 YAML 子集解析
# DESIGN.md 的 frontmatter 是受控子集：仅嵌套映射 + 标量，无需完整 YAML。
# 不引入 PyYAML，保证零依赖（框架脚本一律纯标准库）。

def _scalar(text: str):
    """解析一个标量，返回 (值, 是否有引号)。

    需同时处理两件易错事：
    1) **行尾 YAML 注释**：`primary: "#1A1C1E"  # 说明` → 值应为 `#1A1C1E`（`#` 是颜色符号，不能一刀切）
    2) **引号内的 `#`**：`"#ffffff"` 中的 `#` 不是注释开始
    """
    t = text.strip()
    if not t:
        return "", False

    # 带引号：扫描到配对的收尾引号，其余（含行尾注释）丢弃
    if t[0] in "\"'":
        quote = t[0]
        out, i, escaped = [], 1, False
        while i < len(t):
            c = t[i]
            if escaped:
                out.append(c)
                escaped = False
            elif c == "\\" and quote == '"':
                escaped = True
            elif c == quote:
                break
            else:
                out.append(c)
            i += 1
        return "".join(out), True

    # 不带引号：从"空白 + #"处截断（行首 # 亦视为注释）
    if t.startswith("#"):
        return "", False
    m = re.search(r"\s+#", t)
    if m:
        t = t[: m.start()]
    return t.strip(), False


def parse_frontmatter(text: str):
    """返回 (数据字典, 错误列表)。仅支持本规范用到的 YAML 子集。"""
    errors = []
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    if not m:
        return None, ["未找到 frontmatter：文件须以 '---' 行开始、以 '---' 行结束"]
    body = m.group(1)

    data = {}
    # 栈：(缩进量, 容器)
    stack = [(-1, data)]
    for raw in body.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()

        # 折叠标量（>-, >, |）用作说明文本，跳过其缩进内容
        if line.endswith((">-", ">", "|-", "|")) and ":" in line:
            key = line.split(":", 1)[0].strip()
            while stack and indent <= stack[-1][0]:
                stack.pop()
            stack[-1][1][key] = ""
            continue

        if ":" not in line:
            # 折叠块的续行——忽略（不承载令牌）
            continue

        key, _, value = line.partition(":")
        key = _scalar(key)[0]
        value = value.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        if value == "":
            node = {}
            stack[-1][1][key] = node
            stack.append((indent, node))
        else:
            val, _ = _scalar(value)
            stack[-1][1][key] = val
    return data, errors


# ---------------------------------------------------------------- 颜色与对比度

def parse_color(value: str):
    """把颜色解析为 (r, g, b) 0-255；不支持的写法返回 None。"""
    if not isinstance(value, str):
        return None
    v = value.strip()
    low = v.lower()
    if low in NAMED_COLORS:
        return {"black": (0, 0, 0), "white": (255, 255, 255)}.get(low)
    if HEX_RE.match(v):
        h = v[1:]
        if len(h) in (3, 4):
            r, g, b = (int(c * 2, 16) for c in h[:3])
            return r, g, b
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    if FUNC_RE.match(v):
        nums = re.findall(r"[-+]?\d*\.?\d+%?", v)
        if low.startswith(("rgb",)) and len(nums) >= 3:
            def ch(x):
                if x.endswith("%"):
                    return round(float(x[:-1]) * 255 / 100)
                return round(float(x))
            return ch(nums[0]), ch(nums[1]), ch(nums[2])
        if low.startswith(("hsl", "hsla")) and len(nums) >= 3:
            return _hsl_to_rgb(float(nums[0]), float(nums[1].rstrip("%")) / 100,
                               float(nums[2].rstrip("%")) / 100)
        if low.startswith(("oklch", "oklab")):
            # 近似可判：oklch L 为感知亮度，L>=0.7 视为浅色、L<=0.35 视为深色
            try:
                l = float(nums[0].rstrip("%")) if nums else 0.5
            except ValueError:
                return None
            g = round(max(0.0, min(1.0, l)) * 255)
            return g, g, g
    return None


def _hsl_to_rgb(h, s, l):
    h = (h % 360) / 360.0
    if s == 0:
        g = round(l * 255)
        return g, g, g

    def hue(p, q, t):
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    return (round(hue(p, q, h + 1 / 3) * 255),
            round(hue(p, q, h) * 255),
            round(hue(p, q, h - 1 / 3) * 255))


def _luminance(rgb):
    def f(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg):
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return round((hi + 0.05) / (lo + 0.05), 2)


# ---------------------------------------------------------------- 校验

def lint_text(text: str):
    findings = []

    def add(severity, message, path=None, rule=None):
        f = {"severity": severity, "message": message}
        if path:
            f["path"] = path
        if rule:
            f["rule"] = rule
        findings.append(f)

    data, errs = parse_frontmatter(text)
    if data is None:
        for e in errs:
            add("error", e, None, "structure")
        return findings

    # --- 1. 结构
    if not data.get("name"):
        add("warning", "缺少 name：读者（人与 AI）无从称呼这套设计语言。", "name", "structure")

    # --- 2. 令牌收集与色值合法性
    defined = set()
    for group in TOKEN_GROUPS:
        node = data.get(group)
        if node is None:
            continue
        if not isinstance(node, dict):
            add("error", f"'{group}' 应为令牌映射，实际是标量。", group, "structure")
            continue
        for token, val in node.items():
            defined.add(f"{group}.{token}")
            if group == "colors":
                if parse_color(val) is None:
                    add("error",
                        f"'{val}' 不是合法颜色。期望 CSS 颜色值"
                        "（如 #ffffff、rgb(0 0 0)、oklch(0.5 0.2 240)）。",
                        f"colors.{token}", "invalid-color")

    colors = data.get("colors") or {}
    # 与官方同判：primary 键存在但色值非法 → 视同未定义（官方在此时报 missing-primary）
    primary_ok = isinstance(colors, dict) and parse_color(colors.get("primary")) is not None
    if isinstance(colors, dict) and not primary_ok:
        add("warning",
            "未定义 'primary' 色：AI 会自行生成主色，你对调色的控制力下降。",
            "colors", "missing-primary")

    components = data.get("components") or {}
    if not isinstance(components, dict):
        add("error", "'components' 应为映射。", "components", "structure")
        components = {}

    # --- 3. 组件子令牌 + 引用解析 + 对比度
    referenced = set()
    for comp, tokens in components.items():
        if not isinstance(tokens, dict):
            add("error", f"组件 '{comp}' 应为映射。", f"components.{comp}", "structure")
            continue
        for sub, val in tokens.items():
            if sub not in VALID_SUBTOKENS:
                add("warning",
                    f"'{sub}' 不是可识别的组件子令牌。合法子令牌："
                    f"{', '.join(sorted(VALID_SUBTOKENS))}。",
                    f"components.{comp}.{sub}", "invalid-subtoken")
            for ref in REF_RE.findall(str(val)):
                if ref in defined:
                    referenced.add(ref)
                else:
                    add("error",
                        f"引用 {{{ref}}} 无法解析到任何已定义令牌。",
                        f"components.{comp}", "broken-ref")

        bg, fg = tokens.get("backgroundColor"), tokens.get("textColor")
        if bg and fg:
            cbg, cfg = _resolved(bg, colors), _resolved(fg, colors)
            if cbg and cfg:
                ratio = contrast_ratio(cfg, cbg)
                if ratio < 4.5:
                    add("warning",
                        f"textColor ({fg}) 在 backgroundColor ({bg}) 上的对比度为 "
                        f"{ratio:.2f}:1，低于 WCAG AA 的 4.5:1。",
                        f"components.{comp}", "contrast-ratio")

    # --- 4. 孤儿令牌：定义但无人引用
    # 口径对齐：官方 lint 只把 **颜色** 孤儿报为 warning（rule: orphaned-tokens），
    # 其余令牌组不报。兜底工具必须与官方同判（同判据原则），故非颜色组降为 info 提示。
    # 另：官方对"规范色角色"（ORPHAN_EXEMPT_COLORS）豁免，无论是否被引用都不报——同判需照办。
    for group in TOKEN_GROUPS:
        node = data.get(group) or {}
        if not isinstance(node, dict):
            continue
        for token in node:
            key = f"{group}.{token}"
            if key in referenced:
                continue
            if group == "colors":
                if token in ORPHAN_EXEMPT_COLORS:
                    continue
                add("warning",
                    f"'{token}' 已定义但未被任何组件引用。",
                    key, "orphaned-tokens")
            else:
                add("info",
                    f"提示（非官方规则）：'{token}' 未被任何组件引用，"
                    f"若仅用于正文说明可忽略。",
                    key, "orphaned-tokens-advisory")

    # --- 5. 正文章节：顺序锁定 + 缺节提示
    # 规范规定**各节均可省略（omittable），只在出现时锁顺序**——故缺节不是 error，
    # 官方 lint 亦不报；此处仅给 info 提示，避免兜底工具比官方更严而破坏"同判据"。
    headings = [h.strip().lower() for h in
                re.findall(r"^##[ \t]+(.+?)[ \t]*$", text, re.MULTILINE)]
    idx = -1
    for h in headings:
        if h in CANONICAL_SECTIONS:
            i = CANONICAL_SECTIONS.index(h)
            if i < idx:
                add("warning",
                    f"章节顺序不符规范：'{h}' 出现在更靠后的章节之后。"
                    f"规范顺序为 {' → '.join(CANONICAL_SECTIONS)}（可省略，不可乱序）。",
                    f"§{h}", "section-order")
            idx = max(idx, i)
    omitted = data.get("omitted") or []
    omitted_txt = str(omitted).lower()
    for need in REQUIRED_SECTIONS:
        if need not in headings and need not in omitted_txt:
            add("info",
                f"提示（非官方规则）：未写 '{need}' 章节。规范允许省略，"
                f"若有意省略建议在 frontmatter 的 omitted 中声明。",
                f"§{need}", "missing-section-advisory")

    # --- 6. 摘要
    n_colors = len(colors) if isinstance(colors, dict) else 0
    n_typo = len(data.get("typography") or {})
    n_round = len(data.get("rounded") or {})
    n_space = len(data.get("spacing") or {})
    add("info",
        f"设计系统定义了 {n_colors} 个颜色、{n_typo} 组字体、{n_round} 级圆角、"
        f"{n_space} 个间距、{len(components)} 个组件。", None, "token-summary")

    return findings


def _resolved(value, colors):
    """把 {colors.x} 或裸色值解析成 rgb 三元组。"""
    if not isinstance(value, str):
        return None
    m = REF_RE.fullmatch(value.strip())
    if m:
        ref = m.group(1)
        if ref.startswith("colors."):
            return parse_color(colors.get(ref.split(".", 1)[1]))
        return None
    return parse_color(value)


def summarize(findings):
    counts = {"errors": 0, "warnings": 0, "infos": 0}
    for f in findings:
        sev = f.get("severity")
        if sev == "error":
            counts["errors"] += 1
        elif sev == "warning":
            counts["warnings"] += 1
        else:
            counts["infos"] += 1
    return counts


# ---------------------------------------------------------------- 入口

def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    as_json = "--json" in argv
    if len(args) != 1:
        print(__doc__)
        return 2
    target = args[0]

    paths = []
    if os.path.isdir(target):
        for root, _dirs, files in os.walk(target):
            for fn in files:
                if fn == "DESIGN.md":
                    paths.append(os.path.join(root, fn))
        paths.sort()
    else:
        paths = [target]

    if not paths:
        print(f"[ERROR] 未找到 DESIGN.md：{target}", file=sys.stderr)
        return 2

    total = {"errors": 0, "warnings": 0, "infos": 0}
    per_file = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            print(f"[ERROR] 读取失败 {p}: {e}", file=sys.stderr)
            return 2
        findings = lint_text(text)
        counts = summarize(findings)
        for k in total:
            total[k] += counts[k]
        per_file.append((p, findings, counts))

    if as_json:
        payload = {"files": [
            {"path": p, "findings": f, "summary": c} for p, f, c in per_file
        ], "summary": total}
        if len(per_file) == 1:
            payload = {"findings": per_file[0][1], "summary": total}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for p, findings, counts in per_file:
            print(f"# {p}")
            if not findings:
                print("  （无发现）")
            for f in findings:
                loc = f.get("path") or "-"
                print(f"  [{f['severity'].upper():7}] {loc}: {f['message']}")
            print(f"  → errors={counts['errors']} warnings={counts['warnings']} "
                  f"infos={counts['infos']}")
        if len(per_file) > 1:
            print(f"\n合计：errors={total['errors']} warnings={total['warnings']} "
                  f"infos={total['infos']}")

    return 1 if total["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
