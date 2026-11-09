# -*- coding: utf-8 -*-
"""UX 门 L2 运行时断言 + L3 像素基线（框架通用版 v5.17.0）。

- **L2**：起好的应用 + CDP **相对断言**（在视口内 / 命中的是自己 / 可点 / 状态能渲染 /
  几何一致 / iframe 不被裁 / 键位跨帧有效）。全部是"相对当前视口"的判断，
  不写死窗口尺寸——所以同一份条款在最小/默认/最大化下都能跑。
- **L3**：固定取样条件（尺寸 / DPR / 主题 / 字体 / 关动画）后截图，与基线逐像素比。
  **阈值纪律**：基线取样连跑 10 次取峰值 ×2 当阈值；`diff > 500 px` 或 `ratio > 0.05`
  视为**掩蔽**（说明取样条件没固定住，先查尺寸/DPR/主题/动画，别调阈值）。
  零依赖：PNG 解码用标准库 zlib，不装 Pillow / numpy / Playwright。

条款从 `clauses.json`（schema `ux-clauses/1.0`）来：规则名写在 `check_rule`，参数写在 `params`。
本脚本不含任何项目的端口 / 选择器 / 类名（全都从参数来）。

用法：
    # L2：连已启动应用的调试端口（端口谁给？——应用启动归各宿主/项目脚本，本工具不负责拉应用）
    python -X utf8 scripts/ux_gate/runtime_assert.py <项目根> --port 9333 --depth full --json-out 证据/ux/L2.json
    # 冒烟档只跑 窗口/导航/状态 三组（结论只能写"冒烟未见异常"）
    python -X utf8 scripts/ux_gate/runtime_assert.py <项目根> --port 9333 --depth smoke
    # L3：先采基线（连跑 10 次），再比对
    python -X utf8 scripts/ux_gate/runtime_assert.py <项目根> --port 9333 --baseline 基线/ux --baseline-capture
    python -X utf8 scripts/ux_gate/runtime_assert.py <项目根> --port 9333 --baseline 基线/ux

退出码：0 = 无 fail；1 = 有 fail；2 = 用法/条款文件错误。
`skipped` 不计失败，但必须带原因（未实现的规则 / 无基线 / 未装依赖，都不许静默当通过）。
"""
import argparse
import json
import os
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:  # 包内导入（被别的脚本 import）
    from .cdp_client import One
    from .static_check import DEFAULT_CLAUSES, load_clauses, preflight
except (ImportError, ValueError):  # 直接当脚本跑
    from cdp_client import One
    from static_check import DEFAULT_CLAUSES, load_clauses, preflight

OUT_SCHEMA = "ux-gate-runtime/1.0"
DEFAULT_DIFF_PIXELS = 500      # 超过 = 掩蔽（不是"真差异"，是取样条件没固定）
DEFAULT_DIFF_RATIO = 0.05      # 同上，按像素占比
DEFAULT_AA_THRESHOLD = 0.1     # pixelmatch 语义：单通道差 ≤ 25.5/255 视为抗锯齿噪声


# ---------------------------------------------------------------- L3 像素底座
def png_read(path):
    """读 8bit 非隔行 PNG，返回 (宽, 高, 通道数, 行列表)。纯标准库。"""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG：%s" % path)
    pos, idat = 8, b""
    width = height = depth = color = interlace = None
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, _comp, _filt, interlace = struct.unpack(">IIBBBBB", chunk[:13])
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if width is None:
        raise ValueError("PNG 缺 IHDR：%s" % path)
    if depth != 8 or interlace != 0:
        raise ValueError("只支持 8bit 非隔行 PNG（实际 depth=%s interlace=%s）" % (depth, interlace))
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise ValueError("不支持的 PNG 颜色类型：%s" % color)
    raw = zlib.decompress(idat)
    bpp = channels
    stride = width * bpp
    rows, prev, i = [], bytearray(stride), 0
    for _y in range(height):
        ftype = raw[i]
        i += 1
        line = bytearray(raw[i:i + stride])
        i += stride
        if ftype == 1:      # Sub
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 0xFF
        elif ftype == 2:    # Up
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ftype == 3:    # Average
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:    # Paeth
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 0xFF
        elif ftype != 0:
            raise ValueError("未知 PNG 滤波类型 %d" % ftype)
        rows.append(line)
        prev = line
    return width, height, channels, rows


def png_diff(a_path, b_path, aa_threshold=DEFAULT_AA_THRESHOLD):
    """逐像素比两张 PNG；返回 {width, height, diff_pixels, diff_ratio}。尺寸不一致直接抛。"""
    wa, ha, ca, ra = png_read(a_path)
    wb, hb, cb, rb = png_read(b_path)
    if (wa, ha) != (wb, hb):
        raise ValueError("尺寸不一致：%dx%d vs %dx%d（取样条件没固定住，先查尺寸/DPR）"
                         % (wa, ha, wb, hb))
    cut = int(round(aa_threshold * 255))
    diff = 0
    for y in range(ha):
        la, lb = ra[y], rb[y]
        if ca != cb:  # 通道数不同 → 按 0 补齐后比
            raise ValueError("通道数不一致：%d vs %d" % (ca, cb))
        for x in range(0, wa * ca, ca):
            if max(abs(la[x + k] - lb[x + k]) for k in range(ca)) > cut:
                diff += 1
    return {"width": wa, "height": ha, "diff_pixels": diff,
            "diff_ratio": round(diff / float(wa * ha), 6)}


# ---------------------------------------------------------------- 规则库
RULES = {}


def rule(name, doc, defaults=None):
    def deco(fn):
        RULES[name] = {"name": name, "doc": doc, "defaults": defaults or {}, "fn": fn}
        return fn
    return deco


def _js_get(selector):
    return "document.querySelector(%s)" % json.dumps(selector)


def _need(p, *keys):
    """模板占位未填 → (True, 原因)，调用方应 return None 走 skipped（不当作通过）。"""
    empty = [k for k in keys if not p.get(k)]
    if empty:
        return True, "params.%s 未填（模板占位）" % " / ".join(empty)
    return False, ""


RECT_JS = ("(()=>{const e=%s;if(!e)return {missing:true};const r=e.getBoundingClientRect();"
           "return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height,"
           "vw:innerWidth,vh:innerHeight}})()")


@rule("in_viewport", "元素完整落在视口内（可满/可点/不被裁）",
      {"selector": "#app", "tolerance": 1})
def rt_in_viewport(page, p):
    bad, why = _need(p, "selector")
    if bad:
        return None, why
    tol = float(p.get("tolerance") or 1)
    r = page.ev(RECT_JS % _js_get(p["selector"]))
    if not r or r.get("missing"):
        return False, "找不到 %s" % p["selector"]
    ok = (r["left"] >= -tol and r["top"] >= -tol
          and r["right"] <= r["vw"] + tol and r["bottom"] <= r["vh"] + tol)
    return ok, "rect=(%d,%d,%d,%d) 视口=%dx%d" % (
        r["left"], r["top"], r["right"], r["bottom"], r["vw"], r["vh"])


@rule("hit_test", "元素中心点可点：elementFromPoint 命中的是它自己或其后代",
      {"selector": "#app"})
def rt_hit_test(page, p):
    bad, why = _need(p, "selector")
    if bad:
        return None, why
    r = page.ev(RECT_JS % _js_get(p["selector"]))
    if not r or r.get("missing"):
        return False, "找不到 %s" % p["selector"]
    cx, cy = (r["left"] + r["right"]) / 2.0, (r["top"] + r["bottom"]) / 2.0
    js = ("(()=>{const e=%s;const hit=document.elementFromPoint(%f,%f);"
          "return {hit:hit?(hit.id||hit.className||hit.tagName):null,self:!!hit&&(hit===e||e.contains(hit))}})()"
          % (_js_get(p["selector"]), cx, cy))
    out = page.ev(js)
    return bool(out and out.get("self")), "中心点命中 %s" % (
        out.get("hit") if out else "null")


@rule("no_h_overflow", "指定尺寸下无横向溢出（最小 / 默认 / 最大化都要过）",
      {"sizes": [{"width": 1024, "height": 700}, {"width": 1280, "height": 800}],
       "tolerance": 1})
def rt_no_h_overflow(page, p):
    tol = float(p.get("tolerance") or 1)
    bad = []
    for size in p["sizes"]:
        page.resize(size["width"], size["height"])
        page.ev("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        out = page.ev("({sw:document.documentElement.scrollWidth,"
                      "cw:document.documentElement.clientWidth,"
                      "bw:document.body?document.body.scrollWidth:0,"
                      "bvw:innerWidth})")
        if out["sw"] > out["cw"] + tol or out["bw"] > out["bvw"] + tol:
            bad.append("%dx%d: scrollWidth=%d > clientWidth=%d" % (
                size["width"], size["height"], out["sw"], out["cw"]))
    return (not bad), ("；".join(bad) if bad else "%d 档尺寸均无横向溢出" % len(p["sizes"]))


@rule("no_overlap", "两段文字/元素不重叠（标题与副标题挤行类）",
      {"a": "#title", "b": "#sub", "relation": "below", "tolerance": 1})
def rt_no_overlap(page, p):
    bad, why = _need(p, "a", "b")
    if bad:
        return None, why
    tol = float(p.get("tolerance") or 1)
    ra = page.ev(RECT_JS % _js_get(p["a"]))
    rb = page.ev(RECT_JS % _js_get(p["b"]))
    if not ra or ra.get("missing"):
        return False, "找不到 %s" % p["a"]
    if not rb or rb.get("missing"):
        return False, "找不到 %s" % p["b"]
    if p.get("relation") == "right":
        ok = ra["right"] <= rb["left"] + tol
        detail = "a.right=%d b.left=%d" % (ra["right"], rb["left"])
    else:
        ok = ra["bottom"] <= rb["top"] + tol
        detail = "a.bottom=%d b.top=%d" % (ra["bottom"], rb["top"])
    return ok, detail


@rule("persist", "状态持久化：写进去 → reload → 还在（主题/布局/筛选）",
      {"set_expr": "", "read_expr": "", "expected": "", "reload": 1})
def rt_persist(page, p):
    if not p.get("set_expr") or not p.get("read_expr"):
        return None, "params.set_expr / read_expr 未填（模板占位）"
    page.ev(p["set_expr"])
    for _i in range(int(p.get("reload") or 1)):
        page.call("Page.reload", {"ignoreCache": False})
        page.wait("document.readyState !== 'loading'", 10, "reload 完成")
        time.sleep(0.4)
    got = page.ev(p["read_expr"])
    want = p.get("expected")
    ok = (got == want) if want != "" else (got not in (None, "", False))
    return ok, "reload 后读到 %r（期望 %r）" % (got, want)


@rule("probe_persist", "几何/视图记忆：reload 前后探针值一致",
      {"probe_expr": "", "reloads": 2, "tolerance": 0})
def rt_probe_persist(page, p):
    if not p.get("probe_expr"):
        return None, "params.probe_expr 未填（模板占位）"
    before = page.ev(p["probe_expr"])
    for _i in range(int(p.get("reloads") or 1)):
        page.call("Page.reload", {"ignoreCache": False})
        page.wait("document.readyState !== 'loading'", 10, "reload 完成")
        time.sleep(0.4)
    after = page.ev(p["probe_expr"])
    tol = float(p.get("tolerance") or 0)
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        ok = abs(before - after) <= tol
    else:
        ok = before == after
    return ok, "reload 前 %r → 后 %r" % (before, after)


@rule("tab_dedupe", "去重：重复触发同一目标不会多开",
      {"action_expr": "", "count_expr": "", "times": 2, "allow_growth": 0})
def rt_tab_dedupe(page, p):
    if not p.get("action_expr") or not p.get("count_expr"):
        return None, "params.action_expr / count_expr 未填（模板占位）"
    base = page.ev(p["count_expr"])
    for _i in range(int(p.get("times") or 2)):
        page.ev(p["action_expr"])
        time.sleep(0.3)
    after = page.ev(p["count_expr"])
    allow = int(p.get("allow_growth") or 0)
    ok = isinstance(base, int) and isinstance(after, int) and after <= base + allow
    return ok, "重复触发后计数 %r → %r" % (base, after)


@rule("iframe_fit", "内嵌 iframe 不被底边裁切（高度用可用空间，不是 100vh）",
      {"selector": "iframe", "tolerance": 1})
def rt_iframe_fit(page, p):
    bad, why = _need(p, "selector")
    if bad:
        return None, why
    r = page.ev(RECT_JS % _js_get(p["selector"]))
    if not r or r.get("missing"):
        return False, "找不到 %s" % p["selector"]
    tol = float(p.get("tolerance") or 1)
    ok = r["height"] > 0 and r["bottom"] <= r["vh"] + tol
    return ok, "iframe bottom=%d 视口高=%d" % (r["bottom"], r["vh"])


@rule("key_probe", "键位跨帧有效：发真实按键后探针值按预期变化",
      {"setup_expr": "", "probe_expr": "", "key": {"key": "k", "ctrl": True}})
def rt_key_probe(page, p):
    if not p.get("probe_expr"):
        return None, "params.probe_expr 未填（模板占位）"
    if p.get("setup_expr"):
        page.ev(p["setup_expr"])
    before = page.ev(p["probe_expr"])
    key = p.get("key") or {"key": "k"}
    page.key(key.get("key", "k"), ctrl=key.get("ctrl", False), meta=key.get("meta", False),
             shift=key.get("shift", False), alt=key.get("alt", False))
    time.sleep(0.4)
    after = page.ev(p["probe_expr"])
    ok = before != after
    return ok, "按键后探针 %r → %r" % (before, after)


@rule("empty_state", "空态/非空态互斥：空列表时必须给空态，有数据时不得残留空态",
      {"list_expr": "", "empty_selector": ""})
def rt_empty_state(page, p):
    if not p.get("list_expr") or not p.get("empty_selector"):
        return None, "params.list_expr / empty_selector 未填（模板占位）"
    n = page.ev(p["list_expr"])
    seen = page.ev("!!document.querySelector(%s)" % json.dumps(p["empty_selector"]))
    if not isinstance(n, int):
        return False, "list_expr 没返回数量：%r" % (n,)
    if n == 0:
        return bool(seen), "列表为空，空态%s" % ("存在" if seen else "缺失")
    return (not seen), "列表有 %d 项，空态%s" % (n, "残留" if seen else "未残留")


@rule("text_present", "指定元素存在且含指定文本（超限提示 / 状态文案）",
      {"selector": "", "contains": ""})
def rt_text_present(page, p):
    if not p.get("selector"):
        return None, "params.selector 未填（模板占位）"
    out = page.ev("(()=>{const e=%s;return e?{text:(e.textContent||'').trim(),"
                  "visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}:null})()"
                  % _js_get(p["selector"]))
    if not out:
        return False, "找不到 %s" % p["selector"]
    want = p.get("contains") or ""
    ok = out["visible"] and (want in out["text"] if want else True)
    return ok, "文本=%r 可见=%s" % (out["text"][:60], out["visible"])


@rule("no_console_error", "本轮无未处理错误（console error / 未捕获异常）",
      {"seconds": 1.5, "allow": []})
def rt_no_console_error(page, p):
    logs = page.console_errors(float(p.get("seconds") or 1.5))
    bad = []
    for msg in logs:
        text = json.dumps(msg, ensure_ascii=False)
        if any(a in text for a in (p.get("allow") or [])):
            continue
        if (msg.get("method") == "Runtime.exceptionThrown"
                or '"level": "error"' in text or '"level":"error"' in text):
            bad.append(text[:160])
    return (not bad), ("；".join(bad[:3]) if bad else "无错误事件")


@rule("axe_scan", "可访问性严重级违例为 0（需要 --axe 指定的 axe.min.js）",
      {"impact": ["serious", "critical"]})
def rt_axe_scan(page, p):
    axe_src = page.__dict__.get("axe_source") or ""
    if not axe_src:
        return None, "未提供 --axe <axe.min.js>，本条跳过（不当作通过）"
    page.ev(axe_src)
    out = page.ev("axe.run(document, {resultTypes:['violations']}).then(r=>"
                  "r.violations.filter(v=>%s.includes(v.impact))"
                  ".map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.length})))"
                  % json.dumps(p.get("impact") or ["serious", "critical"]))
    if out is None:
        return False, "axe 没有返回结果（注入失败？）"
    return (not out), ("违例：%s" % json.dumps(out, ensure_ascii=False)[:300]) if out else "0 条严重级违例"


@rule("state_renders", "声明的状态都能渲染（空态 / 加载态 / 错误态的节点都在且可见）",
      {"selectors": []})
def rt_state_renders(page, p):
    if not p.get("selectors"):
        return None, "params.selectors 未填（模板占位）"
    missing = [s for s in p["selectors"] if not page.visible(s)]
    return (not missing), ("缺/不可见：%s" % "；".join(missing[:5])) if missing \
        else "%d 个状态节点可见" % len(p["selectors"])


@rule("input_kept", "状态切换后用户输入与当前视图不丢",
      {"input_selector": "", "value": "", "switch_expr": "", "back_expr": "", "read_expr": ""})
def rt_input_kept(page, p):
    if not p.get("read_expr"):
        return None, "params.read_expr 未填（模板占位）"
    if p.get("input_selector") and p.get("value"):
        page.ev("(()=>{const e=%s;if(!e)return;e.value=%s;"
                "e.dispatchEvent(new Event('input',{bubbles:true}));"
                "e.dispatchEvent(new Event('change',{bubbles:true}))})()"
                % (_js_get(p["input_selector"]), json.dumps(p["value"])))
    before = page.ev(p["read_expr"])
    if p.get("switch_expr"):
        page.ev(p["switch_expr"])
        time.sleep(0.4)
    if p.get("back_expr"):
        page.ev(p["back_expr"])
        time.sleep(0.4)
    after = page.ev(p["read_expr"])
    return before == after, "切换前后读到 %r → %r" % (before, after)


@rule("overlay_focus", "浮层焦点：打开即聚焦 → Tab 不逃逸 → Esc 可关且焦点归位触发元素",
      {"trigger_selector": "", "overlay_selector": "", "close_key": "Escape", "settle": 0.4})
def rt_overlay_focus(page, p):
    trig, ov = p.get("trigger_selector"), p.get("overlay_selector")
    if not trig or not ov:
        return None, "params.trigger_selector / overlay_selector 未填（模板占位）"
    if not page.click(trig):
        return False, "点不到触发元素 %s" % trig
    time.sleep(float(p.get("settle") or 0.4))
    if not page.visible(ov):
        return False, "浮层 %s 没打开" % ov
    focus_js = ("(()=>{const o=document.querySelector(%s),a=document.activeElement;"
                "return !!(o&&a&&(a===o||o.contains(a)))})()" % _js_get(ov))
    if not page.ev(focus_js):
        who = page.ev("document.activeElement ? (document.activeElement.tagName + '.' + "
                      "(document.activeElement.className || '')) : 'null'")
        return False, "打开后焦点不在浮层内（当前 %s）" % who
    page.key("Tab")
    time.sleep(0.25)
    if not page.ev(focus_js):
        return False, "Tab 一次后焦点逃出浮层"
    page.key(p.get("close_key") or "Escape")
    time.sleep(0.4)
    if page.visible(ov):
        return False, "%s 没关掉浮层" % (p.get("close_key") or "Escape")
    back_js = ("(()=>{const t=document.querySelector(%s),a=document.activeElement;"
               "return !!(t&&a&&(a===t||t.contains(a)))})()" % _js_get(trig))
    if not page.ev(back_js):
        return False, "关闭后焦点没回到触发元素（焦点归位失败）"
    return True, "打开聚焦 → Tab 不逃逸 → Esc 关闭 → 焦点归位"


@rule("submit_once", "连点两次只产生一次提交（提交按钮进 loading 后必须锁定）",
      {"button_selector": "", "count_expr": "", "expect_delta": 1, "settle": 0.8})
def rt_submit_once(page, p):
    btn, cnt = p.get("button_selector"), p.get("count_expr")
    if not btn or not cnt:
        return None, "params.button_selector / count_expr 未填（模板占位）"
    before = page.ev(cnt)
    if not page.click(btn):
        return False, "点不到提交按钮 %s" % btn
    time.sleep(0.15)
    page.click(btn)  # 连点第二下：没锁就会多一次
    time.sleep(float(p.get("settle") or 0.8))
    after = page.ev(cnt)
    if not isinstance(before, int) or not isinstance(after, int):
        return False, "count_expr 没返回数字：%r → %r" % (before, after)
    want = int(p.get("expect_delta") or 1)
    return (after - before == want), "连点两次产生 %d 次提交（应 %d 次）" % (after - before, want)


@rule("confirm_gate", "不可逆操作必须二次确认；取消后状态不变",
      {"action_selector": "", "confirm_selector": "", "cancel_selector": "", "effect_expr": ""})
def rt_confirm_gate(page, p):
    act, cfm = p.get("action_selector"), p.get("confirm_selector")
    if not act or not cfm:
        return None, "params.action_selector / confirm_selector 未填（模板占位）"
    before = page.ev(p["effect_expr"]) if p.get("effect_expr") else None
    if not page.click(act):
        return False, "点不到不可逆动作 %s" % act
    time.sleep(0.4)
    if not page.visible(cfm):
        return False, "点击后没出现确认层 %s" % cfm
    if p.get("cancel_selector"):
        if not page.click(p["cancel_selector"]):
            return False, "点不到取消按钮 %s" % p["cancel_selector"]
        time.sleep(0.4)
        if p.get("effect_expr"):
            after = page.ev(p["effect_expr"])
            if after != before:
                return False, "取消后状态变了：%r → %r" % (before, after)
    return True, "确认层出现%s" % ("，取消后状态未变" if p.get("cancel_selector") else "")


@rule("undo_window", "撤销窗口真实可用：执行 → 撤销 → 读回等于执行前",
      {"action_selector": "", "undo_selector": "", "read_expr": "", "expect_change": True})
def rt_undo_window(page, p):
    if not p.get("read_expr"):
        return None, "params.read_expr 未填（模板占位）"
    before = page.ev(p["read_expr"])
    if not page.click(p.get("action_selector") or ""):
        return False, "点不到执行元素 %s" % p.get("action_selector")
    time.sleep(0.5)
    mid = page.ev(p["read_expr"])
    if p.get("expect_change", True) and mid == before:
        return False, "执行后状态没变化，撤销无从验证（%r）" % (mid,)
    if not page.click(p.get("undo_selector") or ""):
        return False, "点不到撤销元素 %s" % p.get("undo_selector")
    time.sleep(0.5)
    after = page.ev(p["read_expr"])
    return after == before, "执行后 %r → 撤销后 %r（执行前 %r）" % (mid, after, before)


@rule("journey", "旅程脚本化复现：按步骤走到高潮节拍并断言关键状态",
      {"steps": [], "assert_expr": "", "expected": "", "settle": 0.4})
def rt_journey(page, p):
    steps = p.get("steps") or []
    if not steps or not p.get("assert_expr"):
        return None, "params.steps / assert_expr 未填（模板占位）"
    done = []
    for i, st in enumerate(steps, 1):
        try:
            if "click" in st:
                if not page.click(st["click"]):
                    return False, "第 %d 步点不到 %s" % (i, st["click"])
            elif "expr" in st:
                page.ev(st["expr"])
            elif "key" in st:
                page.key(st["key"])
            elif "wait" in st:
                page.wait(st["wait"], float(st.get("timeout") or 8), "第 %d 步" % i)
            time.sleep(float(st.get("sleep") or p.get("settle") or 0.4))
            done.append(str(i))
        except Exception as exc:  # noqa: BLE001
            return False, "第 %d 步异常：%s: %s" % (i, type(exc).__name__, exc)
    got = page.ev(p["assert_expr"])
    want = p.get("expected")
    ok = (got == want) if want != "" else bool(got)
    return ok, "走完 %d 步，断言读到 %r（期望 %r）" % (len(done), got, want)


@rule("recover_action", "失败路径给得出下一步动作（重试 / 返回 / 保留输入）",
      {"fail_expr": "", "fail_selector": "", "action_selectors": [], "wait": 0.8})
def rt_recover_action(page, p):
    if not p.get("action_selectors"):
        return None, "params.action_selectors 未填（模板占位）"
    if p.get("fail_selector"):
        if not page.click(p["fail_selector"]):
            return False, "点不到失败触发元素 %s" % p["fail_selector"]
    elif p.get("fail_expr"):
        page.ev(p["fail_expr"])
    else:
        return None, "params.fail_expr / fail_selector 至少填一个（模板占位）"
    time.sleep(float(p.get("wait") or 0.8))
    found = [s for s in p["action_selectors"] if page.visible(s)]
    return bool(found), ("可恢复动作：%s" % "；".join(found[:3])) if found \
        else "失败后没有任何可恢复动作（%d 个候选都不可见）" % len(p["action_selectors"])


# ---------------------------------------------------------------- 主流程
def _run_one(page, clause):
    name = clause.get("check_rule")
    entry = {"clause": clause["id"], "title": clause.get("title", ""),
             "layer": clause.get("layer"), "rule": name,
             "ok": False, "skipped": False, "detail": ""}
    spec = RULES.get(name)
    if spec is None:
        entry.update(skipped=True, detail="运行时规则未实现：%s（不当作通过）" % name)
        return entry
    params = dict(spec["defaults"])
    params.update(clause.get("params") or {})
    try:
        ok, detail = spec["fn"](page, params)
    except Exception as exc:  # noqa: BLE001 —— 断言崩 = fail，不许静默
        ok, detail = False, "断言异常：%s: %s" % (type(exc).__name__, exc)
    entry.update(ok=bool(ok) if ok is not None else False,
                 skipped=ok is None, detail=detail)
    return entry


def _pixel_clause(page, clause, base_dir, shot_dir, opts):
    entry = {"clause": clause["id"], "title": clause.get("title", ""),
             "layer": clause.get("layer", "L3"), "rule": "pixel_baseline",
             "ok": False, "skipped": False, "detail": ""}
    params = clause.get("params") or {}
    size = params.get("size") or {"width": 1280, "height": 800}
    page.resize(size["width"], size["height"], params.get("dpr") or 1.0)
    page.ev("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
    cur = os.path.join(shot_dir, "%s.png" % clause["id"]) if shot_dir else \
        os.path.join(base_dir, "current", "%s.png" % clause["id"])
    page.shot(cur)
    base = os.path.join(base_dir, "base", "%s.png" % clause["id"])
    if opts["capture"]:
        peak_path = os.path.join(base_dir, "peak.json")
        peaks = {}
        if os.path.isfile(peak_path):
            with open(peak_path, "r", encoding="utf-8") as fh:
                peaks = json.load(fh)
        shots = [cur]
        for i in range(max(1, opts["capture_times"]) - 1):
            extra = os.path.join(base_dir, "samples", "%s.%02d.png" % (clause["id"], i))
            page.shot(extra)
            shots.append(extra)
        peak_px, peak_ratio = 0, 0.0
        for i in range(len(shots) - 1):
            d = png_diff(shots[i], shots[i + 1])
            peak_px = max(peak_px, d["diff_pixels"])
            peak_ratio = max(peak_ratio, d["diff_ratio"])
        os.makedirs(os.path.join(base_dir, "base"), exist_ok=True)
        with open(base, "wb") as dst, open(shots[0], "rb") as src:
            dst.write(src.read())
        peaks[clause["id"]] = {"peak_pixels": peak_px, "peak_ratio": peak_ratio,
                               "samples": len(shots),
                               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(peak_path, "w", encoding="utf-8") as fh:
            json.dump(peaks, fh, ensure_ascii=False, indent=2)
        masked = peak_px > opts["mask_pixels"] or peak_ratio > opts["mask_ratio"]
        entry.update(ok=not masked,
                     detail="基线已采（%d 次）：峰值 %d px / %.4f%s" % (
                         len(shots), peak_px, peak_ratio,
                         "；**掩蔽**（取样条件没固定住：尺寸/DPR/主题/动画）" if masked else ""))
        return entry
    if not os.path.isfile(base):
        entry.update(skipped=True, detail="无基线：%s（先跑 --baseline-capture）" % base)
        return entry
    try:
        diff = png_diff(cur, base)
    except Exception as exc:  # noqa: BLE001
        entry.update(skipped=True, detail="基线不可比：%s" % exc)
        return entry
    peaks = {}
    peak_path = os.path.join(base_dir, "peak.json")
    if os.path.isfile(peak_path):
        with open(peak_path, "r", encoding="utf-8") as fh:
            peaks = json.load(fh)
    peak = peaks.get(clause["id"]) or {}
    limit_px = max(2 * int(peak.get("peak_pixels") or 0), int(opts["min_diff_pixels"]))
    limit_ratio = max(2 * float(peak.get("peak_ratio") or 0.0), 0.01)
    masked = (diff["diff_pixels"] > opts["mask_pixels"]
              or diff["diff_ratio"] > opts["mask_ratio"])
    ok = (not masked) and diff["diff_pixels"] <= limit_px and diff["diff_ratio"] <= limit_ratio
    entry.update(ok=ok, detail="差异 %d px / %.4f（阈值 %d px %.4f；%s）" % (
        diff["diff_pixels"], diff["diff_ratio"], limit_px, limit_ratio,
        "掩蔽：差异过大，先查取样条件" if masked else "未超阈值" if ok else "超阈值"))
    return entry


def run(root, clauses_path, page, depth="full", baseline="", capture=False,
        shot_dir="", axe_source="", opts=None):
    _data, clauses = load_clauses(clauses_path)
    problems = preflight(clauses)
    opts = opts or {}
    results = []
    for c in clauses:
        kind = c.get("check_kind")
        groups = c.get("groups") or []
        if depth == "smoke" and groups and not (set(groups) & {"window", "nav", "status"}):
            continue
        if kind == "runtime":
            results.append(_run_one(page, c))
        elif kind == "pixel":
            if not baseline:
                results.append({"clause": c["id"], "title": c.get("title", ""),
                                "layer": "L3", "rule": "pixel_baseline", "ok": False,
                                "skipped": True, "detail": "未给 --baseline，L3 未跑（不当作通过）"})
            else:
                results.append(_pixel_clause(page, c, baseline, shot_dir, opts))
    failed = [r for r in results if not r["ok"] and not r["skipped"]]
    return {
        "schema": OUT_SCHEMA,
        "root": os.path.abspath(root),
        "clauses_file": os.path.abspath(clauses_path),
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "depth": depth,
        "baseline": os.path.abspath(baseline) if baseline else "",
        "ok": not failed and not problems,
        "total": len(results),
        "passed": len([r for r in results if r["ok"]]),
        "failed": len(failed),
        "skipped": len([r for r in results if r["skipped"]]),
        "preflight": problems,
        "results": results,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="UX 门 L2 运行时断言 + L3 像素基线")
    ap.add_argument("root", nargs="?", default=".", help="项目根（含 project-state/ 的目录）")
    ap.add_argument("--clauses", default="", help="条款文件；默认 <根>/%s" % DEFAULT_CLAUSES)
    ap.add_argument("--port", type=int, default=0, help="CDP 调试端口（必须由宿主/项目脚本先启动应用）")
    ap.add_argument("--app-port", type=int, default=0, help="可选：应用自身 HTTP 端口（api 探针用）")
    ap.add_argument("--url-sub", default="", help="只连 URL 含该子串的 page 目标")
    ap.add_argument("--wait", type=float, default=15.0, help="等 CDP 目标出现的秒数")
    ap.add_argument("--depth", default="full", choices=["smoke", "targeted", "full"],
                    help="smoke = 只跑 窗口/导航/状态 三组")
    ap.add_argument("--baseline", default="", help="L3 基线目录（base/ 放基线，peak.json 放峰值）")
    ap.add_argument("--baseline-capture", action="store_true", help="采基线（连跑 N 次取峰值）")
    ap.add_argument("--capture-times", type=int, default=10, help="采基线的连跑次数（默认 10）")
    ap.add_argument("--shot-dir", default="", help="本轮截图落盘目录（证据）")
    ap.add_argument("--axe", default="", help="axe.min.js 路径（可访问性条款需要）")
    ap.add_argument("--max-diff-pixels", type=int, default=DEFAULT_DIFF_PIXELS,
                    help="超过即判「掩蔽」（默认 500）")
    ap.add_argument("--max-diff-ratio", type=float, default=DEFAULT_DIFF_RATIO,
                    help="超过即判「掩蔽」（默认 0.05）")
    ap.add_argument("--min-diff-pixels", type=int, default=50, help="通过下限（默认 50 px）")
    ap.add_argument("--json-out", default="", help="结果写 JSON（证据落盘用）")
    ap.add_argument("--list-rules", action="store_true", help="列出内置运行时规则")
    args = ap.parse_args(argv)

    if args.list_rules:
        for name in sorted(RULES):
            print("%-20s %s" % (name, RULES[name]["doc"]))
        return 0

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("项目根不存在：%s" % root, file=sys.stderr)
        return 2
    clauses_path = args.clauses or os.path.join(root, *DEFAULT_CLAUSES.split("/"))
    if not os.path.isfile(clauses_path):
        print("条款文件不存在：%s" % clauses_path, file=sys.stderr)
        return 2
    if not args.port:
        print("必须给 --port（应用需先以 --remote-debugging-port=<port> 启动）", file=sys.stderr)
        return 2

    page = One(args.port, args.url_sub, args.app_port, wait=args.wait)
    if args.axe and os.path.isfile(args.axe):
        with open(args.axe, "r", encoding="utf-8", errors="replace") as fh:
            page.axe_source = fh.read()

    shot_dir = args.shot_dir or os.path.join(root, "project-state", "证据", "ux")
    os.makedirs(shot_dir, exist_ok=True)
    baseline = args.baseline or ""
    if baseline:
        os.makedirs(baseline, exist_ok=True)
    opts = {"capture": bool(args.baseline_capture), "capture_times": args.capture_times,
            "mask_pixels": args.max_diff_pixels, "mask_ratio": args.max_diff_ratio,
            "min_diff_pixels": args.min_diff_pixels}

    report = run(root, clauses_path, page, args.depth, baseline,
                 bool(args.baseline_capture), shot_dir,
                 getattr(page, "axe_source", ""), opts)

    for r in report["results"]:
        tag = "skip" if r["skipped"] else ("ok  " if r["ok"] else "FAIL")
        print("[%s] %-12s %-4s %-20s %s" % (tag, r["clause"], r["layer"], r["rule"], r["detail"]))
    for p in report["preflight"]:
        print("[FAIL] 条款自检：%s" % p)
    print("==== L2/L3 运行时 %d/%d 通过，FAIL %d，SKIP %d（深度 %s%s）====" % (
        report["passed"], report["total"], report["failed"], report["skipped"],
        report["depth"], "，基线模式" if args.baseline_capture else ""))

    out = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        folder = os.path.dirname(os.path.abspath(args.json_out))
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(out)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
