// Build this tree, then run: node tools/test-ai-controls.mjs <absolute-evidence-directory>
// The server, model configuration, worker states and account are synthetic.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

assert(process.argv[2] && path.isAbsolute(process.argv[2]), 'Provide an absolute evidence directory')
const output = path.resolve(process.argv[2])
mkdirSync(output, { recursive: true })
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const dist = fileURLToPath(new URL('../dist/', import.meta.url))
const executablePath = process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const FAKE_KEY = 'synthetic-ai-control-key-DO-NOT-USE'
const control = { allowed: false, reason: 'global_paused', epoch: 1 }
let controlPresent = true
let globalWriteMode = 'ok'
let anchorWriteMode = 'ok'
let delayedAnchorRead = false
let hasKey = true
let testDelay = 100
const counts = { save: 0, delete: 0, test: 0, global: 0, automatic: 0, provider: 0 }
const checks = []
const screenshots = []
const requests = []
const streams = new Set()
const rows = [
  { tag: 'fixture-idle', intent: false, legacy: true, watch_running: false, is_live: null, worker: 'waiting_collector' },
  { tag: 'fixture-offline', intent: true, legacy: true, watch_running: true, is_live: false, worker: 'waiting_live' },
  { tag: 'fixture-ready', intent: true, legacy: true, watch_running: true, is_live: true, worker: 'waiting_input' },
  { tag: 'fixture-unknown', intent: true, legacy: true, watch_running: true, is_live: true, worker: 'unknown' }
]
const publicLLM = () => ({ available: true, enabled: control.allowed, config_enabled: false,
  ...(controlPresent ? { control: { ...control } } : {}), active_provider: 'demo', enabled_source: 'runtime',
  providers: [{ name: 'demo', protocol: 'openai_compatible', base_url: 'https://example.invalid/v1',
    model: 'synthetic-model', has_key: hasKey, key_tail: '', active: true }],
  today: { calls: 0, probe_calls: 0, failed: 0, prompt_tokens: 0, completion_tokens: 0, by_provider: {} },
  budget: { used_tokens: 0, cap_tokens: 0, config_cap_tokens: 0 }, flags: {}, errors: [] })
function publicAnchors() {
  return { anchors: rows.map((row) => ({ tag: row.tag, name: row.tag, watch: true, record: false, enabled: true,
    llm: row.legacy, llm_gate_off: false, process_llm: true, state: row.watch_running ? 'ok' : 'stopped',
    watch_running: row.watch_running, is_live: row.is_live, watch_age_sec: 1,
    automatic_analysis: { enabled: row.intent, effective: row.intent && control.allowed && hasKey,
      reason: !row.intent ? 'automatic_disabled' : (!control.allowed ? 'global_paused' : (hasKey ? 'allowed' : 'no_key')) },
    worker_state: { state: row.worker, reason: row.worker === 'unknown' ? 'stale' : row.worker, observed_at: Date.now() / 1000 } })),
    health: { total: rows.length, enabled: rows.length, ok: 3, stopped: 1 } }
}
const reply = (res, status, value) => {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
  res.end(JSON.stringify(value))
}
async function body(req) {
  let raw = ''
  for await (const part of req) { raw += part; assert(raw.length < 20000) }
  return JSON.parse(raw || '{}')
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  requests.push({ method: req.method, path: url.pathname })
  try {
    if (url.pathname === '/api/me') return reply(res, 200, { user: 'synthetic-ui', role: 'admin' })
    if (url.pathname === '/api/sse') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' })
      res.write('event: snapshot\ndata: {"tags":[],"date":"2026-11-15","now_ts":1700000000}\n\n')
      streams.add(res); res.on('close', () => streams.delete(res)); return
    }
    if (url.pathname === '/api/llm' && req.method === 'GET') return reply(res, 200, publicLLM())
    if (url.pathname === '/api/llm/gate') {
      counts.global++
      const value = await body(req)
      assert.equal(typeof value.enabled, 'boolean')
      await new Promise((resolve) => setTimeout(resolve, 120))
      if (globalWriteMode === 'fail') return reply(res, 503, { error: 'Synthetic write failure' })
      if (globalWriteMode === 'ok') {
        control.allowed = value.enabled; control.reason = value.enabled ? 'allowed' : 'global_paused'; control.epoch++
      }
      return reply(res, 200, publicLLM())
    }
    if (url.pathname === '/api/llm/settings') {
      counts.save++; const value = await body(req)
      if (value.api_key) hasKey = true
      return reply(res, 200, { ok: true, config_saved: true, sync: { total: 0, synced: 0, failed: 0 } })
    }
    if (url.pathname === '/api/llm/key/delete') {
      counts.delete++; assert.deepEqual(await body(req), { provider: 'demo' }); hasKey = false
      return reply(res, 200, { ok: true, config_saved: true, sync: { total: 0, synced: 0, failed: 0 } })
    }
    if (url.pathname === '/api/llm/test') {
      counts.test++; await body(req)
      const allowedWhenSent = control.allowed
      await new Promise((resolve) => setTimeout(resolve, testDelay))
      return reply(res, 200, { ok: allowedWhenSent, code: allowedWhenSent ? 'ok' : 'global_paused' })
    }
    if (url.pathname === '/api/anchors' && req.method === 'GET') {
      const value = publicAnchors()
      if (delayedAnchorRead) { delayedAnchorRead = false; await new Promise((resolve) => setTimeout(resolve, 900)) }
      return reply(res, 200, value)
    }
    if (url.pathname === '/api/anchors' && req.method === 'PATCH') {
      counts.automatic++
      const value = await body(req)
      assert.deepEqual(Object.keys(value), ['llm']); assert.equal(typeof value.llm, 'boolean')
      await new Promise((resolve) => setTimeout(resolve, 180))
      if (anchorWriteMode === 'fail') return reply(res, 503, { error: 'Synthetic patch failure' })
      const row = rows.find((item) => item.tag === url.searchParams.get('tag'))
      row.intent = value.llm
      return reply(res, 200, { ok: true, anchor: { tag: row.tag, llm: row.intent } })
    }
    if (url.pathname === '/api/llm/provider') { counts.provider++; return reply(res, 200, publicLLM()) }
    if (['/settings', '/anchors'].includes(url.pathname) || /^\/assets\/[\w.-]+\.(js|css)$/.test(url.pathname)) {
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
  const errors = [], external = []
  let acceptPermission = true
  page.on('dialog', (dialog) => acceptPermission ? dialog.accept() : dialog.dismiss())
  page.on('pageerror', (error) => errors.push(error.message))
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (request.url().startsWith(origin + '/') || request.url().startsWith('data:')) request.continue()
    else { external.push(new URL(request.url()).origin); request.abort() }
  })
  page.setDefaultTimeout(10000); page.setDefaultNavigationTimeout(30000)
  await page.setViewport({ width: 1440, height: 1100 })
  const text = (selector) => page.$eval(selector, (el) => el.textContent.trim())
  async function waitText(selector, value) {
    await page.waitForFunction((selector, value) => document.querySelector(selector)?.textContent.includes(value), {}, selector, value)
  }
  async function fill(selector, value) {
    await page.$eval(selector, (el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })) }, value)
  }
  async function ready() {
    await page.waitForFunction(() => { const b = document.querySelector('[data-testid=save-provider]'); return !!b && !b.disabled })
  }
  async function shot(name) {
    assert(!(await text('body')).includes(FAKE_KEY))
    await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true })
    screenshots.push(name + '.png')
  }
  const autoButton = (tag) => `[data-tag="${tag}"] [data-testid=automatic-analysis-toggle]`
  const worker = (tag) => `[data-tag="${tag}"] [data-testid=worker-state]`
  await page.goto(origin + '/settings', { waitUntil: 'domcontentloaded' })
  await waitText('[data-testid=ai-control-state]', '已暂停内置AI调用')
  assert.equal(await page.$eval('[data-testid=test-provider]', (el) => el.disabled), true)
  await page.$eval('[data-testid=test-provider]', (el) => { el.disabled = false; el.click() })
  await waitText('[data-testid=connection-notice]', '内置AI调用已暂停')
  assert.equal(counts.test, 0)
  await fill('#provider-key', FAKE_KEY)
  await page.click('[data-testid=save-provider]'); await waitText('[data-testid=provider-notice]', '配置已保存'); await ready()
  await fill('#provider-key', FAKE_KEY + '-draft')
  await page.click('[data-testid=delete-provider-key]')
  assert.equal(counts.delete, 0)
  assert.equal(await text('[data-testid=delete-provider-key]'), '确认删除密钥')
  await page.click('[data-testid=delete-provider-key]')
  await waitText('[data-testid=provider-notice]', '密钥已删除。'); await ready()
  assert.equal(counts.save, 1); assert.equal(counts.delete, 1); assert.equal(counts.test, 0)
  await shot('01-global-paused-config-available')
  checks.push('Global pause blocks test POST even if a disabled DOM button is forced; save/delete remain usable')

  await fill('#provider-key', FAKE_KEY)
  await page.click('[data-testid=save-provider]'); await waitText('[data-testid=provider-notice]', '配置已保存'); await ready()
  acceptPermission = false
  await page.click('[data-testid=global-ai-toggle]')
  assert.equal(counts.global, 0)
  acceptPermission = true
  await page.$eval('[data-testid=global-ai-toggle]', (el) => { el.click(); el.click() })
  await waitText('[data-testid=ai-control-state]', '已允许内置AI调用')
  assert.equal(counts.global, 1)
  assert(rows.every((row, i) => row.intent === (i !== 0)))
  assert(!(await text('body')).includes('config-on'))
  checks.push('Explicit permission confirmation and single-flight toggling preserve every anchor intent without restart guidance')

  testDelay = 650
  await page.click('[data-testid=test-provider]')
  await page.waitForFunction(() => document.querySelector('[data-testid=test-provider]')?.textContent.includes('测试中'))
  assert.equal(await page.$eval('[data-testid=global-ai-toggle]', (el) => el.disabled), false)
  await page.click('[data-testid=global-ai-toggle]')
  await waitText('[data-testid=ai-control-state]', '已暂停内置AI调用')
  await waitText('[data-testid=connection-notice]', '连接验证通过')
  await ready()
  assert.equal(counts.test, 1); assert.equal(control.allowed, false)
  assert.equal(await text('[data-testid=ai-control-state]'), '已暂停内置AI调用')
  assert.equal(await page.$eval('[data-testid=test-provider]', (el) => el.disabled), true)
  await shot('02-pause-while-test-settles')
  checks.push('Pause remains available during a sent test; its late success never re-enables global permission')

  await page.click('[data-testid=global-ai-toggle]'); await waitText('[data-testid=ai-control-state]', '已允许内置AI调用')
  globalWriteMode = 'fail'
  await page.click('[data-testid=global-ai-toggle]'); await waitText('[data-testid=ai-control-state]', '许可状态待确认')
  assert.equal(await page.$eval('[data-testid=test-provider]', (el) => el.disabled), true)
  assert.equal(control.allowed, true)
  await shot('03-control-failure-unknown')
  globalWriteMode = 'ok'
  await page.click('[data-testid=refresh-ai-control]'); await waitText('[data-testid=ai-control-state]', '已允许内置AI调用')
  globalWriteMode = 'mismatch'
  await page.click('[data-testid=global-ai-toggle]'); await waitText('[data-testid=ai-control-state]', '许可状态待确认')
  assert.equal(await page.$eval('[data-testid=test-provider]', (el) => el.disabled), true)
  assert.equal(control.allowed, true)
  globalWriteMode = 'ok'
  await page.click('[data-testid=refresh-ai-control]'); await waitText('[data-testid=ai-control-state]', '已允许内置AI调用')
  controlPresent = false
  await page.click('[data-testid=refresh-ai-control]'); await waitText('[data-testid=ai-control-state]', '许可状态待确认')
  assert.equal(await page.$eval('[data-testid=test-provider]', (el) => el.disabled), true)
  controlPresent = true
  control.allowed = false; control.reason = 'control_unavailable'; control.epoch++
  await page.click('[data-testid=refresh-ai-control]'); await waitText('[data-testid=ai-control-state]', '许可状态待确认')
  assert.equal(await page.$eval('[data-testid=test-provider]', (el) => el.disabled), true)
  assert.equal(await page.$eval('[data-testid=save-provider]', (el) => el.disabled), false)
  assert.equal(await page.$eval('[data-testid=delete-provider-key]', (el) => el.disabled), false)
  control.allowed = true; control.reason = 'allowed'; control.epoch++
  await page.click('[data-testid=refresh-ai-control]'); await waitText('[data-testid=ai-control-state]', '已允许内置AI调用')
  await page.click('[data-testid=global-ai-toggle]'); await waitText('[data-testid=ai-control-state]', '已暂停内置AI调用')
  checks.push('Write failures or missing control data show unknown and block tests until authoritative refresh')

  await page.goto(origin + '/anchors', { waitUntil: 'domcontentloaded' })
  await waitText(autoButton('fixture-idle'), '关')
  await waitText(worker('fixture-idle'), '等待采集进程')
  await waitText(worker('fixture-offline'), '等待开播')
  await waitText(worker('fixture-unknown'), '实际运行状态待确认')
  assert.equal(await text(autoButton('fixture-idle')), '关', 'Old llm/CLI flags must not grant automatic intent')
  await page.click('[data-testid=show-add-anchor]')
  assert.equal(await page.$eval('[data-testid=new-anchor-automatic]', (el) => el.checked), false)
  await page.click('[data-testid=show-add-anchor]')
  await shot('04-anchors-truthful-waiting')
  checks.push('Anchor default is off; old CLI flags never imply granted intent or running worker; idle/offline/stale receipts stay waiting/unknown')

  await page.$eval(autoButton('fixture-idle'), (el) => { el.click(); el.click() })
  await waitText(autoButton('fixture-idle'), '开')
  assert.equal(counts.automatic, 1); assert.equal(control.allowed, false)
  await waitText(worker('fixture-idle'), '等待采集进程')
  assert((await text('body')).includes('不阻止人工复盘和智能体读取数据'))
  checks.push('Automatic intent is independent of global permission; rapid clicks write once and do not claim worker startup')

  anchorWriteMode = 'fail'
  await page.click(autoButton('fixture-idle')); await waitText(autoButton('fixture-idle'), '待确认')
  assert((await text('body')).includes('自动实况设置未确认'))
  assert.equal(rows[0].intent, true)
  await shot('05-automatic-failure-unconfirmed')
  anchorWriteMode = 'ok'
  await page.click('[data-testid=refresh-anchors]'); await waitText(autoButton('fixture-idle'), '开')
  delayedAnchorRead = true
  await page.click('[data-testid=refresh-anchors]')
  await page.click(autoButton('fixture-idle')); await waitText(autoButton('fixture-idle'), '关')
  await new Promise((resolve) => setTimeout(resolve, 1100))
  assert.equal(await text(autoButton('fixture-idle')), '关')
  checks.push('Failed automatic writes stay unconfirmed; a late pre-write read cannot overwrite the latest saved state')

  control.allowed = true; control.reason = 'allowed'; control.epoch++
  rows[2].worker = 'waiting_input'
  await page.click('[data-testid=refresh-anchors]'); await waitText(worker('fixture-ready'), '已就绪，等待新事件')
  rows[2].worker = 'running'
  await page.click('[data-testid=refresh-anchors]'); await waitText(worker('fixture-ready'), '正在处理实况事件')
  await shot('06-worker-observed-state')
  checks.push('Only explicit backend worker states display ready or processing, independently of switch values')
  assert.deepEqual(errors, []); assert.deepEqual(external, [])
  assert.equal(await page.evaluate(() => localStorage.length), 0)
  assert.equal(await page.evaluate(() => sessionStorage.length), 0)
  const html = readFileSync(path.join(dist, 'index.html'), 'utf8')
  const assets = ['index.html', ...[...html.matchAll(/\/assets\/([^"']+)/g)].map((m) => 'assets/' + m[1])]
  const result = { passed: true, checks, counts, screenshots, no_page_errors: true, external_requests: 0,
    production_dist: Object.fromEntries(assets.map((name) => [name, createHash('sha256').update(readFileSync(path.join(dist, name))).digest('hex')])),
    note: 'All control and worker receipts are synthetic; real Python gate/worker behavior is checked separately.' }
  writeFileSync(path.join(output, 'verification.json'), JSON.stringify(result, null, 2) + '\n')
  console.log(JSON.stringify({ passed: true, checks: checks.length, counts, external_requests: 0 }))
} catch (error) {
  writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ name: error.name,
    message: String(error.message).replaceAll(FAKE_KEY, '[synthetic key]'), counts, requests }, null, 2) + '\n')
  if (page) await page.screenshot({ path: path.join(output, 'failure-page.png'), fullPage: true }).catch(() => {})
  throw error
} finally {
  if (browser) await browser.close()
  for (const stream of streams) stream.end()
  await new Promise((resolve) => server.close(resolve))
  const resolved = path.resolve(profile)
  assert.equal(path.dirname(resolved), output); assert(path.basename(resolved).startsWith('headless-profile-'))
  rmSync(resolved, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
  writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify({ browser_closed: true, loopback_server_closed: true,
    temporary_profile_removed: !existsSync(resolved) }, null, 2) + '\n')
}
