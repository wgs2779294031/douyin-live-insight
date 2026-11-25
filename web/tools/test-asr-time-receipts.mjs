// Built SPA, existing Edge, generated loopback only; no ASR/model/backend worker.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

const output = path.resolve(process.argv[2]), web = fileURLToPath(new URL('../', import.meta.url)), dist = path.join(web, 'dist')
assert(!existsSync(output), 'Use a new evidence directory')
mkdirSync(output, { recursive: true })
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const digest = (file) => createHash('sha256').update(readFileSync(file)).digest('hex')
const sourceHashes = () => Object.fromEntries(['src/components/AsrStatusCard.vue', 'src/store.js', 'src/views/Settings.vue', 'tools/test-asr-time-receipts.mjs'].map((name) => [name, digest(path.join(web, name))]))
const distHashes = () => Object.fromEntries(['index.html', ...readdirSync(path.join(dist, 'assets')).map((name) => 'assets/' + name)].map((name) => [name, digest(path.join(dist, name))]))
const before = { source: sourceHashes(), dist: distHashes() }
let wall = 1800000000, reads = 0, posts = 0, otherWrites = 0
const checks = [], trace = [], pageErrors = [], external = [], streams = new Set()
const token = 'generated-local-write-token'.padEnd(64, 'x')
const summary = (sequence, stamp = wall) => ({ generation: 'generated-boot-A', sequence, state: 'idle', verified: true,
  resource_held: true, stale: false, no_progress: false, snapshot_at: stamp,
  counts: { pending: 0, projection_pending: 0, retryable: 0, blocked: 0, complete: 1 },
  scan: { partial: false, limited: false, checked: 1 }, current: {}, error: '', next_condition: 'periodic_scan' })
let guide = summary(1, wall + 1), actionReply = null
const send = (res, value, headers = {}) => { res.writeHead(200, { 'Content-Type': 'application/json', ...headers }); res.end(JSON.stringify(value)) }
const server = http.createServer(async (req, res) => {
  const pathname = new URL(req.url, 'http://generated.invalid').pathname
  trace.push({ method: req.method, path: pathname })
  try {
    if (pathname === '/api/access') return send(res, { mode: 'local' })
    if (pathname === '/api/local/session') return send(res, { mode: 'local', role: 'local', user: '生成用户', write_token: token }, { 'Set-Cookie': 'generated=1; HttpOnly; SameSite=Strict; Path=/' })
    if (pathname.startsWith('/api/')) {
      assert((req.headers.cookie || '').includes('generated=1'))
      if (pathname === '/api/sse') {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write('event: snapshot\ndata: {"tags":[],"date":"2000-01-01","now_ts":1000}\n\n')
        streams.add(res); res.on('close', () => streams.delete(res)); return
      }
      if (pathname === '/api/asr' && req.method === 'GET') { reads++; return send(res, { ok: true, status: guide }) }
      if (pathname === '/api/asr' && req.method === 'POST') {
        assert.equal(req.headers['x-local-write-token'], token)
        assert.equal(req.headers.origin, base)
        let body = ''; for await (const chunk of req) { body += chunk; assert(body.length < 4096) }
        assert.deepEqual(JSON.parse(body), { action: 'retry' }); posts++
        return send(res, { ok: true, status: actionReply || guide })
      }
      if (req.method !== 'GET') otherWrites++
      if (pathname === '/api/me') return send(res, { user: '生成用户', role: 'local', mode: 'local' })
      if (pathname === '/api/anchors') return send(res, { anchors: [], health: {} })
      if (pathname === '/api/llm') return send(res, { available: false, enabled: false, config_enabled: false,
        enabled_source: 'config', control: { allowed: false, reason: 'global_paused', epoch: 1 }, providers: [],
        active_provider: '', today: {}, budget: { used_tokens: 0, cap_tokens: 0 }, flags: {}, errors: [] })
      if (pathname === '/api/llm/prices') return send(res, { schema: 1, entries: [] })
      if (pathname === '/api/browser-login') return send(res, { ok: true, status: { browser: { state: 'not_prepared' }, channel: { state: 'not_checked', source: 'none', target_tag: '', checked_at: null }, busy: false } })
      return send(res, {})
    }
    const file = pathname.startsWith('/assets/') ? path.join(dist, pathname) : path.join(dist, 'index.html')
    assert(file.startsWith(dist + path.sep))
    res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8' })
    res.end(readFileSync(file))
  } catch (error) { pageErrors.push('fixture: ' + error.message); if (!res.headersSent) res.writeHead(500); res.end() }
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const base = 'http://127.0.0.1:' + server.address().port
let browser = null, page = null, failure = null
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const view = () => page.evaluate(() => ({ current: document.querySelector('[data-testid="asr-state"]').dataset.current === 'true',
  green: document.querySelector('[data-testid="asr-dot"]').classList.contains('on'),
  retry: !document.querySelector('[data-testid="asr-retry"]').disabled }))
const unknown = async () => assert.deepEqual(await view(), { current: false, green: false, retry: false })
const advance = async (seconds) => { wall += seconds; await page.evaluate((time) => { window.__asrNow = time * 1000 }, wall); await pause(1150) }
const refresh = async () => { await page.click('[data-testid="asr-refresh"]'); await page.waitForFunction(() => !document.querySelector('[data-testid="asr-refresh"]').disabled); await pause(80) }
try {
  browser = await puppeteer.launch({ executablePath: process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, userDataDir: profile, args: ['--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-sync', '--disable-extensions'] })
  page = await browser.newPage(); await page.setViewport({ width: 1400, height: 1000 })
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (!request.url().startsWith(base + '/') && !request.url().startsWith('data:')) { external.push(new URL(request.url()).origin); return request.abort('blockedbyclient') }
    request.continue()
  })
  await page.evaluateOnNewDocument(() => {
    window.__asrNow = 1800000000000
    const OriginalDate = Date
    window.Date = class extends OriginalDate { static now() { return window.__asrNow } }
    const interval = window.setInterval
    window.setInterval = (callback, delay, ...args) => interval(callback, delay === 5000 ? 600000 : delay, ...args)
  })
  await page.goto(base + '/settings', { waitUntil: 'networkidle2' })
  await page.waitForSelector('[data-testid="asr-status-card"]')
  await page.waitForFunction(() => !document.querySelector('[data-testid="asr-refresh"]').disabled)
  await unknown(); assert.equal(reads, 1)
  await advance(1); await unknown(); assert.equal(reads, 1); assert.equal(posts, 0)
  await page.screenshot({ path: path.join(output, '01-future-stays-unknown.png'), fullPage: true })
  await advance(1); await unknown(); assert.equal(reads, 1)
  guide = summary(2); await refresh(); assert.deepEqual(await view(), { current: true, green: true, retry: true })
  checks.push('near-future +1s GET receipt stays invalid through clock-only ticks; a new valid receipt restores current/green/retry')
  await advance(14); await refresh(); assert.equal((await view()).current, true)
  await advance(2); await unknown(); await refresh(); await unknown()
  guide = summary(3); await refresh(); assert.equal((await view()).green, true)
  checks.push('duplicate sequence/timestamp GET cannot extend worker TTL; a new valid sequence restores it')
  actionReply = summary(4, wall + 1)
  await page.click('[data-testid="asr-retry"]')
  await page.waitForFunction(() => !document.querySelector('[data-testid="asr-refresh"]').disabled)
  await unknown(); assert.equal(posts, 1)
  const beforeTickReads = reads
  await advance(1); await unknown(); assert.equal(reads, beforeTickReads); assert.equal(posts, 1)
  guide = summary(5); await refresh(); assert.equal((await view()).green, true); assert.equal(posts, 1)
  await page.screenshot({ path: path.join(output, '02-new-valid-receipt.png'), fullPage: true })
  checks.push('future POST reply also stays invalid on tick; explicit GET recovers without replaying the single action')
  assert.equal(otherWrites, 0); assert.equal(pageErrors.length, 0); assert.equal(external.length, 0)
} catch (error) { failure = { name: error.name, message: error.message, stack: error.stack } }
finally {
  if (page && !page.isClosed()) await page.close()
  let exited = !browser
  if (browser) {
    const child = browser.process(); await browser.close()
    for (let i = 0; i < 100 && child.exitCode === null && child.signalCode === null; i++) await pause(50)
    exited = child.exitCode !== null || child.signalCode !== null
  }
  for (const response of streams) response.end()
  server.closeAllConnections(); await new Promise((resolve) => server.close(resolve))
  const owned = realpathSync(profile)
  assert(owned.startsWith(realpathSync(output) + path.sep))
  let cleanupError = null
  if (exited) { try { rmSync(owned, { recursive: true, force: true }) } catch (error) { cleanupError = error.code || error.name } }
  const cleanup = { browser_process_exited: exited, server_closed: !server.listening, streams_remaining: streams.size,
    profile_removed: !existsSync(profile), cleanup_error: cleanupError }
  if (!exited || server.listening || streams.size || existsSync(profile) || cleanupError) failure ||= { message: 'owned cleanup incomplete' }
  const after = { source: sourceHashes(), dist: distHashes() }
  try { assert.deepEqual(after, before) } catch (error) { failure ||= { message: 'source/dist changed during narrow UI run' } }
  writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify(cleanup, null, 2))
  writeFileSync(path.join(output, 'result.json'), JSON.stringify({ checks, failure, reads, posts, otherWrites, trace, pageErrors, external, before, after,
    timing: 'Date.now is generated and advanced; actual mounted 1s interval callbacks run. Only 5s GET poll is delayed to isolate clock-only changes. Production constants unchanged.' }, null, 2))
}
console.log(JSON.stringify({ checks: checks.length, failure, reads, posts, otherWrites }))
if (failure) process.exitCode = 1
