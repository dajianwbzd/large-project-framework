#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原型门禁 prototype_lint（零依赖，只用 python 标准库）。

校验三层：
  L1 spec   —— 屏幕/流转/REQ/card 的结构与引用完整性、可达性
  L2 html   —— 渲染产物：离线自包含（无外链）、每屏/每流转/每 REQ 都在 DOM 里
  L3 shots  —— CDP 截图（cdp_shots.mjs 产）：每屏 + 总览的每档都有图、宽=档位、sha256 一致、
               manifest.htmlSha256=当前 HTML（防过期）、探针 innerWidth=档位、无横向溢出

用法：
  python -X utf8 prototype_lint.py --spec spec/demo-proto.json --html out/prototype.html \
      --shots out/shots [--widths 375,1440] [--prefix prototype] [--json] [--no-shots]

退出码：0 无 error / 1 有 error / 2 用法或读取错误
（对齐 大型项目开发框架/skills/UI设计师/scripts/design_lint.py 的契约）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# 渲染器白名单（render_proto.BLOCK_RENDERERS 的 key）；spec 写错 block type 会被静默跳过，
# 所以这里对未知类型出 warn —— 曾发生过把 "filters" 写成 "filter" 导致整块不渲染、门禁却全绿。
BLOCK_TYPES = ("topbar", "kpi", "filters", "list", "detail", "actions")


class Report:
    def __init__(self) -> None:
        self.findings: list[dict] = []

    def add(self, level: str, rule: str, msg: str, where: str = "") -> None:
        self.findings.append({"level": level, "rule": rule, "where": where, "message": msg})

    def error(self, rule: str, msg: str, where: str = "") -> None:
        self.add("error", rule, msg, where)

    def warn(self, rule: str, msg: str, where: str = "") -> None:
        self.add("warn", rule, msg, where)

    def count(self, level: str) -> int:
        return sum(1 for f in self.findings if f["level"] == level)


def lint_spec(spec: dict, rep: Report) -> None:
    for key in ("project", "requirements", "cards", "screens", "flows"):
        if key not in spec:
            rep.error("spec-missing-key", "spec 缺少顶层字段 %s" % key, "spec")
            return
    req_ids = {r.get("id") for r in spec["requirements"]}
    card_ids = {c.get("id") for c in spec["cards"]}
    screens = spec["screens"]
    flows = spec["flows"]

    for name, items in (("requirement", spec["requirements"]), ("card", spec["cards"]),
                        ("screen", screens), ("flow", flows)):
        seen = set()
        for it in items:
            iid = it.get("id")
            if not iid:
                rep.error("spec-missing-id", "%s 缺 id" % name, name)
                continue
            if iid in seen:
                rep.error("spec-duplicate-id", "%s 重复 id: %s" % (name, iid), iid)
            seen.add(iid)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", iid):
                rep.error("spec-bad-id", "%s id 非法（只允许字母数字 _ . -）: %s" % (name, iid), iid)

    screen_ids = {s.get("id") for s in screens}
    first = screens[0].get("id") if screens else None

    for s in screens:
        sid = s.get("id")
        if not s.get("title"):
            rep.error("spec-missing-title", "屏幕缺 title", str(sid))
        card = s.get("card")
        if card not in card_ids:
            rep.error("spec-unknown-card", "屏幕 %s 绑定的 %r 不在 cards 里" % (sid, card), str(sid))
        reqs = s.get("req") or []
        if not reqs:
            rep.warn("spec-no-req", "屏幕 %s 未绑定任何 REQ-x.y" % sid, str(sid))
        for r in reqs:
            if r not in req_ids:
                rep.error("spec-unknown-req", "屏幕 %s 引用了不存在的需求 %s" % (sid, r), str(sid))
        if not (s.get("blocks") or []):
            rep.warn("spec-no-blocks", "屏幕 %s 没有任何 block，渲染出来是空页" % sid, str(sid))
        for blk in s.get("blocks") or []:
            btype = blk.get("type")
            if btype not in BLOCK_TYPES:
                rep.warn("spec-unknown-block",
                         "屏幕 %s 含未知 block type %r（渲染器白名单：%s，该块会被静默跳过）"
                         % (sid, btype, "/".join(BLOCK_TYPES)), str(sid))

    for f in flows:
        fid = f.get("id")
        for side in ("from", "to"):
            if f.get(side) not in screen_ids:
                rep.error("spec-bad-flow", "流转 %s 的 %s=%r 不存在" % (fid, side, f.get(side)), str(fid))
        # 流转的 req 允许写成单个字符串或列表（渲染器两者都能打；门禁两者都要认，
        # 否则 spec 里写 ["REQ-1.2"] 时 lint 会 TypeError 崩掉——比漏报更糟）
        freqs = f.get("req")
        if isinstance(freqs, (list, tuple)):
            bad = [r for r in freqs if r not in req_ids]
        else:
            bad = [] if freqs in req_ids else [freqs]
        if bad:
            rep.error("spec-bad-flow-req", "流转 %s 的 req=%r 不在需求表" % (fid, f.get("req")), str(fid))
        if not f.get("action"):
            rep.warn("spec-flow-no-action", "流转 %s 缺 action 文案（点击什么）" % fid, str(fid))

    # 可达性：从首屏 BFS，未覆盖的屏 = 点不到，属阻断问题
    if first:
        graph: dict[str, set[str]] = {sid: set() for sid in screen_ids}
        for f in flows:
            if f.get("from") in graph and f.get("to") in graph:
                graph[f["from"]].add(f["to"])
        seen, queue = {first}, [first]
        while queue:
            cur = queue.pop()
            for nxt in graph[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        for sid in sorted(screen_ids - seen):
            rep.error("spec-unreachable-screen", "屏幕 %s 从首屏 %s 无法到达（点不到）" % (sid, first), str(sid))
    used_cards = {s.get("card") for s in screens}
    for cid in sorted(card_ids - used_cards):
        rep.warn("spec-unused-card", "需求卡 %s 没有任何原型屏覆盖" % cid, str(cid))


def lint_html(spec: dict, html_path: Path, rep: Report) -> str:
    try:
        doc = html_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        rep.error("html-unreadable", "读取 HTML 失败: %s" % exc, str(html_path))
        return ""
    if "<html" not in doc.lower():
        rep.error("html-not-html", "产物不是 HTML", str(html_path))

    refs = re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', doc)
    external = [r for r in refs if r.startswith(("http://", "https://", "//"))]
    if external:
        rep.error("html-external-ref", "存在外部资源引用（离线/内网会裂图）: %s" % ", ".join(sorted(set(external))[:5]),
                  str(html_path))
    if re.search(r"<script[^>]+src\s*=", doc, re.I):
        rep.error("html-script-src", "含 <script src=…>，原型必须单文件自包含", str(html_path))
    if re.search(r"<link[^>]+stylesheet", doc, re.I):
        rep.error("html-link-css", "含 <link rel=stylesheet>，样式必须内联", str(html_path))

    for s in spec["screens"]:
        sid = s["id"]
        if 'data-screen="%s"' % sid not in doc:
            rep.error("html-missing-screen", "HTML 里找不到屏幕 %s" % sid, sid)
        if 'id="screen-%s"' % sid not in doc:
            rep.error("html-missing-anchor", "HTML 里找不到锚点 #/screen-%s" % sid, sid)
    for f in spec["flows"]:
        if 'data-flow="%s"' % f["id"] not in doc:
            rep.error("html-missing-flow", "HTML 里找不到流转 %s 的可点元素" % f["id"], f["id"])
    for r in spec["requirements"]:
        if 'data-req="%s"' % r["id"] not in doc and r["id"] not in doc:
            rep.warn("html-missing-req", "HTML 里没有出现需求 %s 的标注" % r["id"], r["id"])

    targets = set(re.findall(r'data-flow-to="([^"]+)"', doc))
    screen_ids = {s["id"] for s in spec["screens"]}
    for t in sorted(targets - screen_ids):
        rep.error("html-bad-target", "可点按钮指向不存在的屏幕 %r" % t, str(html_path))
    return doc


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(PNG_MAGIC) or len(data) < 24:
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def lint_shots(spec: dict, html_path: Path, shots_dir: Path, rep: Report,
               widths: tuple[int, ...] = (375, 1440), page_key: str = "prototype") -> None:
    """L3：截图必须是 cdp_shots.mjs 产出的（带探针），且与当前 HTML 同代。"""
    manifest = load_json(shots_dir / "manifest.json")
    if manifest is None:
        rep.error("shots-no-manifest",
                  "缺少 %s（先跑 cdp_shots.mjs --routes …；CLI 的 chrome --screenshot 不算数）"
                  % (shots_dir / "manifest.json").name, str(shots_dir))
        return
    probes = load_json(shots_dir / "probes.json")
    if probes is None:
        rep.error("shots-no-probes", "缺少 probes.json（截图必须是 cdp_shots.mjs 产的）", str(shots_dir))
        return

    page = (manifest.get("pages") or {}).get(page_key)
    if not page:
        rep.error("shots-no-page", "manifest.json 里没有前缀 %r 的记录" % page_key, page_key)
        return
    routes = page.get("routes") or {}
    page_probes = probes.get(page_key) or {}

    try:
        html_sha = sha256_of(html_path)
    except OSError as exc:
        rep.error("shots-html-unreadable", "算 HTML sha256 失败: %s" % exc, str(html_path))
        return
    if page.get("htmlSha256") != html_sha:
        rep.error("shots-stale", "HTML 改过但截图没重出（manifest.htmlSha256 与当前文件不一致）", page_key)

    expected = ["flow"] + [s["id"] for s in spec["screens"]]
    for route in expected:
        for width in widths:
            key = "%s@%d" % (route, width)
            entry = routes.get(key)
            if not entry:
                rep.error("shots-missing", "缺少截图 %s（flow=总览，其余=屏幕 id；档位 %d）" % (key, width), route)
                continue
            png = shots_dir / str(entry.get("file", ""))
            if not png.exists():
                rep.error("shots-file-missing", "截图文件不存在: %s" % png.name, key)
                continue
            size = png_size(png)
            if not size:
                rep.error("shots-not-png", "截图不是合法 PNG: %s" % png.name, key)
                continue
            w, h = size
            if w != width:
                rep.error("shots-width-mismatch", "截图宽 %d ≠ 档位 %d: %s" % (w, width, png.name), key)
            if h < 200:
                rep.error("shots-too-small", "截图高 %d < 200: %s" % (h, png.name), key)
            digest = sha256_of(png)
            if entry.get("sha256") != digest:
                rep.error("shots-stale-hash", "截图 sha256 与清单不一致（文件被改过）: %s" % png.name, key)
            probe = page_probes.get(key)
            if not probe:
                rep.error("shots-probe-missing",
                          "%s 没有探针记录（必须用 cdp_shots.mjs 出图，CLI 的 320 档实际按 500px 布局）" % key, key)
                continue
            if probe.get("innerWidth") != width:
                rep.error("shots-probe-width-mismatch",
                          "%s 探针 innerWidth=%r ≠ %d（视口没真到该档）" % (key, probe.get("innerWidth"), width), key)
            overflow = (probe.get("overflow") or {})
            if overflow.get("horizontal"):
                doc = probe.get("doc") or {}
                elems = overflow.get("elements") or []
                sample = "；".join(
                    "%s.%s 右边界 %s" % (e.get("tag"), e.get("cls") or "-", e.get("right")) for e in elems[:3])
                rep.error("shots-overflow",
                          "%s 横向溢出 scrollWidth=%s > %d%s"
                          % (key, doc.get("scrollWidth"), width, ("（%s）" % sample) if sample else ""), key)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="原型门禁：spec/HTML/截图 三层校验")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--html", required=True)
    ap.add_argument("--shots", default="")
    ap.add_argument("--no-shots", action="store_true", help="只校验 spec+html（开发期用）")
    ap.add_argument("--widths", default="375,1440", help="原型验收档位（默认 375,1440）")
    ap.add_argument("--prefix", default="prototype", help="cdp_shots.mjs 的 --prefix（默认 prototype）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])

    spec_path, html_path = Path(args.spec), Path(args.html)
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print("读取 spec 失败: %s" % exc)
        return 2
    if not spec_path.exists() or not html_path.exists():
        print("spec 或 html 路径不存在")
        return 2

    rep = Report()
    lint_spec(spec, rep)
    lint_html(spec, html_path, rep)
    if not args.no_shots:
        if not args.shots:
            rep.error("shots-no-dir", "未给 --shots 目录（或用 --no-shots 跳过）")
        else:
            widths = tuple(int(x) for x in args.widths.split(",") if x.strip())
            lint_shots(spec, html_path, Path(args.shots), rep, widths=widths, page_key=args.prefix)

    errors, warns = rep.count("error"), rep.count("warn")
    if args.json:
        print(json.dumps({"findings": rep.findings,
                          "summary": {"errors": errors, "warnings": warns, "infos": 0}}, ensure_ascii=False, indent=2))
    else:
        for f in rep.findings:
            print("[%s] %-22s %s%s" % (
                f["level"].upper(), f["rule"], f["message"],
                ("  ← %s" % f["where"]) if f["where"] else ""))
        print("prototype_lint: %d error / %d warn" % (errors, warns))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
