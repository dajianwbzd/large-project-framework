#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_slop_gate.py — 反 AI 默认风格门禁（slop_gate.py）的回归 + 变异对拍（纯 stdlib）

为什么有这个脚本
----------------
`UI设计师/scripts/slop_gate.py` 的设计是「**规则集合从 `slop-gates.md` 现读**」：
表里有规则、代码没实现 → 退出码 2（漂移），而不是默默通过。所以这个脚本要盯三类事：

  ① **能拦**：每个 Floor 规则一个故意违规的夹具，且**必须**是退出码 1；
  ② **不乱拦**：干净夹具退出码 0（误报比漏报更伤，设计稿会被无谓打回）；
  ③ **三态不能混**：规则表与实现漂移 → 退出码 **2**，绝不能被当成 0 或 1；
  ④ **豁免边界**：普通规则行内豁免有效（进 report 不进 exit），**Floor 规则豁免必须被拒**。

夹具全部是合成 HTML（不依赖浏览器；脚本本来就是静态文本判据）。

用法
----
    python test_slop_gate.py          # 全跑，失败非零退出
    python test_slop_gate.py -v       # 打印每条实际结果
"""

import os
import shutil
import sys
from pathlib import Path

HERE = Path(os.path.abspath(__file__)).parent
FRAMEWORK = Path(HERE, os.pardir, os.pardir)


def _find_script(*parts):
    for base in (FRAMEWORK, Path(FRAMEWORK, os.pardir), HERE):
        cand = Path(base, *parts)
        if os.path.exists(cand):
            return cand
    return Path(FRAMEWORK, *parts)


SCRIPTS = _find_script("skills", "UI设计师", "scripts")
sys.path.insert(0, str(SCRIPTS))

import slop_gate  # noqa: E402

TMP = Path(HERE, "_fixture_tmp_slop")

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


def write(name, text):
    p = Path(TMP, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------- 夹具
BAD_HTML = """<!doctype html>
<html><head><style>
.hero { background: linear-gradient(135deg, #7C5CFF, #4C1D95); border-radius: 24px;
        box-shadow: 0 24px 48px rgba(0,0,0,.35); padding: 120px; }
.card { border-radius: 28px; box-shadow: 0 20px 40px rgba(0,0,0,.3); padding: 112px; }
.panel { border-radius: 32px; box-shadow: 0 32px 64px rgba(0,0,0,.4); padding: 104px; }
.badge { border-radius: 16px; }
</style></head>
<body><section class="hero"><h1>你的标题</h1><p>Lorem ipsum</p></section>
<div class="card">示例文本</div><div class="card">c</div></body></html>
"""

GOOD_HTML = """<!doctype html>
<html><head><style>
body { background: #F7F8FA; color: #1A1C1E; }
.topbar { padding: 12px 16px; background: #1A1C1E; border-radius: 0; }
.row { padding: 16px; border-radius: 4px; box-shadow: 0 1px 2px rgba(0,0,0,.12); }
</style></head>
<body><header class="topbar"><h1>运维值守台</h1></header>
<div class="row">近 24 小时告警 3 条</div></body></html>
"""

EXEMPT_HTML = """<!doctype html>
<html><head><style>
<!-- slop-gate: off [SG-04] 理由：这是间距标尺页，超大内边距是本页要展示的内容本身 -->
.a { padding: 120px; } .b { padding: 104px; } .c { padding: 96px; } .d { padding: 112px; }
<!-- slop-gate: on -->
.p { padding: 16px; }
</style></head><body><div class="p">近 24 小时告警 3 条</div></body></html>
"""

FLOOR_EXEMPT_HTML = """<!doctype html>
<html><body>
<!-- slop-gate: off [SG-06] 理由：这页就是要展示占位文案的对比 -->
<div>Lorem ipsum</div><div>示例文本</div>
<!-- slop-gate: on -->
<div>真实内容</div>
</body></html>
"""

RULES_DRIFT = """| 编号 | 规则 | 判据 | 档 | Floor |
|---|---|---|---|---|
| `SG-01` | 紫蓝渐变 | x | 可判 | ✅ |
| `SG-99` | 本脚本没实现的规则 | x | 可判 | — |
"""


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    # ① 默认规则表：8 条 + Floor 4 条（表是唯一来源，改表就该失败）
    rules = slop_gate.read_rules()
    ids = sorted(r["id"] for r in rules)
    check(len(ids) == 8, "规则表 8 条（实际 %d: %s）" % (len(ids), ",".join(ids)))
    floor = sorted(r["id"] for r in rules if r["floor"])
    check(floor == ["SG-01", "SG-02", "SG-05", "SG-06"], "Floor 恰为 SG-01/02/05/06（实际 %s）" % floor)
    tier = {r["id"]: r["tier"] for r in rules}
    check(tier.get("SG-01") == "可判", "SG-01 档 = 可判（实际 %r）" % tier.get("SG-01"))
    check(tier.get("SG-03") == "半可判", "SG-03 档 = 半可判（实际 %r）" % tier.get("SG-03"))
    check(all(not r["floor"] for r in rules if r["tier"] != "可判"),
          "半可判/人判的规则不得标 Floor")

    # ② 能拦：bad.html 必须退出码 1，且命中 SG-02 与 SG-06
    bad = write("bad.html", BAD_HTML)
    r = slop_gate.run([bad])
    hit = {v["rule"] for v in r["violations"]}
    check(r["status"] == "fail", "bad.html status=fail（实际 %r）" % r["status"])
    check(r["exit"] == 1, "bad.html exit=1（实际 %r）" % r["exit"])
    check("SG-02" in hit, "bad.html 命中 SG-02 圆角（实际 %s）" % sorted(hit))
    check("SG-06" in hit, "bad.html 命中 SG-06 占位文案（实际 %s）" % sorted(hit))

    # ③ 不乱拦：good.html 零违例、退出码 0
    good = write("good.html", GOOD_HTML)
    g = slop_gate.run([good])
    check(g["violations"] == [], "good.html 零违例（实际 %s）" % [v["rule"] for v in g["violations"]])
    check(g["exit"] == 0, "good.html exit=0（实际 %r）" % g["exit"])

    # ④ 豁免：非 Floor 规则被豁免 → 不计数、留痕、退出码 0
    ex = write("exempt.html", EXEMPT_HTML)
    e = slop_gate.run([ex])
    ex_rules = {v["rule"] for v in e["violations"]}
    check("SG-04" not in ex_rules, "exempt.html SG-04 已被豁免（实际 %s）" % sorted(ex_rules))
    check(e["exemptions"] != [], "exempt.html 豁免留痕非空")
    check(e["exit"] == 0, "豁免后 exit=0（实际 %r）" % e["exit"])

    # ⑤ Floor 豁免必须被拒：SG-06 写了 off 也不生效，仍退出码 1（违例照算 + 留痕被拒）
    fx = write("floor_exempt.html", FLOOR_EXEMPT_HTML)
    f = slop_gate.run([fx])
    f_rules = {v["rule"] for v in f["violations"]}
    check("SG-06" in f_rules, "Floor 规则 SG-06 拒绝豁免（实际 %s）" % sorted(f_rules))
    check(f["rejected_exemptions"] != [], "Floor 豁免被拒留痕非空")
    check(f["exit"] == 1, "Floor 豁免被拒后 exit=1（实际 %r）" % f["exit"])

    # ⑥ 漂移：规则表里有 SG-99 但脚本没实现 → 退出码 2，且不得判通过
    drift = write("filtrate-gates.md", RULES_DRIFT)
    d = slop_gate.run([good], rules_path=str(drift))
    check(d["status"] == "drift", "漂移 status=drift（实际 %r）" % d["status"])
    check(d["exit"] == 2, "漂移 exit=2（实际 %r）" % d["exit"])
    check("SG-99" in d["missing_impl"], "missing_impl 含 SG-99（实际 %r）" % (d["missing_impl"],))

    # ⑦ 非 HTML 跳过文本规则：md 文件里的 lorem 不算占位文案（规则文档不自咬）
    md = write("note.md", "示例文本 Lorem ipsum 待补充\n")
    m = slop_gate.run([md])
    check(m["violations"] == [], "md 跳过 SG-06（实际 %s）" % [v["rule"] for v in m["violations"]])
    check(m["exit"] == 0, "md exit=0（实际 %r）" % m["exit"])

    # ⑧ 目标不存在 → UsageError（不是静默 0 条）
    try:
        slop_gate.run([Path(TMP, "no-such-file.html")])
        check(False, "不存在的目标应抛 UsageError")
    except slop_gate.UsageError:
        check(True, "不存在的目标抛 UsageError")

    # ⑨ render 有人读结论行
    text = slop_gate.render(r)
    check("结论：" in text and "VIOLATION" in text, "render 含结论行")
    check("规则源" in text, "render 标明规则源路径")

    if TMP.exists():
        shutil.rmtree(TMP)

    print("\n通过 %d / 失败 %d" % (len(PASSES), len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  FAIL %s" % f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
