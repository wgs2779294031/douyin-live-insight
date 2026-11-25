// Final SPA against generated local HTTP. Never visits a real platform or account.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

const output = path.resolve(process.argv[2] || '.browser-login-evidence')
assert(!existsSync(output), 'Each run needs a new evidence directory')
mkdirSync(output, { recursive: true })
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const web = fileURLToPath(new URL('../', import.meta.url)), dist = path.join(web, 'dist')
const executablePath = process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
assert(existsSync(executablePath))
const checks = [], trace = [], streams = new Set(), pages = [], pageErrors = [], external = []
const SECRET = 'synthetic-cookie-token-signature-DO-NOT-RENDER'
const counts = { prepare: 0, open: 0, check: 0, other_writes: 0 }
const tagA = '合成主播A', tagB = '合成主播B'
let roster = [], mode = 'local', accountLoggedIn = false, epoch = 1, nextFailure = 0, deferAction = false
let pending = null, actionId = 0
const token = () => ('synthetic-browser-login-token-' + epoch).padEnd(64, 'x')
const cookie = () => 'fixture_session=synthetic-' + epoch
const channel = (state = 'not_checked', target = '', age = 0) => ({ state, reason: SECRET,
  target_tag: target, checked_at: target ? Date.now() / 1000 - age : null,
  source: target ? 'owned_browser_profile_api' : 'none' })
const status = (browser = 'not_prepared', state = 'not_checked', target = '', age = 0) => ({
  browser: { state: browser, reason: SECRET }, channel: channel(state, target, age), busy: false,
  action: '', action_id: actionId, login_confirmed: false, collection_recovered: false })
let guide = status(), nextChannel = 'channel_offline'
const send = (res, code, value, headers = {}) => {
  if (res.destroyed) return
  res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...headers })
  res.end(JSON.stringify(value))
}
async function body(req) {
  let value = ''
  for await (const chunk of req) { value += chunk; assert(value.length < 4096) }
  return value ? JSON.parse(value) : {}
}
const llm = () => ({ available: false, enabled: false, config_enabled: false, enabled_source: 'config',
  control: { allowed: false, reason: 'global_paused', epoch: 1 }, providers: [], active_provider: '',
  today: {}, budget: { used_tokens: 0, cap_tokens: 0 }, flags: {}, errors: [] })
const server = http.createServer(async (req, res) => {
  const pathname = new URL(req.url, 'http://fixture.invalid').pathname
  const entry = { method: req.method, path: pathname, write_token_present: !!req.headers['x-local-write-token'] }
  trace.push(entry)
  try {
    if (pathname === '/api/access') return send(res, 200, { mode })
    if (pathname === '/api/local/session') return send(res, 200,
      { mode: 'local', user: '本机用户', role: 'local', write_token: token() },
      { 'Set-Cookie': cookie() + '; HttpOnly; SameSite=Strict; Path=/' })
    if (pathname === '/api/login') {
      accountLoggedIn = true
      return send(res, 200, { user: 'synthetic-account', role: 'admin' })
    }
    if (pathname.startsWith('/api/')) {
      const authenticated = mode === 'account' ? accountLoggedIn : (req.headers.cookie || '').split('; ').includes(cookie())
      if (!authenticated) return send(res, 401, { error: SECRET })
      if (req.method !== 'GET' && mode === 'local' && req.headers['x-local-write-token'] !== token()) return send(res, 403, { error: SECRET })
      if (pathname === '/api/me') return send(res, 200, { user: mode === 'account' ? 'synthetic-account' : '本机用户', role: mode === 'account' ? 'admin' : 'local', mode })
      if (pathname === '/api/sse') {
        res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' })
        res.write('event: snapshot\ndata: {"tags":[],"date":"2026-11-15","now_ts":1794700000}\n\n')
        streams.add(res); res.on('close', () => streams.delete(res)); return
      }
      if (pathname === '/api/anchors') return send(res, 200, { anchors: roster.map((tag) => ({ tag, sec_uid: SECRET })), health: {} })
      if (pathname === '/api/llm') return send(res, 200, llm())
      if (pathname === '/api/llm/prices') return send(res, 200, { schema: 1, entries: [] })
      if (pathname === '/api/llm/usage') return send(res, 200, {})
      if (pathname === '/api/browser-login' && req.method === 'GET') return send(res, 200, { ok: true, status: guide })
      if (pathname === '/api/browser-login' && req.method === 'POST') {
        const value = await body(req)
        assert(['prepare', 'open', 'check'].includes(value.action))
        assert.deepEqual(Object.keys(value).sort(), value.action === 'check' ? ['action', 'tag'] : ['action'])
        if (value.action === 'check') assert(roster.includes(value.tag))
        entry.action = value.action
        counts[value.action]++
        actionId++
        if (nextFailure) { const code = nextFailure; nextFailure = 0; return send(res, code, { error: SECRET }) }
        const target = value.action === 'check' ? value.tag : ''
        if (deferAction) {
          deferAction = false
          guide = { ...status('ready', 'checking', target), busy: true }
          pending = { res, target, action: value.action }
          return
        }
        guide = value.action === 'prepare' ? status('starting', 'need_target')
          : value.action === 'open' ? status('ready', 'page_opened') : status('ready', nextChannel, target)
        return send(res, 200, { ok: true, status: guide })
      }
      if (req.method !== 'GET') counts.other_writes++
      return send(res, 404, { error: 'synthetic endpoint unsupported' })
    }
    const file = pathname.startsWith('/assets/') ? path.join(dist, pathname) : path.join(dist, 'index.html')
    assert(file.startsWith(dist + path.sep))
    res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8' })
    res.end(readFileSync(file))
  } catch (error) { send(res, 500, { error: 'synthetic fixture error' }); pageErrors.push('fixture: ' + error.message) }
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const base = 'http://127.0.0.1:' + server.address().port
const pause = (ms = 120) => new Promise((resolve) => setTimeout(resolve, ms))
const record = (check) => {
  checks.push(check)
  writeFileSync(path.join(output, 'progress.json'), JSON.stringify({ checks, counts }) + '\n')
}
let browser, failure = null
async function newPage() {
  const page = await browser.newPage()
  pages.push(page)
  await page.setViewport({ width: 1400, height: 1000 })
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (!request.url().startsWith(base + '/') && !request.url().startsWith('data:')) {
      external.push(new URL(request.url()).origin); return request.abort('blockedbyclient')
    }
    return request.continue()
  })
  return page
}
async function text(page, testid) { return page.$eval('[data-testid="' + testid + '"]', (node) => node.textContent) }
async function ready(page) {
  await page.waitForSelector('[data-testid="local-mode"]', { timeout: 10000 })
  await page.waitForSelector('[data-testid="browser-login-guide"]')
  await page.waitForFunction(() => !document.querySelector('[data-testid="login-refresh"]').disabled)
}
async function click(page, name) {
  await page.click('[data-testid="login-' + name + '"]')
  await page.waitForFunction(() => !document.querySelector('[data-testid="login-refresh"]').disabled)
  await pause()
}
async function current(page) { return page.$eval('[data-testid="login-channel-status"]', (node) => node.dataset.current === 'true') }
async function pendingRequest() {
  for (let turn = 0; turn < 100 && !pending; turn++) await pause(20)
  assert(pending, 'Controlled deferred request must have reached the fixture')
}
async function reconnect(page) {
  await page.waitForSelector('[data-testid="reconnect-local"]')
  await page.click('[data-testid="reconnect-local"]')
  await ready(page)
  await pause()
}
function resolvePending(state, mutate = true) {
  assert(pending)
  const held = pending; pending = null
  const result = status('ready', state, held.target)
  if (mutate) guide = result
  send(held.res, 200, { ok: true, status: result })
}

try {
  browser = await puppeteer.launch({ executablePath, headless: true, userDataDir: profile,
    args: ['--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-sync', '--disable-extensions'] })
  const page = await newPage()
  await page.goto(base + '/settings', { waitUntil: 'networkidle2', timeout: 30000 })
  await ready(page)
  assert.deepEqual(counts, { prepare: 0, open: 0, check: 0, other_writes: 0 })
  assert(await page.$('[data-testid="login-empty-roster"]'))
  assert(await page.$eval('[data-testid="login-check"]', (node) => node.disabled))
  assert.equal(await page.$('[data-testid="account-settings"]'), null)
  record('empty roster/local/no key: safe reads only, no automatic browser or platform action')

  await click(page, 'prepare')
  assert.match(await text(page, 'login-browser-state'), /正在启动/)
  await click(page, 'open')
  assert.match(await text(page, 'login-browser-state'), /自有浏览器已准备/)
  assert.match(await text(page, 'login-channel-status'), /登录页已打开/)
  assert.equal(await current(page), false)
  assert.equal(counts.prepare, 1); assert.equal(counts.open, 1); assert.equal(counts.check, 0)
  await page.screenshot({ path: path.join(output, '01-empty-roster-open.png'), fullPage: true })
  record('explicit prepare/open work without a target and do not claim login or collection recovery')

  roster = [tagA, tagB]
  await click(page, 'refresh')
  await page.select('[data-testid="login-target"]', tagA)
  await click(page, 'check')
  assert.match(await text(page, 'login-channel-status'), /目标显示未开播/)
  assert.equal(await current(page), true)
  assert.match(await page.$eval('[data-testid="browser-login-guide"]', (node) => node.textContent), /受管浏览器资料通道的单次响应/)
  await page.screenshot({ path: path.join(output, '02-offline-evidence.png'), fullPage: true })
  record('explicit selected target check shows time/source and offline channel evidence, never exact login success')

  nextChannel = 'restricted'
  deferAction = true
  await page.click('[data-testid="login-check"]')
  await pendingRequest()
  assert.equal(await current(page), false)
  resolvePending('restricted')
  await page.waitForFunction(() => !document.querySelector('[data-testid="login-refresh"]').disabled)
  assert.equal(await current(page), false)
  assert.match(await text(page, 'login-channel-status'), /需扫码\/验证或环境受限/)
  nextChannel = 'unknown'
  await click(page, 'check')
  assert.equal(await current(page), false)
  assert.match(await text(page, 'login-channel-status'), /查询未知/)
  record('restricted and unknown results replace the old successful channel state')

  guide = status('ready', 'channel_live', tagA, 58.5)
  await click(page, 'refresh')
  assert.equal(await current(page), true)
  const beforeClockExpiry = trace.filter((entry) => entry.path === '/api/browser-login').length
  await page.waitForFunction(() => document.querySelector('[data-testid="login-channel-status"]').dataset.current === 'false', { timeout: 5000 })
  assert.equal(trace.filter((entry) => entry.path === '/api/browser-login').length, beforeClockExpiry)
  assert.match(await text(page, 'login-channel-status'), /过期/)
  guide = status('ready', 'channel_live', tagA, 61)
  await click(page, 'refresh')
  assert.equal(await current(page), false)
  assert.match(await text(page, 'login-channel-status'), /过期/)
  guide = status('closed', 'channel_live', tagA)
  await click(page, 'refresh')
  assert.equal(await current(page), false)
  assert.match(await text(page, 'login-browser-state'), /已关闭/)
  record('local clock expires old evidence without HTTP; expired evidence or a closed/rebuilt browser cannot retain green')

  guide = status('ready', 'channel_live', tagA, -120)
  await click(page, 'refresh')
  assert.equal(await current(page), false)
  assert.match(await text(page, 'login-channel-status'), /查询未知/)
  guide = status('ready', 'channel_live', tagA)
  guide.channel.source = SECRET
  await click(page, 'refresh')
  assert.equal(await current(page), false)
  assert.match(await text(page, 'login-channel-status'), /查询未知/)
  record('future timestamps and unrecognized evidence sources are unknown instead of current success')

  guide = status('ready', 'not_checked')
  deferAction = true
  await page.click('[data-testid="login-check"]')
  await page.waitForFunction(() => document.querySelector('[data-testid="login-check"]').disabled)
  await pendingRequest()
  assert.equal(await current(page), false)
  const beforeDuplicate = counts.check
  await page.click('[data-testid="login-check"]')
  await pause()
  assert.equal(counts.check, beforeDuplicate)
  await page.select('[data-testid="login-target"]', tagB)
  resolvePending('channel_live')
  await page.waitForFunction(() => !document.querySelector('[data-testid="login-refresh"]').disabled)
  assert.equal(await current(page), false)
  assert.doesNotMatch(await text(page, 'login-channel-status'), /目标显示直播/)
  record('busy actions stay single-flight; late success for a previous selected target never revives green')

  nextChannel = 'channel_live'
  await click(page, 'check')
  assert.equal(await current(page), true)
  assert.match(await text(page, 'login-channel-status'), /目标显示直播/)
  const currentChecks = counts.check
  await page.reload({ waitUntil: 'networkidle2' })
  await ready(page)
  assert.equal(counts.check, currentChecks)
  assert.equal(await current(page), false)
  record('refresh can safely read previous evidence but never restores a selected target or active platform check')

  await page.select('[data-testid="login-target"]', tagA)
  nextFailure = 401
  const before401 = counts.check
  await page.click('[data-testid="login-check"]')
  await reconnect(page)
  assert.equal(counts.check, before401 + 1)
  assert.equal(await current(page), false)
  nextFailure = 403
  const before403 = counts.open
  await page.click('[data-testid="login-open"]')
  await reconnect(page)
  assert.equal(counts.open, before403 + 1)
  record('401/403 revoke local access and explicit reconnect never replays open or check')

  await page.select('[data-testid="login-target"]', tagA)
  await page.evaluate(() => {
    const original = window.setTimeout
    window.setTimeout = (callback, delay, ...args) => original(callback, delay === 20000 ? 1500 : delay, ...args)
  })
  deferAction = true
  const beforeTimeout = counts.check
  await click(page, 'check')
  assert.equal(counts.check, beforeTimeout + 1)
  assert.equal(await current(page), false)
  assert.match(await text(page, 'login-hint'), /操作结果待确认/)
  guide = status('ready', 'unknown', tagA)
  resolvePending('channel_live', false)
  await pause(250)
  assert.equal(counts.check, beforeTimeout + 1)
  assert.equal(await current(page), false)
  record('20s action timeout path is finite and never retries; late success after client abort stays unconfirmed')

  const dom = await page.$eval('[data-testid="browser-login-guide"]', (node) => node.textContent)
  assert(!dom.includes(SECRET))
  assert.equal(counts.other_writes, 0)
  assert(trace.filter((entry) => entry.path === '/api/browser-login' && entry.method === 'POST').every((entry) => entry.write_token_present))
  await page.screenshot({ path: path.join(output, '03-unknown-no-replay.png'), fullPage: true })
  guide = status('ready', 'channel_offline', tagA)
  await click(page, 'refresh')
  assert.match(await text(page, 'login-browser-state'), /自有浏览器已准备/)
  const beforeStatusExpiry = trace.filter((entry) => entry.path === '/api/browser-login').length
  await page.evaluate(() => {
    const original = Date.now
    Date.now = () => original() + 61000
  })
  await page.waitForFunction(() => document.querySelector('[data-testid="login-browser-state"]').textContent.includes('状态读取已过期'), { timeout: 4000 })
  assert.equal(await page.$eval('[data-testid="login-browser-dot"]', (node) => node.classList.contains('on')), false)
  assert.equal(await current(page), false)
  assert.equal(trace.filter((entry) => entry.path === '/api/browser-login').length, beforeStatusExpiry)
  await page.screenshot({ path: path.join(output, '04-browser-status-expired.png'), fullPage: true })
  record('safe fixed payloads hide sensitive fields; browser status also expires locally without HTTP or platform action')

  await page.close()
  const guideReadsBeforeAccount = trace.filter((entry) => entry.path === '/api/browser-login' && entry.method === 'GET').length
  mode = 'account'; accountLoggedIn = false
  const account = await newPage()
  await account.goto(base + '/settings', { waitUntil: 'networkidle2' })
  await account.waitForSelector('#u')
  await account.type('#p', 'synthetic-password')
  await account.click('form.login button[type="submit"]')
  await account.waitForSelector('[data-testid="account-settings"]')
  assert.equal(await account.$('[data-testid="browser-login-guide"]'), null)
  assert.equal(trace.filter((entry) => entry.path === '/api/browser-login' && entry.method === 'GET').length, guideReadsBeforeAccount)
  record('standalone account mode keeps its account settings and never mounts browser guidance')
  assert.equal(pageErrors.length, 0)
  assert.equal(external.length, 0)
} catch (error) {
  failure = { name: error.name, message: error.message, stack: error.stack }
} finally {
  if (pending) { guide = status('ready', 'unknown', pending.target); resolvePending('unknown') }
  for (const page of pages) if (!page.isClosed()) await page.close().catch(() => {})
  let browserClosed = !browser
  if (browser) { await browser.close(); browserClosed = true }
  for (const response of streams) response.destroy()
  server.closeIdleConnections?.(); server.closeAllConnections?.()
  await new Promise((resolve) => server.close(resolve))
  await pause(20)
  const resolved = realpathSync(profile), root = realpathSync(output)
  assert(resolved.toLowerCase().startsWith((root + path.sep).toLowerCase()) && path.basename(resolved).startsWith('headless-profile-'))
  rmSync(resolved, { recursive: true, force: false })
  const hash = (file) => createHash('sha256').update(readFileSync(file)).digest('hex')
  const sourceFiles = ['src/store.js', 'src/views/Settings.vue', 'src/components/BrowserLoginGuide.vue']
  const distFiles = ['index.html', ...readdirSync(path.join(dist, 'assets')).map((name) => 'assets/' + name)]
  writeFileSync(path.join(output, 'result.json'), JSON.stringify({ checks, counts, failure,
    page_errors: pageErrors, external_requests: external.length, request_trace: trace,
    source_sha256: Object.fromEntries(sourceFiles.map((file) => [file, hash(path.join(web, file))])),
    dist_sha256: Object.fromEntries(distFiles.map((file) => [file, hash(path.join(dist, file))])),
    timeout_test: 'The browser fixture maps only the 20000ms action timer to 1500ms; production constant remains 20000ms.',
    browser_status_expiry_test: 'Only the final local test page advances Date.now by 61 seconds; a normal local clock tick expires status without HTTP.' }, null, 2) + '\n')
  writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify({ browser_closed: browserClosed,
    server_closed: !server.listening, profile_removed: !existsSync(profile), streams_remaining: streams.size }) + '\n')
}
console.log(JSON.stringify({ checks: checks.length, counts, failure }, null, 2))
if (failure) process.exitCode = 1
