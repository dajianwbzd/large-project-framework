#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arch_mermaid.py — 架构图 JSON → Mermaid 代码（可视化闭环 P0 兜底）
生成 flowchart（组件拓扑 + 边界 subgraph）+ sequenceDiagram（可选，connections 按 from 顺序）+ stateDiagram-v2（可选）。
用法: python arch_mermaid.py <架构图.json> [--flowchart|--sequence|--state] [--out out.md]
默认输出 flowchart 代码块到 stdout；--out 时追加到指定 .md（含 ```mermaid 围栏）。
"""
import json
import os
import sys


def resolve_out(p):
    """--out 边界校验：只允许追加当前项目目录内的文件（拒 ../ 与目录外绝对路径逃逸）。"""
    base = os.path.normcase(os.path.realpath(os.getcwd()))
    target = os.path.normcase(os.path.realpath(os.path.abspath(p)))
    if not target.startswith(base + os.sep):
        return None
    return os.path.realpath(os.path.abspath(p))

MERMAID_CLASS = {"frontend": "id (Frontend)", "backend": "id (Backend)", "database": "id[(Database)]",
                 "cloud": "id(Cloud)", "security": "id[Security]", "messagebus": "id{MessageBus}", "external": "id[(External)]"}


def esc(label):
    return label.replace('"', '&quot;')


def flow_id(c):
    return c["id"]


PALETTE = {"frontend": "#22d3ee", "backend": "#34d399", "database": "#a78bfa",
           "cloud": "#fbbf24", "security": "#fb7185", "messagebus": "#fb923c", "external": "#94a3b8"}


def flowchart(graph):
    """组件拓扑 → Mermaid flowchart。节点统一 id[\"label\"]，类型用 Mermaid classDef 着色。"""
    lines = ["graph LR"]
    for t, color in PALETTE.items():
        lines.append(f"  classDef {t} fill:{color},stroke:#0f172a")
    for i, b in enumerate(graph.get("boundaries", [])):
        lines.append(f"  subgraph B{i}[\"{esc(b['label'])}\"]")
    for c in graph.get("components", []):
        lines.append(f"  {c['id']}[\"{esc(c['label'])}\"]")
    for e in graph.get("connections", []):
        if e.get("label"):
            lines.append(f"  {e['from']} -- \"{esc(e['label'])}\" --> {e['to']}")
        else:
            lines.append(f"  {e['from']} --- {e['to']}")
    if graph.get("boundaries"):
        for _ in graph["boundaries"]:
            lines.append("  end")
    for c in graph.get("components", []):
        lines.append(f"  class {c['id']} {c.get('type', 'external')}")
    return "\n".join(lines)


def sequence(graph, main_path=None):
    """把 connections 链展开为 participant + 消息；main_path 可指定 from→to 主路径（缺省按连接定义顺序）"""
    lines = ["sequenceDiagram"]
    parts = []
    for e in graph.get("connections", []):
        if e["from"] not in parts:
            parts.append(e["from"])
        if e["to"] not in parts:
            parts.append(e["to"])
    for p in parts:
        lines.append(f"  participant {p}")
    for e in graph.get("connections", []):
        lbl = e.get("label") or e.get("id", "调用")
        lines.append(f"  {e['from']}->>{e['to']}: {esc(lbl)}")
    return "\n".join(lines)


def wrap_fence(body):
    return f"```mermaid\n{body}\n```"


def main():
    if len(sys.argv) < 2:
        print("用法: python arch_mermaid.py <架构图.json> [--flowchart|--sequence] [--out out.md]", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        graph = json.load(f)
    mode = "--sequence" if "--sequence" in sys.argv else "--flowchart"
    body = sequence(graph) if mode == "--sequence" else flowchart(graph)
    out_idx = sys.argv.index("--out") if "--out" in sys.argv else -1
    if out_idx > -1 and out_idx + 1 < len(sys.argv):
        out_path = resolve_out(sys.argv[out_idx + 1])
        if out_path is None:
            print("拒绝写入：--out 必须落在当前项目目录内（cwd=%s）" % os.getcwd(), file=sys.stderr)
            return 2
        with open(out_path, "a", encoding="utf-8") as f:
            f.write("\n\n" + wrap_fence(body) + "\n")
    else:
        print(wrap_fence(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())