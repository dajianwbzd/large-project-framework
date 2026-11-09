#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dna_probe.py —— 从参考截图抽「设计 DNA 候选」（零依赖，复用 png_probe 的 PNG 解码器）

**它做什么**：把截图里**可测的东西**量出来——主色板与占比、背景/文字/强调色候选、明度分布、
对比度（WCAG）、明暗模式判定；并给出 `tokens.css` / `DESIGN.md` frontmatter 的**骨架**。

**它不做什么（诚实边界，别指望它）**：
  - 字号阶、间距节奏、圆角档位、组件结构、动效意图、信息层级 —— **从像素推不出来**，
    需要 DOM/CSS 或人判；这些在输出里一律标「占位」，必须在 §4.1 契约化时人工定。
  - 判不了「好不好看」：它只报数，不评价。选不选这个参考、要不要跟它像，是人（和用户）的事。
  - 只吃 PNG（`png_probe` 只解 8bit 非隔行 colortype 2/6）。JPEG/WebP 请先转换：
    `ffmpeg -i shot.jpg shot.png`（或 `magick shot.jpg shot.png`）。
  - 采样是**等步长抽样**（默认每 3px 一个点），不是全像素统计；截图有大量渐变/照片时，
    占比只代表"大致分布"，不能当精确色值来源。

用法：
  python -X utf8 dna_probe.py <png...> [--json]
  python -X utf8 dna_probe.py shot.png --css        # 打印 tokens.css 骨架（候选）
  python -X utf8 dna_probe.py shot.png --design-md  # 打印 DESIGN.md frontmatter 骨架（候选）
  python -X utf8 dna_probe.py shot.png --out x.json

退出码：0 成功 ｜ 2 用法错 / 不是可解析的 PNG
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import png_probe  # noqa: E402  （同目录，角色自带的零依赖 PNG 解码器）


def _srgb_channel(v):
    c = v / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = rgb
    return (
        0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g) + 0.0722 * _srgb_channel(b)
    )


def contrast_ratio(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def to_hsl(rgb):
    r, g, b = [v / 255.0 for v in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    lightness = (mx + mn) / 2.0
    d = mx - mn
    if d == 0:
        return 0.0, 0.0, lightness
    sat = d / (1 - abs(2 * lightness - 1)) if abs(1 - abs(2 * lightness - 1)) > 1e-9 else 0.0
    if mx == r:
        hue = 60 * (((g - b) / d) % 6)
    elif mx == g:
        hue = 60 * ((b - r) / d + 2)
    else:
        hue = 60 * ((r - g) / d + 4)
    return hue % 360.0, sat, lightness


def to_hex(rgb):
    return "#%02X%02X%02X" % tuple(int(max(0, min(255, round(v)))) for v in rgb)


def probe(path, step=3, top_n=12):
    try:
        img = png_probe.decode(path)
    except Exception as exc:  # noqa: BLE001  解码器对不支持的格式会抛各种错
        raise SystemExitErr("无法解析 PNG：%s（%s）" % (path, exc))

    width, height = img["width"], img["height"]
    bpp = img.get("bpp") or 3
    rows = img["rows"]

    buckets = {}      # (r>>3,g>>3,b>>3) -> [count, sum_r, sum_g, sum_b]
    total = 0
    lum_sum = 0.0
    lum_samples = []
    for y in range(0, height, step):
        row = rows[y]
        for x in range(0, width, step):
            off = x * bpp
            if off + 2 >= len(row):
                continue
            r, g, b = row[off], row[off + 1], row[off + 2]
            key = (r >> 3, g >> 3, b >> 3)
            slot = buckets.get(key)
            if slot is None:
                buckets[key] = [1, r, g, b]
            else:
                slot[0] += 1
                slot[1] += r
                slot[2] += g
                slot[3] += b
            total += 1
            if len(lum_samples) < 200000:
                lum_samples.append(round(luminance((r, g, b)), 4))
            lum_sum += luminance((r, g, b))

    if total == 0:
        raise SystemExitErr("采样为 0 个像素：%s（图片尺寸 %dx%d 或解码异常）" % (path, width, height))

    colors = []
    for count, sr, sg, sb in buckets.values():
        rgb = (sr / float(count), sg / float(count), sb / float(count))
        hue, sat, light = to_hsl(rgb)
        colors.append(
            {
                "hex": to_hex(rgb),
                "rgb": [int(round(v)) for v in rgb],
                "share": count / float(total),
                "hue": round(hue, 1),
                "sat": round(sat, 3),
                "light": round(light, 3),
                "luminance": round(luminance(rgb), 4),
            }
        )
    colors.sort(key=lambda c: c["share"], reverse=True)
    palette = colors[:top_n]

    bg = palette[0] if palette[0]["share"] >= 0.20 else None
    luminance_sorted = sorted(lum_samples)
    mean_light = lum_sum / float(total)
    p10 = luminance_sorted[int(len(luminance_sorted) * 0.10)]
    p90 = luminance_sorted[min(len(luminance_sorted) - 1, int(len(luminance_sorted) * 0.90))]

    text_candidate = None
    if bg:
        candidates = [c for c in colors if c["share"] >= 0.003 and c["hex"] != bg["hex"]]
        if candidates:
            text_candidate = max(candidates, key=lambda c: contrast_ratio(c["rgb"], bg["rgb"]))

    accents = [c for c in colors if c["sat"] >= 0.35 and c["share"] >= 0.003 and c["hex"] != (bg or {}).get("hex")]
    accents.sort(key=lambda c: c["share"], reverse=True)
    accents = accents[:3]

    muted = None
    border = None
    secondary = None
    text_hex = text_candidate["hex"] if text_candidate else None
    if bg:
        for c in colors:
            if c["hex"] == bg["hex"] or c["share"] < 0.01:
                continue
            if muted is None and abs(c["luminance"] - bg["luminance"]) < 0.08:
                muted = c
            # 分割线：低饱和、介于底与字之间、且不是文字色本身
            if (
                border is None
                and c["sat"] < 0.18
                and c["hex"] != text_hex
                and bg["luminance"] > c["luminance"] > 0
                and c["share"] >= 0.002
            ):
                border = c
        for c in colors:
            if c["hex"] in (bg["hex"], text_hex) or c["share"] < 0.002:
                continue
            if secondary is None and c["sat"] < 0.20:
                # 次要文字：明度落在底与字之间、且不是浅色底
                span_lo, span_hi = sorted((bg["luminance"], (text_candidate or bg)["luminance"]))
                if span_lo < c["luminance"] < span_hi:
                    secondary = c

    mode = "未知"
    if bg:
        mode = "亮色（浅底深字）" if bg["luminance"] > 0.45 else "暗色（深底浅字）"

    return {
        "file": os.path.abspath(path),
        "width": width,
        "height": height,
        "aspect": round(width / float(height), 4) if height else None,
        "sample_step": step,
        "sampled_pixels": total,
        "mode": mode,
        "palette": palette,
        "background": bg,
        "text": text_candidate,
        "secondary": secondary,
        "surface_muted": muted,
        "border": border,
        "accents": accents,
        "luminance": {
            "mean": round(mean_light, 4),
            "p10": round(p10, 4),
            "p90": round(p90, 4),
            "contrast_text_bg": round(contrast_ratio(text_candidate["rgb"], bg["rgb"]), 2)
            if (text_candidate and bg)
            else None,
        },
        "caveats": [
            "等步长抽样，占比是分布估计，不是精确色值来源",
            "字号阶/间距/圆角/组件/动效从像素推不出来，必须人工定",
            "不评价好看与否；参考要不要跟，由人决定",
        ],
    }


class SystemExitErr(Exception):
    pass


def _css(result):
    bg = result["background"]
    text = result["text"]
    muted = result["surface_muted"]
    border = result["border"]
    accents = result["accents"]
    today = datetime.date.today().isoformat()
    lines = []
    lines.append(
        "/* 由 dna_probe.py 从 %s 抽出（%s）—— 全部是**候选值**，须人核后落进 DESIGN.md */"
        % (os.path.basename(result["file"]), today)
    )
    lines.append("/* 截图 %dx%d · %s · 抽样 %d 点 */" % (
        result["width"], result["height"], result["mode"], result["sampled_pixels"]))
    lines.append(":root {")
    if bg:
        lines.append("  --color-surface: %s;        /* 占 %.1f%% · 背景候选 */" % (bg["hex"], bg["share"] * 100))
    if muted:
        lines.append("  --color-surface-muted: %s;  /* 占 %.1f%% · 次表面候选 */" % (muted["hex"], muted["share"] * 100))
    if text:
        lines.append(
            "  --color-primary: %s;        /* 占 %.1f%% · 与 surface 对比 %.1f:1（文字候选）*/"
            % (text["hex"], text["share"] * 100, contrast_ratio(text["rgb"], bg["rgb"]) if bg else 0.0)
        )
    if border:
        lines.append("  --color-border: %s;         /* 占 %.1f%% · 低饱和、介于底与字之间 */" % (border["hex"], border["share"] * 100))
    if result.get("secondary"):
        sec = result["secondary"]
        lines.append("  --color-secondary: %s;      /* 占 %.1f%% · 次要文字候选 */" % (sec["hex"], sec["share"] * 100))
    for i, acc in enumerate(accents):
        lines.append(
            "  --color-%s: %s;        /* 占 %.1f%% · 强调色候选（饱和度 %.0f%%）*/"
            % ("accent" if i == 0 else "accent-%d" % (i + 1), acc["hex"], acc["share"] * 100, acc["sat"] * 100)
        )
    lines.append("  /* 以下三项从像素推不出来，是占位：按 §4.1 DESIGN.md 规范人工定 */")
    lines.append('  --radius-md: 8px;')
    lines.append('  --space-md: 16px;')
    lines.append(
        '  --font-body: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;'
    )
    lines.append("}")
    return "\n".join(lines)


def _design_md(result):
    bg = result["background"]
    text = result["text"]
    muted = result["surface_muted"]
    border = result["border"]
    accents = result["accents"]
    primary = (text or bg or {"hex": "#1A1C1E"})["hex"]
    surface = (bg or {"hex": "#FFFFFF"})["hex"]
    secondary = (result.get("secondary") or muted or border or text or {"hex": "#5C6266"})["hex"]
    lines = [
        "---",
        "# 由 dna_probe.py 抽出的**骨架**（候选值，非定稿）：先补 components，再过 design_lint 零 error",
        "version: alpha",
        "name: <项目名> 设计语言",
        "colors:",
        '  primary: "%s"        # 文字候选（来自抽样占比 %.1f%%）' % (primary, (text or bg or {"share": 0}).get("share", 0) * 100),
        '  secondary: "%s"' % secondary,
        '  surface: "%s"        # 背景候选（占 %.1f%%）' % (surface, (bg or {"share": 0})["share"] * 100),
    ]
    if muted:
        lines.append('  surface-muted: "%s"' % muted["hex"])
    if border:
        lines.append('  border: "%s"' % border["hex"])
    for i, acc in enumerate(accents):
        lines.append('  %s: "%s"        # 强调色候选' % ("alert" if i == 0 else "accent-%d" % (i + 1), acc["hex"]))
    lines += [
        "typography:",
        "  # 字号/行高从截图推不出来：以下为规范默认，按项目改",
        "  body:",
        '    fontFamily: "-apple-system, \\"Segoe UI\\", \\"PingFang SC\\", \\"Microsoft YaHei\\", sans-serif"',
        "    fontSize: 14px",
        "    fontWeight: 400",
        "    lineHeight: 1.5",
        "rounded:",
        "  sm: 4px",
        "  md: 8px",
        "spacing:",
        "  xs: 4px",
        "  sm: 8px",
        "  md: 16px",
        "  lg: 24px",
        "components: {}   # 必须人补（design_lint 会报 orphaned-tokens，直到组件引用令牌）",
        "---",
        "",
        "## Overview",
        "",
        "（人写：一句话说清取向与理由，以及为什么没选其它候选方向）",
        "",
        "## Colors",
        "",
        "（人写：逐令牌写用途与对比度；脚本给的对比度见 dna_probe --json 的 luminance.contrast_text_bg）",
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="从参考截图抽设计 DNA 候选（色板/明度/对比度）")
    ap.add_argument("pngs", nargs="+", help="PNG 文件（可用 ffmpeg/magick 先转）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--css", action="store_true", help="打印 tokens.css 骨架（仅用第一张图）")
    ap.add_argument("--design-md", action="store_true", help="打印 DESIGN.md frontmatter 骨架（仅用第一张图）")
    ap.add_argument("--out", help="把输出写到文件（不打印）")
    ap.add_argument("--step", type=int, default=3, help="抽样步长（默认 3px）")
    args = ap.parse_args(argv)

    if args.step < 1:
        sys.stderr.write("[ERROR] --step 必须 ≥1\n")
        return 2
    try:
        results = [probe(p, step=args.step) for p in args.pngs]
    except SystemExitErr as exc:
        sys.stderr.write("[ERROR] %s\n" % exc)
        return 2

    if args.css:
        payload = _css(results[0])
    elif args.design_md:
        payload = _design_md(results[0])
    elif args.json or len(results) > 1:
        payload = json.dumps(results if len(results) > 1 else results[0], ensure_ascii=False, indent=2)
    else:
        payload = _text(results[0])

    if args.out:
        out_dir = os.path.dirname(os.path.abspath(args.out))
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        sys.stdout.write("[OK] 已写入 %s\n" % os.path.abspath(args.out))
    else:
        sys.stdout.write(payload + "\n")
    return 0


def _text(result):
    def fmt(c, note=""):
        return "%-8s 占 %5.1f%%  H %5.1f° S %4.2f L %4.2f  对比 %5.1f:1 %s" % (
            c["hex"],
            c["share"] * 100,
            c["hue"],
            c["sat"],
            c["light"],
            contrast_ratio(c["rgb"], result["background"]["rgb"]) if result["background"] else 0.0,
            note,
        )

    lines = []
    lines.append("设计 DNA 候选 · %s" % result["file"])
    lines.append(
        "尺寸 %dx%d（比例 %.2f）· %s · 抽样 %d 点（步长 %d）"
        % (result["width"], result["height"], result["aspect"] or 0, result["mode"],
           result["sampled_pixels"], result["sample_step"])
    )
    lines.append("")
    lines.append("色板（按占比，前 %d）:" % len(result["palette"]))
    for c in result["palette"]:
        lines.append("  %s  占 %5.1f%%  H %5.1f° S %4.2f L %4.2f" % (
            c["hex"], c["share"] * 100, c["hue"], c["sat"], c["light"]))
    lines.append("")
    if result["background"]:
        lines.append("背景候选 : %s" % fmt(result["background"]).strip())
    else:
        lines.append("背景候选 : 未定（最大占比 < 20%，可能是照片/渐变占主导——别硬套色板）")
    if result["text"]:
        lines.append("文字候选 : %s" % fmt(result["text"]).strip())
    if result.get("secondary"):
        lines.append("次要文字 : %s" % fmt(result["secondary"]).strip())
    if result["surface_muted"]:
        lines.append("次表面   : %s" % fmt(result["surface_muted"]).strip())
    if result["border"]:
        lines.append("分割线   : %s" % fmt(result["border"]).strip())
    if result["accents"]:
        lines.append("强调色候选:")
        for a in result["accents"]:
            lines.append("  %s  占 %5.1f%%  饱和度 %4.0f%%" % (a["hex"], a["share"] * 100, a["sat"] * 100))
    lum = result["luminance"]
    lines.append("")
    lines.append(
        "明度：均值 %.3f ｜ p10 %.3f ｜ p90 %.3f ｜ 文字/背景对比 %s"
        % (lum["mean"], lum["p10"], lum["p90"],
           ("%.1f:1" % lum["contrast_text_bg"]) if lum["contrast_text_bg"] else "不可算")
    )
    lines.append("")
    lines.append("边界：字号阶/间距/圆角/组件/动效从像素推不出来（要 DOM/CSS 或人判）；占比是抽样估计；不评价好坏。")
    lines.append("下一步：`--css` 出 tokens.css 骨架 / `--design-md` 出 DESIGN.md frontmatter 骨架 → §2 方向探索核对 → §4.1 定稿并过 lint。")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
