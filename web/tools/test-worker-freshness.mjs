// Build this tree, then run with an absolute evidence directory. --before records the known old failure.
// Only a synthetic random-loopback server and an isolated headless profile are used.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

assert(process.argv[2] && path.isAbsolute(process.argv[2]), 'Provide an absolute evidence directory')
const output = path.resolve(process.argv[2])
const before = process.argv.includes('--before')
const pendingBefore = process.argv.includes('--pending-before')
mkdirSync(output, { recursive: true })
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const dist = fileURLToPath(new URL('../dist/', import.meta.url))
const executablePath = process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
let current = { enabled: true, state: 'running' }
const reads = [], patches = [], streams = new Set(), pending = new Set()
const checks = [], screenshots = [], errors = [], external = [], requests = []
const counts = { reads: 0, patches: 0 }
const snapshot = ({ enabled, state }) => ({ anchors: [{ tag: 'fixture-room', name: 'fixture-room',
  enabled: true, watch: true, record: false, watch_running: true, state: 'ok', is_live: true,
  watch_age_sec: 1, automatic_analysis: { enabled, effective: enabled, reason: 'allowed' },
  worker_state: { state, reason: 'worker_' + state, observed_at: Date.now() / 1000 } }],
  health: { total: 1, enabled: 1, ok: 1 } })
const reply = (res, code, value) => {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
  res.end(JSON.stringify(value))
}
function plan(queue, value = {}) {
  let arrived, release
  const reached = new Promise((resolve) => { arrived = resolve })
  const gate = new Promise((resolve) => { release = resolve })
  const entry = { ...value, arrived, gate }
  queue.push(entry)
  if (entry.hold) pending.add(release)
  return { reached, release: () => { pending.delete(release); release() } }
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  requests.push({ method: req.method, path: url.pathname })
  try {
    if (url.pathname === '/api/me') return reply(res, 200, { user: 'synthetic-ui', role: 'admin' })
    if (url.pathname === '/api/sse') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' })
      res.write('event: snapshot\ndata: {"tags":[],"date":"2026-11-15"}\n\n')
      streams.add(res); res.on('close', () => streams.delete(res)); return
    }
    if (url.pathname === '/api/anchors' && req.method === 'GET') {
      counts.reads++
      const entry = reads.shift() || {}
      const value = entry.value || snapshot(current)
      entry.arrived?.()
      if (entry.hold) await entry.gate
      return reply(res, entry.error ? 503 : 200, entry.error ? { error: 'Synthetic read failure' } : value)
    }
    if (url.pathname === '/api/anchors' && req.method === 'PATCH') {
      counts.patches++
      let raw = ''
      for await (const chunk of req) { raw += chunk; assert(raw.length < 10000) }
      const body = JSON.parse(raw)
      const entry = patches.shift() || {}
      entry.arrived?.()
      if (entry.hold) await entry.gate
      if (entry.error) return reply(res, 503, { error: 'Synthetic patch failure' })
      if ('llm' in body) current.enabled = body.llm
      return reply(res, 200, { ok: true, anchor: { tag: 'fixture-room' } })
    }
    if (url.pathname === '/anchors' || /^\/assets\/[\w.-]+\.(js|css)$/.test(url.pathname)) {
      const file = url.pathname.startsWith('/assets/') ? url.pathname.slice(1) : 'index.html'
      const type = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'
      res.writeHead(200, { 'Content-Type': type + '; charset=utf-8', 'Cache-Control': 'no-store' })
      return res.end(readFileSync(path.join(dist, file)))
    }
    return reply(res, 404, { error: 'Synthetic endpoint not registered' })
  } catch (_) { return reply(res, 500, { error: 'Synthetic fixture failed' }) }
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const origin = 'http://127.0.0.1:' + server.address().port
let browser, page
try {
  browser = await puppeteer.launch({ executablePath, headless: true, userDataDir: profile,
    args: ['--disable-gpu', '--no-proxy-server'] })
  page = await browser.newPage()
  page.on('pageerror', (error) => errors.push(error.message))
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (request.url().startsWith(origin + '/') || request.url().startsWith('data:')) request.continue()
    else { external.push(new URL(request.url()).origin); request.abort() }
  })
  await page.evaluateOnNewDocument(() => {
    const realInterval = window.setInterval
    window.setInterval = (callback, interval, ...args) => {
      if (interval === 15000) {
        window.__refreshAnchorsForTest = () => callback(...args)
        return realInterval(callback, 600000, ...args)
      }
      return realInterval(callback, interval, ...args)
    }
  })
  page.setDefaultTimeout(10000)
  page.setDefaultNavigationTimeout(30000)
  await page.setViewport({ width: 1440, height: 1000 })
  const worker = '[data-testid=worker-state]'
  const automatic = '[data-testid=automatic-analysis-toggle]'
  const lastIntent = '[data-testid=automatic-last-confirmed]'
  const text = (selector) => page.$eval(selector, (el) => el.textContent.trim())
  const waitText = (selector, value) => page.waitForFunction((selector, value) =>
    document.querySelector(selector)?.textContent.includes(value), {}, selector, value)
  const refresh = () => page.evaluate(() => window.__refreshAnchorsForTest())
  const settled = () => page.waitForFunction(() => !document.querySelector('[data-testid=automatic-analysis-toggle]')?.disabled)
  async function shot(name) {
    await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true })
    screenshots.push(name + '.png')
  }
  await page.goto(origin + '/anchors', { waitUntil: 'domcontentloaded' })
  await waitText(worker, '正在处理实况事件')
  await shot('01-authoritative-running')
  if (pendingBefore) {
    const slowOne = plan(reads, { hold: true, error: true })
    await refresh(); await slowOne.reached
    const slowTwo = plan(reads, { hold: true, error: true })
    await refresh(); await slowTwo.reached
    const slowThree = plan(reads, { hold: true, error: true })
    await refresh(); await slowThree.reached
    const olderFailure = page.waitForResponse((response) => response.url() === origin + '/api/anchors' && response.status() === 503)
    slowOne.release(); await olderFailure
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    const observed = await text(worker)
    assert.equal(observed, '正在处理实况事件', 'Intermediate dist must reproduce the pending stale-running report')
    await shot('02-overlapping-pending-still-running')
    checks.push('Intermediate dist reproduces old running while overlapping slow GETs remain pending and an older failure is ignored')
    writeFileSync(path.join(output, 'pending-old-failure.json'), JSON.stringify({ reproduced: true,
      during_pending_reads: observed, expected_failure: 'A pending latest GET and superseded failures retain old running' }, null, 2) + '\n')
    slowTwo.release(); slowThree.release()
  } else {
  plan(reads, { error: true }); await refresh()
  await waitText('.banner.err', 'Synthetic read failure')
  if (before) {
    const observed = await text(worker)
    assert.equal(observed, '正在处理实况事件', 'Old dist must reproduce the stale-running report')
    await shot('02-failed-read-still-running')
    checks.push('Old dist reproduces running retained after the latest authoritative read fails')
    writeFileSync(path.join(output, 'old-failure.json'), JSON.stringify({ reproduced: true,
      after_failed_read: observed, expected_failure: 'The old UI still claims processing after a read failure' }, null, 2) + '\n')
  } else {
    await waitText(worker, '实际运行状态待确认')
    await waitText(automatic, '待确认'); await waitText(lastIntent, '上次确认：开')
    assert(!(await text(worker)).includes('正在处理'))
    await waitText('[data-testid=worker-reason]', '上次回执已失效')
    await shot('02-failed-read-invalidates-receipt')
    checks.push('Latest GET failure invalidates worker evidence and marks retained intent as last confirmed')

    current.state = 'waiting_input'
    await refresh(); await waitText(worker, '已就绪，等待新事件')
    assert.equal(await text(automatic), '开'); assert.equal(await page.$(lastIntent), null)
    checks.push('An authoritative successful GET restores current worker and intent display')

    const oldSuccess = plan(reads, { hold: true, value: snapshot({ enabled: false, state: 'running' }) })
    await refresh(); await oldSuccess.reached
    const latestFailure = plan(reads, { error: true }); await refresh(); await latestFailure.reached
    await waitText('.banner.err', 'Synthetic read failure'); await waitText(worker, '实际运行状态待确认')
    const oldSuccessResponse = page.waitForResponse((response) => response.url() === origin + '/api/anchors' && response.status() === 200)
    oldSuccess.release(); await oldSuccessResponse
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    assert.equal(await text(worker), '实际运行状态待确认')
    assert.equal(await text(automatic), '待确认'); await waitText(lastIntent, '上次确认：开')
    await shot('03-late-success-cannot-revalidate')
    checks.push('A superseded successful GET cannot revalidate a receipt after the latest GET fails')

    const oldFailure = plan(reads, { hold: true, error: true })
    await refresh(); await oldFailure.reached
    current = { enabled: false, state: 'waiting_live' }
    await refresh(); await waitText(worker, '等待开播'); await waitText(automatic, '关')
    const oldFailureResponse = page.waitForResponse((response) => response.url() === origin + '/api/anchors' && response.status() === 503)
    oldFailure.release(); await oldFailureResponse
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    assert.equal(await text(worker), '等待开播'); assert.equal(await page.$('.banner.err'), null)
    checks.push('A superseded failed GET cannot invalidate a newer successful receipt')

    current = { enabled: true, state: 'running' }
    await refresh(); await waitText(worker, '正在处理实况事件')
    const prewriteRead = plan(reads, { hold: true })
    await refresh(); await prewriteRead.reached
    const write = plan(patches, { hold: true })
    await page.click(automatic); await write.reached
    await waitText(worker, '实际运行状态待确认'); await waitText(lastIntent, '上次确认：开')
    plan(reads, { error: true }); write.release()
    await settled(); await waitText(automatic, '待确认')
    await waitText(worker, '实际运行状态待确认')
    const prewriteResponse = page.waitForResponse((response) => response.url() === origin + '/api/anchors' && response.status() === 200)
    prewriteRead.release(); await prewriteResponse
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    assert.equal(await text(worker), '实际运行状态待确认')
    assert.equal(await text(automatic), '待确认'); await waitText(lastIntent, '上次确认：开')
    await shot('04-write-read-failure-unknown')
    checks.push('A mutation immediately invalidates old worker evidence; failed confirmation and a late pre-write GET cannot restore it')

    current.state = 'stopping'
    await refresh(); await waitText(worker, '正在停止自动实况'); await waitText(automatic, '关')
    assert.equal(await page.$(lastIntent), null)
    plan(patches, { error: true })
    await page.click(automatic); await settled(); await waitText(automatic, '待确认')
    await waitText(worker, '实际运行状态待确认'); await waitText(lastIntent, '上次确认：关')
    current.state = 'waiting_live'
    await refresh(); await waitText(worker, '等待开播'); await waitText(automatic, '关')
    await shot('05-authoritative-recovery')
    checks.push('Failed mutation stays unknown and preserves labeled old intent; only a new successful GET restores stopping or waiting-live states')

    current = { enabled: true, state: 'running' }
    await refresh(); await waitText(worker, '正在处理实况事件')
    const slowOne = plan(reads, { hold: true, error: true })
    await refresh(); await slowOne.reached
    await waitText(worker, '实际运行状态待确认'); await waitText(lastIntent, '上次确认：开')
    const slowTwo = plan(reads, { hold: true, error: true })
    await refresh(); await slowTwo.reached
    const slowThree = plan(reads, { hold: true, error: true })
    await refresh(); await slowThree.reached
    for (const oldRead of [slowOne, slowTwo]) {
      const response = page.waitForResponse((item) => item.url() === origin + '/api/anchors' && item.status() === 503)
      oldRead.release(); await response
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
      assert.equal(await text(worker), '实际运行状态待确认')
      assert.equal(await text(automatic), '待确认')
    }
    await shot('06-overlapping-slow-reads-pending-unknown')
    current.state = 'waiting_input'
    await refresh(); await waitText(worker, '已就绪，等待新事件'); await waitText(automatic, '开')
    const finalOldFailure = page.waitForResponse((item) => item.url() === origin + '/api/anchors' && item.status() === 503)
    slowThree.release(); await finalOldFailure
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    assert.equal(await text(worker), '已就绪，等待新事件'); assert.equal(await page.$(lastIntent), null)
    assert(!/自动实况设置(?:尚)?未确认/.test(await text('body')), 'Authoritative recovery must clear the old unconfirmed-operation hint')
    await shot('07-newest-success-after-slow-overlap')
    checks.push('Every GET invalidates the prior receipt immediately; overlapping slow failures cannot retain running, and only a newer successful GET restores it')
  }
  }
  assert.deepEqual(errors, []); assert.deepEqual(external, [])
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0)
  const html = readFileSync(path.join(dist, 'index.html'), 'utf8')
  const assets = ['index.html', ...[...html.matchAll(/\/assets\/([^"']+)/g)].map((match) => 'assets/' + match[1])]
  writeFileSync(path.join(output, 'verification.json'), JSON.stringify({ passed: !before && !pendingBefore, old_failure_reproduced: before || pendingBefore,
    checks, counts, screenshots, no_page_errors: true, external_requests: 0,
    production_dist: Object.fromEntries(assets.map((name) => [name, createHash('sha256').update(readFileSync(path.join(dist, name))).digest('hex')])),
    note: 'Synthetic API and worker receipts only; no real account, model key, Python service or external API.' }, null, 2) + '\n')
  console.log(JSON.stringify({ passed: !before && !pendingBefore, old_failure_reproduced: before || pendingBefore, checks: checks.length, counts }))
} catch (error) {
  writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ name: error.name, message: error.message, counts, requests }, null, 2) + '\n')
  if (page) await page.screenshot({ path: path.join(output, 'failure-page.png'), fullPage: true }).catch(() => {})
  throw error
} finally {
  for (const release of pending) release()
  if (browser) await browser.close()
  for (const stream of streams) stream.end()
  await new Promise((resolve) => server.close(resolve))
  const resolved = path.resolve(profile)
  assert.equal(path.dirname(resolved), output); assert(path.basename(resolved).startsWith('headless-profile-'))
  rmSync(resolved, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
  writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify({ browser_closed: true, loopback_server_closed: true,
    temporary_profile_removed: !existsSync(resolved) }, null, 2) + '\n')
}
