// Local-material presentation and request isolation. No Vue, network or DOM side effects on import.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const VERSION = /^v_[0-9a-f]{32}$/
const TERMINAL = new Set(['published', 'failed', 'unconfirmed', 'busy'])
const STREAMS = new Set(['watch', 'room_meta', 'stats', 'danmu', 'events', 'speech'])
const RECOVERY_KEY = 'local-materials-request:v1'
const clone = (value) => JSON.parse(JSON.stringify(value))
const token = (value) => typeof value === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(value) ? value : ''
const version = (value) => typeof value === 'string' && VERSION.test(value)
const time = (value) => value === null || typeof value === 'number' && Number.isFinite(value) && value >= 0

export function materialsError(error) {
  if ([401, 403].includes(error?.status)) return '会话已失效。重新连接后只读核对；更新不会自动重发。'
  if (error?.status === 409) return '材料版本或文件已变化，请从头读取；不会自动拼接旧页。'
  const labels = {
    materials_timeout: '等待已到期，结果待确认；请只读核对，不要重复提交。',
    materials_not_ready: '尚未建立本地资料，可明确点击“更新资料”。',
    materials_corrupt: '资料完整性未确认，请保留现有材料并核对。',
    materials_unavailable: '暂时无法读取资料，请重新读取状态。',
    invalid_filter: '筛选条件无效，请检查日期和时间范围。',
    invalid_limit: '每页条数无效。', invalid_cursor: '续页标识无效，请从头读取。',
    invalid_parameters: '材料请求参数无效。'
  }
  return labels[error?.code] || '材料请求未确认，请重新读取状态。'
}

export function materialsReason(code) {
  const labels = {
    discovery_capacity: '输入计费或发现范围达到整理上限，已有发布版按原机制保留。',
    material_capacity: '资料容量达到整理上限，已有发布版按原机制保留。',
    material_gc_pending: '新版已提交，旧派生资料回收待处理。',
    material_diagnostic_unavailable: '新版已提交，整理诊断暂时无法确认。',
    material_gc_blocked: '派生资料回收受阻，本次未继续整理。',
    material_publish_unconfirmed: '发布结果无法确认，请只读核对当前可读版本。',
    materials_stopping: '应用正在停止，本次未接纳更新。',
    materials_busy: '另一次更新正在进行，本次未接纳、未排队。',
    material_busy: '本次未执行整理；外部占用工作的状态未知。',
    invalid_parameters: '请求参数未被接受，本次未执行整理。',
    invalid_refresh_request: '更新请求参数未被接受，本次未执行整理。'
  }
  return labels[code] || (code ? '整理回执有未确认事项，请保留现有资料并核对。' : '')
}

function beijingTime(value) {
  if (!value) return null
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) throw new Error('filter')
  const exact = value.length === 16 ? value + ':00' : value
  const stamp = Date.parse(exact + '+08:00') / 1000
  if (!Number.isFinite(stamp) || new Date((stamp + 28800) * 1000).toISOString().slice(0, 19) !== exact) throw new Error('filter')
  return stamp
}

export function materialsFilters(raw = {}) {
  const view = raw.view || 'canonical', room = raw.room || '', day = raw.date || ''
  const platform = raw.platform || '', stream = raw.stream || ''
  if (!['canonical', 'shared'].includes(view) || typeof room !== 'string' || room.length > 128 || /[\x00-\x1f\x7f]/.test(room)
    || platform && !['douyin', 'bilibili'].includes(platform) || stream && !STREAMS.has(stream)) throw new Error('filter')
  if (day && (!/^\d{4}-\d{2}-\d{2}$/.test(day) || new Date(day + 'T00:00:00Z').toISOString().slice(0, 10) !== day)) throw new Error('filter')
  const from = beijingTime(raw.from), until = beijingTime(raw.until)
  if (from !== null && until !== null && from >= until) throw new Error('filter')
  return { view, ...(room ? { room } : {}), ...(day ? { date: day } : {}), ...(platform ? { platform } : {}),
    ...(stream ? { stream } : {}), ...(from !== null ? { time_from: from } : {}), ...(until !== null ? { time_until: until } : {}), limit: 50 }
}

function receipt(value) {
  if (!value || typeof value !== 'object' || !UUID.test(value.service_id) || !UUID.test(value.request_id)
    || !['running', ...TERMINAL].includes(value.state) || value.version !== null && !version(value.version)
    || value.previous !== null && !version(value.previous) || value.state === 'unconfirmed' && value.version !== null
    || !time(value.started_at) || !time(value.finished_at)) throw new Error('receipt')
  return { service_id: value.service_id, request_id: value.request_id, state: value.state, version: value.version,
    previous: value.previous, code: token(value.code), warning: token(value.warning), started_at: value.started_at, finished_at: value.finished_at }
}

function status(value) {
  if (value?.ok !== true || !UUID.test(value.service_id) || value.active_id !== null && !UUID.test(value.active_id)
    || !['latest', 'found', 'unknown', 'service_changed'].includes(value.lookup) || typeof value.stopping !== 'boolean'
    || !['ready', 'not_ready', 'unavailable', 'corrupt'].includes(value.material?.state)
    || value.material.state === 'ready' && !version(value.material.version)) throw new Error('status')
  const material = value.material
  return { serviceId: value.service_id, activeId: value.active_id, stopping: value.stopping, lookup: value.lookup,
    operation: value.operation === null ? null : receipt(value.operation),
    material: { state: material.state, code: token(material.code), version: version(material.version) ? material.version : null,
      generated_at: time(material.generated_at) ? material.generated_at : null,
      verified_at: time(material.verified_at) ? material.verified_at : null,
      refresh: material.refresh && typeof material.refresh === 'object' ? {
        state: token(material.refresh.state), snapshot_stale: material.refresh.snapshot_stale !== false,
        error: token(material.refresh.error) } : null } }
}

function page(value) {
  if (!version(value?.version) || !Array.isArray(value.items) || value.items.length > 200
    || value.next_cursor !== null && (typeof value.next_cursor !== 'string' || !value.next_cursor || value.next_cursor.length > 2048)
    || !value.scan || typeof value.scan.limited !== 'boolean' || typeof value.scan.complete !== 'boolean') throw new Error('page')
  return clone(value)
}

export function createMaterialsState() {
  return { connected: false, statusFresh: false, statusReading: false, groupsReading: false, rowsReading: false,
    posting: false, serviceId: '', activeId: null, stopping: false, material: null, recovery: null,
    receipt: null, requestState: 'not_requested', statusError: '', groupsError: '', rowsError: '', notice: '',
    storageWarning: '', view: 'canonical', filters: materialsFilters(), filterError: '', groups: null, page: null, selection: null }
}

export function createMaterialsController({ api, state = createMaterialsState(), storage, uuid = () => crypto.randomUUID() }) {
  let disposed = false, epoch = 0, groupsEpoch = 0, rowsEpoch = 0, statusSequence = 0, postSequence = 0
  const pending = new Map()
  const abort = (kind) => { pending.get(kind)?.abort(); pending.delete(kind) }
  const begin = (kind) => { abort(kind); const controller = new AbortController(); pending.set(kind, controller); return controller }
  const live = (captured) => !disposed && state.connected && captured === epoch
  function saveRecovery(value) {
    state.recovery = value
    try { if (value) storage?.setItem(RECOVERY_KEY, JSON.stringify(value)); else storage?.removeItem(RECOVERY_KEY) }
    catch (_) { state.storageWarning = '无法保存本页找回标识；离开页面后可能无法找回本次回执。' }
  }
  try {
    const saved = JSON.parse(storage?.getItem(RECOVERY_KEY) || 'null')
    if (saved && UUID.test(saved.request_id) && UUID.test(saved.service_id)) {
      state.recovery = { request_id: saved.request_id, service_id: saved.service_id }; state.requestState = 'unknown'
    }
  } catch (_) { state.storageWarning = '无法读取本页找回标识，请只读核对资料状态。' }

  function clearRows() { ++rowsEpoch; abort('query'); state.rowsReading = false; state.page = null; state.rowsError = '' }
  function clearPages() { clearRows(); ++groupsEpoch; abort('groups'); state.groupsReading = false; state.groups = null; state.groupsError = ''; state.selection = null }
  function applyReceipt(value) {
    const own = state.recovery
    if (!own || value.service_id !== own.service_id || value.request_id !== own.request_id) return
    if (state.receipt && TERMINAL.has(state.receipt.state) && value.state === 'running') return
    state.receipt = value; state.requestState = value.state; state.notice = ''
  }
  function acceptStatus(raw) {
    const value = status(raw), previousVersion = state.material?.version
    if (state.serviceId && state.serviceId !== value.serviceId || previousVersion !== value.material.version) clearPages()
    state.serviceId = value.serviceId; state.activeId = value.activeId; state.stopping = value.stopping
    state.material = value.material; state.statusFresh = true; state.statusError = ''
    if (state.recovery) {
      if (state.recovery.service_id !== value.serviceId || ['unknown', 'service_changed'].includes(value.lookup)) {
        state.receipt = null; state.requestState = 'unknown'
      } else if (value.operation) applyReceipt(value.operation)
    }
  }
  async function readStatus(loadGroups = false) {
    if (!state.connected || disposed) return
    const captured = epoch, sequence = ++statusSequence, controller = begin('status')
    state.statusReading = true
    try {
      const value = await api.status(state.recovery || {}, { signal: controller.signal })
      if (!live(captured) || sequence !== statusSequence) return
      acceptStatus(value)
      if (loadGroups && state.material.state === 'ready') void readGroups(false)
    } catch (error) {
      if (!live(captured) || sequence !== statusSequence || controller.signal.aborted) return
      state.statusFresh = false; state.statusError = materialsError(error); clearPages()
    } finally { if (live(captured) && sequence === statusSequence) state.statusReading = false }
  }
  async function readResource(kind, next = false) {
    if (!state.connected || !state.statusFresh || state.material?.state !== 'ready' || disposed) return
    const isGroups = kind === 'groups', old = isGroups ? state.groups : state.page
    if (next && !old?.next_cursor || !isGroups && state.filterError) return
    const captured = epoch, resource = isGroups ? ++groupsEpoch : ++rowsEpoch, controller = begin(kind)
    const filters = isGroups ? { view: state.view, limit: 50 } : clone(state.filters)
    const expectedVersion = state.material.version
    if (next) filters.cursor = old.next_cursor
    if (isGroups) { state.groupsReading = true; state.groupsError = '' } else { state.rowsReading = true; state.rowsError = '' }
    const current = () => live(captured) && resource === (isGroups ? groupsEpoch : rowsEpoch)
    try {
      const value = page(await api[kind](filters, { signal: controller.signal }))
      if (!current()) return
      if (value.items.some((item) => !item || item.view !== filters.view)) throw new Error('page')
      if (value.version !== expectedVersion || value.version !== state.material?.version) {
        clearPages(); state.notice = '材料版本已变化，已清除旧页；请从头读取。'; void readStatus(false); return
      }
      if (isGroups) state.groups = value
      else state.page = { ...value, filters: clone(state.filters) }
    } catch (error) {
      if (!current() || controller.signal.aborted) return
      // A server-observed version/file change invalidates both resources.
      if (error.status === 409) clearPages()
      if (isGroups) { state.groups = null; state.groupsError = materialsError(error) }
      else { state.page = null; state.rowsError = materialsError(error) }
    } finally { if (current()) { if (isGroups) state.groupsReading = false; else state.rowsReading = false } }
  }
  const readGroups = (next = false) => readResource('groups', next)
  const readPage = (next = false) => readResource('query', next)
  function editFilters(raw) {
    const view = raw.view === 'shared' ? 'shared' : 'canonical'
    if (view !== state.view) { state.view = view; clearPages() } else clearRows()
    state.selection = null
    try { state.filters = materialsFilters(raw); state.filterError = '' }
    catch (_) { state.filterError = '筛选条件无效，请检查北京时间范围、日期或房间。' }
  }
  async function refresh() {
    if (!state.connected || !state.statusFresh || state.posting || state.activeId || state.stopping || disposed) return false
    const requestId = uuid()
    if (!UUID.test(requestId) || !UUID.test(state.serviceId)) { state.notice = '无法建立本次更新标识，请重新读取状态。'; return false }
    const captured = epoch, sequence = ++postSequence, controller = begin('post'), serviceId = state.serviceId
    ++statusSequence; abort('status'); state.statusReading = false
    saveRecovery({ request_id: requestId, service_id: serviceId })
    state.posting = true; state.receipt = null; state.requestState = 'pending'; state.notice = ''
    try {
      const value = status(await api.refresh(requestId, { signal: controller.signal }))
      if (!live(captured) || sequence !== postSequence) return false
      // A POST reply supplies only this request's receipt. Current material is read separately.
      if (value.serviceId === serviceId && state.serviceId === serviceId && value.operation) applyReceipt(value.operation)
      else state.requestState = 'unknown'
    } catch (error) {
      if (!live(captured) || sequence !== postSequence) return false
      if (error.status === 409 && error.code === 'materials_busy' || error.status === 503 && error.code === 'materials_stopping'
        || error.status === 400 && ['invalid_parameters', 'invalid_refresh_request'].includes(error.code)) {
        saveRecovery(null); state.requestState = 'not_accepted'; state.receipt = null; state.notice = materialsReason(error.code)
      } else { state.requestState = 'unknown'; state.receipt = null; state.notice = materialsError(error) }
    } finally {
      if (live(captured) && sequence === postSequence) { state.posting = false; state.statusFresh = false; void readStatus(true) }
    }
    return true
  }
  function disconnect() {
    state.connected = false; ++epoch; ++statusSequence; ++postSequence
    for (const kind of pending.keys()) abort(kind)
    state.statusFresh = false; state.statusReading = false; state.posting = false; clearPages()
    if (state.recovery && !TERMINAL.has(state.requestState)) state.requestState = 'unknown'
  }
  function connect() { if (disposed || state.connected) return; state.connected = true; void readStatus(true) }
  return { state, connect, disconnect, readStatus, readGroups, readPage, editFilters, refresh,
    selectGroup: (group) => { state.selection = clone(group) },
    needsPoll: () => state.connected && (state.posting || Boolean(state.activeId)),
    dispose: () => { disconnect(); disposed = true } }
}

export function materialsPageExport(current) {
  if (!current || !version(current.version) || !Array.isArray(current.items)
    || !['canonical', 'shared'].includes(current.filters?.view)) throw new Error('No returned page')
  const metadata = { stream: 'materials_page_info', schema: 1, version: current.version,
    view: current.filters.view, filters: clone(current.filters), scan: clone(current.scan),
    returned_items: current.items.length, has_more: Boolean(current.next_cursor), export_scope: 'returned_page_only',
    window_is_session: false, tag_is_speaker: false, speaker_separation: false,
    source_freshness: current.source_freshness, order: current.order, content_verification: current.content_verification }
  return { filename: 'materials-' + current.filters.view + '-' + current.version + '-page.jsonl',
    text: [metadata, ...current.items].map((row) => JSON.stringify(row)).join('\n') + '\n' }
}
