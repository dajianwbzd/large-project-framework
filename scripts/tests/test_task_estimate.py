#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_estimate.py 的回归测试（v4.5.0 补）。

为什么这个测试必须存在：本脚本产出的是**夜间无人值守编排的输入**。它对外承诺的
「不可逆动作不进无人序列」「人工依赖不进无人序列」「超预算即熔断」是**安全承诺**——
承诺一旦与代码脱钩，代价是整夜烧掉额度、或早上起来发现发出去了一份不该发的东西。
所以每条承诺都有一个会变红的断言。

覆盖：
- 无人值守：`不可逆` 卡被排除、`待人工` 卡被排除（各一条断言）。
- 人工依赖：依赖 `待办/自然人.md` 未闭环项的卡被排除，且原因**点名**该文件。
- 依赖序：选中序列满足拓扑序（被依赖卡在前）——排不出去的估算等于没估。
- 预算熔断：超 `--budget-tokens` 置 budget_stop，不静默烧完。
- 实测校正：同类实测 n>=3 时置信度升到「高」并应用偏差系数。
- 时间窗分档：0.5h→极短窗/短优先；9h→长时窗/长优先且自动进无人值守模式。
- Windows GBK 控制台不崩（errors=replace）。
- `--write-plan` 幂等（标记段只替换不清空文件）。

v4.6.0 补（安全承诺从「静态排除」扩到「动态熔断」，所以断言也要跟上）：
- 三点估算数学：BetaPERT 均值==(O+4M+P)/6、σ 用**精确方差**（>教科书 (P-O)/6），
  并用 2 万次蒙特卡洛把解析 σ 与序列命中概率钉住——公式一改就变红。
- 命中概率不足 → **裁尾**（宁可少排不虚报），被裁项进 spill 且写明原因。
- 可实施性关卡：AC 空/契约未就绪/依赖悬空 → FAIL（退出码 1）；缺字段 → 记 undeclared
  但**不等于 FAIL**（未声明 ≠ 查过且干净）；低置信度发现**聚合成一条**不许逐卡刷行。
- 事中成本流水：无 cost_table 时**不猜价**、有表时按表算、超预算置 breaker、
  累计值**逐行重算**（手改累计列也不采信）。
- 真口径台账（v5.3.0 / F-06）：`估算实测.csv` 11 列与 `成本流水.csv` 10 列**一律按表头名定位**
  （不许按位置猜），旧 6/10 列账本继续按自身表头可读可写，缺项写 `-`。

用法：python scripts/tests/test_task_estimate.py
退出码 0=全过；1=有失败。
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parents[1]
SCRIPT = Path(os.environ.get("TE_SCRIPT") or (FRAMEWORK / "scripts" / "task_estimate.py"))
ROOT = Path(tempfile.mkdtemp(prefix="te_test_"))

fails: list = []
passes: list = []


def _load_module():
    """把被测脚本当模块导进来，直接调 pert/seq_prob/estimate 做数学对拍。

    走文件导入而不是 import 名，是为了让 TE_SCRIPT 环境变量（指向别处的副本）依然有效。
    """
    try:
        spec = importlib.util.spec_from_file_location("te_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def check(cond, label, detail=""):
    (passes if cond else fails).append(label)
    tail = f"  -- {detail}" if detail and not cond else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{tail}")


def build(natural_person_rows: str = ""):
    """搭一个最小工作区：只放本脚本要读的三样东西（台账/待办/工作计划）。"""
    if ROOT.exists():
        shutil.rmtree(ROOT, ignore_errors=True)
    (ROOT / "project-state" / "进度").mkdir(parents=True)
    (ROOT / "project-state" / "待办").mkdir(parents=True)
    (ROOT / "project-state" / "待办" / "自然人.md").write_text(
        "# 待办/自然人.md\n\n| 事项 | 为何要自然人 | 状态 | 回执 |\n|---|---|---|---|\n"
        + (natural_person_rows or ""), encoding="utf-8")


def write_cands(items, name="cands.json"):
    p = ROOT / name
    p.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return p


def write_cards(kind, states):
    """建卡卷宗：kind='card' → 需求卡/card-NNN/card.md；kind='tc' → 测试卡/tc-NNN/tc.md。

    status 行**故意带行内注释**——真实模板里就是 `status: todo   # todo|in_progress|...`，
    解析器必须剥掉注释，否则会把整串注释当成状态值（那会让"全收口"永远判不成立）。
    """
    sub = "需求卡" if kind == "card" else "测试卡"
    fname = "card.md" if kind == "card" else "tc.md"
    base = ROOT / "project-state" / sub
    for cid, status in states.items():
        d = base / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / fname).write_text(
            f"---\nid: {cid}\ntitle: 测试卡标题\nstatus: {status}"
            "   # todo(未开始) | in_progress | done | reviewing | blocked\n---\n\n正文\n",
            encoding="utf-8")
    return base


def write_contract(payload):
    p = ROOT / "project-state" / "跑批契约.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def guard_json(*extra):
    p = run("--guard", *extra)
    try:
        return json.loads(p.stdout), p
    except ValueError:
        return None, p


def cond(gj, key):
    """按条件 key 取闸门状态（time / token / cards / test_gate）。"""
    for x in (gj or {}).get("conditions", []):
        if x["key"] == key:
            return x
    return {}


def run(*extra, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(ROOT), *extra],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def plan_json(cands_path, window, *extra):
    p = run("--plan", str(cands_path), "--window", str(window), "--json", *extra)
    try:
        return json.loads(p.stdout), p
    except ValueError:
        return None, p


CANDS = [
    {"id": "card-011", "名称": "登录接口", "类型": "后端", "优先级": "P0",
     "文件数": 3, "净增行数": 180, "跨角色协商": True},
    {"id": "card-012", "名称": "登录页", "类型": "前端", "优先级": "P0",
     "文件数": 4, "净增行数": 260, "ui": True, "依赖": ["card-011"]},
    {"id": "card-014", "名称": "等用户定色", "类型": "前端", "优先级": "P1",
     "文件数": 2, "净增行数": 80, "待人工": True},
    {"id": "card-015", "名称": "对外发布包", "类型": "部署", "优先级": "P0",
     "文件数": 2, "净增行数": 40, "不可逆": True},
    {"id": "card-017", "名称": "依赖用户拍的卡", "类型": "后端", "优先级": "P0",
     "文件数": 2, "净增行数": 60, "依赖": ["card-099"]},
]


def main() -> int:
    print("=" * 72)
    print(f"被测脚本：{SCRIPT}")
    print(f"沙箱：{ROOT}")
    print("=" * 72)

    # ---------------- A 无人值守的硬排除 ----------------
    print("\n测试 A：无人值守模式下的排除（不可逆 / 需人工 / 人工依赖）")
    build("| card-099 用户确认配色 | 只有用户能定 | 待用户答复 | |\n")
    f = write_cands(CANDS)
    d, p = plan_json(f, 9)
    check(d is not None, "9 小时窗 --json 可解析", (p.stdout or p.stderr)[:300])
    if d:
        check(d["unattended"] is True, "9 小时窗自动进无人值守模式")
        check(d["window_tier"] == "长时窗", f"分档=长时窗（实际 {d['window_tier']}）")
        ids = [x["id"] for x in d["selected"]]
        exc = {x["id"]: x["reason"] for x in d["excluded"] + d["spill"]}
        check("card-015" not in ids, "不可逆/对外动作卡被排除（无人值守不授权）")
        check("不可逆" in exc.get("card-015", ""), f"排除原因说明不可逆（实际 {exc.get('card-015')!r}）")
        check("card-014" not in ids, "需人工拍板的卡被排除")
        check("人工" in exc.get("card-014", ""), f"排除原因说明人工依赖（实际 {exc.get('card-014')!r}）")
        check("card-017" not in ids, "依赖未闭环人工项的卡被排除")
        check("待办/自然人.md" in exc.get("card-017", ""),
              f"排除原因点名 待办/自然人.md（实际 {exc.get('card-017')!r}）")

    # ---------------- B 依赖序 ----------------
    print("\n测试 B：选中序列满足拓扑序（被依赖卡在前）")
    build()
    d, p = plan_json(write_cands(CANDS), 9)
    if d:
        ids = [x["id"] for x in d["selected"]]
        check("card-011" in ids and "card-012" in ids, f"两卡都被选中（实际 {ids}）")
        check(ids.index("card-011") < ids.index("card-012"),
              f"card-011 排在 card-012 之前（实际 {ids}）")
        check(all(x["basis"] for x in d["selected"]), "每项估算都带依据（不许无依据结论）")
        check(all(x["confidence"] in ("高", "中", "低") for x in d["selected"]), "每项都标了置信度")

    print("\n测试 B2：--done 满足依赖后放行")
    build()
    d2, p2 = plan_json(write_cands(CANDS), 9, "--done", "card-011")
    if d2:
        ids2 = [x["id"] for x in d2["selected"]]
        check("card-011" not in ids2, "已完成的卡不再排入")
        check("card-012" in ids2, "依赖被 --done 满足后放行")

    # ---------------- C 预算熔断 ----------------
    print("\n测试 C：token 预算熔断")
    build()
    d3, p3 = plan_json(write_cands(CANDS), 9, "--budget-tokens", "1000")
    if d3:
        check(d3["budget_stop"] is True, "超预算置 budget_stop（不静默烧完）")
        check(any("熔断" in x["reason"] for x in d3["spill"]),
              f"溢出项原因含熔断（实际 {[x['reason'] for x in d3['spill']][:2]}）")
        check(not d3["selected"], "预算过小时不排任何卡")

    # ---------------- D 时间窗分档 ----------------
    print("\n测试 D：时间窗分档与取向")
    build()
    d4, _ = plan_json(write_cands(CANDS), 0.5)
    if d4:
        check(d4["window_tier"] == "极短窗", f"0.5h→极短窗（实际 {d4['window_tier']}）")
        check(d4["order"] == "short", f"极短窗取向=短优先（实际 {d4['order']}）")

    # ---------------- E 实测校正闭环 ----------------
    print("\n测试 E：--record 回填 → --calibrate 可用 → 估算改用实测（置信度升高）")
    build()
    rec = run("--record", "--card", "card-011", "--type", "后端", "--est-min", "100",
              "--act-min", "150", "--est-tokens", "100000", "--act-tokens", "150000")
    check(rec.returncode == 0, "--record 退出码 0", (rec.stderr or "")[-300:])
    for cid in ("card-002", "card-003"):
        run("--record", "--card", cid, "--type", "后端", "--est-min", "100",
            "--act-min", "150", "--est-tokens", "100000", "--act-tokens", "150000")
    cal = run("--calibrate", "--json")
    try:
        cj = json.loads(cal.stdout)
    except ValueError:
        cj = None
    check(cj is not None, "--calibrate --json 可解析", (cal.stdout or "")[:200])
    if cj:
        info = cj["types"].get("后端") or {}
        check(info.get("n") == 3, f"后端样本 n=3（实际 {info.get('n')}）")
        check(info.get("usable") is True, "n>=3 标记为可用")
        check(abs((info.get("time_factor") or 0) - 1.5) < 0.01,
              f"估时偏差系数=1.5（实际 {info.get('time_factor')}）")
        check("后端" in cj["usable_types"], "后端进入可用于估算的类型")

    d5, _ = plan_json(write_cands(CANDS), 9)
    if d5:
        est = {x["id"]: x for x in d5["selected"]}
        e11 = est.get("card-011")
        check(e11 is not None and e11["confidence"] == "高",
              f"有实测的卡置信度=高（实际 {e11 and e11['confidence']}）")
        check(e11 is not None and any("实测校正" in b for b in e11["basis"]),
              "依据里写明用的是实测校正（可复核）")

    # ---------------- F GBK 控制台 ----------------
    print("\n测试 F：Windows GBK 控制台不崩")
    build()
    env = dict(os.environ, PYTHONIOENCODING="gbk")
    f = write_cands(CANDS)
    p = run("--plan", str(f), "--window", "9", env=env)
    check(p.returncode == 0, "GBK 环境退出码 0", (p.stderr or "")[-500:])
    check("UnicodeEncodeError" not in (p.stderr or ""), "stderr 无 UnicodeEncodeError")
    check("✅" not in p.stdout and "⚠" not in p.stdout, "输出无 GBK 不安全符号")

    # ---------------- G --write-plan 幂等 ----------------
    print("\n测试 G：--write-plan 写入计划文件且幂等")
    build()
    (ROOT / "project-state" / "主控工作计划.md").write_text(
        "# 主控工作计划\n\n## 里程碑\n- M1 已定\n", encoding="utf-8")
    f = write_cands(CANDS)
    for _ in range(2):
        run("--plan", str(f), "--window", "9", "--write-plan")
    plan_md = (ROOT / "project-state" / "主控工作计划.md").read_text(encoding="utf-8")
    check(plan_md.count("<!-- task_estimate:begin -->") == 1, "标记段只有一份（幂等）")
    check("## 里程碑" in plan_md, "原有内容未被覆盖")
    check("长时任务估算" in plan_md, "估算段已写入")

    # ---------------- H 三点估算的数学正确性（解析解 vs 蒙特卡洛）----------------
    print("\n测试 H：BetaPERT 精确方差 / 命中概率（蒙特卡洛对拍）")
    # 为什么必须对拍：估时唯一的用途是**选卡与熔断**，而 P(跑完) 直接决定裁不裁尾。
    # 用教科书的 σ=(P-O)/6 会低估方差（对称三点：0.1667 vs 0.1889，差 13%），
    # 从而把「大概率跑不完」报成「大概率跑得完」——这正是整夜翻车的机制。
    # 所以这里用 2 万次采样把解析公式钉住，公式一改就会变红。
    mod = _load_module()
    if mod is None:
        check(False, "可导入被测模块（取 pert/seq_prob 做对拍）")
    else:
        mean, sd, a, b = mod.pert(60.0, 120.0, 240.0)
        span = 240.0 - 60.0
        check(abs(mean - (60.0 + 4 * 120.0 + 240.0) / 6.0) < 1e-9,
              f"BetaPERT 均值 == 教科书 (O+4M+P)/6（实际 {mean:.6f}）")
        # 对称三点（M 正好是中点）才有闭式 σ/跨度 = 1/√28；非对称时 α≠β，比例不同。
        _m2, sd2, a2, b2 = mod.pert(60.0, 150.0, 240.0)
        check(abs(sd2 / 180.0 - 0.1889822) < 1e-6,
              f"对称三点 σ/跨度 == 1/√28 ≈ 0.18898（实际 {sd2 / 180.0:.6f}）")
        check(sd2 > (240.0 - 60.0) / 6.0,
              f"精确 σ 大于教科书 (P-O)/6（{sd2:.3f} > {(240.0 - 60.0) / 6.0:.3f}）——不许用近似式")
        # 正态近似：Φ(0)=0.5、Φ(1.96)=0.975
        check(abs(mod.seq_prob(100.0, 100.0, 10.0) - 0.5) < 1e-6, "P(窗口=期望)=50%")
        check(abs(mod.seq_prob(119.6, 100.0, 10.0) - 0.975) < 1e-3, "P(z=1.96)≈97.5%")

        build()
        ests = [mod.estimate(c, mod.calibrate(ROOT)) for c in CANDS[:2]]
        rng = random.Random(20260911)
        N = 20000
        for e in ests:
            pe = e["pert"]
            draws = [pe["o"] + (pe["p"] - pe["o"]) * rng.betavariate(pe["alpha"], pe["beta"])
                     for _ in range(N)]
            m_sim = statistics.fmean(draws)
            sd_sim = statistics.pstdev(draws)
            check(abs(m_sim - e["est_min"]) / e["est_min"] < 0.02,
                  f"{e['id']} 蒙特卡洛均值≈解析期望（{m_sim:.2f} vs {e['est_min']:.2f}）")
            check(abs(sd_sim - e["sigma_min"]) / e["sigma_min"] < 0.02,
                  f"{e['id']} 蒙特卡洛 σ≈解析 σ（{sd_sim:.2f} vs {e['sigma_min']:.2f}）")
        tot_mean = sum(e["est_min"] for e in ests)
        tot_sd = math.sqrt(sum(e["sigma_min"] ** 2 for e in ests))
        window = tot_mean * 1.10
        sim_tot = [sum(pe["o"] + (pe["p"] - pe["o"]) * rng.betavariate(pe["alpha"], pe["beta"])
                       for pe in [e["pert"] for e in ests]) for _ in range(N)]
        p_sim = sum(1 for x in sim_tot if x <= window) / N
        p_ana = mod.seq_prob(window, tot_mean, tot_sd)
        check(abs(p_sim - p_ana) < 0.03,
              f"序列命中概率对拍一致（模拟 {p_sim:.3f} vs 解析 {p_ana:.3f}）")

    # ---------------- I 命中概率不足 → 裁尾 ----------------
    print("\n测试 I：P 低于下限即裁尾（宁可少排也不虚报）")
    build()
    f = write_cands(CANDS)
    d, p = plan_json(f, 9, "--p-min", "0.999")
    check(d is not None, "--p-min 0.999 可出计划", (p.stdout or p.stderr)[:300])
    if d:
        check(d["p_min"] == 0.999, f"下限被采纳（实际 {d['p_min']}）")
        check(d["p_finish_before_trim"] < 0.999, "裁尾前确实低于下限（否则这条断言没跑到位）")
        check(len(d["trimmed"]) >= 1, f"发生了裁尾（裁掉 {len(d['trimmed'])} 张）")
        check(d["p_finish"] >= d["p_finish_before_trim"], "裁尾后命中概率不降")
        check(all("裁尾" in x["reason"] for x in d["trimmed"]), "裁尾项写明原因")
        check(all(x["id"] in [y["id"] for y in d["spill"]] for x in d["trimmed"]),
              "裁掉的项进 spill（不静默丢项）")
        check(any("裁" in n for n in d["notes"]), "notes 里披露了裁尾")

    # ---------------- J 可实施性关卡 ----------------
    print("\n测试 J：可实施性关卡（PASS/CONCERNS/FAIL + 未声明 ≠ 已就绪）")
    build()
    f = write_cands([
        {"id": "card-021", "名称": "空心卡", "类型": "后端", "优先级": "P0",
         "文件数": 2, "净增行数": 80, "AC": []},
    ])
    p = run("--readiness", str(f), "--json")
    try:
        rj = json.loads(p.stdout)
    except ValueError:
        rj = None
    check(rj is not None, "--readiness --json 可解析", (p.stdout or p.stderr)[:200])
    check(p.returncode == 1, f"FAIL 时退出码 1（实际 {p.returncode}）")
    if rj:
        check(rj["verdict"] == "FAIL", f"AC 显式留空 → FAIL（实际 {rj['verdict']}）")
        fails_j = [x for x in rj["findings"] if x["severity"] == "FAIL"]
        check(any(x["card"] == "card-021" and "AC" in x["issue"] for x in fails_j),
              "FAIL 点名该卡且说明是 AC 问题")
        check(all(x["fixer"] for x in rj["findings"]), "每条发现都指名修复者（不指名=没发现）")
        check("card-021 · 契约就绪" in rj["undeclared"],
              f"缺 契约就绪 记入 undeclared（实际 {rj['undeclared']}）")

    build()
    f = write_cands([
        {"id": "card-022", "名称": "契约没定", "类型": "前端", "优先级": "P0",
         "文件数": 2, "净增行数": 80, "AC": ["能渲染"], "契约就绪": False},
    ])
    p = run("--readiness", str(f), "--json")
    rj = json.loads(p.stdout) if p.stdout.strip().startswith("{") else None
    if rj:
        check(rj["verdict"] == "FAIL", "契约未就绪 → FAIL")
        check(any(x["card"] == "card-022" and "契约" in x["issue"] for x in rj["findings"]),
              "FAIL 说明是契约未就绪")

    build()
    f = write_cands([
        {"id": "card-023", "名称": "依赖悬空", "类型": "后端", "优先级": "P0",
         "文件数": 2, "净增行数": 80, "AC": ["能跑"], "契约就绪": True,
         "参考类": "card-001", "依赖": ["card-777"]},
    ])
    p = run("--readiness", str(f), "--json")
    rj = json.loads(p.stdout) if p.stdout.strip().startswith("{") else None
    if rj:
        check(rj["verdict"] == "FAIL", "依赖既不在候选也不在 --done → FAIL")
        check(any("card-777" in x["issue"] for x in rj["findings"]), "FAIL 点名悬空依赖卡号")

    # 低置信度+无参考类锚是**一个全局病征**，不许逐卡刷行（噪音报告=没人看的报告）
    build()
    f = write_cands([
        {"id": f"card-03{i}", "名称": f"卡{i}", "类型": "后端", "优先级": "P0",
         "文件数": 2, "净增行数": 60, "AC": ["完"], "契约就绪": True} for i in range(1, 5)
    ])
    p = run("--readiness", str(f), "--json")
    rj = json.loads(p.stdout) if p.stdout.strip().startswith("{") else None
    if rj:
        spray = [x for x in rj["findings"] if "低置信度" in x["issue"]]
        check(len(spray) == 1, f"低置信度发现聚合成 1 条（实际 {len(spray)} 条）")
        check(len(rj["needs_review"]) == 4, f"聚合条列出全部 4 张卡（实际 {rj['needs_review']}）")
        check(rj["verdict"] == "CONCERNS", f"仅缺声明 → CONCERNS 不是 FAIL（实际 {rj['verdict']}）")
        check(not any(x["severity"] == "FAIL" for x in rj["findings"]),
              "「未声明」不产生 FAIL——未声明只是没人查过")

    build()
    for cid in ("card-001", "card-002", "card-003"):
        run("--record", "--card", cid, "--type", "后端", "--est-min", "100",
            "--act-min", "100", "--est-tokens", "100000", "--act-tokens", "100000")
    f = write_cands([
        {"id": "card-024", "名称": "齐备卡", "类型": "后端", "优先级": "P0",
         "文件数": 2, "净增行数": 60, "AC": ["能跑"], "契约就绪": True, "参考类": "card-001"},
    ])
    p = run("--readiness", str(f), "--json")
    rj = json.loads(p.stdout) if p.stdout.strip().startswith("{") else None
    check(p.returncode == 0, f"PASS 时退出码 0（实际 {p.returncode}）")
    if rj:
        check(rj["verdict"] == "PASS", f"声明齐备+有实测台账+无可拍板卡 → PASS（实际 {rj['verdict']}）")

    # ---------------- K 事中成本流水 ----------------
    print("\n测试 K：事中成本流水（累计重算 / 不猜价 / 熔断 / 分桶）")
    build()
    p = run("--spend", "--card", "card-011", "--role", "后端", "--model", "无表模型",
            "--round", "1", "--prompt-tokens", "20000", "--completion-tokens", "3000",
            "--budget-tokens", "100000", "--json")
    sj = json.loads(p.stdout) if p.stdout.strip().startswith("{") else None
    check(sj is not None, "--spend --json 可解析", (p.stdout or p.stderr)[:200])
    if sj:
        check(sj["round_tokens"] == 23000, f"本轮合计 23000（实际 {sj['round_tokens']}）")
        check(sj["cum_cost"] is None, "没有 cost_table → 成本留空（**不猜价**）")
        check(sj["breaker"] is False, "未超预算不熔断")
        check(sj["used_pct"] == 23.0, f"已用 23%（实际 {sj['used_pct']}）")

    cfg = ROOT / "project-state" / "config.json"
    cfg.write_text(json.dumps({"cost_table": {"m1": {"prompt": 0.001, "completion": 0.002}}},
                              ensure_ascii=False), encoding="utf-8")
    p = run("--spend", "--card", "card-011", "--role", "后端", "--model", "m1",
            "--round", "2", "--prompt-tokens", "30000", "--completion-tokens", "4000",
            "--budget-tokens", "100000", "--json")
    sj2 = json.loads(p.stdout) if p.stdout.strip().startswith("{") else None
    if sj2:
        check(abs(sj2["cum_cost"] - 0.038) < 1e-9,
              f"按表计价 30k*0.001+4k*0.002=0.038（实际 {sj2['cum_cost']}）")
        check(sj2["cum_tokens"] == 57000, f"累计 token 跨轮相加（实际 {sj2['cum_tokens']}）")

    p = run("--spend", "--card", "card-012", "--role", "前端", "--model", "m1",
            "--round", "3", "--prompt-tokens", "50000", "--completion-tokens", "5000",
            "--budget-tokens", "100000", "--json")
    sj3 = json.loads(p.stdout) if p.stdout.strip().startswith("{") else None
    if sj3:
        check(sj3["breaker"] is True, "累计超预算 → breaker（不静默烧完）")

    st_p = run("--burn-status", "--budget-tokens", "100000", "--json")
    st = json.loads(st_p.stdout) if st_p.stdout.strip().startswith("{") else None
    check(st is not None, "--burn-status --json 可解析", (st_p.stdout or st_p.stderr)[:200])
    if st:
        check(st["rows"] == 3, f"3 轮记录（实际 {st['rows']}）")
        check(st["cum_tokens"] == 112000, f"累计由各行重算=112000（实际 {st['cum_tokens']}）")
        check(st["by_role"].get("后端", {}).get("tokens") == 57000, "按角色分桶：后端 57000")
        check(st["by_role"].get("前端", {}).get("calls") == 1, "按角色分桶记轮次")
        check(st["by_model"].get("m1", {}).get("tokens") == 112000 - 23000,
              "按模型分桶（无表模型那轮单列）")
        check(st["breaker"] is True, "burn-status 也报熔断态")

    # 台账被手改坏（累计列写错）时，以逐行重算为准——不许长出第二份事实源
    burn = ROOT / "project-state" / "进度" / "成本流水.csv"
    rows = burn.read_text(encoding="utf-8").splitlines()
    rows[-1] = rows[-1].rsplit(",", 2)[0] + ",999999,,手改累计列"
    burn.write_text("\n".join(rows) + "\n", encoding="utf-8")
    st2_p = run("--burn-status", "--json")
    st2 = json.loads(st2_p.stdout) if st2_p.stdout.strip().startswith("{") else None
    if st2:
        check(st2["cum_tokens"] != 999999,
              f"累计列被手改也不采信，逐行重算（实际 {st2['cum_tokens']}）")
        check(st2["cum_tokens"] == 112000, "重算结果仍是真实合计")

    # ---------------- L 新增输出的 GBK 安全性 ----------------
    print("\n测试 L：新增模式在 GBK 控制台下不崩")
    build()
    env_gbk = dict(os.environ, PYTHONIOENCODING="gbk")
    cfg.write_text(json.dumps({"cost_table": {"m1": {"prompt": 0.001, "completion": 0.002}}},
                              ensure_ascii=False), encoding="utf-8")
    f = write_cands(CANDS)
    run("--spend", "--card", "card-011", "--role", "后端", "--model", "m1",
        "--round", "1", "--prompt-tokens", "1000", "--completion-tokens", "100",
        "--budget-tokens", "100000", env=env_gbk)
    for label, argv, ok_codes in (
            # 这份候选里 card-017 依赖一张不存在的卡 → 关卡裁决 FAIL 是**正确行为**，故 1 也算正常；
            # 本条测的是编码不崩，不是裁决档位（档位在测试 J 里断言）。
            ("--readiness", ["--readiness", str(f)], (0, 1)),
            ("--burn-status", ["--burn-status", "--budget-tokens", "100000"], (0,)),
            ("--plan", ["--plan", str(f), "--window", "9"], (0,))):
        pl = run(*argv, env=env_gbk)
        check("UnicodeEncodeError" not in (pl.stderr or ""), f"{label} stderr 无 UnicodeEncodeError")
        check(pl.returncode in ok_codes, f"{label} GBK 退出码正常（实际 {pl.returncode}）",
              (pl.stderr or "")[-300:])
        check("✅" not in pl.stdout and "⚠" not in pl.stdout, f"{label} 输出无 GBK 不安全符号")

    # ---------------- M 跑批契约与停机闸门（v5.2.0） ----------------
    # 用户声明一次跑批的收工条件（起止时刻 / 时长窗 / token 额度 / 需求卡全收口 / 全量测试过），
    # 运行循环每轮问一次「现在该停了吗」。这一节盯的是**判据本身别骗人**：
    # 没有闸门 ≠ 没到收工；没查到 ≠ 已满足；没声明 ≠ 已关闭。
    print("\n测试 M：跑批契约与停机闸门（时间 / token / 需求卡 / 全量测试）")
    build()

    # ① 没有契约 → 不许静默放行（"没有闸门"不能被读成"没到收工"）
    gj, gp = guard_json("--json")
    check(gp.returncode == 2, f"无契约时 --guard 退出码 2（实际 {gp.returncode}）")
    check("跑批契约" in (gp.stderr or ""), "无契约的报错点名契约文件", (gp.stderr or "")[:200])
    check("不是跑批" in (gp.stderr or ""), "报错点出「没有停机条件的跑批不是跑批」")

    # ② 样板可生成；已存在不覆盖——覆盖=悄悄换掉判据
    ci = run("--contract-init")
    check(ci.returncode == 0 and (ROOT / "project-state" / "跑批契约.json").is_file(),
          "--contract-init 生成契约样板", (ci.stderr or "")[:200])
    again = run("--contract-init")
    check(again.returncode == 0 and "未覆盖" in (again.stdout or ""),
          "已有契约时不覆盖（要覆盖得显式 --force）", (again.stdout or "")[-200:])
    check(run("--contract-init", "--force").returncode == 0, "--force 才覆盖")

    # ③ 一条停机条件都没有的契约 → 同样算没有闸门（start_at 是开跑闸门，不是收工条件）
    write_contract({"依据": "用户只说了句「跑起来」", "start_at": "2026-09-16 20:00"})
    gj, gp = guard_json("--now", "2026-09-16 20:30", "--json")
    check(gp.returncode == 2, f"无任何停机条件 → 退出码 2（实际 {gp.returncode}）", (gp.stderr or "")[:200])
    check("一条停机条件都没有" in (gp.stderr or ""), "报错说明为什么不能放行",
          (gp.stderr or "")[:200])

    # ④ 四条条件齐备：窗口内继续跑 / 未到点不动手 / 到点收工
    write_contract({
        "依据": "用户：20:00 开跑，跑到 22:00 或 3 亿 token，需求卡全收口且全量测试过",
        "start_at": "2026-09-16 20:00", "stop_at": "2026-09-16 22:00",
        "token_budget": 300000000, "cards": {"all_done": True, "ids": []},
        "test_gate": "all_done"})
    write_cards("card", {"card-001": "done", "card-002": "in_progress"})
    write_cards("tc", {"tc-001": "in_progress"})

    gj, gp = guard_json("--now", "2026-09-16 19:30", "--json")
    check(gp.returncode == 4 and gj["verdict"] == "NOT_STARTED",
          f"未到开跑时刻 → NOT_STARTED/退出码 4（实际 {gj and gj['verdict']}/{gp.returncode}）")
    check(gj["start_gate"]["reached"] is False, "开跑闸门报未到点")

    gj, gp = guard_json("--now", "2026-09-16 20:30", "--json")
    check(gp.returncode == 0 and gj["verdict"] == "CONTINUE",
          f"窗口内继续跑（实际 {gj and gj['verdict']}/{gp.returncode}）")
    check(gj["declared"] == 4, f"四条条件都被识别（不算 start_at）（实际 {gj['declared']}）")
    check(gj["unknown"] == [], f"数据齐备时不应有「没查到」（实际 {gj['unknown']}）")

    gj, gp = guard_json("--now", "2026-09-16 22:01", "--json")
    check(gp.returncode == 3 and gj["stop_reason"]["key"] == "time",
          f"到点 → 收工、原因=收工时刻（实际 {gp.returncode}/{gj and gj.get('stop_reason')}）")

    # ⑤ token 额度：额度写在契约里（不必每轮重敲，也就没有敲错预算的机会）
    build()
    write_contract({"依据": "小额试跑", "start_at": "2026-09-16 20:00",
                    "stop_at": "2026-09-17 08:00", "token_budget": 1000})
    sp = run("--spend", "--card", "card-001", "--role", "后端", "--round", "1",
             "--prompt-tokens", "900", "--completion-tokens", "200", "--json")
    sj = json.loads(sp.stdout) if sp.stdout.strip().startswith("{") else {}
    check(sj.get("breaker") is True, "契约额度被 --spend 自动取用并触顶", (sp.stdout or sp.stderr)[:200])
    check("跑批契约" in (sj.get("budget_source") or ""),
          f"额度来源标为跑批契约（实际 {sj.get('budget_source')}）")
    check(sp.returncode == 3, f"触顶是硬信号：退出码 3（实际 {sp.returncode}）")

    gj, gp = guard_json("--now", "2026-09-16 21:00", "--json")
    check(gp.returncode == 3 and gj["stop_reason"]["key"] == "token",
          f"token 触顶 → 收工（实际 {gp.returncode}/{gj and gj.get('stop_reason')}）")
    check("停在安全点" in (gj.get("on_stop") or ""), "收工动作指向安全点（不静默烧完）")

    # ⑥ 需求卡条件：有人未收口 → 继续跑；全收口 → 收工
    build()
    write_contract({"依据": "跑到需求卡全收口", "start_at": "2026-09-16 20:00",
                    "stop_at": "2026-09-17 08:00", "cards": {"all_done": True, "ids": []}})
    write_cards("card", {"card-001": "done", "card-002": "reviewing"})
    gj, gp = guard_json("--now", "2026-09-16 21:00", "--json")
    check(gp.returncode == 0 and cond(gj, "cards")["state"] == "pending",
          f"有卡未收口 → 继续跑（实际 {gp.returncode}/{cond(gj, 'cards').get('state')}）")
    check(gj["dispatch_allowed"] is True,
          "数据齐备且判定 CONTINUE → dispatch_allowed=True（这才是「可以派活」）")
    check("reviewing" in cond(gj, "cards").get("detail", ""),
          "未收口的卡与状态被点名（不用猜是哪张卡卡住了）")

    write_cards("card", {"card-002": "done"})
    gj, gp = guard_json("--now", "2026-09-16 21:00", "--json")
    check(gp.returncode == 3 and gj["stop_reason"]["key"] == "cards",
          f"需求卡全收口 → 收工（实际 {gp.returncode}/{gj and gj.get('stop_reason')}）")
    check("status" not in cond(gj, "cards").get("detail", ""),
          "状态行内注释已剥除（否则「全收口」永远判不成立）")

    # ⑦ 契约点名不存在的卡 → unknown。通用铁律②：「查不到」不等于「已满足」
    build()
    write_contract({"依据": "指定卡收口", "start_at": "2026-09-16 20:00",
                    "stop_at": "2026-09-17 08:00",
                    "cards": {"all_done": False, "ids": ["card-001", "card-404"]}})
    write_cards("card", {"card-001": "done"})
    gj, gp = guard_json("--now", "2026-09-16 21:00", "--json")
    check(cond(gj, "cards")["state"] == "unknown",
          f"点名了不存在的卡 → unknown（实际 {cond(gj, 'cards').get('state')}）")
    check(cond(gj, "cards")["state"] != "fired" and gp.returncode == 5,
          f"「没查到」不据以收工，且**退出码是 5 不是 0**（实际 {gp.returncode}）——"
          "一个数字表达不了「继续」和「没查到」两件事，编排层不该靠翻正文找 unknown")
    check(gj["unknown"] == ["需求卡收工条件"], f"unknown 被显式列出（实际 {gj['unknown']}）")
    check(gj["dispatch_allowed"] is False,
          "unknown 时 dispatch_allowed=False（能继续跑 ≠ 可以派活）")
    txt = run("--guard", "--now", "2026-09-16 21:00").stdout
    check("不等于「已满足」" in txt, "文本报告提醒 unknown≠已满足")
    check("不是 0" in txt, "文本报告点明这次退出码不是 0")

    # ⑧ 需求卡目录整个缺失 → 同样 unknown，不许当成「没有卡要跑」
    build()
    write_contract({"依据": "目录没建也要说实话", "start_at": "2026-09-16 20:00",
                    "stop_at": "2026-09-17 08:00", "cards": {"all_done": True}})
    gj, gp = guard_json("--now", "2026-09-16 21:00", "--json")
    check(cond(gj, "cards")["state"] == "unknown",
          f"需求卡目录不存在 → unknown（实际 {cond(gj, 'cards').get('state')}）",
          cond(gj, "cards").get("detail"))
    check(gp.returncode == 5, "目录缺失本身不构成收工理由，但退出码是 5（不许被当 0 放行）")
    check(gj["dispatch_allowed"] is False, "目录缺失时不给派活许可")

    # ⑨b 优先级：一条已达成 + 另一条没查到 → 判停（3），不是 5——「停」永远是最安全的一侧。
    # 这条是**写编排条款时才对拍出来的**：条款说"该停就停"，就得有断言钉住它不被后加的 unknown 逻辑掀翻。
    build()
    write_contract({"依据": "张卡收口但测试卡目录还没建", "start_at": "2026-09-16 20:00",
                    "stop_at": "2026-09-17 08:00", "cards": {"all_done": True},
                    "test_gate": "all_done"})
    write_cards("card", {"card-001": "done"})
    gj, gp = guard_json("--now", "2026-09-16 21:00", "--json")
    check(gp.returncode == 3 and gj["stop_reason"]["key"] == "cards",
          f"已达成优先于没查到 → 判停（实际 {gp.returncode}/{gj and (gj.get('stop_reason') or {}).get('key')}）")
    check(gj["unknown"] == ["全量测试条件"], f"没查到的那条仍要列出来（实际 {gj['unknown']}）")

    # ⑨ 全量测试条件（test_gate=all_done）
    build()
    write_contract({"依据": "跑到全量测试过", "start_at": "2026-09-16 20:00",
                    "stop_at": "2026-09-17 08:00", "test_gate": "all_done"})
    write_cards("tc", {"tc-001": "done", "tc-002": "in_progress"})
    gj, gp = guard_json("--now", "2026-09-16 21:00", "--json")
    check(cond(gj, "test_gate")["state"] == "pending" and gp.returncode == 0,
          f"测试卡未收口 → pending（实际 {cond(gj, 'test_gate').get('state')}）")
    write_cards("tc", {"tc-002": "done"})
    gj, gp = guard_json("--now", "2026-09-16 21:00", "--json")
    check(gp.returncode == 3 and gj["stop_reason"]["key"] == "test_gate",
          f"全量测试收口 → 收工（实际 {gp.returncode}/{gj and gj.get('stop_reason')}）")

    # ⑩ 契约解析：写错一律报错——静默当成「关掉这条」会让一次跑批少半个闸门，报告上看不出来
    build()
    write_contract({"依据": "x", "start_at": "2026-09-16 20:00", "test_gate": "all_green"})
    g = run("--guard", "--now", "2026-09-16 21:00")
    check(g.returncode == 2 and "test_gate" in (g.stderr or ""),
          "test_gate 拼错 → 退出码 2 并点名（不许降级成 none）", (g.stderr or "")[:200])

    write_contract({"依据": "x", "stop_at": "22:00", "token_budget": 1000})
    g = run("--guard", "--now", "2026-09-16 21:00")
    check(g.returncode == 2, "stop_at 只有 HH:MM 且无 start_at 锚点 → 报错而不是猜今天/明天",
          (g.stderr or "")[:200])

    write_contract({"依据": "x", "start_at": "2026-09-16 20:00", "stop_at": "2026-09-16 22:00"})
    check(run("--guard", "--now", "昨天").returncode == 2, "--now 写废 → 退出码 2")

    # ⑪ 跨零点：20:00 → 02:00 顺延到次日（夜间序列就是这么写的）
    build()
    write_contract({"依据": "夜跑", "start_at": "2026-09-16 20:00", "stop_at": "02:00"})
    gj, gp = guard_json("--now", "2026-09-16 23:00", "--json")
    check(gj and gj["verdict"] == "CONTINUE",
          f"23:00 未到次日 02:00 → 继续跑（实际 {gj and gj['verdict']}）")
    gj, gp = guard_json("--now", "2026-09-17 02:30", "--json")
    check(gp.returncode == 3 and gj["stop_reason"]["key"] == "time", "跨零点后到点照样收工")

    # ⑫ 契约折算时间窗与预算：--plan 不必再给 --window，且无人值守自动开启
    build()
    write_contract({"依据": "整夜 6 小时", "start_at": "2026-09-16 20:00", "window_hours": 6})
    f = write_cands(CANDS)
    p = run("--plan", str(f), "--json")
    check(p.returncode == 0, f"窗口可由契约折算（实际 {p.returncode}）", (p.stderr or "")[:200])
    pd = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
    check(pd.get("window_hours") == 6.0, f"折算窗口=6 小时（实际 {pd.get('window_hours')}）")
    check(pd.get("unattended") is True, "契约 unattended=true → 按无人值守排除不可逆卡")
    check(any(e["id"] == "card-015" for e in pd.get("excluded", [])),
          "不可逆卡（对外发布）被排除在夜间序列外")

    build()
    f = write_cands(CANDS)
    p = run("--plan", str(f), "--json")
    check(p.returncode == 2, f"既无 --window 又无契约 → 退出码 2（不许默认成某个窗口）（实际 {p.returncode}）")

    # ⑬ 用户原话的完整走查：「晚上八点开始跑，跑到十点或者跑到 3 亿 token」
    #     三处细节是端到端手测才暴露的（单测当时全是绿的），各钉一条：
    #     ① 额度要按「亿」渲染（300000.0k 没人这么读，而契约是要拿给人核对的那份）
    #     ② 收工条件先于开跑闸门：条件已达成而还没到点时，判 STOP（无需开跑）而非 NOT_STARTED
    #     ③ 这种组合必须被说明白，否则"没活干"会被读成"活干完了"
    build()
    write_contract({
        "依据": "用户：20:00 开始跑，跑到 22:00 或 3 亿 token",
        "start_at": "2026-09-16 20:00", "stop_at": "2026-09-16 22:00",
        "token_budget": 300000000, "test_gate": "all_done"})
    write_cards("tc", {"tc-001": "done", "tc-002": "done"})
    txt = run("--guard", "--now", "2026-09-16 19:30").stdout
    check("3.0亿 token" in txt, "三亿额度按「亿」渲染（不是 300000.0k）", txt[txt.find("token 额度"):][:120])
    gj, gp = guard_json("--now", "2026-09-16 19:30", "--json")
    check(gp.returncode == 3 and gj["verdict"] == "STOP",
          f"收工条件已达成但未到开跑时刻 → STOP（实际 {gj and gj['verdict']}/{gp.returncode}）")
    check(gj["start_gate"]["reached"] is False, "同时如实报出「未到开跑时刻」")
    check("还没到开跑时刻" in txt, "文本报告把「条件已满足 + 未到点」说清楚（免得读成跑了一半被叫停）")

    write_cards("tc", {"tc-002": "in_progress"})      # 条件未达成 → 回到常规闸门语义
    gj, gp = guard_json("--now", "2026-09-16 19:30", "--json")
    check(gp.returncode == 4 and gj["verdict"] == "NOT_STARTED",
          f"条件未达成时未到点仍判 NOT_STARTED（实际 {gj and gj['verdict']}/{gp.returncode}）")
    gj, gp = guard_json("--now", "2026-09-16 20:30", "--json")
    check(gp.returncode == 0 and gj["verdict"] == "CONTINUE", "进窗口后放行跑")
    gj, gp = guard_json("--now", "2026-09-16 22:01", "--json")
    check(gp.returncode == 3 and gj["stop_reason"]["key"] == "time", "到十点收工")

    # ⑭ GBK 控制台：闸门输出有中文表格，不能崩    build()
    env_gbk = dict(os.environ, PYTHONIOENCODING="gbk")
    write_contract({"依据": "编码试跑（中文）", "start_at": "2026-09-16 20:00",
                    "stop_at": "2026-09-16 22:00", "token_budget": 100000,
                    "cards": {"all_done": True}, "test_gate": "all_done"})
    write_cards("card", {"card-001": "done"})
    write_cards("tc", {"tc-001": "done"})
    for label, argv, ok_codes in (("--guard", ["--guard"], (0, 3, 4)),
                                   ("--contract-init", ["--contract-init", "--force"], (0,))):
        pl = run(*argv, env=env_gbk)
        check("UnicodeEncodeError" not in (pl.stderr or ""), f"{label} stderr 无 UnicodeEncodeError")
        check(pl.returncode in ok_codes, f"{label} GBK 退出码正常（实际 {pl.returncode}）",
              (pl.stderr or "")[-300:])
        check("✅" not in pl.stdout and "⚠" not in pl.stdout, f"{label} 输出无 GBK 不安全符号")

    # ---------------- N 真口径台账 I/O（F-06 / oi-020） ----------------
    # 真账本是 进度/估算实测.csv（11 列，小时制）与 进度/成本流水.csv（10 列，逐行、无累计列）。
    # 老实现按**位置**取列：真账本下会把「角色」读成「类型」、把「估算O(小时)」读成「估时分钟」……
    # 这一节把「按表头名定位 / 认不出列名整批记坏行 / 只追加不改表头 / 缺项写 -」钉住。
    print("\n测试 N：真口径台账 I/O（11 列估算实测 / 10 列成本流水，按表头名定位）")
    EST_HEADER = ("卡号,角色,估算O(小时),估算M(小时),估算P(小时),P(窗口),实际(小时),偏差,裁尾,"
                  "登记时间,证据")
    BURN_HEADER = "时间,卡号,角色,模型,tokens_in,tokens_out,估算成本,币种,依据,登记时间"
    build()
    est = ROOT / "project-state" / "进度" / "估算实测.csv"
    est.write_text(EST_HEADER + "\n"
                   "card-037,集成工程师,3.75,7.08,13.83,-,1.05,-6.03,-,2026-10-08 13:13,"
                   "估算取自 project-state/需求卡/card-037/card.md::77\n", encoding="utf-8")
    cp = run("--calibrate", "--json")
    cj = json.loads(cp.stdout) if cp.stdout.strip().startswith("{") else None
    check(cj is not None, "真 11 列账本 --calibrate --json 可解析", (cp.stdout or cp.stderr)[:200])
    if cj:
        check(cj.get("ledger_schema") == "11列真口径",
              f"认得出真口径（实际 {cj.get('ledger_schema')}）")
        check(cj["samples_total"] == 1, f"真账本 1 行算 1 个样本（实际 {cj['samples_total']}）")
        check(cj["bad_rows"] == 0, f"无坏行（实际 {cj['bad_rows']}）")
        info = cj["types"].get("集成工程师", {})
        check(info.get("n") == 1, f"按「角色」列分桶（实际桶 {list(cj['types'])}）")
        check(abs((info.get("median_act_min") or 0) - 63.0) < 1e-6,
              f"「实际(小时)=1.05」换算成 63 分钟（实际 {info.get('median_act_min')}）")
        check(info.get("token_factor") is None, "真账本没有 token 列 → token 系数留空（不拿 0 顶替）")
        check(info.get("time_factor") is not None and abs(info["time_factor"] - 0.148) < 1e-3,
              f"估时偏差系数=实际/估算M≈0.148（实际 {info.get('time_factor')}）")
    ntxt = run("--calibrate")
    check(ntxt.returncode == 0 and "11列真口径" in ntxt.stdout,
          "文本版 --calibrate 报出口径、中位数为空也不崩", (ntxt.stdout or ntxt.stderr)[-200:])

    rec = run("--record", "--card", "card-040", "--role", "集成工程师",
              "--est-o", "2", "--est-m", "4", "--est-p", "8", "--act-hours", "6",
              "--evidence", "project-state/需求卡/card-040/card.md::12",
              "--record-time", "2026-10-08 15:00", "--json")
    rj = json.loads(rec.stdout) if rec.stdout.strip().startswith("{") else None
    check(rec.returncode == 0 and rj is not None and rj.get("schema") == "11列真口径",
          "--record 写进真 11 列账本", (rec.stderr or rec.stdout)[:200])
    lines = est.read_text(encoding="utf-8").splitlines()
    check(lines[0] == EST_HEADER, "追加后表头逐字节不变（不新长出一套列）")
    check(len(lines) == 3, f"只追加一行（实际 {len(lines)} 行）")
    r0 = lines[-1].split(",")
    check(r0[0] == "card-040" and r0[1] == "集成工程师", f"列序按表头走（实际 {r0[:2]}）")
    check(r0[5] == "-", f"没给的 P(窗口) 写 `-`（不写 0、不写空；实际 {r0[5]!r}）")
    check(r0[9] == "2026-10-08 15:00", f"登记时间按给定的写（实际 {r0[9]!r}）")
    try:
        ok_act = abs(float(r0[6]) - 6.0) < 1e-9 and abs(float(r0[7]) - 2.0) < 1e-9
    except ValueError:
        ok_act = False
    check(ok_act, f"实际=6h、偏差=实际−M=2h（实际 {r0[6]}/{r0[7]}）")
    if rj:
        check(rj["row"].get("P(窗口)") == "-", "JSON 回执里缺项同样是 `-`")

    # 旧别名（分钟制）在真账本下仍可用，但 token 没地方放时必须**明说**（不静默丢）
    warn = run("--record", "--card", "card-041", "--est-min", "60", "--act-min", "90",
               "--est-tokens", "10000", "--act-tokens", "20000")
    check(warn.returncode == 0, "旧别名 --est-min/--act-min 仍可用", (warn.stderr or "")[-200:])
    check("没有 token 实测列" in (warn.stderr or ""),
          "真账本下 token 参数被明说「未写入」（不静默丢）", (warn.stderr or "")[-200:])
    rleg = est.read_text(encoding="utf-8").splitlines()[-1].split(",")
    try:
        ok_leg = abs(float(rleg[3]) - 1.0) < 1e-9 and abs(float(rleg[6]) - 1.5) < 1e-9
    except ValueError:
        ok_leg = False
    check(ok_leg, f"--est-min 60→估算M 1h、--act-min 90→实际 1.5h（实际 {rleg[3]}/{rleg[6]}）")

    # 旧 6 列账本：继续按它自己的列名读、按它自己的列序写（不迁移、不混列）
    build()
    est.write_text("卡号,类型,估时分钟,实时分钟,估token,实token\n"
                   "card-001,后端,100,150,100000,150000\n"
                   "card-002,后端,100,150,100000,150000\n"
                   "card-003,后端,100,150,100000,150000\n", encoding="utf-8")
    lj = json.loads(run("--calibrate", "--json").stdout)
    check(lj.get("ledger_schema") == "6列旧口径", f"认得出旧口径（实际 {lj.get('ledger_schema')}）")
    check(lj["samples_total"] == 3, f"旧账本 3 行 3 样本（实际 {lj['samples_total']}）")
    li = lj["types"].get("后端", {})
    check(li.get("n") == 3 and abs((li.get("time_factor") or 0) - 1.5) < 0.01,
          f"旧账本仍按自身列名解析（1.5，实际 {li.get('time_factor')}）")
    check("后端" in lj["usable_types"], "旧账本 3 样本仍判可用")
    check(li.get("token_factor") is not None, "旧账本有 token 列 → token 系数照旧算得出来")
    run("--record", "--card", "card-004", "--type", "后端", "--est-min", "100", "--act-min", "150")
    lines = est.read_text(encoding="utf-8").splitlines()
    check(lines[0].startswith("卡号,类型,估时分钟"), "往旧账本追加仍按旧表头（不被改写成 11 列）")
    check(len(lines) == 5, f"旧账本追加后 5 行（实际 {len(lines)}）")

    # 真 10 列成本流水：逐行读数、`未查过` 不计价、没有累计列可被手改
    build()
    burn = ROOT / "project-state" / "进度" / "成本流水.csv"
    burn.write_text(BURN_HEADER + "\n"
                    "2026-10-08 14:00,card-037,集成工程师,deepseek-v4,12000,800,未查过,未查过,"
                    "job-abc,2026-10-08 14:01\n"
                    "2026-10-08 14:10,card-037,集成工程师,deepseek-v4,30000,2000,0.0123,CNY,"
                    "job-def,2026-10-08 14:11\n", encoding="utf-8")
    bp = run("--burn-status", "--budget-tokens", "100000", "--json")
    bj = json.loads(bp.stdout) if bp.stdout.strip().startswith("{") else None
    check(bj is not None, "真 10 列成本流水 --burn-status --json 可解析", (bp.stdout or bp.stderr)[:200])
    if bj:
        check(bj.get("schema") == "10列真口径", f"认得出真口径（实际 {bj.get('schema')}）")
        check(bj["rows"] == 2 and bj["cum_tokens"] == 44800,
              f"逐行 tokens_in+out 求和=44800（实际 {bj['rows']} 行 / {bj['cum_tokens']}）")
        check(abs((bj["cum_cost"] or 0) - 0.0123) < 1e-9,
              f"`未查过` 不计价、有值的求和（实际 {bj['cum_cost']}）")
        check(bj["by_role"].get("集成工程师", {}).get("tokens") == 44800, "按角色分桶（真列名）")
    sp = run("--spend", "--card", "card-037", "--role", "集成工程师", "--model", "deepseek-v4",
             "--prompt-tokens", "5000", "--completion-tokens", "500", "--note", "job-ghi",
             "--budget-tokens", "100000", "--json")
    sj = json.loads(sp.stdout) if sp.stdout.strip().startswith("{") else None
    check(sj is not None and sj.get("schema") == "10列真口径", "--spend 认得出真口径",
          (sp.stdout or sp.stderr)[:200])
    if sj:
        check(sj["cum_tokens"] == 50300, f"累计由各行重算=50300（实际 {sj['cum_tokens']}）")
        check(abs((sj["cum_cost"] or 0) - 0.0123) < 1e-9,
              f"本轮没成本 ⇒ 不加价，累计仍是账本里唯一有值的 0.0123（实际 {sj['cum_cost']}）")
        check(not sj["breaker"], "未超预算不熔断")
    lines = burn.read_text(encoding="utf-8").splitlines()
    check(lines[0] == BURN_HEADER, "追加后成本流水表头逐字节不变")
    check(len(lines) == 4, f"真口径只追加一行（实际 {len(lines)} 行）")
    last = lines[-1].split(",")
    check(len(last) == 10, f"真口径 10 字段、不写累计列（实际 {len(last)}）")
    check(last[6] == "未查过" and last[7] == "未查过",
          f"没给成本时写 `未查过`（实际 {last[6]}/{last[7]}）")
    check(last[8] == "job-ghi", f"`依据` 落 --note/作业 id（实际 {last[8]!r}）")
    check(last[4] == "5000" and last[5] == "500", f"逐行读数（实际 {last[4]}/{last[5]}）")

    # 陌生表头 = 拒写（v5.3.1 / 独立复核 V-01）：读路径"整批坏行"与写路径"照写不误"必须对称
    build()
    est = ROOT / "project-state" / "进度" / "估算实测.csv"
    est.write_text("甲,乙,丙\nr1,1,2\nr2,3,4\n", encoding="utf-8")
    est_before = est.read_bytes()
    rp = run("--record", "--card", "card-B1", "--est-m", "3", "--act-hours", "4")
    check(rp.returncode == 2, f"陌生表头 --record 拒写 rc=2（实际 {rp.returncode}）")
    check("表头不认识" in (rp.stderr or ""), "拒写 stderr 说明原因", (rp.stderr or "")[:150])
    check(est.read_bytes() == est_before, "拒写后估算账本一字未变（不留 `-,-,-` 垃圾行）")
    cj = run("--calibrate", "--json")
    try:
        cd = json.loads(cj.stdout)
    except ValueError:
        cd = None
    check(cd is not None and cd.get("ledger_schema") == "列名不认识" and cd.get("bad_rows") == 2,
          "同一份账本读路径仍如实报「列名不认识 + 整批坏行」", (cj.stdout or "")[:160])

    build()
    burn = ROOT / "project-state" / "进度" / "成本流水.csv"
    burn.write_text("甲,乙,丙\nr1,1,2\n", encoding="utf-8")
    burn_before = burn.read_bytes()
    spp = run("--spend", "--card", "card-B1", "--prompt-tokens", "100", "--completion-tokens", "50")
    check(spp.returncode == 2, f"陌生表头 --spend 拒写 rc=2（实际 {spp.returncode}）")
    check("表头不认识" in (spp.stderr or ""), "成本拒写 stderr 说明原因", (spp.stderr or "")[:150])
    check(burn.read_bytes() == burn_before, "拒写后成本流水一字未变")

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
