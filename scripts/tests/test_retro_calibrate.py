#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""retro_calibrate.py 的回归测试（v4.7.0 新增）。

为什么这个测试必须存在：本脚本管两件**容易悄悄退化**的事——
① 复盘点**按收口卡数**触发（不按墙钟时间），时间只作陈旧提醒；
② 机制 K：上一轮行动项**核验是否真落地**。
第 ② 条最怕的失效是"把没写证据的行动项当成已完成"——提了就完事、报告一片绿，而框架
以为流程在改进。所以每条硬纪律都有一个**会变红的断言**，且本文件用变异验证过
（把守卫改坏 → 对应断言确实变红，见交付报告）。

覆盖：
- A `--due`：首轮（无台账+有卡）为真；差值 <every_n → 假且"还差 N 张"对；差值 ≥every_n → 真；
  陈旧提醒在"日期很旧+卡数有增长"时为真、"日期很旧+无增长"时为假。
- B `--record-action`：ID 递增、懒创建表头、写入后可被 `--actions` 读到。
- C `--actions` 证据判定分档（landed / evidence-missing / unverified）+ `dropped` 不计问题，
  **逾期与落地情况正交**（`landed`+`逾期=是` 同时成立且退出码 1）；退出码 1 只在
  unverified/evidence-missing/逾期 出现时给出。
- D `路径::关键字` 断言：文件在但关键字不在 → evidence-missing。
- E `--actions` 写回 行动项.csv（内容真变、含逗号的行与其他列不被切坏）。
- F `--close` 幂等：同一天调两次不产生重复轮次。
- G `--assemble` 来源部分缺失时明确列"未查过"，不当作"没有异常"。
- H 配置项 `retro_cadence` 缺失/无 config.json 时用默认值 20/30 正常工作。
- I Windows GBK 控制台下各模式不崩、无 GBK 不安全符号。
- J 输入错误退出码 2（不是框架工作区 / 缺必需参数 / 类别非法）。

用法：python scripts/tests/test_retro_calibrate.py
退出码 0=全过；1=有失败。
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parents[1]
SCRIPT = Path(os.environ.get("RC_SCRIPT") or (FRAMEWORK / "scripts" / "retro_calibrate.py"))
ROOT = Path(tempfile.mkdtemp(prefix="rc_test_"))

LEDGER_FIELDS = ["轮次", "日期", "迭代标记", "里程碑", "结论摘要", "行动项数", "已落地数"]
ACTION_FIELDS = ["行动项ID", "来源轮次", "提出日期", "类别", "行动项", "责任人",
                 "预期效果", "核验证据", "核验轮次", "状态", "逾期", "落地情况"]
BURN_FIELDS = ["时间戳", "卡", "角色", "模型", "轮次", "prompt_tokens", "completion_tokens",
               "累计token", "累计成本", "备注"]

fails: list = []
passes: list = []


def check(cond, label, detail=""):
    (passes if cond else fails).append(label)
    tail = f"  -- {detail}" if detail and not cond else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{tail}")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def write_rows(path: Path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def read_rows(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_ledger(rows):
    write_rows(ROOT / "project-state" / "复盘" / "复盘台账.csv", LEDGER_FIELDS, rows)


def write_actions(rows):
    write_rows(ROOT / "project-state" / "复盘" / "行动项.csv", ACTION_FIELDS, rows)


def build(cards=0, config="__unset__", make_progress=True, est_csv=None, burn_csv=None, roles=None,
          evidence_file=None):
    """搭一个最小框架工作区。

    config="__unset__" 表示**不写 config.json**（测缺文件）；传 {} 表示写了但无 retro_cadence。
    """
    if ROOT.exists():
        shutil.rmtree(ROOT, ignore_errors=True)
    ps = ROOT / "project-state"
    (ps / "需求卡").mkdir(parents=True)
    if make_progress:
        (ps / "进度").mkdir(parents=True)
    names = cards if isinstance(cards, (list, tuple)) else \
        [f"card-{i:03d}" for i in range(1, int(cards or 0) + 1)]
    for n in names:
        (ps / "需求卡" / n).mkdir(parents=True, exist_ok=True)
    if config != "__unset__":
        (ps / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    if est_csv is not None:
        write_rows(ps / "进度" / "估算实测.csv",
                   ["卡号", "类型", "估时分钟", "实时分钟", "估token", "实token"], est_csv)
    if burn_csv is not None:
        write_rows(ps / "进度" / "成本流水.csv", BURN_FIELDS, burn_csv)
    for r in (roles or []):
        (ps / "进度" / f"{r}.json").write_text("{}", encoding="utf-8")
    if evidence_file is not None:
        p = ps / "证据.txt"
        p.write_text(evidence_file, encoding="utf-8")
    return ROOT


def rc(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def rc_json(*args, env=None):
    p = rc(*args, "--json", env=env)
    try:
        return json.loads(p.stdout), p
    except ValueError:
        return None, p


def main() -> int:
    print("=" * 72)
    print(f"被测脚本：{SCRIPT}")
    print(f"沙箱：{ROOT}")
    print("=" * 72)

    # ---------------- A --due 到点判定（按收口卡数，不按时间） ----------------
    print("\n测试 A：--due 到点判定（迭代口径 / 首轮 / 陈旧提醒）")
    build(cards=3)                                    # 无台账 + 有卡
    d, p = rc_json("--due", str(ROOT))
    check(d is not None, "无台账 --due --json 可解析", (p.stdout or p.stderr)[:300])
    if d:
        check(d["due"] is True, "无台账 + 有卡 → 首轮 due 为真")
        check(d["first_retro"] is True, "标记 first_retro")
        check(d["current_cards"] == 3, f"收口卡数=3（实际 {d['current_cards']}）")
        check(d["last_iteration"] == 0, "无台账时上次迭代标记=0")

    cfg20 = {"retro_cadence": {"every_n_cards": 20, "stale_days": 30}}
    build(cards=25, config=cfg20)
    write_ledger([{"轮次": "1", "日期": today(), "迭代标记": "10", "里程碑": "M1",
                   "结论摘要": "首轮", "行动项数": "0", "已落地数": "0"}])
    d, _ = rc_json("--due", str(ROOT))
    if d:
        check(d["gap"] == 15, f"差值=25-10=15（实际 {d['gap']}）")
        check(d["due"] is False, "差值 15 < 20 → due 假")
        check(d["cards_to_go"] == 5, f"'还差 5 张到点'（实际 {d['cards_to_go']}）")
    p = rc("--due", str(ROOT))
    check("还差 5 张" in p.stdout, f"文本里写明还差几张（实际首行：{(p.stdout or '').splitlines()[0:1]}）")

    build(cards=30, config=cfg20)
    write_ledger([{"轮次": "1", "日期": today(), "迭代标记": "10", "里程碑": "M1",
                   "结论摘要": "首轮", "行动项数": "0", "已落地数": "0"}])
    d, _ = rc_json("--due", str(ROOT))
    if d:
        check(d["gap"] == 20 and d["due"] is True, "差值 20 >= 20 → due 真")
        check(d["cards_to_go"] == 0, "到点后还差 0 张")

    # 陈旧提醒：日期很旧 + 卡数增长 → 真
    cfg_stale = {"retro_cadence": {"every_n_cards": 100, "stale_days": 1}}
    build(cards=5, config=cfg_stale)
    write_ledger([{"轮次": "1", "日期": "2020-01-01", "迭代标记": "0", "里程碑": "",
                   "结论摘要": "", "行动项数": "0", "已落地数": "0"}])
    d, _ = rc_json("--due", str(ROOT))
    if d:
        check(d["stale_reminder"] is True, "日期很旧 + 卡数有增长 → 陈旧提醒为真")
        check(d["due"] is False, "陈旧提醒**不改** due 真假（差值 5 < 100 仍为假）")

    # 陈旧提醒：日期很旧但卡数没增长 → 假
    build(cards=5, config=cfg_stale)
    write_ledger([{"轮次": "1", "日期": "2020-01-01", "迭代标记": "5", "里程碑": "",
                   "结论摘要": "", "行动项数": "0", "已落地数": "0"}])
    d, _ = rc_json("--due", str(ROOT))
    if d:
        check(d["stale_reminder"] is False, "日期很旧但卡数无增长 → 陈旧提醒为假")
        check("无增长" in d.get("stale_reason", ""), "理由写明无新数据不作提醒")

    # ---------------- B --record-action ----------------
    print("\n测试 B：--record-action（ID 递增 / 懒创建表头 / 可被 --actions 读到）")
    build(cards=1)
    ap = ROOT / "project-state" / "复盘" / "行动项.csv"
    check(not ap.exists(), "起始时 行动项.csv 不存在")
    p = rc("--record-action", str(ROOT), "--类别", "流程优化", "--行动项", "重构站会,含逗号",
           "--责任人", "项目经理")
    check(p.returncode == 0, "--record-action 退出码 0", (p.stderr or "")[-300:])
    check(ap.exists(), "懒创建了 行动项.csv")
    rows = read_rows(ap)
    check(list(rows[0].keys()) == ACTION_FIELDS if rows else False, "表头即 ACTION_FIELDS")
    check(rows and rows[0]["行动项ID"] == "act-001", f"首条 ID=act-001（实际 {rows[0]['行动项ID'] if rows else None}）")
    check(rows and rows[0]["状态"] == "open", "默认状态=open")
    check(rows and rows[0]["逾期"] == "", "新登记行动项 逾期 留空（正交列）")
    check(rows and rows[0]["行动项"] == "重构站会,含逗号", "含逗号的行动项被正确保存（未被切列）")

    rc("--record-action", str(ROOT), "--类别", "工具改进", "--行动项", "第二项", "--责任人", "架构师")
    rows = read_rows(ap)
    check(len(rows) == 2 and rows[1]["行动项ID"] == "act-002", "ID 递增到 act-002")

    shown, _ = rc_json("--actions", str(ROOT))
    check(shown is not None, "--actions 能读到 record-action 写入的行")
    if shown:
        check(len(shown["items"]) == 2, f"--actions 读到 2 条（实际 {len(shown['items'])}）")
        check(shown["items"][0]["行动项ID"] == "act-001", "读到的首条 ID 一致")

    # ---------------- C --actions 证据分档 + 逾期正交 + dropped ----------------
    print("\n测试 C：--actions 证据分档 + 逾期正交 + dropped 不计问题 + 退出码")
    build(cards=1, evidence_file="流程已落地关键字")
    write_actions([
        {"行动项ID": "act-001", "来源轮次": "1", "提出日期": today(), "类别": "流程优化",
         "行动项": "主动放弃项", "责任人": "PM", "状态": "dropped", "核验证据": "", "核验轮次": ""},
        {"行动项ID": "act-002", "来源轮次": "1", "提出日期": today(), "类别": "工具改进",
         "行动项": "空证据项", "责任人": "架构师", "状态": "open", "核验证据": "", "核验轮次": ""},
        {"行动项ID": "act-003", "来源轮次": "1", "提出日期": today(), "类别": "文档补齐",
         "行动项": "有证据项", "责任人": "产品", "状态": "open",
         "核验证据": "project-state/证据.txt", "核验轮次": ""},
        {"行动项ID": "act-004", "来源轮次": "1", "提出日期": today(), "类别": "其他",
         "行动项": "路径不存在项", "责任人": "测试", "状态": "open",
         "核验证据": "project-state/nope.txt", "核验轮次": ""},
        {"行动项ID": "act-005", "来源轮次": "1", "提出日期": today(), "类别": "流程优化",
         "行动项": "空证据且逾期项", "责任人": "运维", "状态": "open", "核验证据": "",
         "核验轮次": "1"},   # 无台账 → 当前轮次=1；核验轮次 1<=1 → 逾期=是（证据仍判 unverified）
        {"行动项ID": "act-006", "来源轮次": "1", "提出日期": today(), "类别": "职责调整",
         "行动项": "关键字命中项", "责任人": "主控", "状态": "open",
         "核验证据": "project-state/证据.txt::流程已落地关键字", "核验轮次": ""},
        {"行动项ID": "act-007", "来源轮次": "1", "提出日期": today(), "类别": "职责调整",
         "行动项": "关键字未命中项", "责任人": "主控", "状态": "open",
         "核验证据": "project-state/证据.txt::不存在的关键字", "核验轮次": ""},
        {"行动项ID": "act-008", "来源轮次": "1", "提出日期": today(), "类别": "职责调整",
         "行动项": "已落地但逾期未收口项", "责任人": "主控", "状态": "open",
         "核验证据": "project-state/证据.txt", "核验轮次": "1"},   # 落地 landed + 逾期=是 同时成立
    ])
    a, p = rc_json("--actions", str(ROOT))
    check(a is not None, "--actions --json 可解析", (p.stdout or p.stderr)[:300])
    check(p.returncode == 1, f"存在未落地/逾期 → 退出码 1（实际 {p.returncode}）")
    if a:
        c = a["counts"]
        check(c["landed"] == 3, f"landed=3（act-003/006/008，实际 {c['landed']}）")
        check(c["evidence-missing"] == 2, f"evidence-missing=2（act-004/007，实际 {c['evidence-missing']}）")
        check(c["unverified"] == 2, f"unverified=2（act-002/005 空证据，实际 {c['unverified']}）")
        check(c["dropped"] == 1, f"dropped=1（act-001，实际 {c['dropped']}）")
        check(a["overdue_count"] == 2, f"逾期=是 共 2 条（act-005/008，实际 {a['overdue_count']}）")
        check(a["fail_count"] == 6, f"问题合计=2+2+2=6（实际 {a['fail_count']}）")
        by_id = {x["行动项ID"]: x for x in a["items"]}
        check(by_id["act-001"]["落地情况"] == "dropped", "dropped 项单列为 dropped")
        check(by_id["act-001"]["逾期"] == "", "dropped 项不标逾期")
        check(by_id["act-002"]["逾期"] == "", "核验轮次为空时不标逾期")
        check("没写核验证据" in by_id["act-002"]["理由"], "空证据理由点明硬纪律")
        # 正交核心：landed 与 逾期=是 同时成立
        check(by_id["act-008"]["落地情况"] == "landed" and by_id["act-008"]["逾期"] == "是",
              "landed 与 逾期=是 同时成立（没人核收也要可见）")
        check("act-008" in a["landed_but_overdue"], "'已落地但逾期未收口'单列含 act-008")
        check("act-005" not in a["landed_but_overdue"], "未落地的逾期项不进'已落地但逾期'小节")
        check(all(it["落地情况"] != "overdue" for it in a["items"]),
              "落地情况 不再含 overdue（逾期是独立列，两列正交）")

    # 只有 landed + dropped → 退出码 0（landed 且未到核验时点不算问题）
    build(cards=1, evidence_file="OK")
    write_actions([
        {"行动项ID": "act-001", "来源轮次": "1", "提出日期": today(), "类别": "其他",
         "行动项": "已落地", "责任人": "PM", "状态": "open",
         "核验证据": "project-state/证据.txt", "核验轮次": ""},
        {"行动项ID": "act-002", "来源轮次": "1", "提出日期": today(), "类别": "其他",
         "行动项": "放弃", "责任人": "PM", "状态": "dropped", "核验证据": "", "核验轮次": ""},
    ])
    a2, p2 = rc_json("--actions", str(ROOT))
    check(p2.returncode == 0, f"无问题 → 退出码 0（实际 {p2.returncode}）")
    if a2:
        check(a2["fail_count"] == 0, "无问题时 fail_count=0")
        check(a2["landed_but_overdue"] == [], "无'已落地但逾期'项时小节为空")

    # ---------------- D 路径::关键字 断言 ----------------
    print("\n测试 D：--actions 的 路径::关键字 断言（文件在但关键字不在 → evidence-missing）")
    if a:
        by_id = {x["行动项ID"]: x for x in a["items"]}
        check(by_id["act-006"]["落地情况"] == "landed", "关键字命中 → landed")
        check(by_id["act-007"]["落地情况"] == "evidence-missing", "文件在但关键字不在 → evidence-missing")
        check("关键内容不在" in by_id["act-007"]["理由"], "理由写明'证据在但关键内容不在'")

    # ---------------- E --actions 写回 行动项.csv（两列都写） ----------------
    print("\n测试 E：--actions 写回 行动项.csv（两列真变 + 其他列不被破坏）")
    build(cards=1, evidence_file="OK")
    write_actions([
        {"行动项ID": "act-001", "来源轮次": "1", "提出日期": today(), "类别": "工具改进",
         "行动项": "带,逗号的项", "责任人": "PM", "状态": "open",
         "核验证据": "project-state/证据.txt", "核验轮次": ""},
        {"行动项ID": "act-002", "来源轮次": "1", "提出日期": today(), "类别": "文档补齐",
         "行动项": "空证据项", "责任人": "架构师", "状态": "open", "核验证据": "", "核验轮次": ""},
        {"行动项ID": "act-003", "来源轮次": "1", "提出日期": today(), "类别": "文档补齐",
         "行动项": "已落地但逾期项", "责任人": "架构师", "状态": "open",
         "核验证据": "project-state/证据.txt", "核验轮次": "1"},
    ])
    ap = ROOT / "project-state" / "复盘" / "行动项.csv"
    before = ap.read_text(encoding="utf-8")
    rc("--actions", str(ROOT))
    after = ap.read_text(encoding="utf-8")
    check(after != before, "写回后文件内容确实变了")
    rows = read_rows(ap)
    by_id = {r["行动项ID"]: r for r in rows}
    check(by_id["act-001"]["落地情况"] == "landed", "写回 landed")
    check(by_id["act-002"]["落地情况"] == "unverified", "写回 unverified")
    check(by_id["act-003"]["落地情况"] == "landed" and by_id["act-003"]["逾期"] == "是",
          "两列都写回：act-003 落地情况=landed 且 逾期=是")
    check(by_id["act-001"]["逾期"] == "", "未到核验时点的行 逾期 留空")
    check(by_id["act-001"]["行动项"] == "带,逗号的项", "含逗号的行写入后未被切坏")
    check(by_id["act-001"]["类别"] == "工具改进" and by_id["act-002"]["责任人"] == "架构师",
          "其余列保持不动")

    # ---------------- F --close 幂等 + 未传结论保留旧值 ----------------
    print("\n测试 F：--close 幂等（同日不重复） + 未传 --结论 保留旧值")
    build(cards=2, evidence_file="OK")
    write_actions([
        {"行动项ID": "act-001", "来源轮次": "1", "提出日期": today(), "类别": "其他",
         "行动项": "已落地", "责任人": "PM", "状态": "open",
         "核验证据": "project-state/证据.txt", "核验轮次": ""},
        {"行动项ID": "act-002", "来源轮次": "1", "提出日期": today(), "类别": "其他",
         "行动项": "已落地但逾期未收口", "责任人": "PM", "状态": "open",
         "核验证据": "project-state/证据.txt", "核验轮次": "1"},   # landed + 逾期，不应影响落地数
    ])
    rc("--actions", str(ROOT))          # 先跑一次把落地情况写进 CSV，--close 才能数 landed
    r1, p1 = rc_json("--close", str(ROOT), "--结论", "第一版", "--milestone", "M1")
    check(p1.returncode == 0 and r1 is not None, "--close 退出码 0", (p1.stderr or "")[-200:])
    check(r1 and r1["updated_existing"] is False, "首次 --close 是新开一轮")
    check(r1 and r1["landed_total"] == 2, f"已落地数=2，逾期不影响落地率（实际 {r1 and r1['landed_total']}）")
    # 第二次：不带 --结论、只带 --milestone → 结论必须保留
    r2, p2 = rc_json("--close", str(ROOT), "--milestone", "M2")
    check(r2 and r2["updated_existing"] is True, "同日第二次 --close 更新已有行")
    check(r2 and r2["conclusion"] == "第一版", f"未传 --结论 保留旧结论（实际 {r2 and r2['conclusion']!r}）")
    check(r2 and r2["milestone"] == "M2", "传了 --milestone 则更新")
    check(r1 and r2 and r1["round"] == r2["round"] == 1, "轮次仍是 1（未产生轮次 2）")
    led = read_rows(ROOT / "project-state" / "复盘" / "复盘台账.csv")
    check(len(led) == 1, f"台账只有 1 个数据行（实际 {len(led)}）")
    check(len([x for x in led if x["轮次"] == "1"]) == 1, "轮次 1 只有一行")
    check(led and led[0]["结论摘要"] == "第一版", "当天那一行的旧结论未被清空")
    check(led and led[0]["行动项数"] == "2" and led[0]["已落地数"] == "2",
          "行动项数/已落地数已写入，且逾期项仍计入已落地数")
    check(led and led[0]["迭代标记"] == "2", "迭代标记=当前收口卡数 2")

    # ---------------- G --assemble 部分来源缺失 ----------------
    print("\n测试 G：--assemble 来源部分缺失 → 明确'未查过'，不当作'没有异常'")
    build(cards=1, burn_csv=[{"时间戳": "2026-09-11 10:00:00", "卡": "card-001", "角色": "后端开发",
                              "模型": "m1", "轮次": "1", "prompt_tokens": "20000",
                              "completion_tokens": "3000", "累计token": "23000",
                              "累计成本": "", "备注": ""}],
          roles=["后端开发"], est_csv=None)
    a, p = rc_json("--assemble", str(ROOT))
    check(a is not None, "--assemble --json 可解析", (p.stdout or p.stderr)[:300])
    if a:
        check("进度/估算实测.csv" in a["missing_sources"], "缺 估算实测.csv 被列入缺失来源")
        check(a["estimate_by_type"] == {}, "缺失来源的估算分档为空——**没有数据 ≠ 估得准**")
        check(any("未查过" in n for n in a["notes"]), "notes 明确写'未查过'")
        check(any(r["role"] == "后端开发" for r in a["roles"]), "仍汇总可得的成本来源")
    ptxt = rc("--assemble", str(ROOT))
    check("未查过" in ptxt.stdout, "文本输出含'未查过'")
    check("未查过（文件/目录不存在）" in ptxt.stdout, "来源表把缺失项显式标为'未查过'（未当成已查）")

    build(cards=1, make_progress=False)
    a3, p3 = rc_json("--assemble", str(ROOT))
    check(p3.returncode == 0 and a3 is not None, "三来源全缺时不崩（退出码 0）", (p3.stderr or "")[-200:])
    if a3:
        check(len(a3["missing_sources"]) == 3, f"三来源全缺（实际 {a3['missing_sources']}）")

    # ---------------- H 配置缺省 ----------------
    print("\n测试 H：retro_cadence 缺失 → 用默认值 20/30")
    build(cards=1, config={})
    d, _ = rc_json("--due", str(ROOT))
    if d:
        check(d["every_n_cards"] == 20, f"无配置项时 every_n_cards=20（实际 {d['every_n_cards']}）")
        check(d["stale_days"] == 30, f"无配置项时 stale_days=30（实际 {d['stale_days']}）")
    build(cards=1, config="__unset__")
    d2, p2 = rc_json("--due", str(ROOT))
    check(p2.returncode == 0, "无 config.json 也能跑（退出码 0）", (p2.stderr or "")[-200:])
    check(d2 is not None and d2["every_n_cards"] == 20 and d2["stale_days"] == 30,
          "无 config.json 时同样回落默认值")

    # ---------------- I GBK 控制台 ----------------
    print("\n测试 I：Windows GBK 控制台下各模式不崩、无 GBK 不安全符号")
    build(cards=30, config=cfg20, evidence_file="OK",
          burn_csv=[{"时间戳": "2026-09-11 10:00:00", "卡": "card-001", "角色": "后端开发",
                     "模型": "m1", "轮次": "1", "prompt_tokens": "20000",
                     "completion_tokens": "3000", "累计token": "23000", "累计成本": "", "备注": ""}],
          roles=["后端开发"])
    write_actions([
        {"行动项ID": "act-001", "来源轮次": "1", "提出日期": today(), "类别": "其他",
         "行动项": "空证据项", "责任人": "PM", "状态": "open", "核验证据": "", "核验轮次": ""},
    ])
    env_gbk = dict(os.environ, PYTHONIOENCODING="gbk")
    for label, argv, ok_codes in (
            ("--due", ["--due", str(ROOT)], (0,)),
            ("--assemble", ["--assemble", str(ROOT)], (0,)),
            ("--actions", ["--actions", str(ROOT)], (0, 1)),   # 有未落地 → 1 是正确行为
            ("--close", ["--close", str(ROOT)], (0,))):
        pl = rc(*argv, env=env_gbk)
        check("UnicodeEncodeError" not in (pl.stderr or ""), f"{label} stderr 无 UnicodeEncodeError")
        check(pl.returncode in ok_codes, f"{label} GBK 退出码正常（实际 {pl.returncode}）",
              (pl.stderr or "")[-300:])
        check("✅" not in pl.stdout and "⚠" not in pl.stdout, f"{label} 输出无 GBK 不安全符号")

    # ---------------- J 输入错误退出码 2 ----------------
    print("\n测试 J：输入错误 → 退出码 2")
    empty = ROOT.parent / (ROOT.name + "_empty")
    if empty.exists():
        shutil.rmtree(empty, ignore_errors=True)
    empty.mkdir(parents=True)
    p = rc("--due", str(empty))
    check(p.returncode == 2, f"非框架工作区 → 退出码 2（实际 {p.returncode}）")
    build(cards=1)
    p = rc("--record-action", str(ROOT), "--类别", "流程优化")   # 缺 行动项/责任人
    check(p.returncode == 2, f"缺必需参数 → 退出码 2（实际 {p.returncode}）")
    p = rc("--record-action", str(ROOT), "--类别", "乱写类别", "--行动项", "x", "--责任人", "y")
    check(p.returncode == 2, f"类别非法 → 退出码 2（实际 {p.returncode}）")
    shutil.rmtree(empty, ignore_errors=True)

    # ---------------- K 真口径账本（11 列估算实测 / 10 列成本流水） ----------------
    # 真账本列名与旧账本不同（角色 / 估算M(小时) / 实际(小时)；tokens_in/out + 当轮 估算成本）。
    # 若按位置硬套旧列名，`--assemble` 会把角色读成类型、把 token 全读成不可解析、成本漏光：
    # 这一节把「真口径也认、旧口径照旧」钉住。
    print("\n测试 K：--assemble 认真口径账本（按表头名定位，不是按位置猜）")
    build(cards=1, roles=[])
    ps = ROOT / "project-state"
    (ps / "进度" / "估算实测.csv").write_text(
        "卡号,角色,估算O(小时),估算M(小时),估算P(小时),P(窗口),实际(小时),偏差,裁尾,登记时间,证据\n"
        "card-037,集成工程师,3.75,7.08,13.83,-,1.05,-6.03,-,2026-10-08 13:13,证据::1\n"
        "card-038,集成工程师,1,2,4,0.8,3.5,1.5,-,2026-10-08 14:00,证据::2\n",
        encoding="utf-8")
    (ps / "进度" / "成本流水.csv").write_text(
        "时间,卡号,角色,模型,tokens_in,tokens_out,估算成本,币种,依据,登记时间\n"
        "2026-10-08 14:00,card-037,集成工程师,deepseek-v4,12000,800,未查过,未查过,job-a,2026-10-08 14:01\n"
        "2026-10-08 14:10,card-037,集成工程师,deepseek-v4,30000,2000,0.0123,CNY,job-b,2026-10-08 14:11\n",
        encoding="utf-8")
    ka, kp = rc_json("--assemble", str(ROOT))
    check(ka is not None, "真口径账本 --assemble --json 可解析", (kp.stdout or kp.stderr)[:300])
    if ka:
        est = ka["estimate_by_type"].get("集成工程师", {})
        check(est.get("n") == 2, f"按「角色」列分档（实际桶 {list(ka['estimate_by_type'])}）")
        check(ka["estimate_rows"] == 2, f"2 行实测都读到（实际 {ka['estimate_rows']}）")
        # 1.05/7.08=0.148、3.5/2=1.75 → 中位数 0.949：只断言「有值且量级合理」，
        # 即**没有把小时当分钟读**（那会得到 63/7.08 之类的荒谬值）
        check(est.get("median_ratio") is not None and 0.1 < est["median_ratio"] < 2.0,
              f"比值量级合理、没把小时当分钟（实际 {est.get('median_ratio')}）")
        check(est.get("cards") == ["card-037", "card-038"], f"卡号逐行带上（实际 {est.get('cards')}）")
        burn = ka["burn_by_role"].get("集成工程师", {})
        check(burn.get("rounds") == 2, f"成本流水 2 轮（实际 {burn.get('rounds')}）")
        check(burn.get("tokens") == 44800,
              f"`tokens_in/out` 逐行相加=44800（实际 {burn.get('tokens')}）")
        check(abs((burn.get("cost") or 0) - 0.0123) < 1e-9,
              f"`估算成本` 是当轮值：`未查过` 不计、有值的相加（实际 {burn.get('cost')}）")
        check(ka["burn_bad_rows"] == 0, f"真口径不是「token 不可解析」（实际 {ka['burn_bad_rows']}）")

    # 真口径成本列全 `未查过` + 无 cost_table → 留空不猜价
    build(cards=1)
    (ps / "进度" / "成本流水.csv").write_text(
        "时间,卡号,角色,模型,tokens_in,tokens_out,估算成本,币种,依据,登记时间\n"
        "2026-10-08 14:00,card-037,集成工程师,m1,1000,100,未查过,未查过,job-a,2026-10-08 14:01\n",
        encoding="utf-8")
    a4, _ = rc_json("--assemble", str(ROOT))
    if a4:
        check(a4["burn_by_role"]["集成工程师"]["cost"] is None, "全 `未查过` ⇒ 成本留空（不猜价）")
        check(any("未查过" in n and "cost_table" in n for n in a4["notes"]),
              "notes 说明为什么不写成本（留空 = 不猜价）")

    # V-02 定案：`未查过` 是正式记法 ⇒ 即使 config.json 里有 cost_table 也不得反算成本
    build(cards=1, config={"cost_table": {"m1": {"prompt": 0.001, "completion": 0.002}}})
    (ps / "进度" / "成本流水.csv").write_text(
        "时间,卡号,角色,模型,tokens_in,tokens_out,估算成本,币种,依据,登记时间\n"
        "2026-10-08 14:00,card-037,集成工程师,m1,1000,100,未查过,未查过,job-a,2026-10-08 14:01\n"
        "2026-10-08 14:05,card-037,集成工程师,m1,2000,800,未查过,未查过,job-b,2026-10-08 14:06\n",
        encoding="utf-8")
    a4b, _ = rc_json("--assemble", str(ROOT))
    if a4b:
        check(a4b["burn_by_role"]["集成工程师"]["cost"] is None,
              "有 cost_table 时 `未查过` 仍留空（不替操作者反算）")
        check(a4b.get("cost_source") is None, f"cost_source 不谎报记录值（实际 {a4b.get('cost_source')}）")
        check(any("不因" in n and "cost_table" in n for n in a4b["notes"]),
              "notes 明写「不因 config 里有 cost_table 就反算」")
        check(a4b["burn_by_role"]["集成工程师"]["tokens"] == 3900,
              f"成本不猜但 token 仍逐行累加（实际 {a4b['burn_by_role']['集成工程师']['tokens']}）")

    # cost_table 只在账本**完全没有成本列**时兜底，且必须标 cost_source=cost_table（估算值≠记录值）
    build(cards=1, config={"cost_table": {"m1": {"prompt": 0.001, "completion": 0.002}}})
    (ps / "进度" / "成本流水.csv").write_text(
        "时间,卡号,角色,模型,tokens_in,tokens_out,依据,登记时间\n"
        "2026-10-08 15:00,card-037,集成工程师,m1,1000,100,job-c,2026-10-08 15:01\n",
        encoding="utf-8")
    a4c, _ = rc_json("--assemble", str(ROOT))
    if a4c:
        c4 = a4c["burn_by_role"]["集成工程师"]
        check(c4["tokens"] == 1100, f"无成本列时 token 仍逐行相加（实际 {c4['tokens']}）")
        check(abs((c4["cost"] or 0) - 0.0012) < 1e-9,
              f"无成本列 ⇒ 按 cost_table 兜底计价 1000*0.001+100*0.002=0.0012（实际 {c4['cost']}）")
        check(a4c.get("cost_source") == "cost_table",
              f"兜底计价必须标 cost_source（实际 {a4c.get('cost_source')}）")
        check(any("估算值" in n for n in a4c["notes"]), "notes 明写这是估算值不是记录值")

    # 旧 10 列（累计成本）仍按逐行差分还原，未被真口径改写带偏
    build(cards=1)
    write_rows(ps / "进度" / "成本流水.csv", BURN_FIELDS,
               [{"时间戳": "2026-09-11 10:00:00", "卡": "card-001", "角色": "后端开发", "模型": "m1",
                 "轮次": "1", "prompt_tokens": "20000", "completion_tokens": "3000",
                 "累计token": "23000", "累计成本": "0.010", "备注": ""},
                {"时间戳": "2026-09-11 11:00:00", "卡": "card-001", "角色": "后端开发", "模型": "m1",
                 "轮次": "2", "prompt_tokens": "10000", "completion_tokens": "1000",
                 "累计token": "34000", "累计成本": "0.025", "备注": ""}])
    a5, _ = rc_json("--assemble", str(ROOT))
    if a5:
        b5 = a5["burn_by_role"]["后端开发"]
        check(b5["rounds"] == 2 and b5["tokens"] == 34000,
              f"旧口径 token 逐行相加=34000（实际 {b5['tokens']}）")
        check(abs((b5["cost"] or 0) - 0.025) < 1e-9,
              f"旧口径累计列逐行差分=0.010+0.015=0.025（实际 {b5['cost']}）")

    print("\n" + "=" * 72)
    print(f"结果：{len(passes)} 通过 / {len(fails)} 失败")
    for x in fails:
        print(f"  FAIL -> {x}")
    print("=" * 72)
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        os.chdir(HERE)
        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(code)
