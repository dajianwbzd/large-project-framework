#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
arch_reconcile.py — 图账对账（借鉴 atlas-archify-coding A1 "图=投影 码=实相"）
把 架构图.json 的组件集 与 任务清单.md 的任务引用 逐条对账，点名不一致：
  - GAP:   图上有组件、任务清单里没有任何任务承接它（悬空设计）
  - DRIFT: 任务清单引用了图上不存在的组件（游离任务）
    （引用方式：任务行内嵌 `(组件:组件id)` 标注，或任务行内出现组件 id 前缀）
用法: python arch_reconcile.py <架构图.json> <任务清单.md> [--json] [--strict]
退出码: 0 = 账平衡 / 1 = 存在问题 / 2 = 用法错

读取适配层（2026-10-08，card-037 裁决 2）：
  真契约 `契约/架构图.json`（v3.15.0）的组件**无 label，用 name**；本脚本原先在 :61 硬读
  `c['label']` ⇒ 对真契约必然 `KeyError`（D8 复核留档的两份取证件就是崩溃转写）。现取显示名
  `label or name or id`；`--strict` 无实际差异，仅为与 arch_validate.py 口径一致而保留。
附带修正（同一裁决，2026-10-08）：原 `组件[:：]\s*([a-zA-Z0-9_-]+)` 只能捕获**第一个** id，
对 `(组件:c-shell-registry, c-contract)` 这种多组件标注漏记 ⇒ GAP 会被虚报。现按整串捕获后切分。
"""
import json
import re
import sys

COMPONENT_REF = re.compile(r"组件[:：]\s*((?:[A-Za-z0-9_*-]+)(?:\s*[,、，]\s*[A-Za-z0-9_*-]+)*)")


def display_name(c):
    """组件显示名：label（框架规约）优先，回退 name（工程真契约），再回退 id。"""
    return c.get("label") or c.get("name") or c.get("id")


def refs_in(line):
    """取一行里的全部组件引用（支持 `(组件:a, b、c)` 多组件标注与 `c-shell-*` 前缀通配）。"""
    out = set()
    for m in COMPONENT_REF.finditer(line):
        for part in re.split(r"[,、，]", m.group(1)):
            part = part.strip()
            if part:
                out.add(part)
    return out


def expand_refs(specs, comps):
    """把引用规格展开成真实组件 id：`c-shell-*` 按前缀匹配；其余按精确 id。"""
    exact, prefixes = set(), []
    for s in specs:
        if s.endswith("*"):
            prefixes.append(s[:-1])
        else:
            exact.add(s)
    for p in prefixes:
        exact |= {cid for cid in comps if cid.startswith(p)}
    return exact


def parse_tasks(path):
    """解析任务清单.md：每行 T-xx [card-yyy] 描述 (组件:id)。返回 [(task_id, {ref_comps})]"""
    tasks = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as ex:
        print(f"无法读取任务清单: {ex}", file=sys.stderr)
        return tasks
    for ln in lines:
        m = re.match(r"\s*[-*]\s*\[.?\]\s*(T-\d+)", ln)
        if not m:
            continue
        tid = m.group(1)
        tasks.append((tid, refs_in(ln)))
    return tasks


def reconcile(graph_path, task_path):
    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)
    comps = {c["id"]: c for c in graph.get("components", [])}
    tasks = parse_tasks(task_path)
    if not tasks:
        return [{"rule": "no_tasks", "severity": "error",
                 "message": "任务清单中未解析到任何 T-xx 行（格式: `- [ ] T-01 [card-001] 描述 (组件:id)`）"}]

    ref_count = {}      # 图上组件 -> 精确 id 引用它的任务数
    ref_wild = {}       # 图上组件 -> 仅由 `前缀*` 通配承接的引用数
    task_refs = {}      # 任务 -> 已解析的组件集合
    drift_specs = {}    # 任务 -> 未能解析的引用规格（精确 id 不存在，或通配零命中）
    for tid, specs in tasks:
        resolved = expand_refs(specs, comps)
        wild = {cid for s in specs if s.endswith("*") for cid in comps if cid.startswith(s[:-1])}
        task_refs[tid] = resolved
        bad = []
        for s in specs:
            if s.endswith("*"):
                if not any(cid.startswith(s[:-1]) for cid in comps):
                    bad.append(s)
            elif s not in comps:
                bad.append(s)
        drift_specs[tid] = bad
        for r in (resolved - wild):
            ref_count[r] = ref_count.get(r, 0) + 1
        for r in wild:
            ref_wild[r] = ref_wild.get(r, 0) + 1

    issues = []
    # GAP: 图上组件零任务承接（精确 id 与 `前缀*` 通配都算；仅靠通配承接的另发警告，不静默算作已承接）
    for cid, c in comps.items():
        if ref_count.get(cid, 0) == 0:
            if ref_wild.get(cid, 0) > 0:
                issues.append({
                    "rule": "A1_WILDCARD_ONLY",
                    "severity": "warning",
                    "subject": f"components[{cid}]",
                    "message": (f"图上组件 {display_name(c)!r} 只有 `前缀*` 通配任务承接、没有任何精确 id 承接"
                                "（通配会掩盖悬空设计，建议任务行写精确 id）"),
                })
            else:
                issues.append({
                    "rule": "A1_GAP",
                    "severity": "error",
                    "subject": f"components[{cid}]",
                    "message": f"图上组件 {display_name(c)!r} 没有任何任务承接（悬空设计，任务清单漏拆）",
                })
    # DRIFT: 任务引用图上不存在的组件（含零命中的 `前缀*` 通配）
    for tid, bad in drift_specs.items():
        for r in bad:
            issues.append({
                "rule": "A1_DRIFT",
                "severity": "error",
                "subject": f"tasks[{tid}]",
                "message": f"任务 {tid} 引用了图上不存在的组件 {r!r}（游离任务或图过期）",
            })
    # 未标注：完全没有任何 `(组件:...)` 规格的任务（只挂错 id 的算 DRIFT，不重复计 NOREF）
    no_ref = [tid for tid, specs in tasks if not specs]
    for tid in no_ref:
        issues.append({
            "rule": "A1_NOREF",
            "severity": "warning",
            "subject": f"tasks[{tid}]",
            "message": f"任务 {tid} 未标注 (组件:...) 引用，无法对账",
        })

    if not issues:
        issues.append({"rule": "A1_OK", "severity": "ok", "subject": "reconcile",
                       "message": f"账平衡：{len(comps)} 组件全部有任务承接，0 游离任务"})
    return issues


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print("用法: python arch_reconcile.py <架构图.json> <任务清单.md> [--json] [--strict]", file=sys.stderr)
        return 2
    graph_path, task_path = args[0], args[1]
    as_json = "--json" in sys.argv[3:]
    issues = reconcile(graph_path, task_path)
    errors = [i for i in issues if i.get("severity") == "error"]
    gaps = [i for i in errors if i["rule"] == "A1_GAP"]
    drifts = [i for i in errors if i["rule"] == "A1_DRIFT"]
    if as_json:
        n_comp = len(json.load(open(graph_path, encoding="utf-8")).get("components", []))
        wild_only = [i for i in issues if i["rule"] == "A1_WILDCARD_ONLY"]
        print(json.dumps({"ok": not errors, "components": n_comp,
                          "tasks": len(parse_tasks(task_path)), "A1_GAP": len(gaps), "A1_DRIFT": len(drifts),
                          "A1_NOREF": len([i for i in issues if i["rule"] == "A1_NOREF"]),
                          "A1_WILDCARD_ONLY": len(wild_only),
                          "exact_covered_components": n_comp - len(gaps) - len(wild_only),
                          "issues": issues}, ensure_ascii=False, indent=2))
    else:
        for i in issues:
            print(f"[{i['severity'].upper():7}] {i['rule']:10} {i['subject']}: {i['message']}")
        print(f"\n对账结果: {'平衡' if not errors else f'存在问题 {len(errors)} 处（A1_GAP {len(gaps)} / A1_DRIFT {len(drifts)}）'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
