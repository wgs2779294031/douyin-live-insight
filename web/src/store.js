import { reactive, readonly } from 'vue'

/**
 * 全局状态：一个 reactive 单例。只有 user / snapshot / conn 三块，
 * 不值得引 Pinia（多一层心智负担 + 一个依赖）。
 */
const state = reactive({
  accessMode: null,    // Only /api/access selects local or account mode.
  accessState: 'loading', // loading | ready | login | error | expired
  accessError: '',
  user: null,
  role: '',
  snapshot: null,      // 实时快照（/api/live 与 SSE 同结构）
  conn: 'idle',        // idle | open | error
  lastError: ''
})

let es = null
let lastAuthCheck = 0
let onAuthLost = null
let localWriteToken = '' // Memory only; never URL, storage or Vue state.
let accessGeneration = 0
let taskReadConfirmed = false

export function useBoard() {
  return readonly(state)
}

export function setAuthLostHandler(fn) {
  onAuthLost = fn
}

function requestError(message, status = 0, code = '') {
  const error = new Error(message)
  error.status = status
  error.code = code
  return error
}

function accessLost(status, generation = accessGeneration) {
  if (generation !== accessGeneration) return
  ++accessGeneration
  localWriteToken = ''
  taskReadConfirmed = false
  stopSSE()
  state.user = null
  state.role = ''
  state.snapshot = null
  state.accessState = state.accessMode === 'local' ? 'expired' : 'login'
  state.accessError = state.accessMode === 'local'
    ? '本地会话已失效，请重新连接。刚才的操作不会自动重试，请连接后核对状态。' : ''
  if (onAuthLost) onAuthLost(status)
}

function connectionLost(generation = accessGeneration) {
  if (generation !== accessGeneration) return
  ++accessGeneration
  localWriteToken = ''
  taskReadConfirmed = false
  stopSSE()
  state.accessState = 'error'
  state.accessError = '暂时无法连接看板，请确认本地应用仍在运行后重新连接。刚才的操作不会自动重试。'
}

export async function fetchMe() {
  const generation = accessGeneration
  const r = await fetch('/api/me', { cache: 'no-store', credentials: 'same-origin' })
  if (r.status === 401) return null
  if (!r.ok) throw requestError('无法确认看板访问状态（' + r.status + '）', r.status)
  const me = await r.json()
  if (typeof me.user !== 'string' || !me.user) throw requestError('看板访问状态无效')
  if (generation !== accessGeneration) return null
  state.user = me.user
  state.role = me.role || ''
  return me
}

/** Explicit initial connect/reconnect. Never repeat a prior business request. */
export async function initializeAccess() {
  const generation = ++accessGeneration
  localWriteToken = ''
  taskReadConfirmed = false
  lastAuthCheck = 0
  stopSSE()
  state.accessState = 'loading'
  state.accessError = ''
  state.user = null
  state.role = ''
  state.snapshot = null
  try {
    const response = await fetch('/api/access', { cache: 'no-store', credentials: 'same-origin' })
    if (!response.ok) throw requestError('无法读取看板访问模式（' + response.status + '）', response.status)
    const access = await response.json()
    if (!['local', 'account'].includes(access.mode)) throw requestError('看板未提供有效访问模式')
    if (generation !== accessGeneration) return false
    state.accessMode = access.mode
    if (access.mode === 'local') {
      const session = await fetch('/api/local/session', { method: 'POST', cache: 'no-store',
        credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      if (!session.ok) throw requestError('无法建立本地会话（' + session.status + '）', session.status)
      const me = await session.json()
      if (generation !== accessGeneration) return false
      if (me.mode !== 'local' || typeof me.user !== 'string' || !me.user
        || typeof me.write_token !== 'string' || me.write_token.length < 32 || me.write_token.length > 256) {
        throw requestError('本地会话回执无效')
      }
      localWriteToken = me.write_token
      state.user = me.user
      state.role = me.role || 'local'
      state.accessState = 'ready'
    } else {
      const me = await fetchMe()
      if (generation !== accessGeneration) return false
      state.accessState = me ? 'ready' : 'login'
    }
    return state.accessState === 'ready'
  } catch (_) {
    if (generation === accessGeneration) connectionLost(generation)
    return false
  }
}

export async function login(username, password) {
  if (state.accessMode !== 'account') throw requestError('当前访问模式不使用账号登录')
  const r = await fetch('/api/login', {
    method: 'POST',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password })
  })
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.error || '登录失败（' + r.status + '）')
  }
  const me = await r.json()
  state.user = me.user
  state.role = me.role || ''
  state.accessState = 'ready'
  return me
}

export async function logout() {
  if (state.accessMode !== 'account') return
  try {
    await fetch('/api/logout', { method: 'POST' })
  } catch (e) {
    /* 忽略：清本地态即可 */
  }
  stopSSE()
  state.user = null
  state.role = ''
  state.snapshot = null
  state.accessState = 'login'
}

export function stopSSE() {
  if (es) {
    es.close()
    es = null
  }
  state.conn = 'idle'
}

/** 订阅实时快照。断线自动重连；cookie 过期（401）则触发登录失效回调。 */
export function startSSE() {
  stopSSE()
  if (state.accessState !== 'ready') return
  const generation = accessGeneration
  es = new EventSource('/api/sse')

  es.addEventListener('open', () => {
    if (generation !== accessGeneration) return
    state.conn = 'open'
  })

  es.addEventListener('snapshot', (e) => {
    if (generation !== accessGeneration) return
    try {
      state.snapshot = JSON.parse(e.data)
      state.conn = 'open'
    } catch (err) {
      state.lastError = '快照解析失败：' + err
    }
  })

  es.addEventListener('error', async () => {
    if (generation !== accessGeneration) return
    state.conn = 'error'
    // EventSource 在 401 下会无限重连，主动查一次身份，过期就回登录页
    const now = Date.now()
    if (now - lastAuthCheck < 5000) return
    lastAuthCheck = now
    try {
      const me = await fetchMe()
      if (!me) accessLost(401, generation)
    } catch (error) {
      if ([401, 403].includes(error.status) && state.accessMode === 'local') accessLost(error.status, generation)
      else connectionLost(generation)
    }
  })
}

// ---------------- 通用请求 ----------------

async function jsonFetch(url, options) {
  const generation = accessGeneration
  const method = (options?.method || 'GET').toUpperCase()
  const headers = new Headers(options?.headers || {})
  if (state.accessMode === 'local') {
    if (state.accessState !== 'ready' || !localWriteToken) throw requestError('本地会话尚未就绪，请重新连接', 401)
    if (!['GET', 'HEAD'].includes(method) || url.split('?', 1)[0] === '/api/review/backfill') {
      headers.set('X-Local-Write-Token', localWriteToken)
    }
  }
  let r
  try {
    r = await fetch(url, { cache: 'no-store', credentials: 'same-origin', ...(options || {}), headers })
  } catch (error) {
    if (error.name !== 'AbortError') connectionLost(generation)
    throw error
  }
  if (r.status === 401 || (r.status === 403 && state.accessMode === 'local')) {
    accessLost(r.status, generation)
    throw requestError(state.accessMode === 'local' ? '本地会话已失效；此操作不会自动重试，请重新连接后核对状态' : '登录已过期', r.status)
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    const modelSettings = ['/api/llm/settings', '/api/llm/key/delete', '/api/llm/test'].includes(url)
    const sensitiveSettings = modelSettings || url === '/api/notifications/feishu'
    const error = new Error(sensitiveSettings ? (modelSettings ? '模型配置请求失败（' : '通知配置请求失败（') + r.status + '）'
      : (body.error || '请求失败（' + r.status + '）'))
    error.status = r.status
    // Configuration forms need partial-save state, never an echoed request or key.
    error.result = {
      ok: body.ok === true,
      code: typeof body.code === 'string' ? body.code : '',
      config_saved: body.config_saved === true,
      sync: body.sync && typeof body.sync === 'object' ? {
        total: Number.isSafeInteger(body.sync.total) ? body.sync.total : 0,
        synced: Number.isSafeInteger(body.sync.synced) ? body.sync.synced : 0,
        failed: Number.isSafeInteger(body.sync.failed) ? body.sync.failed : 0
      } : null
    }
    throw error
  }
  return r.json()
}

async function reviewTaskFetch(url, options) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 12000)
  try {
    return await jsonFetch(url, { ...(options || {}), signal: controller.signal })
  } finally {
    clearTimeout(timer)
  }
}

async function browserLoginFetch(options) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), options?.method === 'POST' ? 20000 : 6000)
  try {
    return await jsonFetch('/api/browser-login', { ...(options || {}), signal: controller.signal })
  } finally {
    clearTimeout(timer)
  }
}

async function asrFetch(options = {}) {
  const controller = new AbortController()
  const external = options.signal
  const abort = () => controller.abort()
  if (external?.aborted) controller.abort()
  else external?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(abort, 6000)
  try {
    return await jsonFetch('/api/asr', { ...options, signal: controller.signal })
  } finally {
    clearTimeout(timer)
    external?.removeEventListener('abort', abort)
  }
}

// Materials use the existing session/token flow; a lost write is never replayed.
async function materialsFetch(url, options = {}) {
  if (state.accessMode !== 'local') throw requestError('本地材料仅在客户本地应用中可用', 0, 'materials_local_only')
  const generation = accessGeneration
  const controller = new AbortController()
  const external = options.signal
  let timedOut = false
  const abort = () => controller.abort()
  if (external?.aborted) abort()
  else external?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(() => { timedOut = true; abort() }, options.method === 'POST' ? 45000 : 6000)
  try {
    const result = await jsonFetch(url, { ...options, signal: controller.signal })
    if (generation !== accessGeneration) throw requestError('材料请求所属连接已失效', 0, 'materials_stale')
    return result
  } catch (error) {
    const code = timedOut ? 'materials_timeout' : controller.signal.aborted ? 'materials_aborted'
      : error.code || error.result?.code
    throw requestError('材料请求未确认，请只读核对状态', error.status || 0,
      typeof code === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(code) ? code : 'materials_unavailable')
  } finally {
    clearTimeout(timer)
    external?.removeEventListener('abort', abort)
  }
}

function materialsParams(filters, names) {
  const query = new URLSearchParams()
  for (const name of names) {
    const value = filters[name]
    if (value === undefined || value === null || value === '') continue
    for (const item of Array.isArray(value) ? value : [value]) query.append(name, String(item))
  }
  return query.toString()
}

export const materialsAPI = {
  status: (lookup = {}, options = {}) => materialsFetch('/api/materials/status?'
    + materialsParams(lookup, ['request_id', 'service_id']), { signal: options.signal }),
  groups: (filters = {}, options = {}) => materialsFetch('/api/materials/groups?'
    + materialsParams(filters, ['view', 'room', 'date', 'platform', 'cursor', 'limit']), { signal: options.signal }),
  query: (filters = {}, options = {}) => materialsFetch('/api/materials/query?'
    + materialsParams(filters, ['view', 'room', 'date', 'platform', 'stream', 'time_from', 'time_until', 'cursor', 'limit']),
  { signal: options.signal }),
  refresh: (requestId, options = {}) => materialsFetch('/api/materials/refresh', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: requestId }), signal: options.signal
  })
}

// Presentation only: totals and currency amounts are computed by the read API.
export function usageDisplay(summary) {
  const count = (value) => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '未知'
  if (!summary?.scope || !summary?.totals || !summary?.coverage) return { available: false }
  const totals = summary.totals, coverage = summary.coverage, scope = summary.scope
  const labels = { complete: '已记录指标内的 usage 完整', partial: 'usage 仅部分已知',
    unknown: 'usage 未知', no_records: '没有用量记录' }
  const gaps = [['unknown_usage_records', 'usage 未知记录'], ['legacy_records', '旧口径记录'],
    ['invalid_records', '无效记录'], ['unattributed_records', '未能归属的记录'], ['uncovered_dispatches', '缺少完整指标的发送标记']]
    .filter(([field]) => Number.isSafeInteger(coverage[field]) && coverage[field] > 0)
    .map(([field, label]) => `${label} ${count(coverage[field])}`)
  if (coverage.pending_task) gaps.push('任务仍有待确认的发送阶段，任务状态与费用覆盖分别判断')
  const estimates = (Array.isArray(summary.estimates) ? summary.estimates : []).map((row) => ({
    currency: /^[A-Z]{3,8}$/.test(row.currency) ? row.currency : '币种未知',
    amount: typeof row.amount === 'string' && /^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(row.amount) ? row.amount : '未知',
    partial: row.partial !== false,
    versions: Array.isArray(row.price_versions) ? row.price_versions.filter((v) => typeof v === 'string') : [],
    snapshots: (Array.isArray(row.price_snapshots) ? row.price_snapshots : []).map((snapshot) => ({
      version: snapshot.version, source: snapshot.source === 'user_entered' ? '用户填写' : '来源未记录',
      saved_at: snapshot.saved_at || '时点未记录'
    })),
    priced: count(row.priced_components), unpriced: count(row.unknown_price_components)
  }))
  return { available: true, scope, coverage: labels[coverage.status] || '用量覆盖状态待确认', gaps,
    callDate: scope.call_date || '全部已记录调用日',
    records: count(totals.metric_records), attempts: count(totals.sdk_attempts),
    input: coverage.status === 'no_records' ? '未记录' : count(totals.known_prompt_tokens),
    output: coverage.status === 'no_records' ? '未记录' : count(totals.known_completion_tokens),
    notSent: count(totals.not_sent_records), failed: count(totals.failed_records),
    knownRecords: count(coverage.known_usage_records), estimates,
    probeRecords: count(summary.diagnostics?.probe_records), testRecords: count(summary.diagnostics?.test_records),
    diagnosticsExcluded: summary.diagnostics?.excluded === true,
    days: (Array.isArray(summary.by_call_date) ? summary.by_call_date : []).map((row) => ({
      day: row.call_date, attempts: count(row.sdk_attempts), input: count(row.known_prompt_tokens), output: count(row.known_completion_tokens)
    })),
    providers: (Array.isArray(summary.by_provider) ? summary.by_provider : []).map((row) => ({
      provider: row.provider, model: row.model, records: count(row.metric_records), attempts: count(row.sdk_attempts),
      input: count(row.known_prompt_tokens), output: count(row.known_completion_tokens)
    })) }
}

export const api = {
  feishuNotifications: () => jsonFetch('/api/notifications/feishu'),
  feishuNotificationAction: async (body) => {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 25000)
    try {
      return await jsonFetch('/api/notifications/feishu', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: controller.signal
      })
    } finally { clearTimeout(timer) }
  },
  asrStatus: (options = {}) => asrFetch(options),
  asrRetry: (options = {}) => asrFetch({ ...options, method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'retry' }) }),
  browserLoginStatus: () => browserLoginFetch(),
  browserLoginAction: (action, tag) => browserLoginFetch({
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, ...(action === 'check' ? { tag } : {}) })
  }),
  llmUsage: (filters = {}) => {
    const query = new URLSearchParams()
    for (const name of ['call_date', 'tag', 'task_id', 'data_date', 'purpose']) {
      if (filters[name] !== undefined && filters[name] !== null && filters[name] !== '') query.set(name, String(filters[name]))
    }
    return reviewTaskFetch('/api/llm/usage?' + query)
  },
  llmPrices: () => jsonFetch('/api/llm/prices'),
  llmSavePrices: (body) => jsonFetch('/api/llm/prices', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  }),
  reviewDates: () => jsonFetch('/api/review/dates'),
  reviewTags: (date) => jsonFetch('/api/review/tags?date=' + encodeURIComponent(date)),
  reviewBackfill: (tag, date, force, requestId) => reviewTaskFetch('/api/review/backfill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag, date, force: !!force, request_id: requestId })
  }),
  reviewBackfillStatus: (tag, date, { taskId = '', requestId = '', userInitiated = false } = {}) => {
    if (state.accessMode === 'local' && !taskReadConfirmed) {
      if (!userInitiated || state.accessState !== 'ready') {
        return Promise.reject(requestError('请主动查询任务状态；重新连接不会自动查询或重试任务', 0, 'task_confirmation_required'))
      }
      taskReadConfirmed = true
    }
    return reviewTaskFetch('/api/review/backfill?tag=' + encodeURIComponent(tag)
      + '&date=' + encodeURIComponent(date)
      + (taskId ? '&task_id=' + encodeURIComponent(taskId) : '')
      + (requestId ? '&request_id=' + encodeURIComponent(requestId) : ''))
  },
  review: (date, tag) =>
    jsonFetch('/api/review?date=' + encodeURIComponent(date) + '&tag=' + encodeURIComponent(tag)),
  llm: () => jsonFetch('/api/llm'),
  llmSettings: (body) => jsonFetch('/api/llm/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }),
  llmDeleteKey: (provider) => jsonFetch('/api/llm/key/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider })
  }),
  llmTest: async (body) => {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 12000)
    try {
      return await jsonFetch('/api/llm/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: controller.signal
      })
    } finally {
      clearTimeout(timer)
    }
  },
  llmGate: (enabled) => jsonFetch('/api/llm/gate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: !!enabled })
  }),
  llmProvider: (provider) => jsonFetch('/api/llm/provider', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider })
  }),
  llmBudget: (capTokens) => jsonFetch('/api/llm/budget', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cap_tokens: capTokens })
  }),
  passwd: (oldPwd, newPwd) =>
    jsonFetch('/api/passwd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old: oldPwd, new: newPwd })
    }),

  // ---- 主播管理（P6）----
  anchors: () => jsonFetch('/api/anchors'),
  anchorsHealth: () => jsonFetch('/api/anchors/health'),
  anchorStart: (tag, kind = 'all') =>
    jsonFetch('/api/anchors/start?tag=' + encodeURIComponent(tag) + '&kind=' + kind,
      { method: 'POST' }),
  anchorStop: (tag, kind = 'all', persist = false) =>
    jsonFetch('/api/anchors/stop?tag=' + encodeURIComponent(tag) + '&kind=' + kind +
      '&persist=' + (persist ? 1 : 0), { method: 'POST' }),
  anchorRestart: (tag) =>
    jsonFetch('/api/anchors/restart?tag=' + encodeURIComponent(tag), { method: 'POST' }),
  anchorAdd: (body) =>
    jsonFetch('/api/anchors', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }),
  anchorUpdate: (tag, body) =>
    jsonFetch('/api/anchors?tag=' + encodeURIComponent(tag), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }),
  anchorDelete: (tag) =>
    jsonFetch('/api/anchors?tag=' + encodeURIComponent(tag) + '&confirm=' + encodeURIComponent(tag),
      { method: 'DELETE' })
}
