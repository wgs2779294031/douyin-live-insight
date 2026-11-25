// 看板前端自验：登录态截图 + 抓页面报错（2026-10-23）
//
// 为什么有这个东西：10-22 改完前端，只能让老大截图回来当测试员。当时手搓了 100 行
// CDP WebSocket，attach/flatten 两种都超时，40 分钟没成 —— 而 puppeteer 里
// page.setCookie() + page.screenshot() 就是两行。教训：先找现成的。
//
// 它不下载 Chromium：puppeteer-core 只驱动本机已装的 Edge / Chrome。
//
// 用法（在 web/ 目录下）：
//   node tools/shot.mjs /              # 登录页
//   node tools/shot.mjs /review /tmp/review.png
//
// 登录态二选一：
//   BOARD_COOKIE=<wb_token 的值>      # 本地验证最省事，token 由 scripts/board_auth.py 造
//   BOARD_USER=admin BOARD_PWD=xxx    # 走登录表单，会真的过一遍登录流程
// 其余环境变量：BASE（默认 http://127.0.0.1:8765）、WIDTH/HEIGHT、FULL（0 只要首屏）
//
// 退出码非 0 = 有问题（页面报错 / 白屏 / 没渲染出来），可以直接当回归门用。

import fs from 'node:fs'
import path from 'node:path'
import puppeteer from 'puppeteer-core'

const BASE = process.env.BASE || 'http://127.0.0.1:8765'

// Git Bash(MSYS) 会把以 / 开头的参数当路径改写：`node shot.mjs /` 传进来的
// 实际是 `C:/Program Files/Git/`，然后 Page.navigate 报 "invalid URL"。
// 禁用它用 MSYS2_ARG_CONV_EXCL='*'；这里再兜一层，别让人踩第二次。
function normalizeRoute(raw) {
  if (!raw) return '/'
  if (/^[A-Za-z]:[\\/]/.test(raw) || raw.includes('\\')) return '/'
  return raw.startsWith('/') ? raw : '/' + raw
}

const ROUTE = normalizeRoute(process.argv[2])
const OUT = process.argv[3] || path.join('tools', 'shots', (ROUTE.replace(/[^\w]+/g, '_') || 'root') + '.png')
const WIDTH = Number(process.env.WIDTH || 1440)
const HEIGHT = Number(process.env.HEIGHT || 900)
const FULL = process.env.FULL !== '0'

const BROWSERS = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe'
]

function findBrowser() {
  for (const p of BROWSERS) if (fs.existsSync(p)) return p
  throw new Error('本机没找到 Edge/Chrome，检查 BROWSERS 列表')
}

const t0 = Date.now()
const browser = await puppeteer.launch({
  executablePath: findBrowser(),
  headless: true,
  args: ['--disable-gpu', '--hide-scrollbars', '--window-size=' + WIDTH + ',' + HEIGHT]
})

const page = await browser.newPage()
await page.setViewport({ width: WIDTH, height: HEIGHT })

const errors = []
page.on('pageerror', (e) => errors.push('pageerror: ' + (e && e.message)))
page.on('requestfailed', (r) => errors.push('requestfailed: ' + r.url() + ' ' + (r.failure() || {}).errorText))
page.on('response', (r) => {
  if (r.status() >= 400) errors.push('http ' + r.status() + ' ' + r.url())
})
page.on('console', (m) => {
  if (m.type() === 'error') errors.push('console: ' + m.text())
})

const cookie = (process.env.BOARD_COOKIE || '').trim()
if (cookie) {
  // token 12 小时过期，这里不设 expires，随浏览器会话走
  await page.setCookie({
    name: 'wb_token',
    value: cookie,
    domain: '127.0.0.1',
    path: '/',
    httpOnly: true,
    sameSite: 'Lax'
  })
}

await page.goto(BASE + ROUTE, { waitUntil: 'domcontentloaded', timeout: 20000 })

// 走表单登录：只在明确给了账号密码时
const user = process.env.BOARD_USER
const pwd = process.env.BOARD_PWD
if (!cookie && user && pwd) {
  await page.waitForSelector('input[type=password]', { timeout: 5000 })
  const inputs = await page.$$('input')
  await inputs[0].type(user)
  await inputs[1].type(pwd)
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 15000 }).catch(() => {}),
    page.keyboard.press('Enter')
  ])
}

// 单页应用：数据是异步灌进来的，等「正在连接」消失（登录态下再等出卡片）才截。
// 上一版只等 900ms，截出来 cards=0、textLen=60 —— 而且退出码还是 0（假绿）。
const loggedIn = Boolean(cookie || (user && pwd))
const waited = await page.waitForFunction(
  (needCards) => {
    const txt = document.body.innerText || ''
    if (txt.includes('正在连接')) return false
    return needCards ? document.querySelectorAll('.card').length > 0 : true
  },
  { timeout: 15000 },
  loggedIn
).then(() => true).catch(() => false)
if (!waited) errors.push('content-timeout: 15s 内没等到内容' + (loggedIn ? '（含卡片）' : ''))
await new Promise((r) => setTimeout(r, 600))

const info = await page.evaluate(() => {
  const text = (document.body && document.body.innerText || '').trim()
  return {
    title: document.title,
    url: location.href,
    textLen: text.length,
    head: text.slice(0, 200),
    cards: document.querySelectorAll('.card').length,
    svg: document.querySelectorAll('svg').length
  }
})

fs.mkdirSync(path.dirname(OUT), { recursive: true })
await page.screenshot({ path: OUT, fullPage: FULL })
await browser.close()

const ms = Date.now() - t0
const blank = info.textLen < (loggedIn ? 40 : 20)
const loginPage = /请登录|登录后|密码/.test(info.head) && !loggedIn
// 未登录态下 /api/me、/api/anchors/health 返 401 是正常路径，不算缺陷
const realErrors = loggedIn ? errors : errors.filter((e) => !e.includes('401'))

console.log(JSON.stringify({
  ok: !blank && realErrors.length === 0,
  file: OUT,
  ms,
  blank,
  loginPage,
  ...info,
  errors: realErrors.slice(0, 12)
}, null, 2))

process.exit(blank || realErrors.length ? 1 : 0)
