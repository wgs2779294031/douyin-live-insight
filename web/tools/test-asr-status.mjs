// Final SPA against generated loopback data only. No worker/model/platform calls.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

const output = path.resolve(process.argv[2])
assert(!existsSync(output), 'Use a new evidence directory for each run')
mkdirSync(output, { recursive: true })
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const web = fileURLToPath(new URL('../', import.meta.url)), dist = path.join(web, 'dist')
const executablePath = process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
assert(existsSync(executablePath))
const SECRET = 'synthetic-secret-cookie-token-and-private-path:C:/not-for-rendering'
const checks = [], trace = [], pages = [], streams = new Set(), pageErrors = [], external = []
const counts = { retry: 0, otherWrites: 0 }
let mode = 'local', accountLoggedIn = false, epoch = 1, statusSequence = 0, nextFailure = 0, deferAction = false, pending = null
const token = () => ('synthetic-asr-token-' + epoch).padEnd(64, 'x')
const cookie = () => 'fixture_asr=synthetic-' + epoch
const state = (overrides = {}) => ({ generation: 'generated_A', sequence: ++statusSequence, state: 'idle',
  verified: true, resource_held: true, stale: false, no_progress: false, snapshot_at: Date.now() / 1000,
  counts: { pending: 0, projection_pending: 0, retryable: 0, blocked: 0, complete: 1 },
  scan: { partial: false, limited: false, checked: 1, last_complete_at: Date.now() / 1000, counts_scope: 'recent_window', sample_limit: 256 },
  current: {}, error: '', next_condition: 'periodic_scan', next_retry_at: null, last_progress_at: Date.now() / 1000, last_completed_at: Date.now() / 1000,
  private_path: SECRET, ...overrides })
let guide = state({ state: 'starting', verified: false, snapshot_at: null,
  counts: { pending: null, projection_pending: null, retryable: null, blocked: null, complete: null }, scan: { partial: true } })
const send = (res, code, value, headers = {}) => {
  if (res.destroyed || res.writableEnded) return
  res.writeHead(code, { 'Content-Type': 'application/json', ...headers }); res.end(JSON.stringify(value))
}
async function body(req) {
  let value = ''
  for await (const chunk of req) { value += chunk; assert(value.length < 4096) }
  return value ? JSON.parse(value) : {}
}
const llm = () => ({ available: false, enabled: false, config_enabled: false, enabled_source: 'config',
  control: { allowed: false, reason: 'global_paused', epoch: 1 }, providers: [], active_provider: '',
  today: {}, budget: { used_tokens: 0, cap_tokens: 0 }, flags: {}, errors: [] })
const loginGuide = () => ({ browser: { state: 'not_prepared' },
  channel: { state: 'not_checked', source: 'none', target_tag: '', checked_at: null }, busy: false })
const server = http.createServer(async (req, res) => {
  const pathname = new URL(req.url, 'http://generated.invalid').pathname
  const entry = { method: req.method, path: pathname, writeToken: !!req.headers['x-local-write-token'], origin: req.headers.origin || '' }
  trace.push(entry)
  try {
    if (pathname === '/api/access') return send(res, 200, { mode })
    if (pathname === '/api/local/session') return send(res, 200,
      { mode: 'local', user: '本机用户', role: 'local', write_token: token() },
      { 'Set-Cookie': cookie() + '; HttpOnly; SameSite=Strict; Path=/' })
    if (pathname === '/api/login') { accountLoggedIn = true; return send(res, 200, { user: 'generated-account', role: 'admin' }) }
    if (pathname.startsWith('/api/')) {
      const authenticated = mode === 'account' ? accountLoggedIn : (req.headers.cookie || '').split('; ').includes(cookie())
      if (!authenticated) return send(res, 401, { error: SECRET })
      if (req.method !== 'GET' && mode === 'local' && req.headers['x-local-write-token'] !== token()) return send(res, 403, { error: SECRET })
      if (pathname === '/api/me') return send(res, 200, { user: mode === 'account' ? 'generated-account' : '本机用户', role: mode === 'account' ? 'admin' : 'local', mode })
      if (pathname === '/api/sse') {
        res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' })
        res.write('event: snapshot\ndata: {"tags":[],"date":"2000-01-01","now_ts":1000}\n\n')
        streams.add(res); res.on('close', () => streams.delete(res)); return
      }
      if (pathname === '/api/anchors') return send(res, 200, { anchors: [], health: {} })
      if (pathname === '/api/llm') return send(res, 200, llm())
      if (pathname === '/api/llm/prices') return send(res, 200, { schema: 1, entries: [] })
      if (pathname === '/api/browser-login') return send(res, 200, { ok: true, status: loginGuide() })
      if (pathname === '/api/asr' && req.method === 'GET') return send(res, 200, { ok: true, status: guide })
      if (pathname === '/api/asr' && req.method === 'POST') {
        const value = await body(req)
        assert.deepEqual(value, { action: 'retry' })
        counts.retry++
        if (nextFailure) { const code = nextFailure; nextFailure = 0; return send(res, code, { error: SECRET }) }
        if (deferAction) { deferAction = false; pending = { res }; return }
        guide = state({ state: 'scanning' })
        return send(res, 200, { ok: true, status: guide })
      }
      if (req.method !== 'GET') counts.otherWrites++
      return send(res, 200, {})
    }
    const file = pathname.startsWith('/assets/') ? path.join(dist, pathname) : path.join(dist, 'index.html')
    assert(file.startsWith(dist + path.sep))
    res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8' })
    res.end(readFileSync(file))
  } catch (error) { pageErrors.push('fixture: ' + error.message); send(res, 500, { error: 'generated fixture error' }) }
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const base = 'http://127.0.0.1:' + server.address().port
const pause = (ms = 120) => new Promise((resolve) => setTimeout(resolve, ms))
const record = (value) => { checks.push(value); writeFileSync(path.join(output, 'progress.json'), JSON.stringify({ checks, counts }) + '\n') }
let browser = null, failure = null
async function newPage({ noPoll = false } = {}) {
  const page = await browser.newPage(); pages.push(page)
  await page.setViewport({ width: 1400, height: 1000 })
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (!request.url().startsWith(base + '/') && !request.url().startsWith('data:')) {
      external.push(new URL(request.url()).origin); return request.abort('blockedbyclient')
    }
    request.continue()
  })
  if (noPoll) await page.evaluateOnNewDocument(() => {
    const original = window.setInterval
    window.setInterval = (callback, delay, ...args) => original(callback, delay === 5000 ? 600000 : delay, ...args)
  })
  return page
}
const text = (page, name) => page.$eval('[data-testid="asr-' + name + '"]', (node) => node.textContent)
const current = (page) => page.$eval('[data-testid="asr-state"]', (node) => node.dataset.current === 'true')
const green = (page) => page.$eval('[data-testid="asr-dot"]', (node) => node.classList.contains('on'))
async function ready(page) {
  await page.waitForSelector('[data-testid="local-mode"]', { timeout: 10000 })
  await page.waitForSelector('[data-testid="asr-status-card"]')
  await page.waitForFunction(() => !document.querySelector('[data-testid="asr-refresh"]').disabled)
}
async function refresh(page) {
  await page.click('[data-testid="asr-refresh"]')
  await page.waitForFunction(() => !document.querySelector('[data-testid="asr-refresh"]').disabled)
  await pause()
}
async function waitPending() {
  for (let turn = 0; turn < 100 && !pending; turn++) await pause(20)
  assert(pending, 'Deferred action must reach the fake server')
}
function resolvePending() { if (pending) { const value = pending; pending = null; send(value.res, 200, { ok: true, status: guide }) } }
async function reconnect(page) {
  await page.waitForSelector('[data-testid="reconnect-local"]')
  await page.click('[data-testid="reconnect-local"]'); await ready(page)
}
try {
  browser = await puppeteer.launch({ executablePath, headless: true, userDataDir: profile,
    args: ['--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-sync', '--disable-extensions'] })
  const page = await newPage({ noPoll: true })
  await page.goto(base + '/settings', { waitUntil: 'networkidle2' }); await ready(page)
  assert.match(await text(page, 'state'), /正在启动/)
  assert.match(await text(page, 'counts'), /待确认/)
  assert.equal(counts.retry, 0); assert.equal(counts.otherWrites, 0)
  record('local settings mount reads only; unknown counts remain unknown, never zero backlog')

  const polling = await newPage()
  await polling.goto(base + '/settings', { waitUntil: 'networkidle2' }); await ready(polling)
  const beforePoll = trace.filter((entry) => entry.path === '/api/asr' && entry.method === 'GET').length
  await pause(5200)
  assert(trace.filter((entry) => entry.path === '/api/asr' && entry.method === 'GET').length > beforePoll)
  assert.equal(counts.retry, 0); await polling.close()
  record('the real five-second periodic timer performs safe GET only and never dispatches retry')

  guide = state({ state: 'not_started', verified: false, snapshot_at: null }); await refresh(page)
  assert.match(await text(page, 'state'), /尚未启动/)
  assert.equal(await current(page), false)
  assert.equal(await page.$eval('[data-testid="asr-retry"]', (node) => node.disabled), true)
  record('the legitimate not-started manager state remains explicit and cannot dispatch work')

  guide = state(); await refresh(page)
  assert.equal(await current(page), true); assert.equal(await green(page), true)
  for (const [name, label] of [['busy', '其它转写'], ['processing', '正在处理'], ['backoff', '等待下次'], ['dependency_error', '依赖需要检查'], ['storage_error', '保存失败']]) {
    guide = state({ state: name }); await refresh(page); assert.match(await text(page, 'state'), new RegExp(label))
  }
  guide = state({ state: 'processing', no_progress: true, counts: { pending: 4, projection_pending: 1, retryable: 2, blocked: 1, complete: 3 },
    scan: { partial: true, limited: true, checked: 8, last_complete_at: null },
    current: { tag: '合成主播', date: '2000-01-01', segment: 'seg_0001.flv', phase: 'recognizing', attempt: 2 } })
  await refresh(page)
  assert.match(await text(page, 'no-progress'), /仍有资源占用/)
  assert.match(await text(page, 'scan'), /不是整库积压/)
  assert.match(await text(page, 'current'), /合成主播.*seg_0001/)
  assert.equal(await green(page), false)
  await page.screenshot({ path: path.join(output, '01-partial-processing.png'), fullPage: true })
  record('lane categories, partial scan, attempt/current source, and no-progress occupancy remain distinct')

  const beforeConditions = counts.retry
  for (const [condition, label] of [
    ['periodic_scan', '按周期继续检查'], ['writer_release', '等待其它转写释放'],
    ['after_backoff', '退避结束后重新检查'], ['explicit_retry_or_dependency_change', '等待手动重试或依赖变化'],
    ['repair_evidence', '核对并恢复来源或身份凭据'], ['waiting_admission', '等待本轮转写接纳'],
    ['stop_after_current', '等待已接纳的当前片收尾'], ['none', '暂无安排的下一步']
  ]) {
    guide = state({ state: 'dependency_error', error: 'retry_exhausted', next_retry_at: null, next_condition: condition })
    await refresh(page)
    assert.match(await text(page, 'next-condition'), new RegExp(label))
  }
  guide = state({ state: 'dependency_error', error: 'retry_exhausted', next_retry_at: null,
    next_condition: 'explicit_retry_or_dependency_change' }); await refresh(page)
  assert.match(await text(page, 'error'), /自动重试上限/)
  assert.match(await text(page, 'next-condition'), /等待手动重试或依赖变化/)
  await page.screenshot({ path: path.join(output, '03-next-condition.png'), fullPage: true })
  for (const unknown of [SECRET, null, { private: SECRET }]) {
    guide = state({ next_condition: unknown }); await refresh(page)
    assert.match(await text(page, 'next-condition'), /待确认/)
    const card = await page.$eval('[data-testid="asr-status-card"]', (node) => node.textContent)
    assert(!card.includes(SECRET) && !card.includes('[object Object]'))
  }
  assert.equal(counts.retry, beforeConditions)
  record('next conditions are allowlisted; exhausted retries wait for explicit retry or dependency change; unknown values stay safe and never dispatch')

  for (const name of ['stopping', 'draining', 'identity_unknown', 'admission_unknown', 'snapshot_unconfirmed']) {
    guide = state({ state: name, verified: false }); await refresh(page)
    assert.equal(await current(page), false)
    assert.equal(await page.$eval('[data-testid="asr-retry"]', (node) => node.disabled), true)
  }
  record('stopping and unknown ownership/admission snapshots cannot dispatch retry or show current worker')

  guide = state({ snapshot_at: Date.now() / 1000 + 100 }); await refresh(page)
  assert.equal(await current(page), false); assert.equal(await green(page), false)
  guide = state({ stale: true }); await refresh(page); assert.equal(await current(page), false)
  guide = state({ generation: 'generated_B' }); await refresh(page); assert.equal(await current(page), true)
  guide = state({ generation: 'generated_A' }); await refresh(page); assert.equal(await current(page), false)
  guide = state({ generation: 'generated_B', sequence: statusSequence + 10 }); await refresh(page)
  const newest = { ...guide }
  guide = { ...newest, sequence: newest.sequence - 1, snapshot_at: Date.now() / 1000 }; await refresh(page)
  assert.equal(await current(page), false)
  guide = { ...newest, snapshot_at: newest.snapshot_at + .01 }; await refresh(page); assert.equal(await current(page), false)
  record('future/stale snapshots, retired worker generation and old/equal sequence cannot revive current status')

  guide = state({ generation: 'generated_C', error: SECRET, current: { tag: SECRET, date: SECRET, segment: SECRET, phase: SECRET }, text: SECRET })
  await refresh(page)
  assert(!(await page.$eval('[data-testid="asr-status-card"]', (node) => node.textContent)).includes(SECRET))
  const beforeAction = counts.retry
  deferAction = true
  await page.click('[data-testid="asr-retry"]'); await waitPending()
  await page.click('[data-testid="asr-retry"]')
  assert.equal(counts.retry, beforeAction + 1); assert.equal(await current(page), false)
  guide = state({ generation: 'generated_C', state: 'scanning' }); resolvePending()
  await page.waitForFunction(() => !document.querySelector('[data-testid="asr-refresh"]').disabled)
  assert.match(await text(page, 'action-hint'), /已请求重新检查/)
  record('safe fields hide raw errors/paths/text; explicit retry stays single-flight and sends only retry action')

  guide = state({ generation: 'generated_C' }); await refresh(page)
  deferAction = true; const beforeUnmount = counts.retry
  await page.click('[data-testid="asr-retry"]'); await waitPending()
  await page.click('a[href="/live"]'); await page.waitForFunction(() => !document.querySelector('[data-testid="asr-status-card"]'))
  guide = state({ generation: 'generated_D', state: 'identity_unknown', verified: false }); resolvePending()
  await page.click('a[href="/settings"]'); await ready(page)
  assert.equal(counts.retry, beforeUnmount + 1); assert.equal(await current(page), false)
  record('unmount aborts the request; delayed action success does not revive the previous boot or replay on return')

  for (const code of [401, 403]) {
    guide = state({ generation: 'generated_D' }); await refresh(page)
    nextFailure = code; const before = counts.retry
    await page.click('[data-testid="asr-retry"]'); await reconnect(page)
    assert.equal(counts.retry, before + 1)
  }
  record('401/403 invalidate local access; explicit reconnect performs reads and never replays retry')

  guide = state({ generation: 'generated_D' }); await refresh(page)
  await page.evaluate(() => { const original = window.setTimeout; window.setTimeout = (callback, delay, ...args) => original(callback, delay === 6000 ? 300 : delay, ...args) })
  deferAction = true; const beforeTimeout = counts.retry
  await page.click('[data-testid="asr-retry"]'); await waitPending()
  await page.waitForFunction(() => document.querySelector('[data-testid="asr-action-hint"]')?.textContent.includes('结果待确认'))
  guide = state({ generation: 'generated_D' }); resolvePending(); await pause(400)
  assert.equal(counts.retry, beforeTimeout + 1); assert.equal(await current(page), false)
  record('bounded timeout reports uncertainty without replay; a delayed response after abort cannot restore current')
  await page.close()

  guide = state({ generation: 'generated_E' })
  const ttl = await newPage({ noPoll: true })
  await ttl.goto(base + '/settings', { waitUntil: 'networkidle2' }); await ready(ttl)
  assert.equal(await green(ttl), true)
  const beforeTTL = trace.filter((entry) => entry.path === '/api/asr').length
  await ttl.evaluate(() => { const original = Date.now; Date.now = () => original() + 16000 })
  await ttl.waitForFunction(() => document.querySelector('[data-testid="asr-state"]').dataset.current === 'false', { timeout: 3000 })
  assert.equal(await green(ttl), false)
  assert.equal(trace.filter((entry) => entry.path === '/api/asr').length, beforeTTL)
  await ttl.screenshot({ path: path.join(output, '02-expired-snapshot.png'), fullPage: true }); await ttl.close()
  record('15s local TTL expires on a normal clock tick without HTTP; no same-sequence timestamp refresh')

  const reads = trace.filter((entry) => entry.path === '/api/asr').length
  mode = 'account'; accountLoggedIn = false
  const account = await newPage(); await account.goto(base + '/settings', { waitUntil: 'networkidle2' })
  await account.waitForSelector('#p'); await account.type('#p', 'generated-password'); await account.click('form.login button[type="submit"]')
  await account.waitForSelector('[data-testid="account-settings"]')
  assert.equal(await account.$('[data-testid="asr-status-card"]'), null)
  assert.equal(trace.filter((entry) => entry.path === '/api/asr').length, reads)
  record('standalone account settings never mount or poll the local ASR card')
  assert.equal(counts.otherWrites, 0)
  assert(trace.filter((entry) => entry.path === '/api/asr' && entry.method === 'POST').every((entry) => entry.writeToken && entry.origin === base))
  assert.equal(pageErrors.length, 0); assert.equal(external.length, 0)
} catch (error) { failure = { name: error.name, message: error.message, stack: error.stack } }
finally {
  resolvePending()
  for (const page of pages) if (!page.isClosed()) await page.close()
  let browserClosed = !browser, processExited = !browser
  if (browser) {
    const process = browser.process()
    await browser.close(); browserClosed = true
    for (let attempt = 0; attempt < 100 && process.exitCode === null && process.signalCode === null; attempt++) await pause(50)
    processExited = process.exitCode !== null || process.signalCode !== null
  }
  for (const res of streams) res.end()
  server.closeAllConnections()
  await new Promise((resolve) => server.close(resolve))
  let cleanupError = null
  const actual = realpathSync(profile)
  assert(actual.startsWith(realpathSync(output) + path.sep), 'Delete only the newly created profile inside this output')
  if (processExited) { try { rmSync(actual, { recursive: true, force: true }) } catch (error) { cleanupError = error.code || error.name } }
  const cleanup = { browser_closed: browserClosed, browser_process_exited: processExited,
    server_closed: !server.listening, streams_remaining: streams.size, profile_removed: !existsSync(profile), cleanup_error: cleanupError }
  if (!Object.values(cleanup).every((value) => value === true || value === 0 || value === null)) failure ||= { message: 'owned resource cleanup was not complete' }
  const hashes = (dir) => Object.fromEntries(readdirSync(dir).filter((name) => !name.startsWith('headless-profile')).map((name) => [name, createHash('sha256').update(readFileSync(path.join(dir, name))).digest('hex')]))
  writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify(cleanup, null, 2) + '\n')
  writeFileSync(path.join(output, 'result.json'), JSON.stringify({ checks, counts, failure, trace, pageErrors, external,
    source_sha256: Object.fromEntries(['src/components/AsrStatusCard.vue', 'src/views/Settings.vue', 'src/store.js', 'tools/test-asr-status.mjs']
      .map((name) => [name, createHash('sha256').update(readFileSync(path.join(web, name))).digest('hex')])),
    dist_sha256: { 'index.html': createHash('sha256').update(readFileSync(path.join(dist, 'index.html'))).digest('hex'), ...hashes(path.join(dist, 'assets')) },
    timing: 'The dedicated polling page uses the real five-second timer. Controlled state/TTL pages map polling5000 to600000; timeout maps6000 to300ms, TTL advances Date.now by16s. Production constants unchanged.' }, null, 2) + '\n')
}
console.log(JSON.stringify({ checks: checks.length, counts, failure }, null, 2))
if (failure) process.exitCode = 1
