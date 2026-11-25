// Run after building this tree: node tools/test-model-settings.mjs <evidence-directory>
// All HTTP endpoints are synthetic and bind only a random loopback port.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

const output = path.resolve(process.argv[2] || '.model-settings-evidence')
mkdirSync(output, { recursive: true })
const deleteConfirmationOnly = process.argv[3] === 'delete-confirmation'
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const dist = fileURLToPath(new URL('../dist/', import.meta.url))
const browserExe = process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
assert(existsSync(browserExe), 'Set BROWSER_EXE to a dedicated test browser executable')
const FAKE_KEY = 'synthetic-ui-key-DO-NOT-USE-123456'
const REPLACEMENT_KEY = FAKE_KEY + '-replacement'
const ERROR_KEY = 'synthetic-error-key-DO-NOT-USE-654321'
const providers = {
  doubao: { protocol: 'doubao', base_url: 'https://example.invalid/doubao/v3', model: 'synthetic-model', api_key: '' },
  deepseek: { protocol: 'openai_compatible', base_url: 'https://example.invalid/deepseek/v1', model: 'synthetic-chat', api_key: '' }
}
let saveMode = 'ok'
let deleteMode = 'ok'
let testCode = 'ok'
const counts = { save: 0, delete: 0, test: 0, gate: 0, provider: 0 }
const payloadChecks = []
const checks = []
const screenshots = []
const streams = new Set()
const pages = []
const requestTrace = []
function publicState() {
  return { available: true, enabled: true, config_enabled: false, enabled_source: 'runtime',
    control: { allowed: true, reason: 'allowed', epoch: 1 },
    active_provider: 'doubao', providers: Object.entries(providers).map(([name, p]) => ({ name,
      protocol: p.protocol, base_url: p.base_url, model: p.model, has_key: !!p.api_key,
      key_tail: '', active: name === 'doubao' })),
    today: { calls: 0, probe_calls: 0, failed: 0, prompt_tokens: 0, completion_tokens: 0, by_provider: {} },
    budget: { used_tokens: 0, cap_tokens: 0, config_cap_tokens: 0 }, flags: {}, errors: [] }
}
function reply(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
  res.end(JSON.stringify(body))
}
async function readBody(req) {
  let text = ''
  for await (const part of req) {
    text += part
    assert(text.length < 20000, 'Synthetic test request unexpectedly large')
  }
  return text ? JSON.parse(text) : {}
}
const server = http.createServer(async (req, res) => {
  requestTrace.push({ method: req.method, path: req.url, side: 'fixture' })
  try {
    if (req.url === '/api/access') return reply(res, 200, { mode: 'account' })
    if (req.url === '/api/me') return reply(res, 200, { user: 'synthetic-ui', role: 'admin' })
    if (req.url === '/api/sse') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' })
      res.write('event: snapshot\ndata: {"tags":[],"date":"2026-11-14","now_ts":1700000000}\n\n')
      streams.add(res); res.on('close', () => streams.delete(res)); return
    }
    if (req.url === '/api/llm' && req.method === 'GET') return reply(res, 200, publicState())
    if (req.url === '/api/llm/settings') {
      counts.save++
      const b = await readBody(req)
      payloadChecks.push({ operation: 'save', provider: b.provider, key_submitted: Object.hasOwn(b, 'api_key') })
      await new Promise((resolve) => setTimeout(resolve, 150))
      if (saveMode === 'invalid') return reply(res, 400, { ok: false, code: 'invalid_config', config_saved: false,
        error: ERROR_KEY, message: ERROR_KEY, request: b })
      if (saveMode === 'storage') return reply(res, 503, { ok: false, code: 'storage_error', config_saved: false,
        error: ERROR_KEY, message: ERROR_KEY, request: b })
      const old = providers[b.provider] || {}
      providers[b.provider] = { ...old, protocol: b.protocol, base_url: b.base_url, model: b.model,
        api_key: b.api_key || old.api_key || '' }
      const partial = saveMode === 'partial'
      return reply(res, partial ? 503 : 200, { ok: !partial, code: partial ? 'partial_sync' : 'ok',
        config_saved: true, sync: { total: 2, synced: partial ? 1 : 2, failed: partial ? 1 : 0 },
        message: ERROR_KEY })
    }
    if (req.url === '/api/llm/key/delete') {
      counts.delete++
      const b = await readBody(req)
      assert.deepEqual(Object.keys(b), ['provider'])
      payloadChecks.push({ operation: 'delete', provider: b.provider, key_submitted: false })
      const p = providers[b.provider]
      if (p) p.api_key = ''
      const partial = deleteMode === 'partial'
      return reply(res, partial ? 503 : 200, { ok: !partial, code: partial ? 'partial_sync' : 'ok',
        config_saved: true, sync: { total: 2, synced: partial ? 1 : 2, failed: partial ? 1 : 0 },
        message: ERROR_KEY })
    }
    if (req.url === '/api/llm/test') {
      counts.test++
      const b = await readBody(req)
      payloadChecks.push({ operation: 'test', provider: b.provider, key_submitted: Object.hasOwn(b, 'api_key') })
      await new Promise((resolve) => setTimeout(resolve, testCode === 'slow_local' ? 13000 : 220))
      const code = b.api_key || providers[b.provider]?.api_key ? testCode : 'missing_key'
      return reply(res, 200, { ok: code === 'ok', code, latency_ms: 220, message: ERROR_KEY })
    }
    if (req.url === '/api/llm/gate') { counts.gate++; return reply(res, 200, publicState()) }
    if (req.url === '/api/llm/provider') { counts.provider++; return reply(res, 200, publicState()) }
    if (req.url === '/settings' || /^\/assets\/[\w.-]+\.(css|js)$/.test(req.url)) {
      const name = req.url === '/settings' ? 'index.html' : req.url.slice(1)
      const type = name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'
      res.writeHead(200, { 'Content-Type': type + '; charset=utf-8', 'Cache-Control': 'no-store' })
      return res.end(readFileSync(path.join(dist, name)))
    }
    reply(res, 404, { error: 'Synthetic endpoint not registered' })
  } catch (_) { reply(res, 500, { error: 'Synthetic fixture rejected request' }) }
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const origin = 'http://127.0.0.1:' + server.address().port
let browser
try {
  browser = await puppeteer.launch({ executablePath: browserExe, headless: true,
    userDataDir: profile, args: ['--disable-gpu', '--no-proxy-server'] })
  const page = await browser.newPage()
  pages.push(page)
  const errors = []
  const externalRequests = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('requestfailed', (request) => requestTrace.push({ method: request.method(),
    path: new URL(request.url()).pathname, side: 'browser', failure: request.failure()?.errorText }))
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (request.url().startsWith(origin + '/') || request.url().startsWith('data:')) request.continue()
    else { externalRequests.push(new URL(request.url()).origin); request.abort() }
  })
  await page.setViewport({ width: 1440, height: 1000 })
  page.setDefaultTimeout(10000)
  page.setDefaultNavigationTimeout(30000)
  await page.goto(origin + '/settings', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('#provider-key')
  const value = (selector) => page.$eval(selector, (el) => el.value)
  async function fill(selector, text) {
    await page.$eval(selector, (el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })) }, text)
  }
  async function notice(selector, text) {
    await page.waitForFunction((selector, text) => document.querySelector(selector)?.textContent.includes(text), {}, selector, text)
  }
  async function settled() { await page.waitForFunction(() => !document.querySelector('[data-testid=save-provider]')?.disabled) }
  async function noLeak() {
    const visible = await page.$eval('body', (el) => el.textContent)
    assert(!visible.includes(FAKE_KEY) && !visible.includes(ERROR_KEY), 'Key echoed into page text')
    assert.equal(await page.evaluate(() => localStorage.length), 0)
    assert.equal(await page.evaluate(() => sessionStorage.length), 0)
  }
  async function shot(name) {
    await noLeak()
    const form = await page.$('.provider-editor')
    const box = await form.evaluate((el) => {
      const r = el.getBoundingClientRect()
      return { x: r.x + scrollX, y: r.y + scrollY, width: r.width, height: r.height }
    })
    await page.screenshot({ path: path.join(output, name + '.png'), captureBeyondViewport: true,
      clip: { x: Math.max(0, box.x - 12), y: Math.max(0, box.y - 12), width: box.width + 24, height: box.height + 24 } })
    screenshots.push(name + '.png')
  }
  if (deleteConfirmationOnly) {
    async function flushRender() {
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve())))
    }
    async function resetSavedKeys() {
      providers.doubao = { protocol: 'doubao', base_url: 'https://example.invalid/doubao/v3',
        model: 'synthetic-model', api_key: FAKE_KEY }
      providers.deepseek = { protocol: 'openai_compatible', base_url: 'https://example.invalid/deepseek/v1',
        model: 'synthetic-chat', api_key: FAKE_KEY }
      await page.reload({ waitUntil: 'domcontentloaded' })
      await page.waitForFunction(() => {
        const button = document.querySelector('[data-testid=delete-provider-key]')
        return !!button && !button.disabled
      })
    }
    async function assertArmed() {
      await flushRender()
      const caption = await page.$eval('[data-testid=delete-provider-key]', (el) => el.textContent.trim())
      assert.equal(caption, '确认删除密钥', 'First click must leave a stable confirmation after reactive updates')
    }

    await resetSavedKeys()
    await fill('#provider-key', ERROR_KEY)
    const original = { ...providers.doubao }
    await page.click('[data-testid=delete-provider-key]')
    await assertArmed()
    assert.equal(counts.delete, 0, 'First delete click must not send a request')
    assert.equal(await value('#provider-key'), '', 'First delete click clears the unsaved key draft')
    assert.equal(providers.doubao.api_key, original.api_key, 'Arming does not change the saved key')
    await shot('01-draft-first-click-confirmation')
    await page.click('[data-testid=delete-provider-key]')
    await notice('[data-testid=provider-notice]', '密钥已删除。')
    await settled()
    assert.equal(counts.delete, 1, 'Exactly two clicks must produce exactly one deletion')
    assert.equal(providers.doubao.api_key, '')
    assert.equal(providers.doubao.base_url, original.base_url)
    assert.equal(providers.doubao.model, original.model)
    await shot('02-draft-second-click-deleted')
    checks.push('Saved key plus nonempty draft: first click only arms and clears draft; second click deletes exactly once')

    const edits = [
      ['model', () => fill('#provider-model', 'synthetic-edited-model')],
      ['API address', () => fill('#provider-url', 'https://example.invalid/edited/v1')],
      ['protocol', () => page.select('#provider-protocol', 'openai_compatible')],
      ['key draft', () => fill('#provider-key', ERROR_KEY)],
      ['provider', () => page.select('#provider-edit', 'deepseek')]
    ]
    for (const [field, edit] of edits) {
      await resetSavedKeys()
      const before = counts.delete
      await page.click('[data-testid=delete-provider-key]')
      await assertArmed()
      await edit()
      await flushRender()
      assert.equal(await page.$eval('[data-testid=delete-provider-key]', (el) => el.textContent.trim()), '删除已保存密钥',
        `${field} edit must cancel an old confirmation`)
      assert.equal(counts.delete, before)
      if (field === 'key draft') await shot('03-edit-cancels-old-confirmation')
      await page.click('[data-testid=delete-provider-key]')
      await assertArmed()
      assert.equal(counts.delete, before, `${field} edit requires a new first step`)
      await page.click('[data-testid=delete-provider-key]')
      await notice('[data-testid=provider-notice]', '密钥已删除。')
      await settled()
      assert.equal(counts.delete, before + 1, `${field} edit requires a new second step`)
      assert.equal(payloadChecks.at(-1).provider, field === 'provider' ? 'deepseek' : 'doubao')
      await noLeak()
      checks.push(`${field} editing invalidates confirmation; a fresh two-click sequence is required`)
    }
    assert.equal(counts.delete, 6)
    assert.equal(counts.save, 0)
    assert.equal(counts.test, 0)
    assert.equal(counts.gate, 0)
    assert.equal(counts.provider, 0)
  } else {
  await notice('[data-testid=no-key-notice]', '基础采集、转写和数据查询仍可使用')
  assert.equal(await page.$eval('#provider-key', (el) => el.type), 'password')
  assert.equal(await page.$eval('#provider-key', (el) => el.autocomplete), 'off')
  assert.deepEqual(counts, { save: 0, delete: 0, test: 0, gate: 0, provider: 0 })
  await shot('01-no-key')
  checks.push('No-key basic-use notice, password field, no automatic write/test/control calls')

  await fill('#provider-key', FAKE_KEY)
  await page.select('#provider-edit', 'deepseek')
  assert.equal(await value('#provider-key'), '')
  assert.equal(await value('#provider-protocol'), 'openai_compatible')
  checks.push('Provider selection clears plaintext key and selects the existing protocol')

  await page.select('#provider-edit', '__new__')
  await fill('#provider-key', FAKE_KEY)
  await page.click('[data-testid=save-provider]')
  await notice('[data-testid=provider-notice]', '请填写提供商标识')
  assert.equal(await value('#provider-key'), '')
  assert.equal(counts.save, 0)
  checks.push('Incomplete local draft clears key and is not submitted')
  await fill('#provider-name', 'synthetic-custom')
  await fill('#provider-url', 'https://example.invalid/custom/v1')
  await fill('#provider-model', 'synthetic-custom-model')
  await fill('#provider-key', FAKE_KEY)
  await page.$eval('[data-testid=save-provider]', (el) => { el.click(); el.click() })
  await notice('[data-testid=provider-notice]', '配置已保存，连接尚未验证')
  await settled()
  assert.equal(counts.save, 1)
  assert.equal(counts.test, 0)
  assert.equal(providers['synthetic-custom'].api_key, FAKE_KEY)
  assert.equal(await value('#provider-key'), '')
  assert.equal(await value('#provider-edit'), 'synthetic-custom')
  await shot('02-saved-unverified')
  checks.push('Custom compatible provider save/replacement, single submission, cleared key, saved is not tested')

  await fill('#provider-key', REPLACEMENT_KEY)
  await page.click('[data-testid=save-provider]')
  await notice('[data-testid=provider-notice]', '配置已保存')
  await settled()
  assert.equal(providers['synthetic-custom'].api_key, REPLACEMENT_KEY)
  assert.equal(await value('#provider-key'), '')
  checks.push('Existing provider replacement submits the new key and clears input')

  await fill('#provider-model', 'synthetic-updated-model')
  await page.click('[data-testid=save-provider]')
  await notice('[data-testid=provider-notice]', '配置已保存')
  await settled()
  assert.equal(providers['synthetic-custom'].api_key, REPLACEMENT_KEY)
  assert.equal(payloadChecks.at(-1).key_submitted, false)
  checks.push('Saving a blank key omits it and retains the saved key')

  const beforeTest = JSON.stringify(providers)
  await page.$eval('[data-testid=test-provider]', (el) => { el.click(); el.click() })
  await notice('[data-testid=connection-notice]', '连接验证通过')
  await settled()
  assert.equal(counts.test, 1)
  assert.equal(JSON.stringify(providers), beforeTest)
  assert.equal(counts.gate, 0)
  assert.equal(counts.provider, 0)
  await shot('03-test-success')
  checks.push('Connection test is click-only, single-flight, uses saved key, does not persist or enable controls')

  const outcomes = { auth_failed: '服务商拒绝了密钥', rate_limited: '服务商暂时限流',
    model_not_found: '接口未找到所填模型', timeout: '连接测试超时', network_error: '无法连接服务商',
    busy: '已有连接测试正在进行', upstream_error: '服务商未返回可用结果' }
  for (const [code, text] of Object.entries(outcomes)) {
    testCode = code
    await fill('#provider-key', FAKE_KEY)
    await page.click('[data-testid=test-provider]')
    await notice('[data-testid=connection-notice]', text)
    await settled()
    assert.equal(await value('#provider-key'), '')
    assert.equal(await page.$('.mask'), null, 'Provider failure incorrectly logged user out')
    await noLeak()
  }
  await shot('04-test-failure')
  checks.push('Fixed safe messages for 401/429/model missing/timeout/network/busy/upstream; key cleared, login retained')

  testCode = 'slow_local'
  const boundedStart = Date.now()
  await page.click('[data-testid=test-provider]')
  await page.waitForFunction(() => document.querySelector('[data-testid=connection-notice]')?.textContent.includes('等待连接测试结果超时'), { timeout: 15000 })
  await settled()
  assert(Date.now() - boundedStart >= 11500 && Date.now() - boundedStart < 15000)
  assert.equal(await value('#provider-key'), '')
  checks.push('Browser stops waiting after 12 seconds without automatic retries if the local service does not return')

  saveMode = 'invalid'
  await fill('#provider-key', ERROR_KEY)
  await page.click('[data-testid=save-provider]')
  await notice('[data-testid=provider-notice]', '配置未保存，请检查')
  await settled()
  assert.equal(await value('#provider-key'), '')
  await noLeak()
  saveMode = 'storage'
  await fill('#provider-key', ERROR_KEY)
  await page.click('[data-testid=save-provider]')
  await notice('[data-testid=provider-notice]', '未能完成配置操作')
  await settled()
  assert.equal(await value('#provider-key'), '')
  assert.equal(providers['synthetic-custom'].api_key, REPLACEMENT_KEY)
  checks.push('Validation/storage failures do not echo adversarial messages and clear plaintext input')

  saveMode = 'partial'
  await fill('#provider-key', ERROR_KEY)
  await page.click('[data-testid=save-provider]')
  await notice('[data-testid=provider-notice]', '配置已保存，监控配置同步尚未全部完成（1 项失败）')
  await settled()
  assert.equal(await page.$eval('[data-testid=provider-notice]', (el) => el.classList.contains('warning')), true)
  assert.equal(providers['synthetic-custom'].api_key, ERROR_KEY)
  assert.equal(await value('#provider-key'), '')
  await shot('05-partial-save')
  checks.push('HTTP503 partial-save body remains available and does not falsely report all saved or all failed')

  const beforeDelete = { ...providers['synthetic-custom'] }
  await page.click('[data-testid=delete-provider-key]')
  assert.equal(counts.delete, 0)
  await page.click('[data-testid=delete-provider-key]')
  await notice('[data-testid=provider-notice]', '密钥已删除。')
  await settled()
  assert.equal(counts.delete, 1)
  assert.equal(providers['synthetic-custom'].api_key, '')
  assert.equal(providers['synthetic-custom'].base_url, beforeDelete.base_url)
  assert.equal(providers['synthetic-custom'].model, beforeDelete.model)
  assert.equal(await page.$eval('[data-testid=delete-provider-key]', (el) => el.disabled), true)
  await shot('06-deleted-key')
  checks.push('Independent confirmed key deletion sends only provider and preserves endpoint/model')

  testCode = 'ok'
  await page.click('[data-testid=test-provider]')
  await notice('[data-testid=connection-notice]', '没有可用密钥')
  await settled()
  assert.equal(counts.gate, 0)
  assert.equal(counts.provider, 0)
  await noLeak()
  checks.push('No key test remains a safe error without affecting basic state or controls')

  saveMode = 'ok'
  await fill('#provider-key', FAKE_KEY)
  await page.click('[data-testid=save-provider]')
  await notice('[data-testid=provider-notice]', '配置已保存')
  await settled()
  deleteMode = 'partial'
  await page.click('[data-testid=delete-provider-key]')
  await page.click('[data-testid=delete-provider-key]')
  await notice('[data-testid=provider-notice]', '密钥已删除，监控配置同步尚未全部完成（1 项失败）')
  await settled()
  assert.equal(providers['synthetic-custom'].api_key, '')
  await noLeak()
  checks.push('Partial key deletion is distinguished from both full success and an unsaved failure')

  await page.setViewport({ width: 390, height: 844 })
  const countsBeforeRefresh = { ...counts }
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForSelector('#provider-key')
  assert.equal(await value('#provider-key'), '')
  assert.deepEqual(counts, countsBeforeRefresh)
  await shot('07-mobile-no-key')
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true)
  checks.push('390px viewport provider form has no horizontal page overflow')
  }
  assert.deepEqual(errors, [])
  assert.deepEqual(externalRequests, [])
  const result = { passed: true, checks, counts, payload_checks: payloadChecks,
    suite: deleteConfirmationOnly ? 'delete-confirmation' : 'model-settings', screenshots,
    production_dist: Object.fromEntries(['index.html', ...readFileSync(path.join(dist, 'index.html'), 'utf8').matchAll(/\/assets\/([^"']+)/g)].map((item) => typeof item === 'string' ? item : 'assets/' + item[1]).map((name) => [name, createHash('sha256').update(readFileSync(path.join(dist, name))).digest('hex')])),
    no_page_errors: true, external_requests: 0, no_real_api: true,
    note: 'Browser checks use local synthetic HTTP fixtures; backend behavior is verified separately. Adversarial fake error messages are intentionally ignored by the UI.' }
  writeFileSync(path.join(output, 'verification.json'), JSON.stringify(result, null, 2) + '\n')
  console.log(JSON.stringify({ passed: true, checks: checks.length, counts, external_requests: 0 }))
} catch (error) {
  const message = String(error.message).replaceAll(FAKE_KEY, '[synthetic key]').replaceAll(ERROR_KEY, '[synthetic key]')
  writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ error: error.name, message, counts, requestTrace }, null, 2) + '\n')
  if (pages[0]) await pages[0].screenshot({ path: path.join(output, 'failure-page.png'), fullPage: true }).catch(() => {})
  throw error
} finally {
  if (browser) await browser.close()
  for (const stream of streams) stream.end()
  await new Promise((resolve) => server.close(resolve))
  const resolvedProfile = path.resolve(profile)
  assert.equal(path.dirname(resolvedProfile), output, 'Only this run-owned profile may be removed')
  assert(path.basename(resolvedProfile).startsWith('headless-profile-'), 'Unexpected profile name')
  rmSync(resolvedProfile, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
  writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify({ browser_closed: true, loopback_server_closed: true,
    temporary_profile_removed: !existsSync(resolvedProfile) }, null, 2) + '\n')
}
