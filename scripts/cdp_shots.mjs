#!/usr/bin/env node
/**
 * cdp_shots.mjs —— 用 Chrome/Edge 的 DevTools Protocol 精确出图（零依赖，Node 22 内置 WebSocket）
 *
 * 为什么要 CDP 而不是 `chrome --screenshot --window-size`：
 *   --window-size 是**窗口**尺寸，不是**布局视口**。实测 320 档布局仍按 500px 算，
 *   右侧被裁掉（同一份页面在 CDP 下 innerWidth=320、CLI 下是 500）。
 *   凡是"四档断点截图作为验收证据"的场景，必须用 Emulation.setDeviceMetricsOverride 精确控视口。
 *
 * 产物（与门禁 scripts/ui_design_lint.py、scripts/prototype_lint.py 的约定对齐）：
 *   <out-dir>/<prefix>_<w>.png         视口图（宽高精确 = w × height）
 *   <out-dir>/<prefix>_full_<w>.png    整页长图（captureBeyondViewport，可选）
 *   <out-dir>/probes.json              每档机器判据（innerWidth/文档宽高/令牌回读/横向溢出/状态清单）
 *   <out-dir>/manifest.json            产物 sha256 + 源 html/design 的 sha256（防 stale）
 *   两者按 prefix 合并写入，可对同一目录反复追加多个页面。
 *
 * 用法：
 *   node cdp_shots.mjs --html <页面.html> --out-dir <shots 目录> --prefix <页面名> \
 *        [--widths 320,768,1024,1440] [--height 800] [--routes flow,screen-a] \
 *        [--design-md <DESIGN.md>] [--no-full] [--browser <chrome.exe>] [--json]
 *   --routes 用于原型：逐条 hash 路由截图（<prefix>_<route>_<w>.png）。
 *
 * 退出码：0 全部成功 / 1 有失败 / 2 用法或环境错误
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const EXIT = { ok: 0, fail: 1, usage: 2 };

const BROWSER_CANDIDATES = [
  process.env.CDP_CHROME || '',
  process.env.UI_SHOTS_BROWSER || '',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
];

function usage(msg) {
  if (msg) console.error('cdp_shots: ' + msg);
  console.error('用法: node cdp_shots.mjs --html <页面.html> --out-dir <目录> --prefix <名字> [--widths ...] [--height 800] [--routes a,b] [--design-md <DESIGN.md>] [--no-full] [--browser <exe>] [--json]');
  process.exit(EXIT.usage);
}

function parseArgs(argv) {
  const out = { widths: [320, 768, 1024, 1440], height: 800, routes: [], full: true, json: false, scheme: 'dark', suffix: '' };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const need = () => {
      const v = argv[++i];
      if (v === undefined) usage('缺少参数值: ' + a);
      return v;
    };
    switch (a) {
      case '--html': out.html = need(); break;
      case '--out-dir': out.outDir = need(); break;
      case '--prefix': out.prefix = need(); break;
      case '--widths': out.widths = need().split(',').map((s) => Number(s.trim())).filter((n) => n > 0); break;
      case '--height': out.height = Number(need()); break;
      case '--routes': out.routes = need().split(',').map((s) => s.trim()).filter(Boolean); break;
      case '--design-md': out.designMd = need(); break;
      case '--scheme': out.scheme = need(); break;
      case '--suffix': out.suffix = need(); break;
      case '--browser': out.browser = need(); break;
      case '--no-full': out.full = false; break;
      case '--json': out.json = true; break;
      default: usage('不认识的参数: ' + a);
    }
  }
  if (!out.html) usage('必须给 --html');
  if (!out.outDir) usage('必须给 --out-dir');
  if (!out.prefix) out.prefix = path.basename(out.html, path.extname(out.html));
  if (!out.widths.length) usage('--widths 至少一个正数');
  if (!out.height || out.height < 200) usage('--height 至少 200');
  return out;
}

function findBrowser(explicit) {
  if (explicit) {
    if (!existsSync(explicit)) usage('指定的浏览器不存在: ' + explicit);
    return explicit;
  }
  for (const c of BROWSER_CANDIDATES) if (c && existsSync(c)) return c;
  usage('找不到 Chrome/Edge，用 --browser 指定可执行文件');
}

const sha256 = (p) => createHash('sha256').update(readFileSync(p)).digest('hex');

function readJson(p, fallback) {
  try { return JSON.parse(readFileSync(p, 'utf8')); } catch { return fallback; }
}

/** 页内探针：纯 JSON 可序列化，供门禁判数（不是给人看的） */
const PROBE_EXPR = `(() => {
  const root = document.documentElement;
  const cs = getComputedStyle(root);
  // 从 <style id="design-tokens"> 里取声明过的令牌名，再回读计算值（声明≠生效，所以要回读）
  const tkEl = document.getElementById('design-tokens');
  const tokens = {};
  if (tkEl) {
    const txt = tkEl.textContent || '';
    const re = /(--[A-Za-z0-9_-]+)\\s*:\\s*([^;{}]+)/g;
    let m;
    while ((m = re.exec(txt))) tokens[m[1]] = cs.getPropertyValue(m[1]).trim();
  }
  const de = document.documentElement;
  const overflowEls = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && (r.right > innerWidth + 1 || r.left < -1)) {
      overflowEls.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.className && String(el.className).slice(0, 60)) || '',
        left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width),
      });
      if (overflowEls.length >= 20) break;
    }
  }
  const states = Array.from(document.querySelectorAll('[data-state]')).map((e) => e.getAttribute('data-state'));
  return JSON.stringify({
    innerWidth: innerWidth, innerHeight: innerHeight, dpr: devicePixelRatio,
    url: location.href,
    page: root.getAttribute('data-page'),
    spec: root.getAttribute('data-spec'),
    journey: root.getAttribute('data-journey'),
    route: (location.hash || '').replace(/^#\\/?/, ''),
    title: document.title,
    doc: { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth,
           scrollHeight: de.scrollHeight, clientHeight: de.clientHeight },
    bodyBg: getComputedStyle(document.body).backgroundColor,
    tokens, states,
    hasTokenBlock: !!tkEl,
    overflow: { horizontal: de.scrollWidth > de.clientWidth + 1, elements: overflowEls },
  });
})()`;

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = new Map();
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
        return;
      }
      if (msg.method) {
        const key = msg.method + (msg.sessionId ? ':' + msg.sessionId : '');
        const q = this.listeners.get(key);
        if (q && q.length) q.shift()(msg.params);
      }
    };
  }
  send(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++this.nextId;
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
  once(method, sessionId, timeoutMs = 20000) {
    return new Promise((resolve, reject) => {
      const key = method + (sessionId ? ':' + sessionId : '');
      const q = this.listeners.get(key) ?? [];
      const timer = setTimeout(() => reject(new Error('等待 ' + method + ' 超时')), timeoutMs);
      q.push((params) => { clearTimeout(timer); resolve(params); });
      this.listeners.set(key, q);
    });
  }
}

async function launchChrome(browser) {
  const ud = mkdtempSync(path.join(tmpdir(), 'cdp-shots-'));
  const proc = spawn(browser, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', '--disable-extensions', '--allow-file-access-from-files',
    `--user-data-dir=${ud}`, '--remote-debugging-port=0', '--window-size=1440,900', 'about:blank',
  ], { stdio: ['ignore', 'pipe', 'pipe'] });
  const wsUrl = await new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('等 DevTools 端口超时:\n' + buf)), 25000);
    proc.stderr.on('data', (chunk) => {
      buf += chunk.toString();
      const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m) { clearTimeout(timer); resolve(m[1]); }
    });
    proc.on('exit', (code) => { clearTimeout(timer); reject(new Error('浏览器提前退出 code=' + code + '\n' + buf)); });
  });
  return { proc, wsUrl };
}

async function shootAll(args, browser) {
  const { proc, wsUrl } = await launchChrome(browser);
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('WebSocket 连接失败')); });
  const cdp = new CDP(ws);
  try {
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
    await cdp.send('Page.enable', {}, sessionId);
    await cdp.send('Runtime.enable', {}, sessionId);

    // 色板模拟：--scheme dark|light|auto（auto = 不动系统偏好）。
    // 缺省 dark 保持历史出图口径不变；浅色模式靠它出对照图。
    const scheme = args.scheme || 'dark';
    if (scheme !== 'auto') {
      await cdp.send('Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-color-scheme', value: scheme }],
      }, sessionId);
    }

    const htmlAbs = path.resolve(args.html);
    const fileUrl = pathToFileURL(htmlAbs).href;
    const outDir = path.resolve(args.outDir);
    mkdirSync(outDir, { recursive: true });

    const routes = args.routes.length ? args.routes : [null];
    const probes = readJson(path.join(outDir, 'probes.json'), {});
    const manifest = readJson(path.join(outDir, 'manifest.json'), { version: 1, pages: {} });
    const pageKey = args.prefix + (args.suffix || '');
    const entry = { html: path.basename(htmlAbs), htmlSha256: sha256(htmlAbs), widths: {}, full: {}, probes: {} };
    if (args.designMd) {
      const dm = path.resolve(args.designMd);
      if (!existsSync(dm)) throw new Error('DESIGN.md 不存在: ' + dm);
      entry.designMd = path.basename(dm);
      entry.designMdSha256 = sha256(dm);
    }

    let failed = 0;
    for (const route of routes) {
      for (const width of args.widths) {
        await cdp.send('Emulation.setDeviceMetricsOverride',
          // mobile 必须为 false：true 会启用移动端视口模拟，页面没有 <meta viewport> 时
          // Chrome 会把布局宽退化成 980，截出来的 320 档变成 980×整页（实测踩过）。
          { width, height: args.height, deviceScaleFactor: 1, mobile: false }, sessionId);
        await cdp.send('Page.navigate', { url: 'about:blank' }, sessionId); // 清干净再进，保证 load 事件必发
        const loaded = cdp.once('Page.loadEventFired', sessionId);
        await cdp.send('Page.navigate', { url: route ? fileUrl + '#/' + route : fileUrl }, sessionId);
        await loaded;
        await new Promise((r) => setTimeout(r, 200));

        const { result } = await cdp.send('Runtime.evaluate',
          { expression: PROBE_EXPR, returnByValue: true, awaitPromise: false }, sessionId);
        const probe = JSON.parse(result.value);
        const probeKey = route ? `${route}@${width}` : String(width);
        probes[pageKey] = probes[pageKey] || {};
        probes[pageKey][probeKey] = probe;

        const nameOf = (kind) => route
          ? `${pageKey}_${route}_${width}${kind === 'full' ? '_full' : ''}.png`
          : `${pageKey}_${width}${kind === 'full' ? '_full' : ''}.png`;

        const vp = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true }, sessionId);
        const vpFile = path.join(outDir, nameOf('vp'));
        writeFileSync(vpFile, Buffer.from(vp.data, 'base64'));
        const push = (bucket, file, kind) => {
          bucket[route ? `${route}@${width}` : String(width)] = {
            file: path.basename(file), kind, bytes: statSync(file).size, sha256: sha256(file),
            expectWidth: width, expectHeight: kind === 'full' ? null : args.height,
          };
        };
        const vpBucket = route ? entry.routes ?? (entry.routes = {}) : entry.widths;
        push(vpBucket, vpFile, 'viewport');

        if (args.full) {
          const metrics = await cdp.send('Page.getLayoutMetrics', {}, sessionId);
          const size = metrics.cssContentSize ?? metrics.contentSize;
          const full = await cdp.send('Page.captureScreenshot', {
            format: 'png', fromSurface: true, captureBeyondViewport: true,
            clip: { x: 0, y: 0, width: size.width, height: size.height, scale: 1 },
          }, sessionId);
          const fullFile = path.join(outDir, nameOf('full'));
          writeFileSync(fullFile, Buffer.from(full.data, 'base64'));
          const fullBucket = route ? entry.fullRoutes ?? (entry.fullRoutes = {}) : entry.full;
          push(fullBucket, fullFile, 'full');
        }
        if (!args.json) {
          process.stdout.write(`  OK ${path.basename(nameOf('vp'))} ${probe.innerWidth}x${probe.innerHeight}` +
            (probe.overflow.horizontal ? `  [横向溢出 ${probe.doc.scrollWidth}>${probe.doc.clientWidth}]` : '') + '\n');
        }
      }
    }

    entry.routesCount = args.routes.length || 0;
    entry.scheme = scheme;
    entry.widthsList = args.widths;
    entry.height = args.height;
    entry.generatedAt = new Date().toISOString();
    manifest.pages[pageKey] = entry;
    writeFileSync(path.join(outDir, 'probes.json'), JSON.stringify(probes, null, 2) + '\n', 'utf8');
    writeFileSync(path.join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n', 'utf8');
    return { failed, outDir, pageKey, probes: probes[pageKey] };
  } finally {
    try { await cdp.send('Browser.close'); } catch { /* 忽略 */ }
    try { ws.close(); } catch { /* 忽略 */ }
    try { proc.kill(); } catch { /* 忽略 */ }
  }
}

const args = parseArgs(process.argv.slice(2));
const browser = findBrowser(args.browser);
shootAll(args, browser)
  .then((r) => {
    if (args.json) {
      console.log(JSON.stringify({
        ok: r.failed === 0, prefix: r.pageKey, outDir: r.outDir,
        shots: Object.keys(r.probes),
        innerWidths: Object.fromEntries(Object.entries(r.probes).map(([k, v]) => [k, v.innerWidth])),
      }, null, 2));
    } else {
      console.log(`CDP_OK ${r.pageKey}：${Object.keys(r.probes).length} 张 → ${r.outDir}`);
    }
    process.exit(r.failed === 0 ? EXIT.ok : EXIT.fail);
  })
  .catch((err) => {
    console.error('CDP_FAIL ' + (err && err.message ? err.message : err));
    process.exit(EXIT.fail);
  });
