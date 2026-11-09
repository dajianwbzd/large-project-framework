# -*- coding: utf-8 -*-
"""UX 门 L1 静态检查（框架通用版 v5.17.0）——不启动应用，毫秒级，先把"继承冲突类"拦住。

设计约束（来自 `02_方法论` 风险 3）：**L1 必须 <1s**，否则会被跳过。
所以本脚本只做文本级判定，不解析 CST、不起浏览器、不读图片。

条款从 `clauses.json`（schema `ux-clauses/1.0`）来，**规则名写在条款里**（`check_rule`），
参数写在 `params`——脚本本身不含任何项目的类名/选择器/端口，换个项目只改 JSON。

用法：
    python -X utf8 scripts/ux_gate/static_check.py <项目根> [--clauses <路径>] [--json-out <路径>]
    python -X utf8 scripts/ux_gate/static_check.py <项目根> --list-rules

退出码：0 = 无 fail；1 = 有 fail；2 = 用法/条款文件错误。
`skipped` 不计失败，但**必须带原因**（未实现的规则不许静默通过）。
"""
import argparse
import fnmatch
import glob
import json
import os
import re
import sys
import time

SCHEMA = "ux-clauses/1.0"
OUT_SCHEMA = "ux-gate-static/1.0"
DEFAULT_CLAUSES = os.path.join("project-state", "契约", "ux", "clauses.json")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             "dist-electron", "out", "coverage", ".idea", ".vscode", "交付"}
DEFAULT_GLOBS = ["*.html", "*.htm", "*.css", "*.scss", "*.js", "*.mjs", "*.cjs",
                 "*.jsx", "*.ts", "*.tsx", "*.vue", "*.svelte"]


# ---------------------------------------------------------------- 读条款
def load_clauses(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit("clauses 文件根必须是对象：%s" % path)
    schema = data.get("schema") or data.get("$schema")
    if schema != SCHEMA:
        raise SystemExit("clauses schema 必须是 %r，实际 %r：%s" % (SCHEMA, schema, path))
    clauses = data.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        raise SystemExit("clauses 必须是非空数组：%s" % path)
    return data, clauses


def preflight(clauses):
    """条款自身齐备性（缺 id / 缺判据 = 伪条款，直接算 fail）。"""
    problems = []
    seen = set()
    for i, c in enumerate(clauses):
        cid = c.get("id")
        if not cid:
            problems.append("第 %d 条缺 id" % (i + 1))
            continue
        if not re.match(r"^UX-[A-Z][A-Z0-9]*-\d{2}$", cid):
            problems.append("%s 的 id 不符合 UX-<节缩码>-NN" % cid)
        if cid in seen:
            problems.append("条款 id 重复：%s" % cid)
        seen.add(cid)
        for key in ("title", "clause", "judgment", "layer", "check_kind"):
            if not c.get(key):
                problems.append("%s 缺字段 %s" % (cid, key))
        if c.get("layer") not in ("L1", "L2", "L3", "L4", "L1+L2", "L2+L3", "L2+L4", "L1+L4"):
            problems.append("%s 的 layer 非法：%r" % (cid, c.get("layer")))
    return problems


# ---------------------------------------------------------------- 文件集合
def collect_files(root, globs):
    out = []
    for pat in globs:
        for path in glob.glob(os.path.join(root, "**", pat), recursive=True):
            if os.path.isdir(path):
                continue
            parts = set(os.path.normpath(path).split(os.sep))
            if parts & SKIP_DIRS:
                continue
            out.append(path)
    return sorted(set(out))


class Ctx:
    def __init__(self, root, globs):
        self.root = root
        self.globs = list(globs or DEFAULT_GLOBS)
        self._files = collect_files(root, self.globs)
        self._cache = {}
        self.clauses = []
        self.contract = ""

    def files(self):
        for p in self._files:
            if p not in self._cache:
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        self._cache[p] = fh.read()
                except OSError:
                    self._cache[p] = ""
        return [(p, self._cache[p]) for p in self._files]

    def rel(self, path):
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def hits(self, pattern, flags=0):
        """在文件集合里搜正则，返回 ['相对路径:行号: 片段']。"""
        rx = re.compile(pattern, flags)
        out = []
        for path, text in self.files():
            for n, line in enumerate(text.splitlines(), 1):
                m = rx.search(line)
                if m:
                    out.append("%s:%d: %s" % (self.rel(path), n, m.group(0)[:80]))
        return out


def brief(items, limit=4):
    if not items:
        return ""
    head = "；".join(items[:limit])
    return head + ("（共 %d 处）" % len(items) if len(items) > limit else "")


# ---------------------------------------------------------------- 规则库
RULES = {}


def rule(name, doc, defaults=None):
    def deco(fn):
        RULES[name] = {"name": name, "doc": doc, "defaults": defaults or {}, "fn": fn}
        return fn
    return deco


@rule("unique_declaration", "某属性（如栅格/布局模板）只允许在一个文件里声明",
      {"property": "grid-template-rows", "globs": ["*.css", "*.scss"]})
def r_unique_declaration(ctx, p):
    prop = p["property"]
    files = set()
    for path, text in ctx.files():
        if not any(fnmatch.fnmatch(os.path.basename(path), g) for g in p["globs"]):
            continue
        if re.search(r"(?<![\w-])%s\s*:" % re.escape(prop), text):
            files.add(ctx.rel(path))
    if not files:
        return False, "未找到 %s 的任何声明（条款没有对象可判）" % prop
    if len(files) > 1:
        return False, "%s 在 %d 个文件里重复声明：%s" % (prop, len(files), brief(sorted(files)))
    return True, "%s 只在 %s 声明" % (prop, sorted(files)[0])


@rule("explicit_value", "指定选择器的规则块里必须显式声明某属性=某值（防继承/默认值被改）",
      {"selectors": [".topbar-actions"], "property": "flex-wrap", "value": "nowrap",
       "globs": ["*.css", "*.scss"]})
def r_explicit_value(ctx, p):
    prop, want = p["property"], p["value"]
    missing = []
    for sel in p["selectors"]:
        found = False
        pat = re.compile(r"%s\s*\{([^}]*)\}" % re.escape(sel))
        for path, text in ctx.files():
            if not any(fnmatch.fnmatch(os.path.basename(path), g) for g in p["globs"]):
                continue
            for block in pat.findall(text):
                if re.search(r"(?<![\w-])%s\s*:\s*[^;]*%s" % (re.escape(prop), re.escape(want)), block):
                    found = True
                    break
            if found:
                break
        if not found:
            missing.append(sel)
    if missing:
        return False, "%s 未显式声明 %s: %s" % (brief(missing), prop, want)
    return True, "%d 个选择器均显式声明 %s: %s" % (len(p["selectors"]), prop, want)


@rule("no_pattern", "禁用写法：命中即违例（写死计数 / 写死分母 / 结构禁忌）",
      {"globs": ["*.js", "*.jsx", "*.ts", "*.tsx", "*.vue", "*.html"], "patterns": []})
def r_no_pattern(ctx, p):
    if not p.get("patterns"):
        return None, "params.patterns 未填（模板占位）——本条跳过，不当作通过"
    hits = []
    for pat in p.get("patterns") or []:
        hits.extend(ctx.hits(pat))
    if hits:
        return False, brief(hits)
    return True, "未命中禁用写法（%d 条模式）" % len(p.get("patterns") or [])


@rule("token_present", "标记存在性：宿主约定的标记/钩子必须都在（如窗控三键）",
      {"globs": ["*.html", "*.jsx", "*.tsx", "*.vue"], "tokens": []})
def r_token_present(ctx, p):
    if not p.get("tokens"):
        return None, "params.tokens 未填（模板占位）——本条跳过，不当作通过"
    missing = []
    for tok in p.get("tokens") or []:
        rx = re.compile(tok if tok.startswith("<") or tok.startswith("\\") else re.escape(tok))
        if not any(rx.search(text) for _path, text in ctx.files()):
            missing.append(tok)
    if missing:
        return False, "缺标记：%s" % brief(missing)
    return True, "%d 个标记齐备" % len(p.get("tokens") or [])


@rule("keybinding_conflict", "快捷键表冲突：同一组合键不得绑定两次",
      {"globs": ["*.js", "*.jsx", "*.ts", "*.tsx", "*.vue"],
       "regex": r"(?:ctrlKey|metaKey)[^\n]{0,40}?key\s*===?\s*['\"]([a-zA-Z0-9])['\"]|\bkey\s*===?\s*['\"]([a-zA-Z0-9])['\"][^\n]{0,40}?(?:ctrlKey|metaKey)"})
def r_keybinding_conflict(ctx, p):
    rx = re.compile(p["regex"])
    seen = {}
    for path, text in ctx.files():
        if not any(fnmatch.fnmatch(os.path.basename(path), g) for g in p["globs"]):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            m = rx.search(line)
            if not m:
                continue
            combo = (m.group(1) or m.group(2) or "?").lower()
            seen.setdefault(combo, []).append("%s:%d" % (ctx.rel(path), n))
    dup = ["%s ← %s" % (k, ", ".join(v)) for k, v in seen.items() if len(v) > 1]
    if dup:
        return False, "组合键重复绑定：%s" % brief(dup)
    return True, "%d 个组合键无重复" % len(seen)


@rule("guard_whitelist", "某类危险写法只允许出现在白名单函数内（如路由解析函数）",
      {"globs": ["*.js", "*.ts", "*.vue"], "pattern": "#/host/", "allow_functions": [],
       "window": 600})
def r_guard_whitelist(ctx, p):
    rx = re.compile(re.escape(p["pattern"]))
    allow = p.get("allow_functions") or []
    win = int(p.get("window") or 600)
    bad = []
    for path, text in ctx.files():
        if not any(fnmatch.fnmatch(os.path.basename(path), g) for g in p["globs"]):
            continue
        for m in rx.finditer(text):
            ctx_slice = text[max(0, m.start() - win):m.start()]
            if any(re.search(r"function\s+%s\b|%s\s*[:=]\s*(?:function|\()" % (re.escape(f), re.escape(f)),
                             ctx_slice) for f in allow):
                continue
            line = text[:m.start()].count("\n") + 1
            bad.append("%s:%d" % (ctx.rel(path), line))
    if bad:
        return False, "%r 出现在白名单函数之外：%s" % (p["pattern"], brief(bad))
    return True, "%r 全部落在白名单函数内（%d 个）" % (p["pattern"], len(allow))


@rule("aria_label", "只有图标的按钮必须有无障碍名（aria-label / aria-labelledby / title）",
      {"globs": ["*.html", "*.jsx", "*.tsx", "*.vue"]})
def r_aria_label(ctx, p):
    bad = []
    btn = re.compile(r"<button\b([^>]*)>(.*?)</button>", re.S)
    for path, text in ctx.files():
        if not any(fnmatch.fnmatch(os.path.basename(path), g) for g in p["globs"]):
            continue
        for m in btn.finditer(text):
            attrs, inner = m.group(1), m.group(2)
            plain = re.sub(r"<[^>]*>", "", inner).strip()
            if plain:
                continue
            if not re.search(r"aria-label|aria-labelledby|\btitle\s*=", attrs, re.I):
                line = text[:m.start()].count("\n") + 1
                bad.append("%s:%d" % (ctx.rel(path), line))
    if bad:
        return False, "纯图标按钮缺无障碍名：%s" % brief(bad)
    return True, "纯图标按钮均有无障碍名"


@rule("forbid_words", "禁用词：出现在 UI 文案里即违例（如确认按钮只写「确定」）",
      {"globs": ["*.html", "*.jsx", "*.tsx", "*.vue", "*.js"], "words": ["确定"],
       "allow_context": []})
def r_forbid_words(ctx, p):
    bad = []
    for word in p.get("words") or []:
        for hit in ctx.hits(re.escape(word)):
            if any(allow in hit for allow in (p.get("allow_context") or [])):
                continue
            bad.append(hit)
    if bad:
        return False, brief(bad)
    return True, "未命中禁用词（%d 个）" % len(p.get("words") or [])


@rule("clause_refs_resolve", "条款回指：clauses.json 的每条 id 都必须能在体验契约里找到（否则=契约缺口）",
      {"contract": "project-state/契约/EXPERIENCE.md"})
def r_clause_refs_resolve(ctx, p):
    path = os.path.join(ctx.root, *p["contract"].split("/"))
    if not os.path.isfile(path):
        return False, "体验契约不存在：%s（条款没有落地处）" % p["contract"]
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    missing = [c["id"] for c in ctx.clauses if c["id"] not in text]
    if missing:
        return False, "契约缺口（提 CR 回填 %s）：%s" % (p["contract"], brief(missing))
    return True, "%d 条条款全部能在契约里找到" % len(ctx.clauses)


@rule("clause_instances_present", "条款对象可达：`params.page` 指定的界面/文件必须存在（模板占位未填就会被抓到）",
      {"globs": ["*.html", "*.js", "*.ts", "*.vue"], "page": ""})
def r_clause_instances_present(ctx, p):
    page = p.get("page") or ""
    if not page:
        return None, "params.page 未填（模板占位）——本条跳过，不当作通过"
    if any(ctx.rel(path).endswith(page) for path, _t in ctx.files()):
        return True, "找到页面 %s" % page
    return False, "找不到页面 %s" % page


# ---------------------------------------------------------------- 执行
def run(root, clauses_path, globs=None):
    data, clauses = load_clauses(clauses_path)
    ctx = Ctx(root, globs or data.get("scan_globs") or DEFAULT_GLOBS)
    ctx.clauses = clauses
    problems = preflight(clauses)
    has_files = bool(ctx.files())
    results = []
    for c in clauses:
        if c.get("check_kind") != "static":
            continue
        name = c.get("check_rule")
        entry = {"clause": c["id"], "title": c.get("title", ""), "layer": c.get("layer"),
                 "rule": name, "ok": False, "skipped": False, "detail": ""}
        if not has_files:
            entry.update(skipped=True,
                         detail="扫描范围为空（globs %s 没命中任何文件）——本条跳过，不当作通过"
                                % (ctx.globs,))
            results.append(entry)
            continue
        spec = RULES.get(name)
        if spec is None:
            entry.update(skipped=True, detail="静态规则未实现：%s（不当作通过）" % name)
            results.append(entry)
            continue
        params = dict(spec["defaults"])
        params.update(c.get("params") or {})
        try:
            ok, detail = spec["fn"](ctx, params)
        except Exception as exc:  # noqa: BLE001 —— 规则崩了也必须是 fail，不许静默
            ok, detail = False, "规则异常：%s: %s" % (type(exc).__name__, exc)
        entry.update(ok=bool(ok) if ok is not None else False,
                     skipped=ok is None, detail=detail)
        results.append(entry)
    # 元检查：条款文件 ↔ 体验契约互指（对不上 = 契约缺口，判 fail，不是 skip）
    if "clause_refs_resolve" in RULES:
        meta = dict(RULES["clause_refs_resolve"]["defaults"])
        try:
            m_ok, m_detail = RULES["clause_refs_resolve"]["fn"](ctx, meta)
        except Exception as exc:  # noqa: BLE001
            m_ok, m_detail = False, "元检查异常：%s: %s" % (type(exc).__name__, exc)
        results.append({"clause": "*meta*", "title": "条款 ↔ 契约互指", "layer": "L1",
                        "rule": "clause_refs_resolve",
                        "ok": bool(m_ok), "skipped": m_ok is None, "detail": m_detail})
    failed = [r for r in results if not r["ok"] and not r["skipped"]]
    skipped = [r for r in results if r["skipped"]]
    passed = [r for r in results if r["ok"]]
    return {
        "schema": OUT_SCHEMA,
        "root": os.path.abspath(root),
        "clauses_file": os.path.abspath(clauses_path),
        "clauses_schema": SCHEMA,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": not failed and not problems,
        "total": len(results),
        "passed": len(passed),
        "failed": len(failed),
        "skipped": len(skipped),
        "preflight": problems,
        "results": results,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="UX 门 L1 静态检查")
    ap.add_argument("root", nargs="?", default=".", help="项目根（含 project-state/ 的目录）")
    ap.add_argument("--clauses", default="", help="条款文件；默认 <根>/%s" % DEFAULT_CLAUSES)
    ap.add_argument("--globs", default="", help="逗号分隔的扫描通配（覆盖 clauses 的 scan_globs）")
    ap.add_argument("--json-out", default="", help="结果写 JSON（证据落盘用）")
    ap.add_argument("--list-rules", action="store_true", help="列出内置静态规则")
    args = ap.parse_args(argv)

    if args.list_rules:
        for name in sorted(RULES):
            print("%-24s %s" % (name, RULES[name]["doc"]))
        return 0

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("项目根不存在：%s" % root, file=sys.stderr)
        return 2
    clauses_path = args.clauses or os.path.join(root, *DEFAULT_CLAUSES.split("/"))
    if not os.path.isfile(clauses_path):
        print("条款文件不存在：%s（先从 scripts/ux_gate/clauses.json 复制到 %s 再填）"
              % (clauses_path, DEFAULT_CLAUSES), file=sys.stderr)
        return 2
    globs = [g.strip() for g in args.globs.split(",") if g.strip()] or None

    started = time.time()
    report = run(root, clauses_path, globs)
    elapsed = (time.time() - started) * 1000

    for r in report["results"]:
        tag = "skip" if r["skipped"] else ("ok  " if r["ok"] else "FAIL")
        print("[%s] %-12s %-4s %-24s %s" % (tag, r["clause"], r["layer"], r["rule"], r["detail"]))
    for p in report["preflight"]:
        print("[FAIL] 条款自检：%s" % p)

    print("==== L1 静态 %d/%d 通过，FAIL %d，SKIP %d（%d ms%s）====" % (
        report["passed"], report["total"], report["failed"], report["skipped"],
        int(elapsed), "，超 1s 预算！" if elapsed > 1000 else ""))

    out = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        folder = os.path.dirname(os.path.abspath(args.json_out))
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(out)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
