#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arch_validate.py — 架构图 JSON 契约校验器（纯 stdlib，零依赖）
借鉴 archify (tt-a1i, MIT) 的 typed JSON IR + 校验回执设计，落成框架可用的最小实现。
规约（框架版架构图契约，见 02_方法论.md）：
{
  "schema_version": 1,
  "diagram_type": "architecture",
  "meta": {"title": "...", "cards": ["card-001", "REQ-1.1"]},
  "components": [{"id","type"(frontend|backend|database|cloud|security|messagebus|external),"label",("sources":[{path,line}])?}],
  "boundaries":  [{"kind"(region|security-group),"label","wraps":[component id]}],
  "connections": [{"from","to",("label")?}]
}
用法: python arch_validate.py <架构图.json> [--json] [--strict]
退出码: 0 = 通过(0 error) / 1 = 有 error / 2 = 用法错
诊断格式对齐 archify: {rule, severity, subject, message}

读取适配层（2026-10-08，card-037 裁决 2 —— 工程真契约与本规约两套字段名不同义）：
  真契约 `project-state/契约/架构图.json`（v3.15.0，19 组件 / 22 边）用的是：
    schema_version="1"（字符串）/ **无** diagram_type / **无** meta /
    components[].{id,name,kind,paths,responsibility,cards,depends_on,interfaces} /
    连边在 edges[{from,to,via,protocol}] / **无** boundaries。
  两者差别只在**字段名与承载位置**，故默认模式做等义回退：
    label ← name ｜ type ← kind ｜ connections ← edges ｜ meta.title ← summary|milestone
    meta.cards ← components[].cards 的并集 ｜ schema_version 接受 1 或 "1"
  回退点一律降级为 warning 并在诊断里点名，**不放弃任何结构判据**（id 唯一、type 合法性、
  from/to 引用存在性、自环、孤立组件、挂卡追溯全部保留）。
  `--strict` 关闭全部回退，恢复上面这份原规约（框架新建架构图应走 strict 口径）。
"""
import json
import sys

COMPONENT_TYPES = {"frontend", "backend", "database", "cloud", "security", "messagebus", "external"}
BOUNDARY_KINDS = {"region", "security-group"}


def display_name(c):
    """组件显示名：label（框架规约）优先，回退 name（工程真契约）。"""
    return c.get("label") or c.get("name") or c.get("id")


def validate(graph, strict=False):
    diags = []

    def err(rule, subject, message):
        diags.append({"rule": rule, "severity": "error", "subject": subject, "message": message})

    def warn(rule, subject, message):
        diags.append({"rule": rule, "severity": "warning", "subject": subject, "message": message})

    # --- 基础结构 ---
    if not isinstance(graph, dict):
        return [{"rule": "not_object", "severity": "error", "subject": "root", "message": "顶层必须是 JSON 对象"}]
    if graph.get("diagram_type") != "architecture":
        if strict or "diagram_type" in graph:
            err("bad_diagram_type", "diagram_type", f"期望 architecture，得到 {graph.get('diagram_type')!r}")
        else:
            warn("missing_diagram_type", "diagram_type", "未声明 diagram_type（适配层按 architecture 处理）")
    sv = graph.get("schema_version")
    if sv not in (1, "1"):
        if strict or sv is not None:
            err("bad_schema_version", "schema_version", f"期望 1（或 \"1\"），得到 {sv!r}")
        else:
            warn("missing_schema_version", "schema_version", "未声明 schema_version（适配层按 1 处理）")

    meta = graph.get("meta") or {}
    if not meta and not strict:
        meta = {"title": graph.get("summary") or graph.get("milestone"),
                "cards": sorted({c for comp in (graph.get("components") or []) for c in (comp.get("cards") or [])})}
        warn("meta_fallback", "meta", "无 meta：标题回退 summary|milestone、挂卡回退 components[].cards 并集（适配层）")
    if not (meta.get("title") if meta else None):
        err("missing_title", "meta.title", "meta.title 必填")

    comps_raw = graph.get("components")
    if not isinstance(comps_raw, list) or not comps_raw:
        err("empty_components", "components", "components 必须是非空数组")
        return diags

    # --- 组件 ---
    seen_ids = set()
    comps = {}
    for i, c in enumerate(comps_raw):
        subj = f"components[{i}]"
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            err("missing_component_id", subj, "组件缺少 id")
            continue
        if cid in seen_ids:
            err("duplicate_id", f"components[{i}].id", f"组件 id 重复: {cid!r}")
        seen_ids.add(cid)
        ctype = c.get("type")
        if ctype is None and not strict:
            ctype = c.get("kind")
            if ctype is not None:
                warn("type_fallback", f"{subj}.type", f"组件 {cid!r} 无 type，回退 kind={ctype!r}（适配层）")
        if ctype not in COMPONENT_TYPES:
            if strict or "type" in c or ctype is None:
                err("bad_component_type", f"{subj}.type",
                    f"非法 type {ctype!r}（合法: {sorted(COMPONENT_TYPES)}）")
            else:
                warn("noncanonical_component_type", f"{subj}.type",
                     f"组件 {cid!r} 的 kind={ctype!r} 不在 {sorted(COMPONENT_TYPES)} 内（适配层降级为 warning）")
        if not isinstance(c.get("label"), str) or not c["label"]:
            if isinstance(c.get("name"), str) and c["name"] and not strict:
                warn("label_fallback", f"{subj}.label", f"组件 {cid!r} 无 label，回退 name={c['name']!r}（适配层）")
            else:
                err("missing_label", f"{subj}.label", f"组件 {cid!r} 缺少 label")
        srcs = c.get("sources")
        if srcs is not None:
            if not isinstance(srcs, list) or not srcs:
                err("bad_sources", f"{subj}.sources", "sources 必须是非空数组")
            else:
                for s in srcs:
                    if not isinstance(s.get("path"), str) or not s.get("path"):
                        err("bad_source_path", f"{subj}.sources", "source.path 必填")
                    if not isinstance(s.get("line"), int) or s["line"] < 1:
                        err("bad_source_line", f"{subj}.sources", f"source.line 必须 ≥1，得到 {s.get('line')!r}")
        comps[cid] = c

    # --- 边界（真契约无 boundaries：适配层不报错，只在缺键时点名） ---
    if "boundaries" not in graph and not strict:
        warn("missing_boundaries", "boundaries", "无 boundaries 字段（真契约把分组信息放在 edges/invariants，适配层跳过）")
    for j, b in enumerate(graph.get("boundaries") or []):
        subj = f"boundaries[{j}]"
        if b.get("kind") not in BOUNDARY_KINDS:
            err("bad_boundary_kind", f"{subj}.kind", f"非法 kind {b.get('kind')!r}（合法: {sorted(BOUNDARY_KINDS)}）")
        wraps = b.get("wraps")
        if not isinstance(wraps, list) or not wraps:
            err("empty_wraps", f"{subj}.wraps", f"边界 {b.get('label')!r} 的 wraps 必须是非空数组")
        else:
            for w in wraps:
                if w not in comps:
                    err("bad_wrap_ref", f"{subj}.wraps", f"边界 {b.get('label')!r} 包裹了不存在的组件 {w!r}")

    # --- 连接（真契约在 edges：适配层按 from/to 等义读取；depends_on 仅作连通性补充） ---
    edges = graph.get("connections")
    if edges is None and not strict:
        edges = graph.get("edges") or []
        if "connections" not in graph and edges:
            warn("connections_fallback", "connections", f"无 connections，按 edges 等义读取（{len(edges)} 条边，适配层）")
            for k, e in enumerate(edges):
                if not isinstance(e.get("label"), str) and e.get("protocol"):
                    pass
    connected = set()
    for k, e in enumerate(edges or []):
        subj = f"connections[{k}]"
        frm, to = e.get("from") or e.get("source"), e.get("to") or e.get("target")
        if frm not in comps:
            err("bad_connection_ref", f"{subj}.from", f"连接 from 引用不存在的组件 {frm!r}")
        if to not in comps:
            err("bad_connection_ref", f"{subj}.to", f"连接 to 引用不存在的组件 {to!r}")
        if frm == to:
            err("self_loop", subj, f"自环连接: {frm!r} → {to!r}")
        if frm in comps:
            connected.add(frm)
        if to in comps:
            connected.add(to)
    for cid, c in comps.items():
        for dep in (c.get("depends_on") or []):
            if dep in comps:
                connected.add(cid)
                connected.add(dep)

    # --- 孤立组件告警 ---
    for cid, c in comps.items():
        if cid not in connected and len(comps) > 1:
            warn("orphan_component", f"components[{cid}]",
                 f"组件 {display_name(c)!r} 没有任何连接，可能是孤立设计")

    # --- 追溯引用（框架要求：图必挂卡） ---
    cards = meta.get("cards")
    if not cards:
        warn("missing_cards_ref", "meta.cards", "meta.cards 为空——架构图未挂接需求卡（card-xxx / REQ）引用")

    return diags


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("用法: python arch_validate.py <架构图.json> [--json] [--strict]", file=sys.stderr)
        return 2
    path = args[0]
    as_json = "--json" in sys.argv[2:]
    strict = "--strict" in sys.argv[2:]
    try:
        with open(path, encoding="utf-8") as f:
            graph = json.load(f)
    except Exception as ex:
        diags = [{"rule": "unreadable", "severity": "error", "subject": path, "message": f"无法读取/解析: {ex}"}]
    else:
        diags = validate(graph, strict=strict)

    errors = [d for d in diags if d["severity"] == "error"]
    warnings = [d for d in diags if d["severity"] == "warning"]
    if as_json:
        print(json.dumps({"ok": len(errors) == 0, "strict": strict, "errorCount": len(errors),
                          "warningCount": len(warnings), "diagnostics": diags}, ensure_ascii=False, indent=2))
    else:
        for d in diags:
            print(f"[{d['severity'].upper():7}] {d['rule']:20} {d['subject']}: {d['message']}")
        print(f"\n结果: {'通过' if not errors else f'失败 {len(errors)} error'} / {len(warnings)} warning")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
