#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_browser_resource_audit.py — `browser_resource_audit.py` 的回归与变异测试（v5.10.0）

体例照 `test_test_scope_audit.py` / `test_case_audit.py`：
  1 干净夹具不误报   2 单点变异逐条命中   3 成对反向断言（**不许报**）
  4 注释与围栏里的伪命中不得计入        5 大小写与空白容错
  6 格式旧 ≠ 内容缺（**且不逐行报错**）  7 三态退出码（0/1/2，2 专表「无法检查」）
  8 顺序与编码                          9 检查器自身变异（反向控制证明「有牙」）
末尾**全绿才清场**（失败留着复现）。

为什么这一层不能省：本检查器的核心判据（`BRW-NO-RETURN` 取用未归还、
`BRW-OVERLAP` 独占时段重叠）在真实工程里**只有出事那天才会命中**——
不自己造夹具，发布时它们就是**未验证代码**。

运行：python -X utf8 scripts/tests/test_browser_resource_audit.py [-v]
"""

import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = next((p for p in (os.path.join(HERE, "browser_resource_audit.py"),
                          os.path.join(HERE, os.pardir, "browser_resource_audit.py"))
              if os.path.exists(p)), os.path.join(HERE, os.pardir, "browser_resource_audit.py"))
TMP = os.path.join(HERE, "_fixture_brw_tmp")
MUT = os.path.join(TMP, "_mutated")

CANON_HEADER = "id | card | session | tabs | take | return | evidence"
OLD_HEADER = "id | card | session | tabs | take"

VERBOSE = "-v" in sys.argv
PASS = 0
FAILS = []
CODE_RE = re.compile(r"BRW-[A-Z-]+")


def check(cond, label):
    global PASS
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL %s" % label)


def codes(text):
    return set(CODE_RE.findall(text))


# ------------------------------------------------------------------ 夹具
def card_dir(name):
    """需求卡在 `需求卡/`，测试卡在 `测试卡/`（证据路径的目录前缀跟着卡的种类走）。"""
    return "测试卡" if name.startswith("tc-") else "需求卡"


def row(bid, card, session="xyza", tabs="3", take="2026-09-30 10:00",
        ret="2026-09-30 10:07", ev=None):
    """机读块的一行（列序与表头一致）。`ev=None` ⇒ 默认证据路径（夹具会把它落成真文件）。"""
    if ev is None:
        ev = "%s/%s/证据/前端/会话.md" % (card_dir(card), card)
    return " | ".join([bid, card, session, tabs, take, ret, ev])


def card_body(name, browser=True, junk=None):
    lines = ["# %s" % name, "", "## 需求描述", "", "- 目标：把这一件事做完。"]
    if browser:
        lines += ["- 取证：用 BrowserSkill 打开页面截图留证（`bsk upload` 上传附件）。"]
    if junk:
        lines += junk
    return "\n".join(lines) + "\n"


def fm_text(name, kind, tab_cap=3, drop=(), extra=()):
    pairs = [("id", name), ("title", "t-%s" % name), ("status", "todo")]
    if tab_cap is not None:
        pairs.append(("browser_tab_cap", str(tab_cap)))
    pairs = [(k, v) for k, v in pairs if k not in set(drop)]
    pairs += list(extra)
    body = ["---"]
    body += ["%s: %s" % (k, v) for k, v in pairs]
    body += ["created: 2026-09-30", "---", ""]
    return "\n".join(body)


def make_fixture(name, cards=None, none=False, raw=None, encoding="utf-8",
                 evidence=("需求卡/card-001/证据/前端/会话.md",)):
    """cards: [{"name":…, "kind":"card"|"tc", "rows":[…], "header":…, "body":…,
                "tab_cap":…, "drop":(), "nofile":bool}]"""
    root = os.path.join(TMP, name)
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    if none:
        os.makedirs(root)
        return root
    base = os.path.join(root, "project-state")
    for d in ("需求卡", "测试卡"):
        os.makedirs(os.path.join(base, d))
    if raw is not None:
        d = os.path.join(base, "需求卡", "card-001")
        os.makedirs(d)
        with open(os.path.join(d, "card.md"), "wb") as fh:
            fh.write(raw)
        return root
    for c in (cards or []):
        folder = "需求卡" if c.get("kind", "card") == "card" else "测试卡"
        fname = "card.md" if c.get("kind", "card") == "card" else "tc.md"
        d = os.path.join(base, folder, c["name"])
        os.makedirs(d, exist_ok=True)
        if c.get("nofile"):
            continue
        text = fm_text(c["name"], c.get("kind", "card"), tab_cap=c.get("tab_cap", 3),
                       drop=c.get("drop", ()), extra=c.get("extra", ()))
        if c.get("rows") is not None:
            text += "<!-- machine:browser -->\n%s\n" % c.get("header", CANON_HEADER)
            text += "\n".join(c["rows"]) + "\n<!-- /machine:browser -->\n"
        text += "\n" + (c.get("body") if c.get("body") is not None
                        else card_body(c["name"], c.get("browser", True), c.get("junk")))
        with open(os.path.join(d, fname), "wb") as fh:
            fh.write(text.encode(encoding))
    for e in evidence or ():
        p = os.path.join(base, e)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("会话记录：start / tabs / return\n")
    # 默认证据路径（`<卡目录>/<卡名>/证据/前端/会话.md`）落成真文件：
    # 故意写出的悬空路径（如 `证据/不存在-会话.md`）不在此列，仍要被抓。
    for c in (cards or []):
        dflt = "%s/%s/证据/前端/会话.md" % (card_dir(c["name"]), c["name"])
        p = os.path.join(base, dflt)
        if os.path.exists(p):
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("会话记录：start / tabs / return\n")
    return root


def run(root, *args, **kw):
    script = kw.get("script") or AUDIT
    cmd = [sys.executable, "-X", "utf8", script, root] + list(args)
    env = dict(os.environ)
    env.update(kw.get("env") or {})
    p = subprocess.run(cmd, cwd=os.path.dirname(script), capture_output=True, env=env)
    text = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    return p.returncode, text


def run_json(root, *args, **kw):
    code, text = run(root, "--json", *args, **kw)
    try:
        return code, json.loads(text[text.index("{"):text.rindex("}") + 1]), text
    except Exception:
        return code, None, text


def expect(root, want_exit, want_code=None, absent=(), label="", *args):
    code, text = run(root, *args)
    tag = label or (want_code or "clean")
    check(code == want_exit, "%s: 退出码 = %d（实得 %d）" % (tag, want_exit, code))
    cs = codes(text)
    if want_code:
        check(want_code in cs, "%s: 命中 %s（实得 %s）" % (tag, want_code, sorted(cs) or "无"))
    for a in absent:
        check(a not in cs, "%s: **不许**报 %s（实得 %s）" % (tag, a, sorted(cs) or "无"))
    return code, text


def clean_cards():
    """干净夹具：两张需求卡 + 一张测试卡，全部成对归还、证据落地。"""
    return [
        {"name": "card-001", "rows": [
            row("BRW-001", "card-001", "xyza", "3", "2026-09-30 10:00", "2026-09-30 10:07"),
        ]},
        {"name": "card-002", "rows": [
            row("BRW-002", "card-002", "xyzb", "2", "2026-09-30 10:10", "2026-09-30 10:14"),
        ]},
        {"name": "tc-001", "kind": "tc", "rows": [
            row("BRW-003", "tc-001", "xyzc", "1", "2026-09-30T10:20", "2026-09-30T10:22"),
        ]},
    ]


_ONE_SEQ = [0]


def one_card(rows, name="card-001", **over):
    """单卡夹具：把干净夹具压成一张卡，只放要测的行。"""
    _ONE_SEQ[0] += 1
    c = {"name": name, "rows": rows}
    c.update(over)
    return make_fixture("one_%03d" % _ONE_SEQ[0], [c])


def main():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)

    print("== test_browser_resource_audit.py | 被测：%s ==" % AUDIT)
    check(os.path.isfile(AUDIT), "被测脚本存在：%s" % AUDIT)

    # ------------------------------------------------ 1 干净夹具不误报
    print("\n[1] 干净夹具")
    clean = make_fixture("clean", clean_cards())
    code, text = expect(clean, 0, label="干净三卡")
    check(codes(text) == set(), "干净三卡: 无任何 code（实得 %s）" % (sorted(codes(text)) or "无"))
    check("扫描 3 张卡" in text, "干净三卡: 汇总行印出扫描卡数")
    check("browser_resource_audit：ERROR 0 / WARN 0" in text, "干净三卡: 汇总行格式正确")
    check("取用记录：3 行" in text, "干净三卡: 印出取用记录行数")
    check("只核「声明与配对」" in text, "干净三卡: 印出范围声明")
    check("核不了「会话是否真的被关掉」" in text, "干净三卡: 明写核不了会话是否真关")
    check("核不了「tab 是否真的关干净」" in text, "干净三卡: 明写核不了 tab 是否真关干净")
    check("只证明「声明与配对没错」" in text, "干净三卡: 明写结论边界")
    code, js, _t = run_json(clean)
    check(code == 0 and js and js.get("ok") is True and js.get("exit") == 0,
          "--json: ok=True / exit=0")
    check(js and js.get("cards") == 3, "--json: cards=3（实得 %s）" % (js or {}).get("cards"))
    check(js and js.get("counts") == {"ERROR": 0, "WARN": 0, "INFO": 0},
          "--json: counts 全 0（实得 %s）" % (js or {}).get("counts"))
    check(js and js.get("tab_cap") == 3, "--json: 印出 tab_cap=3")
    check(js and len(js.get("rows") or []) == 3, "--json: rows 3 条")
    check(js and (js.get("rows") or [{}])[0].get("returned") is True, "--json: rows 带 returned 布尔")
    code, text = run(clean, "--check")
    check(code == 0 and "ERROR 0" in text and "取用记录 3 行" in text,
          "--check: 单行计数且退出码 0")
    check("[E]" not in text and "[W]" not in text, "--check: 不印明细")
    # 边 `|` 写法同样干净
    piped = make_fixture("clean_piped", [{"name": "card-001", "rows": [
        "| " + row("BRW-001", "card-001") + " |",
    ]}])
    code, text = expect(piped, 0, label="带边 `|` 写法")
    code, js, _t = run_json(piped)
    check(js and js.get("cards") == 1 and js.get("counts", {}).get("ERROR") == 0,
          "带边 `|` 写法: 一格不少、0 ERROR")

    # ---------------------------------------- 2 单点变异逐条命中
    print("\n[2] 单点变异逐条命中（该报的必须报）")
    expect(one_card([row("", "card-001")]), 1, "BRW-EMPTY-ID", label="id 为空")
    expect(one_card([row("BRW-1", "card-001")]), 1, "BRW-EMPTY-ID", label="id 位数不足")
    expect(one_card([row("BRW-ABC", "card-001")]), 1, "BRW-EMPTY-ID", label="id 非数字")
    expect(one_card([row("BRW-001", "card-001"), row("BRW-001", "card-001")]), 1,
           "BRW-ID-DUP", label="id 重复")
    expect(one_card([row("BRW-002", "card-001"), row("BRW-001", "card-001")]), 1,
           "BRW-ID-ORDER", label="id 乱序")
    expect(one_card([row("BRW-001", "card-001", ret="")]), 1, "BRW-NO-RETURN", label="return 空")
    expect(one_card([row("BRW-001", "card-001", ret="-")]), 1, "BRW-NO-RETURN", label="return 占位 -")
    expect(one_card([row("BRW-001", "card-001", ret="无")]), 1, "BRW-NO-RETURN", label="return 写「无」")
    expect(one_card([row("BRW-001", "card-001", ev="")]), 1, "BRW-NO-EVIDENCE", label="已归还无证据")
    expect(one_card([row("BRW-001", "card-001", ev="-")]), 1, "BRW-NO-EVIDENCE", label="证据占位 -")
    expect(one_card([row("BRW-001", "card-001", ev="证据/不存在-会话.md")]), 1,
           "BRW-EVIDENCE-DANGLING", label="证据路径悬空")
    expect(one_card([row("BRW-001", "card-001", ev="见 证据/不存在-会话.md")]), 1,
           "BRW-EVIDENCE-DANGLING", label="引导词「见」后仍核路径")
    expect(one_card([row("BRW-001", "card-001", tabs="4")]), 1, "BRW-TABS-OVER", label="tabs 超上限")
    expect(one_card([row("BRW-001", "card-001", tabs="15")]), 1, "BRW-TABS-OVER",
           label="tabs 15（事故形状）")
    expect(one_card([row("BRW-001", "card-001", tabs="0")]), 1, "BRW-TABS-OVER", label="tabs=0")
    expect(one_card([row("BRW-001", "card-001", tabs="三")]), 1, "BRW-TABS-OVER", label="tabs 非数字")
    expect(one_card([row("BRW-001", "card-001", tabs="")]), 1, "BRW-TABS-OVER", label="tabs 为空")
    expect(one_card([row("BRW-001", "card-001", session="")]), 1, "BRW-SESSION-EMPTY",
           label="session 为空")
    # `-` 是占位不是空白：规则写的是 session **为空**才报；把 `-` 也算空会把
    # 「填了个没用的值」和「这一格不在」混成一件事（先例 v5.7.0）
    expect(one_card([row("BRW-001", "card-001", session="-")]), 0, absent=("BRW-SESSION-EMPTY",),
           label="session 写 -（占位 ≠ 空白，不报空）")

    # ---- 规则 7：声明了浏览器取证却没有机读块
    expect(make_fixture("m_noblock", [{"name": "card-001", "rows": None,
                                       "body": card_body("card-001", True)}]),
           1, "BRW-MISSING-BLOCK", label="有浏览器字样无机读块")
    expect(make_fixture("m_noblock_brand", [{"name": "card-001", "rows": None,
                                             "body": "- 用 BrowserSkill 开页面。\n- 上传走 bsk upload。\n"}]),
           1, "BRW-MISSING-BLOCK", label="BrowserSkill/bsk 字样同样命中")

    # ---- 规则 6：独占时段重叠
    def ov(bid="BRW-001", name="card-001", tag="ov"):
        return make_fixture(tag, [{"name": name, "rows": [
            row(bid, name, "xyza", "3", "2026-09-30 10:00", "2026-09-30 10:10"),
            row("BRW-%03d" % (int(bid.split("-")[1]) + 1), name, "xyzb", "3",
                "2026-09-30 10:05", "2026-09-30 10:15"),
        ]}])
    expect(ov(tag="ov_main"), 1, "BRW-OVERLAP", label="独占时段重叠")
    cross = make_fixture("m_overlap_cross", [
        {"name": "card-001", "rows": [row("BRW-001", "card-001", "xyza", "3",
                                          "2026-09-30 10:00", "2026-09-30 10:10")]},
        {"name": "tc-001", "kind": "tc", "rows": [row("BRW-002", "tc-001", "xyzb", "3",
                                                      "2026-09-30 10:08", "2026-09-30 10:20")]},
    ])
    expect(cross, 1, "BRW-OVERLAP", absent=("BRW-EVIDENCE-DANGLING", "BRW-NO-RETURN"),
           label="跨卡重叠（需求卡↔测试卡）")
    # 三行：1↔2 重叠、2↔3 相交——至少要报出重叠（扫过最大 return 的写法）
    chained = make_fixture("m_overlap_chain", [{"name": "card-001", "rows": [
        row("BRW-001", "card-001", "s1", "3", "2026-09-30 10:00", "2026-09-30 10:30"),
        row("BRW-002", "card-001", "s2", "3", "2026-09-30 10:05", "2026-09-30 10:40"),
        row("BRW-003", "card-001", "s3", "3", "2026-09-30 10:35", "2026-09-30 10:50"),
    ]}])
    expect(chained, 1, "BRW-OVERLAP", label="三行递进重叠")

    # ---- 时间解析不了只报 WARN
    expect(one_card([row("BRW-001", "card-001", take="昨天上午")]), 0, "BRW-TIME-BAD",
           absent=("BRW-OVERLAP",), label="时间解析不了只 WARN")
    expect(one_card([row("BRW-001", "card-001", take="2026-09-30 10:30",
                         ret="2026-09-30 10:10")]), 0, "BRW-TIME-BAD",
           absent=("BRW-OVERLAP",), label="return 早于 take 只 WARN")

    # ---------------------------------------- 3 成对反向断言（不许报）
    print("\n[3] 成对反向断言：改回正确值就变绿")
    good = make_fixture("n_returned", [{"name": "card-001", "rows": [row("BRW-001", "card-001")]}])
    code, text = expect(good, 0, absent=("BRW-NO-RETURN", "BRW-NO-EVIDENCE"),
                        label="成对归还+证据")
    bad = one_card([row("BRW-001", "card-001", ret="", ev="")])
    expect(bad, 1, "BRW-NO-RETURN", label="反向配对：未归还必红")
    code, _t = run(good)
    check(code == 0, "反向配对：改回归还即变绿（不是恒红）")
    # take 恰好等于上一行 return：**不算重叠**（边界不算重叠）
    edge = make_fixture("n_edge_touch", [{"name": "card-001", "rows": [
        row("BRW-001", "card-001", "xyza", "3", "2026-09-30 10:00", "2026-09-30 10:10"),
        row("BRW-002", "card-001", "xyzb", "3", "2026-09-30 10:10", "2026-09-30 10:20"),
    ]}])
    expect(edge, 0, absent=("BRW-OVERLAP",), label="首尾相接（take == 上一行 return）")
    # tabs 恰好等于上限 3：放行；4 才报
    at_cap = make_fixture("n_cap3", [{"name": "card-001", "rows": [row("BRW-001", "card-001", tabs="3")]}])
    expect(at_cap, 0, absent=("BRW-TABS-OVER",), label="tabs == 上限 3 放行")
    # 纯自述证据：不算悬空（机器核不了自述真假）
    expect(one_card([row("BRW-001", "card-001", ev="自述")]), 0,
           absent=("BRW-EVIDENCE-DANGLING", "BRW-NO-EVIDENCE"), label="证据写「自述」放行")
    # 证据带后缀说明文字：路径部分仍核、说明不算路径
    suffix = make_fixture("n_ev_suffix", [{"name": "card-001", "rows": [
        row("BRW-001", "card-001", ev="需求卡/card-001/证据/前端/会话.md（含 start/return 时序）"),
    ]}])
    expect(suffix, 0, absent=("BRW-EVIDENCE-DANGLING",), label="证据带说明文字仍认得路径")
    # 时长很短的会话：不报任何东西（不判「开太短/太久」）
    expect(one_card([row("BRW-001", "card-001", take="2026-09-30 10:00", ret="2026-09-30 10:00")]),
           0, absent=("BRW-OVERLAP",), label="零时长会话不报重叠")
    # 未归还的行不参与重叠排序：不该因为「没归还」而顺带报重叠
    code, text = run(one_card([row("BRW-001", "card-001", "xyza", "3",
                                   "2026-09-30 10:00", "2026-09-30 10:10"),
                               row("BRW-002", "card-001", "xyzb", "3",
                                   "2026-09-30 10:05", "")]))
    check("BRW-NO-RETURN" in codes(text), "未归还行: 报 NO-RETURN")
    check("BRW-OVERLAP" not in codes(text), "未归还行: **不许**参与重叠排序（不重复定罪）")

    # -------------------------- 4 注释与围栏里的伪命中不得计入
    print("\n[4] 注释与围栏干扰")
    fenced = make_fixture("n_fence", [{"name": "card-001", "rows": [
        row("BRW-001", "card-001")], "body": card_body("card-001", False, junk=[
        "- 本卡不碰浏览器：见模板示例",
        "```markdown",
        "- 示例：用 BrowserSkill 打开页面",
        "```",
        "",
        "<!-- 历史记录：曾用 浏览器 取证，已撤销 -->",
    ])}])
    code, text = expect(fenced, 0, absent=("BRW-MISSING-BLOCK", "BRW-TAB-CAP-UNDECLARED"),
                        label="围栏与注释里的浏览器字样")
    check("BRW-MISSING-BLOCK" not in codes(text),
          "围栏内伪命中: **不许**当成「声明了浏览器取证」")
    check(codes(text) == set(), "围栏内伪命中: 整张卡干净（实得 %s）" % (sorted(codes(text)) or "无"))
    # 正文**真的**命中（不在围栏/注释里）却没有机读块：必须报
    outside = make_fixture("m_fence_outside", [{"name": "card-001", "rows": None,
                                                "body": card_body("card-001", False, junk=[
                                                    "- 示例见围栏",
                                                    "```markdown",
                                                    "- 示例：用 BrowserSkill 打开页面",
                                                    "```",
                                                    "- 本卡确实要用 浏览器 取证。",
                                                ])}])
    expect(outside, 1, "BRW-MISSING-BLOCK", label="围栏外真命中（对照组）")

    # ------------------------------ 5 大小写与空白容错
    print("\n[5] 大小写与空白容错")
    upper = make_fixture("n_upper", [{"name": "card-001", "rows": None,
                                      "body": "- 取证走 BROWSERSKILL 与 BSK UPLOAD。\n"}])
    code, text = expect(upper, 1, "BRW-MISSING-BLOCK", label="大写浏览器字样")
    code, text = expect(make_fixture("n_upper_ok", [{"name": "card-001", "rows": [
        row("BRW-001", "card-001")], "body": "- 取证走 BROWSERSKILL。\n"}]), 0,
        absent=("BRW-TAB-CAP-UNDECLARED", "BRW-MISSING-BLOCK"), label="大写字样 + 有块 + 有预算声明")
    ws = make_fixture("n_ws", [{"name": "card-001", "rows": [
        "BRW-001|card-001|xyza|3|2026-09-30 10:00|2026-09-30 10:07|需求卡/card-001/证据/前端/会话.md",
        "  BRW-002 |  card-001 | xyzb | 2 | 2026-09-30 10:10 | 2026-09-30 10:12 |  需求卡/card-001/证据/前端/会话.md  ",
    ]}])
    code, text = expect(ws, 0, absent=("BRW-EMPTY-ID", "BRW-EVIDENCE-DANGLING", "BRW-TABS-OVER"),
                        label="紧凑与多余空白")
    # 表头前后空白也认
    hdr_ws = make_fixture("n_hdr_ws", [{"name": "card-001", "header": "  " + CANON_HEADER + "  ",
                                        "rows": [row("BRW-001", "card-001")]}])
    expect(hdr_ws, 0, absent=("BRW-FORMAT-STALE",), label="表头前后空白")

    # ------------------------------ 6 格式旧 ≠ 内容缺
    print("\n[6] 格式旧 ≠ 内容缺")
    stale = make_fixture("m_stale", [{"name": "card-001", "header": OLD_HEADER, "rows": [
        "BRW-001 | card-001 | xyza | 3 | 2026-09-30 10:00",
        "BRW-002 | card-001 | xyzb | 9 | 2026-09-30 10:10",
    ]}])
    code, text = expect(stale, 0, "BRW-FORMAT-STALE",
                        absent=("BRW-NO-RETURN", "BRW-NO-EVIDENCE", "BRW-TABS-OVER"),
                        label="旧列头 5 列")
    check(text.count("BRW-FORMAT-STALE") == 1,
          "旧列头: 整块**只报一次**（实得 %d）" % text.count("BRW-FORMAT-STALE"))
    check("格式旧 ≠ 内容缺" in text, "旧列头: 提示语点明迁移纪律")
    check("跳过该块的行级检查" in text or "跳过行级检查" in text, "旧列头: 明写跳过行级检查")
    check("BRW-TABS-OVER" not in codes(text),
          "旧列头: **不许**逐行报「tabs 超限」（那行 tabs=9 在旧块里）")
    nc = make_fixture("m_stale_nc", [{"name": "card-001", "rows": None,
                                      "body": "<!-- machine:browser -->\nid | card\n<!-- /machine:browser -->\n"
                                              + card_body("card-001", True)}])
    expect(nc, 0, "BRW-FORMAT-STALE", absent=("BRW-NO-RETURN",), label="无表头行只报一次 WARN")
    unclosed = make_fixture("m_unclosed", [{"name": "card-001", "rows": None,
                                            "body": "<!-- machine:browser -->\n%s\n%s\n"
                                                    % (CANON_HEADER, row("BRW-001", "card-001"))
                                                    + card_body("card-001", True)}])
    expect(unclosed, 0, "BRW-FORMAT-STALE", absent=("BRW-NO-RETURN",),
           label="块未闭合（不吞后面正文）")

    # ------------------------------ 7 三态退出码
    print("\n[7] 三态退出码：0 / 1 / 2（2 = 无法检查）")
    nc_root = make_fixture("nocards", none=True)
    code, text = run(nc_root)
    check(code == 2, "无任何卡: 退出码 2（实得 %d）" % code)
    check("BRW-NO-CARD" in text, "无任何卡: 命中 BRW-NO-CARD")
    check("无法检查，不是「没问题」" in text, "无任何卡: 明写「无法检查 ≠ 没问题」")
    check("扫描 0 张卡" in text, "无任何卡: 汇总印出扫描 0 张卡")
    code, js, _t = run_json(nc_root)
    check(code == 2 and js and js.get("ok") is None and js.get("exit") == 2 and js.get("cards") == 0,
          "无任何卡: --json ok=None / exit=2 / cards=0")
    # 有目录、有卡目录但没有 card.md / tc.md
    empty_cards = make_fixture("emptycards", [{"name": "card-001", "nofile": True},
                                              {"name": "tc-001", "kind": "tc", "nofile": True}])
    code, text = run(empty_cards)
    check(code == 2 and "BRW-NO-CARD" in text, "有目录无 card.md/tc.md: 退出码 2（同样无法检查）")
    check("BRW-MISSING-BLOCK" not in text, "有目录无卡: 不许报「声明了没记录」（根本没卡）")
    bad = one_card([row("BRW-001", "card-001", ret="")])
    code, text = run(bad)
    check(code == 1 and "退出码 1" in text, "有 ERROR: 退出码 1")
    check("2026-09-29" in text, "有 ERROR: 提示语指向 2026-09-29 事故")
    code, _t = run(good)
    check(code == 0, "干净: 退出码 0")
    code, text = run(clean, "--nope")
    check(code == 2, "未知参数: 退出码 2（不能当通过）")
    code, text = run(clean, "--help")
    check(code == 0 and "退出码：0 干净" in text, "--help: 退出码 0 且印出三态语义")
    check("--tab-cap" in text, "--help: 印出 --tab-cap 开关")
    code, text = run(os.path.join(TMP, "不存在的根"))
    check(code == 2 and "无法检查" in text, "根不存在: 退出码 2")

    # ------------------------------ 8 顺序与编码
    print("\n[8] 顺序与编码")
    multi = make_fixture("order", [{"name": "card-001", "rows": [
        row("BRW-001", "card-001", "s1", "3", "2026-09-30 10:00", "2026-09-30 10:05"),
        row("BRW-002", "card-001", "s2", "3", "2026-09-30 10:10", "2026-09-30 10:15"),
    ]}, {"name": "card-002", "rows": [
        row("BRW-003", "card-002", "s3", "3", "2026-09-30 10:20", "2026-09-30 10:25"),
    ]}])
    code, text = expect(multi, 0, absent=("BRW-ID-ORDER", "BRW-ID-DUP"), label="跨卡递增")
    dup_cross = make_fixture("order_dup", [{"name": "card-001", "rows": [
        row("BRW-001", "card-001", "s1", "3", "2026-09-30 10:00", "2026-09-30 10:05")]},
        {"name": "card-002", "rows": [
            row("BRW-001", "card-002", "s2", "3", "2026-09-30 10:10", "2026-09-30 10:15")]}])
    expect(dup_cross, 1, "BRW-ID-DUP", label="跨卡重复 id")
    order_cross = make_fixture("order_cross", [{"name": "card-001", "rows": [
        row("BRW-005", "card-001", "s1", "3", "2026-09-30 10:00", "2026-09-30 10:05")]},
        {"name": "card-002", "rows": [
            row("BRW-003", "card-002", "s2", "3", "2026-09-30 10:10", "2026-09-30 10:15")]}])
    expect(order_cross, 1, "BRW-ID-ORDER", label="跨卡乱序（5 → 3）")
    gbk = make_fixture("gbk", clean_cards(), encoding="gbk")
    expect(gbk, 0, absent=("BRW-FORMAT-STALE",), label="GBK 卡文件")
    gbk_bad = make_fixture("gbk_bad", [{"name": "card-001", "rows": [
        row("BRW-001", "card-001", ret="")], "body": "- 用 BrowserSkill 取证。\n"}], encoding="gbk")
    expect(gbk_bad, 1, "BRW-NO-RETURN", label="GBK 也能报错")
    root_ps = make_fixture("projroot", clean_cards())
    inner = os.path.join(root_ps, "project-state")
    ca, ta = run(root_ps, "--check")
    cb, tb = run(inner, "--check")
    check(ca == 0 and cb == 0 and ta == tb, "传 project-state 与传根等价")

    # ------------------------------ 9 检查器自身变异（证明断言有牙）
    print("\n[9] 检查器自身变异（失去检测力才算变异成功）")
    src = open(AUDIT, "r", encoding="utf-8").read()
    os.makedirs(MUT, exist_ok=True)
    mutations = [
        ("no_return", "if not returned:", "if False:", "取用未归还",
         lambda: one_card([row("BRW-001", "card-001", ret="")])),
        ("overlap", "if prev is not None and t < prev[1]:", "if False:", "独占时段重叠",
         lambda: ov(tag="ov_mut")),
        ("tabs_over", "elif tnum > tab_cap:", "elif False:", "tab 超预算",
         lambda: at_cap),
    ]
    want = {"no_return": "BRW-NO-RETURN", "overlap": "BRW-OVERLAP", "tabs_over": "BRW-TABS-OVER"}
    for name, old, new, desc, mk in mutations:
        path = os.path.join(MUT, "%s.py" % name)
        check(old in src, "变异 %s: 锚点存在（%s）" % (name, desc))
        if old not in src:
            continue
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src.replace(old, new))
        code, text = run(mk(), script=path)
        check(want[name] not in codes(text),
              "变异 %s: 该规则**失去检测力**（%s 未命中）" % (name, want[name]))
        code, text = run(clean, script=path)
        check(code == 0 and codes(text) == set(),
              "变异 %s: 反向控制——干净夹具仍 0 且无 code" % name)

    # ---- 开关本身也要变异：把上限改成 4 必须**漏报**，证明断言真的在核上限
    over4 = make_fixture("n_cap4", [{"name": "card-001", "rows": [row("BRW-001", "card-001", tabs="4")]}])
    code, text = run(over4)
    check(code == 1 and "BRW-TABS-OVER" in codes(text), "上限 3：tabs=4 被抓住")
    code, text = run(over4, "--tab-cap", "4")
    check(code == 0 and "BRW-TABS-OVER" not in codes(text),
          "上限改 4（--tab-cap 4）：同一夹具**漏报**（证明断言真在核上限）")
    code, text = run(over4, "--tab-cap", "2")
    check(code == 1 and "BRW-TABS-OVER" in codes(text), "上限改 2：tabs=4 仍报")
    check("单会话上限 2" in text, "上限改 2: 提示语里的上限跟着变")
    cap4_mut = os.path.join(MUT, "cap4.py")
    check('DEFAULT_TAB_CAP = 3' in src, "变异 cap: 锚点存在（源码里默认上限是 3）")
    if 'DEFAULT_TAB_CAP = 3' in src:
        with open(cap4_mut, "w", encoding="utf-8") as fh:
            fh.write(src.replace("DEFAULT_TAB_CAP = 3", "DEFAULT_TAB_CAP = 4"))
        code, text = run(over4, script=cap4_mut)
        check(code == 0 and "BRW-TABS-OVER" not in codes(text),
              "变异 cap: 把上限临时改成 4 必须漏报（证明该断言真的在核上限）")

    # ------------------------------------------------------------ 汇总
    print("\n断言：%d 通过 / %d 失败" % (PASS, len(FAILS)))
    if FAILS:
        print("失败清单：")
        for f in FAILS:
            print("  - %s" % f)
        print("（夹具保留在 %s 供复现）" % TMP)
        return 1
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
