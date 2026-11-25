// cdp.mjs —— 直连 Chrome DevTools Protocol 的最小桥（零依赖，Node 22 全局 WebSocket）
// 用法：
//   node cdp.mjs list
//   node cdp.mjs targets
//   node cdp.mjs where                 # 当前页面 url / title
//   node cdp.mjs goto <url>
//   node cdp.mjs eval  "<js>"
//   node cdp.mjs evalfile <path>
//   node cdp.mjs shot <path.png>
//   node cdp.mjs text                  # 页面 body.innerText
//   node cdp.mjs newtab [url]          # 新开自己的标签页，返回 index
//   node cdp.mjs pages                 # 列出所有 page 目标（含 index）
//   node cdp.mjs list                  # 所有 target（pageIdx=-1 表示非 page，不能定位）
// 环境变量：
//   CDP_PORT（默认 9222）
//   CDP_TARGET（指定 page 序号，默认 0）—— ⚠️ 标签开合时序号会漂
//   CDP_TARGET_URL（按 url 子串定位，更稳；与 CDP_TARGET 同时存在时优先）

const PORT = process.env.CDP_PORT || '9222';
const BASE = `http://127.0.0.1:${PORT}`;

async function targets() {
  const r = await fetch(`${BASE}/json/list`);
  if (!r.ok) throw new Error(`CDP /json/list HTTP ${r.status}`);
  return await r.json();
}

function pages(ts) {
  return ts.filter(t => t.type === 'page' && t.webSocketDebuggerUrl);
}

class CDP {
  constructor(wsUrl) { this.wsUrl = wsUrl; this._id = 0; this._p = new Map(); this._h = []; }
  on(fn) { this._h.push(fn); }
  open() {
    return new Promise((res, rej) => {
      const ws = new WebSocket(this.wsUrl);
      this.ws = ws;
      const t = setTimeout(() => rej(new Error('WS 连接超时')), 10000);
      ws.onopen = () => { clearTimeout(t); res(); };
      ws.onerror = () => { clearTimeout(t); rej(new Error('WS 连接失败')); };
      ws.onmessage = (ev) => {
        let msg; try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.id !== undefined) {
          const p = this._p.get(msg.id);
          if (!p) return;
          this._p.delete(msg.id);
          msg.error ? p.reject(new Error(JSON.stringify(msg.error))) : p.resolve(msg.result);
        } else if (msg.method) {
          for (const fn of this._h) { try { fn(msg); } catch {} }
        }
      };
      ws.onclose = () => {
        for (const [, p] of this._p) p.reject(new Error('WS 已关闭'));
        this._p.clear();
      };
    });
  }
  send(method, params = {}, timeoutMs = 45000) {
    const id = ++this._id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this._p.delete(id); reject(new Error('CDP 超时: ' + method)); }, timeoutMs);
      this._p.set(id, {
        resolve: v => { clearTimeout(timer); resolve(v); },
        reject: e => { clearTimeout(timer); reject(e); },
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

async function pickTarget() {
  const ts = await targets();
  const ps = pages(ts);
  if (!ps.length) throw new Error('没有可用的 page 目标（浏览器没开页面？）');
  // ★ CDP_TARGET_URL：按 url 子串定位（标签开合时 index 会漂，用 url 更稳）
  if (process.env.CDP_TARGET_URL) {
    const k = process.env.CDP_TARGET_URL;
    const hit = ps.find(t => (t.url || '').includes(k));
    if (!hit) throw new Error(`没有 url 含 "${k}" 的 page，现有：${ps.map(t => t.url).join(' | ')}`);
    return hit;
  }
  const idx = process.env.CDP_TARGET ? Number(process.env.CDP_TARGET) : 0;
  const t = ps[idx];
  if (!t) throw new Error(`page 目标 #${idx} 不存在，共 ${ps.length} 个`);
  return t;
}

async function evaluate(expr) {
  const t = await pickTarget();
  const c = new CDP(t.webSocketDebuggerUrl);
  await c.open();
  try {
    const r = await c.send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise: true, userGesture: true,
    });
    if (r.exceptionDetails) {
      throw new Error('页面 JS 抛错: ' + JSON.stringify(r.exceptionDetails).slice(0, 800));
    }
    return r.result.value;
  } finally { c.close(); }
}

const [cmd, ...args] = process.argv.slice(2);
const out = v => process.stdout.write(typeof v === 'string' ? v : JSON.stringify(v, null, 2));

try {
  switch (cmd) {
    case 'list': case 'targets': {
      const ts = await targets();
      const ps = pages(ts);
      out(ts.map(t => ({
        pageIdx: ps.findIndex(p => p.id === t.id),   // -1 = 非 page，不可用 CDP_TARGET 定位
        type: t.type, title: (t.title || '').slice(0, 60), url: (t.url || '').slice(0, 100),
        ws: !!t.webSocketDebuggerUrl,
      })));
      process.stdout.write('\n');
      break;
    }
    case 'pages': {
      const ts = await targets();
      out(pages(ts).map((t, i) => ({ i, title: t.title, url: t.url })));
      process.stdout.write('\n');
      break;
    }
    case 'closetab': {
      // closetab —— 关掉当前定位到的标签页（用 CDP_TARGET_URL 指定最稳）
      const t = await pickTarget();
      const ver = await (await fetch(`${BASE}/json/version`)).json();
      const bc = new CDP(ver.webSocketDebuggerUrl);
      await bc.open();
      try {
        await bc.send('Target.closeTarget', { targetId: t.id });
        out({ closed: t.id, url: (t.url || '').slice(0, 90) });
      } finally { bc.close(); }
      process.stdout.write('\n');
      break;
    }
    case 'newtab': {
      // newtab [url] —— 新开一个自己的标签页（避免与其它占用浏览器的进程抢标签）
      const url = args[0] || 'about:blank';
      const ver = await (await fetch(`${BASE}/json/version`)).json();
      const bc = new CDP(ver.webSocketDebuggerUrl);
      await bc.open();
      let tid;
      try {
        const r = await bc.send('Target.createTarget', { url });
        tid = r.targetId;
      } finally { bc.close(); }
      await new Promise(r => setTimeout(r, 1200));
      const ps = pages(await targets());
      out({ targetId: tid, index: ps.findIndex(t => t.id === tid), url });
      process.stdout.write('\n');
      break;
    }
    case 'screenshot': {
      // screenshot [outfile] —— 截当前定位标签页（云端扫码：导出二维码图给老大）
      const t = await pickTarget();
      const c = new CDP(t.webSocketDebuggerUrl);
      await c.open();
      try {
        const r = await c.send('Page.captureScreenshot', { format: 'png' });
        const fs = await import('node:fs/promises');
        const file = args[0] || 'data/screenshot.png';
        await fs.writeFile(file, Buffer.from(r.data, 'base64'));
        console.log(file);
      } finally { c.close(); }
      break;
    }
    case 'where': {
      const r = await evaluate('({url: location.href, title: document.title})');
      out(r); process.stdout.write('\n');
      break;
    }
    case 'goto': {
      const url = args[0];
      if (!url) throw new Error('缺 url');
      const t = await pickTarget();
      const c = new CDP(t.webSocketDebuggerUrl);
      await c.open();
      try {
        await c.send('Page.enable');
        await c.send('Page.navigate', { url });
        await new Promise(r => setTimeout(r, 3500));
        out(await evaluateWith(c, '({url: location.href, title: document.title})'));
        process.stdout.write('\n');
      } finally { c.close(); }
      break;
    }
    case 'eval': {
      out(await evaluate(args.join(' ')));
      process.stdout.write('\n');
      break;
    }
    case 'evalfile': {
      const fs = await import('node:fs/promises');
      const js = await fs.readFile(args[0], 'utf8');
      out(await evaluate(js));
      process.stdout.write('\n');
      break;
    }
    case 'text': {
      out(await evaluate('document.body ? document.body.innerText : "(no body)"'));
      process.stdout.write('\n');
      break;
    }
    case 'show': {
      // 把浏览器窗口恢复到前台可见（否则 GUI 窗口可能在后台/最小化）
      const ts = await targets();
      const ps = pages(ts);
      if (!ps.length) throw new Error('没有 page 目标');
      const t = ps[0];
      const ver = await (await fetch(`${BASE}/json/version`)).json();
      const bc = new CDP(ver.webSocketDebuggerUrl);
      await bc.open();
      let res = {};
      try {
        const g = await bc.send('Browser.getWindowForTarget', { targetId: t.id });
        res.windowId = g.windowId;
        res.wasState = g.bounds && g.bounds.windowState;
        res.bounds = g.bounds;
        await bc.send('Browser.setWindowBounds', { windowId: g.windowId, bounds: { windowState: 'minimized' } });
        await new Promise(r => setTimeout(r, 500));
        await bc.send('Browser.setWindowBounds', { windowId: g.windowId, bounds: { windowState: 'normal' } });
        await new Promise(r => setTimeout(r, 300));
        await bc.send('Browser.setWindowBounds', {
          windowId: g.windowId,
          bounds: { left: 60, top: 30, width: 1400, height: 800 },
        });
        res.ok = true;
      } finally { bc.close(); }
      const c = new CDP(t.webSocketDebuggerUrl);
      await c.open();
      try {
        await c.send('Page.enable');
        await c.send('Page.bringToFront');
        res.url = await evaluateWith(c, 'location.href');
        res.hidden = await evaluateWith(c, 'document.hidden');
      } finally { c.close(); }
      out(res); process.stdout.write('\n');
      break;
    }
    case 'net': {
      // net <urlRegex> <outFile> [waitMs] [reload|noreload]
      const filter = args[0] || 'aweme';
      const outFile = args[1] || 'net-capture.json';
      const waitMs = Number(args[2] || 9000);
      const doReload = (args[3] || 'reload') !== 'noreload';
      const t = await pickTarget();
      const c = new CDP(t.webSocketDebuggerUrl);
      await c.open();
      const hits = [];
      const seen = new Set();
      let re; try { re = new RegExp(filter); } catch { re = /aweme/; }
      c.on(m => {
        if (m.method === 'Network.responseReceived') {
          const r = m.params.response;
          if (re.test(r.url) && !seen.has(m.params.requestId)) {
            seen.add(m.params.requestId);
            hits.push({ requestId: m.params.requestId, url: r.url, status: r.status });
          }
        }
      });
      const fs = await import('node:fs/promises');
      try {
        await c.send('Network.enable', { maxResourceBufferSize: 120 * 1024 * 1024, maxTotalBufferSize: 240 * 1024 * 1024 });
        await c.send('Page.enable');
        if (doReload) await c.send('Page.reload', { ignoreCache: false });
        await new Promise(r => setTimeout(r, waitMs));
        const records = [];
        for (const h of hits) {
          let body = null, err = null;
          try {
            const b = await c.send('Network.getResponseBody', { requestId: h.requestId }, 25000);
            body = b.base64Encoded ? Buffer.from(b.body, 'base64').toString('utf8') : b.body;
          } catch (e) { err = String((e && e.message) || e); }
          records.push({ url: h.url, status: h.status, len: body ? body.length : 0, err, body });
        }
        await fs.writeFile(outFile, JSON.stringify(records, null, 1), 'utf8');
        out({ outFile, hitCount: hits.length, hits: records.map(r => ({ url: r.url.slice(0, 120), status: r.status, len: r.len, err: r.err })) });
        process.stdout.write('\n');
      } finally { c.close(); }
      break;
    }
    case 'wsframes': {
      // wsframes <outFile> [waitMs] [urlRegex] —— 抓浏览器侧 WebSocket **原始帧**（真机取证）
      // 用途：抖音直播页自己会连 /webcast/im/push/v2 推流；这里把服务端推下来的二进制帧
      //      （opcode=2）连同 payloadData(base64) 落盘，交给 python 端用 protobuf 解。
      //      比"照开源 proto 枚举"可靠：能看到平台**实际**推了哪些 method。
      // ⚠️ 只读：只监听网络事件，不点任何按钮、不发任何业务请求。
      const outFile = args[0] || 'wsframes.jsonl';
      const waitMs = Number(args[1] || 60000);
      const filter = args[2] || 'im/push|webcast';
      let re; try { re = new RegExp(filter); } catch { re = /im\/push/; }
      const t = await pickTarget();
      const c = new CDP(t.webSocketDebuggerUrl);
      await c.open();
      const wsUrls = new Map();
      const frames = [];
      c.on(m => {
        if (m.method === 'Network.webSocketCreated') {
          wsUrls.set(m.params.requestId, m.params.url);
        } else if (m.method === 'Network.webSocketFrameReceived') {
          const url = wsUrls.get(m.params.requestId) || '';
          if (!re.test(url)) return;
          const resp = m.params.response || {};
          frames.push({
            ts: m.params.timestamp, requestId: m.params.requestId, url,
            opcode: resp.opcode, len: (resp.payloadData || '').length,
            payloadData: resp.payloadData,
          });
        }
      });
      const fs = await import('node:fs/promises');
      try {
        await c.send('Network.enable', { maxResourceBufferSize: 120 * 1024 * 1024, maxTotalBufferSize: 240 * 1024 * 1024 });
        await c.send('Page.enable');
        // 第 4 个参数：http(s) URL → 先导航过去（重新建立 WS）；'reload' → 原地刷新。
        // ⚠️ 必须在 Network.enable **之后**再建连，否则 CDP 不回报已存在连接的帧。
        const action = args[3] || '';
        if (/^https?:/.test(action)) {
          await c.send('Page.navigate', { url: action });
        } else if (action === 'reload') {
          await c.send('Page.reload', { ignoreCache: false });
        }
        await new Promise(r => setTimeout(r, waitMs));
        await fs.writeFile(outFile, frames.map(f => JSON.stringify(f)).join('\n'), 'utf8');
        const byUrl = {};
        for (const f of frames) byUrl[f.url] = (byUrl[f.url] || 0) + 1;
        out({ outFile, frames: frames.length, byUrl });
        process.stdout.write('\n');
      } finally { c.close(); }
      break;
    }
    case 'shot': {
      const p = args[0] || 'shot.png';
      const t = await pickTarget();
      const c = new CDP(t.webSocketDebuggerUrl);
      await c.open();
      try {
        await c.send('Page.enable');
        try { await c.send('Page.bringToFront'); } catch {}
        await new Promise(r => setTimeout(r, 700));
        const r = await c.send('Page.captureScreenshot',
          { format: 'png', captureBeyondViewport: false, fromSurface: true }, 20000);
        const fs = await import('node:fs/promises');
        await fs.writeFile(p, Buffer.from(r.data, 'base64'));
        out({ saved: p, bytes: Buffer.from(r.data, 'base64').length });
        process.stdout.write('\n');
      } finally { c.close(); }
      break;
    }
    default:
      process.stdout.write('用法: node cdp.mjs <list|pages|where|goto|eval|evalfile|text|shot|net|wsframes>\n');
  }
} catch (e) {
  process.stderr.write('ERR: ' + (e && e.message ? e.message : String(e)) + '\n');
  process.exit(1);
}

async function evaluateWith(c, expr) {
  const r = await c.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true, userGesture: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 500));
  return r.result.value;
}
