# -*- coding: utf-8 -*-
"""零依赖 CDP 客户端（UX 门 L2/L3 的底座，框架通用版 v5.17.0）。

为什么自己写而不是引 Playwright：UX 门是**闸门**，闸门挂一堆外部依赖就会被跳过；
CDP 的 `--remote-debugging-port` 已经够用，WebSocket 握手 + 帧收发本文件 100 行搞定。
（口径沿 `02_方法论` 风险节：不引入 Playwright，省 300 MB。）

用法（命令行探针，调 selectors / 看真窗时用）：
    python -X utf8 scripts/ux_gate/cdp_client.py --port 9333 --url-sub "<URL 子串>" --eval "<js>"
    python -X utf8 scripts/ux_gate/cdp_client.py --port 9333 --eval "<js>" --shot out.png --json-out probe.json

被 `runtime_assert.py` 复用的编程接口：
    page = One(port=9333, url_sub="", app_port=0)
    page.ev("document.title")            # 求值（returnByValue）
    page.wait("!!document.querySelector('#app')", 10, "应用已挂载")
    page.nav("#/orders", 0.5)            # 改 hash 并等待 settle
    page.shot("证据/01.png")             # 截图（L3 取样的原料）
    page.api("/api/health")              # 顺带打同进程 HTTP（不要求存在）

**每条命令一条短连接**（v0.4 实测教训）：reload / 点击后长连接会卡死，
表现为 `ev()` 永远不返回、整轮门禁超时。宁可多握手几次，也不要把门禁挂在死连接上。

约定：所有函数不打印中文到 stdout 之外（Windows GBK 控制台下用
`python -X utf8` 调用，脚本内部一律 `ensure_ascii=False` + UTF-8 写文件）。
"""
import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

DEFAULT_WAIT = 10.0


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_targets(port, want_url_sub="", wait=DEFAULT_WAIT):
    """等 CDP 端口上出现 page 目标。`want_url_sub` 为空 = 不筛。"""
    end = time.time() + wait
    last = None
    while time.time() < end:
        try:
            items = http_json("http://127.0.0.1:%d/json/list" % port)
        except Exception as exc:  # noqa: BLE001 —— 端口没起来是常态，继续等
            last = exc
            time.sleep(0.4)
            continue
        pages = [t for t in items if t.get("type") == "page"]
        if want_url_sub:
            pages = [t for t in pages if want_url_sub in (t.get("url") or "")]
        if pages:
            return pages
        last = "no page target yet"
        time.sleep(0.4)
    raise SystemExit("no CDP page target on port %d (%s)" % (port, last))


class WS:
    """最小 WebSocket 客户端（只做 CDP 需要的那点事）。"""

    def __init__(self, ws_url, timeout=20):
        if not ws_url.startswith("ws://"):
            raise SystemExit("unsupported ws url: %r" % ws_url[:80])
        rest = ws_url[5:]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET /%s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("ws handshake closed")
            buf += chunk
        head, self.left = buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise SystemExit("ws handshake failed: %r" % head[:200])

    def _recv_exact(self, n):
        out = b""
        if self.left:
            take = min(n, len(self.left))
            out += self.left[:take]
            self.left = self.left[take:]
            n -= take
        while n > 0:
            chunk = self.sock.recv(min(65536, n))
            if not chunk:
                raise SystemExit("ws closed")
            out += chunk
            n -= len(chunk)
        return out

    def send(self, obj):
        data = json.dumps(obj).encode("utf-8")
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self):
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(length) if length else b""
            if opcode == 0x8:
                raise SystemExit("ws close frame")
            if opcode in (0x9, 0xA):
                continue
            if opcode in (0x1, 0x2):
                text = payload.decode("utf-8", "replace")
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if fin:
                    return msg
            # 分片帧不在本探针场景内，忽略

    def call(self, mid, method, params=None):
        self.send({"id": mid, "method": method, "params": params or {}})
        while True:
            msg = self.recv()
            if msg.get("id") == mid:
                return msg

    def close(self):
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


class One:
    """一次门禁轮次用的页面句柄：每条命令开一条短连接。"""

    def __init__(self, port, url_sub="", app_port=0, wait=DEFAULT_WAIT, timeout=20):
        self.port = int(port)
        self.url_sub = url_sub or ""
        self.app_port = int(app_port or 0)
        self.wait_s = float(wait)
        self.timeout = timeout
        self._mid = 0
        self._target = None

    # ---------------------------------------------------------------- 连接
    def target(self):
        if self._target is None:
            pages = wait_targets(self.port, self.url_sub, self.wait_s)
            self._target = pages[0]
        return self._target

    def call(self, method, params=None):
        """发一条 CDP 命令，返回 result 字典；协议错误抛 RuntimeError。"""
        ws = WS(self.target()["webSocketDebuggerUrl"], timeout=self.timeout)
        try:
            self._mid += 1
            msg = ws.call(self._mid, method, params or {})
        finally:
            ws.close()
        if "error" in msg:
            raise RuntimeError("%s -> %s" % (method, msg["error"].get("message")))
        return msg.get("result") or {}

    # ---------------------------------------------------------------- JS
    def ev(self, expr):
        r = self.call("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": True,
        })
        if r.get("exceptionDetails"):
            raise RuntimeError("JS 异常：%s" % str(r["exceptionDetails"])[:300])
        return (r.get("result") or {}).get("value")

    def wait(self, expr, timeout=8.0, label=""):
        """轮询到表达式为真；超时抛 RuntimeError（门禁里应记 fail 而不是崩）。"""
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                if self.ev(expr):
                    return True
            except RuntimeError as exc:  # 页面正在导航时求值会失败，重试
                last = exc
            time.sleep(0.2)
        raise RuntimeError("等待超时：%s（%s）" % (label or expr, last or "条件始终为假"))

    def nav(self, target_hash, settle=0.5):
        """改 hash 导航（SPA 常态）；给整段 URL 则改 location.href。"""
        if "://" in target_hash:
            self.ev("location.href = %s" % json.dumps(target_hash))
        else:
            self.ev("location.hash = %s" % json.dumps(target_hash))
        time.sleep(settle)

    def shot(self, path, clip=None):
        """截图并写盘；返回字节数。"""
        params = {"format": "png", "captureBeyondViewport": False}
        if clip:
            params["clip"] = clip
        r = self.call("Page.captureScreenshot", params)
        data = r.get("data")
        if not data:
            raise RuntimeError("截图失败：%r" % (r.get("error") or r))
        folder = os.path.dirname(os.path.abspath(path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))
        return os.path.getsize(path)

    # ---------------------------------------------------------------- 杂项
    def resize(self, width, height, dpr=1.0):
        """固定取样条件（L3 像素基线必须先固定尺寸/DPR，否则基线必然漂）。"""
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": int(width), "height": int(height),
            "deviceScaleFactor": float(dpr), "mobile": False,
        })

    def key(self, key, ctrl=False, meta=False, shift=False, alt=False, code=None):
        """发一次真实按键（Input.dispatchKeyEvent 三段式：rawKeyDown/keyUp）。"""
        mods = (8 if shift else 0) | (2 if ctrl else 0) | (4 if alt else 0) | (1 if meta else 0)
        vk = ord(key.upper()) if len(key) == 1 else 0
        base = {"key": key, "code": code or ("Key" + key.upper() if len(key) == 1 else key),
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": mods}
        self.call("Input.dispatchKeyEvent", dict(base, type="rawKeyDown"))
        self.call("Input.dispatchKeyEvent", dict(base, type="keyUp"))

    def click_at(self, x, y, button="left"):
        """真实鼠标点击（Input.dispatchMouseEvent 两段式）。JS 的 .click() 不搬焦点，浮层类条款必须走真点击。"""
        common = {"x": float(x), "y": float(y), "button": button, "clickCount": 1}
        self.call("Input.dispatchMouseEvent", dict(common, type="mousePressed"))
        time.sleep(0.02)
        self.call("Input.dispatchMouseEvent", dict(common, type="mouseReleased"))

    def click(self, selector):
        """按选择器取中心点后真实点击；返回是否点到（找不到元素 = False）。"""
        r = self.ev("(()=>{const e=document.querySelector(%s);if(!e)return null;"
                    "e.scrollIntoView({block:'center'});const b=e.getBoundingClientRect();"
                    "return {x:b.left+b.width/2,y:b.top+b.height/2}})()" % json.dumps(selector))
        if not r:
            return False
        self.click_at(r["x"], r["y"])
        return True

    def visible(self, selector):
        """元素存在且占位（offsetWidth/Height 或 getClientRects 非空）。"""
        return bool(self.ev("(()=>{const e=document.querySelector(%s);if(!e)return false;"
                            "return !!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)})()"
                            % json.dumps(selector)))

    def console_errors(self, seconds=1.0):
        """收一小段 console/exception（「不许有未处理错误」类条款用）。"""
        ws = WS(self.target()["webSocketDebuggerUrl"], timeout=max(2, int(seconds) + 1))
        out = []
        try:
            ws.call(1, "Runtime.enable")
            ws.call(2, "Log.enable")
            ws.sock.settimeout(0.25)
            end = time.time() + seconds
            while time.time() < end:
                try:
                    msg = ws.recv()
                except (socket.timeout, SystemExit):
                    continue
                if msg.get("method") in ("Runtime.consoleAPICalled", "Log.entryAdded",
                                         "Runtime.exceptionThrown"):
                    out.append(msg)
                time.sleep(0.02)
        finally:
            ws.close()
        return out

    def api(self, path, timeout=5):
        """同进程 HTTP（可选能力；没有该端点就返回 None，不算失败）。"""
        if not self.app_port:
            return None
        try:
            return http_json("http://127.0.0.1:%d%s" % (self.app_port, path), timeout=timeout)
        except Exception:  # noqa: BLE001
            return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="零依赖 CDP 探针")
    ap.add_argument("--port", type=int, required=True, help="CDP 调试端口（宿主自带，本工具不负责启动应用）")
    ap.add_argument("--url-sub", default="", help="只连 URL 含该子串的 page 目标；空 = 第一个")
    ap.add_argument("--wait", type=float, default=DEFAULT_WAIT, help="等目标出现的秒数")
    ap.add_argument("--eval", default="", help="要执行的 JS 表达式")
    ap.add_argument("--script-file", default="", help="从文件读 JS（长脚本用这个）")
    ap.add_argument("--shot", default="", help="截图落盘路径")
    ap.add_argument("--console", type=float, default=0.0, help="收 N 秒 console/exception")
    ap.add_argument("--json-out", default="", help="结果写 JSON")
    args = ap.parse_args(argv)

    page = One(args.port, args.url_sub, wait=args.wait)
    target = page.target()
    result = {"target": {"title": target.get("title"), "url": target.get("url")}}

    js = args.eval
    if args.script_file:
        with open(args.script_file, "r", encoding="utf-8") as fh:
            js = fh.read()
    if js:
        result["eval"] = page.ev(js)
    if args.shot:
        result["screenshot"] = {"path": args.shot, "bytes": page.shot(args.shot)}
    if args.console:
        result["console"] = page.console_errors(args.console)[:20]

    out = json.dumps(result, ensure_ascii=False, indent=2)
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
    print(out)


if __name__ == "__main__":
    main()
