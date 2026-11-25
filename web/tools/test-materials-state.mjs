// New pure controller and actual store adapter only. All API/fetch responses are generated in memory.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createMaterialsController, createMaterialsState, materialsFilters, materialsPageExport } from '../src/materials.js'

const SERVICE = '11111111-1111-4111-8111-111111111111', OTHER = '22222222-2222-4222-8222-222222222222'
const REQUEST = '33333333-3333-4333-8333-333333333333', ACTIVE = '44444444-4444-4444-8444-444444444444'
const A = 'v_' + 'a'.repeat(32), B = 'v_' + 'b'.repeat(32)
const tick = () => new Promise((resolve) => setImmediate(resolve))
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const copy = (value) => JSON.parse(JSON.stringify(value))
const operation = (state = 'published', extra = {}) => ({ service_id: SERVICE, request_id: REQUEST,
  state, version: A, previous: null, code: null, warning: null, started_at: 10, finished_at: state === 'running' ? null : 11, ...extra })
const status = (extra = {}) => ({ ok: true, service_id: SERVICE, active_id: null, operation: null, lookup: 'latest', stopping: false,
  retention: { scope: 'http_instance', terminal_limit: 32, persistent: false, ttl_seconds: null },
  material: { state: 'ready', code: null, version: A, generated_at: 10, verified_at: 10, refresh: { state: 'success', snapshot_stale: false, error: '' } }, ...extra })
const group = (view = 'canonical', extra = {}) => ({ id: 'generated-group', tag: 'transcript_only', date: '2000-01-01', platform: 'douyin',
  view, view_info: { available: true, records: 0, status: 'no_speech', redaction: { policy: 'none', applied: false } },
  source: { time_basis: 'view_dependent_unverified' }, completeness: { state: 'current_at_build', counts: { no_speech: 1 } }, ...extra })
const row = (text = 'generated original', view = 'canonical') => ({ stream: view === 'canonical' ? 'speech' : 'danmu', view,
  tag: 'transcript_only', platform: 'douyin', ts: 10, text,
  source: { result_ref: 'audio_generated_2000-01-01/.results/seg_0001.json', json_pointer: '/rows/0' },
  completeness: { state: 'current_at_build' }, redaction: { policy: view === 'canonical' ? 'none' : 'legacy_url_email_phone' } })
const page = (items = [row()], next = null, version = A) => ({ version, items, next_cursor: next,
  scan: { bytes: 100, lines: items.length, files: 1, limited: Boolean(next), complete: !next },
  source_freshness: 'snapshot_at_build_raw_not_rechecked', order: 'manifest_file_then_line', content_verification: 'consumed_chunk_sha256' })
function memory() { const map = new Map(); return { getItem: (key) => map.get(key) || null, setItem: (key, value) => map.set(key, value), removeItem: (key) => map.delete(key) } }
function fixture(overrides = {}, storage = memory()) {
  const calls = [], api = {}
  const defaults = { status: async () => status(), groups: async (filters) => page([group(filters.view)]),
    query: async (filters) => page([row('generated original', filters.view)]), refresh: async () => status({ lookup: 'found', operation: operation() }) }
  for (const name of Object.keys(defaults)) api[name] = (...args) => { calls.push({ name, args }); return (overrides[name] || defaults[name])(...args) }
  const state = createMaterialsState(), controller = createMaterialsController({ api, state, storage, uuid: () => REQUEST })
  return { api, state, controller, calls, storage }
}
async function connected(overrides, storage) { const f = fixture(overrides, storage); f.controller.connect(); await tick(); return f }

test('exact legal room identifiers and Beijing half-open filters', () => {
  for (const room of [' 房间🙂尾点. ', '尾点.', 'emoji🙂']) assert.equal(materialsFilters({ room }).room, room)
  const value = materialsFilters({ view: 'shared', room: ' A ', date: '2026-01-01', stream: 'danmu', from: '2026-01-01T01:00', until: '2026-01-01T02:00' })
  assert.equal(new Date(value.time_from * 1000).toISOString(), '2025-12-31T17:00:00.000Z')
  assert.equal(value.limit, 50)
  for (const raw of [{ date: '2026-02-30' }, { from: '2026-02-30T00:00' }, { from: '2026-01-01T02:00', until: '2026-01-01T01:00' }, { room: 'bad\0tag' }, { room: {} }]) assert.throws(() => materialsFilters(raw))
})

test('groups discover transcript-only and zero-body groups independently of old review APIs', async () => {
  const f = await connected()
  assert.deepEqual(f.calls.map((call) => call.name), ['status', 'groups'])
  assert.equal(f.state.groups.items[0].date, '2000-01-01')
  assert.equal(f.state.groups.items[0].view_info.records, 0)
  assert.equal(f.state.groups.items[0].view_info.status, 'no_speech')
  assert.equal(f.state.groups.items[0].views, undefined)
  f.controller.dispose()
})

test('not_ready still permits an explicit update but never an automatic one', async () => {
  const f = await connected({ status: async () => status({ material: { state: 'not_ready', version: null, generated_at: null, verified_at: null, refresh: null } }) })
  assert.equal(f.state.statusFresh, true)
  assert.equal(f.state.material.state, 'not_ready')
  assert.deepEqual(f.calls.map((call) => call.name), ['status'])
  assert.equal(f.state.posting, false)
  f.controller.dispose()
})

test('empty groups and empty record pages retain cursor progress without appending pages', async () => {
  const f = await connected({ groups: async (filters) => filters.cursor ? page([group()]) : page([], 'next-group'),
    query: async (filters) => filters.cursor ? page([row('second-page')]) : page([], 'next-row') })
  assert.equal(f.state.groups.items.length, 0)
  await f.controller.readGroups(true)
  assert.equal(f.calls.at(-1).args[0].cursor, 'next-group')
  await f.controller.readPage()
  assert.equal(f.state.page.items.length, 0)
  await f.controller.readPage(true)
  assert.equal(f.calls.at(-1).args[0].cursor, 'next-row')
  assert.deepEqual(f.state.page.items.map((item) => item.text), ['second-page'])
  f.controller.dispose()
})

test('filter changes abort and discard late query replies', async () => {
  const late = deferred(); let reads = 0
  const f = await connected({ query: async () => ++reads === 1 ? late.promise : page([row('new-filter')]) })
  const old = f.controller.readPage(), signal = f.calls.at(-1).args[1].signal
  f.controller.editFilters({ room: ' new room ' })
  await f.controller.readPage()
  late.resolve(page([row('obsolete')], 'old-cursor')); await old
  assert(signal.aborted)
  assert.equal(f.state.page.items[0].text, 'new-filter')
  assert.equal(f.state.page.filters.room, ' new room ')
  assert.equal(f.state.page.next_cursor, null)
  f.controller.dispose()
})

test('view changes discard late groups and preserve the public view_info shape', async () => {
  const late = deferred(); let reads = 0
  const f = await connected({ groups: async (filters) => ++reads === 1 ? late.promise : page([group(filters.view)]) })
  f.controller.editFilters({ view: 'shared' }); await f.controller.readGroups()
  late.resolve(page([group('canonical')], 'old')); await tick()
  assert.equal(f.state.groups.items[0].view, 'shared')
  assert.equal(f.state.groups.items[0].view_info.records, 0)
  f.controller.dispose()
})

test('status version switches clear both cursors, selected facts and page cache', async () => {
  let latest = status()
  const f = await connected({ status: async () => latest, groups: async () => page([group()], 'groups-A'), query: async () => page([row()], 'rows-A') })
  await f.controller.readPage(); f.controller.selectGroup(group())
  latest = status({ material: { state: 'ready', version: B, generated_at: 20, verified_at: 20 } })
  await f.controller.readStatus(false)
  assert.equal(f.state.material.version, B)
  assert.equal(f.state.page, null); assert.equal(f.state.groups, null); assert.equal(f.state.selection, null)
  f.controller.dispose()
})

test('a different version page is rejected and only status is read, without automatic page looping', async () => {
  let reads = 0
  const f = await connected({ status: async () => ++reads === 1 ? status() : status({ material: { state: 'ready', version: B, generated_at: 20 } }), query: async () => page([row()], null, B) })
  await f.controller.readPage(); await tick()
  assert.equal(f.state.page, null)
  assert.equal(f.state.material.version, B)
  assert.equal(f.calls.filter((call) => call.name === 'query').length, 1)
  assert.equal(f.calls.filter((call) => call.name === 'refresh').length, 0)
  f.controller.dispose()
})

test('409 clears the returned page and a new first read omits the expired cursor', async () => {
  let reads = 0
  const f = await connected({ query: async () => { if (++reads === 2) throw { status: 409, code: 'cursor_expired' }; return page([row()], 'cursor') } })
  await f.controller.readPage(); await f.controller.readPage(true)
  assert.equal(f.state.page, null); assert.match(f.state.rowsError, /从头/)
  await f.controller.readPage()
  assert.equal(f.calls.at(-1).args[0].cursor, undefined)
  f.controller.dispose()
})

test('out-of-order status replies cannot revive an old service or material version', async () => {
  const late = deferred(); let reads = 0
  const f = await connected({ status: async () => { reads++; return reads === 2 ? late.promise : status(reads > 2 ? { service_id: OTHER, material: { state: 'ready', version: B } } : {}) } })
  const old = f.controller.readStatus(false)
  await f.controller.readStatus(false)
  late.resolve(status()); await old
  assert.equal(f.state.serviceId, OTHER); assert.equal(f.state.material.version, B)
  f.controller.dispose()
})

test('a groups version conflict clears the independent record page and both cursors', async () => {
  let groups = 0
  const f = await connected({ groups: async () => { if (++groups > 1) throw { status: 409, code: 'material_version_changed' }; return page([group()], 'groups-old') },
    query: async () => page([row()], 'rows-old') })
  await f.controller.readPage()
  await f.controller.readGroups(true)
  assert.equal(f.state.groups, null); assert.equal(f.state.page, null)
  assert.match(f.state.groupsError, /从头/)
  f.controller.dispose()
})

test('old service recovery stays unknown and preserves the old lookup pair after reconnect', async () => {
  const storage = memory(); storage.setItem('local-materials-request:v1', JSON.stringify({ request_id: REQUEST, service_id: SERVICE }))
  const f = await connected({ status: async () => status({ service_id: OTHER, lookup: 'service_changed' }) }, storage)
  assert.equal(f.state.requestState, 'unknown')
  assert.equal(f.state.recovery.service_id, SERVICE)
  f.controller.disconnect(); f.controller.connect(); await tick()
  assert.deepEqual(f.calls.filter((call) => call.name === 'status').at(-1).args[0], { request_id: REQUEST, service_id: SERVICE })
  assert.equal(f.calls.filter((call) => call.name === 'refresh').length, 0)
  f.controller.dispose()
})

test('one explicit POST is deduplicated locally; a published warning remains published', async () => {
  const pending = deferred(); let finished = false
  const final = status({ lookup: 'found', operation: operation('published', { version: B, warning: 'material_gc_pending' }), material: { state: 'ready', version: B, generated_at: 20 } })
  const f = await connected({ refresh: async () => pending.promise, status: async (lookup) => lookup.request_id ? finished ? final : status({ lookup: 'found', active_id: REQUEST, operation: operation('running') }) : status() })
  const first = f.controller.refresh(); assert.equal(await f.controller.refresh(), false)
  assert.equal(f.calls.filter((call) => call.name === 'refresh').length, 1)
  finished = true; pending.resolve(final); await first; await tick()
  assert.equal(f.state.requestState, 'published'); assert.equal(f.state.receipt.warning, 'material_gc_pending')
  assert.equal(f.state.material.version, B)
  f.controller.dispose()
})

test('POST timeout only GETs its receipt; a failed attempt and old current stay separate', async () => {
  const f = await connected({ refresh: async () => { throw { code: 'materials_timeout' } }, status: async (lookup) => lookup.request_id
    ? status({ lookup: 'found', operation: operation('failed', { code: 'discovery_capacity' }) }) : status() })
  await f.controller.refresh(); await tick()
  assert.equal(f.state.requestState, 'failed'); assert.equal(f.state.material.version, A)
  assert.equal(f.state.notice, '')
  assert.equal(f.calls.filter((call) => call.name === 'refresh').length, 1)
  assert.deepEqual(f.calls.filter((call) => call.name === 'status').at(-1).args[0], { request_id: REQUEST, service_id: SERVICE })
  f.controller.dispose()
})

for (const rejected of [{ status: 409, code: 'materials_busy' }, { status: 503, code: 'materials_stopping' },
  { status: 400, code: 'invalid_parameters' }, { status: 400, code: 'invalid_refresh_request' }]) {
  test('known unaccepted POST ' + rejected.code + ' clears pending without replay', async () => {
    const f = await connected({ refresh: async () => { throw rejected }, status: async () => status({ active_id: rejected.status === 409 ? ACTIVE : null, stopping: rejected.status === 503 }) })
    // Initial status must allow this first manual submit.
    f.state.activeId = null; f.state.stopping = false
    await f.controller.refresh(); await tick()
    assert.equal(f.state.requestState, 'not_accepted'); assert.equal(f.state.recovery, null)
    assert.equal(f.calls.filter((call) => call.name === 'refresh').length, 1)
    f.controller.dispose()
  })
}

test('generic POST 503 and a changed service do not become a known failure or success', async () => {
  const f = await connected({ refresh: async () => { throw { status: 503, code: 'materials_unavailable' } }, status: async (lookup) => lookup.request_id
    ? status({ service_id: OTHER, lookup: 'service_changed', material: { state: 'ready', version: B } }) : status() })
  await f.controller.refresh(); await tick()
  assert.equal(f.state.requestState, 'unknown'); assert.equal(f.state.recovery.service_id, SERVICE)
  assert.equal(f.state.material.version, B)
  f.controller.dispose()
})

test('HTTP-acknowledged CLI busy is a terminal receipt, not knowledge of the external worker', async () => {
  const answer = status({ lookup: 'found', operation: operation('busy', { version: null, code: 'material_busy' }) })
  const f = await connected({ status: async (lookup) => lookup.request_id ? answer : status(), refresh: async () => answer })
  await f.controller.refresh(); await tick()
  assert.equal(f.state.requestState, 'busy'); assert.equal(f.state.receipt.version, null)
  assert.equal(f.state.material.version, A); assert.equal(f.state.recovery.request_id, REQUEST)
  assert.equal(f.calls.filter((call) => call.name === 'refresh').length, 1)
  f.controller.dispose()
})

test('unconfirmed receipt has no claimed published version while current is separately readable', async () => {
  const answer = status({ lookup: 'found', operation: operation('unconfirmed', { version: null, finished_at: null }),
    material: { state: 'ready', version: B, generated_at: 20 } })
  const f = await connected({ status: async (lookup) => lookup.request_id ? answer : status(), refresh: async () => answer })
  await f.controller.refresh(); await tick()
  assert.equal(f.state.requestState, 'unconfirmed'); assert.equal(f.state.receipt.version, null)
  assert.equal(f.state.material.version, B)
  f.controller.dispose()
})

test('an auth loss during a POST keeps lookup recovery and reconnects using GET only', async () => {
  const pending = deferred(), storage = memory()
  const f = await connected({ refresh: async () => pending.promise, status: async (lookup) => lookup.request_id
    ? status({ lookup: 'found', operation: operation() }) : status() }, storage)
  const sent = f.controller.refresh()
  f.controller.disconnect(); pending.reject({ status: 403, code: 'auth' }); await sent
  assert.equal(f.state.requestState, 'unknown')
  f.controller.connect(); await tick()
  assert.equal(f.state.requestState, 'published')
  assert.equal(f.calls.filter((call) => call.name === 'refresh').length, 1)
  f.controller.dispose()
})

test('disconnect/unmount ignores late POST and a new component recovers by GET only', async () => {
  const pending = deferred(), storage = memory()
  const f = await connected({ refresh: async () => pending.promise }, storage)
  const sent = f.controller.refresh(); f.controller.dispose()
  pending.resolve(status({ lookup: 'found', operation: operation() })); await sent; await tick()
  assert.equal(f.state.requestState, 'unknown')
  assert.equal(f.calls.filter((call) => call.name === 'status').length, 1)
  const next = await connected({ status: async () => status({ lookup: 'found', operation: operation() }) }, storage)
  assert.equal(next.state.requestState, 'published')
  assert.equal(next.calls.filter((call) => call.name === 'refresh').length, 0)
  next.controller.dispose()
})

test('page export is exactly the returned page and metadata; it does not fetch cursors', async () => {
  const f = await connected({ query: async () => page([row('<img src=x onerror=alert(1)>')], 'not-exported-cursor') })
  await f.controller.readPage()
  const before = f.calls.length, exported = materialsPageExport(f.state.page), lines = exported.text.trimEnd().split('\n').map(JSON.parse)
  assert.equal(f.calls.length, before); assert.equal(lines.length, 2)
  assert.equal(lines[0].export_scope, 'returned_page_only'); assert.equal(lines[0].has_more, true)
  assert.deepEqual(lines[1], f.state.page.items[0]); assert.equal(lines[0].version, A)
  assert(!exported.text.includes('not-exported-cursor'))
  assert.match(exported.filename, /^materials-canonical-v_[a-f0-9]{32}-page\.jsonl$/)
  assert.throws(() => materialsPageExport({ ...f.state.page, filters: { view: '../../bad' } }))
  f.controller.dispose()
})

test('malformed status or wrong-view pages cannot be treated as a confirmed empty result', async () => {
  const bad = await connected({ status: async () => ({ ok: true, service_id: 'private-invalid' }) })
  assert.equal(bad.state.statusFresh, false); assert.equal(await bad.controller.refresh(), false)
  assert(!bad.state.statusError.includes('private-invalid')); bad.controller.dispose()
  const f = await connected({ query: async () => page([row('wrong', 'shared')]) })
  await f.controller.readPage(); assert.equal(f.state.page, null); assert(f.state.rowsError)
  f.controller.dispose()
})

test('actual materialsAPI preserves auth/no replay, deadlines and safe errors with only mocked fetch', async () => {
  const { initializeAccess, materialsAPI } = await import('../src/store.js')
  const originalFetch = globalThis.fetch, originalTimer = globalThis.setTimeout, originalClear = globalThis.clearTimeout
  const calls = [], timers = [], token = 'generated-materials-token'.padEnd(64, 'x')
  let responseCode = 200, pendingWrite = false
  globalThis.setTimeout = (fn, delay, ...args) => [6000, 45000].includes(delay) ? (timers.push({ fn, delay }), timers.at(-1)) : originalTimer(fn, delay, ...args)
  globalThis.clearTimeout = (handle) => { if (!timers.includes(handle)) originalClear(handle) }
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options })
    if (url === '/api/access') return new Response(JSON.stringify({ mode: 'local' }))
    if (url === '/api/local/session') return new Response(JSON.stringify({ mode: 'local', user: 'generated', write_token: token }))
    assert.equal(options.credentials, 'same-origin')
    if (options.method === 'POST') {
      assert.equal(options.headers.get('X-Local-Write-Token'), token)
      assert.deepEqual(JSON.parse(options.body), { request_id: REQUEST })
      if (pendingWrite) return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true }))
    }
    return new Response(JSON.stringify(responseCode === 200 ? { ok: true } : { code: responseCode === 503 ? 'materials_stopping' : 'auth', error: 'PRIVATE-RAW-ERROR' }), { status: responseCode })
  }
  try {
    await initializeAccess(); await materialsAPI.status()
    assert.equal(timers.at(-1).delay, 6000)
    responseCode = 503
    await assert.rejects(materialsAPI.refresh(REQUEST), (error) => error.code === 'materials_stopping' && !error.message.includes('PRIVATE'))
    assert.equal(timers.at(-1).delay, 45000)
    responseCode = 200; pendingWrite = true
    const timeout = materialsAPI.refresh(REQUEST); timers.at(-1).fn()
    await assert.rejects(timeout, (error) => error.code === 'materials_timeout')
    pendingWrite = false
    for (const code of [401, 403]) {
      await initializeAccess(); responseCode = code
      const count = calls.filter((call) => call.url === '/api/materials/refresh').length
      await assert.rejects(materialsAPI.refresh(REQUEST), (error) => error.status === code)
      assert.equal(calls.filter((call) => call.url === '/api/materials/refresh').length, count + 1)
      responseCode = 200
    }
  } finally { globalThis.fetch = originalFetch; globalThis.setTimeout = originalTimer; globalThis.clearTimeout = originalClear }
})

test('new component and local-only Review composition compile without building dist', async () => {
  const { parse, compileScript, compileTemplate } = await import('@vue/compiler-sfc')
  for (const filename of ['../src/components/LocalMaterials.vue', '../src/views/Review.vue']) {
    const { descriptor, errors } = parse(readFileSync(new URL(filename, import.meta.url), 'utf8'), { filename })
    assert.deepEqual(errors, [])
    const script = compileScript(descriptor, { id: 'materials-static-check' })
    const compiled = compileTemplate({ source: descriptor.template.content, filename, id: 'materials-static-check', compilerOptions: { bindingMetadata: script.bindings } })
    assert.deepEqual(compiled.errors, [])
  }
})
