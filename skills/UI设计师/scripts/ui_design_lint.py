#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui_design_lint.py —— UI「逐页视觉设计稿」门禁（零依赖，契约仿 design_lint.py）

管什么：`project-state/契约/UI资源/designs/` 下的 **逐页高保真设计稿**：
     <页面>.html        自包含单文件视觉稿（源）
     <页面>.md          既有文本规格（意图与状态清单）
     shots/<页面>_<w>.png  四档渲染件（320/768/1024/1440）

不管什么：DESIGN.md 自身的结构/对比度等——那是 skills/UI设计师/scripts/design_lint.py 的职责，两者不重叠。

核心判据（把"设计稿和令牌一致"从口头约定变成可复算）：
  1. 页面齐套：有 <页面>.md 就必须有 <页面>.html，反之亦然；
  2. 令牌一致：HTML 的 `#design-tokens` 里 `--color-<k>` 必须与 DESIGN.md colors.<k> 逐值相等，
     且 DESIGN.md 每个 color 令牌都要出现（防"用了但没声明"与"声明了但没用"）；
  3. 状态覆盖：EXPERIENCE.md 里该页声明的状态，HTML 必须有对应 `data-state` 容器（至少 default）；
  4. 自包含：禁 http(s) 外链（离线/内网会裂）；
  5. 四档截图：四张都在、宽度分别对齐、非空白、且主色令牌色真的被画上去（像素级）。

用法：
    python -X utf8 ui_design_lint.py --root <project-state> [--json] [--no-shots] [--pixel]
退出码：0 = 无 error；1 = 有 error；2 = 用法或读取错误。
"""
import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    import png_probe
except Exception:  # noqa: BLE001
    png_probe = None

WIDTHS = [320, 768, 1024, 1440]
# 横向溢出的容忍量（px）：1px 是亚像素/滚动条取整的常见抖动，不算溢出
OVERFLOW_TOLERANCE = 1


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback
# 注意：不能把 "}}" 列入禁词——CSS 里 `@media(...){.a{...}}` 天然出现双右花括号。
# 模板占位以 "{{" 为准（mustache 必然成对），另加中文占位词。
FORBIDDEN = ["Lorem", "lorem", "TODO", "待填", "占位", "xxx", "{{", "FIXME"]
STATE_WORDS = [
    "default", "loading", "empty", "error", "partial", "success", "disabled",
    "hover", "focus", "active", "offline", "permission-denied", "not-found", "readonly",
]

findings = []


def add(rule, level, msg, where=""):
    findings.append({"rule": rule, "level": level, "message": msg, "where": where})


def read(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def parse_design_tokens(design_md):
    """从 DESIGN.md frontmatter 解析 colors/typography/rounded/spacing 的键与值（不依赖 yaml）。"""
    text = read(design_md)
    m = re.search(r"^---\s*$(.*?)^---\s*$", text, re.S | re.M)
    fm = m.group(1) if m else text
    groups = {"colors": {}, "typography": {}, "rounded": {}, "spacing": {}}

    def grab(section):
        lines = fm.splitlines()
        out, cur, cur_indent = {}, None, None
        for i, line in enumerate(lines):
            if re.match(r"^%s:\s*$" % section, line):
                cur = i
                continue
            if cur is None:
                continue
            if line.strip() and not line.startswith((" ", "\t")):
                cur = None
                continue
            mm = re.match(r"^(\s+)([A-Za-z0-9_-]+):\s*(.*)$", line)
            if mm:
                indent, key, val = len(mm.group(1)), mm.group(2), mm.group(3).strip()
                if cur_indent is None:
                    cur_indent = indent
                if indent == cur_indent:
                    out[key] = val.split("#")[0].strip().strip('"').strip("'") if section != "colors" else val.strip().strip('"').strip("'")
        return out

    for sec in groups:
        groups[sec] = grab(sec)
    # colors 值里可能带行尾注释，剥离
    for k, v in list(groups["colors"].items()):
        v2 = re.sub(r"\s+#.*$", "", v).strip().strip('"').strip("'")
        groups["colors"][k] = v2
    return groups


def norm_color(v):
    return (v or "").strip().strip('"').strip("'").upper()


def parse_html_tokens(html):
    m = re.search(r'<style[^>]*id=["\']design-tokens["\'][^>]*>(.*?)</style>', html, re.S | re.I)
    if not m:
        return None
    body = m.group(1)
    vars_ = {}
    for name, val in re.findall(r"--([A-Za-z0-9-]+)\s*:\s*([^;]+);", body):
        vars_[name.strip()] = val.strip()
    return vars_


def parse_states(html):
    return set(re.findall(r'data-state=["\']([A-Za-z0-9_-]+)["\']', html))


def expected_states(experience_md, page):
    if not experience_md or not os.path.isfile(experience_md):
        return None
    text = read(experience_md)
    found = set()
    for line in text.splitlines():
        if page in line:
            for w in STATE_WORDS:
                if re.search(r'(?<![A-Za-z-])%s(?![A-Za-z-])' % re.escape(w), line):
                    found.add(w)
    return found or None


def lint_html_tokens(page, html_path, html, tokens):
    vars_ = parse_html_tokens(html)
    if vars_ is None:
        add("token-block-missing", "error", "缺少 <style id=\"design-tokens\">，无法校验令牌落地", page)
        return
    declared = {k.lower(): v for k, v in tokens["colors"].items()}
    seen = set()
    for name, val in vars_.items():
        if not name.startswith("color-"):
            continue
        key = name[len("color-"):]
        seen.add(key)
        if key not in declared:
            add("token-unknown", "warn", "HTML 里的 --color-%s 未在 DESIGN.md colors 中声明" % key, page)
            continue
        if norm_color(val) != norm_color(declared[key]):
            add("token-mismatch", "error",
                "--color-%s=%s 与 DESIGN.md 的 %s 不一致" % (key, val, declared[key]), page)
    for key in declared:
        if key not in seen:
            add("token-missing", "error", "DESIGN.md 声明的 colors.%s 未在 HTML 令牌块中复写" % key, page)
    # typography / rounded / spacing 只做"用了就必有声明"的软校验
    for name in vars_:
        for grp in ("font", "radius", "space"):
            if name.startswith(grp + "-"):
                k = name[len(grp) + 1:]
                bucket = {"font": "typography", "radius": "rounded", "space": "spacing"}[grp]
                if tokens[bucket] and k not in tokens[bucket]:
                    add("token-unknown", "warn", "HTML 里的 --%s 未在 DESIGN.md %s 中声明" % (name, bucket), page)


def lint_shots(page, html_path, designs_dir, tokens, pixel):
    """四档截图：文件齐 → 宽度对 → 非空白 → 主色真画上 → 探针自证 → 不过期。"""
    primary = tokens["colors"].get("primary")
    shots_dir = os.path.join(designs_dir, "shots")
    probes_all = load_json(os.path.join(shots_dir, "probes.json"), None)
    manifest = load_json(os.path.join(shots_dir, "manifest.json"), None)

    # --- 防 stale（最容易被糊弄的一环：改了 HTML 没重出图，旧图看起来"都对"）---
    entry = None
    if manifest is None:
        add("shot-manifest-missing", "error",
            "缺少 shots/manifest.json——截图必须由 cdp_shots.mjs 产出（它会写 sha256 防过期）", page)
    else:
        entry = (manifest.get("pages") or {}).get(page)
        if entry is None:
            add("shot-manifest-missing", "error", "shots/manifest.json 里没有该页记录（重跑 cdp_shots.mjs）", page)
        else:
            if entry.get("htmlSha256") and entry["htmlSha256"] != sha256_of(html_path):
                add("shot-stale", "error",
                    "HTML 改过但截图没重出（manifest.htmlSha256 与当前文件不一致）", page)
            dm = entry.get("designMd")
            if dm:
                design_md = os.path.join(os.path.dirname(os.path.dirname(designs_dir)), dm)
                # designs 的上级是 UI资源，再上级是 契约；DESIGN.md 在 契约/ 下
                if not os.path.isfile(design_md):
                    design_md = os.path.join(os.path.dirname(designs_dir), dm)
                if os.path.isfile(design_md) and entry.get("designMdSha256") \
                        and entry["designMdSha256"] != sha256_of(design_md):
                    add("shot-stale-design", "error",
                        "DESIGN.md 改过但设计稿截图没重出（令牌可能已变）", page)

    # --- 探针：证明"这张图真的是这个宽度"，不是 --window-size 裁出来的假档位 ---
    probes = (probes_all or {}).get(page) if probes_all else None
    if probes is None:
        add("shot-probe-missing", "error",
            "缺少 shots/probes.json 中该页的探针记录——四档图必须用 cdp_shots.mjs（CDP 精确视口）出，"
            "chrome --screenshot --window-size 的 320 档实际按 500px 布局，会得出尺寸对但内容是裁切的假图", page)

    for w in WIDTHS:
        png = os.path.join(shots_dir, "%s_%d.png" % (page, w))
        if not os.path.isfile(png):
            add("shot-missing", "error", "缺少 %d 档截图 shots/%s_%d.png（跑 cdp_shots.mjs）" % (w, page, w), page)
            continue
        if os.path.getsize(png) < 1000:
            add("shot-too-small", "error", "截图不足 1KB，疑似空图：" + os.path.basename(png), page)
            continue
        if pixel and png_probe is not None:
            try:
                st = png_probe.stats(png, primary if primary else None)
            except Exception as exc:  # noqa: BLE001
                add("shot-decode-failed", "error", "截图解码失败 %s: %s" % (os.path.basename(png), exc), page)
                continue
            if st["width"] != w:
                add("shot-width-mismatch", "error",
                    "截图宽 %d 与档位 %d 不符：%s" % (st["width"], w, os.path.basename(png)), page)
            if st["blank_ratio"] > 0.985:
                add("shot-blank", "error", "疑似空白图（占比最高单色 %.1f%%）：%s" % (st["blank_ratio"] * 100, os.path.basename(png)), page)
            if primary and st.get("color_share", 0) < 0.003:
                add("shot-token-not-painted", "error",
                    "primary 令牌色 %s 在图中占比 %.2f%%（<0.3%%），令牌可能没真用上：%s"
                    % (primary, st.get("color_share", 0) * 100, os.path.basename(png)), page)

        # 逐档探针
        if probes is not None:
            pr = probes.get(str(w))
            if pr is None:
                add("shot-probe-missing", "error", "probes.json 缺 %d 档探针（该档图来源不可信）" % w, page)
                continue
            if pr.get("innerWidth") != w:
                add("shot-probe-width-mismatch", "error",
                    "%d 档探针 innerWidth=%s（布局视口不等于档位宽度，图是裁切的假档位）"
                    % (w, pr.get("innerWidth")), page)
            if pr.get("doc", {}).get("scrollWidth", 0) > w + OVERFLOW_TOLERANCE:
                sample = (pr.get("overflow") or {}).get("elements") or []
                hint = "；示例：%s" % json.dumps(sample[:2], ensure_ascii=False) if sample else ""
                add("shot-overflow", "error",
                    "%d 档出现横向溢出 scrollWidth=%s > %d%s"
                    % (w, pr["doc"]["scrollWidth"], w, hint), page)
            # 令牌"声明 ≠ 生效"：回读计算值必须与 DESIGN.md 相等
            live = pr.get("tokens") or {}
            for k, want in tokens["colors"].items():
                got = live.get("--color-" + k)
                if got is None:
                    continue  # 声明缺失由 token-missing 管
                if norm_color(got) != norm_color(want):
                    add("token-live-mismatch", "error",
                        "%d 档回读 --color-%s=%s，DESIGN.md 是 %s（令牌没生效或被覆盖）"
                        % (w, k, got, want), page)
            if entry is not None and entry.get("height") and pr.get("innerHeight") != entry["height"]:
                add("shot-probe-height", "warn",
                    "%d 档 innerHeight=%s 与出图高度 %s 不符" % (w, pr.get("innerHeight"), entry["height"]), page)


def lint_page(root, page, html_path, tokens, experience_md, check_shots, pixel):
    designs_dir = os.path.dirname(html_path)
    html = read(html_path)

    m = re.search(r'data-page=["\']([^"\']+)["\']', html)
    if not m:
        add("html-missing-page", "error", "缺少 data-page 标记", page)
    elif m.group(1) != page:
        add("html-page-mismatch", "error", "data-page=%s 与文件名 %s 不一致" % (m.group(1), page), page)

    ms = re.search(r'data-spec=["\']([^"\']+)["\']', html)
    if not ms:
        add("html-missing-spec", "warn", "缺少 data-spec（指向同目录规格 md）", page)
    elif not os.path.isfile(os.path.join(designs_dir, ms.group(1))):
        add("html-spec-not-found", "error", "data-spec 指向的 %s 不存在" % ms.group(1), page)

    if re.search(r'(?:src|href)\s*=\s*["\']https?://', html, re.I) or re.search(r'@import\s+["\']?https?://', html, re.I):
        add("html-external-ref", "error", "存在 http(s) 外链资源，设计稿必须自包含（离线/内网会裂）", page)

    for w in FORBIDDEN:
        if w in html:
            add("html-forbidden-string", "error", "出现占位残留 %r（文案必须真实）" % w, page)

    lint_html_tokens(page, html_path, html, tokens)

    states = parse_states(html)
    if "default" not in states:
        add("state-default-missing", "error", "缺少 data-state=\"default\"", page)
    exp = expected_states(experience_md, page)
    if exp:
        missing = sorted(s for s in exp if s not in states)
        if missing:
            add("state-missing", "error", "EXPERIENCE.md 声明的状态缺失：%s" % ", ".join(missing), page)
    else:
        add("state-set-unknown", "warn", "EXPERIENCE.md 中未解析到该页状态集，仅校验 default", page)

    if check_shots:
        lint_shots(page, html_path, designs_dir, tokens, pixel)


def main(argv):
    ap = argparse.ArgumentParser(description="UI 逐页设计稿门禁（零依赖）")
    ap.add_argument("--root", required=True, help="project-state 根")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-shots", action="store_true", help="跳过截图校验")
    ap.add_argument("--no-pixel", action="store_true", help="跳过像素级校验（只查文件存在与宽度）")
    a = ap.parse_args(argv)

    root = os.path.abspath(a.root)
    design_md = os.path.join(root, "契约", "DESIGN.md")
    exp_md = os.path.join(root, "契约", "EXPERIENCE.md")
    designs = os.path.join(root, "契约", "UI资源", "designs")
    if not os.path.isdir(designs):
        print(json.dumps({"ok": False, "error": "找不到目录 %s" % designs}, ensure_ascii=False))
        return 2
    if not os.path.isfile(design_md):
        print(json.dumps({"ok": False, "error": "缺少 契约/DESIGN.md"}, ensure_ascii=False))
        return 2

    tokens = parse_design_tokens(design_md)
    if not tokens["colors"].get("primary"):
        add("design-no-primary", "error", "DESIGN.md 未声明 colors.primary，设计稿无法对齐主色", "DESIGN.md")

    names = sorted(os.listdir(designs))
    pages = sorted({os.path.splitext(n)[0] for n in names if n.lower().endswith(".md") and not n.startswith("设计稿模板")})
    htmls = {os.path.splitext(n)[0] for n in names if n.lower().endswith(".html")}
    if not pages and not htmls:
        add("designs-empty", "error", "designs/ 下既无页面规格 md 也无设计稿 html", "designs/")

    for p in pages:
        if p not in htmls:
            add("page-html-missing", "error", "有 %s.md 规格但没有 %s.html 视觉稿" % (p, p), p)
    for h in sorted(htmls):
        if h not in pages:
            add("page-spec-missing", "error", "有 %s.html 但没有 %s.md 规格（规格是意图来源，不能缺）" % (h, h), h)
        lint_page(root, h, os.path.join(designs, h + ".html"), tokens, exp_md,
                  not a.no_shots, not a.no_pixel)

    errors = [f for f in findings if f["level"] == "error"]
    warns = [f for f in findings if f["level"] == "warn"]
    out = {
        "ok": not errors,
        "root": root,
        "pages": pages,
        "colors": len(tokens["colors"]),
        "summary": {"error": len(errors), "warn": len(warns)},
        "findings": findings,
    }
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print("[%s] %-24s %s  ← %s" % (f["level"].upper(), f["rule"], f["message"], f["where"]))
        print("ui_design_lint: %d error / %d warn" % (len(errors), len(warns)))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
