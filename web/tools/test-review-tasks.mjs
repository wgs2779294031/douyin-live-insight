// Synthetic task API, independent headless profile; no model or production service.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

assert(process.argv[2] && path.isAbsolute(process.argv[2]), 'Provide an absolute evidence directory')
const output = path.resolve(process.argv[2]); mkdirSync(output, { recursive: true })
const profile = mkdtempSync(path.join(output, 'headless-profile-'))
const dist = fileURLToPath(new URL('../dist/', import.meta.url))
const executablePath = process.env.BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const tasks = new Map(), latest = new Map(), requestIds = new Map(), streams = new Set(), releases = new Set()
const taskReads = [], reviewReads = [], postPlans = [], checks = [], screenshots = [], errors = [], external = [], requests = []
const counts = { posts: 0, executions: 0, status_gets: 0, review_gets: 0 }
let dropNextPost = false, taskNumber = 0
const key = (tag, date) => tag + ':' + date
const empty = (tag, date) => ({ tag, data_date: date, state: tag === 'beta' && date === '2026-11-15' ? 'legacy_unverified' : 'not_requested' })
function task(tag, date, state = 'running') {
  const value = { task_id: 'fixture-task-' + ++taskNumber, tag, data_date: date, state, status: state,
    submitted_at: 1794700000, started_at: 1794700001, finished_at: null, reason: 'allowed',
    counts: { ok: 0, fail: 0, skipped: 0, windows: 4, generate_calls: 0 },
    input: { event_count: 12, start_ts: 1794700000, end_ts: 1794700300, captured_at: 1794700400 },
    output: { ref: 'portrait_' + tag + '_' + date + '.jsonl' } }
  tasks.set(value.task_id, value); latest.set(key(tag, date), value.task_id); return value
}
task('beta', '2026-11-14', 'succeeded')
const reply = (res, code, value) => { res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(value)) }
function plan(queue, options = {}) {
  let arrived, release
  const reached = new Promise((resolve) => { arrived = resolve }), gate = new Promise((resolve) => { release = resolve })
  queue.push({ ...options, arrived, gate }); if (options.hold) releases.add(release)
  return { reached, release: () => { releases.delete(release); release() } }
}
function review(tag, date) {
  return { tag, date, room_id: 'fixture-' + tag + '-' + date, empty: false, files: { danmu: { exists: true, mtime: 1794700000 } },
    stats: { online_series: [], peak_online: 1, total_pv: 1, points: 0 }, danmu: { permin: [], total: 12, danmaku_count: 12, latest: [] },
    portrait: { series: [{ ts: 1794700000, sentiment: {} }], latest: { portrait: { suggestions: ['preserved-output-' + tag + '-' + date] } } },
    coldline: { events: [], sessions: [] }, llm: { calls: 0, prompt_tokens: 0, completion_tokens: 0, cost_cny: 0 } }
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1'), tag = url.searchParams.get('tag'), date = url.searchParams.get('date')
  requests.push({ method: req.method, path: url.pathname, tag, date, task_id: url.searchParams.get('task_id'), request_id: url.searchParams.get('request_id') })
  try {
    if (url.pathname === '/api/access') return reply(res, 200, { mode: 'account' })
    if (url.pathname === '/api/me') return reply(res, 200, { user: 'synthetic-ui', role: 'admin' })
    if (url.pathname === '/api/sse') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' }); res.write('event: snapshot\ndata: {"tags":[],"date":"2026-11-15"}\n\n')
      streams.add(res); res.on('close', () => streams.delete(res)); return
    }
    if (url.pathname === '/api/review/dates') return reply(res, 200, { dates: ['2026-11-15', '2026-11-14'] })
    if (url.pathname === '/api/review/tags') return reply(res, 200, { tags: ['alpha', 'beta'], all_tags: ['alpha', 'beta'] })
    if (url.pathname === '/api/review') {
      counts.review_gets++; const entry = reviewReads.shift() || {}, value = entry.value || review(tag, date)
      entry.arrived?.(); if (entry.hold) await entry.gate
      return reply(res, entry.error ? 503 : 200, entry.error ? { error: 'Synthetic read failure' } : value)
    }
    if (url.pathname === '/api/review/backfill' && req.method === 'GET') {
      counts.status_gets++; const entry = taskReads.shift() || {}
      const id = url.searchParams.get('task_id') || requestIds.get(url.searchParams.get('request_id')) || latest.get(key(tag, date))
      const value = structuredClone(entry.value || tasks.get(id) || empty(tag, date))
      entry.arrived?.(); if (entry.hold) await entry.gate
      return reply(res, entry.error ? 503 : 200, entry.error ? { error: 'Synthetic status failure' } : value)
    }
    if (url.pathname === '/api/review/backfill' && req.method === 'POST') {
      counts.posts++; let raw = ''; for await (const chunk of req) { raw += chunk; assert(raw.length < 10000) }
      const body = JSON.parse(raw); assert.match(body.request_id, /^[\da-f-]{36}$/); assert.equal(typeof body.force, 'boolean')
      let found = tasks.get(requestIds.get(body.request_id)), created = false
      const prior = tasks.get(latest.get(key(body.tag, body.date)))
      if (!found) found = prior && (!body.force || ['starting', 'running'].includes(prior.state)) ? prior : null
      if (!found) { found = task(body.tag, body.date, 'starting'); created = true; counts.executions++ }
      requestIds.set(body.request_id, found.task_id)
      const entry = postPlans.shift() || {}
      entry.arrived?.(); if (entry.hold) await entry.gate
      if (dropNextPost) {
        dropNextPost = false
        res.writeHead(200, { 'Content-Type': 'application/json' }); res.flushHeaders(); res.write('{"task":')
        setTimeout(() => req.socket.destroy(), 30); return
      }
      return reply(res, 200, { ok: true, created, started: created, reused: !created, task_id: found.task_id, task: found })
    }
    if (url.pathname === '/review' || /^\/assets\/[\w.-]+\.(js|css)$/.test(url.pathname)) {
      const file = url.pathname.startsWith('/assets/') ? url.pathname.slice(1) : 'index.html'
      res.writeHead(200, { 'Content-Type': (file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html') + '; charset=utf-8' })
      return res.end(readFileSync(path.join(dist, file)))
    }
    return reply(res, 404, { error: 'Synthetic endpoint not registered' })
  } catch (_) { return reply(res, 500, { error: 'Synthetic fixture failed' }) }
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const origin = 'http://127.0.0.1:' + server.address().port
let browser, page
try {
  browser = await puppeteer.launch({ executablePath, headless: true, userDataDir: profile, args: ['--disable-gpu', '--no-proxy-server'] })
  page = await browser.newPage(); let confirmRedo = true
  page.on('dialog', (dialog) => confirmRedo ? dialog.accept() : dialog.dismiss())
  page.on('pageerror', (error) => errors.push(error.message))
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (request.url().startsWith(origin + '/') || request.url().startsWith('data:')) request.continue()
    else { external.push(new URL(request.url()).origin); request.abort() }
  })
  await page.evaluateOnNewDocument(() => {
    const set = window.setTimeout, clear = window.clearTimeout, pending = new Map()
    window.setTimeout = (callback, ms, ...args) => {
      if (ms !== 3000) return set(callback, ms, ...args)
      const id = set(() => {}, 600000); pending.set(id, () => callback(...args)); return id
    }
    window.clearTimeout = (id) => { pending.delete(id); clear(id) }
    window.__runPortraitPoll = () => {
      const first = pending.entries().next().value
      if (first) { pending.delete(first[0]); clear(first[0]); return first[1]() }
    }
  })
  page.setDefaultTimeout(10000); page.setDefaultNavigationTimeout(30000)
  await page.setViewport({ width: 1440, height: 1100 })
  const panel = '[data-testid=portrait-task-panel]', state = '[data-testid=portrait-task-state]', id = '[data-testid=portrait-task-id]'
  const generate = '[data-testid=portrait-generate]', redo = '[data-testid=portrait-redo]', recheck = '[data-testid=portrait-recheck]'
  const text = (selector) => page.$eval(selector, (el) => el.textContent.trim())
  const waitText = (selector, value) => page.waitForFunction((selector, value) => document.querySelector(selector)?.textContent.includes(value), {}, selector, value)
  const enabled = (selector) => page.waitForFunction((selector) => { const b = document.querySelector(selector); return b && !b.disabled }, {}, selector)
  const poll = () => page.evaluate(() => window.__runPortraitPoll())
  const requery = async () => { await enabled(recheck); await page.click(recheck) }
  async function shot(name) { await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true }); screenshots.push(name + '.png') }
  await page.goto(origin + '/review?tag=alpha&date=2026-11-15', { waitUntil: 'domcontentloaded' })
  await waitText(state, '尚未生成'); await enabled(generate)
  assert(!/¥0\.06|¥0\.7|40\s*次/.test(await text(panel)))
  dropNextPost = true
  await page.$eval(generate, (button) => { button.click(); button.click() })
  await waitText('[data-testid=portrait-task-notice]', '提交结果待确认')
  assert.equal(counts.posts, 1); assert.equal(counts.executions, 1)
  assert.equal(await page.$eval(generate, (button) => button.disabled), true)
  await page.$eval(generate, (button) => { button.disabled = false; button.click(); button.disabled = true })
  assert.equal(counts.posts, 1)
  await shot('01-unknown-submit-read-only-recovery')
  plan(taskReads, { value: empty('alpha', '2026-11-15') })
  await requery(); await waitText('[data-testid=portrait-task-notice]', '本次提交尚未找到')
  assert.equal(await page.$eval(generate, (button) => button.disabled), true); assert.equal(counts.posts, 1)
  await requery(); await waitText(state, '启动中')
  const firstId = await text(id)
  assert(requests.some((r) => r.request_id && r.method === 'GET'))
  await page.reload({ waitUntil: 'domcontentloaded' }); await waitText(id, firstId); await waitText(state, '启动中')
  assert.equal(counts.posts, 1)
  assert(requests.some((r) => r.task_id === firstId && r.method === 'GET'))
  checks.push('Unknown POST is sent once, retained UUID is discovered read-only, and refresh resumes its stable task ID without POST')

  plan(taskReads, { error: true }); await poll(); await waitText(state, '状态待确认')
  assert.equal(await page.$eval(redo, (button) => button.disabled), true)
  assert.equal(counts.posts, 1)
  await requery(); await waitText(state, '启动中')
  tasks.get(firstId).state = 'partial'; tasks.get(firstId).counts = { ok: 2, fail: 1, skipped: 1, windows: 4, generate_calls: 3 }
  await poll(); await waitText(state, '部分成功')
  await waitText('[data-testid=portrait-task-details]', '成功 2'); await waitText('body', 'preserved-output-alpha-2026-11-15')
  await enabled(generate); await page.click(generate); await waitText(state, '部分成功'); assert.equal(counts.posts, 1)
  await shot('02-partial-retains-output-and-counts')
  checks.push('Poll failure is unknown with read-only recovery; partial output and exact counts remain available, ordinary reads never resubmit')

  confirmRedo = false; await enabled(redo); await page.click(redo); assert.equal(counts.posts, 1)
  confirmRedo = true; await page.$eval(redo, (button) => { button.click(); button.click() })
  await waitText(state, '启动中'); const secondId = await text(id)
  assert.notEqual(secondId, firstId); assert.equal(counts.posts, 2); assert.equal(counts.executions, 2)
  await waitText('body', 'preserved-output-alpha-2026-11-15')
  tasks.get(secondId).state = 'blocked'; tasks.get(secondId).reason = 'global_paused'
  await poll(); await waitText(state, '已暂停或被阻断'); await waitText(panel, '内置AI总许可已暂停')
  await enabled(generate); await page.click(generate); await waitText(state, '已暂停或被阻断'); assert.equal(counts.posts, 2)
  await shot('03-blocked-append-task-preserves-old-output')
  checks.push('Only confirmed append-redo creates an independent run; rapid clicks stay single-flight and blocked tasks are reused read-only')

  const delayed = plan(taskReads, { hold: true })
  await requery(); await delayed.reached
  await page.select('[data-testid=review-tag]', 'beta'); await waitText(state, '历史画像，完整性未确认')
  const oldResponse = page.waitForResponse((r) => r.url().includes('/api/review/backfill') && r.status() === 200)
  delayed.release(); await oldResponse
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  assert((await text(panel)).includes('beta')); assert.equal(await page.$(id), null)
  await enabled(generate); await page.click(generate); await waitText(state, '历史画像，完整性未确认'); assert.equal(counts.posts, 2)
  await shot('04-legacy-read-only-and-tag-isolation')
  checks.push('Late task replies cannot cross tags; legacy nonempty portrait stays unverified and ordinary access is read-only')

  const delayedData = plan(reviewReads, { hold: true }), delayedTask = plan(taskReads, { hold: true })
  await page.click('[data-testid=review-refresh]'); await delayedData.reached; await delayedTask.reached
  await page.select('[data-testid=review-date]', '2026-11-14'); await waitText(state, '成功')
  await waitText('body', 'preserved-output-beta-2026-11-14')
  delayedData.release(); delayedTask.release()
  await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 100)))
  assert((await text(panel)).includes('2026-11-14')); assert(!(await text('body')).includes('preserved-output-beta-2026-11-15'))
  await page.reload({ waitUntil: 'domcontentloaded' }); await waitText(state, '成功'); await waitText('body', 'preserved-output-beta-2026-11-14')
  assert.equal(counts.posts, 2)
  checks.push('Date switches invalidate delayed output and task receipts; URL and stable identity restore the selected resource across refresh')

  const finalId = await text(id)
  for (const [value, label] of [['failed', '失败'], ['interrupted', '已中断'], ['unknown', '结果未知']]) {
    tasks.get(finalId).state = value
    await requery(); await waitText(state, label); await enabled(generate); await page.click(generate); await waitText(state, label)
    assert.equal(counts.posts, 2)
  }
  plan(taskReads, { value: { ...tasks.get(finalId), tag: 'wrong-resource' } })
  await requery(); await waitText(state, '状态待确认'); assert.equal(await page.$eval(redo, (b) => b.disabled), true)
  await requery(); await waitText(state, '结果未知')
  const reasonFixtures = [
    ['control_changed', '控制许可已变化'], ['credentials_changed', '模型配置或密钥已变化'],
    ['client_unavailable', '没有可用模型客户端'], ['daily_budget_exhausted', 'token 护栏'],
    ['budget_ledger_error', '用量账本无法确认'], ['task_state_unavailable', '任务记录无法确认'],
    ['startup_ack_timeout', '启动交接未及时确认'], ['startup_unknown', '启动结果无法确认'],
    ['owner_exited_unknown', '原执行进程已退出'], ['owner_identity_unknown', '原执行进程身份无法确认'],
    ['request_result_unknown', '模型请求结果未知'], ['call_limit', '画像生成次数上限'],
    ['no_data', '没有可用的落盘弹幕数据'], ['no_eligible_windows', '没有达到生成条件'],
    ['output_write_failed', '未能确认写入'], ['cleanup_failed', '资源清理未确认']
  ]
  for (const [code, expected] of reasonFixtures) {
    tasks.get(finalId).reason = code
    await requery(); await waitText(panel, expected)
    assert.equal(counts.posts, 2)
  }
  tasks.get(finalId).reason = 'request_result_unknown'
  await requery(); await waitText(panel, '模型请求结果未知')
  await shot('05-unknown-task-no-automatic-retry')
  checks.push('Failed/interrupted/unknown tasks do not resubmit; wrong-resource receipts stay unknown and fixed backend reasons display readable explanations')

  await page.select('[data-testid=review-tag]', 'alpha'); await waitText(state, '尚未生成'); await enabled(generate)
  const heldPost = plan(postPlans, { hold: true })
  await page.click(generate); await heldPost.reached
  const lateId = latest.get(key('alpha', '2026-11-14')); tasks.get(lateId).state = 'succeeded'
  await page.select('[data-testid=review-tag]', 'beta'); await waitText(state, '结果未知')
  await page.select('[data-testid=review-tag]', 'alpha'); await waitText(state, '成功'); await enabled(redo)
  await page.click(redo); await waitText(state, '启动中')
  const newerId = await text(id); assert.notEqual(newerId, lateId)
  const latePostResponse = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().includes('/api/review/backfill'))
  heldPost.release(); await latePostResponse
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  assert.equal(await text(id), newerId)
  await page.reload({ waitUntil: 'domcontentloaded' }); await waitText(id, newerId)
  assert.equal(counts.posts, 4); assert.equal(counts.executions, 4)
  checks.push('A late old POST cannot replace a newer append-run recovery identity in sessionStorage or after refresh')

  assert.deepEqual(errors, []); assert.deepEqual(external, [])
  const html = readFileSync(path.join(dist, 'index.html'), 'utf8'), assets = ['index.html', ...[...html.matchAll(/\/assets\/([^"']+)/g)].map((m) => 'assets/' + m[1])]
  writeFileSync(path.join(output, 'verification.json'), JSON.stringify({ passed: true, checks, counts, screenshots,
    no_page_errors: true, external_requests: 0, production_dist: Object.fromEntries(assets.map((name) => [name, createHash('sha256').update(readFileSync(path.join(dist, name))).digest('hex')])),
    note: 'All tasks, output, timestamps and accounts are fake. No real model, Python service or production data.' }, null, 2) + '\n')
  console.log(JSON.stringify({ passed: true, checks: checks.length, counts }))
} catch (error) {
  writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ name: error.name, message: error.message, counts, requests }, null, 2) + '\n')
  if (page) await page.screenshot({ path: path.join(output, 'failure-page.png'), fullPage: true }).catch(() => {})
  throw error
} finally {
  for (const release of releases) release()
  if (browser) await browser.close()
  for (const stream of streams) stream.end()
  await new Promise((resolve) => server.close(resolve))
  const resolved = path.resolve(profile)
  assert.equal(path.dirname(resolved), output); assert(path.basename(resolved).startsWith('headless-profile-'))
  rmSync(resolved, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
  writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify({ browser_closed: true, loopback_server_closed: true, temporary_profile_removed: !existsSync(resolved) }, null, 2) + '\n')
}
