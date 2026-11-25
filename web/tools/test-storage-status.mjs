// Same-build storage UI only: generated loopback JSON, no worker or audio deletion.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

const output = path.resolve(process.argv[2]), web = fileURLToPath(new URL('../', import.meta.url)), dist = path.join(web, 'dist')
assert(!existsSync(output), 'Use a unique evidence directory')
mkdirSync(output, { recursive: true })
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const sha = (file) => createHash('sha256').update(readFileSync(file)).digest('hex')
const hashes = () => ({ source: Object.fromEntries(['src/components/AsrStatusCard.vue', 'src/store.js', 'src/views/Settings.vue', 'tools/test-storage-status.mjs'].map((name) => [name, sha(path.join(web, name))])),
  dist: Object.fromEntries(['index.html', ...readdirSync(path.join(dist, 'assets')).map((name) => 'assets/' + name)].map((name) => [name, sha(path.join(dist, name))])) })
const before = hashes(), SECRET = 'synthetic-private-path-and-token:C:/not-for-rendering'
const GiB = 1073741824, checks = [], trace = [], pageErrors = [], external = [], streams = new Set()
const diagnostics = { preflight: [], browser_requests: [], browser_failures: [], browser_pid: null }
let wall = 1800000000, sequence = 0, reads = 0, posts = 0, otherWrites = 0, failureCode = 0
let deferRead = false, pendingRead = null
const token = 'generated-storage-write-token'.padEnd(64, 'x')
const disk = (extra = {}) => ({ free_bytes: 2 * GiB, total_bytes: 8 * GiB, low_space: false,
  threshold_bytes: GiB, sampled_at: wall, error: '', verified: true, stale: false, ...extra })
const storage = (extra = {}) => ({ schema: 1, snapshot_at: wall, verified: true, stale: false,
  audio_bytes: 1572864, audio_files: 3, transcription_pending: 1, cleanup_pending: 2, cleanup_failed: 3, cleanup_blocked: 4,
  scan: { partial: false, limited: false, checked: 12, started_at: wall - 2, updated_at: wall,
    last_complete_at: wall, approximate: true, counts_scope: 'scan_round' }, disk: disk(),
  last_cleanup: { at: wall, tag: '合成来源', date: '2000-01-01', segment: 'seg_0001.flv', state: 'deleted', reason: 'audio_deleted' }, ...extra })
const status = (extra = {}) => ({ generation: 'storage-boot-A', sequence: ++sequence, state: 'idle',
  verified: true, resource_held: true, stale: false, no_progress: false, snapshot_at: wall,
  counts: { pending: 0, projection_pending: 0, retryable: 0, blocked: 0, complete: 1 },
  scan: { partial: false, limited: false, checked: 1 }, current: {}, error: '', next_condition: 'periodic_scan',
  storage: storage(), ...extra })
let guide = status()
const send = (res, code, value, headers = {}) => { if (res.destroyed || res.writableEnded) return; res.writeHead(code, { 'Content-Type': 'application/json', ...headers }); res.end(JSON.stringify(value)) }
const server = http.createServer(async (req, res) => {
  const pathname = new URL(req.url, 'http://generated.invalid').pathname
  trace.push({ method: req.method, path: pathname })
  try {
    if (pathname === '/api/access') return send(res, 200, { mode: 'local' })
    if (pathname === '/api/local/session') return send(res, 200, { mode: 'local', role: 'local', user: '生成用户', write_token: token }, { 'Set-Cookie': 'storage_generated=1; HttpOnly; SameSite=Strict; Path=/' })
    if (pathname.startsWith('/api/')) {
      assert((req.headers.cookie || '').includes('storage_generated=1'))
      if (pathname === '/api/sse') {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write('event: snapshot\ndata: {"tags":[],"date":"2000-01-01","now_ts":1000}\n\n')
        streams.add(res); res.on('close', () => streams.delete(res)); return
      }
      if (pathname === '/api/asr' && req.method === 'GET') {
        reads++
        if (deferRead) { deferRead = false; pendingRead = { res, value: JSON.parse(JSON.stringify(guide)) }; return }
        return send(res, 200, { ok: true, status: guide })
      }
      if (pathname === '/api/asr' && req.method === 'POST') {
        assert.equal(req.headers['x-local-write-token'], token); assert.equal(req.headers.origin, base)
        let body = ''; for await (const chunk of req) { body += chunk; assert(body.length < 4096) }
        assert.deepEqual(JSON.parse(body), { action: 'retry' }); posts++
        if (failureCode) { const code = failureCode; failureCode = 0; return send(res, code, { error: SECRET }) }
        return send(res, 200, { ok: true, status: guide })
      }
      if (req.method !== 'GET') otherWrites++
      if (pathname === '/api/me') return send(res, 200, { user: '生成用户', role: 'local', mode: 'local' })
      if (pathname === '/api/anchors') return send(res, 200, { anchors: [], health: {} })
      if (pathname === '/api/llm') return send(res, 200, { available: false, enabled: false, config_enabled: false, enabled_source: 'config',
        control: { allowed: false, reason: 'global_paused', epoch: 1 }, providers: [], active_provider: '', today: {}, budget: { used_tokens: 0, cap_tokens: 0 }, flags: {}, errors: [] })
      if (pathname === '/api/llm/prices') return send(res, 200, { schema: 1, entries: [] })
      if (pathname === '/api/browser-login') return send(res, 200, { ok: true, status: { browser: { state: 'not_prepared' }, channel: { state: 'not_checked', source: 'none', target_tag: '', checked_at: null }, busy: false } })
      return send(res, 200, {})
    }
    const file = pathname.startsWith('/assets/') ? path.join(dist, pathname) : path.join(dist, 'index.html')
    assert(file.startsWith(dist + path.sep))
    res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8' }); res.end(readFileSync(file))
  } catch (error) { pageErrors.push('fixture: ' + error.message); if (!res.headersSent) res.writeHead(500); res.end() }
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const base = 'http://127.0.0.1:' + server.address().port
const pause = (ms = 100) => new Promise((resolve) => setTimeout(resolve, ms))
let browser = null, page = null, failure = null
const text = (id) => page.$eval('[data-testid="' + id + '"]', (node) => node.textContent)
const current = (id = 'storage-status') => page.$eval('[data-testid="' + id + '"]', (node) => node.dataset.current === 'true')
const refresh = async () => { await page.click('[data-testid="asr-refresh"]'); await page.waitForFunction(() => !document.querySelector('[data-testid="asr-refresh"]').disabled); await pause() }
const tick = async (seconds) => {
  wall += seconds; await page.evaluate((time) => { window.__storageNow = time * 1000 }, wall)
  await page.waitForFunction((time) => {
    const clocks = window.__storageTimers.filter((timer) => timer.active && timer.delay === 1000)
    return clocks.length > 0 && clocks.every((timer) => timer.at === time)
  }, { timeout: 5000 }, wall * 1000)
  await pause(30)
}
const record = (name) => { checks.push(name); writeFileSync(path.join(output, 'progress.json'), JSON.stringify({ checks, reads, posts })) }
const screenshot = (name) => page.$('[data-testid="storage-status"]').then((node) => node.screenshot({ path: path.join(output, name) }))
const ready = async () => { await page.waitForSelector('[data-testid="storage-status"]'); await page.waitForFunction(() => !document.querySelector('[data-testid="asr-refresh"]').disabled) }
try {
  for (const name of ['index.html', ...readdirSync(path.join(dist, 'assets')).map((name) => 'assets/' + name)]) {
    const pathname = name === 'index.html' ? '/settings' : '/' + name
    const response = await new Promise((resolve, reject) => {
      const request = http.get(base + pathname, { agent: false }, (res) => {
        const chunks = []; let size = 0
        res.on('data', (chunk) => { size += chunk.length; if (size > 2097152) res.destroy(new Error('generated preflight too large')); else chunks.push(chunk) })
        res.on('error', reject); res.on('end', () => resolve({ code: res.statusCode, body: Buffer.concat(chunks) }))
      })
      request.setTimeout(5000, () => request.destroy(new Error('generated loopback preflight timeout'))); request.on('error', reject)
    })
    assert.equal(response.code, 200); assert.equal(createHash('sha256').update(response.body).digest('hex'), sha(path.join(dist, name)))
    diagnostics.preflight.push({ path: pathname, status: response.code, bytes: response.body.length })
  }
  writeFileSync(path.join(output, 'server-preflight.json'), JSON.stringify({ listening: server.listening, base, ...diagnostics }, null, 2))
  browser = await puppeteer.launch({ executablePath: process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true,
    userDataDir: profile, args: ['--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-sync', '--disable-extensions'] })
  diagnostics.browser_pid = browser.process()?.pid || null
  page = await browser.newPage(); await page.setViewport({ width: 1400, height: 1000 })
  page.setDefaultNavigationTimeout(60000)
  page.on('pageerror', (error) => pageErrors.push(error.message)); await page.setRequestInterception(true)
  page.on('requestfailed', (request) => diagnostics.browser_failures.push({ path: new URL(request.url()).pathname, error: request.failure()?.errorText || '' }))
  page.on('request', (request) => {
    diagnostics.browser_requests.push({ method: request.method(), path: new URL(request.url()).pathname })
    if (!request.url().startsWith(base + '/') && !request.url().startsWith('data:')) { external.push(new URL(request.url()).origin); return request.abort('blockedbyclient') }
    request.continue()
  })
  await page.evaluateOnNewDocument(() => {
    window.__storageNow = 1800000000000; const OriginalDate = Date
    window.Date = class extends OriginalDate { static now() { return window.__storageNow } }
    const interval = window.setInterval, clear = window.clearInterval
    window.__storageTimers = []
    window.setInterval = (callback, delay, ...args) => {
      const record = { delay, at: null, active: true, id: null }
      record.id = interval((...params) => { callback(...params); record.at = window.__storageNow }, delay === 5000 ? 600000 : delay, ...args)
      window.__storageTimers.push(record); return record.id
    }
    window.clearInterval = (id) => { const record = window.__storageTimers.find((timer) => timer.id === id); if (record) record.active = false; clear(id) }
  })
  await page.goto(base + '/settings', { waitUntil: 'domcontentloaded' }); await ready()
  assert.equal(reads, 1); assert.equal(posts, 0); assert.equal(await current(), true)
  assert.match(await text('storage-audio-bytes'), /1.5 MiB/); assert.equal(await text('storage-audio-files'), '3')
  for (const [field, value] of [['transcription-pending', '1'], ['cleanup-pending', '2'], ['cleanup-failed', '3'], ['cleanup-blocked', '4']]) assert.equal(await text('storage-' + field), value)
  assert.match(await text('storage-scope'), /最近完整扫描.*近似量/); assert.match(await text('storage-disk'), /2 GiB/)
  assert.equal((await page.$$('[data-testid="storage-status"] button')).length, 0)
  assert.match(await text('storage-status'), /扫描不是原子快照.*逻辑大小.*物理空间/)
  await screenshot('01-complete-storage.png')
  record('storage piggybacks the single ASR GET; logical bytes/files and independent category counts, approximate complete scan, no storage actions')

  guide = status({ storage: storage({ audio_bytes: 0, audio_files: 0, scan: { ...storage().scan, partial: true, limited: true, last_complete_at: null } }) }); await refresh()
  assert.equal(await current(), true); assert.match(await text('storage-scope'), /本轮仅已检查部分.*不是全部/)
  await screenshot('02-partial-storage.png')
  for (const raw of [undefined, { schema: 2 }, storage({ audio_bytes: -1, audio_files: true, cleanup_pending: '0', private_path: SECRET }),
    storage({ audio_files: 1000000001 }), storage({ scan: { ...storage().scan, checked: 1000000001 } }),
    storage({ snapshot_at: wall - 1, scan: { ...storage().scan, updated_at: wall - 1, started_at: wall, last_complete_at: wall - 1 } }),
    storage({ snapshot_at: wall - 1, scan: { ...storage().scan, updated_at: wall - 1, last_complete_at: wall } })]) {
    guide = status({ storage: raw }); await refresh(); assert.equal(await current(), false)
    assert.equal(await text('storage-audio-bytes'), '待确认'); assert.equal(await text('storage-audio-files'), '待确认')
    assert.equal(await current('asr-state'), true); assert(!(await text('storage-status')).includes(SECRET))
  }
  guide = status({ storage: storage({ audio_files: 1000000000 }) }); await refresh()
  assert.equal(await current(), true); assert.equal(await text('storage-audio-files'), '1,000,000,000')
  record('partial/limited zero values never imply global zero; missing/versioned/bad numeric storage remains unknown without invalidating ASR')

  guide = status({ storage: storage({ snapshot_at: wall + 1, scan: { ...storage().scan, updated_at: wall + 1 } }) }); await refresh()
  assert.equal(await current(), false); assert.equal(await current('storage-disk'), true); assert.equal(await current('asr-state'), true)
  const beforeFuture = reads; await tick(1); assert.equal(await current(), false); assert.equal(reads, beforeFuture)
  await refresh(); assert.equal(await current(), false) // same rejected generation/sequence cannot heal on reread
  guide = status(); await refresh(); assert.equal(await current(), true)
  record('future stats receipt stays invalid through tick and same-sequence reread; a new valid sequence restores stats, independently of disk/ASR')

  guide = status({ storage: storage({ disk: disk({ sampled_at: wall + 1, free_bytes: GiB / 2, low_space: false }) }) }); await refresh()
  assert.equal(await current(), true); assert.equal(await current('storage-disk'), false); assert.equal(await page.$('[data-testid="storage-low-space"]'), null)
  const diskFutureReads = reads; await tick(1); assert.equal(reads, diskFutureReads); assert.equal(await current('storage-disk'), false)
  await refresh(); assert.equal(await current('storage-disk'), false)
  guide = status({ storage: storage({ disk: disk({ free_bytes: GiB / 2, low_space: false }) }) }); await refresh()
  assert.equal(await current('storage-disk'), true); assert.match(await text('storage-low-space'), /不足 1 GiB/)
  await screenshot('03-low-space.png')
  guide = status({ storage: storage({ disk: disk({ free_bytes: GiB, low_space: true }) }) }); await refresh(); assert.equal(await page.$('[data-testid="storage-low-space"]'), null)
  guide = status({ storage: storage({ disk: disk({ free_bytes: 0, total_bytes: GiB }) }) }); await refresh()
  assert.equal(await current('storage-disk'), true); assert.match(await text('storage-low-space'), /不足 1 GiB/)
  for (const bad of [disk({ free_bytes: -1 }), disk({ free_bytes: 10 * GiB, total_bytes: GiB }), disk({ free_bytes: '0' }), disk({ free_bytes: null }), disk({ free_bytes: 0, total_bytes: 0 })]) {
    guide = status({ storage: storage({ disk: bad }) }); await refresh(); assert.equal(await current('storage-disk'), false)
    assert.equal(await page.$('[data-testid="storage-low-space"]'), null); assert.match(await text('storage-disk'), /待确认/)
  }
  record('disk future receipt is separately latched; safe free<=total derives the strict 1GiB warning, ignoring raw low flag and invalid/unknown values')

  const oldStats = storage(); guide = status({ storage: oldStats }); await refresh(); await tick(16)
  guide = status({ storage: oldStats }); await refresh()
  assert.equal(await current(), false); assert.equal(await current('storage-disk'), true); assert.match(await text('storage-scope'), /上次记录/)
  await tick(44); guide = status({ storage: oldStats }); await refresh(); assert.equal(await current('storage-disk'), true)
  await tick(1); assert.equal(await current('storage-disk'), false); assert.equal(await current('asr-state'), true)
  guide = status({ storage: { ...oldStats, disk: disk() } }); await refresh()
  assert.equal(await current(), false); assert.equal(await current('storage-disk'), true)
  guide = status(); await refresh(); assert.equal(await current(), true)
  record('stats actual observation expires at15s despite heartbeat; disk stays independently fresh through60s and expires without renewing stats')

  guide = status(); await refresh(); const rollbackReads = reads
  await tick(-1); assert.equal(await current(), false); assert.equal(await current('storage-disk'), false)
  await tick(1); assert.equal(reads, rollbackReads); assert.equal(await current(), false); assert.equal(await current('storage-disk'), false)
  assert.equal(await current('asr-state'), true)
  await screenshot('04-cached-clock-rollback.png')
  await refresh(); assert.equal(await current(), false); assert.equal(await current('storage-disk'), false)
  guide = status(); await refresh(); assert.equal(await current(), true); assert.equal(await current('storage-disk'), true)
  const olderSamples = storage(); await tick(5); guide = status({ storage: olderSamples }); await refresh()
  assert.equal(await current(), true); assert.equal(await current('storage-disk'), true)
  const envelopeRollbackReads = reads
  await tick(-1); assert.equal(await current(), false); assert.equal(await current('storage-disk'), false)
  await tick(2); assert.equal(reads, envelopeRollbackReads); assert.equal(await current('asr-state'), true)
  assert.equal(await current(), false); assert.equal(await current('storage-disk'), false)
  await refresh(); assert.equal(await current(), false); assert.equal(await current('storage-disk'), false)
  guide = status({ storage: olderSamples }); await refresh(); assert.equal(await current(), true); assert.equal(await current('storage-disk'), true)
  guide = status(); await refresh(); const beforeExpiryRollback = reads
  await tick(61); assert.equal(await current(), false); assert.equal(await current('storage-disk'), false)
  await tick(-60); assert.equal(reads, beforeExpiryRollback); assert.equal(await current('asr-state'), true)
  assert.equal(await current(), false); assert.equal(await current('storage-disk'), false)
  await refresh(); assert.equal(await current(), false); assert.equal(await current('storage-disk'), false)
  guide = status(); await refresh(); assert.equal(await current(), true); assert.equal(await current('storage-disk'), true)
  record('cached sample-future, envelope-only-future and expired-then-clock-rollback receipts latch stats/disk through time recovery and same-sequence reread; a new valid sequence restores')

  for (const stateName of ['deleted', 'pending', 'kept', 'busy']) {
    guide = status({ storage: storage({ last_cleanup: { at: wall, tag: '合成来源', date: '2000-01-01', segment: 'seg_0001.flv', state: stateName, reason: 'cleanup_retry_exhausted' } }) }); await refresh()
    assert.match(await text('storage-last-cleanup'), /合成来源.*seg_0001.*自动清理重试上限/)
  }
  guide = status({ state: 'processing', error: 'cleanup_retry_exhausted', current: { tag: '合成来源', date: '2000-01-01', segment: 'seg_0001.flv', phase: 'cleaning', attempt: 1 },
    storage: storage({ last_cleanup: { at: wall, tag: SECRET, date: SECRET, segment: SECRET, state: 'deleted', reason: SECRET }, private_path: SECRET, deleted_now: true, freed_bytes: GiB }) }); await refresh()
  assert.match(await text('asr-current'), /清理已完成转写/); assert.match(await text('asr-error'), /自动清理/)
  assert(!(await text('storage-status')).includes(SECRET)); assert(!(await text('storage-status')).includes('[object Object]'))
  record('recent result states/reasons are allowlisted and sanitized; cleanup phase/failure stays distinct from ASR failure and never reports freed bytes')

  guide = status({ generation: 'storage-boot-B' }); await refresh(); assert.equal(await current(), true)
  guide = status({ generation: 'storage-boot-A' }); await refresh(); assert.equal(await current(), false); assert.equal(await current('storage-disk'), false)
  guide = status({ generation: 'storage-boot-B' }); await refresh()
  deferRead = true; await page.click('[data-testid="asr-refresh"]')
  for (let i = 0; i < 100 && !pendingRead; i++) await pause(20)
  assert(pendingRead)
  await page.click('a[href="/live"]'); await page.waitForFunction(() => !document.querySelector('[data-testid="storage-status"]'))
  guide = status({ generation: 'storage-boot-C' })
  await page.click('a[href="/settings"]'); await ready(); const beforeOldReply = await text('storage-status')
  send(pendingRead.res, 200, { ok: true, status: pendingRead.value }); pendingRead = null; await pause()
  assert.equal(await text('storage-status'), beforeOldReply); assert.equal(await current(), true)
  record('retired generations and a delayed GET after unmount cannot revive old storage or disk')

  for (const code of [401, 403]) {
    guide = status({ generation: 'storage-boot-C' }); await refresh(); failureCode = code
    const beforeAction = posts; await page.click('[data-testid="asr-retry"]')
    await page.waitForSelector('[data-testid="reconnect-local"]'); await page.click('[data-testid="reconnect-local"]'); await ready()
    assert.equal(posts, beforeAction + 1); assert.equal(await current(), true)
  }
  assert.equal(posts, 2); assert.equal(otherWrites, 0); assert.equal(pageErrors.length, 0); assert.equal(external.length, 0)
  record('401/403 invalidate the shared snapshot; explicit reconnect only GETs storage and never replays the two explicit retry actions')
} catch (error) { failure = { name: error.name, message: error.message, stack: error.stack } }
finally {
  if (pendingRead) { send(pendingRead.res, 200, { ok: true, status: pendingRead.value }); pendingRead = null }
  if (page && !page.isClosed()) await page.close()
  let exited = !browser
  if (browser) { const child = browser.process(); await browser.close(); for (let i = 0; i < 100 && child.exitCode === null && child.signalCode === null; i++) await pause(50); exited = child.exitCode !== null || child.signalCode !== null }
  for (const response of streams) response.end()
  server.closeAllConnections(); await new Promise((resolve) => server.close(resolve))
  const owned = realpathSync(profile); assert(owned.startsWith(realpathSync(output) + path.sep))
  let cleanupError = null
  if (exited) { try { rmSync(owned, { recursive: true, force: true }) } catch (error) { cleanupError = error.code || error.name } }
  const cleanup = { browser_process_exited: exited, server_closed: !server.listening, streams_remaining: streams.size, profile_removed: !existsSync(profile), cleanup_error: cleanupError }
  if (!exited || server.listening || streams.size || existsSync(profile) || cleanupError) failure ||= { message: 'owned cleanup incomplete' }
  const after = hashes(); try { assert.deepEqual(after, before) } catch (_) { failure ||= { message: 'source/dist changed during UI run' } }
  writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify(cleanup, null, 2))
  writeFileSync(path.join(output, 'result.json'), JSON.stringify({ checks, failure, reads, posts, otherWrites, trace, pageErrors, external, before, after, diagnostics,
    timing: 'Generated Date.now advances; every active real1s interval callback is observed before assertions. The 5s polling interval alone is delayed to600s. Navigation uses DOMContentLoaded plus card-specific readiness after own loopback/static preflight. Production timers/assertions unchanged; no real waiting15/60s claimed.' }, null, 2))
}
console.log(JSON.stringify({ checks: checks.length, failure, reads, posts, otherWrites }))
if (failure) process.exitCode = 1
