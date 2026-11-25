// 从 web 目录运行：npm run build && node tools/test-live-roster.mjs
// 使用本机 Edge/Chrome，在回环端口向生产构建连续推送测试快照。
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import http from 'node:http'
import puppeteer from 'puppeteer-core'

const executablePath = [
  process.env.BROWSER_EXE,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe'
].find((p) => p && existsSync(p))
if (!executablePath) throw new Error('请通过 BROWSER_EXE 指定已安装的 Chrome 或 Edge')

const dist = new URL('../dist/', import.meta.url)
let stream
const server = http.createServer((req, res) => {
  if (req.url === '/api/sse') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' })
    res.flushHeaders()
    stream = res
    return
  }
  if (req.url === '/api/me' || req.url === '/api/anchors/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' })
    return res.end(req.url === '/api/me' ? '{"user":"fixture","role":"viewer"}' : '{}')
  }
  if (req.url === '/live' || /^\/assets\/[\w.-]+\.(css|js)$/.test(req.url)) {
    const file = req.url === '/live' ? 'index.html' : req.url.slice(1)
    const type = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'
    res.writeHead(200, { 'Content-Type': type + '; charset=utf-8' })
    return res.end(readFileSync(new URL(file, dist)))
  }
  res.writeHead(404).end()
})

function anchor(tag, live = false, reason = 'watch探测', probe = live ? 'live' : 'offline') {
  return { tag, live: [live, reason], probe_state: probe, last_ts: 0, totals: {}, cold: {}, rate_per_min: 0 }
}

await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
let browser
try {
  browser = await puppeteer.launch({ executablePath, headless: true, args: ['--disable-gpu', '--no-proxy-server'] })
  const page = await browser.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.setDefaultTimeout(15000)
  page.setDefaultNavigationTimeout(30000)
  await page.goto('http://127.0.0.1:' + server.address().port + '/live', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => document.querySelector('.topbar')?.textContent.includes('已连接'))

  // 比较完整标签列表，同时核对唯一选中项和相应详情，防止残留已删除的标签。
  async function check(anchors, selected, message, rosterError = null, absent = []) {
    stream.write('event: snapshot\ndata: ' + JSON.stringify({ tags: anchors, roster_error: rosterError }) + '\n\n')
    await page.waitForFunction((expected, selected, message, absent) => {
      const labels = (selector) => [...document.querySelectorAll(selector)].map((el) => el.textContent.trim())
      const content = document.querySelector('.content').textContent
      return JSON.stringify(labels('.content .tab')) === JSON.stringify(expected) &&
        JSON.stringify(labels('.content .tab.on')) === JSON.stringify(selected ? [selected] : []) &&
        content.includes(message) && absent.every((s) => !content.includes(s))
    }, {}, anchors.map((a) => a.tag), selected, message, absent)
  }
  const a = anchor('甲')
  const b = anchor('乙')
  const c = anchor('丙', false, '数据流静默', 'missing')
  await check([a, b], '甲', '未开播')
  await page.click('.content .tab:nth-child(2)')
  await check([a, b, c], '乙', '未开播') // 新增无文件主播，保留手动选择
  await check([a, anchor('丙', true)], '丙', '当前在线') // 乙被移除，优先选在播项
  await check([a, c], '丙', '等待首次采集数据')
  await check([], '', '暂无启用监控的主播')
  await check([anchor('甲', true), c], '甲', '当前在线') // 清空时须忘记先前选中的丙
  await check([], '', '监控名册读取异常', '测试读取错误')
  await check([b], '乙', '未开播')
  const error = anchor('乙', false, '探测异常', 'error')
  error.probe_hint = 'private-probe-debug-marker'
  await check([b, { ...error, tag: '甲' }], '乙', '1 路开播探测异常（甲）')
  await check([error], '乙', '探测异常，开播状态未知。请检查监控浏览器的登录状态和验证提示。',
    null, ['未开播', 'private-probe-debug-marker', '当前在线'])
  await check([anchor('乙', true, '数据流活动', 'error')], '乙', '近期弹幕或人气数据仍有活动',
    null, ['未开播', '开播状态未知'])
  assert.ok((await page.$eval('.content', (el) => el.textContent)).includes('当前在线'))
  await check([anchor('乙', false, '探测过期', 'stale')], '乙', '当前开播状态未知',
    null, ['未开播', '探测异常', '当前在线'])
  await check([b], '乙', '未开播', null, ['探测异常', '开播状态未知', '验证提示'])
  await check([anchor('乙', true)], '乙', '当前在线', null, ['探测异常', '验证提示'])
  assert.deepEqual(errors, [])
  console.log('实时主播列表 SSE 浏览器验证通过')
} finally {
  stream?.end()
  server.closeAllConnections()
  server.close()
  await browser?.close()
}
