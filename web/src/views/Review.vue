<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { api, usageDisplay, useBoard } from '../store'
import { fmtN, fmtTime, fmtMin, fmtDur, pct, levelText } from '../utils'
import KpiCard from '../components/KpiCard.vue'
import TrendChart from '../components/TrendChart.vue'
import DanmuFeed from '../components/DanmuFeed.vue'
import LocalMaterials from '../components/LocalMaterials.vue'

const board = useBoard()
const dates = ref([])
const tags = ref([])
const allTags = ref([])
const date = ref('')
const tag = ref('')
const data = ref(null)
const loading = ref(false)
const err = ref('')
const initialQuery = new URLSearchParams(window.location.search)
let preferredTag = initialQuery.get('tag') || ''
let tagSequence = 0
let reviewSequence = 0
let resourceEpoch = 0
let taskSequence = 0
let disposed = false
let bfTimer = null
const bfTask = ref(null)
const taskUsage = ref(null)
const taskUsageReading = ref(false)
const taskUsageNotice = ref('')
let taskUsageSequence = 0
const bfReading = ref(false)
const bfPosting = ref(false)
const bfFresh = ref(false)
const bfUncertain = ref(false)
const bfMsg = ref('')
const recovery = ref({})
const storageWarning = ref('')
const taskStates = new Set(['starting', 'running', 'succeeded', 'partial', 'failed', 'blocked',
  'interrupted', 'unknown', 'legacy_unverified', 'not_requested'])
const taskLabels = {
  starting: '启动中', running: '正在生成', succeeded: '成功', partial: '部分成功', failed: '失败',
  blocked: '已暂停或被阻断', interrupted: '已中断', unknown: '结果未知',
  legacy_unverified: '历史画像，完整性未确认', not_requested: '尚未生成'
}
const taskReasons = {
  global_paused: '内置AI总许可已暂停', control_unavailable: '内置AI许可状态待确认',
  purpose_paused: '人工画像用途已暂停', missing_key: '没有可用模型密钥', no_credentials: '没有可用模型密钥',
  no_key: '没有可用模型密钥', budget_exceeded: '预算护栏已阻止继续调用', budget_blocked: '预算护栏已阻止继续调用',
  control_epoch_changed: '控制许可已变化，本任务不会自动续跑', control_changed: '控制许可已变化，本任务不会自动续跑',
  credentials_changed: '模型配置或密钥已变化，本任务不会自动续跑', client_unavailable: '当前没有可用模型客户端，请检查配置和密钥',
  daily_budget_exhausted: '当日 token 护栏已阻止继续调用', budget_ledger_error: '用量账本无法确认，已阻止继续调用',
  task_state_unavailable: '任务记录无法确认，已停止继续执行', task_blocked: '任务执行许可已失效，未继续调用模型',
  startup_ack_timeout: '启动交接未及时确认，不会自动重试', startup_unknown: '启动结果无法确认，不会自动重试',
  owner_exited_unknown: '原执行进程已退出，任务结束结果未确认', owner_identity_unknown: '原执行进程身份无法确认，未追加执行',
  request_result_unknown: '模型请求结果未知，不会自动重试', response_unknown: '模型请求结果未知，不会自动重试',
  call_limit: '已达到本次画像生成次数上限，剩余窗口未继续处理', no_data: '没有可用的落盘弹幕数据',
  no_eligible_windows: '没有达到生成条件的弹幕窗口', output_write_failed: '模型结果未能确认写入，请保留现有输出并核对',
  cleanup_failed: '任务资源清理未确认，结束结果仍待核对', execution_failed: '任务执行异常，请核对任务状态与已有输出',
  invalid_scope: '任务主播或数据日期不匹配，已停止执行', invalid_parameters: '画像参数无效，未继续执行',
  route_changed: '模型路由已变化，未继续发送', completed: '本次任务已完成', completed_with_failures: '本次任务已结束，部分窗口失败',
  interrupted: '任务被中断，不会自动重跑'
}
const bfState = computed(() => bfTask.value?.state || 'not_requested')
const bfActive = computed(() => bfFresh.value && ['starting', 'running'].includes(bfState.value))
const bfLabel = computed(() => bfPosting.value ? '正在确认接单' : (!bfFresh.value
  ? '状态待确认' : taskLabels[bfState.value] || '结果未知'))
const bfDisabled = computed(() => !tag.value || !date.value || bfReading.value || bfPosting.value
  || bfUncertain.value || bfActive.value || !bfFresh.value)
const bfExisting = computed(() => !!bfTask.value?.task_id || bfState.value === 'legacy_unverified')
const bfRedoDisabled = computed(() => bfDisabled.value || bfTask.value?.can_redo === false)
const bfReason = computed(() => bfFresh.value ? (taskReasons[bfTask.value?.reason] || '') : '')
const context = () => ({ tag: tag.value, date: date.value, epoch: resourceEpoch })
const isCurrent = (ctx) => !disposed && ctx.epoch === resourceEpoch && ctx.tag === tag.value && ctx.date === date.value
const recoveryKey = (ctx) => 'review-portrait-task:v1:' + JSON.stringify([ctx.tag, ctx.date])
function readRecovery(ctx) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(recoveryKey(ctx)) || '{}')
    return Object.fromEntries(['task_id', 'request_id'].filter((key) => typeof saved[key] === 'string'
      && saved[key].length <= 128).map((key) => [key, saved[key]]))
  } catch (_) {
    storageWarning.value = '无法保存本页找回标识；可按主播与日期主动查询任务。'
    return {}
  }
}
function saveRecovery(ctx, saved, expectedRequest = '') {
  if (expectedRequest) {
    try {
      const latest = JSON.parse(sessionStorage.getItem(recoveryKey(ctx)) || '{}')
      if (latest.request_id !== expectedRequest) return false
    } catch (_) {
      if (!isCurrent(ctx) || recovery.value.request_id !== expectedRequest) return false
    }
  }
  try { sessionStorage.setItem(recoveryKey(ctx), JSON.stringify(saved)) } catch (_) {
    if (isCurrent(ctx)) storageWarning.value = '无法保存本页找回标识；可按主播与日期主动查询任务。'
  }
  if (isCurrent(ctx)) recovery.value = saved
  return true
}
function normalizeTask(response, ctx, expectedId = '') {
  const raw = response?.task || (typeof response?.status === 'object' ? response.status : response)
  const state = raw?.state || raw?.status
  if (!raw || raw.tag !== ctx.tag || raw.data_date !== ctx.date || !taskStates.has(state)
    || (expectedId && raw.task_id !== expectedId)
    || (!['legacy_unverified', 'not_requested'].includes(state) && (typeof raw.task_id !== 'string' || !raw.task_id))) {
    throw new Error('Unverified task receipt')
  }
  return { ...raw, state }
}
function clearPoll() { if (bfTimer) clearTimeout(bfTimer); bfTimer = null }
function schedulePoll(ctx, task) {
  clearPoll()
  if (isCurrent(ctx) && ['starting', 'running'].includes(task.state)) {
    const taskId = task.task_id
    bfTimer = setTimeout(() => lookupTask(ctx, { taskId }), 3000)
  }
}
function countValue(key) {
  const n = bfTask.value?.counts?.[key]
  return numberValue(n)
}
function numberValue(n) {
  return Number.isSafeInteger(n) && n >= 0 ? String(n) : '未记录'
}
function taskTime(value) {
  if (value === null || value === undefined || value === '') return '未记录'
  const parsed = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString('zh-CN', { hour12: false }) : '未记录'
}
async function lookupTask(ctx = context(), ids = null, userInitiated = false) {
  if (!ctx.tag || !ctx.date || !isCurrent(ctx)) return false
  clearPoll()
  const sequence = ++taskSequence
  bfFresh.value = false
  bfReading.value = true
  const saved = recovery.value
  const lookup = ids || { taskId: saved.task_id || '', requestId: saved.task_id ? '' : saved.request_id || '' }
  try {
    const response = await api.reviewBackfillStatus(ctx.tag, ctx.date, { ...lookup, userInitiated })
    if (!isCurrent(ctx) || sequence !== taskSequence) return false
    const task = normalizeTask(response, ctx, lookup.taskId)
    if (task.state === 'not_requested' && saved.request_id) {
      bfTask.value = null
      bfUncertain.value = true
      bfMsg.value = '本次提交尚未找到，接单结果仍待确认。请主动查询；不会自动重新提交。'
      return false
    }
    bfTask.value = task
    bfFresh.value = true
    bfUncertain.value = false
    bfMsg.value = ''
    if (task.task_id) saveRecovery(ctx, { ...saved, task_id: task.task_id })
    schedulePoll(ctx, task)
    if (['succeeded', 'partial'].includes(task.state)) loadReview(ctx)
    return true
  } catch (error) {
    if (isCurrent(ctx) && sequence === taskSequence) {
      bfUncertain.value = true
      bfMsg.value = error.code === 'task_confirmation_required'
        ? '请点击“查询任务状态”确认现有记录。重新连接后不会自动查询、重试或追加任务。'
        : '任务状态读取失败，当前状态待确认。请主动查询；这不会重新执行模型。'
    }
    return false
  } finally {
    if (isCurrent(ctx) && sequence === taskSequence) bfReading.value = false
  }
}
async function doBackfill(force = false) {
  if (bfDisabled.value || (force && bfRedoDisabled.value)) return
  if (!force && bfExisting.value) { await lookupTask(context(), null, true); return }
  const ctx = context()
  if (force && !window.confirm('为 ' + ctx.date + ' · ' + ctx.tag
    + ' 追加重做现有弹幕画像？这会创建独立任务并可能再次产生模型费用；旧画像会保留。'
    + '若原任务结果未知，追加重做可能重复消耗。')) return
  if (!window.crypto?.randomUUID) {
    bfMsg.value = '当前环境无法生成唯一提交标识，请刷新后再操作。'
    return
  }
  const requestId = window.crypto.randomUUID()
  saveRecovery(ctx, { request_id: requestId })
  const sequence = ++taskSequence
  clearPoll()
  bfTask.value = null
  bfFresh.value = false
  bfPosting.value = true
  bfUncertain.value = true
  bfMsg.value = '正在确认接单；刷新不会自动再次提交，请主动查询任务状态。'
  try {
    const response = await api.reviewBackfill(ctx.tag, ctx.date, force, requestId)
    const task = normalizeTask(response, ctx)
    if (!task.task_id) throw new Error('Missing task identity')
    saveRecovery(ctx, { request_id: requestId, task_id: task.task_id }, requestId)
    if (!isCurrent(ctx) || sequence !== taskSequence) return
    bfTask.value = task
    bfFresh.value = true
    bfUncertain.value = false
    bfMsg.value = response.reused ? '已读取已有任务，没有创建新的执行。' : '任务已登记；实际进度以任务状态为准。'
    schedulePoll(ctx, task)
    if (['succeeded', 'partial'].includes(task.state)) loadReview(ctx)
  } catch (_) {
    if (isCurrent(ctx) && sequence === taskSequence) {
      bfMsg.value = '提交结果待确认，可能已经接单。请查询本次提交；不会自动重试或追加任务。'
    }
  } finally {
    if (isCurrent(ctx) && sequence === taskSequence) bfPosting.value = false
  }
}
async function loadDates() {
  try {
    const result = await api.reviewDates()
    if (disposed) return
    dates.value = result.dates || []
    const preferredDate = initialQuery.get('date')
    date.value = dates.value.includes(preferredDate) ? preferredDate : dates.value[0] || ''
  } catch (_) { if (!disposed) err.value = '日期列表加载失败，请刷新重试。' }
}
async function loadTags(selectedDate, previousTag) {
  const sequence = ++tagSequence
  tags.value = []; allTags.value = []; tag.value = ''
  if (!selectedDate) return
  try {
    const result = await api.reviewTags(selectedDate)
    if (disposed || sequence !== tagSequence || date.value !== selectedDate) return
    tags.value = result.tags || []
    allTags.value = result.all_tags || tags.value
    const wanted = preferredTag || previousTag
    preferredTag = ''
    tag.value = [...tags.value, ...allTags.value].includes(wanted) ? wanted : tags.value[0] || allTags.value[0] || ''
  } catch (_) { if (sequence === tagSequence && !disposed) err.value = '主播列表加载失败，请刷新重试。' }
}
async function loadReview(ctx = context()) {
  if (!ctx.tag || !ctx.date || !isCurrent(ctx)) return
  const sequence = ++reviewSequence
  loading.value = true
  err.value = ''
  try {
    const result = await api.review(ctx.date, ctx.tag)
    if (!isCurrent(ctx) || sequence !== reviewSequence) return
    data.value = result
  } catch (_) {
    if (isCurrent(ctx) && sequence === reviewSequence) {
      err.value = '落盘数据读取失败，请只读刷新；不会生成画像。'
      data.value = null
    }
  } finally { if (isCurrent(ctx) && sequence === reviewSequence) loading.value = false }
}
function refreshCurrent() { loadReview(); lookupTask(context(), null, true) }
async function loadTaskUsage(taskId = bfTask.value?.task_id, ctx = context()) {
  if (!taskId || !isCurrent(ctx)) return
  const sequence = ++taskUsageSequence
  taskUsage.value = null
  taskUsageReading.value = true
  taskUsageNotice.value = ''
  try {
    const result = await api.llmUsage({ task_id: taskId, tag: ctx.tag, data_date: ctx.date, purpose: 'review' })
    if (!isCurrent(ctx) || sequence !== taskUsageSequence || bfTask.value?.task_id !== taskId) return
    if (result.scope?.kind !== 'task' || result.scope.task_id !== taskId
      || result.scope.room_tag !== ctx.tag || result.scope.data_date !== ctx.date) throw new Error('Wrong usage scope')
    taskUsage.value = result
  } catch (_) {
    if (isCurrent(ctx) && sequence === taskUsageSequence) taskUsageNotice.value = '任务用量读取失败，请只读重查；任务状态与费用覆盖分别判断。'
  } finally {
    if (isCurrent(ctx) && sequence === taskUsageSequence) taskUsageReading.value = false
  }
}
watch(bfTask, (task) => {
  ++taskUsageSequence
  taskUsage.value = null; taskUsageReading.value = false; taskUsageNotice.value = ''
  if (task?.task_id && bfFresh.value) loadTaskUsage(task.task_id)
})
watch(bfFresh, (fresh) => {
  if (!fresh) { ++taskUsageSequence; taskUsage.value = null; taskUsageReading.value = false }
})
const roomUsage = computed(() => {
  const value = data.value?.llm
  return usageDisplay(value?.scope?.kind === 'room' && value.scope.room_tag === tag.value
    && value.scope.data_date === date.value ? value : null)
})
const usageCards = computed(() => [{ key: 'room', title: '所选主播的已记录业务用量',
  hint: `主播 ${tag.value} · 材料日期 ${date.value} · 跨实际调用日汇总`, view: roomUsage.value },
  ...(bfTask.value?.task_id ? [{ key: 'task', title: '所选任务的已记录业务用量',
    hint: `任务 ${bfTask.value.task_id} · 材料日期 ${date.value}`, view: usageDisplay(taskUsage.value) }] : [])])
watch(date, (value, previous) => { loadTags(value, tag.value) }, { flush: 'sync' })
watch([date, tag], () => {
  ++resourceEpoch; ++reviewSequence; ++taskSequence
  clearPoll()
  data.value = null; loading.value = false; err.value = ''
  bfTask.value = null; bfFresh.value = false; bfReading.value = false; bfPosting.value = false
  bfUncertain.value = false; bfMsg.value = ''
  const ctx = context()
  recovery.value = ctx.tag && ctx.date ? readRecovery(ctx) : {}
  if (!ctx.tag || !ctx.date) return
  const url = new URL(window.location.href)
  url.searchParams.set('tag', ctx.tag); url.searchParams.set('date', ctx.date)
  window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash)
  loadReview(ctx); lookupTask(ctx)
}, { flush: 'sync' })
onMounted(loadDates)
onUnmounted(() => { disposed = true; ++resourceEpoch; ++tagSequence; ++reviewSequence; ++taskSequence; clearPoll() })

// ---------- 派生 ----------
// 名册里有、但这天未观察到旧聚合材料的号。单独一组，
// 让人能分清「今天没播」和「这个号没在监控里」。
const quietTags = computed(() => allTags.value.filter((x) => !tags.value.includes(x)))

const onlineSeries = computed(() => {
  if (!data.value) return []
  const pts = data.value.stats.online_series || []
  return [{ name: '在线', color: '#4c8dff', points: pts }]
})

const danmuSeries = computed(() => {
  if (!data.value) return []
  const pts = (data.value.danmu.permin || []).map((x) => [x.ts, x.danmaku])
  return [{ name: '弹幕', color: '#9a6bff', points: pts }]
})

const sentimentSeries = computed(() => {
  if (!data.value) return []
  const arr = data.value.portrait.series || []
  const pick = (key) => arr.map((x) => [x.ts, (x.sentiment && x.sentiment[key]) || 0])
  return [
    { name: '正向', color: '#30a46c', points: pick('positive') },
    { name: '中性', color: '#8b97a5', points: pick('neutral') },
    { name: '负向', color: '#e5484d', points: pick('negative') }
  ]
})

const portrait = computed(() => (data.value && data.value.portrait.latest && data.value.portrait.latest.portrait) || null)
const portSeries = computed(() => (data.value && data.value.portrait.series) || [])
const coldEvents = computed(() => (data.value && data.value.coldline.events) || [])
// 默认喂「互动流」：只含 danmaku/gift。全部事件（进入/点赞）占 95%，
// 直接喂 latest 会出现满屏"进入了直播间"（2026-10-25 反馈）。
// 旧后端没有 latest_interactive 字段 → 退回 latest，由 DanmuFeed 自己筛。
const feedItems = computed(() => {
  const dd = (data.value && data.value.danmu) || {}
  return dd.latest_interactive || dd.latest || []
})
const allItems = computed(() => (data.value && data.value.danmu.latest) || [])

const kpis = computed(() => {
  if (!data.value) return null
  const d = data.value
  const cl = d.coldline || {}
  return {
    danmu: d.danmu.danmaku_count != null ? d.danmu.danmaku_count : d.danmu.total,
    events: d.danmu.total,
    peak: d.stats.peak_online,
    pv: d.stats.total_pv,
    // 冷场次数优先用规则层（cold_events_*.jsonl）：它跟开不开 LLM 无关。
    // 老数据没有这个文件时退回数 coldline 的 cold_start。
    cold: cl.count != null ? cl.count : coldEvents.value.length,
    coldSec: cl.total_seconds || 0,
    coldSource: cl.source || 'none'
  }
})

// 规则层场次（有起止和时长）；没有规则层文件的老场次是空数组
const coldSessions = computed(() => (data.value && data.value.coldline.sessions) || [])

const coldSub = computed(() => {
  const k = kpis.value
  if (!k) return ''
  if (k.coldSource === 'rules') {
    return '规则层统计 · 累计 ' + fmtDur(k.coldSec)
  }
  if (k.coldSource === 'llm') {
    return '旧口径（这天没有规则层冷场文件）· 按台词事件数'
  }
  return '这天的规则层与 LLM 层都没有冷场记录'
})
</script>

<template>
  <LocalMaterials v-if="board.accessMode === 'local'" />
  <div class="toolbar">
    <label>日期</label>
    <select v-model="date" class="field" data-testid="review-date">
      <option v-for="d in dates" :key="d" :value="d">{{ d }}</option>
    </select>

    <label>主播</label>
    <select v-model="tag" class="field" :disabled="!tags.length && !allTags.length" data-testid="review-tag">
      <optgroup v-if="tags.length" label="当天有数据">
        <option v-for="x in tags" :key="x" :value="x">{{ x }}</option>
      </optgroup>
      <optgroup v-if="quietTags.length" label="当天未观察到聚合材料">
        <option v-for="x in quietTags" :key="x" :value="x">{{ x }}</option>
      </optgroup>
    </select>

    <button class="btn" :disabled="loading || bfReading || bfPosting" @click="refreshCurrent" data-testid="review-refresh">
      {{ loading ? '加载中…' : '刷新' }}
    </button>

    <span v-if="loading" class="spin"></span>
    <span v-if="data" class="meta" style="font-size: 12px; color: var(--dim)">
      room_id {{ data.room_id || '—' }} ·
      {{ data.files.danmu && data.files.danmu.exists ? '落盘 ' + fmtTime(data.files.danmu.mtime) : '无弹幕文件' }}
    </span>
  </div>

  <div v-if="err" class="banner err">{{ err }}</div>

  <div v-if="tag && date" class="card portrait-task" data-testid="portrait-task-panel">
    <h3>现有弹幕画像<span class="sub">按次生成 · {{ date }} · {{ tag }}</span></h3>
    <p class="task-help">使用已落盘的弹幕生成画像，可能产生模型费用。读取已有任务、刷新和查询状态不会发起模型调用；当天新增数据不会自动重做。此入口不分析转写，也不提供深度复盘。</p>
    <div class="task-actions">
      <button class="btn primary" :disabled="bfDisabled" @click="doBackfill(false)" data-testid="portrait-generate">{{ bfExisting ? '读取已有任务或画像' : '生成现有弹幕画像' }}</button>
      <button class="btn" :disabled="bfRedoDisabled" @click="doBackfill(true)" data-testid="portrait-redo">追加重做</button>
      <button class="btn ghost" :disabled="bfReading || bfPosting" @click="lookupTask(context(), null, true)" data-testid="portrait-recheck">{{ bfReading ? '查询中…' : '查询任务状态' }}</button>
    </div>
    <div class="task-state" data-testid="portrait-task-state">{{ bfLabel }}</div>
    <div v-if="!bfFresh && bfTask" class="task-help">以下为上次回执，当前状态尚未确认。</div>
    <div v-if="bfMsg" class="task-notice" data-testid="portrait-task-notice">{{ bfMsg }}</div>
    <div v-if="bfReason" class="task-notice">{{ bfReason }}</div>
    <div v-if="storageWarning" class="task-notice">{{ storageWarning }}</div>
    <div v-if="bfTask && bfTask.task_id" class="task-details" data-testid="portrait-task-details">
      <div>任务 <code data-testid="portrait-task-id">{{ bfTask.task_id }}</code></div>
      <div>数据日期 {{ bfTask.data_date }} · 主播 {{ bfTask.tag }}</div>
      <div>提交 {{ taskTime(bfTask.submitted_at) }} · 开始 {{ taskTime(bfTask.started_at) }} · 结束 {{ taskTime(bfTask.finished_at) }}</div>
      <div>成功 {{ countValue('ok') }} · 失败 {{ countValue('fail') }} · 跳过 {{ countValue('skipped') }} · 已处理窗口 {{ countValue('windows') }} / {{ countValue('windows_total') }} · 生成调用 {{ countValue('generate_calls') }} · 结果未知 {{ countValue('unknown') }}</div>
      <template v-if="bfTask.input">
        <div>输入弹幕 {{ numberValue(bfTask.input.event_count) }} · 无效记录 {{ numberValue(bfTask.input.invalid_count) }} · 字节 {{ numberValue(bfTask.input.bytes) }}</div>
        <div>输入范围 {{ taskTime(bfTask.input.start_ts) }} 至 {{ taskTime(bfTask.input.end_ts) }} · 采样截止 {{ taskTime(bfTask.input.captured_at) }}</div>
        <div v-if="bfTask.input.ref">输入引用 {{ bfTask.input.ref }}</div>
      </template>
      <div v-if="bfTask.output && bfTask.output.ref" data-testid="portrait-output-ref">输出引用 {{ bfTask.output.ref }} · 本任务写入 {{ numberValue(bfTask.output.rows) }} 行 · 旧画像保留</div>
    </div>
    <p v-if="bfState === 'legacy_unverified' && bfFresh" class="task-notice" data-testid="portrait-legacy">已保留历史画像，但缺少可靠任务记录，完整性未确认。普通读取不会重新调用模型；如需生成新结果，请明确选择追加重做。</p>
    <p v-if="bfState === 'partial' && bfFresh" class="task-notice">仅部分窗口成功；已生成的画像保留在下方输出中，未完成部分不会自动重跑。</p>
    <p v-if="['failed', 'blocked', 'interrupted', 'unknown'].includes(bfState) && bfFresh" class="task-help">此任务不会自动续跑。读取已有任务保留当前结果；只有明确追加重做才可能创建新执行。</p>
  </div>

  <div v-if="tag && date" class="grid-2 usage-cards">
    <section v-for="card in usageCards" :key="card.key" class="card usage-card" :data-testid="card.key + '-usage'">
      <h3>{{ card.title }}</h3><p class="task-help">{{ card.hint }}</p>
      <p class="task-help">仅覆盖已落盘指标，不是提供商完整账单。SDK 尝试不是 HTTP 次数；画像点、生成次数与指标记录分别统计，任务成功不代表费用覆盖完整。</p>
      <template v-if="card.key === 'task'"><button class="btn ghost" :disabled="taskUsageReading" @click="loadTaskUsage()" data-testid="task-usage-refresh">{{ taskUsageReading ? '只读读取用量中…' : '只读重查任务用量' }}</button><p v-if="taskUsageNotice" class="task-notice" data-testid="task-usage-notice">{{ taskUsageNotice }}</p></template>
      <p v-if="!card.view.available" class="task-notice">用量口径待确认，不能据此认为用量或费用为零。</p>
      <template v-else>
        <div class="task-state" :data-testid="card.key + '-usage-coverage'">{{ card.view.coverage }}</div>
        <div class="task-details">指标记录 {{ card.view.records }} · SDK 尝试 {{ card.view.attempts }} · 未发送记录 {{ card.view.notSent }} · 失败记录 {{ card.view.failed }}</div>
        <div class="task-details">已知输入 {{ card.view.input }} token · 已知输出 {{ card.view.output }} token</div>
        <div v-for="gap in card.view.gaps" :key="gap" class="task-notice">{{ gap }}</div>
        <p class="task-help">探测记录 {{ card.view.probeRecords }} · 连接测试记录 {{ card.view.testRecords }}；{{ card.view.diagnosticsExcluded ? '独立标记，不计上述业务用量与估算' : '诊断纳入口径待确认' }}。未落盘消费者不在统计内。</p>
        <p v-if="!card.view.estimates.length" class="task-notice" :data-testid="card.key + '-estimate-missing'">没有可用估价依据，不代表费用为零；可能缺少已知 usage 或发送时价格快照。</p>
        <div v-for="estimate in card.view.estimates" :key="estimate.currency" class="estimate-row" :data-testid="card.key + '-estimate'">
          <b>{{ estimate.partial ? '已知部分估算' : '已记录部分估算' }}：{{ estimate.currency }} {{ estimate.amount }}</b>
          <div>已定价分量 {{ estimate.priced }} · 缺价格分量 {{ estimate.unpriced }}；币种分列，不换汇合并。</div>
          <div>发送时价格版本：{{ estimate.versions.join('、') || '未记录' }}</div>
          <div v-for="snapshot in estimate.snapshots" :key="snapshot.version">{{ snapshot.source }} · 版本 {{ snapshot.version }} · 快照保存时点 {{ snapshot.saved_at }}</div>
        </div>
        <details v-if="card.view.days.length" class="task-details"><summary>按实际调用日查看（与材料日期分别统计）</summary><div v-for="row in card.view.days" :key="row.day">{{ row.day }} · SDK 尝试 {{ row.attempts }} · 已知输入 {{ row.input }} / 输出 {{ row.output }} token</div></details>
        <details v-if="card.view.providers.length" class="task-details"><summary>按发送时提供商 / 模型查看</summary><div v-for="row in card.view.providers" :key="row.provider + ':' + row.model">{{ row.provider }} / {{ row.model }} · 指标 {{ row.records }} · SDK 尝试 {{ row.attempts }} · 已知输入 {{ row.input }} / 输出 {{ row.output }} token</div></details>
      </template>
    </section>
  </div>

  <div v-if="!dates.length" class="waiting">
    <div class="big">未观察到历史聚合材料</div>
    <div class="small">未观察到相应日期的聚合记录，不能据此判断是否开播</div>
  </div>

  <div v-else-if="!tag" class="waiting">
    <div class="big">{{ date }} 未观察到聚合材料</div>
    <div class="small">换一个日期试试</div>
  </div>

  <div v-else-if="loading && !data" class="waiting">
    <div><span class="spin"></span></div>
    <div class="small">正在聚合 {{ date }} · {{ tag }} 的落盘数据…</div>
  </div>

  <template v-else-if="data">
    <!-- 当天没数据就别渲染一排 0 的 KPI：老大看图会以为「今天播了但一个人没有」 -->
    <div v-if="data.empty" class="waiting">
      <div class="big">{{ date }} · {{ tag }} 未观察到聚合材料</div>
      <div class="small">
        {{ quietTags.includes(tag)
          ? '该房间在监控名册中，但当天未观察到弹幕/在线等聚合材料，不能据此判断是否开播'
          : '当天未观察到该房间的相应聚合记录；转写等材料请查看独立本地材料区' }}
      </div>
    </div>
    <template v-else>
    <div class="grid-kpi">
      <KpiCard k="弹幕条数" :v="fmtN(kpis.danmu)" :s="'真弹幕 · 全部事件 ' + fmtN(kpis.events) + ' 条'" />
      <KpiCard k="在线峰值" :v="fmtN(kpis.peak)" :s="'采样点 ' + (data.stats.points || 0) + ' 条'" tone="blue" />
      <KpiCard k="累计观看 PV" :v="fmtN(kpis.pv)" s="最后一条 room_user_seq" />
      <KpiCard k="冷场次数" :v="kpis.cold" :s="coldSub" />
      <KpiCard k="冷场总时长" :v="fmtDur(kpis.coldSec)" :s="coldSessions.length ? '共 ' + coldSessions.length + ' 段' : '—'" />
    </div>

    <div class="grid-2" style="margin-top: 14px">
      <div class="card">
        <h3>每分钟弹幕量<span class="sub">{{ (data.danmu.permin || []).length }} 个分钟点</span></h3>
        <TrendChart :series="danmuSeries" :height="160" :gap="180" />
      </div>
      <div class="card">
        <h3>在线人数<span class="sub">每分钟取最后一点</span></h3>
        <TrendChart :series="onlineSeries" :height="160" :gap="180" />
      </div>
    </div>

    <div class="grid-3" style="margin-top: 14px">
      <div class="card">
        <h3>
          画像输出<span class="sub">保留的最新快照 · {{ portSeries.length }} 期序列</span>
        </h3>

        <template v-if="portrait">
          <div style="display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 10px">
            <div v-if="portrait.sentiment">
              <div class="k" style="font-size: 11px; color: var(--dim)">情绪</div>
              <div style="display: flex; gap: 10px; margin-top: 4px; font-size: 13px">
                <span style="color: var(--green)">正 {{ pct(portrait.sentiment.positive) }}</span>
                <span style="color: var(--dim)">中 {{ pct(portrait.sentiment.neutral) }}</span>
                <span style="color: var(--red)">负 {{ pct(portrait.sentiment.negative) }}</span>
              </div>
            </div>
            <div v-if="portrait.consumption_intent">
              <div class="k" style="font-size: 11px; color: var(--dim)">消费意向</div>
              <div style="margin-top: 4px; font-size: 13px">
                {{ levelText(portrait.consumption_intent.level) }}
                <span style="color: var(--dim-2); font-size: 12px">
                  {{ (portrait.consumption_intent.keywords || []).slice(0, 4).join(' / ') }}
                </span>
              </div>
            </div>
          </div>

          <div v-if="portrait.interest_topics && portrait.interest_topics.length" style="margin-bottom: 10px">
            <div class="k" style="font-size: 11px; color: var(--dim); margin-bottom: 4px">兴趣话题</div>
            <div style="display: flex; gap: 6px; flex-wrap: wrap">
              <span
                v-for="(x, i) in portrait.interest_topics.slice(0, 8)"
                :key="i"
                style="font-size: 12px; border: 1px solid var(--line); border-radius: 999px; padding: 2px 9px; color: var(--ink-2)"
              >{{ x }}</span>
            </div>
          </div>

          <div v-if="portrait.question_topics && portrait.question_topics.length">
            <div class="k" style="font-size: 11px; color: var(--dim); margin-bottom: 2px">提问聚类</div>
            <ul class="list">
              <li v-for="(q, i) in portrait.question_topics.slice(0, 6)" :key="i">
                <span class="q">{{ q.topic }}</span><span class="n">×{{ q.count }}</span>
                <div v-if="q.examples && q.examples.length" class="ex">如：{{ q.examples[0] }}</div>
              </li>
            </ul>
          </div>

          <div v-if="portrait.suggestions && portrait.suggestions.length" style="margin-top: 10px">
            <div class="k" style="font-size: 11px; color: var(--dim); margin-bottom: 2px">建议</div>
            <ul class="list">
              <li v-for="(s, i) in portrait.suggestions" :key="i" class="arrow">{{ s }}</li>
            </ul>
          </div>
        </template>
        <div v-else class="empty">该场次没有画像快照（LLM 层未开或未到快照周期）</div>
      </div>

      <div class="stack">
        <div class="card">
          <h3>情绪趋势<span class="sub">取自画像序列</span></h3>
          <TrendChart
            v-if="portSeries.length"
            :series="sentimentSeries"
            :height="130"
            :gap="900"
          />
          <div v-else class="empty">无画像序列</div>
        </div>

        <div class="card cold-records-card">
          <h3 id="review-cold-records-title">
            冷场记录
            <span class="sub">
              {{ coldSessions.length ? coldSessions.length + ' 段 · 累计 ' + fmtDur(kpis.coldSec) : '最多 50 条台词' }}
            </span>
          </h3>
          <div
            class="cold-records-body"
            role="region"
            aria-labelledby="review-cold-records-title"
            tabindex="0"
          >
          <!-- 规则层场次：与开不开 LLM 无关，永远准确 -->
          <div v-if="coldSessions.length" style="margin-bottom: 10px">
            <div
              v-for="(s, i) in coldSessions.slice().reverse()"
              :key="'s' + i"
              style="font-size: 12px; color: var(--ink-2); padding: 2px 0"
            >
              <span style="color: var(--amber); font-variant-numeric: tabular-nums">{{ fmtTime(s.start) }}</span>
              <span style="color: var(--dim-2)"> → {{ s.end ? fmtTime(s.end) : '未闭合' }}</span>
              <span style="color: var(--dim); margin-left: 6px">{{ s.duration != null ? fmtDur(s.duration) : '' }}</span>
              <span v-if="s.count != null" style="color: var(--dim-2); margin-left: 6px">窗口弹幕 {{ s.count }} 条</span>
            </div>
          </div>
          <ul v-if="coldEvents.length" class="list">
            <li v-for="(e, i) in coldEvents.slice().reverse()" :key="i">
              <span style="color: var(--amber); font-variant-numeric: tabular-nums">{{ fmtTime(e.ts) }}</span>
              <span style="color: var(--dim); margin-left: 6px">{{ e.event || '' }}</span>
              <div
                v-for="(line, j) in (e.lines || [])"
                :key="j"
                style="color: var(--ink-2); font-size: 12px; margin-top: 2px"
              >{{ line }}</div>
            </li>
          </ul>
          <div v-else-if="coldSessions.length" class="empty">有冷场，但这场没开 LLM，所以没有救场台词</div>
          <div v-else class="empty">该场次无冷场事件</div>
          </div>
        </div>

      </div>
    </div>

    <div class="card" style="margin-top: 14px">
      <h3>弹幕流<span class="feed-hint">互动 {{ feedItems.length }} 条 · 最新在上</span></h3>
      <DanmuFeed :items="feedItems" :all-items="allItems" :height="340" :limit="200" />
    </div>
    </template>
  </template>
</template>

<style scoped>
.cold-records-card { min-width: 0; }
.cold-records-card > h3 { flex-wrap: wrap; }
.cold-records-body {
  min-width: 0;
  max-height: clamp(160px, 45vh, 420px);
  overflow-y: auto;
  overflow-wrap: anywhere;
  scrollbar-gutter: stable;
  outline-offset: 3px;
}
.cold-records-body:focus-visible { outline: 2px solid var(--blue); border-radius: 4px; }
.portrait-task { margin-bottom: 14px; }
.task-help { color: var(--dim); font-size: 12px; line-height: 1.8; margin: 8px 0; }
.task-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.task-state { font-size: 14px; font-weight: 600; margin: 8px 0; }
.task-notice { color: var(--amber); font-size: 12px; line-height: 1.8; margin: 6px 0; }
.task-details { font-size: 12px; color: var(--ink-2); line-height: 1.9; overflow-wrap: anywhere; }
.usage-cards { margin-bottom: 14px; }
.usage-card h3 { margin-bottom: 6px; }
.estimate-row { border: 1px solid var(--line-soft); border-radius: 6px; padding: 8px 10px; margin: 8px 0; font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; }
</style>
