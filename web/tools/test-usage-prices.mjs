// node tools/test-usage-prices.mjs <absolute-evidence-dir> <real-module-fixtures.json>
// Only synthetic loopback endpoints; aggregate JSON comes from board_usage/llm_pricing.
import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

assert(process.argv[2] && path.isAbsolute(process.argv[2]) && process.argv[3] && path.isAbsolute(process.argv[3]))
const output = path.resolve(process.argv[2]); mkdirSync(output, { recursive: true })
const fixturePath = path.resolve(process.argv[3]), fixtures = JSON.parse(readFileSync(fixturePath, 'utf8'))
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const dist = fileURLToPath(new URL('../dist/', import.meta.url))
const legacyFile = fileURLToPath(new URL('../../scripts/dashboard_assets/index.html', import.meta.url))
const FAKE_KEY = 'synthetic-UI-key-DO-NOT-USE'
const counts = { prices: 0, budget: 0, model_test: 0, gate: 0, key_save: 0, key_delete: 0, backfill_post: 0, usage_reads: 0 }
const payloads = [], requests = [], checks = [], screenshots = [], errors = [], external = []
const streams = new Set(), held = new Set(), usagePlans = []
let globalMode = 'no_records', configCap = 321, priceMode = 'ok'
let priceBook = structuredClone(fixtures.prices)
function publicLLM() {
  return { available: true, enabled: false, control: { allowed: false, reason: 'global_paused', epoch: [0, 0, 0] },
    active_provider: 'demo', providers: [{ name: 'demo', protocol: 'openai_compatible', model: 'fixture-model',
      base_url: 'https://example.invalid/v1', has_key: false, key_tail: '', active: true }], today: fixtures[globalMode],
    budget: { used_tokens: 0, config_cap_tokens: configCap, cap_tokens: 17, pending: configCap !== 17 }, flags: {}, errors: [] }
}
const reply = (res, status, value) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(value)) }
async function body(req) { let value = ''; for await (const part of req) { value += part; assert(value.length < 20000) }; return JSON.parse(value || '{}') }
function holdUsage(options = {}) {
  let arrival, release
  const reached = new Promise((resolve) => { arrival = resolve }), gate = new Promise((resolve) => { release = resolve })
  usagePlans.push({ ...options, arrival, gate }); held.add(release)
  return { reached, release: () => { held.delete(release); release() } }
}
function task(tag, date) {
  if (date !== '2026-11-14') return { tag, data_date: date, task_id: null, state: 'not_requested', status: 'not_requested' }
  return { tag, data_date: date, task_id: tag === 'fixture-room' ? fixtures.ids.task : fixtures.ids.other_task,
    state: 'succeeded', status: 'succeeded', submitted_at: '2026-11-16T00:00:00+00:00',
    counts: { ok: 1, fail: 0, skipped: 0, windows: 1, windows_total: 1, generate_calls: 1 }, reason: 'completed', can_redo: true }
}
function review(tag, date) {
  const usage = date === '2026-11-14' ? (tag === 'fixture-room' ? fixtures.room : fixtures.other_room)
    : tag === 'fixture-room' ? fixtures.empty_room : fixtures.empty_other_room
  return { tag, date, room_id: 'synthetic-room', empty: false, files: { danmu: { exists: true, mtime: 1794700000 } },
    stats: { online_series: [], peak_online: 1, total_pv: 1 }, danmu: { permin: [], total: 12, latest: [] },
    portrait: { series: [{ ts: 1794700000, sentiment: {} }], latest: { portrait: { suggestions: ['preserved portrait output'] } } },
    coldline: { events: [], sessions: [] }, llm: usage }
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  requests.push({ method: req.method, path: url.pathname, query: Object.fromEntries(url.searchParams) })
  try {
    if (url.pathname === '/api/me') return reply(res, 200, { user: 'synthetic-ui', role: 'admin' })
    if (url.pathname === '/api/sse') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write('event: snapshot\ndata: {"tags":[],"date":"2026-11-16"}\n\n')
      streams.add(res); res.on('close', () => streams.delete(res)); return
    }
    if (url.pathname === '/api/llm') return reply(res, 200, publicLLM())
    if (url.pathname === '/api/llm/budget') {
      counts.budget++; const value = await body(req); assert.deepEqual(Object.keys(value), ['cap_tokens'])
      assert(Number.isSafeInteger(value.cap_tokens) && value.cap_tokens >= 0); configCap = value.cap_tokens
      payloads.push({ endpoint: 'budget', ...value }); return reply(res, 200, { ...publicLLM(), hint: '目标已保存，运行护栏待重新加载' })
    }
    if (url.pathname === '/api/llm/prices' && req.method === 'GET') return reply(res, 200, priceBook)
    if (url.pathname === '/api/llm/prices' && req.method === 'POST') {
      counts.prices++; const value = await body(req)
      assert.deepEqual(Object.keys(value).sort(), ['provider', 'model', 'currency', 'input_per_million', 'output_per_million'].sort())
      assert.equal(value.provider, 'demo'); assert(value.model)
      for (const name of ['input_per_million', 'output_per_million']) assert(value[name] === null || typeof value[name] === 'number' && Number.isFinite(value[name]) && value[name] >= 0)
      const removing = value.input_per_million === null && value.output_per_million === null
      assert(removing || /^[A-Z]{3,8}$/.test(value.currency))
      const entry = removing ? null : { ...value, source: 'user_entered', version: randomUUID().replaceAll('-', ''), saved_at: new Date().toISOString() }
      priceBook.entries = priceBook.entries.filter((old) => old.provider !== value.provider || old.model !== value.model)
      if (entry) priceBook.entries.push(entry)
      payloads.push({ endpoint: 'prices', ...value })
      if (priceMode === 'uncertain') return reply(res, 503, { error: 'Synthetic unconfirmed price response' })
      return reply(res, 200, { ok: true, config_saved: true, entry, prices: priceBook })
    }
    if (url.pathname === '/api/llm/usage') {
      counts.usage_reads++; const plan = usagePlans.shift() || {}
      const value = structuredClone(url.searchParams.get('task_id') === fixtures.ids.task ? fixtures.task : fixtures.other_task)
      plan.arrival?.(); if (plan.gate) await plan.gate
      return reply(res, plan.error ? 503 : 200, plan.error ? { error: 'Synthetic usage read failure' } : value)
    }
    if (url.pathname === '/api/review/dates') return reply(res, 200, { dates: ['2026-11-14', '2026-11-13'] })
    if (url.pathname === '/api/review/tags') return reply(res, 200, { tags: ['fixture-room', 'other-room'], all_tags: ['fixture-room', 'other-room'] })
    if (url.pathname === '/api/review') return reply(res, 200, review(url.searchParams.get('tag'), url.searchParams.get('date')))
    if (url.pathname === '/api/review/backfill' && req.method === 'GET') return reply(res, 200, task(url.searchParams.get('tag'), url.searchParams.get('date')))
    if (url.pathname === '/api/state') {
      const usage = fixtures[globalMode]
      return reply(res, 200, { date: '2026-11-16', now: 1794700000, files: {}, danmu: { total: 0, counts: {}, latest: [] },
        portrait: { latest: null, series: [] }, coldline: { latest: [] }, metrics: { provider: 'demo', success_rate: null,
          prompt_tokens: null, completion_tokens: null, usage, cost_cny: null } })
    }
    const prohibited = { '/api/llm/test': 'model_test', '/api/llm/gate': 'gate', '/api/llm/settings': 'key_save',
      '/api/llm/key/delete': 'key_delete', '/api/review/backfill': 'backfill_post' }
    if (prohibited[url.pathname]) { counts[prohibited[url.pathname]]++; return reply(res, 500, { error: 'Unexpected action in read/estimate tests' }) }
    if (url.pathname === '/legacy') { res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); return res.end(readFileSync(legacyFile, 'utf8').replaceAll('__DASHBOARD_DATE__', '2026-11-16')) }
    if (['/settings', '/review'].includes(url.pathname) || /^\/assets\/[\w.-]+\.(js|css)$/.test(url.pathname)) {
      const name = url.pathname.startsWith('/assets/') ? url.pathname.slice(1) : 'index.html'
      res.writeHead(200, { 'Content-Type': (name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html') + '; charset=utf-8' })
      return res.end(readFileSync(path.join(dist, name)))
    }
    return reply(res, 404, { error: 'Synthetic endpoint not registered' })
  } catch (_) { return reply(res, 500, { error: 'Synthetic fixture failed' }) }
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const origin = 'http://127.0.0.1:' + server.address().port
let browser, page
try {
  browser = await puppeteer.launch({ executablePath: process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, userDataDir: profile, args: ['--disable-gpu', '--no-proxy-server'] })
  page = await browser.newPage(); page.on('pageerror', (error) => errors.push(error.message))
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (request.url().startsWith(origin + '/') || request.url().startsWith('data:')) request.continue()
    else { external.push(new URL(request.url()).origin); request.abort() }
  })
  await page.evaluateOnNewDocument(() => {
    const interval = window.setInterval
    window.setInterval = (fn, ms, ...args) => interval(fn, ms === 3000 ? 600000 : ms, ...args)
  })
  page.setDefaultTimeout(10000); page.setDefaultNavigationTimeout(30000)
  await page.setViewport({ width: 1440, height: 1100 })
  const text = (selector) => page.$eval(selector, (el) => el.textContent.trim())
  const waitText = (selector, value) => page.waitForFunction((selector, value) => document.querySelector(selector)?.textContent.includes(value), {}, selector, value)
  const enabled = (selector) => page.waitForFunction((selector) => { const el = document.querySelector(selector); return el && !el.disabled }, {}, selector)
  const fill = (selector, value) => page.$eval(selector, (el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })) }, value)
  async function shot(name) { assert(!(await text('body')).includes(FAKE_KEY)); await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true }); screenshots.push(name + '.png') }
  async function global(value) {
    globalMode = value; await page.click('[data-testid=refresh-ai-control]')
    await enabled('[data-testid=refresh-ai-control]')
  }
  await page.goto(origin + '/settings', { waitUntil: 'domcontentloaded' })
  await waitText('[data-testid=global-usage-coverage]', '没有用量记录'); await enabled('[data-testid=save-prices]')
  assert((await text('[data-testid=global-estimate-missing]')).includes('不代表费用为零'))
  assert.equal(await page.$eval('[data-testid=token-budget]', (el) => el.value), '321')
  await fill('[data-testid=token-budget]', '7'); await page.click('[data-testid=save-token-budget]'); await waitText('[data-testid=token-budget-notice]', '已保存')
  await enabled('[data-testid=save-token-budget]'); assert.equal(configCap, 7)
  await fill('[data-testid=token-budget]', '0'); await page.click('[data-testid=save-token-budget]'); await enabled('[data-testid=save-token-budget]'); assert.equal(configCap, 0)
  for (const value of ['', '-1', '1.5', '1e309']) {
    await fill('[data-testid=token-budget]', value); await page.click('[data-testid=save-token-budget]')
    await waitText('[data-testid=token-budget-notice]', '非负整数'); assert.equal(counts.budget, 2)
  }
  assert(!(await text('body')).includes('238095')); assert(!(await text('body')).includes('元/天'))
  checks.push('Token cap is saved exactly as a nonnegative integer including 7 and 0; invalid/empty values do not write and pending cap is retained')

  await fill('#provider-key', FAKE_KEY)
  await fill('[data-testid=price-input]', ''); await fill('[data-testid=price-output]', '')
  await page.click('[data-testid=save-prices]'); await waitText('[data-testid=price-notice]', '单价已清除'); await enabled('[data-testid=save-prices]')
  assert.equal(payloads.at(-1).input_per_million, null); assert.equal(payloads.at(-1).output_per_million, null)
  await fill('[data-testid=price-currency]', 'CNY'); await fill('[data-testid=price-input]', '0'); await fill('[data-testid=price-output]', '0')
  await page.click('[data-testid=save-prices]'); await waitText('[data-testid=price-notice]', '单价已保存'); await enabled('[data-testid=save-prices]')
  assert.equal(payloads.at(-1).input_per_million, 0); assert.equal(payloads.at(-1).output_per_million, 0)
  await waitText('[data-testid=price-snapshot]', '输入 0 / 输出 0 CNY')
  await fill('[data-testid=price-input]', '.125'); await fill('[data-testid=price-output]', '2.5')
  await page.click('[data-testid=save-prices]'); await enabled('[data-testid=save-prices]'); assert.equal(payloads.at(-1).input_per_million, .125)
  await fill('[data-testid=price-currency]', 'USDT'); await fill('[data-testid=price-input]', '0'); await fill('[data-testid=price-output]', '')
  await page.click('[data-testid=save-prices]'); await enabled('[data-testid=save-prices]'); assert.equal(payloads.at(-1).output_per_million, null)
  const priceWrites = counts.prices
  for (const value of ['-1', 'NaN', 'Infinity', '1e309', '0x10', 'true']) {
    await fill('[data-testid=price-input]', value); await page.click('[data-testid=save-prices]')
    await waitText('[data-testid=price-notice]', '非负有限'); assert.equal(counts.prices, priceWrites)
  }
  assert.equal(await page.$eval('#provider-key', (el) => el.value), FAKE_KEY)
  assert.equal(configCap, 0); assert.equal(await text('[data-testid=ai-control-state]'), '已暂停内置AI调用')
  checks.push('Blank/null, explicit zero, decimals and one-sided prices differ; invalid/non-finite prices never POST, and price edits preserve key drafts, gates and token caps')

  await fill('[data-testid=price-currency]', 'USD'); await fill('[data-testid=price-input]', '.25'); priceMode = 'uncertain'
  await page.click('[data-testid=save-prices]'); await waitText('[data-testid=price-notice]', '保存结果未确认'); await enabled('[data-testid=reload-prices]')
  const uncertainWrites = counts.prices; priceMode = 'ok'
  await page.click('[data-testid=reload-prices]'); await enabled('[data-testid=save-prices]')
  assert.equal(counts.prices, uncertainWrites); await waitText('[data-testid=price-snapshot]', 'USD')
  checks.push('An unconfirmed price save is resolved by a read-only reload without automatic retries or model/control writes')

  await global('no_price'); await waitText('[data-testid=global-estimate-missing]', '没有可用估价依据')
  await global('zero_complete'); await waitText('[data-testid=global-estimate]', 'CNY 0')
  await waitText('[data-testid=global-usage-coverage]', 'usage 完整')
  await global('zero_unknown'); await waitText('[data-testid=global-usage-coverage]', '仅部分已知')
  await waitText('[data-testid=global-estimate]', '已知部分估算：CNY 0')
  assert(!(await text('[data-testid=global-usage]')).includes('usage 完整'))
  await shot('01-zero-estimate-with-unknown-usage')
  await global('legacy'); await waitText('[data-testid=global-usage]', '旧口径记录 1'); await waitText('[data-testid=global-estimate-missing]', '不代表费用为零')
  checks.push('No price/no records/legacy usage are not free; explicit zero is valid but a zero amount never converts partial usage into complete coverage')

  await global('mixed'); await waitText('[data-testid=global-usage]', 'USD')
  for (const estimate of fixtures.mixed.estimates) assert((await text('[data-testid=global-usage]')).includes(estimate.currency + ' ' + estimate.amount))
  await waitText('[data-testid=global-usage]', '探测记录 1'); await waitText('[data-testid=global-usage]', fixtures.snapshots.decimal.version)
  assert(!(await text('[data-testid=global-usage]')).includes(priceBook.entries[0].version))
  await shot('02-mixed-currency-and-historical-snapshots')
  checks.push('Real aggregate JSON keeps currencies separate, probes excluded, and historical snapshot versions unchanged after editing current prices')

  await page.goto(origin + '/review?tag=fixture-room&date=2026-11-14', { waitUntil: 'domcontentloaded' })
  await waitText('[data-testid=portrait-task-state]', '成功'); await waitText('[data-testid=task-usage-coverage]', '仅部分已知')
  await waitText('[data-testid=room-usage]', '材料日期 2026-11-14')
  await page.$eval('[data-testid=task-usage] details', (el) => { el.open = true })
  await waitText('[data-testid=task-usage]', '2026-11-15'); await waitText('[data-testid=task-usage]', '2026-11-16')
  assert((await text('[data-testid=room-usage]')).includes('已知输入 100 token'))
  assert(requests.some((r) => r.path === '/api/llm/usage' && r.query.task_id === fixtures.ids.task && r.query.data_date === '2026-11-14' && !r.query.call_date))
  await shot('03-successful-task-partial-fees-cross-call-days')
  checks.push('Task success and partial fee coverage remain separate; selected-room totals exclude other rooms and old material is grouped by actual attempt days')

  const failedRead = holdUsage({ error: true }); await page.click('[data-testid=task-usage-refresh]'); await failedRead.reached; failedRead.release()
  await waitText('[data-testid=task-usage-notice]', '读取失败'); assert.equal(await text('[data-testid=portrait-task-state]'), '成功')
  assert.equal(await page.$('[data-testid=task-estimate]'), null)
  await page.click('[data-testid=task-usage-refresh]'); await waitText('[data-testid=task-usage-coverage]', '仅部分已知')
  const oldRead = holdUsage(); await page.click('[data-testid=task-usage-refresh]'); await oldRead.reached
  await page.select('[data-testid=review-tag]', 'other-room'); await waitText('[data-testid=task-usage-coverage]', 'usage 完整')
  oldRead.release(); await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 100)))
  assert((await text('[data-testid=task-usage]')).includes(fixtures.ids.other_task))
  assert(!(await text('[data-testid=task-usage]')).includes(fixtures.ids.task))
  await page.select('[data-testid=review-date]', '2026-11-13'); await waitText('[data-testid=portrait-task-state]', '尚未生成')
  assert.equal(await page.$('[data-testid=task-usage]'), null)
  checks.push('Usage read failures stay unknown without changing successful task state; late replies cannot cross selected room/date/task, and all recovery is GET-only')

  globalMode = 'no_records'; await page.goto(origin + '/legacy', { waitUntil: 'domcontentloaded' })
  await waitText('#hd-llm', '无估价依据'); assert(!(await text('#hd-llm')).includes('0.000'))
  const tooltip = await page.$eval('#hd-llm', (el) => el.title)
  assert(tooltip.includes('已知输入 未知 / 输出 未知')); assert(tooltip.includes('SDK 尝试')); assert(tooltip.includes('不是 HTTP 次数或画像点'))
  globalMode = 'mixed'; await page.reload({ waitUntil: 'domcontentloaded' }); await waitText('#hd-llm', 'USD')
  for (const estimate of fixtures.mixed.estimates) assert((await text('#hd-llm')).includes(estimate.currency + ' ' + estimate.amount))
  await shot('04-legacy-currency-estimates-without-zero-fallback')
  checks.push('The legacy release page has no null-to-zero money/token fallback and shows separate exact currency estimates with accurate metric/SDK units')

  for (const name of ['model_test', 'gate', 'key_save', 'key_delete', 'backfill_post']) assert.equal(counts[name], 0)
  assert.deepEqual(errors, []); assert.deepEqual(external, [])
  const html = readFileSync(path.join(dist, 'index.html'), 'utf8'), assets = ['index.html', ...[...html.matchAll(/\/assets\/([^"']+)/g)].map((m) => 'assets/' + m[1])]
  const value = { passed: true, checks, counts, payloads, screenshots, external_requests: 0, no_page_errors: true,
    production_dist: Object.fromEntries(assets.map((name) => [name, createHash('sha256').update(readFileSync(path.join(dist, name))).digest('hex')])),
    legacy_html_sha256: createHash('sha256').update(readFileSync(legacyFile)).digest('hex'),
    real_fixture_sha256: createHash('sha256').update(readFileSync(fixturePath)).digest('hex'),
    fixture_provenance: JSON.parse(readFileSync(path.join(path.dirname(fixturePath), 'provenance.json'), 'utf8')),
    note: 'Independent fake endpoints and synthetic data; usage/pricing JSON generated by actual modules. No real model/account/key/production service.' }
  writeFileSync(path.join(output, 'verification.json'), JSON.stringify(value, null, 2) + '\n')
  console.log(JSON.stringify({ passed: true, checks: checks.length, counts }))
} catch (error) {
  writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ name: error.name, message: error.message, counts, requests }, null, 2) + '\n')
  if (page) await page.screenshot({ path: path.join(output, 'failure-page.png'), fullPage: true }).catch(() => {})
  throw error
} finally {
  for (const release of held) release()
  if (browser) await browser.close()
  for (const stream of streams) stream.end()
  await new Promise((resolve) => server.close(resolve))
  const resolved = path.resolve(profile)
  assert.equal(path.dirname(resolved), output); assert(path.basename(resolved).startsWith('headless-profile-'))
  rmSync(resolved, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
  writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify({ browser_closed: true, loopback_server_closed: true,
    temporary_profile_removed: !existsSync(resolved) }, null, 2) + '\n')
}
