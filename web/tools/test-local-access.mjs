// Final built SPA only; generated HTTP fixtures, isolated browser profile, no external services.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

const output = path.resolve(process.argv[2] || '.local-access-evidence')
mkdirSync(output, { recursive: true })
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const webRoot = fileURLToPath(new URL('../', import.meta.url))
const dist = path.join(webRoot, 'dist')
const browserExe = process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
assert(existsSync(browserExe), 'Set BROWSER_EXE to the isolated test browser executable')
const checks = [], trace = [], streams = new Set(), pageErrors = [], pages = []
let mode = 'local', epoch = 1, bootstrapCount = 0, accountLoggedIn = false
let nextFailure = null, statusFailure = 0, taskRunning = false
const counts = { gate: 0, status: 0, paid: 0, login: 0 }
const token = () => ('synthetic-local-write-' + epoch).padEnd(64, 'x')
const cookie = () => 'fixture_session=synthetic-' + epoch
const date = '2026-11-15', tag = '合成主播'
const llm = () => ({ available: false, enabled: true, config_enabled: true, enabled_source: 'config',
  control: { allowed: true, reason: 'allowed', epoch: 1 }, active_provider: 'synthetic',
  providers: [{ name: 'synthetic', protocol: 'openai_compatible', base_url: 'https://example.invalid/v1',
    model: 'synthetic-model', has_key: false, active: true }],
  today: { calls: 0, failed: 0, prompt_tokens: 0, completion_tokens: 0, by_provider: {} },
  budget: { used_tokens: 0, cap_tokens: 0, config_cap_tokens: 0 }, flags: {}, errors: [] })
const send = (res, code, body, extra = {}) => {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...extra })
  res.end(JSON.stringify(body))
}
const server = http.createServer(async (req, res) => {
  const parsed = new URL(req.url, 'http://fixture.invalid')
  const pathname = parsed.pathname
  const controlled = !['GET', 'HEAD'].includes(req.method) || pathname === '/api/review/backfill'
  trace.push({ method: req.method, path: pathname, write_token_present: !!req.headers['x-local-write-token'] })
  if (pathname === '/api/access') return send(res, 200, { mode })
  if (pathname === '/legacy' || pathname === '/legacy/') {
    res.writeHead(410, { 'Content-Type': 'text/plain; charset=utf-8' }); res.end('本地模式不提供旧版看板，请使用新版页面。'); return
  }
  if (pathname === '/api/local/session' && req.method === 'POST') {
    bootstrapCount++
    return send(res, 200, { mode: 'local', user: '本机用户', role: 'local', write_token: token() },
      { 'Set-Cookie': cookie() + '; Path=/; HttpOnly; SameSite=Strict' })
  }
  if (pathname === '/api/login') {
    counts.login++; accountLoggedIn = true
    return send(res, 200, { user: 'synthetic-account', role: 'admin' }, { 'Set-Cookie': 'fixture_account=ok; Path=/; HttpOnly' })
  }
  if (pathname === '/api/logout') {
    accountLoggedIn = false; return send(res, 200, { ok: true })
  }
  if (pathname.startsWith('/api/')) {
    if (pathname === '/api/llm/gate' && req.method === 'POST') counts.gate++
    if (pathname === '/api/review/backfill') req.method === 'POST' ? counts.paid++ : counts.status++
    const authenticated = mode === 'local' ? (req.headers.cookie || '').split('; ').includes(cookie()) : accountLoggedIn
    if (!authenticated) return send(res, 401, { error: 'synthetic expired session' })
    if (mode === 'local' && controlled && req.headers['x-local-write-token'] !== token()) {
      return send(res, 403, { error: 'synthetic missing token' })
    }
    if (nextFailure && pathname === nextFailure.path) {
      const code = nextFailure.status; nextFailure = null; return send(res, code, { error: 'synthetic rejection' })
    }
    if (pathname === '/api/me') return send(res, 200, { user: mode === 'local' ? '本机用户' : 'synthetic-account', role: mode === 'local' ? 'local' : 'admin', mode })
    if (pathname === '/api/sse') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' })
      res.write('event: snapshot\ndata: ' + JSON.stringify({ tags: [], date, now_ts: 1794700000 }) + '\n\n')
      streams.add(res); res.on('close', () => streams.delete(res)); return
    }
    if (pathname === '/api/llm') return send(res, 200, llm())
    if (pathname === '/api/llm/prices') return send(res, 200, { schema: 1, entries: [] })
    if (pathname === '/api/llm/gate') return send(res, 200, { ok: true })
    if (pathname === '/api/anchors') return send(res, 200, { anchors: [], health: { total: 0, enabled: 0 } })
    if (pathname === '/api/browser-login' && req.method === 'GET') return send(res, 200, { ok: true, status: {
      browser: { state: 'not_prepared', reason: '' }, channel: { state: 'not_checked', reason: '',
        target_tag: '', checked_at: null, source: 'none' }, busy: false, action: '', action_id: 0,
      login_confirmed: false, collection_recovered: false } })
    if (pathname === '/api/anchors/health') return send(res, 200, { total: 0, enabled: 0 })
    if (pathname === '/api/review/dates') return send(res, 200, { dates: [date] })
    if (pathname === '/api/review/tags') return send(res, 200, { tags: [tag], all_tags: [tag] })
    if (pathname === '/api/review') return send(res, 200, { empty: true, files: {}, room_id: 'synthetic' })
    if (pathname === '/api/review/backfill') {
      if (statusFailure) { const code = statusFailure; statusFailure = 0; return send(res, code, { error: 'synthetic receipt rejection' }) }
      return send(res, 200, { task: { state: taskRunning ? 'running' : 'not_requested', tag, data_date: date,
        ...(taskRunning ? { task_id: 'synthetic-task-001', counts: {}, can_redo: false } : {}) } })
    }
    if (pathname === '/api/llm/usage') return send(res, 200, {})
    return send(res, 404, { error: 'synthetic path not implemented' })
  }
  const file = pathname.startsWith('/assets/') ? path.join(dist, pathname) : path.join(dist, 'index.html')
  if (!file.startsWith(dist + path.sep) || !existsSync(file)) return send(res, 404, {})
  res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8' })
  res.end(readFileSync(file))
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const base = 'http://127.0.0.1:' + server.address().port
let browser
const pause = (ms = 150) => new Promise((resolve) => setTimeout(resolve, ms))
const record = (name) => {
  checks.push(name)
  writeFileSync(path.join(output, 'progress.json'), JSON.stringify({ checks, counts }) + '\n')
}
async function newPage() {
  const page = await browser.newPage()
  pages.push(page)
  await page.setViewport({ width: 1400, height: 1000 })
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (!request.url().startsWith(base) && !request.url().startsWith('data:')) return request.abort('blockedbyclient')
    request.continue()
  })
  return page
}
async function ready(page) {
  await page.waitForSelector('[data-testid="local-mode"]', { timeout: 10000 })
  assert.equal(await page.$('#u'), null)
}
async function reconnect(page) {
  await page.waitForSelector('[data-testid="reconnect-local"]')
  await page.click('[data-testid="reconnect-local"]')
  await ready(page)
  await pause()
}
async function navigate(page, route) {
  await page.click('a[href="/' + route + '"]')
  await page.waitForFunction((route) => location.pathname === '/' + route, {}, route)
  await pause()
}

try {
  browser = await puppeteer.launch({ executablePath: browserExe, headless: true, userDataDir: profile,
    args: ['--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-sync', '--disable-extensions'] })
  const page = await newPage()
  await page.goto(base + '/settings', { waitUntil: 'networkidle2' })
  await ready(page)
  await page.waitForSelector('[data-testid="local-access-settings"]')
  assert.equal(await page.$('[data-testid="account-settings"]'), null)
  assert.equal(await page.$('input[autocomplete="current-password"]'), null)
  assert.equal(await page.$('a[href="/legacy"]'), null)
  assert.equal(await page.evaluate(() => [...document.querySelectorAll('button')].some((button) => button.textContent.trim() === '退出')), false)
  assert.equal(counts.paid, 0)
  await page.screenshot({ path: path.join(output, 'local-settings.png'), fullPage: true })
  record('local deep link/settings without accounts, password inputs, logout or model call')

  for (const route of ['live', 'review', 'anchors', 'settings']) {
    await navigate(page, route)
    assert.equal(await page.$('#u'), null)
  }
  record('all four pages remain local and account-free')
  assert.equal(counts.status, 0)
  await navigate(page, 'review')
  await page.waitForSelector('[data-testid="portrait-recheck"]')
  assert.equal(counts.status, 0)
  await page.click('[data-testid="portrait-recheck"]')
  await page.waitForFunction(() => document.querySelector('[data-testid="portrait-task-state"]')?.textContent === '尚未生成')
  assert.equal(counts.status, 1)
  assert.equal(trace.find((request) => request.path === '/api/review/backfill').write_token_present, true)
  record('reconcile GET requires explicit query and carries in-memory write token')

  for (const status of [401, 403]) {
    await navigate(page, 'settings')
    await page.waitForSelector('[data-testid="global-ai-toggle"]')
    const before = counts.gate, sessions = bootstrapCount
    nextFailure = { path: '/api/llm/gate', status }
    await page.click('[data-testid="global-ai-toggle"]')
    await page.waitForSelector('[data-testid="reconnect-local"]')
    await pause(300)
    assert.equal(counts.gate, before + 1)
    assert.equal(bootstrapCount, sessions)
    assert.equal(await page.$('#u'), null)
    await reconnect(page)
    await pause(300)
    assert.equal(counts.gate, before + 1)
    assert.equal(counts.paid, 0)
    record(status + ' write rejection is not replayed or sent to password login')
  }

  await navigate(page, 'review')
  await page.waitForSelector('[data-testid="portrait-recheck"]')
  const statusBefore = counts.status
  assert.equal(counts.status, statusBefore)
  statusFailure = 403
  await page.click('[data-testid="portrait-recheck"]')
  await page.waitForSelector('[data-testid="reconnect-local"]')
  assert.equal(counts.status, statusBefore + 1)
  await reconnect(page)
  await pause(3500)
  assert.equal(counts.status, statusBefore + 1)
  record('403 task GET is not replayed by reconnect, component remount or polling')

  await page.reload({ waitUntil: 'networkidle2' })
  await ready(page)
  await pause(300)
  assert.equal(counts.status, statusBefore + 1)
  record('browser refresh recovers deep link without automatic task reconciliation')

  taskRunning = true
  await page.click('[data-testid="portrait-recheck"]')
  await page.waitForFunction(() => document.querySelector('[data-testid="portrait-task-state"]')?.textContent === '正在生成')
  const beforePoll = counts.status
  statusFailure = 401
  await page.waitForSelector('[data-testid="reconnect-local"]', { timeout: 10000 })
  assert.equal(counts.status, beforePoll + 1)
  await reconnect(page)
  await pause(3500)
  assert.equal(counts.status, beforePoll + 1)
  record('401 active-task poll is stopped and never restored by session reconnect')

  taskRunning = false
  // Independent paid-submit case: do not pretend a known task identity vanished.
  await page.evaluate(() => sessionStorage.clear())
  await page.reload({ waitUntil: 'networkidle2' })
  await ready(page)
  await page.click('[data-testid="portrait-recheck"]')
  await page.waitForFunction(() => document.querySelector('[data-testid="portrait-task-state"]')?.textContent === '尚未生成')
  nextFailure = { path: '/api/review/backfill', status: 401 }
  await page.click('[data-testid="portrait-generate"]')
  await page.waitForSelector('[data-testid="reconnect-local"]')
  assert.equal(counts.paid, 1)
  const afterPaidStatus = counts.status
  await reconnect(page)
  await pause(3500)
  assert.equal(counts.paid, 1)
  assert.equal(counts.status, afterPaidStatus)
  record('401 paid-task submission is not replayed and does not trigger automatic status reconciliation')

  epoch++ // Simulated process restart invalidates the old cookie and write token.
  for (const stream of streams) stream.destroy()
  await page.waitForSelector('[data-testid="reconnect-local"]', { timeout: 12000 })
  const afterRestartStatus = counts.status
  await reconnect(page)
  await pause(3500)
  assert.equal(counts.status, afterRestartStatus)
  assert.equal(counts.paid, 1)
  record('SSE detects restarted instance; explicit reconnect restores only safe data/SSE')
  const storage = await page.evaluate(() => ({ local: Object.values(localStorage), session: Object.values(sessionStorage) }))
  assert(!JSON.stringify(storage).includes('synthetic-local-write'))
  assert(!(await page.evaluate(() => document.cookie)).includes('fixture_session'))
  const sseRequests = trace.filter((item) => item.path === '/api/sse')
  assert(sseRequests.length > 0 && sseRequests.every((item) => !item.write_token_present))
  record('write token absent from browser storage/URL and session cookie is HttpOnly; SSE uses cookie')

  const failurePage = await newPage()
  let failAccess = true
  failurePage.removeAllListeners('request')
  failurePage.on('request', (request) => {
    if (failAccess && request.url() === base + '/api/access') return request.abort('connectionfailed')
    if (!request.url().startsWith(base) && !request.url().startsWith('data:')) return request.abort('blockedbyclient')
    request.continue()
  })
  await failurePage.goto(base + '/live', { waitUntil: 'networkidle2' })
  await failurePage.waitForSelector('[data-testid="reconnect-local"]')
  assert.equal(await failurePage.$('#u'), null)
  await failurePage.screenshot({ path: path.join(output, 'initial-connection-failure.png'), fullPage: true })
  failAccess = false
  await reconnect(failurePage)
  record('initial network failure stays connection error and explicit retry confirms server mode')
  await failurePage.goto(base + '/legacy')
  assert.equal(await failurePage.$('input[type="password"]'), null)
  assert((await failurePage.content()).includes('本地模式不提供旧版看板'))
  record('legacy fixture denial presents no old password form')

  await page.close()
  await failurePage.close()

  mode = 'account'
  accountLoggedIn = false
  const accountPage = await newPage()
  await accountPage.goto(base + '/settings', { waitUntil: 'networkidle2' })
  await accountPage.waitForSelector('#u')
  await accountPage.type('#p', 'synthetic-account-password')
  await accountPage.click('form.login button[type="submit"]')
  await accountPage.waitForSelector('[data-testid="account-settings"]')
  assert.equal(await accountPage.$('[data-testid="local-mode"]'), null)
  assert.equal(counts.login, 1)
  assert.equal(await accountPage.evaluate(() => [...document.querySelectorAll('button')].some((button) => button.textContent.trim() === '退出')), true)
  record('explicit standalone account mode retains login/account settings/logout')
  assert.deepEqual(pageErrors, [])
  const hash = (file) => createHash('sha256').update(readFileSync(file)).digest('hex')
  const sourceNames = ['src/store.js', 'src/App.vue', 'src/views/Settings.vue', 'src/views/Review.vue']
  const distNames = ['index.html', ...readdirSync(path.join(dist, 'assets')).map((name) => 'assets/' + name)]
  writeFileSync(path.join(output, 'result.json'), JSON.stringify({ checks, counts, bootstrap_count: bootstrapCount,
    source_sha256: Object.fromEntries(sourceNames.map((name) => [name, hash(path.join(webRoot, name))])),
    dist_sha256: Object.fromEntries(distNames.map((name) => [name, hash(path.join(dist, name))])),
    request_trace: trace, page_errors: pageErrors }, null, 2) + '\n')
  console.log(JSON.stringify({ passed: checks.length, checks, counts }))
} catch (error) {
  writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ error: error.name,
    message: error.message, checks, counts, request_trace: trace, page_errors: pageErrors }, null, 2) + '\n')
  throw error
} finally {
  for (const page of pages) if (!page.isClosed()) await page.close()
  for (const stream of streams) stream.destroy()
  if (browser) await browser.close()
  server.closeAllConnections()
  await new Promise((resolve) => server.close(resolve))
  assert(profile.startsWith(output + path.sep) && path.basename(profile).startsWith('headless-profile-'))
  rmSync(profile, { recursive: true, force: true })
  writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify({ browser_closed: !!browser, server_closed: true,
    profile_removed: !existsSync(profile), streams_remaining: streams.size }) + '\n')
}
