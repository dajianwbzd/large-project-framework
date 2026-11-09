#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""png_probe.py —— 零依赖 PNG 解码与像素统计（供 ui_shots.py / *lint.py 复用）

只支持 Chrome/Edge headless 截图产出的常见形态：8bit · 非隔行 · colortype 2(RGB) / 6(RGBA)。
用途：把"截图是不是空白图/是不是真的画上了令牌色"变成可复算的判定。

CLI:
    python -X utf8 png_probe.py <png> [--color '#1A1C1E'] [--json]

口径：
    blank_ratio   = 占比最高的单色像素比例（>0.985 视为近空白图）
    variance      = 采样像素的平均通道标准差（越小越平）
    color_share   = 目标色像素占比（容差 ±6/通道，兼容抗锯齿）
    passed        = (width>0) and (blank_ratio<=0.985) and (variance>=3.0)
                     且给了 --color 时 color_share>=0.003
"""
import json
import struct
import sys
import zlib
from collections import Counter

SAMPLE_STEP = 3          # 像素采样步长（性能与精度折中）
BLANK_RATIO_MAX = 0.985
VARIANCE_MIN = 3.0
COLOR_TOL = 6
COLOR_SHARE_MIN = 0.003


def _read_chunks(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a PNG file")
    pos = 8
    chunks = {}
    idat = []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            chunks["IHDR"] = body
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
    if "IHDR" not in chunks:
        raise ValueError("missing IHDR")
    return chunks["IHDR"], b"".join(idat)


def _unfilter(raw, width, height, bpp, stride):
    out = []
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 0:
            pass
        elif ftype == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise ValueError("unsupported PNG filter type %d" % ftype)
        out.append(bytes(line))
        prev = line
    return out


def decode(path):
    ihdr, idat = _read_chunks(path)
    width, height, bitdepth, colortype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr[:13])
    if bitdepth != 8 or interlace != 0 or colortype not in (2, 6):
        raise ValueError("unsupported PNG: bitdepth=%d colortype=%d interlace=%d" % (bitdepth, colortype, interlace))
    bpp = 3 if colortype == 2 else 4
    stride = width * bpp
    raw = zlib.decompress(idat)
    if len(raw) < height * (stride + 1):
        raise ValueError("truncated PNG data")
    rows = _unfilter(raw, width, height, bpp, stride)
    return {"width": width, "height": height, "bpp": bpp, "rows": rows}


def stats(path, color=None, step=SAMPLE_STEP):
    img = decode(path)
    rows, bpp, width = img["rows"], img["bpp"], img["width"]
    counter = Counter()
    sums = [0.0, 0.0, 0.0]
    sqs = [0.0, 0.0, 0.0]
    n = 0
    target = None
    if color:
        c = color.strip().lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        target = (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    hit = 0
    for y in range(0, len(rows), step):
        line = rows[y]
        for x in range(0, width, step):
            i = x * bpp
            r, g, b = line[i], line[i + 1], line[i + 2]
            n += 1
            counter[(r >> 3, g >> 3, b >> 3)] += 1
            for k, v in enumerate((r, g, b)):
                sums[k] += v
                sqs[k] += v * v
            if target and abs(r - target[0]) <= COLOR_TOL and abs(g - target[1]) <= COLOR_TOL and abs(b - target[2]) <= COLOR_TOL:
                hit += 1
    if n == 0:
        raise ValueError("empty image")
    var = 0.0
    for k in range(3):
        mean = sums[k] / n
        var += max(0.0, sqs[k] / n - mean * mean)
    var = (var / 3.0) ** 0.5
    blank_ratio = counter.most_common(1)[0][1] / n
    res = {
        "path": path,
        "width": img["width"],
        "height": img["height"],
        "sampled": n,
        "blank_ratio": round(blank_ratio, 4),
        "variance": round(var, 2),
        "unique_buckets": len(counter),
    }
    if target:
        res["color"] = color
        res["color_share"] = round(hit / n, 4)
    res["passed"] = (
        res["width"] > 0
        and res["blank_ratio"] <= BLANK_RATIO_MAX
        and res["variance"] >= VARIANCE_MIN
        and (("color_share" not in res) or res["color_share"] >= COLOR_SHARE_MIN)
    )
    return res


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="PNG 像素统计（零依赖）")
    ap.add_argument("png")
    ap.add_argument("--color", default=None, help="目标色，如 '#1A1C1E'")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        res = stats(a.png, a.color)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"path": a.png, "passed": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print("width=%(width)d height=%(height)d blank_ratio=%(blank_ratio)s variance=%(variance)s passed=%(passed)s" % res)
        if "color_share" in res:
            print("color=%s color_share=%s" % (res["color"], res["color_share"]))
    return 0 if res["passed"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
