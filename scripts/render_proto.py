#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""低保真可点击原型渲染器（零依赖，只用 python 标准库）。

输入：spec/*.json（屏幕 + 流转 + REQ/card 绑定）
输出：一个自包含 HTML（无任何外链资源），带 hash 路由，可直接截图：
      out/prototype.html#/flow            → 主流程总览（所有屏 + 流转标注）
      out/prototype.html#/<screen-id>     → 单屏（可点击出边按钮跳转）

退出码：0 成功 / 1 渲染错误 / 2 用法或读取错误
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

MEDIA_CSS = """
@media (max-width:900px){
  .kpi{grid-template-columns:repeat(2,minmax(0,1fr))}
  .kpi .v{font-size:20px;white-space:normal;overflow-wrap:anywhere}
  .kpi .l{overflow-wrap:anywhere}
  .strip{flex-direction:column;gap:10px}
  .edge{width:auto;padding:2px 0;align-items:flex-start}
  .edge .arw{transform:rotate(90deg)}
  .frame{min-height:0}
  .body,.flow-wrap,.single-wrap{padding:14px 12px}
  .pairs{grid-template-columns:minmax(0,1fr)}
  .pairs .k{border-right:0;border-bottom:1px solid var(--line-soft)}
  h1{font-size:16px}
  .nav{gap:4px;flex-wrap:wrap;overflow-x:visible}
  .nav span{padding:4px 8px;font-size:12.5px}
}
@media (max-width:520px){
  .row .meta{display:none}
  .chip em{display:none}
  .kpi{grid-template-columns:minmax(0,1fr)}
  .kpi div{flex-wrap:wrap;gap:2px 10px}
  .kpi .v{font-size:18px;white-space:normal;overflow-wrap:anywhere}
  .kpi .l{overflow-wrap:anywhere}
}
"""

CSS = """
:root{--ink:#1f2328;--ink-2:#57606a;--line:#d0d7de;--line-soft:#eaeef2;--bg:#fff;--bg-2:#f6f8fa;--accent:#0969da}
*{box-sizing:border-box}
body{margin:0;background:var(--bg-2);color:var(--ink);font:14px/1.55 -apple-system,"Segoe UI",system-ui,sans-serif}
.frame{max-width:1440px;width:100%;margin:0 auto;background:var(--bg);border:1px solid var(--line);min-height:900px}
.chrome{display:flex;align-items:center;gap:10px;padding:8px 14px;background:var(--bg-2);border-bottom:1px solid var(--line);font-size:12px;color:var(--ink-2);flex-wrap:wrap}
.chrome b{color:var(--ink);font-size:13px}
.chrome .dot{width:9px;height:9px;border-radius:50%;background:var(--line);display:inline-block}
.badge{display:inline-block;padding:1px 7px;border:1px solid var(--line);border-radius:999px;background:#fff;font-size:11px;color:var(--ink-2);margin-left:4px}
.badge.req{color:var(--accent);border-color:#b6d4f7}
.topbar{display:flex;align-items:center;gap:18px;padding:12px 20px;border-bottom:1px solid var(--line-soft);flex-wrap:wrap}
.brand{font-weight:600;letter-spacing:.2px}
.nav{display:flex;gap:6px;flex-wrap:nowrap;overflow-x:auto;max-width:100%;min-width:0;scrollbar-width:none;-ms-overflow-style:none}
.nav::-webkit-scrollbar{display:none}
.nav span{padding:4px 10px;border-radius:6px;color:var(--ink-2);white-space:nowrap;flex:0 0 auto}
.nav span.on{background:var(--bg-2);color:var(--ink);font-weight:600}
.body{padding:20px 20px 28px}
h1{font-size:17px;margin:0 0 14px;letter-spacing:.2px}
.kpi{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.kpi div{border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:#fff;min-width:0;display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.kpi .l{font-size:12px;color:var(--ink-2)}
.kpi .v{font-size:22px;font-weight:600;margin-top:0;white-space:nowrap}
.panel{border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-bottom:16px}
.panel h2{font-size:13px;margin:0;padding:10px 14px;background:var(--bg-2);border-bottom:1px solid var(--line-soft);color:var(--ink-2);font-weight:600}
.row{display:flex;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--line-soft)}
.row:last-child{border-bottom:0}
.row .r{color:var(--ink-2);font-variant-numeric:tabular-nums;white-space:nowrap}
.row .mid{min-width:0}
.row .meta{margin-top:3px;font-size:12px;color:var(--ink-2);line-height:1.4}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:6px 11px;border:1px solid var(--line);border-radius:999px;background:#fff;font-size:13px}
.chip em{font-style:normal;font-size:12px;color:var(--ink-2)}
.chip.on{border-color:var(--accent);color:var(--accent);background:#f5f9ff}
.pairs{display:grid;grid-template-columns:120px minmax(0,1fr);gap:0}
.pairs .k{padding:9px 14px;background:var(--bg-2);color:var(--ink-2);border-bottom:1px solid var(--line-soft);border-right:1px solid var(--line-soft)}
.pairs .v{padding:9px 14px;border-bottom:1px solid var(--line-soft)}
.flows{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.flow-btn{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border:1px solid var(--accent);border-radius:6px;color:var(--accent);text-decoration:none;background:#f5f9ff;font-size:13px}
.flow-btn:hover{background:#e8f2ff}
.flow-btn small{color:var(--ink-2)}
.crumb{font-size:12px;color:var(--ink-2);padding:8px 20px;background:var(--bg-2);border-bottom:1px solid var(--line-soft)}
/* 总览 */
.flow-wrap{padding:18px}
.flow-title{font-size:15px;margin:0 0 4px}
.flow-hint{font-size:12px;color:var(--ink-2);margin:0 0 16px}
.strip{display:flex;flex-wrap:wrap;align-items:stretch;gap:0}
.col{flex:1 1 130px;min-width:130px}
.thumb{border:1px solid var(--line);border-radius:8px;background:#fff;overflow:hidden}
.thumb .cap{padding:7px 10px;background:var(--bg-2);border-bottom:1px solid var(--line-soft);font-size:12px;font-weight:600}
.thumb .mini{padding:10px;min-height:150px;display:flex;flex-direction:column;gap:6px}
.mini .bar{height:8px;border-radius:2px;background:var(--line-soft)}
.mini .bar.w60{width:60%}.mini .bar.w40{width:40%}.mini .box{height:52px;border:1px dashed var(--line);border-radius:6px}
.edge{width:150px;flex:0 0 150px;padding:0 8px;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;color:var(--ink-2);font-size:12px;gap:2px}
.edge .arw{font-size:18px;color:var(--accent);line-height:1}
.who{display:none}
body[data-view="single"] .flow-wrap{display:none}
body[data-view="flow"] .single-wrap{display:none}
.single-wrap{padding:18px}
.foot{font-size:11px;color:var(--ink-2);padding:10px 20px;border-top:1px solid var(--line-soft);background:var(--bg-2)}
"""

ROUTER_JS = """
(function(){
  var spec = window.__PROTO__;
  function parse(){ var h = location.hash.replace(/^#\\/?/, ''); return h || 'flow'; }
  function render(){
    var v = parse();
    var isFlow = (v === 'flow' || v === '');
    document.body.dataset.view = isFlow ? 'flow' : 'single';
    var target = isFlow ? 'flow-wrap' : 'screen-' + v;
    var el = document.getElementById(target);
    if (!el) { document.body.dataset.view = 'flow'; return; }
    document.querySelectorAll('[data-view-box]').forEach(function(n){ n.style.display = 'none'; });
    el.style.display = '';
    document.title = (isFlow ? '主流程总览' : (el.getAttribute('data-title') || v)) + ' · ' + spec.project.name;
    window.__CURRENT_VIEW__ = isFlow ? 'flow' : v;
  }
  window.addEventListener('hashchange', render);
  window.addEventListener('DOMContentLoaded', render);
  document.addEventListener('click', function(e){
    var a = e.target.closest && e.target.closest('.flow-btn');
    if (!a) return;
    e.preventDefault();
    location.hash = '#/' + a.getAttribute('data-flow-to');
  });
  render();
})();
"""

BLOCK_RENDERERS = {}


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def block_topbar(block, ctx) -> str:
    navs = "".join(
        '<span class="%s">%s</span>' % ("on" if n == block.get("active") else "", esc(n))
        for n in block.get("nav", [])
    )
    return '<div class="topbar"><div class="brand">%s</div><div class="nav">%s</div></div>' % (
        esc(block.get("brand", ctx["project"])),
        navs,
    )


def block_kpi(block, ctx) -> str:
    cells = "".join(
        '<div><div class="l">%s</div><div class="v">%s</div></div>' % (esc(i.get("label")), esc(i.get("value")))
        for i in block.get("items", [])
    )
    return '<div class="kpi">%s</div>' % cells


def block_list(block, ctx) -> str:
    """列表。rows 项支持 {left, right, meta[]}：meta 是行内次要信息（负责人/优先级/截止等），
    窄档（<=520px）由 CSS 隐藏，避免 375 档每行折成三行。"""
    rows = []
    for r in block.get("rows", []):
        meta = r.get("meta") or []
        meta_html = ('<div class="meta">%s</div>' % esc(" · ".join(str(m) for m in meta))) if meta else ""
        rows.append(
            '<div class="row"><div class="mid"><span>%s</span>%s</div><span class="r">%s</span></div>'
            % (esc(r.get("left")), meta_html, esc(r.get("right")))
        )
    return '<div class="panel"><h2>%s</h2>%s</div>' % (esc(block.get("title", "列表")), "".join(rows))


def block_filters(block, ctx) -> str:
    """筛选条：静态展示"当前生效的筛选值"（低保真原型不承载真实交互）。
    items 项为 {label, value, active?}；active=true 高亮为已启用。"""
    chips = []
    for item in block.get("items", []):
        on = " on" if item.get("active") else ""
        chips.append(
            '<span class="chip%s"><em>%s</em>%s</span>'
            % (on, esc(item.get("label", "")), esc(item.get("value", "")))
        )
    return '<div class="chips">%s</div>' % "".join(chips)


def block_detail(block, ctx) -> str:
    body = "".join(
        '<div class="k">%s</div><div class="v">%s</div>' % (esc(k), esc(v)) for k, v in block.get("pairs", [])
    )
    return '<div class="panel"><h2>%s</h2><div class="pairs">%s</div></div>' % (
        esc(block.get("title", "详情")),
        body,
    )


def block_actions(block, ctx) -> str:
    buttons = []
    flows = {f["id"]: f for f in ctx["flows"]}
    for item in block.get("items", []):
        flow = flows.get(item.get("flow"))
        if flow:
            buttons.append(
                '<a class="flow-btn" href="#/%s" data-flow-to="%s" data-flow="%s" data-req="%s">%s <small>(%s)</small></a>'
                % (
                    esc(flow["to"]),
                    esc(flow["to"]),
                    esc(flow["id"]),
                    esc(flow["req"]),
                    esc(item.get("label") or flow["action"]),
                    esc(flow["req"]),
                )
            )
        else:
            buttons.append('<span class="flow-btn"><span>%s</span></span>' % esc(item.get("label")))
    return '<div class="flows">%s</div>' % "".join(buttons)


BLOCK_RENDERERS.update(
    topbar=block_topbar,
    kpi=block_kpi,
    list=block_list,
    detail=block_detail,
    actions=block_actions,
    filters=block_filters,
)


def render_screen(screen, ctx) -> str:
    parts = []
    for block in screen.get("blocks", []):
        fn = BLOCK_RENDERERS.get(block.get("type"))
        if fn is None:
            raise ValueError("屏幕 %s 含未知 block 类型: %r" % (screen["id"], block.get("type")))
        parts.append(fn(block, ctx))

    # 该屏的出边：渲染成可点击按钮，并标注目标屏与 REQ
    outs = [f for f in ctx["flows"] if f["from"] == screen["id"]]
    if outs:
        links = "".join(
            '<a class="flow-btn" href="#/%s" data-flow-to="%s" data-flow="%s" data-req="%s">%s → %s <small>[%s]</small></a>'
            % (
                esc(f["to"]),
                esc(f["to"]),
                esc(f["id"]),
                esc(f["req"]),
                esc(f["action"]),
                esc(ctx["screen_by_id"][f["to"]]["title"]),
                esc(f["req"]),
            )
            for f in outs
        )
        parts.append('<h1>下一步（可点击）</h1><div class="flows">%s</div>' % links)

    header = (
        '<div class="chrome"><span class="dot"></span><span class="dot"></span><span class="dot"></span>'
        "<b>%s</b><span>·</span><span>%s</span>"
        '<span class="badge">%s</span><span class="badge req">%s</span></div>'
        % (
            esc(ctx["project"]),
            esc(screen["title"]),
            esc(screen.get("card", "")),
            esc(", ".join(screen.get("req", []))),
        )
    )
    return (
        '<div id="screen-%s" data-view-box data-title="%s" data-card="%s" data-req="%s" data-screen="%s" style="display:none">'
        '<div class="single-wrap"><div class="frame">%s<div class="body">%s</div></div></div></div>'
        % (
            esc(screen["id"]),
            esc(screen["title"]),
            esc(screen.get("card", "")),
            esc(",".join(screen.get("req", []))),
            esc(screen["id"]),
            header,
            "".join(parts),
        )
    )


def render_flow(ctx) -> str:
    screens = ctx["screens"]
    cols = []
    for i, screen in enumerate(screens):
        mini = "".join('<div class="bar w60"></div><div class="bar w40"></div><div class="box"></div>' for _ in range(1))
        cols.append(
            '<div class="col"><a class="thumb" href="#/%s" style="text-decoration:none;color:inherit"><div class="cap">%s</div>'
            '<div class="mini">%s</div></a>'
            '<div style="padding:6px 2px;font-size:11px;color:#57606a">%s<span class="badge">%s</span></div></div>'
            % (
                esc(screen["id"]),
                esc(screen["title"]),
                mini,
                esc(", ".join(screen.get("req", []))),
                esc(screen.get("card", "")),
            )
        )
        if i < len(screens) - 1:
            nxt = screens[i + 1]
            edge = next((f for f in ctx["flows"] if f["from"] == screen["id"] and f["to"] == nxt["id"]), None)
            label = esc(edge["action"]) if edge else "（无直连）"
            cols.append(
                '<div class="edge"><div class="arw">→</div><div>%s</div><div>%s</div></div>'
                % (label, esc(edge["req"]) if edge else "")
            )
    body = "".join(cols)
    return (
        '<div id="flow-wrap" data-view-box><div class="flow-wrap">'
        '<h1 class="flow-title">%s · 主流程总览</h1>'
        '<p class="flow-hint">共 %d 屏 / %d 条流转；点任一屏进入单屏视图，单屏内可点按钮跳转。此页可直接截主流程总图。</p>'
        '<div class="strip">%s</div>'
        '<p class="flow-hint" style="margin-top:14px">边缘说明：箭头仅表示主流程方向；异常/回退流转见各屏「下一步」按钮。</p>'
        "</div></div>"
        % (esc(ctx["project"]), len(ctx["screens"]), len(ctx["flows"]), body)
    )


def build(spec: dict) -> str:
    required = ["project", "requirements", "cards", "screens", "flows"]
    missing = [k for k in required if k not in spec]
    if missing:
        raise ValueError("spec 缺少顶层字段: %s" % ",".join(missing))
    ctx = {
        "project": spec["project"].get("name", "原型"),
        "version": spec["project"].get("version", "0.0"),
        "screens": spec["screens"],
        "flows": spec["flows"],
        "screen_by_id": {s["id"]: s for s in spec["screens"]},
    }
    views = render_flow(ctx) + "".join(render_screen(s, ctx) for s in ctx["screens"])
    payload = json.dumps(
        {"project": {"name": ctx["project"], "version": ctx["version"]}, "screens": [s["id"] for s in ctx["screens"]]},
        ensure_ascii=False,
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>%s · 低保真原型 v%s</title><style>%s</style><style>%s</style></head><body data-view=\"flow\">"
        "<script>window.__PROTO__=%s;</script>%s<script>%s</script></body></html>\n"
        % (esc(ctx["project"]), esc(ctx["version"]), CSS, MEDIA_CSS, payload, views, ROUTER_JS)
    )


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("用法: python -X utf8 render_proto.py <spec.json> <out.html>")
        return 2
    spec_path, out_path = Path(argv[1]), Path(argv[2])
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print("读取 spec 失败: %s" % exc)
        return 2
    try:
        doc = build(spec)
    except (ValueError, KeyError, TypeError) as exc:
        print("渲染失败: %s" % exc)
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8", newline="\n")
    print("OK %s 屏 / %s 流转 → %s（%d 字节）" % (
        len(spec["screens"]), len(spec["flows"]), out_path, len(doc.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
