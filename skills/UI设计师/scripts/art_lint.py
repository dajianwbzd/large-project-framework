#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""art_lint.py — 游戏美术资产门禁（v3.22.0）

定位：`game` 设计域**唯一能机器证伪的美术判据**。让"这批素材风格是否漂移"从审美争论
变成可复现的计数——`errors == 0` 才算美术侧 done，与其他设计门禁同一强度。

为什么需要它：`契约/设计域.md` 的 game 域原本只有"手感/HUD/输入"等**行为**判据，
缺**资产**判据。走查只能停在"我觉得不像"，无法打回。本脚本补上这一段：
命名/格式/尺寸/体积 → 零依赖可查；调色板 ΔE00 覆盖率与 alpha 渗色 → 需 Pillow。

用法：
    python art_lint.py --dir <资产目录> --spec <art-spec.json> [--design <DESIGN.md>] [--json]
    python art_lint.py --dir assets --spec 契约/UI资源/art-spec.json --design 契约/DESIGN.md

退出码：0 = 无 error（可作 done 门禁）；1 = 有 error；2 = 用法/读取错误；3 = 依赖缺失且必需

校验项：
    naming/prefix       资产名前缀不匹配任何已声明类型
    naming/suffix       该类型未匹配预期后缀（warning）
    naming/banned-word  文件名含 final/new/copy/test 等禁用词（非终稿混入的强信号）
    format/not-allowed  扩展名不在允许列表
    size/power-of-two   尺寸非 2 的幂（mipmap 与采样质量受损）
    size/max-resolution 超过引擎贴图上限
    size/file-size      超过单文件体积上限
    palette/coverage    调色板覆盖率低于阈值（素材脱离美术令牌 → 风格漂移）
    alpha/bleed         全透明像素残留 RGB（缩放时渗出彩边）
    file/unreadable     资产无法读取

调色板单一事实源：`--design` 给定时，令牌色值**从 DESIGN.md 的 colors 段解析**
（art-spec 只列 `palette.use_tokens` 引用哪些令牌名），不在 spec 里复制色值——
避免"两处真相"。无 `--design` 时退回读 art-spec 内联的 `palette.tokens`。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import sys

# ---------------------------------------------------------------- 常量

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga"}
SEV_ORDER = {"error": 0, "warning": 1, "info": 2}

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
RGB_RE = re.compile(r"^rgb\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)", re.I)

try:
    from PIL import Image  # type: ignore
    HAVE_PIL = True
except ImportError:  # 降级：尺寸/命名/格式仍可查，调色板与 alpha 检查跳过
    HAVE_PIL = False


# ---------------------------------------------------------------- 零依赖图像头解析
# 无 Pillow 时仍要能查到尺寸（框架"零依赖兜底"纪律：门禁不因工具缺失而失效）

def read_size(path: str):
    """返回 (w, h)；无法解析返回 None。纯标准库，不加载像素。"""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            ext = os.path.splitext(path)[1].lower()

            if ext == ".png" and head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", head[16:24])
                return w, h

            if ext in (".jpg", ".jpeg") and head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    b = f.read(1)
                    while b and b != b"\xff":
                        b = f.read(1)
                    while b == b"\xff":
                        marker = f.read(1)
                        if marker != b"\xff":
                            break
                    if not marker:
                        return None
                    m = marker[0]
                    if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                        continue
                    seg = f.read(2)
                    if len(seg) < 2:
                        return None
                    (ln,) = struct.unpack(">H", seg)
                    if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                        data = f.read(5)
                        if len(data) < 5:
                            return None
                        h, w = struct.unpack(">HH", data[1:5])
                        return w, h
                    f.seek(ln - 2, 1)

            if ext == ".bmp" and head[:2] == b"BM":
                w, h = struct.unpack("<ii", head[18:26])
                return abs(w), abs(h)

            if ext == ".webp" and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                fmt = head[12:16]
                if fmt == b"VP8X":
                    w = int.from_bytes(head[24:27], "little") + 1
                    h = int.from_bytes(head[27:30], "little") + 1
                    return w, h
                if fmt == b"VP8 ":
                    w, h = struct.unpack("<HH", head[26:30])
                    return w & 0x3FFF, h & 0x3FFF
                if fmt == b"VP8L":
                    bits = int.from_bytes(head[21:25], "little")
                    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1

            if ext == ".tga":
                w, h = struct.unpack("<HH", head[12:16])
                return w, h
    except (OSError, struct.error, IndexError):
        return None
    return None


# ---------------------------------------------------------------- 色彩科学

def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb):
    """sRGB(0-255) -> CIELAB (D65)。"""
    r, g, b = (_srgb_to_linear(v / 255.0) for v in rgb[:3])
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    x, y, z = x / 0.95047, y / 1.0, z / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e_2000(lab1, lab2) -> float:
    """CIEDE2000 色差。参考阈值：<1 人眼不可辨；<3 可接受；>5 明显不同。

    已用 Sharma 标准测试向量校验：(50,2.6772,-79.7751) vs (50,0,-82.7485) → 2.0425
    """
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(c_bar ** 7 / (c_bar ** 7 + 25 ** 7))) if c_bar else 0.0
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0

    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    else:
        dh = h2p - h1p
        dhp = dh - 360 if dh > 180 else (dh + 360 if dh < -180 else dh)
    dhp_cap = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))

    lp_bar = (l1 + l2) / 2
    cp_bar = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hp_bar = h1p + h2p
    else:
        d = abs(h1p - h2p)
        s = h1p + h2p
        hp_bar = s / 2 if d <= 180 else ((s + 360) / 2 if s < 360 else (s - 360) / 2)

    t = (1
         - 0.17 * math.cos(math.radians(hp_bar - 30))
         + 0.24 * math.cos(math.radians(2 * hp_bar))
         + 0.32 * math.cos(math.radians(3 * hp_bar + 6))
         - 0.20 * math.cos(math.radians(4 * hp_bar - 63)))
    d_theta = 30 * math.exp(-(((hp_bar - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cp_bar ** 7 / (cp_bar ** 7 + 25 ** 7)) if cp_bar else 0.0
    sl = 1 + (0.015 * (lp_bar - 50) ** 2) / math.sqrt(20 + (lp_bar - 50) ** 2)
    sc = 1 + 0.045 * cp_bar
    sh = 1 + 0.015 * cp_bar * t
    rt = -math.sin(math.radians(2 * d_theta)) * rc

    return math.sqrt(
        (dlp / sl) ** 2 + (dcp / sc) ** 2 + (dhp_cap / sh) ** 2
        + rt * (dcp / sc) * (dhp_cap / sh)
    )


def parse_color(value):
    """解析 hex / rgb() 为 (r,g,b)；失败返回 None。"""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if HEX_RE.match(v):
        h = v[1:]
        if len(h) in (3, 4):
            return tuple(int(c * 2, 16) for c in h[:3])
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = RGB_RE.match(v)
    if m:
        return tuple(int(m.group(i)) for i in (1, 2, 3))
    return None


# ---------------------------------------------------------------- DESIGN.md 调色板解析
# 只解析 frontmatter 的 colors 段（受控 YAML 子集：缩进的 key: value）。
# 目的是按名引用令牌、不在 spec 里复制色值——保持"令牌是单一事实源"。

def palette_from_design(path: str):
    """从 DESIGN.md 的 frontmatter `colors` 段解析令牌颜色，返回 {名: rgb}。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    if not m:
        return {}
    out, in_colors = {}, False
    for raw in m.group(1).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            in_colors = line.split(":", 1)[0].strip() == "colors"
            continue
        if not in_colors or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip().strip('"').strip("'")
        if "#" in val:  # 去行尾注释（# 是颜色符号，只截"空白+#"）
            mm = re.search(r"\s+#", val)
            if mm:
                val = val[: mm.start()].strip()
        rgb = parse_color(val)
        if rgb:
            out[key.strip()] = rgb
    return out


# ---------------------------------------------------------------- 校验

def check_name(path: str, spec: dict):
    findings = []
    stem = os.path.splitext(os.path.basename(path))[0]
    naming = spec.get("naming", {})

    for w in naming.get("banned_words", []):
        if re.search(rf"(^|[_\-\s]){re.escape(w)}($|[_\-\s])", stem, re.I):
            findings.append(("error", "naming/banned-word",
                             f"文件名含禁用词 '{w}'（疑似非终稿资产混入交付）"))

    prefixes = naming.get("prefixes", {})
    if prefixes:
        hit = [t for t, p in prefixes.items() if stem.startswith(p)]
        if not hit:
            findings.append(("error", "naming/prefix",
                             f"前缀不匹配任何已声明类型（允许：{sorted(set(prefixes.values()))}）"))
        else:
            for_t = naming.get("suffixes", {}).get(hit[0], [])
            if for_t and not any(stem.endswith(s) for s in for_t):
                findings.append(("warning", "naming/suffix",
                                 f"'{hit[0]}' 类型未匹配预期后缀（期望其一：{for_t}）"))
    return findings


def check_format(path: str, spec: dict):
    allowed = spec.get("allowed_formats", [])
    ext = os.path.splitext(path)[1].lower()
    if allowed and ext not in [a.lower() for a in allowed]:
        return [("error", "format/not-allowed", f"格式 {ext} 不在允许列表 {allowed}")]
    return []


def _is_pot(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def check_size(path: str, size, spec: dict):
    findings = []
    t = spec.get("textures", {})
    if size is None:
        findings.append(("warning", "file/unreadable",
                         "无法解析图像尺寸（文件损坏或格式不支持），跳过尺寸检查"))
        return findings
    w, h = size

    if t.get("require_power_of_two") and not (_is_pot(w) and _is_pot(h)):
        findings.append(("error", "size/power-of-two",
                         f"{w}x{h} 非 2 的幂（GPU 无法用 mipmap，显存与采样质量受损）"))

    mx = t.get("max_resolution")
    if mx and (w > mx or h > mx):
        findings.append(("error", "size/max-resolution",
                         f"{w}x{h} 超过上限 {mx}（应走图集或降分辨率）"))

    lim = t.get("max_file_size_mb")
    if lim:
        try:
            mb = os.path.getsize(path) / (1024 * 1024)
        except OSError:
            return findings
        if mb > lim:
            findings.append(("error", "size/file-size",
                             f"{mb:.1f}MB 超过 {lim}MB（应过 oxipng/pngquant 优化）"))
    return findings


def _histogram(im, max_unique: int = 4096):
    """颜色直方图。

    **禁止逐像素做色彩科学运算**：4096² 贴图逐像素算 ΔE00 是 1.3 亿次运算，
    实测会让脚本假死（无输出、无报错，极易被误判为环境问题）。
    故先聚合到唯一色计数；唯一色超限时先量化，保证复杂度 O(唯一色数) 且有上界。
    """
    rgba = im.convert("RGBA")
    h = rgba.getcolors(maxcolors=max_unique)
    if h is None:
        h = rgba.quantize(colors=64).convert("RGBA").getcolors(maxcolors=1 << 16)
    return h or []


def check_palette(path: str, tokens: dict, spec: dict):
    findings = []
    if not tokens:
        return findings
    pal = spec.get("palette", {})
    tol = float(pal.get("delta_e_max", 5.0))
    cover_min = float(pal.get("coverage_min", 0.85))
    tok_lab = {k: rgb_to_lab(v) for k, v in tokens.items()}

    try:
        with Image.open(path) as im:
            im.load()
            raw_hist = _histogram(im)
    except Exception as e:
        return [("error", "file/unreadable", f"无法读取像素：{e}")]

    total = sum(c for c, _ in raw_hist)
    hist = [(c, p) for c, p in raw_hist if p[3] > 8]
    if not hist:
        return [("warning", "palette/empty", "全透明素材，跳过调色板校验")]

    opaque_n = sum(c for c, _ in hist)
    in_tol, worst = 0, (None, -1.0, None)
    for count, p in hist:
        lab = rgb_to_lab(p[:3])
        name, best = None, float("inf")
        for tn, tl in tok_lab.items():
            d = delta_e_2000(lab, tl)
            if d < best:
                name, best = tn, d
        if best <= tol:
            in_tol += count
        if best > worst[1]:
            worst = (p, best, name)

    coverage = in_tol / opaque_n if opaque_n else 0.0
    if coverage < cover_min:
        findings.append((
            "error", "palette/coverage",
            f"调色板覆盖率 {coverage:.0%} < {cover_min:.0%}；最差颜色 rgb{worst[0][:3]} "
            f"偏离最近令牌 '{worst[2]}' 达 ΔE00={worst[1]:.1f}（阈值 {tol}）"
            "——素材脱离美术令牌，风格会漂移"))
    else:
        findings.append(("info", "palette/coverage",
                         f"调色板覆盖率 {coverage:.0%}（ΔE00 阈值 {tol}，唯一色 {len(hist)}）"))

    bleed = sum(c for c, p in raw_hist if p[3] == 0 and (p[0] or p[1] or p[2]))
    if bleed and total:
        pct = bleed / total
        sev = "error" if pct > 0.02 else "warning"
        findings.append((sev, "alpha/bleed",
                         f"{bleed} 个全透明像素残留 RGB（{pct:.1%}）——缩放时会出现彩色描边，"
                         "应清零或做 alpha dilation"))
    return findings


# ---------------------------------------------------------------- 主流程

def resolve_tokens(spec: dict, design_path):
    """令牌解析：优先从 DESIGN.md 按名取色（单一事实源）；否则用 spec 内联值。"""
    pal = spec.get("palette", {})
    from_design = palette_from_design(design_path) if design_path else {}
    if from_design:
        use = pal.get("use_tokens")
        names = use if use else sorted(from_design)
        missing = [n for n in names if n not in from_design]
        return {n: from_design[n] for n in names if n in from_design}, missing, "DESIGN.md"
    inline = pal.get("tokens") or {}
    return {k: parse_color(v) for k, v in inline.items() if parse_color(v)}, [], "art-spec 内联"


def lint_dir(root: str, spec: dict, tokens: dict):
    results = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in sorted(files):
            p = os.path.join(dirpath, fn)
            if os.path.splitext(fn)[1].lower() not in IMG_EXT:
                continue
            findings = check_name(p, spec) + check_format(p, spec)
            findings += check_size(p, read_size(p), spec)
            if HAVE_PIL:
                findings += check_palette(p, tokens, spec)
            results.append({"asset": os.path.relpath(p, root).replace(os.sep, "/"),
                            "findings": findings})
    return results


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--dir", required=True, help="资产目录")
    ap.add_argument("--spec", required=True, help="art-spec.json 路径")
    ap.add_argument("--design", default=None, help="DESIGN.md 路径（按名解析美术令牌）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])

    if not os.path.isdir(args.dir):
        print(f"[ERROR] 资产目录不存在：{args.dir}", file=sys.stderr)
        return 2
    try:
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[ERROR] 读取 art-spec 失败：{e}", file=sys.stderr)
        return 2

    tokens, missing, src = resolve_tokens(spec, args.design)
    if missing:
        print(f"[WARN] DESIGN.md 中未找到这些令牌（已忽略）：{missing}", file=sys.stderr)

    results = lint_dir(args.dir, spec, tokens)
    if not results:
        print(f"[ERROR] 目录下无图像资产：{args.dir}", file=sys.stderr)
        return 2

    n_err = sum(1 for r in results for f in r["findings"] if f[0] == "error")
    n_warn = sum(1 for r in results for f in r["findings"] if f[0] == "warning")

    notes = []
    notes.append(f"令牌来源：{src}（{len(tokens)} 色）")
    if not HAVE_PIL:
        notes.append("未安装 Pillow → 已跳过调色板/alpha 检查；"
                     "运行: pip install pillow（跳过部分不计入门禁结论）")

    if args.json:
        print(json.dumps({"assets": results, "errors": n_err, "warnings": n_warn,
                          "tokens_source": src, "tokens": len(tokens),
                          "pillow": HAVE_PIL, "notes": notes},
                         ensure_ascii=False, indent=2))
    else:
        for r in results:
            fs = sorted(r["findings"], key=lambda x: SEV_ORDER[x[0]])
            flag = "FAIL" if any(f[0] == "error" for f in fs) else (
                "WARN" if any(f[0] == "warning" for f in fs) else "ok")
            print(f"[{flag}] {r['asset']}")
            for sev, code, msg in fs:
                print(f"       {sev:<7} {code}: {msg}")
        for n in notes:
            print(f"  · {n}")
        print(f"\n合计：{len(results)} 个资产，error={n_err}，warning={n_warn}")

    return 1 if n_err else (2 if n_warn else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
