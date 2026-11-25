// Same-build local materials UI. Fresh retained profile; generated loopback only.
import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, statSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

const web = fileURLToPath(new URL('../', import.meta.url)), dist = path.join(web, 'dist')
const output = path.resolve(process.argv[2]), allowed = realpathSync(path.resolve(web, '../../task-09B/ui'))
assert(!existsSync(output) && path.isAbsolute(output))
assert(realpathSync(path.dirname(output)).startsWith(allowed + path.sep))
mkdirSync(output)
const profile = path.join(output, 'fresh-headless-profile'), downloads = path.join(output, 'downloads')
mkdirSync(profile); mkdirSync(downloads)
writeFileSync(path.join(output, 'owned.json'), JSON.stringify({ task: '09B', profile, downloads, fixtures: 'synthetic-only' }))
const sha = (raw) => createHash('sha256').update(raw).digest('hex')
const files = ['src/store.js', 'src/materials.js', 'src/components/LocalMaterials.vue', 'src/views/Review.vue', 'tools/test-local-materials.mjs']
const hashes = () => ({ source: Object.fromEntries(files.map((name) => [name, sha(readFileSync(path.join(web, name)))])),
  dist: Object.fromEntries(['index.html', ...readdirSync(path.join(dist, 'assets')).map((name) => 'assets/' + name)].map((name) => [name, sha(readFileSync(path.join(dist, name)))])) })
const before = hashes(), checks = [], trace = [], clicks = [], errors = [], external = [], streams = new Set(), pendingReads = []
const sidA = randomUUID(), sidB = randomUUID(), V1 = 'v_' + 'a'.repeat(32), V2 = 'v_' + 'b'.repeat(32)
const ROOM = '转写独有🙂', ZERO = '空语音房间', REMOVED = '移出名册', DAY = '2001-01-01', STAMP = 978278400
const G2 = 'ZzI.' + '1'.repeat(64), Q2 = 'cTI.' + '2'.repeat(64)
const SECRET = 'SHOULD_NOT_RENDER:C:/generated/private-path-and-key'
const RAW = '生成原文 13812345678 https://example.invalid/path <img src=x onerror="window.__materialXss=1">'
const token = 'generated-materials-write-token'.padEnd(64, 'x')
let serviceId = sidA, accessMode = 'local', active = null, last = null, materialVersion = null
let stale = false, stopping = false, queryFailure = 0, postFailure = 0, postMode = 'published'
let posts = 0, otherWrites = 0, pendingPost = null, deferQuery = false, corrupt = false
const terminal = new Map()
const completeness = (extra = {}) => ({ state: 'partial', coverage: 'observed_material_only', capture_complete: null,
  inputs_nonatomic: true, raw_freshness: 'snapshot_at_build_raw_not_rechecked', segments: [],
  counts: { current_success: 1, no_speech: 0, audio_cleaned: 1, incomplete: 0, failed: 0, bad_intent: 0,
    blocked: 0, legacy_unknown: 0, projection_pending: 0 }, ...extra })
const redaction = (view) => view === 'shared' ? { policy: 'legacy_url_email_phone', actual_replacements: null, original_row: null }
  : { policy: 'none', applied: false }
const diagnostic = () => ({ state: stale ? 'failed' : 'success', version: materialVersion,
  updated_at: STAMP, last_success_at: STAMP, error: stale ? 'material_gc_blocked' : '', snapshot_stale: stale })
const group = (index, tag, view, zero = false) => ({ id: String(index).padStart(24, '0'), platform: 'douyin', tag,
  date: DAY, from: STAMP, until_exclusive: STAMP + 86400, window_is_session: false, view,
  view_info: { available: true, records: zero ? 0 : 2, status: zero ? 'no_speech' : 'available', redaction: redaction(view) },
  source: { version: materialVersion, generated_at: STAMP, verified_at: STAMP, data_refs: ['speech_generated_2001-01-01.jsonl'],
    tag_is_speaker: false, time_basis: 'view_dependent_unverified' },
  completeness: completeness(zero ? { state: 'current_at_build', counts: { current_success: 1, no_speech: 1, audio_cleaned: 1 } } : {}) })
const row = (tag, view, index = 0) => ({ tag, platform: 'douyin', stream: 'speech', ts: STAMP + index,
  text: view === 'canonical' ? RAW + ' #' + index : '共享 [PHONE] [URL] #' + index,
  source: { version: materialVersion, generated_at: STAMP, verified_at: STAMP, tag_is_speaker: false,
    time_basis: view === 'canonical' ? 'writer_wall_clock_plus_offset' : 'stored_record_timestamp_unverified',
    material: { version: materialVersion, path: 'canonical/generated.jsonl', sha256: 'c'.repeat(64), byte_offset: index * 200 },
    freshness: 'snapshot_at_build_raw_not_rechecked', ...(view === 'canonical' ? { source_id: 'd'.repeat(64),
      generation: 'e'.repeat(32), result_sha256: 'f'.repeat(64), result_ref: 'audio_generated_2001-01-01/.results/seg_0001.json',
      json_pointer: '/rows/' + index, off: index, audio_state: 'cleaned', cleanup_state: 'deleted' } : {}) },
  completeness: completeness(), view, redaction: redaction(view), window_is_session: false, tag_is_speaker: false })
const pageValue = (items, next = null, extra = {}) => ({ version: materialVersion, items, next_cursor: next,
  refresh: diagnostic(), scan: { bytes: items.length * 200, lines: items.length || 1000, files: 1,
    limited: Boolean(next), complete: !next }, source_freshness: 'snapshot_at_build_raw_not_rechecked',
  window_is_session: false, tag_is_speaker: false, speaker_separation: false, order: 'manifest_file_then_line',
  content_verification: 'consumed_chunk_sha256', ...extra })
function status(params = new URLSearchParams()) {
  const request = params.get('request_id'), previousService = params.get('service_id')
  const changed = previousService && previousService !== serviceId
  const operation = changed ? null : request ? (active?.request_id === request ? active : terminal.get(request) || null) : active || last
  return { ok: true, service_id: serviceId, active_id: active?.request_id || null, operation,
    lookup: changed ? 'service_changed' : request ? operation ? 'found' : 'unknown' : 'latest',
    retention: { scope: 'http_instance', terminal_limit: 32, persistent: false, ttl_seconds: null }, stopping,
    material: { state: corrupt ? 'corrupt' : materialVersion ? 'ready' : 'not_ready',
      code: corrupt ? 'materials_corrupt' : materialVersion ? null : 'materials_not_ready',
      version: corrupt ? null : materialVersion, generated_at: materialVersion ? STAMP : null,
      verified_at: materialVersion ? STAMP : null, refresh: materialVersion ? diagnostic() : null } }
}
function send(res, code, value, headers = {}) {
  if (res.destroyed || res.writableEnded) return
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers })
  res.end(JSON.stringify(value))
}
function completePost(state = 'published', warning = null) {
  assert(active)
  const operation = { ...active, state, version: state === 'published' ? (materialVersion === V1 ? V2 : V1) : state === 'failed' ? materialVersion : null,
    previous: state === 'published' ? materialVersion : null, code: state === 'failed' ? 'discovery_capacity' : state === 'unconfirmed' ? 'material_publish_unconfirmed' : null,
    warning, finished_at: STAMP + 60 }
  if (state === 'published') { materialVersion = operation.version; stale = false }
  if (state === 'failed') stale = true
  terminal.set(operation.request_id, operation); last = operation; active = null
  const held = pendingPost; pendingPost = null
  if (held) send(held, 200, status(new URLSearchParams({ request_id: operation.request_id, service_id: serviceId })))
  return operation
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://generated.invalid'), pathname = url.pathname
  trace.push({ method: req.method, path: pathname, query: Object.fromEntries(url.searchParams) })
  try {
    if (pathname === '/api/access') return send(res, 200, { mode: accessMode })
    if (pathname === '/api/local/session') return send(res, 200, { mode: 'local', user: '生成用户', role: 'local', write_token: token },
      { 'Set-Cookie': 'materials_generated=1; HttpOnly; SameSite=Strict; Path=/' })
    if (pathname.startsWith('/api/')) {
      if (accessMode === 'local') assert((req.headers.cookie || '').includes('materials_generated=1'))
      if (pathname === '/api/sse') {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write('event: snapshot\ndata: {"tags":[],"date":"2001-01-01","now_ts":978278400}\n\n')
        streams.add(res); res.on('close', () => streams.delete(res)); return
      }
      if (pathname === '/api/materials/status') return send(res, 200, status(url.searchParams))
      if (pathname === '/api/materials/refresh' && req.method === 'POST') {
        posts++
        assert.equal(req.headers['x-local-write-token'], token); assert.equal(req.headers.origin, base)
        let raw = ''; for await (const chunk of req) { raw += chunk; assert(raw.length <= 512) }
        const body = JSON.parse(raw); assert.deepEqual(Object.keys(body), ['request_id'])
        if (postFailure) { const code = postFailure; postFailure = 0; return send(res, code, { error: SECRET }) }
        active = { service_id: serviceId, request_id: body.request_id, state: 'running', version: null,
          previous: null, code: null, warning: null, started_at: STAMP, finished_at: null }
        if (postMode === 'held') { pendingPost = res; return }
        pendingPost = res; completePost(postMode, postMode === 'published' ? 'material_gc_pending' : null); return
      }
      if (req.method !== 'GET') { otherWrites++; throw new Error('unexpected write') }
      if (pathname === '/api/materials/groups') {
        if (!materialVersion) return send(res, 404, { code: 'materials_not_ready', error: '尚未建立资料' })
        const view = url.searchParams.get('view') || 'canonical'
        return send(res, 200, pageValue(url.searchParams.has('cursor') ? [group(3, REMOVED, view)] : [group(1, ROOM, view), group(2, ZERO, view, true)], url.searchParams.has('cursor') ? null : G2))
      }
      if (pathname === '/api/materials/query') {
        if (queryFailure) { const code = queryFailure; queryFailure = 0; return send(res, code, { code: code === 409 ? 'cursor_expired' : 'materials_unavailable', error: SECRET }) }
        const view = url.searchParams.get('view') || 'canonical', tag = url.searchParams.get('room') || ROOM
        const data = pageValue(tag === ZERO ? [] : url.searchParams.has('cursor') ? [row(tag, view), row(tag, view, 1)] : [], tag === ZERO || url.searchParams.has('cursor') ? null : Q2)
        if (deferQuery) { deferQuery = false; pendingReads.push({ res, data }); return }
        return send(res, 200, data)
      }
      if (pathname === '/api/me') return send(res, 200, { mode: accessMode, user: '生成用户', role: accessMode === 'local' ? 'local' : 'admin' })
      if (pathname === '/api/review/dates') return send(res, 200, { dates: [] })
      if (pathname === '/api/review/tags') return send(res, 200, { tags: [], all_tags: [] })
      if (pathname === '/api/anchors') return send(res, 200, { anchors: [], health: {} })
      if (pathname === '/api/llm') return send(res, 200, { available: false, enabled: false, config_enabled: false, enabled_source: 'config',
        control: { allowed: false, reason: 'global_paused', epoch: 1 }, providers: [], active_provider: '', today: {}, budget: { used_tokens: 0, cap_tokens: 0 }, flags: {}, errors: [] })
      if (pathname === '/api/llm/prices') return send(res, 200, { schema: 1, entries: [] })
      return send(res, 200, {})
    }
    const file = pathname.startsWith('/assets/') ? path.resolve(dist, pathname.slice(1)) : path.join(dist, 'index.html')
    assert(file.startsWith(dist + path.sep))
    res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8' }); res.end(readFileSync(file))
  } catch (error) { errors.push('fixture: ' + error.message); if (!res.headersSent) res.writeHead(500); res.end() }
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const base = 'http://127.0.0.1:' + server.address().port
const pause = (ms = 80) => new Promise((resolve) => setTimeout(resolve, ms))
let browser = null, page = null, failure = null, browserExited = false, downloaded = null
const text = (id) => page.$eval('[data-testid="' + id + '"]', (node) => node.textContent)
const enabled = (id) => page.waitForFunction((key) => !document.querySelector('[data-testid="' + key + '"]').disabled, { timeout: 10000 }, id)
const click = async (id) => {
  await enabled(id)
  const located = await page.$eval('[data-testid="' + id + '"]', (node) => {
    const describe = () => { const box = node.getBoundingClientRect(), hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
      return { top: box.top, bottom: box.bottom, hit: hit?.closest('[data-testid]')?.dataset.testid || hit?.className || null,
        target: hit === node || node.contains(hit) } }
    const before = describe(); node.scrollIntoView({ block: 'center', inline: 'nearest' }); return { before, centered: describe() }
  })
  clicks.push({ id, ...located }); assert.equal(located.centered.target, true, 'Pointer must hit the actual button')
  await page.click('[data-testid="' + id + '"]'); await pause()
}
const fill = async (id, value) => { await page.$eval('[data-testid="' + id + '"]', (node, next) => { node.value = next; node.dispatchEvent(new Event('input', { bubbles: true })) }, value); await pause(20) }
const materialRequests = () => trace.filter((item) => item.path.startsWith('/api/materials/')).length
const record = (name) => { checks.push(name); writeFileSync(path.join(output, 'progress.json'), JSON.stringify({ checks, posts, requests: trace.length })) }
const screenshot = (name) => page.$('[data-testid="local-materials"]').then((node) => node.screenshot({ path: path.join(output, name) }))
async function settle() { await enabled('materials-status-read'); await pause() }
async function readStatus() { await click('materials-status-read'); await settle() }
async function readFirst() { await click('materials-query'); await enabled('materials-query'); await pause() }
try {
  for (const name of ['index.html', ...readdirSync(path.join(dist, 'assets')).map((name) => 'assets/' + name)]) {
    const response = await new Promise((resolve, reject) => {
      const request = http.get(base + (name === 'index.html' ? '/review' : '/' + name), { agent: false }, (res) => {
        const chunks = []; res.on('data', (chunk) => chunks.push(chunk)); res.on('error', reject)
        res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks) }))
      }); request.setTimeout(5000, () => request.destroy(new Error('preflight timeout'))); request.on('error', reject)
    })
    assert.equal(response.status, 200); assert.equal(sha(response.body), sha(readFileSync(path.join(dist, name))))
  }
  browser = await puppeteer.launch({ executablePath: process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, userDataDir: profile, args: ['--no-first-run', '--no-default-browser-check', '--no-proxy-server', '--disable-background-networking', '--disable-sync', '--disable-extensions'] })
  browser.process().once('exit', () => { browserExited = true })
  page = await browser.newPage(); await page.setViewport({ width: 1280, height: 800 }); await page.emulateTimezone('Asia/Shanghai')
  page.setDefaultTimeout(10000); page.setDefaultNavigationTimeout(60000)
  page.on('pageerror', (error) => errors.push(error.message)); await page.setRequestInterception(true)
  page.on('request', (request) => {
    const url = request.url()
    if (!(url.startsWith(base + '/') || url.startsWith('blob:' + base + '/') || url.startsWith('data:'))) { external.push(url); return request.abort('blockedbyclient') }
    request.continue()
  })
  await page.evaluateOnNewDocument(() => {
    const original = window.setTimeout
    window.__materialsTimerScale = false; window.__materialsPostDeadlineFired = 0
    window.setTimeout = (fn, delay, ...args) => delay === 45000 && window.__materialsTimerScale
      ? original(() => { window.__materialsPostDeadlineFired++; fn(...args) }, 80)
      : original(fn, delay, ...args)
    window.__materialBlobEvents = []
    const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL)
    URL.createObjectURL = (blob) => { const url = create(blob); window.__materialBlobEvents.push({ kind: 'create', url }); return url }
    URL.revokeObjectURL = (url) => { window.__materialBlobEvents.push({ kind: 'revoke', url }); return revoke(url) }
  })
  const cdp = await page.createCDPSession()
  await cdp.send('Page.setDownloadBehavior', { behavior: 'allow', downloadPath: downloads })
  await page.goto(base + '/review', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="local-materials"]'); await enabled('materials-update')
  assert.match(await text('materials-current'), /尚未建立资料/); assert.equal(posts, 0)
  assert(trace.some((item) => item.path === '/api/review/dates'))
  await screenshot('01-not-ready.png')
  record('local not_ready retains explicit update entry despite empty legacy review dates; mounting has zero material POST')

  await click('materials-update'); await enabled('materials-update')
  assert.equal(posts, 1); assert.match(await text('materials-request-result'), /本次已发布.*新版已提交/s)
  await page.waitForSelector('[data-group="000000000000000000000001"]')
  assert.match(await text('materials-groups'), /转写独有🙂.*空语音房间.*0 条.*空语音成功/s)
  assert.match(await text('materials-groups-more'), /还有材料分组/)
  await click('materials-groups-next'); assert.match(await text('materials-groups'), /移出名册/)
  await click('materials-groups-first'); await page.click('[data-group="000000000000000000000002"]'); await enabled('materials-query')
  assert.match(await text('materials-selected-facts'), /空语音成功 1/); assert.match(await text('materials-empty-page'), /本页没有正文/)
  record('published warning remains committed; groups paginate outside roster/60-day gates and zero-row no_speech remains selectable')

  await page.click('[data-group="000000000000000000000001"]'); await enabled('materials-query')
  assert.match(await text('materials-empty-page'), /本页没有正文/); await enabled('materials-query-next')
  await click('materials-query-next'); await enabled('materials-query')
  assert.match(await text('materials-records'), /13812345678/); assert.match(await text('materials-records'), /<img src=x/)
  assert.equal(await page.$eval('[data-testid="materials-records"]', (node) => node.querySelectorAll('img,script').length), 0)
  assert.equal(await page.evaluate(() => window.__materialXss), undefined)
  await screenshot('02-canonical-source.png')
  const beforeDownload = materialRequests()
  await click('materials-download')
  for (let count = 0; count < 100; count++) {
    const names = readdirSync(downloads).filter((name) => name.endsWith('.jsonl'))
    if (names.length) { downloaded = path.join(downloads, names[0]); if (statSync(downloaded).size) break }
    await pause(50)
  }
  assert(downloaded && existsSync(downloaded)); assert.match(path.basename(downloaded), /^materials-canonical-v_[a-f0-9]{32}-page\.jsonl$/)
  const rawDownload = readFileSync(downloaded, 'utf8')
  const lines = rawDownload.trimEnd().split('\n').map((line) => JSON.parse(line))
  assert.equal(lines.length, 3); assert.equal(lines[0].stream, 'materials_page_info'); assert.equal(lines[0].returned_items, 2)
  assert.equal(lines[0].export_scope, 'returned_page_only'); assert.equal(lines[0].version, materialVersion)
  assert.equal(lines[0].filters.room, ROOM); assert.equal(lines[0].has_more, false)
  assert.deepEqual(lines.slice(1), [row(ROOM, 'canonical'), row(ROOM, 'canonical', 1)])
  assert.deepEqual(lines[0].scan, pageValue(lines.slice(1)).scan); assert.equal(lines[0].window_is_session, false)
  assert.deepEqual(readFileSync(downloaded), Buffer.from(lines.map((line) => JSON.stringify(line)).join('\n') + '\n', 'utf8'))
  await pause(); assert.equal(materialRequests(), beforeDownload)
  const blobEvents = await page.evaluate(() => window.__materialBlobEvents)
  assert.equal(blobEvents.filter((item) => item.kind === 'create').length, 1)
  assert.equal(blobEvents.filter((item) => item.kind === 'revoke').length, 1)
  record('empty bounded page can continue; canonical text is escaped; actual current-page JSONL download preserves rows/source/meta with zero requests and Blob revocation')

  await page.select('[data-testid="materials-view"]', 'shared'); await fill('materials-from', '2001-01-01T00:00'); await fill('materials-until', '2001-01-02T00:00')
  await page.select('[data-testid="materials-stream"]', 'speech'); await readFirst(); await click('materials-query-next'); await enabled('materials-query')
  assert.match(await text('materials-records'), /共享 \[PHONE\] \[URL\]/); assert(!(await text('materials-records')).includes('13812345678'))
  const filtered = trace.filter((item) => item.path === '/api/materials/query').at(-1).query
  assert.equal(filtered.view, 'shared'); assert.equal(filtered.stream, 'speech'); assert.equal(Number(filtered.time_until) - Number(filtered.time_from), 86400)
  queryFailure = 409; await readFirst(); assert.match(await text('materials-query-error'), /从头读取/)
  assert.equal(await page.$eval('[data-testid="materials-query-next"]', (node) => node.disabled), true)
  assert.equal(await page.$eval('[data-testid="materials-download"]', (node) => node.disabled), true)
  assert(!(await text('local-materials')).includes(SECRET))
  record('view/stream/Beijing time filters reach read API; shared masking stays distinct;409 clears old page/cursor/download without update')

  deferQuery = true; await click('materials-query'); await page.waitForFunction(() => document.querySelector('[data-testid="materials-query"]').disabled)
  await fill('materials-room', REMOVED); await readFirst()
  for (const held of pendingReads.splice(0)) send(held.res, 200, held.data)
  await pause(); await click('materials-query-next'); await enabled('materials-query')
  assert.match(await text('materials-records'), /移出名册/); assert(!(await text('materials-records')).includes(ROOM))
  record('filter edit aborts old query; delayed old-room response cannot overwrite a newer room or append stale rows')

  postMode = 'held'; await page.evaluate(() => { window.__materialsTimerScale = true }); const heldBefore = posts
  await click('materials-update'); await page.waitForFunction(() => window.__materialsPostDeadlineFired === 1)
  await settle(); assert.equal(posts, heldBefore + 1); assert(active)
  assert.equal(await page.$eval('[data-testid="materials-update"]', (node) => node.disabled), true)
  await page.click('.nav a[href="/live"]'); await pause(); await page.click('.nav a[href="/review"]')
  await page.waitForSelector('[data-testid="local-materials"]'); await readStatus()
  assert.equal(posts, heldBefore + 1); assert.match(await text('materials-current'), /一轮更新仍在执行/)
  completePost('published', 'material_gc_pending'); await readStatus(); await enabled('materials-update')
  assert.equal(posts, heldBefore + 1); assert.match(await text('materials-request-result'), /本次已发布/)
  record('scaled45s POST deadline and component unmount only cause GET recovery; held server work stays active, no POST replay, published warning remains committed')

  postMode = 'failed'; await click('materials-update'); await enabled('materials-update')
  assert.match(await text('materials-request-result'), /本次整理失败.*计费/s); assert.match(await text('materials-current'), /当前可读取.*旧版/s)
  postMode = 'unconfirmed'; await click('materials-update'); await enabled('materials-update')
  assert.match(await text('materials-request-result'), /本次发布结果未确认/); assert.match(await text('materials-current'), /当前可读取/)
  const unchangedPosts = posts; serviceId = sidB; active = null; last = null; terminal.clear(); await readStatus()
  assert.equal(posts, unchangedPosts); assert.match(await text('materials-request-result'), /本次结果待确认/)
  assert.equal(JSON.parse(await page.evaluate(() => sessionStorage.getItem('local-materials-request:v1'))).service_id, sidA)
  record('failed retains a stale readable version; unconfirmed action stays distinct from readable current; service change preserves old UUID/service association as unknown')

  for (const code of [401, 403]) {
    postMode = 'published'; postFailure = code; const count = posts
    await click('materials-update'); await page.waitForSelector('[data-testid="reconnect-local"]')
    await page.click('[data-testid="reconnect-local"]'); await page.waitForSelector('[data-testid="local-materials"]'); await enabled('materials-update')
    assert.equal(posts, count + 1); assert(!(await text('local-materials')).includes(SECRET))
  }
  record('401 and403 after POST invalidate local session; reconnect performs read-only recovery and never repeats a write or echoes raw error')

  for (const [width, height, name] of [[1280, 800, '03-common-window.png'], [900, 700, '04-small-window.png']]) {
    await page.setViewport({ width, height }); await pause()
    assert(await page.$eval('[data-testid="local-materials"]', (node) => node.scrollWidth <= node.clientWidth + 1))
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await screenshot(name)
  }
  const beforeAccount = materialRequests(); accessMode = 'account'; await page.reload({ waitUntil: 'domcontentloaded' }); await page.waitForSelector('.shell')
  assert.equal(await page.$('[data-testid="local-materials"]'), null); assert.equal(materialRequests(), beforeAccount)
  assert.equal(otherWrites, 0); assert.equal(external.length, 0); assert.deepEqual(errors, [])
  record('1280x800 and900x700 material layouts have no horizontal overflow; account mode never mounts or calls local materials')
} catch (error) {
  failure = error
  try { if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }) } catch (_) {}
} finally {
  for (const held of pendingReads) held.res.destroy()
  if (pendingPost) pendingPost.destroy()
  for (const response of streams) response.end()
  if (browser) await browser.close()
  for (let count = 0; browser && !browserExited && count < 50; count++) await pause(20)
  server.closeAllConnections(); await new Promise((resolve) => server.close(resolve))
  const after = hashes()
  writeFileSync(path.join(output, 'receipt.json'), JSON.stringify({ checks, trace, clicks, posts, otherWrites, errors, external,
    before, after, profile_retained: profile, downloaded: downloaded ? { file: path.basename(downloaded), bytes: statSync(downloaded).size, sha256: sha(readFileSync(downloaded)) } : null,
    browser_exited: browserExited, server_listening: server.listening, streams_remaining: streams.size,
    scope: 'Final dist with synthetic Node HTTP protocol fixtures; real customer Handler HTTP tested separately. Only45s timer scaled to80ms for client-path check, not elapsed45s proof.',
    failure: failure ? String(failure.stack) : null }, null, 2))
  assert.deepEqual(after, before); assert.equal(server.listening, false); assert.equal(streams.size, 0)
  if (browser) assert(browserExited, 'owned browser must actually exit')
}
if (failure) throw failure
console.log(JSON.stringify({ checks, passed_groups: checks.length, posts, errors, profile_retained: true }))
