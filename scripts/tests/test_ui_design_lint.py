#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ui_design_lint.py — UI 逐页设计稿门禁的回归 + 变异对拍（纯 stdlib，不依赖 pytest）

夹具写法自 2026-10-25 起 pathlib 化（Path.write_text / write_bytes），不用裸 open 写临时文件。

为什么有这个脚本
----------------
`UI设计师/scripts/ui_design_lint.py` 的判据分三类：**文件齐套**、**令牌一致**、**截图自证**。
其中真实工程里最常走的只有"齐套"这一路；`shot-stale` / `shot-token-not-painted` /
`token-live-mismatch` / `state-missing` 这些分支**只有在出过问题的时候才会跑到**——
也就是说，发布时它们要么是**未验证代码**，要么是"曾经跑过一次、之后没人再确认"。

本文件把三类判据都变成**可复现的夹具**：
  ① **能拦**：每条规则一个故意失败的夹具；
  ② **不乱拦**：一个干净夹具断言 ok=true / 0 error 0 warn；
  ③ **检查器自变异**：把规则改瞎后必须失去命中（防止"夹具恰好让脚本退 1"）。

截图用**合成 PNG + 合成 manifest/probes**（结构照 `cdp_shots.mjs` 产物）：
门禁校验的是"清单与文件是否自洽、令牌有没有真画上"，不依赖 Chrome。

用法
----
    python test_ui_design_lint.py          # 跑全部，失败则非零退出
    python test_ui_design_lint.py -v       # 打印每条实际结果
"""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK = os.path.abspath(Path(HERE, os.pardir, os.pardir))


def _find_script(*parts):
    for base in (FRAMEWORK, Path(FRAMEWORK, os.pardir), HERE):
        cand = Path(base, *parts)
        if os.path.exists(cand):
            return cand
    return Path(FRAMEWORK, *parts)


LINT = _find_script("skills", "UI设计师", "scripts", "ui_design_lint.py")
TMP = Path(HERE, "_fixture_tmp_ui")

WIDTHS = (320, 768, 1024, 1440)
PRIMARY = "#101828"
PRIMARY_RGB = (16, 24, 40)
SURFACE = "#FFFFFF"
BORDER = "#EAECF0"

FAILS = []
PASSES = []
VERBOSE = "-v" in sys.argv


def check(cond, label):
    if cond:
        PASSES.append(label)
    else:
        FAILS.append(label)
    if VERBOSE:
        print("  %s %s" % ("ok  " if cond else "FAIL", label))


def run(args, script=None):
    proc = subprocess.run([sys.executable, "-X", "utf8", script or LINT] + list(args),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def lint_json(root, extra=()):
    rc, out, err = run(["--root", root, "--json"] + list(extra))
    try:
        return rc, json.loads(out), err
    except ValueError:
        return rc, {"_raw": out, "_err": err, "findings": [], "ok": None}, err


def rules(obj):
    return {f["rule"] for f in obj.get("findings") or []}


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_png(path, width, height, band_rgb=PRIMARY_RGB, band_h=120):
    """合成一张"顶部有主色条、其余白底"的图：既非空白，也能被主色占比检查认到。"""
    white = (255, 255, 255)
    band = bytes(band_rgb)
    w = bytes(white)
    rows = []
    for y in range(height):
        rows.append(b"\x00" + (band if y < band_h else w) * width)
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    Path(path).write_bytes(data)


DESIGN_MD = """---
colors:
  primary: "%s"
  surface: "%s"
  border: "%s"
typography:
  body: "14px/1.6 system-ui"
rounded:
  md: "8px"
spacing:
  s4: "16px"
---

# DESIGN（夹具）

令牌来自 frontmatter；页面必须用 var() 消费，不许写死 hex。
""" % (PRIMARY, SURFACE, BORDER)

EXPERIENCE_MD = """# EXPERIENCE（夹具）

| 页面 | 状态 |
|---|---|
| demo-page | default / empty / error |
"""

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>demo-page · 设计稿</title>
<style id="design-tokens">
:root {
  --color-primary: %s;
  --color-surface: %s;
  --color-border: %s;
}
</style>
<style>
  body { margin: 0; background: var(--color-surface); font-family: system-ui, sans-serif; }
  .band { height: 120px; background: var(--color-primary); }
  .card { border: 1px solid var(--color-border); margin: 16px; padding: 16px; }
</style>
</head>
<body data-page="demo-page" data-spec="demo-page.md">
  <div class="band"></div>
  <main data-state="default">
    <div class="card">默认：一条任务「支付回调重试」，负责人 周砚。</div>
  </main>
  <section data-state="empty">
    <div class="card">空态：还没有任务，点右上角「新建」。</div>
  </section>
  <section data-state="error">
    <div class="card">错误：加载失败，请重试。</div>
  </section>
</body>
</html>
""" % (PRIMARY, SURFACE, BORDER)


def fresh(name):
    dest = Path(TMP, name)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    return dest


def make_root(name, make_shots=True):
    """造一个最小 project-state：契约/ + UI资源/designs/ +（可选）shots/。"""
    root = fresh(name)
    contract = Path(root, "契约")
    designs = Path(contract, "UI资源", "designs")
    os.makedirs(designs)
    Path(contract, "DESIGN.md").write_text(DESIGN_MD, encoding="utf-8")
    Path(contract, "EXPERIENCE.md").write_text(EXPERIENCE_MD, encoding="utf-8")
    with open(Path(designs, "demo-page.md"), "w", encoding="utf-8") as fh:
        fh.write("# 设计稿：demo-page\n\n三段式：主色条 + 默认态列表 + 空态 / 错误态。\n")
    html_path = Path(designs, "demo-page.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(PAGE_HTML)
    if make_shots:
        write_shots(designs, html_path, contract)
    return root, contract, designs, html_path


def write_shots(designs, html_path, contract):
    shots = Path(designs, "shots")
    os.makedirs(shots, exist_ok=True)
    for w in WIDTHS:
        write_png(Path(shots, "demo-page_%d.png" % w), w, 800)
    manifest = {"pages": {"demo-page": {
        "html": "demo-page.html",
        "htmlSha256": sha(html_path),
        "designMd": "DESIGN.md",
        "designMdSha256": sha(Path(contract, "DESIGN.md")),
        "height": 800,
        "widthsList": list(WIDTHS),
    }}}
    probes = {"demo-page": {}}
    for w in WIDTHS:
        probes["demo-page"][str(w)] = {
            "innerWidth": w, "innerHeight": 800, "dpr": 1,
            "doc": {"scrollWidth": w, "clientWidth": w, "scrollHeight": 800, "clientHeight": 800},
            "overflow": {"horizontal": False, "elements": []},
            "tokens": {"--color-primary": PRIMARY, "--color-surface": SURFACE, "--color-border": BORDER},
        }
    _dump(Path(shots, "manifest.json"), manifest)
    _dump(Path(shots, "probes.json"), probes)
    return shots


def _dump(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def _load(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def rewrite(path, old, new):
    """按字面替换；锚点不在时返回 False，让用例自己报失败，而不是静默什么都没改。"""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if old not in text:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new))
    return True


def main():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP)
    try:
        # ---------- ① 不乱拦 ----------
        root, contract, designs, html = make_root("clean")
        rc, obj, err = lint_json(root)
        check(rc == 0 and obj.get("ok") is True,
              "clean: ok=true / exit 0（rc=%s, summary=%s, err=%s）"
              % (rc, obj.get("summary"), err.strip()[:160]))
        check(obj.get("summary", {}).get("error") == 0 and obj.get("summary", {}).get("warn") == 0,
              "clean: 0 error 0 warn（%s）" % (obj.get("summary"),))

        # ---------- ② 令牌一致 ----------
        root, contract, designs, html = make_root("token-mismatch")
        check(rewrite(html, "--color-primary: %s;" % PRIMARY, "--color-primary: #FF0000;"),
              "token-mismatch: 夹具改到了主色")
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "token-mismatch" in rules(obj), "token-mismatch: HTML 里主色写错 -> error")

        root, contract, designs, html = make_root("token-missing")
        check(rewrite(html, "  --color-border: %s;\n" % BORDER, ""), "token-missing: 夹具删掉了 border 复写")
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "token-missing" in rules(obj), "token-missing: DESIGN.md 有而 HTML 没复写 -> error")

        root, contract, designs, html = make_root("token-live")
        probes = _load(Path(designs, "shots", "probes.json"))
        probes["demo-page"]["768"]["tokens"]["--color-primary"] = "#00FF00"
        _dump(Path(designs, "shots", "probes.json"), probes)
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "token-live-mismatch" in rules(obj),
              "token-live-mismatch: 声明 ≠ 计算值（令牌没生效）-> error")

        # ---------- ③ 状态覆盖 ----------
        root, contract, designs, html = make_root("state-missing")
        check(rewrite(html, 'data-state="empty"', 'data-state="loading"'), "state-missing: 夹具改掉了空态")
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "state-missing" in rules(obj),
              "state-missing: EXPERIENCE.md 声明的 empty 缺失 -> error")

        root, contract, designs, html = make_root("state-default")
        check(rewrite(html, 'data-state="default"', 'data-state="main"'), "state-default: 夹具改掉了默认态")
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "state-default-missing" in rules(obj), "state-default-missing: 没有默认态 -> error")

        # ---------- ④ 文件齐套 / 自包含 ----------
        root, contract, designs, html = make_root("page-html-missing")
        os.remove(html)
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "page-html-missing" in rules(obj),
              "page-html-missing: 只有 .md 规格没有 .html -> error（md 不能替代 HTML）")

        root, contract, designs, html = make_root("page-spec-missing")
        shutil.copyfile(html, Path(designs, "orphan.html"))
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "page-spec-missing" in rules(obj),
              "page-spec-missing: 只有 .html 没有 .md 规格 -> error")

        root, contract, designs, html = make_root("html-external")
        check(rewrite(html, "</body>", '<script src="https://cdn.example.com/app.js"></script></body>'),
              "html-external-ref: 夹具插进了外链")
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "html-external-ref" in rules(obj), "html-external-ref: 外链资源 -> error")

        root, contract, designs, html = make_root("html-forbidden")
        check(rewrite(html, "错误：加载失败，请重试。", "错误：TODO 补文案"), "html-forbidden: 夹具插进了 TODO")
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "html-forbidden-string" in rules(obj), "html-forbidden-string: 占位文案 -> error")

        root, contract, designs, html = make_root("html-page-mismatch")
        check(rewrite(html, 'data-page="demo-page"', 'data-page="demo-pages"'), "html-page-mismatch: 夹具改了页面名")
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "html-page-mismatch" in rules(obj), "html-page-mismatch: data-page 与文件名不符 -> error")

        root, contract, designs, html = make_root("token-block-missing")
        check(rewrite(html, 'id="design-tokens"', 'id="tokens"'), "token-block-missing: 夹具改了令牌块 id")
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "token-block-missing" in rules(obj), "token-block-missing: 没有令牌块 -> error")

        # ---------- ⑤ 截图自证（四档/CDP/主色真画上）----------
        root, contract, designs, html = make_root("shot-missing")
        os.remove(Path(designs, "shots", "demo-page_768.png"))
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "shot-missing" in rules(obj), "shot-missing: 缺 768 档 -> error")

        root, contract, designs, html = make_root("shot-width")
        write_png(Path(designs, "shots", "demo-page_1024.png"), 768, 800)
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "shot-width-mismatch" in rules(obj),
              "shot-width-mismatch: 1024 档放 768 图 -> error")

        root, contract, designs, html = make_root("shot-blank")
        write_png(Path(designs, "shots", "demo-page_320.png"), 320, 800, band_rgb=(255, 255, 255), band_h=0)
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "shot-blank" in rules(obj), "shot-blank: 纯白图 -> error")
        check("shot-token-not-painted" in rules(obj),
              "shot-token-not-painted: 令牌写了主色但图里没画 -> error（这条是防'写得对、画得不对'）")

        root, contract, designs, html = make_root("shot-too-small")
        with open(Path(designs, "shots", "demo-page_1440.png"), "wb") as fh:
            write_png(Path(TMP, "tiny.png"), 1440, 120)
            fh.write(open(Path(TMP, "tiny.png"), "rb").read())
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "shot-too-small" in rules(obj), "shot-too-small: <200 高的图不认")

        root, contract, designs, html = make_root("shot-stale")
        with open(html, "a", encoding="utf-8") as fh:
            fh.write("<!-- touched -->\n")
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "shot-stale" in rules(obj),
              "shot-stale: HTML 改过没重出图 -> error（旧图看起来最像对的）")

        root, contract, designs, html = make_root("shot-no-manifest")
        os.remove(Path(designs, "shots", "manifest.json"))
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "shot-manifest-missing" in rules(obj),
              "shot-manifest-missing: 没有 manifest（不是 CDP 出的）-> error")

        root, contract, designs, html = make_root("shot-no-probes")
        os.remove(Path(designs, "shots", "probes.json"))
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "shot-probe-missing" in rules(obj),
              "shot-probe-missing: 没有探针（假档位无法识别）-> error")

        root, contract, designs, html = make_root("shot-probe-width")
        probes = _load(Path(designs, "shots", "probes.json"))
        probes["demo-page"]["320"]["innerWidth"] = 500  # CLI 320 档被钳成 500 的经典假档位
        _dump(Path(designs, "shots", "probes.json"), probes)
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "shot-probe-width-mismatch" in rules(obj),
              "shot-probe-width-mismatch: 布局视口 500 冒充 320 -> error")

        root, contract, designs, html = make_root("shot-overflow")
        probes = _load(Path(designs, "shots", "probes.json"))
        probes["demo-page"]["320"]["doc"]["scrollWidth"] = 360
        _dump(Path(designs, "shots", "probes.json"), probes)
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "shot-overflow" in rules(obj), "shot-overflow: 横向溢出 -> error")

        root, contract, designs, html = make_root("shot-stale-design")
        with open(Path(contract, "DESIGN.md"), "a", encoding="utf-8") as fh:
            fh.write("\n<!-- 令牌改过，截图该重出 -->\n")
        rc, obj, _ = lint_json(root)
        check(rc == 1 and "shot-stale-design" in rules(obj),
              "shot-stale-design: DESIGN.md 改过没重出图 -> error")

        # ---------- ⑥ 冒烟：--no-shots / --no-pixel 只降级"看照片"，不放过结构与令牌 ----------
        root, contract, designs, html = make_root("degrade")
        os.remove(Path(designs, "shots", "demo-page_320.png"))
        rc, obj, _ = lint_json(root, extra=["--no-shots"])
        check(obj.get("ok") is True, "degrade: --no-shots 跳过截图检查（结构仍过）")
        rc, obj, _ = lint_json(root)
        check(obj.get("ok") is False, "degrade: 不降级时同一夹具应失败")

        # ---------- ⑦ 检查器自变异 ----------
        # 变异副本放在 TMP 下，必须把 png_probe.py 一起搬过去——否则 import 失败会让像素检查整体降级，
        # "失去命中"就变成假证据（分不清是被改瞎还是根本没跑）。
        probe = Path(os.path.dirname(LINT), "png_probe.py")
        check(os.path.isfile(probe), "mutate: 找得到 png_probe.py（像素判据依赖它）")
        if os.path.isfile(probe):
            shutil.copyfile(probe, Path(TMP, "png_probe.py"))
        dest = Path(TMP, "lint_blind.py")
        with open(LINT, encoding="utf-8") as fh:
            src = fh.read()
        anchor = 'if st["blank_ratio"] > 0.985:'
        check(anchor in src, "mutate: blank_ratio 锚点仍在源码里")
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(src.replace(anchor, "if False:"))
        root, contract, designs, html = make_root("mut-blank")
        write_png(Path(designs, "shots", "demo-page_1024.png"), 1024, 800, band_rgb=(255, 255, 255), band_h=0)
        rc, obj, _ = lint_json(root)
        check("shot-blank" in rules(obj), "mut-blank: 变异前命中 shot-blank")
        proc = subprocess.run([sys.executable, "-X", "utf8", dest, "--root", root, "--json"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("shot-blank" not in proc.stdout,
              "mut-blank: 改瞎 blank 判据后不再命中（证明夹具打在它身上）")
        check("shot-token-not-painted" in proc.stdout,
              "mut-blank: 只剩该规则失去命中（其他判据仍活着，不是整体停摆）")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("")
    print("test_ui_design_lint: %d passed / %d failed" % (len(PASSES), len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  FAIL %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
