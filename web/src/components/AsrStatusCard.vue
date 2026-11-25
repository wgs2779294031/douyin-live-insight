<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, useBoard } from '../store'

const board = useBoard()
const snapshot = ref(null)
const accepted = ref(false)
const reading = ref(false)
const acting = ref(false)
const receivedAt = ref(null)
const clock = ref(Date.now() / 1000)
const readHint = ref('')
const actionHint = ref('')
let sequence = 0, disposed = false, pending = null, tick = null, poll = null
let lastGeneration = '', lastSnapshotAt = null, lastSequence = 0
let storageRejectedGeneration = '', storageRejectedSequence = 0
let diskRejectedGeneration = '', diskRejectedSequence = 0
const retiredGenerations = new Set()
const TTL = 15
const DISK_TTL = 60, LOW_SPACE = 1073741824

const labels = {
  not_started: '转写进程尚未启动', starting: '转写进程正在启动', scanning: '正在检查闭合录音', idle: '转写进程空闲',
  busy: '其它转写正在占用写入通道', processing: '正在处理录音', backoff: '等待下次重试',
  stopping: '正在停止并等待当前片收尾', draining: '正在等待资源收尾', stopped: '转写进程已停止',
  identity_unknown: '转写进程身份待确认', admission_unknown: '转写接纳状态待确认',
  dependency_error: '转写依赖需要检查', restart_limited: '自动启动已暂缓，请检查后重试',
  storage_error: '转写状态保存失败', snapshot_unconfirmed: '转写快照待确认',
  failed: '转写进程启动失败', unknown: '转写状态待确认'
}
const phases = { idle: '等待下一次检查', scan: '检查闭合录音', scanning: '检查闭合录音', verifying: '验证来源和结果',
  writer_busy: '等待其他转写释放', loading_model: '加载本地模型', model_loading: '加载本地模型',
  processing: '转写当前片', recognizing: '识别当前片', committing: '保存规范结果',
  transcribing: '识别当前片', projecting: '恢复文本投影', projection: '恢复文本投影', cleaning: '清理已完成转写的原片',
  waiting: '等待下一次检查', retry: '等待重试', backoff: '等待重试', stopping: '当前片收尾', checking: '重新检查' }
const conditions = {
  periodic_scan: '按周期继续检查闭合录音', writer_release: '等待其它转写释放写入通道',
  after_backoff: '退避结束后重新检查', explicit_retry_or_dependency_change: '等待手动重试或依赖变化',
  repair_evidence: '核对并恢复来源或身份凭据后再检查', waiting_admission: '等待本轮转写接纳',
  stop_after_current: '等待已接纳的当前片收尾', none: '暂无安排的下一步'
}
const errors = {
  model_unavailable: '本地模型不可用', model_load_failed: '本地模型加载失败',
  dependency_missing: '转写依赖缺失', dependency_error: '转写依赖需要检查',
  decode_failed: '音频解码失败，原片已保留', transcription_failed: '本次转写未完成，原片已保留',
  projection_pending: '规范结果已保存，文本投影待恢复', projection_failed: '文本投影暂未恢复',
  source_unconfirmed: '录音来源尚未确认', source_missing: '源片缺失，已有文本保留',
  source_changed: '源片发生变化，需要核对', too_small: '片段过小，未计作转写成功',
  legacy_conflict: '旧材料需要核对，未自动改写', attempt_missing: '接受状态缺失，需要恢复',
  result_generation_missing: '结果代次无法确认，需要恢复', retry_state_invalid: '重试状态损坏，需要检查',
  retry_state_unavailable: '重试状态暂不可用', retry_exhausted: '已达到自动重试上限',
  retry_capacity: '重试状态记录已达上限，需要检查',
  attempt_invalid: '接受状态无法验证，需要恢复', attempt_pending: '存在尚未完成的接受意图',
  attempt_failed: '上次接受的处理未完成', attempt_lineage_mismatch: '接受状态与结果代次不一致',
  predecessors_invalid: '前代授权无法验证，需要恢复', result_missing: '规范结果缺失，需要恢复',
  result_invalid: '规范结果无法验证，需要恢复', record_invalid: '持久记录无法验证，需要恢复',
  projection_invalid: '文本投影凭证无法验证', projection_unverified: '文本投影尚未确认',
  output_changed: '文本输出发生变化，需要核对', commit_unverified: '保存结果尚未确认',
  result_too_large: '单片结果超过处理上限，材料已保留', decode_unconfirmed: '解码结果尚未确认',
  linked_path: '录音来源路径无法确认', unsafe_path: '录音来源路径无法确认',
  invalid_file: '录音来源文件无法确认', invalid_closure: '闭合记录无法确认',
  invalid_segment: '片段身份无法确认', invalid_segment_name: '片段身份无法确认',
  invalid_folder: '录音目录身份无法确认', outside_data: '录音来源超出本次数据范围',
  invalid_json: '持久记录格式无法确认', state_too_large: '状态记录超过处理上限',
  state_changed: '持久记录发生变化，需要核对', worker_error: '转写状态需要检查',
  busy: '其他转写正在使用写入通道', writer_busy: '其他转写正在使用写入通道',
  storage_error: '保存状态失败，材料已保留', state_unavailable: '状态暂不可用'
}
const cleanupStates = { deleted: '已确认原片清理', pending: '清理待重试或结算', kept: '已保留原片', busy: '写入通道忙，尚未执行' }
const cleanupReasons = {
  audio_deleted: '原片已清理，转写文本与来源元数据保留',
  cleanup_platform_unsupported: '当前环境暂不能确认安全清理', cleanup_record_invalid: '清理凭据需要检查',
  cleanup_binding_conflict: '清理凭据与当前片不一致', cleanup_handle_unconfirmed: '录音文件身份待确认',
  cleanup_accept_unconfirmed: '清理接受状态待确认', cleanup_commit_unconfirmed: '清理结算待确认',
  cleanup_cancel_unconfirmed: '清理取消结果待确认', cleanup_delete_unconfirmed: '原片处置结果待确认',
  cleanup_delete_failed: '清理未完成，稍后按策略重试', cleanup_unconfirmed: '清理状态待确认',
  cleanup_retry_exhausted: '已达到自动清理重试上限', cleanup_capacity: '清理调度记录已达上限'
}
const count = (value) => Number.isSafeInteger(value) && value >= 0 && value <= 1000000000 ? value : null
const timestamp = (value) => typeof value === 'number' && Number.isFinite(value) && value > 0
  && Number.isFinite(new Date(value * 1000).getTime()) ? value : null
const safeTag = (value) => typeof value === 'string' && value.length <= 40
  && !/[\x00-\x1f/\\:*?"<>|]/.test(value) ? value : ''
const storageNumber = (value) => Number.isSafeInteger(value) && value >= 0 ? value : null
const nullableStorageNumber = (value) => value === null || storageNumber(value) !== null
const nullableCount = (value) => value === null || count(value) !== null
const nullableTimestamp = (value) => value === null || timestamp(value) !== null
function normalizeStorage(raw) {
  const source = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
  const scan = source.scan && typeof source.scan === 'object' ? source.scan : {}
  const disk = source.disk && typeof source.disk === 'object' ? source.disk : {}
  const keys = ['audio_bytes', 'audio_files', 'transcription_pending', 'cleanup_pending', 'cleanup_failed', 'cleanup_blocked']
  const known = source.schema === 1
  const shape = known && typeof source.verified === 'boolean' && typeof source.stale === 'boolean'
    && keys.every((key) => key === 'audio_bytes' ? nullableStorageNumber(source[key]) : nullableCount(source[key]))
    && typeof scan.partial === 'boolean' && typeof scan.limited === 'boolean'
    && nullableCount(scan.checked) && nullableTimestamp(scan.started_at) && nullableTimestamp(scan.last_complete_at)
    && scan.approximate === true && scan.counts_scope === 'scan_round'
  const free = storageNumber(disk.free_bytes), total = storageNumber(disk.total_bytes)
  const diskShape = known && typeof disk.verified === 'boolean' && typeof disk.stale === 'boolean'
    && free !== null && total !== null && total > 0 && free <= total && disk.threshold_bytes === LOW_SPACE
  const last = source.last_cleanup && typeof source.last_cleanup === 'object' ? source.last_cleanup : null
  const reason = typeof last?.reason === 'string' ? last.reason : ''
  return { known, shape, verified: source.verified === true, stale: source.stale !== false,
    snapshotAt: timestamp(source.snapshot_at), receivedValid: false, displayable: false,
    ...Object.fromEntries(keys.map((key) => [key, key === 'audio_bytes' ? storageNumber(source[key]) : count(source[key])])),
    scan: { partial: scan.partial !== false, limited: scan.limited === true, checked: count(scan.checked),
      startedAt: timestamp(scan.started_at), updatedAt: timestamp(scan.updated_at), lastCompleteAt: timestamp(scan.last_complete_at) },
    disk: { shape: diskShape, verified: disk.verified === true, stale: disk.stale !== false,
      free, total, sampledAt: timestamp(disk.sampled_at), receivedValid: false, displayable: false },
    lastHasRecord: source.last_cleanup !== null,
    last: last ? { state: typeof last.state === 'string' && Object.hasOwn(cleanupStates, last.state) ? last.state : '',
      at: timestamp(last.at), timeValid: false, tag: safeTag(last.tag),
      date: typeof last.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(last.date) ? last.date : '',
      segment: typeof last.segment === 'string' && /^seg_\d{4,}(?:\.flv)?$/.test(last.segment) && last.segment.length <= 64 ? last.segment : '',
      reason: Object.hasOwn(cleanupReasons, reason) ? cleanupReasons[reason] : Object.hasOwn(errors, reason) ? errors[reason] : '清理原因待确认' } : null }
}
function rejectStorage(value) {
  value.storage.receivedValid = false
  if (!value.generation || value.sequence === null || value.sequence < 1) return
  if (storageRejectedGeneration !== value.generation) { storageRejectedGeneration = value.generation; storageRejectedSequence = 0 }
  storageRejectedSequence = Math.max(storageRejectedSequence, value.sequence)
}
function rejectDisk(value) {
  value.storage.disk.receivedValid = false
  if (!value.generation || value.sequence === null || value.sequence < 1) return
  if (diskRejectedGeneration !== value.generation) { diskRejectedGeneration = value.generation; diskRejectedSequence = 0 }
  diskRejectedSequence = Math.max(diskRejectedSequence, value.sequence)
}
function acceptStorage(value, received) {
  const storage = value.storage, disk = storage.disk, generation = value.generation, statusSequence = value.sequence
  const bound = !!generation && statusSequence !== null && statusSequence >= 1
  storage.displayable = storage.shape && storage.snapshotAt !== null && storage.snapshotAt <= received
    && storage.snapshotAt === storage.scan.updatedAt
    && (storage.scan.startedAt === null || storage.scan.startedAt <= storage.snapshotAt)
    && (storage.scan.lastCompleteAt === null || storage.scan.lastCompleteAt <= storage.snapshotAt)
  const valid = bound && storage.displayable && storage.verified && !storage.stale && received - storage.snapshotAt <= TTL
  if (!valid) rejectStorage(value)
  storage.receivedValid = valid && (storageRejectedGeneration !== generation || statusSequence > storageRejectedSequence)
  disk.displayable = disk.shape && disk.sampledAt !== null && disk.sampledAt <= received
  const diskValid = bound && disk.displayable && disk.verified && !disk.stale && received - disk.sampledAt <= DISK_TTL
  if (!diskValid) rejectDisk(value)
  disk.receivedValid = diskValid && (diskRejectedGeneration !== generation || statusSequence > diskRejectedSequence)
  if (storage.last) storage.last.timeValid = storage.last.at !== null && storage.last.at <= received
}
function updateClock() {
  const current = Date.now() / 1000
  const value = snapshot.value
  if (value?.storage) {
    if (value.snapshotAt !== null && (value.snapshotAt > current || current - value.snapshotAt > TTL)) { rejectStorage(value); rejectDisk(value) }
    if (value.storage.snapshotAt !== null && (value.storage.snapshotAt > current || current - value.storage.snapshotAt > TTL)) rejectStorage(value)
    if (value.storage.disk.sampledAt !== null && (value.storage.disk.sampledAt > current || current - value.storage.disk.sampledAt > DISK_TTL)) rejectDisk(value)
  }
  clock.value = current
}
function normalize(response) {
  const value = response?.status
  if (!value || !Object.hasOwn(labels, value.state) || typeof value.verified !== 'boolean'
    || typeof value.resource_held !== 'boolean' || typeof value.stale !== 'boolean') throw new Error('invalid asr summary')
  const generation = typeof value.generation === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value.generation)
    ? value.generation : ''
  const statusSequence = count(value.sequence)
  if (value.verified && (!generation || statusSequence === null || statusSequence < 1)) throw new Error('missing worker generation')
  const source = value.current || {}
  return { state: value.state, verified: value.verified, resourceHeld: value.resource_held,
    stale: value.stale, noProgress: value.no_progress === true, generation, sequence: statusSequence,
    snapshotAt: timestamp(value.snapshot_at), nextRetryAt: timestamp(value.next_retry_at),
    nextCondition: Object.hasOwn(conditions, value.next_condition) ? value.next_condition : 'unknown',
    lastProgressAt: timestamp(value.last_progress_at), lastCompletedAt: timestamp(value.last_completed_at),
    counts: Object.fromEntries(['pending', 'projection_pending', 'retryable', 'blocked', 'complete']
      .map((key) => [key, count(value.counts?.[key])])),
    scan: { partial: value.scan?.partial !== false, limited: value.scan?.limited === true,
      checked: count(value.scan?.checked), lastCompleteAt: timestamp(value.scan?.last_complete_at) },
    current: { tag: safeTag(source.tag), date: typeof source.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(source.date) ? source.date : '',
      segment: typeof source.segment === 'string' && /^seg_\d{4,}(?:\.flv)?$/.test(source.segment) && source.segment.length <= 64 ? source.segment : '',
      phase: Object.hasOwn(phases, source.phase) ? source.phase : '', attempt: count(source.attempt) },
    error: Object.hasOwn(errors, value.error) || Object.hasOwn(cleanupReasons, value.error) ? value.error : (value.error ? 'unknown' : ''),
    storage: normalizeStorage(value.storage) }
}
const connected = computed(() => board.accessMode === 'local' && board.accessState === 'ready')
const readCurrent = computed(() => accepted.value && receivedAt.value !== null
  && receivedAt.value <= clock.value && clock.value - receivedAt.value <= TTL)
const workerCurrent = computed(() => readCurrent.value && snapshot.value?.verified && !snapshot.value.stale
  && snapshot.value.snapshotAt !== null && snapshot.value.snapshotAt <= clock.value
  && clock.value - snapshot.value.snapshotAt <= TTL)
const storageCurrent = computed(() => workerCurrent.value && snapshot.value.storage.receivedValid
  && snapshot.value.storage.snapshotAt <= clock.value && clock.value - snapshot.value.storage.snapshotAt <= TTL)
const diskCurrent = computed(() => workerCurrent.value && snapshot.value.storage.disk.receivedValid
  && snapshot.value.storage.disk.sampledAt <= clock.value && clock.value - snapshot.value.storage.disk.sampledAt <= DISK_TTL)
const diskLow = computed(() => diskCurrent.value && snapshot.value.storage.disk.free < LOW_SPACE)
const healthy = computed(() => workerCurrent.value && snapshot.value.state === 'idle' && !snapshot.value.noProgress
  && !snapshot.value.scan.partial && !snapshot.value.scan.limited
  && ['pending', 'projection_pending', 'retryable', 'blocked'].every((key) => snapshot.value.counts[key] === 0))
const stateText = computed(() => {
  if (!readCurrent.value || !snapshot.value) return '转写状态待确认'
  if (['not_started', 'starting', 'failed', 'restart_limited', 'stopped', 'stopping', 'draining', 'identity_unknown', 'admission_unknown', 'snapshot_unconfirmed', 'unknown'].includes(snapshot.value.state)) return labels[snapshot.value.state]
  return workerCurrent.value ? labels[snapshot.value.state] : '上次转写状态仅作记录，当前待确认'
})
const canRetry = computed(() => connected.value && readCurrent.value && snapshot.value && !reading.value && !acting.value
  && !['stopping', 'draining', 'identity_unknown', 'admission_unknown', 'snapshot_unconfirmed', 'unknown', 'not_started', 'starting'].includes(snapshot.value.state)
  && (workerCurrent.value || ['failed', 'restart_limited', 'stopped'].includes(snapshot.value.state)))
const currentText = computed(() => {
  const value = snapshot.value?.current
  if (!value?.tag && !value?.date && !value?.segment) return '尚无当前片段记录'
  return [value.tag, value.date, value.segment, value.phase ? phases[value.phase] : '阶段待确认',
    value.attempt !== null ? `本轮尝试 ${value.attempt}` : ''].filter(Boolean).join(' · ')
})
const nextConditionText = computed(() => readCurrent.value && snapshot.value
  ? conditions[snapshot.value.nextCondition] || '待确认，请重新读取状态' : '待确认，请重新读取状态')
const storageScopeText = computed(() => {
  if (!storageCurrent.value) return '当前统计待确认；以下只作上次记录，不能据此认定没有积压。'
  if (snapshot.value.storage.scan.partial || snapshot.value.storage.scan.limited) return `本轮仅已检查部分规范录音${snapshot.value.storage.scan.limited ? '（达到检查上限）' : ''}；以下不是全部数据的总量。`
  return '最近完整扫描的近似量；数值反映该轮观察范围。'
})
const shownBytes = (value) => {
  if (value === null || value === undefined) return '待确认'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  let amount = value, index = 0
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index++ }
  return `${amount.toLocaleString('zh-CN', { maximumFractionDigits: index ? 2 : 0 })} ${units[index]}`
}
const shownStorage = (key) => snapshot.value?.storage.displayable && snapshot.value.storage[key] !== null
  ? snapshot.value.storage[key].toLocaleString('zh-CN') : '待确认'
const storageBytes = computed(() => shownBytes(snapshot.value?.storage.displayable ? snapshot.value.storage.audio_bytes : null))
const diskBytes = computed(() => shownBytes(snapshot.value?.storage.disk.displayable ? snapshot.value.storage.disk.free : null))
const lastCleanupText = computed(() => {
  const storage = snapshot.value?.storage
  if (!storage?.displayable) return '最近清理结果待确认'
  if (!storage.lastHasRecord) return '尚无清理结果记录'
  const last = storage.last
  if (!last?.state || !last.timeValid) return '最近清理结果待确认'
  return [cleanupStates[last.state], last.tag, last.date, last.segment, last.reason].filter(Boolean).join(' · ')
})
const shownCount = (key) => snapshot.value?.counts[key] === null || !snapshot.value ? '待确认' : snapshot.value.counts[key].toLocaleString('zh-CN')
const shownTime = (value) => value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚无记录'

function accept(response) {
  const value = normalize(response)
  const received = Date.now() / 1000
  if (value.snapshotAt !== null && value.snapshotAt > received) throw new Error('future asr snapshot')
  if (value.generation && retiredGenerations.has(value.generation)) throw new Error('retired asr generation')
  if (value.verified && value.generation === lastGeneration
    && (value.sequence < lastSequence || (value.sequence === lastSequence && value.snapshotAt !== lastSnapshotAt))) throw new Error('old asr snapshot')
  if (value.generation && value.generation !== lastGeneration) {
    if (lastGeneration) retiredGenerations.add(lastGeneration)
    if (retiredGenerations.size > 8) retiredGenerations.delete(retiredGenerations.values().next().value)
    lastGeneration = value.generation
    lastSnapshotAt = null
    lastSequence = 0
    storageRejectedGeneration = diskRejectedGeneration = ''
    storageRejectedSequence = diskRejectedSequence = 0
  }
  acceptStorage(value, received)
  if (value.verified && value.generation) { lastSnapshotAt = value.snapshotAt; lastSequence = value.sequence }
  snapshot.value = value
  receivedAt.value = received
  clock.value = receivedAt.value
  accepted.value = true
}
function cancel() {
  ++sequence
  pending?.abort()
  pending = null
  accepted.value = false
  reading.value = false
  acting.value = false
}
async function refresh(manual = false) {
  if (!connected.value || reading.value || acting.value || disposed) return
  const request = ++sequence
  const controller = new AbortController()
  pending = controller
  reading.value = true
  if (manual) { readHint.value = ''; actionHint.value = '' }
  try {
    const response = await api.asrStatus({ signal: controller.signal })
    if (disposed || request !== sequence || !connected.value) return
    accept(response)
    readHint.value = ''
  } catch (_) {
    if (!disposed && request === sequence) {
      accepted.value = false
      readHint.value = '状态读取失败，当前待确认。请重新读取；读取不会发起转写。'
    }
  } finally {
    if (!disposed && request === sequence) { pending = null; reading.value = false }
  }
}
async function retry() {
  if (!canRetry.value) return
  const request = ++sequence
  const controller = new AbortController()
  pending = controller
  acting.value = true
  accepted.value = false
  actionHint.value = ''
  try {
    const response = await api.asrRetry({ signal: controller.signal })
    if (disposed || request !== sequence || !connected.value) return
    accept(response)
    actionHint.value = response.ok === true ? '已请求重新检查可重试项；实际结果以之后的状态为准。'
      : '本次请求尚未确认，请重新读取状态后再决定是否重试。'
  } catch (_) {
    if (!disposed && request === sequence) {
      accepted.value = false
      actionHint.value = '请求结果待确认。重新连接或读取只查看状态，不会自动重发本次请求。'
    }
  } finally {
    if (!disposed && request === sequence) { pending = null; acting.value = false }
  }
}
watch(connected, (ready) => {
  cancel()
  if (ready) refresh()
})
onMounted(() => {
  refresh()
  tick = setInterval(updateClock, 1000)
  poll = setInterval(() => refresh(), 5000)
})
onUnmounted(() => { disposed = true; cancel(); clearInterval(tick); clearInterval(poll) })
</script>

<template>
  <section class="card asr-card" data-testid="asr-status-card">
    <h3>录音转写<span class="sub">自动检查已闭合录音，逐片处理</span></h3>
    <div class="status-line" data-testid="asr-state" :data-current="workerCurrent ? 'true' : 'false'">
      <span class="dot" data-testid="asr-dot" :class="healthy ? 'on' : 'wait'"></span>
      <strong>{{ stateText }}</strong>
    </div>
    <p v-if="!workerCurrent" class="notice" data-testid="asr-stale">{{ snapshot ? '快照已过期或本轮身份尚待确认，上次信息仅作记录。' : '尚无有效转写快照，当前状态待确认。' }}</p>
    <p v-if="snapshot?.noProgress" class="notice" data-testid="asr-no-progress">仍有资源占用，长时间未确认新进展；不表示已经停止或完成。</p>
    <p class="detail" data-testid="asr-resource">{{ readCurrent && snapshot?.resourceHeld ? '后台仍保留转写资源。' : '资源占用状态以后台当前核验为准。' }}</p>
    <p class="detail" data-testid="asr-scan">{{ !workerCurrent ? '以下为上次检查范围的记录，当前待确认；不能据此认定没有积压。' : (snapshot?.scan.partial || snapshot?.scan.limited ? '仅部分范围已检查，仍需继续；以下不是整库积压总数。' : '以下为本轮已检查范围内的记录。') }}</p>
    <dl class="counts" data-testid="asr-counts">
      <div><dt>可信待处理</dt><dd>{{ shownCount('pending') }}</dd></div>
      <div><dt>投影待恢复</dt><dd>{{ shownCount('projection_pending') }}</dd></div>
      <div><dt>失败待重试</dt><dd>{{ shownCount('retryable') }}</dd></div>
      <div><dt>来源或证据阻断</dt><dd>{{ shownCount('blocked') }}</dd></div>
      <div><dt>已确认完成</dt><dd>{{ shownCount('complete') }}</dd></div>
    </dl>
    <p class="detail" data-testid="asr-current">{{ currentText }}</p>
    <p v-if="snapshot?.error" class="notice" data-testid="asr-error">{{ errors[snapshot.error] || cleanupReasons[snapshot.error] || '转写需要检查，请核对本地状态。' }}</p>
    <p class="detail">上次真实进展：{{ shownTime(snapshot?.lastProgressAt) }}<br />上次完成：{{ shownTime(snapshot?.lastCompletedAt) }}</p>
    <p class="detail">最近完整扫描：{{ shownTime(snapshot?.scan.lastCompleteAt) }}<br />下次重试：{{ shownTime(snapshot?.nextRetryAt) }}</p>
    <p class="detail" data-testid="asr-next-condition">下次条件：{{ nextConditionText }}</p>
    <div class="actions">
      <button class="btn ghost" data-testid="asr-refresh" :disabled="!connected || reading || acting" @click="refresh(true)">{{ reading ? '读取中…' : '重新读取状态' }}</button>
      <button class="btn" data-testid="asr-retry" :disabled="!canRetry" @click="retry">{{ acting ? '请求中…' : '重新检查 / 重试可重试失败' }}</button>
    </div>
    <p class="detail">刷新和重连只读取状态；重试不会强制重做成功片。完成凭据确认的原片由后台自动清理；停止会等待已接纳的当前片收尾。</p>
    <p v-if="readHint" class="notice" data-testid="asr-read-hint">{{ readHint }}</p>
    <p v-if="actionHint" class="notice" data-testid="asr-action-hint">{{ actionHint }}</p>
    <section class="storage-section" data-testid="storage-status" :data-current="storageCurrent ? 'true' : 'false'">
      <h4>录音存储</h4>
      <p class="detail" data-testid="storage-scope">{{ storageScopeText }}</p>
      <dl class="counts">
        <div><dt>录音逻辑大小</dt><dd data-testid="storage-audio-bytes">{{ storageBytes }}</dd></div>
        <div><dt>录音文件</dt><dd data-testid="storage-audio-files">{{ shownStorage('audio_files') }}</dd></div>
        <div><dt>待完成转写</dt><dd data-testid="storage-transcription-pending">{{ shownStorage('transcription_pending') }}</dd></div>
        <div><dt>待清理</dt><dd data-testid="storage-cleanup-pending">{{ shownStorage('cleanup_pending') }}</dd></div>
        <div><dt>清理失败</dt><dd data-testid="storage-cleanup-failed">{{ shownStorage('cleanup_failed') }}</dd></div>
        <div><dt>清理受阻</dt><dd data-testid="storage-cleanup-blocked">{{ shownStorage('cleanup_blocked') }}</dd></div>
      </dl>
      <p class="detail">本轮已检查：{{ snapshot?.storage.displayable && snapshot.storage.scan.checked !== null ? snapshot.storage.scan.checked.toLocaleString('zh-CN') : '待确认' }} 项<br />统计观察时间：{{ shownTime(snapshot?.storage.displayable ? snapshot.storage.snapshotAt : null) }}<br />最近完整扫描：{{ shownTime(snapshot?.storage.displayable ? snapshot.storage.scan.lastCompleteAt : null) }}</p>
      <p class="detail" data-testid="storage-disk" :data-current="diskCurrent ? 'true' : 'false'">{{ diskCurrent ? '所在磁盘可用空间' : '上次磁盘可用空间（当前待确认）' }}：{{ diskBytes }}<br />磁盘采样：{{ shownTime(snapshot?.storage.disk.displayable ? snapshot.storage.disk.sampledAt : null) }}</p>
      <p v-if="diskLow" class="notice" data-testid="storage-low-space">所在磁盘可用空间不足 1 GiB，请关注存储容量。</p>
      <p class="detail" data-testid="storage-last-cleanup">{{ storageCurrent ? '最近清理结果' : '上次清理记录（当前待确认）' }}：{{ lastCleanupText }}<br />记录时间：{{ shownTime(snapshot?.storage.last?.timeValid ? snapshot.storage.last.at : null) }}</p>
      <p class="detail">扫描不是原子快照，录制增长和清理会让数值变化；逻辑大小与磁盘实际占用可能不同，清理确认不等于回收同量物理空间。上方刷新只读取缓存状态。</p>
    </section>
  </section>
</template>

<style scoped>
.asr-card { align-self: start; }
.status-line { display: flex; align-items: center; gap: 8px; font-size: 13px; margin: 12px 0; }
.detail, .notice { font-size: 12px; line-height: 1.7; margin: 8px 0; }
.detail { color: var(--dim-2); }
.notice { color: var(--amber); }
.counts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 14px; margin: 12px 0; font-size: 12px; }
.counts div { display: flex; justify-content: space-between; gap: 8px; }
dt { color: var(--dim); }
dd { margin: 0; font-variant-numeric: tabular-nums; }
.actions { display: flex; flex-wrap: wrap; gap: 7px; margin: 12px 0; }
.storage-section { border-top: 1px solid var(--line); margin-top: 16px; padding-top: 4px; }
.storage-section h4 { font-size: 13px; margin: 12px 0; }
</style>
