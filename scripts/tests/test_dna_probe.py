# -*- coding: utf-8 -*-
"""dna_probe.py 回归测试（纯 stdlib，不依赖 pytest / PIL）。

覆盖：
  1. 亮色截图 → 色板/背景/文字/强调色/明暗模式/对比度可复现
  2. 暗色截图 → 判暗色
  3. tokens.css 骨架与 DESIGN.md 骨架的必需字段 + 「推不出」占位声明
  4. 人读文本包含边界说明
  5. 非法输入：非 PNG / --step 0 → SystemExitErr 与退出码 2（三态，不静默通过）
  6. 往返工具：to_hex / contrast_ratio 基本正确性

跑法（框架根目录下）：
  $env:PYTHONUTF8='1'; python -X utf8 scripts/tests/test_dna_probe.py -v
"""

import os
import shutil
import struct
import sys
import tempfile
import traceback
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK = os.path.dirname(os.path.dirname(HERE))
SKILL_SCRIPTS = os.path.join(FRAMEWORK, "skills", "UI设计师", "scripts")

if SKILL_SCRIPTS not in sys.path:
    sys.path.insert(0, SKILL_SCRIPTS)

import dna_probe  # noqa: E402

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
FAILS = []
PASSES = []


def check(cond, label):
    if cond:
        PASSES.append(label)
        if VERBOSE:
            print("  ok   " + label)
    else:
        FAILS.append(label)
        print("  FAIL " + label)


# ------------------------------------------------------------------ PNG 夹具


def png_bytes(width, bands):
    """bands = [(相对高度比例, (r,g,b)), ...]，按顺序从上到下铺满。"""
    rows = []
    total = sum(frac for frac, _ in bands)
    plan = []
    for frac, color in bands:
        n = int(round(width * width * frac / total))
        plan.append((n, color))
    plan[-1] = (width - sum(n for n, _ in plan[:-1]), plan[-1][1])  # 补齐最后一条

    for n, color in plan:
        for _ in range(n // width if width else 0):
            rows.append(b"\x00" + bytes(color) * width)
    while len(rows) < width:
        rows.append(b"\x00" + bytes(plan[-1][1]) * width)

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, len(rows), 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + chunk(b"IEND", b"")
    )


SURFACE = (247, 248, 250)   # #F7F8FA  亮底
TEXT = (26, 28, 30)         # #1A1C1E  深字
ACCENT = (228, 87, 46)      # #E4572E  暖强调
MUTED = (237, 239, 242)     # #EDEFF2  次表面

DARK_BG = (18, 20, 23)      # #121417  暗底
DARK_TEXT = (242, 244, 247)  # #F2F4F7 浅字


def main():
    tmp = tempfile.mkdtemp(prefix="_fixture_tmp_dna_")
    try:
        light = os.path.join(tmp, "light.png")
        with open(light, "wb") as fh:
            fh.write(png_bytes(80, [(0.60, SURFACE), (0.20, TEXT), (0.10, MUTED), (0.10, ACCENT)]))

        dark = os.path.join(tmp, "dark.png")
        with open(dark, "wb") as fh:
            fh.write(png_bytes(60, [(0.70, DARK_BG), (0.30, DARK_TEXT)]))

        broken = os.path.join(tmp, "broken.png")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("这不是 PNG\n")

        # ① 亮色截图
        r = dna_probe.probe(light)
        hexes = [c["hex"] for c in r["palette"]]
        check(all(h in hexes for h in ("#F7F8FA", "#1A1C1E", "#EDEFF2", "#E4572E")),
              "色板含四个已知色（实际 %s）" % hexes)
        check(r["background"] and r["background"]["hex"] == "#F7F8FA",
              "背景候选 = #F7F8FA（实际 %r）" % (r["background"] or {}).get("hex"))
        check(r["text"] and r["text"]["hex"] == "#1A1C1E",
              "文字候选 = #1A1C1E（实际 %r）" % (r["text"] or {}).get("hex"))
        check([a["hex"] for a in r["accents"]][:1] == ["#E4572E"],
              "强调色候选含 #E4572E（实际 %s）" % [a["hex"] for a in r["accents"]])
        check(r["mode"].startswith("亮色"), "判亮色（实际 %r）" % r["mode"])
        check(r["luminance"]["contrast_text_bg"] and r["luminance"]["contrast_text_bg"] > 12,
              "文字/背景对比 > 12（实际 %r）" % r["luminance"]["contrast_text_bg"])
        check(abs(r["background"]["share"] - 0.60) < 0.06,
              "背景占比 ≈60%%（实际 %.3f）" % r["background"]["share"])
        check(r["sampled_pixels"] > 0 and r["width"] == 80, "抽样点数与尺寸正确")
        check(len(r["caveats"]) == 3, "三条边界声明齐全")

        # ② 暗色截图
        d = dna_probe.probe(dark)
        check(d["mode"].startswith("暗色"), "判暗色（实际 %r）" % d["mode"])
        check(d["background"]["hex"] == "#121417", "暗色背景 #121417（实际 %r）" % d["background"]["hex"])
        check(d["text"]["hex"] == "#F2F4F7", "暗色文字 #F2F4F7（实际 %r）" % d["text"]["hex"])

        # ③ tokens.css 骨架
        css = dna_probe._css(r)
        check(":root {" in css, "css 骨架有 :root")
        check("--color-surface: #F7F8FA" in css, "css 含 --color-surface")
        check("--color-primary: #1A1C1E" in css, "css 含 --color-primary")
        check("推不出来" in css, "css 标了「推不出来」占位说明")

        # ④ DESIGN.md 骨架
        md = dna_probe._design_md(r)
        check(md.startswith("---"), "design_md 以 frontmatter 起头")
        check("name: " in md, "design_md 含 name")
        check("colors:" in md, "design_md 含 colors")
        check("components: {}" in md, "design_md 的 components 留空待人补")
        check("## Overview" in md, "design_md 有人写的正文段")

        # ⑤ 人读文本
        txt = dna_probe._text(r)
        check("设计 DNA 候选" in txt, "人读文本标题")
        check("推不出来" in txt, "人读文本含边界说明")

        # ⑥ 非法输入
        try:
            dna_probe.probe(broken)
            check(False, "非 PNG 抛 SystemExitErr")
        except dna_probe.SystemExitErr:
            check(True, "非 PNG 抛 SystemExitErr")

        rc_out = os.path.join(tmp, "out", "tokens.css")
        check(dna_probe.main([light, "--css", "--out", rc_out]) == 0, "--out 正常退出码 0")
        check(os.path.isfile(rc_out) and os.path.getsize(rc_out) > 0, "--out 写出了非空文件")
        check(dna_probe.main([broken, "--json"]) == 2, "坏图退出码 2（无法判断）")
        check(dna_probe.main([light, "--step", "0"]) == 2, "--step 0 退出码 2")

        # ⑦ 工具函数
        check(dna_probe.to_hex((228, 87, 46)) == "#E4572E", "to_hex 正确")
        check(abs(dna_probe.contrast_ratio((0, 0, 0), (255, 255, 255)) - 21.0) < 0.05,
              "黑白对比 ≈21:1")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print("用例执行异常 → 按失败计")
        sys.exit(1)
    print("\n通过 %d / 失败 %d" % (len(PASSES), len(FAILS)))
    if FAILS:
        print("失败用例：")
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("全部通过")
