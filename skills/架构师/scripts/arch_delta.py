#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arch_delta.py — 架构演进对比（借鉴 archify Architecture Delta，只读不推断）
对两份已校验的 架构图.json 输出 新增/删除/变化 的组件与连接。
用法: python arch_delta.py <base.json> <head.json> [--json]
"""
import json
import sys


def delta(base, head):
    b_comps = {c["id"]: c for c in base.get("components", [])}
    h_comps = {c["id"]: c for c in head.get("components", [])}
    b_conns = {(e.get("from"), e.get("to")): e for e in base.get("connections", [])}
    h_conns = {(e.get("from"), e.get("to")): e for e in head.get("connections", [])}

    report = {"added_components": [], "removed_components": [], "changed_components": [],
              "added_connections": [], "removed_connections": []}

    for cid in sorted(set(h_comps) - set(b_comps)):
        report["added_components"].append({"id": cid, "label": h_comps[cid].get("label"), "type": h_comps[cid].get("type")})
    for cid in sorted(set(b_comps) - set(h_comps)):
        report["removed_components"].append({"id": cid, "label": b_comps[cid].get("label"), "type": b_comps[cid].get("type")})
    for cid in sorted(set(b_comps) & set(h_comps)):
        changes = {}
        for k in ("label", "type"):
            if b_comps[cid].get(k) != h_comps[cid].get(k):
                changes[k] = {"before": b_comps[cid].get(k), "after": h_comps[cid].get(k)}
        if changes:
            report["changed_components"].append({"id": cid, **changes})

    for key in sorted(set(h_conns) - set(b_conns)):
        report["added_connections"].append({"from": key[0], "to": key[1], "label": h_conns[key].get("label")})
    for key in sorted(set(b_conns) - set(h_conns)):
        report["removed_connections"].append({"from": key[0], "to": key[1], "label": b_conns[key].get("label")})

    return report


def main():
    if len(sys.argv) < 3:
        print("用法: python arch_delta.py <base.json> <head.json> [--json]", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        base = json.load(f)
    with open(sys.argv[2], encoding="utf-8") as f:
        head = json.load(f)
    rep = delta(base, head)
    if "--json" in sys.argv[3:]:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("=== 架构演进 Delta ===")
        print(f"新增组件: {rep['added_components'] or '无'}")
        print(f"移除组件: {rep['removed_components'] or '无'}")
        print(f"变更组件: {rep['changed_components'] or '无'}")
        print(f"新增连接: {rep['added_connections'] or '无'}")
        print(f"移除连接: {rep['removed_connections'] or '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())