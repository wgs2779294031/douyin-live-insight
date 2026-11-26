// Headless UI checks against synthetic loopback endpoints only. No Feishu request is made.
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

const output = path.resolve(process.argv[2] || '.feishu-notifications-evidence')
mkdirSync(output, { recursive: true })
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const dist = fileURLToPath(new URL('../dist/', import.meta.url))
const browserExe = process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
assert(existsSync(browserExe), 'Set BROWSER_EXE to a dedicated test browser executable')
const FAKE_WEBHOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/synthetic-DO-NOT-USE'
const FAKE_SECRET = 'synthetic-signing-secret-DO-NOT-USE'
const ERROR_SECRET = 'synthetic-private-upstream-error-DO-NOT-RENDER'
const TOKEN = 'synthetic-write-token-DO-NOT-USE-123456789'
let settings = { enabled: false, transport: 'webhook', source: 'unconfigured',
  webhook_configured: false, signing_secret_configured: false, legacy_app_available: false }
let webhook = '', signingSecret = '', mode = 'ok'
const requests = [], checks = [], streams = new Set(), externalRequests = [], pageErrors = [], expectedResourceErrors = [], trace = []
let pendingTest = null
const unchangedBitable = JSON.stringify({ enabled: false, backend: 'dry_run', app_id: 'synthetic-existing-app' })
let bitable = unchangedBitable
const publicState = () => ({ ...settings, destination_hint: ERROR_SECRET, unexpected_secret: ERROR_SECRET })
function reply(res, status, value) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
  res.end(JSON.stringify(value))
}
async function body(req) {
  let raw = ''
  for await (const block of req) { raw += block; assert(raw.length < 16000) }
  return JSON.parse(raw || '{}')
}
const server = http.createServer(async (req, res) => {
  trace.push({ method: req.method, path: req.url })
  try {
    if (req.url === '/api/access') return reply(res, 200, { mode: 'local' })
    if (req.url === '/api/local/session') return reply(res, 200, { mode: 'local', user: 'synthetic-local', role: 'local', write_token: TOKEN })
    if (req.url === '/api/sse') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' })
      res.write('event: snapshot\ndata: {"tags":[],"date":"2026-11-26","now_ts":1700000000}\n\n')
      streams.add(res); res.on('close', () => streams.delete(res)); return
    }
    if (req.url === '/api/notifications/feishu') {
      if (req.method === 'GET') return reply(res, 200, { ok: true, settings: publicState() })
      assert.equal(req.headers['x-local-write-token'], TOKEN, 'Local write token must be supplied')
      const data = await body(req)
      requests.push(data)
      if (mode === 'storage' && data.action === 'save') return reply(res, 503, {
        ok: false, code: 'storage_failed', error: ERROR_SECRET, message: ERROR_SECRET, request: data })
      if (data.action === 'save') {
        assert.equal(typeof data.enabled, 'boolean')
        assert(['webhook', 'legacy_app'].includes(data.transport))
        if (data.webhook_url) webhook = data.webhook_url
        if (data.signing_secret) signingSecret = data.signing_secret
        if (data.clear_signing_secret) { assert(!data.signing_secret); signingSecret = '' }
        settings = { ...settings, enabled: data.enabled, transport: data.transport, source: 'explicit',
          webhook_configured: !!webhook, signing_secret_configured: !!signingSecret }
        return reply(res, 200, { ok: true, code: 'settings_saved', delivered: false, settings: publicState(), message: ERROR_SECRET })
      }
      if (data.action === 'clear') {
        assert.deepEqual(data, { action: 'clear' })
        webhook = ''; signingSecret = ''
        settings = { ...settings, source: 'explicit', enabled: false, transport: 'webhook',
          webhook_configured: false, signing_secret_configured: false }
        return reply(res, 200, { ok: true, code: 'settings_cleared', delivered: false, settings: publicState() })
      }
      assert.deepEqual(data, { action: 'test' }, 'Test sends saved configuration only')
      if (mode === 'hold') { pendingTest = res; return }
      const code = mode === 'changed' ? 'configuration_changed' : mode === 'rejected' ? 'remote_rejected' : 'delivered'
      return reply(res, 200, { ok: code === 'delivered', code, delivered: code === 'delivered',
        settings: publicState(), message: ERROR_SECRET, error: ERROR_SECRET })
    }
    if (req.url === '/api/llm') return reply(res, 200, { providers: [], today: {}, budget: {}, flags: {}, errors: [], control: { allowed: false, reason: 'paused' } })
    if (req.url === '/api/anchors') return reply(res, 200, { anchors: [] })
    if (req.url === '/api/browser-login') return reply(res, 200, { status: {
      browser: { state: 'not_prepared' }, channel: { state: 'not_checked' }, busy: false } })
    if (req.url.startsWith('/api/')) return reply(res, 200, { ok: false, code: 'unavailable' })
    if (req.url === '/settings' || /^\/assets\/[\w.-]+\.(css|js)$/.test(req.url)) {
      const file = req.url === '/settings' ? 'index.html' : req.url.slice(1)
      res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' })
      return res.end(readFileSync(path.join(dist, file)))
    }
    reply(res, 404, {})
  } catch (error) { reply(res, 500, { code: 'synthetic_fixture_rejected' }); pageErrors.push(String(error)) }
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const origin = 'http://127.0.0.1:' + server.address().port
let browser, page
try {
  browser = await puppeteer.launch({ executablePath: browserExe, headless: true,
    userDataDir: profile, args: ['--disable-gpu', '--no-proxy-server'] })
  page = await browser.newPage()
  page.on('pageerror', (e) => pageErrors.push(e.message))
  page.on('console', (message) => {
    if (message.type() !== 'error') return
    const text = message.text()
    if (text === 'Failed to load resource: the server responded with a status of 503 (Service Unavailable)') expectedResourceErrors.push(text)
    else pageErrors.push(text)
  })
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (request.url().startsWith(origin + '/') || request.url().startsWith('data:')) request.continue()
    else { externalRequests.push(new URL(request.url()).origin); request.abort() }
  })
  await page.setViewport({ width: 1440, height: 1100 })
  page.setDefaultTimeout(10000)
  page.setDefaultNavigationTimeout(30000)
  async function ready() { await page.waitForSelector('#feishu-webhook'); await page.waitForFunction(() => !document.querySelector('[data-testid=feishu-save]')?.disabled) }
  async function fill(selector, value) { await page.$eval(selector, (el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })) }, value) }
  async function waitText(selector, text) { await page.waitForFunction((selector, text) => document.querySelector(selector)?.textContent.includes(text), {}, selector, text) }
  async function noLeak() {
    const visible = await page.$eval('body', (el) => el.textContent)
    for (const secret of [FAKE_WEBHOOK, FAKE_SECRET, ERROR_SECRET, TOKEN]) assert(!visible.includes(secret), 'Private value rendered in page text')
    assert.equal(await page.evaluate(() => localStorage.length), 0)
    assert.equal(await page.evaluate(() => sessionStorage.length), 0)
    assert(!page.url().includes('synthetic'))
  }
  async function shot(name) {
    await noLeak()
    const el = await page.$('[data-testid=feishu-notifications]')
    await el.screenshot({ path: path.join(output, name + '.png') })
  }
  const count = (action) => requests.filter((r) => r.action === action).length
  await page.goto(origin + '/settings', { waitUntil: 'domcontentloaded' })
  await ready()
  assert.equal(requests.length, 0, 'Reading the form must not save or send')
  checks.push('read-only mount and local access')

  await page.click('[data-testid=feishu-enabled]')
  await fill('#feishu-webhook', FAKE_WEBHOOK)
  await fill('#feishu-signing-secret', FAKE_SECRET)
  assert(await page.$eval('[data-testid=feishu-test]', (el) => el.disabled))
  await page.click('[data-testid=feishu-save]')
  await waitText('[data-testid=feishu-notice]', '通知配置已保存')
  assert.equal(webhook, FAKE_WEBHOOK); assert.equal(signingSecret, FAKE_SECRET)
  assert.equal(count('test'), 0)
  assert.equal(await page.$eval('#feishu-webhook', (el) => el.value), '')
  assert.equal(await page.$eval('#feishu-signing-secret', (el) => el.value), '')
  await shot('01-saved-not-delivered')
  checks.push('explicit save, masked state and no automatic test')

  await page.click('[data-testid=feishu-test]')
  await page.waitForSelector('[data-testid=feishu-test-confirmation]')
  assert.equal(count('test'), 0)
  await shot('02-test-destination-confirmation')
  mode = 'hold'
  await page.click('[data-testid=feishu-test-confirm]')
  await page.waitForFunction(() => document.querySelector('[data-testid=feishu-test]')?.disabled)
  await page.waitForFunction(() => !document.querySelector('[data-testid=feishu-test-confirmation]'))
  assert.equal(count('test'), 1)
  await page.$eval('[data-testid=feishu-test]', (el) => el.click())
  assert.equal(count('test'), 1, 'Disabled test must not resubmit')
  reply(pendingTest, 200, { ok: true, code: 'delivered', delivered: true, settings: publicState() }); pendingTest = null
  await waitText('[data-testid=feishu-test-notice]', '飞书已接受测试通知')
  await shot('03-test-accepted')
  checks.push('two-step test, saved destination only and duplicate suppression')

  await fill('#feishu-signing-secret', 'another-synthetic-draft')
  await page.click('[data-testid=feishu-clear-signing]')
  assert.equal(await page.$eval('#feishu-signing-secret', (el) => el.value), '')
  await page.click('[data-testid=feishu-save]')
  await waitText('[data-testid=feishu-notice]', '通知配置已保存')
  assert.equal(webhook, FAKE_WEBHOOK); assert.equal(signingSecret, '')
  assert.equal(requests.at(-1).clear_signing_secret, true)
  assert(!Object.hasOwn(requests.at(-1), 'signing_secret'))
  assert(!Object.hasOwn(requests.at(-1), 'webhook_url'))
  checks.push('explicit signing-key removal and omitted secrets retained')

  mode = 'rejected'
  await page.click('[data-testid=feishu-test]')
  await page.click('[data-testid=feishu-test-confirm]')
  await waitText('[data-testid=feishu-test-notice]', '飞书未接受')
  await noLeak()
  mode = 'changed'
  await page.click('[data-testid=feishu-test]')
  await page.click('[data-testid=feishu-test-confirm]')
  await waitText('[data-testid=feishu-test-notice]', '配置已经变化')
  checks.push('safe fixed delivery-failure and changed-configuration messages')

  mode = 'storage'
  await fill('#feishu-webhook', 'https://open.feishu.cn/open-apis/bot/v2/hook/replacement-synthetic')
  await page.click('[data-testid=feishu-save]')
  await waitText('[data-testid=feishu-notice]', '配置保存未完成')
  assert.equal(await page.$('[data-testid=feishu-save]'), null, 'Unknown state must hide write controls')
  await noLeak()
  const operations = requests.length
  mode = 'ok'
  await page.click('[data-testid=feishu-refresh]')
  await ready()
  assert.equal(requests.length, operations, 'Read-back must not replay an uncertain write')
  checks.push('uncertain save invalidates form and read-back never repeats a mutation')

  await fill('#feishu-webhook', 'draft-that-must-be-cleared')
  const beforeClear = count('clear')
  await page.click('[data-testid=feishu-clear]')
  assert.equal(count('clear'), beforeClear)
  assert.equal(await page.$eval('[data-testid=feishu-clear]', (el) => el.textContent.trim()), '确认停用并清除')
  await page.click('[data-testid=feishu-clear]')
  await waitText('[data-testid=feishu-notice]', '通知已停用')
  assert.equal(count('clear'), beforeClear + 1)
  assert.equal(settings.enabled, false)
  assert(await page.$eval('[data-testid=feishu-test]', (el) => el.disabled))
  assert.equal(bitable, unchangedBitable)
  await shot('04-cleared-notifications')
  checks.push('stable two-step clear with a draft, no fallback and Bitable untouched')

  settings = { ...settings, source: 'legacy', enabled: true, transport: 'legacy_app', legacy_app_available: true }
  await page.reload({ waitUntil: 'domcontentloaded' })
  await waitText('[data-testid=feishu-legacy-source]', '沿用旧飞书通知配置')
  assert.equal(await page.$('#feishu-webhook'), null)
  await page.click('[data-testid=feishu-save]')
  await waitText('[data-testid=feishu-notice]', '通知配置已保存')
  assert.equal(requests.at(-1).transport, 'legacy_app')
  assert.deepEqual(Object.keys(requests.at(-1)).sort(), ['action', 'enabled', 'transport'])
  assert.equal(bitable, unchangedBitable)
  checks.push('legacy-app compatibility without exposing or editing application credentials')

  await page.setViewport({ width: 390, height: 844 })
  await shot('05-mobile-layout')
  const width = await page.$eval('[data-testid=feishu-notifications]', (el) => ({ client: el.clientWidth, scroll: el.scrollWidth }))
  assert(width.scroll <= width.client + 2, 'Notification card must not overflow at mobile width')
  await noLeak()
  assert.equal(externalRequests.length, 0, 'No external network calls')
  assert.equal(expectedResourceErrors.length, 1, 'Exactly the injected storage-failure 503 must be observed')
  assert.deepEqual(pageErrors, [])
  writeFileSync(path.join(output, 'result.json'), JSON.stringify({ ok: true, checks, actions: requests.map((r) => ({ action: r.action,
    fields: Object.keys(r).sort() })), external_requests: externalRequests, page_errors: pageErrors,
    expected_storage_failure_responses: expectedResourceErrors.length }, null, 2) + '\n')
  console.log(JSON.stringify({ ok: true, checks: checks.length, post_requests: requests.length, external_requests: externalRequests.length }))
} catch (error) {
  const visible = await page?.$eval('body', (el) => el.innerText).catch(() => '')
  await page?.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
  writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ error: String(error), checks, trace, pageErrors, externalRequests, visible }, null, 2) + '\n')
  throw error
} finally {
  if (pendingTest) pendingTest.destroy()
  await browser?.close()
  for (const stream of streams) stream.destroy()
  await new Promise((resolve) => server.close(resolve))
  rmSync(profile, { recursive: true, force: true })
}
