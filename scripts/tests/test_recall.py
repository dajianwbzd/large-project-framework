#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_recall.py — recall.py 的回归 + 变异对拍（纯 stdlib，不依赖 pytest）

为什么有这个脚本
----------------
`recall.py` 的**大部分分支在两个真实工程上都跑不到**：它们一张机读块都没有。
也就是说"覆盖矩阵 result 非法侦测""发现项无归属侦测""证据指针存在性"这些检查
**从没被任何真实数据执行过**——它们在发布时是**未验证代码**。

两条判据缺一不可（本框架踩过的坑）：
  ① **能拦**：每个检查都要有一个**故意让它失败**的夹具。只跑真实工程 = 只证明"不报错"。
  ② **不乱拦**：还要有一个**干净夹具**，断言 0 严重。变异对拍只能证明 ①。

所以本脚本 = 1 个干净夹具（断言 0 严重 0 提示）+ N 个单点变异（每个断言"只有它该响、
且只响一次"）。有几条断言的是**某码不出现**——那是给「空与缺混为一谈」这族 bug 上的锁。

用法
----
    python test_recall.py            # 跑全部，打印逐条结果，失败则非零退出
    python test_recall.py -v         # 额外打印每条的实际命中
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
# 回归与脚本同目录（草稿期）或脚本在上层（框架 `scripts/tests/` 期）——两种布局都要能跑。
# 写死同目录的版本搬进框架就崩（第一次搬进框架时报了 `can't open file '.../tests/recall.py'`）。
RECALL = next((p for p in (os.path.join(HERE, "recall.py"),
                           os.path.join(HERE, os.pardir, "recall.py"))
               if os.path.exists(p)), os.path.join(HERE, "recall.py"))
TMP = os.path.join(HERE, "_fixture_tmp")


def _write_text(path, text):
    """测试夹具唯一写入口：实路径必须仍在本测试的 _fixture_tmp 之下（防穿越）。"""
    rp = os.path.realpath(path)
    root = os.path.realpath(TMP)
    if os.path.commonpath([rp, root]) != root:
        raise AssertionError("夹具路径越界：%r" % path)
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    Path(rp).write_text(text, encoding="utf-8", newline="\n")


# ----------------------------------------------------------------- 夹具构建

CLEAN_CONFIG = {
    "project": "夹具项目",
    "mode": "full",
    "schema_version": "1",
    "status_extensions": ["deprecated"],
}

CLEAN_CARDS = {
    "card-001": ("done", None),
    "card-002": ("in_progress", "等第三方接口联调"),
    "card-004": ("deprecated", None),   # 已在 config 声明 → 不许报
}

CLEAN_COVERAGE = """# 覆盖矩阵

| 维度 | 结果 | 证据 | 发现项 |
|---|---|---|---|
| D1 跨卡链路 | pass | 需求卡/card-001/card.md | - |
| D2 数据对账 | not_covered | - | - |
| D3 契约全量一致 | pass | 自述（报告方自述，本机不可复核） | F-01 |

<!-- machine:coverage -->
dim | result | evidence | finding
D1 | pass | 需求卡/card-001/card.md | -
D2 | not_covered | - | -
D3 | pass | 自述 | F-01
<!-- /machine:coverage -->
"""

CLEAN_FINDINGS = """# 发现项

人读的表（历史散文）保留在此，机读块在其后。

<!-- machine:findings -->
id | severity | owner | ref | state | anchor | entry
F-01 | required | A | card-001 | fixed | 需求卡/card-001/card.md:12 | 复跑 test_login_expired
F-02 | nit | C | - | accepted | 未知 | 未复现，观察窗口 7 天
F-03 | required | B | card-005 | open | 需求卡/card-001/card.md#验收 | python -m pytest -k 契约
<!-- /machine:findings -->
"""

# 修复卡（`source` 非空 = 由测试卡发现项派生）。干净夹具里的这张**三态中最好的一态**：
# 键在、值在、锚点路径存在 ⇒ 任何锚点检查都不该响。
CLEAN_FIX_CARD = {
    "id": "card-005",
    "status": "done",
    "source": "tc-001#F-03",
    "anchor": "需求卡/card-001/card.md:12",
    "entry": "python -m pytest -k 契约",
}


def w(path, text):
    _write_text(path, text)


def card_md(cid, status, last_error=None, attempts=1, extra=None):
    """卡 frontmatter。`extra` = 追加的 `键: 值`（v5.7.0 的 `source`/`anchor`/`entry`）。"""
    lines = ["---", "id: %s" % cid, "status: %s" % status, "attempts: %d" % attempts]
    if last_error:
        lines.append("last_error: %s" % last_error)
    for k, v in (extra or {}).items():
        lines.append("%s: %s" % (k, v))
    lines += ["---", "", "# %s" % cid, "", "卡正文。"]
    return "\n".join(lines) + "\n"


def build_clean(base):
    """干净夹具：每一条检查都存在、且都应当**不响**。"""
    ps = os.path.join(base, "project-state")
    w(os.path.join(ps, "config.json"),
      json.dumps(CLEAN_CONFIG, ensure_ascii=False, indent=2))
    w(os.path.join(ps, "运行日志.md"), "[2026-09-21 10:00] 夹具启动\n")
    w(os.path.join(ps, "任务清单.md"), "# 任务清单\n\n- [ ] T-01 演示任务\n")

    for cid, (status, err) in CLEAN_CARDS.items():
        w(os.path.join(ps, "需求卡", cid, "card.md"), card_md(cid, status, err))
    # 平铺布局的卡（同一工程内两种布局并存，必须都读得到）
    w(os.path.join(ps, "需求卡", "card-003.md"), card_md("card-003", "todo"))
    # 修复卡（带定位锚点）
    w(os.path.join(ps, "需求卡", CLEAN_FIX_CARD["id"], "card.md"),
      card_md(CLEAN_FIX_CARD["id"], CLEAN_FIX_CARD["status"],
              extra={"source": CLEAN_FIX_CARD["source"],
                     "anchor": CLEAN_FIX_CARD["anchor"],
                     "entry": CLEAN_FIX_CARD["entry"]}))

    tc = os.path.join(ps, "测试卡", "tc-001")
    w(os.path.join(tc, "tc.md"), card_md("tc-001", "done"))
    w(os.path.join(tc, "覆盖矩阵.md"), CLEAN_COVERAGE)
    w(os.path.join(tc, "发现项.md"), CLEAN_FINDINGS)

    w(os.path.join(ps, "待办", "自然人.md"), "# 待办\n\n- [ ] 一件待拍板的事\n")
    w(os.path.join(ps, "进度", "overall.json"), json.dumps({
        "project": "夹具项目",
        "updated": "2026-09-21",
        "cards": {
            "card-001": {"status": "done"},
            "card-002": {"status": "in_progress"},
            "card-003": {"status": "todo"},
            "card-004": {"status": "deprecated"},
            "card-005": {"status": "done"},
            "tc-001": {"status": "done"},
        },
    }, ensure_ascii=False, indent=2))
    return base


def clone(base, name):
    dst = os.path.join(TMP, name)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(base, dst)
    return dst


def edit(path, old, new, count=1):
    """读-改-写；`old` 必须出现，否则夹具本身错了（不许静默跳过变异）。"""
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    if old not in txt:
        raise AssertionError("变异目标不存在：%r in %s" % (old[:60], path))
    txt = txt.replace(old, new, count)
    _write_text(path, txt)


def edit_json(path, fn):
    """在**结构层**改夹具（不靠字面替换）——`json.dumps` 的缩进形态会变，
    字面匹配写出来的夹具过两天就匹配不上了（第一版就是这么炸的）。"""
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    fn(d)
    _write_text(path, json.dumps(d, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------- 运行器

def run(root, extra=()):
    cmd = [sys.executable, "-W", "ignore", RECALL, root, "--json"] + list(extra)
    p = subprocess.run(cmd, capture_output=True, encoding="utf-8")
    if p.returncode == 2:
        raise AssertionError("recall.py 无法检查夹具（退出码 2）：%s" % p.stderr.strip())
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise AssertionError("JSON 解析失败；原始输出：\n%s\n%s" % (p.stdout[:2000], p.stderr[:2000]))


def codes(rep):
    out = {}
    for i in rep["issues"]:
        out[i["code"]] = out.get(i["code"], 0) + 1
    for u in rep.get("unmigrated", []):
        # 迁移项不带 code，用固定桶统计
        out["__unmigrated__"] = out.get("__unmigrated__", 0) + 1
    return out


def sev_count(rep, sev):
    return sum(1 for i in rep["issues"] if i["sev"] == sev)


# --------------------------------------------------------------------- 断言

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    return bool(ok)


def expect(rep, must=(), must_not=(), once=(), sev_zero=None):
    """must=必须出现的码；must_not=必须不出现的码；once=必须恰好出现 n 次的码。"""
    cs = codes(rep)
    ok = True
    msgs = []
    for c in must:
        if cs.get(c, 0) < 1:
            ok = False
            msgs.append("缺 %s（实际命中：%s）" % (c, _fmt(cs)))
    for c in must_not:
        if cs.get(c, 0) > 0:
            ok = False
            msgs.append("不该有 %s（出现 %d 次）" % (c, cs[c]))
    for c, n in once:
        if cs.get(c, 0) != n:
            ok = False
            msgs.append("%s 应恰好 %d 次，实际 %d" % (c, n, cs.get(c, 0)))
    if sev_zero is not None and sev_count(rep, sev_zero) != 0:
        ok = False
        msgs.append("严重=%d 应为 0" % sev_count(rep, sev_zero))
    return ok, "；".join(msgs)


def _fmt(cs):
    return ", ".join("%s×%d" % (k, v) for k, v in sorted(cs.items())) or "无"


def main():
    verbose = "-v" in sys.argv
    if os.path.exists(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP)

    clean = build_clean(os.path.join(TMP, "clean"))

    # ---- ① 干净夹具：0 严重、0 提示、0 迁移 —— "不乱拦"
    rep = run(clean)
    ok, d = expect(rep, sev_zero="E")
    ok2 = sev_count(rep, "W") == 0
    check("CLEAN 0 严重 0 提示", ok and ok2,
          d or ("严重=%d 提示=%d" % (sev_count(rep, "E"), sev_count(rep, "W"))))
    check("CLEAN 读到 5 张卡（4 卷宗 + 1 平铺）",
          rep["counts"]["card_dirs"] == 5, "实际 %d" % rep["counts"]["card_dirs"])
    check("CLEAN 读到 1 张测试卡", rep["counts"]["tc_dirs"] == 1)
    check("CLEAN 覆盖 3 维全部可读",
          rep["coverage"][0]["total"] == 3, "实际 %d" % rep["coverage"][0]["total"])
    check("CLEAN 声明过的扩展状态不报",
          "STATUS-EXTENSION" not in codes(rep))
    check("CLEAN evidence=自述/- 不当路径校验",
          "EVIDENCE-MISSING" not in codes(rep))
    # v5.7.0 定位锚点：干净夹具里三个字段齐备 ⇒ 锚点族**一条都不许响**
    check("CLEAN 锚点族 0 命中（含「anchor=未知 且有 entry」不算缺）",
          rep["counts"]["findings_no_anchor"] == 0
          and rep["counts"]["findings_format_stale"] == 0
          and rep["counts"]["cards_no_anchor"] == 0
          and rep["counts"]["cards_anchor_dangling"] == 0
          and not any(c.startswith(("TC-FINDING-NO-ANCHOR", "TC-ANCHOR-DANGLING",
                                    "TC-FINDINGS-FORMAT-STALE", "CARD-NO-ANCHOR",
                                    "CARD-ANCHOR-DANGLING"))
                      for c in codes(rep)),
          "锚点族误报：%s" % _fmt(codes(rep)))

    # 任务清单：夹具的清单**一条都没挂卡** ⇒ 正确行为是报「无从机核」，而不是「无差异」，
    # 也不是对每一张 done 卡报一条"未勾"（那是拿一个没启用的视图造 4 条假差异）。
    out_clean = subprocess.run([sys.executable, "-W", "ignore", RECALL, clean],
                               capture_output=True, encoding="utf-8").stdout
    check("CLEAN 任务清单未挂卡 → 报「无从机核」且不造差异",
          "无从机核" in out_clean and "TASKLIST-STALE" not in out_clean
          and "TASKLIST-AHEAD" not in out_clean, out_clean[-300:])

    # 任务清单真的在用时的两方对账：**完成标准的 `[x]` 不算任务**——纯计数会把它算进去
    # （本脚本首版就是纯计数），故把这个形态钉进回归；两条对齐时应当 0 差异。
    cf = clone(clean, "tasklist")
    w(os.path.join(cf, "project-state/任务清单.md"),
      "# 任务清单\n\n## 完成标准（客观可验证）\n\n- [x] 标准1（编译 0 error）\n"
      "- [x] 标准2\n\n## 任务清单\n\n- [x] T-01 已完成的活\n    - 卡：card-001\n"
      "- [ ] T-02 还没干的活\n    - 卡：card-003\n")
    ok, d = expect(run(cf), must_not=["TASKLIST-STALE", "TASKLIST-AHEAD"])
    check("任务清单两方对齐 0 差异；完成标准的 `[x]` 不被算成任务", ok, d)

    # 未闭环发现项的下钻指针必须**印进摘要**——这是 P0 的全部收益面：
    # 只写不印 = 又一个「声明了从不读」（v5.5.0 教训）。用文本断言锁住。
    p_oa = subprocess.run([sys.executable, "-W", "ignore", RECALL, clean],
                          capture_output=True, encoding="utf-8")
    check("CLEAN 未闭环发现项的 锚点/复现 被印进摘要（下钻入口）",
          "⤷ F-03" in p_oa.stdout and "锚点=`需求卡/card-001/card.md#验收`" in p_oa.stdout
          and "复现=`python -m pytest -k 契约`" in p_oa.stdout,
          p_oa.stdout[-500:])

    # ---- ①b 真实写法对拍（**从真实工程取证来的形态**，不是想出来的）
    #
    # 取证来源：`//nas/work/示例工作台B/project-state/测试卡/tc-001/*` 的 `证据指针` 列，
    # 实际出现过这四种写法。首版只认"相对项目根的裸路径"，把前三种全报成了缺口——
    # 这一组就是把"取证过的写法"钉死在回归里，防止以后又被改窄。
    rw = clone(clean, "realworld")
    edit(os.path.join(rw, "project-state/测试卡/tc-001/覆盖矩阵.md"),
         "D1 | pass | 需求卡/card-001/card.md | -\n"
         "D2 | not_covered | - | -\n"
         "D3 | pass | 自述 | F-01",
         "D1 | pass | 需求卡/card-001/card.md | -\n"          # ① 相对 project-state
         "D2 | not_covered | 需求卡/card-001/* | -\n"          # ② 通配的"证据根"
         "D3 | pass | project-state/需求卡/card-001/card.md | F-01\n"   # ③ 带前缀。④ 命令串见下
         "D3 | pass | grep -cE '^  /' project-state/契约/api.yaml  # 0 | F-01")  # ④ 命令+结果
    ok, d = expect(run(rw), must_not=["EVIDENCE-MISSING"])
    check("真实写法（相对 project-state / 通配根 / 带前缀 / 命令串）都不误报", ok, d)

    # ---- ② 变异：单点改动，断言"该响的响、且只响一次"
    mutants = []

    def mut(name, expect_args, build):
        mutants.append((name, expect_args, build))

    # 账本三件（结构层改，避免缩进形态漂移）
    ledger = lambda d: os.path.join(d, "project-state/进度/overall.json")
    mut("账本缺 card-003（并多一个游离键）",
        dict(must=["LEDGER-MISSING", "LEDGER-ORPHAN"], once=[("LEDGER-MISSING", 1)]),
        lambda d: edit_json(ledger(d),
                            lambda j: j["cards"].update(
                                {"zzz-游离": j["cards"].pop("card-003")})))
    mut("账本多 card-099", dict(must=["LEDGER-ORPHAN"], once=[("LEDGER-ORPHAN", 1)]),
        lambda d: edit_json(ledger(d),
                            lambda j: j["cards"].update({"card-099": {"status": "todo"}})))
    mut("卡账状态漂移", dict(must=["STATUS-DRIFT"], once=[("STATUS-DRIFT", 1)]),
        lambda d: edit_json(ledger(d),
                            lambda j: j["cards"]["card-002"].update({"status": "done"})))

    # 状态写法／扩展
    mut("状态写作 in-progress", dict(must=["STATUS-VARIANT"], once=[("STATUS-VARIANT", 1)]),
        lambda d: edit(os.path.join(d, "project-state/需求卡/card-002/card.md"),
                       "status: in_progress", "status: in-progress"))
    mut("未声明扩展 paused", dict(must=["STATUS-EXTENSION"], once=[("STATUS-EXTENSION", 1)]),
        lambda d: edit(os.path.join(d, "project-state/需求卡/card-002/card.md"),
                       "status: in_progress", "status: paused"))

    # 机读块：覆盖矩阵
    mut("result 非法值 passed", dict(must=["TC-RESULT-INVALID"], once=[("TC-RESULT-INVALID", 1)]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/覆盖矩阵.md"),
                       "D1 | pass | 需求卡/card-001/card.md | -",
                       "D1 | passed | 需求卡/card-001/card.md | -"))
    mut("未回填 unknown（提示）", dict(must=["TC-RESULT-UNKNOWN"], once=[("TC-RESULT-UNKNOWN", 1)]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/覆盖矩阵.md"),
                       "D2 | not_covered | - | -", "D2 | unknown | - | -"))
    mut("证据指针不存在", dict(must=["EVIDENCE-MISSING"], once=[("EVIDENCE-MISSING", 1)]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/覆盖矩阵.md"),
                       "D1 | pass | 需求卡/card-001/card.md | -",
                       "D1 | pass | 证据/根本没有这个文件.md | -"))
    mut("覆盖引用不存在的发现项",
        dict(must=["TC-FINDING-DANGLING"], once=[("TC-FINDING-DANGLING", 1)]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/覆盖矩阵.md"),
                       "D3 | pass | 自述 | F-01", "D3 | pass | 自述 | F-09"))

    # 机读块：发现项
    mut("发现项无归属", dict(must=["TC-FINDING-NO-OWNER"], once=[("TC-FINDING-NO-OWNER", 1)]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/发现项.md"),
                       "F-01 | required | A | card-001 | fixed",
                       "F-01 | required |  | card-001 | fixed"))
    mut("owner=B 但 ref 悬空", dict(must=["TC-REF-DANGLING"], once=[("TC-REF-DANGLING", 1)]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/发现项.md"),
                       "F-01 | required | A | card-001 | fixed",
                       "F-01 | required | B | card-777 | fixed"))
    mut("发现项 state 非法", dict(must=["TC-FINDING-STATE-INVALID"]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/发现项.md"),
                       "F-01 | required | A | card-001 | fixed",
                       "F-01 | required | A | card-001 | wip"))

    # ---- 定位锚点（v5.7.0）——**每条检查都成对**：下面三条是"能拦"，
    #      紧跟的四条反向断言是"不乱拦"（变异套件对"没改坏却报"是结构性失明的）。
    mut("发现项两列皆空 → 无锚点",
        dict(must=["TC-FINDING-NO-ANCHOR"], once=[("TC-FINDING-NO-ANCHOR", 1)]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/发现项.md"),
                       "F-01 | required | A | card-001 | fixed | 需求卡/card-001/card.md:12 "
                       "| 复跑 test_login_expired",
                       "F-01 | required | A | card-001 | fixed |  | "))
    mut("发现项 anchor 路径不存在",
        dict(must=["TC-ANCHOR-DANGLING"], once=[("TC-ANCHOR-DANGLING", 1)]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/发现项.md"),
                       "| 需求卡/card-001/card.md:12 | 复跑 test_login_expired",
                       "| repos/根本没有这个文件.py:12 | 复跑 test_login_expired"))
    mut("发现项机读块为旧列集（5 列）",
        dict(must=["TC-FINDINGS-FORMAT-STALE"],
             # ★ 这一条是 R8 的核心：格式旧时**不许**再逐行报「无锚点」
             #   （旧行内容一条不缺，只是格式老；两条同时报 = 同一件事报两遍，
             #    且把"格式旧"渲染成"内容不合格"）
             must_not=["TC-FINDING-NO-ANCHOR"]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/发现项.md"),
                       "id | severity | owner | ref | state | anchor | entry\n"
                       "F-01 | required | A | card-001 | fixed | 需求卡/card-001/card.md:12 "
                       "| 复跑 test_login_expired\n"
                       "F-02 | nit | C | - | accepted | 未知 | 未复现，观察窗口 7 天\n"
                       "F-03 | required | B | card-005 | open | 需求卡/card-001/card.md#验收 "
                       "| python -m pytest -k 契约",
                       "id | severity | owner | ref | state\n"
                       "F-01 | required | A | card-001 | fixed\n"
                       "F-02 | nit | C | - | accepted\n"
                       "F-03 | required | B | card-005 | open"))
    # ★ 行号漂移**不许**报（S2）：这是本轮的**主动设计选择**，不是漏检——
    #   锚点的身份是「文件（+符号可选）」，行号只用于显示（依据：aider 把 `line`
    #   只用于 render，不作索引身份；行号随无关编辑漂移，拿它当判据会变成假阳性发生器）。
    mut("反·锚点只改行号（12→9999）不许报",
        dict(must_not=["TC-ANCHOR-DANGLING"]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/发现项.md"),
                       "需求卡/card-001/card.md:12", "需求卡/card-001/card.md:9999"))
    # ★ 「anchor=未知」是**合法**的（只要 entry 在）——夹具里的 F-02 就是这个形态，
    #   这条锁住"别把'未知'当成'缺'"，也正是 R1 在锚点上的落地。
    mut("反·anchor=未知 且有 entry 不算缺",
        dict(must_not=["TC-FINDING-NO-ANCHOR"]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/发现项.md"),
                       "F-01 | required | A | card-001 | fixed | 需求卡/card-001/card.md:12 "
                       "| 复跑 test_login_expired",
                       "F-01 | required | A | card-001 | fixed | 未知 | 复跑 test_login_expired"))
    # ★ 锚点写成一句人话（含空白）**不参与路径校验**——否则每次调用都会稳定地
    #   产生同一批噪声（与 `evidence` 列的命令串同一处理）。
    mut("反·锚点写成说明文字（含空白）不报悬空",
        dict(must_not=["TC-ANCHOR-DANGLING"]),
        lambda d: edit(os.path.join(d, "project-state/测试卡/tc-001/发现项.md"),
                       "| 需求卡/card-001/card.md:12 | 复跑 test_login_expired",
                       "| 登录流程那一带（待细化） | 复跑 test_login_expired"))

    # ---- 修复卡的定位锚点：**三态必须分开**（键缺 / 键在值空 / 值在但路径悬空）
    mut("修复卡无锚点（键在值空）→ E",
        dict(must=["CARD-NO-ANCHOR"], once=[("CARD-NO-ANCHOR", 1)]),
        lambda d: edit(os.path.join(d, "project-state/需求卡/card-005/card.md"),
                       "anchor: 需求卡/card-001/card.md:12\nentry: python -m pytest -k 契约",
                       "anchor: \nentry: "))
    mut("修复卡锚点路径不存在 → E",
        dict(must=["CARD-ANCHOR-DANGLING"], once=[("CARD-ANCHOR-DANGLING", 1)]),
        lambda d: edit(os.path.join(d, "project-state/需求卡/card-005/card.md"),
                       "anchor: 需求卡/card-001/card.md:12",
                       "anchor: repos/根本不存在.py:3"))
    # ★ 键**不存在** = 还没迁移 ⇒ 只提示、不判 E（进 §7）。这是 R8 的第三次落地：
    #   不做这个区分，任何旧工程的**全部**修复卡会在升级当天一起变红。
    mut("反·修复卡无 anchor/entry 键（未迁移）不判 E 只提示",
        dict(must_not=["CARD-NO-ANCHOR"], must=["__unmigrated__"]),
        lambda d: edit(os.path.join(d, "project-state/需求卡/card-005/card.md"),
                       "source: tc-001#F-03\nanchor: 需求卡/card-001/card.md:12\n"
                       "entry: python -m pytest -k 契约",
                       "source: tc-001#F-03"))
    # ★ 普通卡（无 source）**不查锚点**——"哪里要改"对新需求而言本就未知，
    #   对它报缺会把这条线淹掉。
    mut("反·普通卡（无 source）不查锚点",
        dict(must_not=["CARD-NO-ANCHOR"]),
        lambda d: edit(os.path.join(d, "project-state/需求卡/card-005/card.md"),
                       "source: tc-001#F-03\n", "source: -\n"))
    mut("机读块整块缺失（非迁移模式）",
        dict(must=["TC-NO-MACHINE-BLOCK", "TC-NO-FINDINGS-BLOCK"]),
        lambda d: _strip_block(os.path.join(d, "project-state/测试卡/tc-001/覆盖矩阵.md"),
                               "coverage")
        or _strip_block(os.path.join(d, "project-state/测试卡/tc-001/发现项.md"), "findings"))
    mut("机读块整块缺失（迁移模式降级）",
        dict(must_not=["TC-NO-MACHINE-BLOCK"], must=["__unmigrated__"]),
        lambda d: _strip_block(os.path.join(d, "project-state/测试卡/tc-001/覆盖矩阵.md"),
                               "coverage"))

    # 账本散文／批次备忘录
    mut("账本字段散文超阈值", dict(must=["LEDGER-PROSE"], once=[("LEDGER-PROSE", 1)]),
        lambda d: edit_json(ledger(d), lambda j: j.update({"updated": "长" * 900})))
    mut("批次备忘录膨胀", dict(must=["LEDGER-BATCH-BLOAT"], once=[("LEDGER-BATCH-BLOAT", 1)]),
        lambda d: edit_json(ledger(d),
                            lambda j: j.update({"project_manager_batch01": "备" * 5000})))

    # ★ 这四条是给「空与缺混为一谈」上的锁：账本读不到 ≠ 账本少条目
    mut("账本无 cards 面（不许报缺失）",
        dict(must=["LEDGER-SCHEMA-UNKNOWN"],
             must_not=["LEDGER-MISSING", "LEDGER-ORPHAN", "STATUS-DRIFT"]),
        lambda d: edit_json(ledger(d), lambda j: j.update({"nope": j.pop("cards")})))
    mut("测试卡平铺（不许报缺覆盖/缺发现项）",
        dict(must=["TC-FLAT-LAYOUT"],
             must_not=["TC-NO-COVERAGE", "TC-NO-FINDINGS"]),
        lambda d: _flatten_tc(d))

    # 任务清单 ↔ 卡状态（**三个方向各一条**：修宽/修窄都必须仍能拦住真差异）
    #
    # 夹具前提：把 card-002 也置 done 并让 T-00 勾上 ⇒ 该视图**处于维护状态**，
    # 逐卡对账才成立（勾选 0 条时脚本按"视图未维护"处理，不产差异——见下一条）。
    def _tl_ready(d):
        edit_json(os.path.join(d, "project-state/进度/overall.json"),
                  lambda j: j["cards"]["card-002"].update({"status": "done"}))
        edit(os.path.join(d, "project-state/需求卡/card-002/card.md"),
             "status: in_progress", "status: done")

    mut("任务行挂着卡却没勾（卡已 done）",
        dict(must=["TASKLIST-STALE"], once=[("TASKLIST-STALE", 1)]),
        lambda d: (_tl_ready(d),
                   w(os.path.join(d, "project-state/任务清单.md"),
                     "# 任务清单\n\n## 完成标准（客观可验证）\n\n- [x] 标准1\n\n"
                     "## 任务清单\n\n- [x] T-00 已完成\n    - 卡：card-002\n"
                     "- [ ] T-01 已完成的活\n    - 卡：card-001\n")))
    mut("任务行超前（关联卡仍 todo）", dict(must=["TASKLIST-AHEAD"], once=[("TASKLIST-AHEAD", 1)]),
        lambda d: w(os.path.join(d, "project-state/任务清单.md"),
                    "# 任务清单\n\n## 任务清单\n\n- [x] T-01 已完成的活\n    - 卡：card-001\n"
                    "- [x] T-09 未来的活\n    - 卡：card-003\n"))
    # ★ done 卡**根本没有任务行**≠「任务行未勾」（实测：某工程 43 条任务全勾，
    #   35 张 done 卡被报成"未勾"，真相是清单压根没覆盖那些卡）——用文本断言锁住，
    #   因为它要求"报了但报成另一类"，不是"多一个码/少一个码"。
    ut = clone(clean, "untracked")
    w(os.path.join(ut, "project-state/任务清单.md"),
      "# 任务清单\n\n## 任务清单\n\n- [x] T-02 别的活\n    - 卡：card-003\n")
    p_ut = subprocess.run([sys.executable, "-W", "ignore", RECALL, ut],
                          capture_output=True, encoding="utf-8")
    check("done 卡无对应任务行 → 算「清单未覆盖」不算「滞后」",
          "清单未覆盖的 done 卡 2 张" in p_ut.stdout and "TASKLIST-STALE" not in p_ut.stdout,
          p_ut.stdout[-400:])

    for idx, (name, exp, build) in enumerate(mutants):
        root = clone(clean, "m_%02d" % idx)
        build(root)
        extra = ("--allow-unmigrated",) if "__unmigrated__" in (exp.get("must") or ()) else ()
        rep = run(root, extra)
        ok, d = expect(rep, **exp)
        check("变异·" + name, ok, d)

    # ---- ③ 预算裁剪：不许裁掉计数
    root = clone(clean, "budget")
    p = subprocess.run([sys.executable, "-W", "ignore", RECALL, root, "--budget", "80"],
                       capture_output=True, encoding="utf-8")
    out = p.stdout
    check("极小预算仍保留 0 一句话计数",
          "需求卡 **5** 张" in out and "测试卡 **1** 张" in out,
          "摘要头部丢失计数：\n%s" % out[:600])
    check("极小预算明细被截断且有显式告警",
          "省略" in out and "计数" in out, "无截断告警")
    check("极小预算输出仍显著小于全文",
          len(out.encode("utf-8")) < 6000, "实际 %d 字节" % len(out.encode("utf-8")))

    # ---- 汇总
    bad = [r for r in RESULTS if not r[1]]
    print("=" * 72)
    for name, ok, d in RESULTS:
        print("%s %s%s" % ("✓" if ok else "✗", name, ("   ← " + d) if (d and (not ok or verbose)) else ""))
    print("=" * 72)
    print("共 %d 条：通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(bad), len(bad)))
    if not os.environ.get("KEEP_FIXTURES"):
        shutil.rmtree(TMP, ignore_errors=True)
    return 1 if bad else 0


def _strip_block(path, name):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    out, skip = [], False
    for ln in lines:
        if ("<!-- machine:%s" % name) in ln:
            skip = True
            continue
        if ("<!-- /machine:%s" % name) in ln:
            skip = False
            continue
        if not skip:
            out.append(ln)
    _write_text(path, "\n".join(out) + "\n")


def _flatten_tc(d):
    """把 tc-001 从卷宗布局改成平铺布局。"""
    src = os.path.join(d, "project-state/测试卡/tc-001")
    dst = os.path.join(d, "project-state/测试卡/tc-002.md")
    shutil.copyfile(os.path.join(src, "tc.md"), dst)
    shutil.rmtree(src)


if __name__ == "__main__":
    sys.exit(main())
