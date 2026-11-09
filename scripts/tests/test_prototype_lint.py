#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_prototype_lint.py — 原型门禁（spec / HTML / 截图三层）的回归 + 变异对拍（纯 stdlib，不依赖 pytest）

夹具写法自 2026-10-25 起 pathlib 化（Path.write_text / write_bytes），不用裸 open 写临时文件。

为什么有这个脚本
----------------
`prototype_lint.py` 三层判据里，**截图层的多数分支在真实工程上一次都跑不到**：
真实工程要么还没出原型（整层不跑），要么交付的是"干净"的原型（只跑通过路径）。
也就是说 `shots-stale` / `shots-stale-hash` / `shots-width-mismatch` / `shots-probe-missing` /
`shots-probe-width-mismatch` / `shots-overflow` 这些分支**从没被任何真实数据执行过**——
发布时它们是**未验证代码**。

两条判据缺一不可（本框架踩过的坑）：
  ① **能拦**：每条规则都要有一个**故意让它失败**的夹具。只跑真实工程 = 只证明「不报错」。
  ② **不乱拦**：还要有一个**干净夹具**，断言 0 error 0 warn。
     变异只能证明 ①，所以「改了该报」与「改了不该报」两类断言在本文件里**成对出现**。

截图用**合成 PNG + 合成 manifest/probes**（结构照 `cdp_shots.mjs` 的产物）：
门禁校验的是"清单与文件是否自洽、探针是否自证"，不是画面好不好看，所以不需要 Chrome。

用法
----
    python test_prototype_lint.py          # 跑全部，失败则非零退出
    python test_prototype_lint.py -v       # 打印每条实际结果
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
# 回归与脚本同目录（草稿期）或脚本在上层（框架 `scripts/tests/` 期）——两种布局都要能跑。
def _find(name):
    for cand in (Path(HERE, name), Path(HERE, os.pardir, name)):
        if os.path.exists(cand):
            return os.path.abspath(cand)
    return Path(HERE, name)


LINT = _find("prototype_lint.py")
RENDER = _find("render_proto.py")
TMP = Path(HERE, "_fixture_tmp_proto")

WIDTHS = (375, 1440)

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


def run(args):
    """跑 lint（固定 UTF-8，避免 GBK 控制台把中文消息吃掉），返回 (rc, stdout, stderr)。"""
    return run_script(LINT, args)


def run_script(script, args):
    proc = subprocess.run([sys.executable, "-X", "utf8", script] + list(args),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def mutate_lint(name, pairs):
    """把 lint 拷一份、按 (旧, 新) 逐处改瞎，返回副本路径。

    第三层判据（对应 `mutate_recall_checker.py` 的做法）：只证明"夹具能让脚本退 1"不够——
    可能是别的规则顺手报的。改瞎该规则后必须**失去命中**，才证明夹具真的打在它身上。
    """
    dst = Path(TMP, "lint_%s.py" % name)
    with open(LINT, encoding="utf-8") as fh:
        src = fh.read()
    for old, new in pairs:
        check(old in src, "mutate %s: 锚点仍在源码里（%s…）" % (name, old.strip().splitlines()[0][:40]))
        src = src.replace(old, new)
    Path(dst).write_text(src, encoding="utf-8")
    return dst


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_png(path, width, height, rgb=(255, 255, 255)):
    """最小 8bit RGB PNG（zero 依赖）。只为让门禁能读到正确的宽高与非空字节。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    Path(path).write_bytes(data)


CLEAN_SPEC = {
    "project": {"name": "fixture", "version": "0.1"},
    "requirements": [{"id": "REQ-1.1", "title": "筛选与列表"},
                     {"id": "REQ-1.2", "title": "任务详情"}],
    "cards": [{"id": "card-001"}, {"id": "card-002"}],
    "screens": [
        {"id": "task-list", "title": "任务列表", "card": "card-001", "req": ["REQ-1.1"],
         "blocks": [{"type": "topbar", "brand": "fixture", "nav": ["任务"]},
                    {"type": "kpi", "items": [{"label": "进行中", "value": "7"}]},
                    {"type": "filters", "items": [{"label": "状态", "value": "进行中", "active": True}]},
                    {"type": "list", "title": "任务", "rows": [
                        {"left": "支付回调重试", "meta": ["周砚", "P0"], "right": "07-24"}]}]},
        {"id": "task-detail", "title": "任务详情", "card": "card-002", "req": ["REQ-1.2"],
         "blocks": [{"type": "topbar", "brand": "fixture"},
                    {"type": "detail", "title": "支付回调重试", "pairs": [["状态", "进行中"]]},
                    {"type": "actions", "actions": [{"label": "认领"}]}]},
    ],
    "flows": [{"id": "to-detail", "from": "task-list", "action": "点行", "to": "task-detail",
               "req": ["REQ-1.2"]}],
}


def make_html(dest, spec=None):
    """用真实的 render_proto.py 渲染（不手写 HTML：手写的会与渲染器脱钩）。"""
    spec_path = Path(dest, "prototype.spec.json")
    html_path = Path(dest, "prototype.html")
    Path(spec_path).write_text(json.dumps(spec if spec is not None else CLEAN_SPEC, ensure_ascii=False, indent=2), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-X", "utf8", RENDER, spec_path, html_path],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return spec_path, html_path, proc


def make_shots(dest, html_path, routes):
    """合成 cdp_shots.mjs 形态的产物：PNG 齐套 + manifest（含 htmlSha256）+ probes（含探针）。"""
    shots = Path(dest, "shots")
    os.makedirs(shots, exist_ok=True)
    manifest = {"pages": {"prototype": {"html": os.path.basename(html_path),
                                        "htmlSha256": sha(html_path),
                                        "widthsList": list(WIDTHS),
                                        "height": 900,
                                        "generatedAt": "2026-10-21T00:00:00",
                                        "routes": {}}}}
    probes = {"prototype": {}}
    for route in routes:
        for w in WIDTHS:
            name = "prototype_%s_%d.png" % (route, w)
            path = Path(shots, name)
            write_png(path, w, 900)
            key = "%s@%d" % (route, w)
            manifest["pages"]["prototype"]["routes"][key] = {
                "file": name, "kind": "viewport", "bytes": os.path.getsize(path),
                "sha256": sha(path), "expectWidth": w, "expectHeight": 900}
            probes["prototype"][key] = {
                "innerWidth": w, "innerHeight": 900, "doc": {"scrollWidth": w, "clientWidth": w,
                                                             "scrollHeight": 900, "clientHeight": 900},
                "overflow": {"horizontal": False, "elements": []}}
    Path(shots, "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(shots, "probes.json").write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
    return shots


def load(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def fresh(name):
    """每个用例一个干净目录：用例之间不能互相污染。"""
    dest = Path(TMP, name)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    return dest


def case_dir(name, shots=False):
    dest = fresh(name)
    spec_path, html_path, proc = make_html(dest)
    check(proc.returncode == 0, "%s: render_proto 能渲染" % name)
    shots_dir = make_shots(dest, html_path, ["flow", "task-list", "task-detail"]) if shots else None
    return dest, spec_path, html_path, shots_dir


def main():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP)
    try:
        # ---------- ① 不乱拦：干净夹具必须 0 error / 0 warn ----------
        dest, spec, html, shots = case_dir("clean", shots=True)
        rc, out, err = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 0, "clean: exit 0（实际 %d）%s" % (rc, err.strip()[:200]))
        check("0 error" in out, "clean: 输出 0 error（%s）" % out.strip().splitlines()[-1:])
        rc2, out2, _ = run(["--spec", spec, "--html", html, "--no-shots", "--json"])
        obj = json.loads(out2)
        check(rc2 == 0 and obj["summary"]["errors"] == 0,
              "clean: --no-shots 也 0 error（summary=%s）" % (obj.get("summary"),))

        # ---------- ② 能拦：spec 层 ----------
        dest, spec, html, _ = case_dir("spec-unknown-block")
        bad = json.loads(json.dumps(CLEAN_SPEC))
        bad["screens"][0]["blocks"].append({"type": "filter"})  # 单数：会被静默跳过
        dump(spec, bad)
        rc, out, _ = run(["--spec", spec, "--html", html, "--no-shots"])
        check(rc == 0 and "spec-unknown-block" in out,
              "spec-unknown-block: 未知块类型报 warn 且不判失败")
        check("filter" in out, "spec-unknown-block: 消息里带出写错的类型名")

        dest, spec, html, _ = case_dir("spec-unreachable")
        bad = json.loads(json.dumps(CLEAN_SPEC))
        bad["screens"].append({"id": "orphan", "title": "孤岛页", "card": "card-002",
                               "req": ["REQ-1.2"], "blocks": [{"type": "topbar"}]})
        dump(spec, bad)
        rc, out, _ = run(["--spec", spec, "--html", html, "--no-shots"])
        check(rc == 1, "spec-unreachable-screen: 不可达屏 -> exit 1（实际 %d）" % rc)
        check("spec-unreachable-screen" in out, "spec-unreachable-screen: 报出孤儿屏")

        dest, spec, html, _ = case_dir("spec-unknown-req")
        bad = json.loads(json.dumps(CLEAN_SPEC))
        bad["screens"][1]["req"] = ["REQ-9.9"]
        dump(spec, bad)
        rc, out, _ = run(["--spec", spec, "--html", html, "--no-shots"])
        check(rc == 1 and "spec-unknown-req" in out, "spec-unknown-req: 引用不存在的需求 -> error")

        # ---------- ③ 能拦：HTML 层 ----------
        dest, spec, html, _ = case_dir("html-external")
        src = open(html, encoding="utf-8").read()
        Path(html).write_text(src.replace("</body>", '<script src="https://cdn.example.com/x.js"></script></body>'), encoding="utf-8")
        rc, out, _ = run(["--spec", spec, "--html", html, "--no-shots"])
        check(rc == 1 and "html-external-ref" in out, "html-external-ref: 外链资源 -> error")

        dest, spec, html, _ = case_dir("html-script-src")
        src = open(html, encoding="utf-8").read()
        Path(html).write_text(src.replace("</body>", '<script src="app.js"></script></body>'), encoding="utf-8")
        rc, out, _ = run(["--spec", spec, "--html", html, "--no-shots"])
        check(rc == 1 and "html-script-src" in out, "html-script-src: 外置脚本（非自包含）-> error")

        dest, spec, html, _ = case_dir("html-missing-screen")
        src = open(html, encoding="utf-8").read()
        Path(html).write_text(src.replace('data-screen="task-detail"', 'data-screen="task-detail-typo"'), encoding="utf-8")
        rc, out, _ = run(["--spec", spec, "--html", html, "--no-shots"])
        check(rc == 1 and "html-missing-screen" in out, "html-missing-screen: 屏锚点丢失 -> error")

        # ---------- ④ 能拦：截图层（covers 的是"看起来都对"的假图）----------
        dest, spec, html, _ = case_dir("shots-no-manifest")
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", Path(dest, "shots")])
        check(rc == 1 and "shots-no-manifest" in out, "shots-no-manifest: 没跑 cdp_shots 的图不认")

        dest, spec, html, shots = case_dir("shots-stale", shots=True)
        with open(html, "a", encoding="utf-8") as fh:
            fh.write("<!-- touched -->\n")
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-stale" in out, "shots-stale: HTML 改了没重出图 -> error")

        dest, spec, html, shots = case_dir("shots-stale-hash", shots=True)
        png = Path(shots, "prototype_task-list_375.png")
        write_png(png, 375, 900, rgb=(18, 52, 86))  # 换像素不改名：内容被改过
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-stale-hash" in out, "shots-stale-hash: 图被改过（sha 不符）-> error")

        dest, spec, html, shots = case_dir("shots-missing", shots=True)
        os.remove(Path(shots, "prototype_flow_1440.png"))
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-file-missing" in out, "shots-file-missing: 清单里有、文件没了 -> error")
        man = load(Path(shots, "manifest.json"))
        del man["pages"]["prototype"]["routes"]["task-detail@1440"]
        dump(Path(shots, "manifest.json"), man)
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-missing" in out, "shots-missing: 档位没出图（清单也没有）-> error")

        dest, spec, html, shots = case_dir("shots-width-mismatch", shots=True)
        write_png(Path(shots, "prototype_flow_375.png"), 320, 900)  # 宽不对
        dump_manifest = Path(shots, "manifest.json")
        man = load(dump_manifest)
        key = "flow@375"
        man["pages"]["prototype"]["routes"][key]["sha256"] = sha(Path(shots, "prototype_flow_375.png"))
        man["pages"]["prototype"]["routes"][key]["bytes"] = os.path.getsize(Path(shots, "prototype_flow_375.png"))
        dump(dump_manifest, man)
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-width-mismatch" in out, "shots-width-mismatch: 320 图冒充 375 -> error")

        dest, spec, html, shots = case_dir("shots-too-small", shots=True)
        p = Path(shots, "prototype_flow_1440.png")
        write_png(p, 1440, 120)
        man = load(Path(shots, "manifest.json"))
        man["pages"]["prototype"]["routes"]["flow@1440"]["sha256"] = sha(p)
        man["pages"]["prototype"]["routes"]["flow@1440"]["bytes"] = os.path.getsize(p)
        dump(Path(shots, "manifest.json"), man)
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-too-small" in out, "shots-too-small: 高度 < 200 的图不认")

        dest, spec, html, shots = case_dir("shots-probe-width", shots=True)
        pr = load(Path(shots, "probes.json"))
        pr["prototype"]["flow@375"]["innerWidth"] = 320  # 320 档按 500 布局的经典假档位
        dump(Path(shots, "probes.json"), pr)
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-probe-width-mismatch" in out,
              "shots-probe-width-mismatch: 探针自证宽度不符 -> error")

        dest, spec, html, shots = case_dir("shots-probe-missing", shots=True)
        pr = load(Path(shots, "probes.json"))
        del pr["prototype"]["task-list@1440"]
        dump(Path(shots, "probes.json"), pr)
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-probe-missing" in out, "shots-probe-missing: 缺探针的档位不认")

        dest, spec, html, shots = case_dir("shots-overflow", shots=True)
        pr = load(Path(shots, "probes.json"))
        pr["prototype"]["task-detail@375"]["doc"]["scrollWidth"] = 415
        pr["prototype"]["task-detail@375"]["overflow"] = {
            "horizontal": True,
            "elements": [{"tag": "div", "cls": "row", "left": 0, "right": 415, "width": 415}]}
        dump(Path(shots, "probes.json"), pr)
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-overflow" in out, "shots-overflow: 横向溢出 -> error")
        check("415" in out, "shots-overflow: 消息带出真实 scrollWidth")

        # ---------- ⑤ 边界：读不到 spec 时不许假绿 ----------
        rc, out, _ = run(["--spec", Path(TMP, "nope.json"), "--html", html, "--no-shots"])
        check(rc == 2, "坏 spec: exit 2（不是 0，也不是 1）")

        # ---------- ⑥ 检查器自变异：改瞎规则后必须失去命中 ----------
        dest, spec, html, shots = case_dir("mut-stale", shots=True)
        with open(html, "a", encoding="utf-8") as fh:
            fh.write("<!-- touched -->\n")
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-stale" in out, "mut-stale: 变异前命中 shots-stale")
        blind = mutate_lint("stale", [('if page.get("htmlSha256") != html_sha:',
                                       'if False:')])
        rc, out, _ = run_script(blind, ["--spec", spec, "--html", html, "--shots", shots])
        check("shots-stale" not in out, "mut-stale: 改瞎 htmlSha256 比对后不再命中（证明夹具打在它身上）")

        dest, spec, html, shots = case_dir("mut-overflow", shots=True)
        pr = load(Path(shots, "probes.json"))
        pr["prototype"]["flow@375"]["doc"]["scrollWidth"] = 500
        pr["prototype"]["flow@375"]["overflow"] = {"horizontal": True, "elements": []}
        dump(Path(shots, "probes.json"), pr)
        rc, out, _ = run(["--spec", spec, "--html", html, "--shots", shots])
        check(rc == 1 and "shots-overflow" in out, "mut-overflow: 变异前命中 shots-overflow")
        blind = mutate_lint("overflow", [('if overflow.get("horizontal"):', 'if False:')])
        rc, out, _ = run_script(blind, ["--spec", spec, "--html", html, "--shots", shots])
        check("shots-overflow" not in out, "mut-overflow: 改瞎水平溢出判断后不再命中")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("")
    print("test_prototype_lint: %d passed / %d failed" % (len(PASSES), len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  FAIL %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
