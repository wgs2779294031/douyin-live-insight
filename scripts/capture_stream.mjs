// capture_stream.mjs —— 抓直播间可直连的 flv 地址 + 全量 douyin cookie
//
// 为什么要这么绕（2026-10-16 / 10-19 实测，别改回去）：
//  1. 直播间页面内 fetch/XHR 会被字节安全 SDK 拦 → 只能在 CDP 网络层抓。
//  2. 播放器自己拉的流**不一定**是 flv：2026-10-19 实测「研途-张老师团队」的播放器
//     改走 WebRTC（拉 `*.sdp`），此时抓 `.flv` 会一条都抓不到。
//  3. 真正的通吃来源 = `webcast/room/enter` 响应的 `stream_url.flv_pull_url`
//     （uhd/hd/ld/sd 全清晰度，带 expire+sign）。用浏览器全量 cookie +
//     curl_cffi(impersonate=chrome) 直连实测 200 + FLV 头正常（四种清晰度全通过）。
//     → 主力用这条；播放器 flv URL 只作兜底。
//
// 用法：
//   node capture_stream.mjs --web-rid 277671499254 --out D:/x/cap.json [--wait 15000] [--close-tab]
// 产物：
//   { webRid, ts, pageUrl, flvPullUrl:{UHD/HD/SD/LD:url}, hlsPullUrl:{}, playerFlvUrls:[], cookies:[] }
//
// 退出码：0 拿到可用 flv；2 页面在但没拿到流（未开播/已结束）；1 其他错误
const PORT = process.env.CDP_PORT || '9222';
const BASE = `http://127.0.0.1:${PORT}`;

function arg(name, def = null) {
  const i = process.argv.indexOf(`--${name}`);
  if (i < 0) return def;
  const v = process.argv[i + 1];
  return (v === undefined || v.startsWith('--')) ? true : v;
}

const webRid = arg('web-rid');
const outFile = arg('out') || 'D:/wb-bin/flv_capture.json';
const waitMs = Number(arg('wait', 15000));
const closeTab = arg('keep-tab', false) !== true;
const tabUrl = arg('tab-url', null) || (webRid ? `live.douyin.com/${webRid}` : 'live.douyin.com');
if (!webRid && !arg('tab-url', null)) {
  console.error('需要 --web-rid 或 --tab-url');
  process.exit(1);
}

class CDP {
  constructor(wsUrl) { this.wsUrl = wsUrl; this._id = 0; this._p = new Map(); this._h = []; }
  on(fn) { this._h.push(fn); }
  open() {
    return new Promise((res, rej) => {
      const ws = new WebSocket(this.wsUrl); this.ws = ws;
      const t = setTimeout(() => rej(new Error('WS 连接超时')), 10000);
      ws.onopen = () => { clearTimeout(t); res(); };
      ws.onerror = () => { clearTimeout(t); rej(new Error('WS 连接失败')); };
      ws.onmessage = (ev) => {
        let m; try { m = JSON.parse(ev.data); } catch { return; }
        if (m.id !== undefined) {
          const p = this._p.get(m.id); if (!p) return; this._p.delete(m.id);
          m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
        } else if (m.method) { for (const fn of this._h) { try { fn(m); } catch {} } }
      };
      ws.onclose = () => { for (const [, p] of this._p) p.reject(new Error('WS 已关闭')); this._p.clear(); };
    });
  }
  send(method, params = {}, timeoutMs = 45000) {
    const id = ++this._id;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => { this._p.delete(id); reject(new Error(`CDP 超时 ${method}`)); }, timeoutMs);
      this._p.set(id, {
        resolve: (v) => { clearTimeout(t); resolve(v); },
        reject: (e) => { clearTimeout(t); reject(e); },
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function listTargets() {
  const r = await fetch(`${BASE}/json/list`);
  if (!r.ok) throw new Error(`CDP /json/list HTTP ${r.status}`);
  return await r.json();
}

async function closeTabsFor(sub) {
  const ts = await listTargets();
  for (const t of ts) {
    if (t.type === 'page' && t.url.includes(sub)) {
      try { await fetch(`${BASE}/json/close/${t.id}`, { method: 'PUT' }); } catch {}
    }
  }
}

async function openTab(url) {
  const r = await fetch(`${BASE}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  if (!r.ok) throw new Error(`新建标签失败 HTTP ${r.status}`);
  const t = await r.json();
  if (!t.webSocketDebuggerUrl) throw new Error('新标签没有 ws 地址');
  return t;
}

let page = (await listTargets()).find(t => t.type === 'page' && t.webSocketDebuggerUrl && t.url.includes(tabUrl));
// 页签标题=裸 URL 说明上次没加载成功（实测会遇到）→ 关掉重开
if (page) {
  const bad = !page.title || page.title === page.url.replace('https://', '');
  if (bad) {
    try { await fetch(`${BASE}/json/close/${page.id}`, { method: 'PUT' }); } catch {}
    page = null;
  }
}
let created = false;
if (!page) {
  page = await openTab(`https://live.douyin.com/${webRid}`);
  created = true;
}
const c = new CDP(page.webSocketDebuggerUrl);
await c.open();
await c.send('Network.enable', { maxResourceBufferSize: 300 * 1024 * 1024, maxTotalBufferSize: 600 * 1024 * 1024 });
await c.send('Page.enable');

const playerFlv = new Set();
let enterId = null;
let enterBody = null;
const bodyTasks = [];
c.on((m) => {
  if (m.method === 'Network.requestWillBeSent') {
    const u = m.params.request.url;
    if (/\.flv(\?|$)/i.test(u)) playerFlv.add(u);
  }
  if (m.method === 'Network.responseReceived') {
    const u = m.params.response.url;
    if (/webcast\/room\/(web\/)?enter\//.test(u)) enterId = m.params.requestId;
  }
  if (m.method === 'Network.loadingFinished' && enterId && m.params.requestId === enterId && !enterBody) {
    bodyTasks.push((async () => {
      try {
        const b = await c.send('Network.getResponseBody', { requestId: enterId }, 15000);
        enterBody = b.base64Encoded ? Buffer.from(b.body, 'base64').toString('utf8') : b.body;
      } catch {}
    })());
  }
});

// 已存在的标签 reload 一次重新触发 enter_room / 播放器请求
await c.send('Page.reload', { ignoreCache: false });

const deadline = Date.now() + waitMs;
while (Date.now() < deadline) {
  if (enterBody || playerFlv.size > 0) { await sleep(1500); break; }
  await sleep(700);
}
await Promise.all(bodyTasks);

function pickStreams(text) {
  // enter_room 里可能有两层：data.data[0].stream_url.flv_pull_url
  //                  或 live_core_sdk_data.pull_data.stream_data（JSON 字符串，含 lls）
  const out = { flv: {}, hls: {} };
  try {
    const j = JSON.parse(text);
    const room = j?.data?.data?.[0] || j?.data?.[0] || j?.data || {};
    const su = room?.stream_url || {};
    for (const [k, v] of Object.entries(su.flv_pull_url || {})) if (typeof v === 'string') out.flv[k] = v;
    for (const [k, v] of Object.entries(su.hls_pull_url_map || {})) if (typeof v === 'string') out.hls[k] = v;
    const sd = j?.data?.live_core_sdk_data?.pull_data?.stream_data;
    if (sd && typeof sd === 'string') {
      const p = JSON.parse(sd);
      const origin = p?.data?.origin || {};
      for (const [k, v] of Object.entries(origin)) {
        if (v?.main?.flv) out.flv[k.toUpperCase()] = v.main.flv;
        if (v?.main?.hls) out.hls[k.toUpperCase()] = v.main.hls;
        if (v?.main?.sdk_params) out.sdk = Object.keys(v.main.sdk_params);
      }
    }
  } catch {}
  return out;
}

const streams = enterBody ? pickStreams(enterBody) : { flv: {}, hls: {} };

const cookies = await c.send('Network.getCookies',
  { urls: ['https://live.douyin.com', 'https://www.douyin.com', 'https://douyin.com'] });
// 抓完把页面 video 静音暂停，省浏览器带宽（拉流由 curl_cffi 另外做）
try {
  await c.send('Runtime.evaluate', {
    expression: `(()=>{try{const v=document.querySelector('video');if(v){v.muted=true;v.pause();}return 1}catch(e){return 0}})()`,
  }, 8000);
} catch {}
const pageUrl = (await c.send('Runtime.evaluate', { expression: 'location.href', returnByValue: true }, 8000))?.result?.value || page.url;
// ★ 2026-10-27：默认关掉这个标签页（要留着调试就传 --keep-tab）。
//   以前是「只有本次新建的才关」，而且调用方一直没传 --close-tab，于是每抓一次
//   就在浏览器里留一个 renderer。这里有个隐蔽的坑：下面统一会 Page.reload，
//   直播间一旦结束/被风控，reload 会把页面跳到 www.douyin.com/jingxuan ——
//   下次按 `live.douyin.com/<web_rid>` 去找就匹配不上，又 openTab 开一个，
//   旧的继续留着。八路各 30 分钟重抓一次，一天能堆出几十个页面，
//   浏览器越跑越大直到端口探不通、被判定卡死重启。
//   现在**无论新建还是复用都关**：反正每次都 reload，复用没有任何收益。
if (closeTab) { try { await fetch(`${BASE}/json/close/${page.id}`, { method: 'PUT' }); } catch {} }
c.close();

const payload = {
  webRid,
  ts: Date.now(),
  time: new Date().toLocaleString('sv-SE'),
  pageUrl,
  flvPullUrl: streams.flv,
  hlsPullUrl: streams.hls,
  playerFlvUrls: [...playerFlv],
  cookies: cookies.cookies || [],
  tabReused: !created,
};
const fs = await import('node:fs');
fs.mkdirSync(outFile.replace(/[\\/][^\\/]+$/, ''), { recursive: true });
fs.writeFileSync(outFile, JSON.stringify(payload, null, 1), 'utf8');
const nFlv = Object.keys(streams.flv).length + playerFlv.size;
console.log(JSON.stringify({
  ok: nFlv > 0, enterFlv: Object.keys(streams.flv), playerFlv: playerFlv.size,
  hls: Object.keys(streams.hls), cookies: (cookies.cookies || []).length, out: outFile,
}));
process.exit(nFlv > 0 ? 0 : 2);
