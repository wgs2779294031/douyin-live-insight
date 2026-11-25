<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, watch } from 'vue'
import { materialsAPI, useBoard } from '../store'
import { createMaterialsController, createMaterialsState, materialsPageExport, materialsReason } from '../materials'

const board = useBoard(), model = reactive(createMaterialsState())
let session
try { session = window.sessionStorage } catch (_) { /* The controller reports unavailable recovery storage. */ }
const controller = createMaterialsController({ api: materialsAPI, state: model, storage: session })
if (!session) model.storageWarning = '无法保存本页找回标识；离开页面后可能无法找回本次回执。'
const form = reactive({ view: 'canonical', room: '', date: '', platform: '', stream: '', from: '', until: '' })
const urls = new Set(), timers = new Set()
let poll = null
const canRead = computed(() => model.connected && model.statusFresh && model.material?.state === 'ready')
const canUpdate = computed(() => model.connected && model.statusFresh && !model.posting && !model.activeId && !model.stopping)
const labels = {
  not_requested: '本页尚未提交更新', pending: '请求已发出，等待本次回执', running: '本次正在整理',
  published: '本次已发布', failed: '本次整理失败', unconfirmed: '本次发布结果未确认',
  busy: '本次未整理：通道占用，外部工作状态未知', unknown: '本次结果待确认', not_accepted: '本次未接纳'
}
const resultLabel = computed(() => (!model.statusFresh && model.receipt ? '上次回执：' : '') + (labels[model.requestState] || labels.unknown))
const materialLabel = computed(() => {
  if (!model.statusFresh) return '当前可读状态待确认'
  return { ready: '当前可读取', not_ready: '尚未建立资料', unavailable: '资料暂不可用', corrupt: '资料完整性未确认' }[model.material?.state] || '当前可读状态待确认'
})
const selectedFacts = computed(() => model.selection?.completeness?.counts || {})
const factNames = { current_success: '当前成功片', no_speech: '空语音成功', audio_cleaned: '原片已清理',
  projection_pending: '投影待恢复', incomplete: '未完成', failed: '失败', bad_intent: '意图未核准', blocked: '受阻', legacy_unknown: '旧来源未核验' }
const statusLabels = { available: '可读取', no_speech: '空语音成功', unavailable: '暂无规范正文', empty: '未观察到此投影正文', unsupported_tag: '旧共享格式不支持此房间标识',
  unknown: '完整性未知', partial: '仅部分材料', current_at_build: '整理时已核准' }
const groupInfo = (group) => group.view === form.view && group.view_info ? group.view_info : {}
const count = (value) => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '未知'
const shortVersion = (value) => value ? value.slice(0, 10) + '…' : '待确认'
const pretty = (value) => JSON.stringify(value, null, 2)
function dateTime(value) {
  return typeof value === 'number' && Number.isFinite(value)
    ? new Date(value * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '时点未知'
}
function recordText(row) {
  for (const key of ['text', 'title', 'content']) if (typeof row[key] === 'string') return row[key]
  return JSON.stringify(Object.fromEntries(Object.entries(row).filter(([key]) => !['source', 'completeness', 'redaction'].includes(key))))
}
async function selectGroup(group) {
  form.room = group.tag; form.date = group.date; form.platform = group.platform
  await nextTick()
  controller.selectGroup(group)
  void controller.readPage(false)
}
function downloadPage() {
  if (!canRead.value || !model.page || model.rowsReading) return
  const file = materialsPageExport(model.page)
  const url = URL.createObjectURL(new Blob([file.text], { type: 'application/x-ndjson;charset=utf-8' }))
  urls.add(url)
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = file.filename; document.body.append(anchor); anchor.click(); anchor.remove()
  const timer = setTimeout(() => { URL.revokeObjectURL(url); urls.delete(url); timers.delete(timer) }, 0)
  timers.add(timer)
}
watch(form, (value) => controller.editFilters(value), { deep: true, flush: 'sync' })
watch(() => [board.accessMode, board.accessState, board.conn], ([mode, access, connection]) => {
  if (mode === 'local' && access === 'ready' && connection === 'open') controller.connect()
  else controller.disconnect()
}, { immediate: true, flush: 'sync' })
onMounted(() => { poll = setInterval(() => { if (controller.needsPoll() && !model.statusReading) void controller.readStatus(false) }, 2000) })
onUnmounted(() => {
  clearInterval(poll); controller.dispose()
  for (const timer of timers) clearTimeout(timer)
  for (const url of urls) URL.revokeObjectURL(url)
  timers.clear(); urls.clear()
})
</script>

<template>
  <section class="card materials" data-testid="local-materials">
    <div class="materials-heading">
      <div><h3>本地材料</h3><p class="hint">独立发现已有房间与北京时间日材料。日期不是场次，房间 tag 不是说话人；没有说话人分离。</p></div>
      <div class="actions">
        <button class="btn primary" :disabled="!canUpdate" @click="controller.refresh()" data-testid="materials-update">{{ model.posting ? '等待更新回执…' : model.requestState === 'unknown' ? '更新资料（新请求）' : '更新资料' }}</button>
        <button class="btn" :disabled="!model.connected || model.statusReading" @click="controller.readStatus(true)" data-testid="materials-status-read">{{ model.statusReading ? '读取状态中…' : '只读核对状态' }}</button>
      </div>
    </div>
    <p class="hint">更新资料只在手动点击时整理，不调用 LLM。达到整理容量会保留已有发布版；读取、翻页和下载不会发起更新。</p>
    <div class="status-grid">
      <div class="summary" data-testid="materials-request-result">
        <b>本次更新结果</b><p>{{ resultLabel }}</p>
        <p v-if="model.receipt?.version">本次回执版本 {{ shortVersion(model.receipt.version) }}</p>
        <p v-if="model.receipt">开始 {{ dateTime(model.receipt.started_at) }} · 结束 {{ dateTime(model.receipt.finished_at) }}</p>
        <p v-if="model.receipt?.code" class="notice">{{ materialsReason(model.receipt.code) }}</p>
        <p v-if="model.receipt?.warning" class="notice">{{ materialsReason(model.receipt.warning) }}</p>
        <p v-if="model.requestState === 'unknown'" class="hint">服务换代或回执超出保留范围时不能推断成败。重连只查回执；若当前明确空闲，再点击会创建独立新请求。</p>
        <p v-if="model.notice" class="notice" data-testid="materials-action-notice">{{ model.notice }}</p>
      </div>
      <div class="summary" data-testid="materials-current">
        <b>当前材料</b><p>{{ materialLabel }}</p>
        <template v-if="model.material?.version">
          <p>{{ model.statusFresh ? '可读版本' : '上次可读版本' }} {{ shortVersion(model.material.version) }} · 整理 {{ dateTime(model.material.generated_at) }}</p>
        </template>
        <p v-if="model.activeId" class="notice">有一轮更新仍在执行，旧版资料可独立读取；客户端等待结束不表示服务端已停止。</p>
        <p v-if="model.stopping" class="notice">应用正在停止，不再接纳更新；已有更新自然收尾。</p>
        <p v-if="model.material?.refresh?.snapshot_stale" class="notice">当前资料为旧版或整理回执待确认，原始数据未重新核验。</p>
        <p v-if="model.material?.refresh?.error" class="notice">{{ materialsReason(model.material.refresh.error) }}</p>
      </div>
    </div>
    <p v-if="model.statusError" class="notice" data-testid="materials-status-error">{{ model.statusError }}</p>
    <p v-if="model.storageWarning" class="notice">{{ model.storageWarning }}</p>
    <p v-if="!model.connected" class="notice">本地连接待确认。重新连接后只读找回回执，不会重放更新。</p>

    <div class="view-line">
      <label>材料视图 <select v-model="form.view" class="field" data-testid="materials-view"><option value="canonical">规范转写原文</option><option value="shared">六流共享投影</option></select></label>
      <span class="hint">{{ form.view === 'canonical' ? '原文不遮罩；时点来自 writer 墙钟与片内偏移，存在识别和时钟误差。' : '旧六流字段投影，替换 URL / 邮箱 / 电话；原始行和实际替换次数未知，不是全字段原文。' }}</span>
    </div>
    <div class="group-heading"><b>材料分组 · 本页 {{ model.groups?.items.length ?? 0 }} 组</b>
      <button class="btn ghost" :disabled="!canRead || model.groupsReading" @click="controller.readGroups(false)" data-testid="materials-groups-first">{{ model.groupsReading ? '读取分组中…' : '从头读取分组' }}</button>
      <button class="btn" :disabled="!canRead || model.groupsReading || !model.groups?.next_cursor" @click="controller.readGroups(true)" data-testid="materials-groups-next">下一页分组</button>
    </div>
    <p class="hint">分组独立于下方记录筛选和旧统计日期列表；这一页的房间、日期不是完整名册。</p>
    <p v-if="model.groupsError" class="notice" data-testid="materials-groups-error">{{ model.groupsError }}</p>
    <div v-if="model.groups" class="groups" data-testid="materials-groups">
      <button v-for="group in model.groups.items" :key="group.id" class="group" @click="selectGroup(group)" :data-group="group.id">
        <b>{{ group.tag }} · {{ group.date }}</b><span>{{ group.platform }} · {{ count(groupInfo(group).records) }} 条 · {{ statusLabels[groupInfo(group).status] || '状态待确认' }}</span>
      </button>
      <p v-if="!model.groups.items.length" class="hint">本页未返回分组，不等于没有开播。</p>
    </div>
    <p v-if="model.groups?.next_cursor" class="notice" data-testid="materials-groups-more">还有材料分组，可继续下一页；空页也可能尚未读完。</p>
    <div v-if="model.selection" class="selection" data-testid="materials-selected-facts">
      <b>{{ model.selection.tag }} · {{ model.selection.date }}</b><span> · {{ statusLabels[model.selection.completeness?.state] || '完整性待确认' }}</span>
      <p class="hint"><span v-for="(label, key) in factNames" :key="key">{{ label }} {{ count(selectedFacts[key]) }} · </span>观察到的材料不证明全程采集完整。</p>
      <details><summary>查看分组来源与片段事实</summary><pre>{{ pretty({ source: model.selection.source, completeness: model.selection.completeness }) }}</pre></details>
    </div>

    <form class="filters" @submit.prevent="controller.readPage(false)">
      <label>房间 <input v-model="form.room" class="field" maxlength="128" placeholder="全部房间" data-testid="materials-room"></label>
      <label>北京时间日 <input v-model="form.date" class="field" type="date" data-testid="materials-date"></label>
      <label>平台 <select v-model="form.platform" class="field" data-testid="materials-platform"><option value="">全部</option><option value="douyin">抖音</option><option value="bilibili">哔哩哔哩</option></select></label>
      <label>记录流 <select v-model="form.stream" class="field" data-testid="materials-stream"><option value="">全部适用流</option><option value="speech">转写</option><option value="watch">巡检</option><option value="room_meta">房间资料</option><option value="stats">统计</option><option value="danmu">弹幕</option><option value="events">事件</option></select></label>
      <label>从（北京时间，含）<input v-model="form.from" class="field" type="datetime-local" data-testid="materials-from"></label>
      <label>到（北京时间，不含）<input v-model="form.until" class="field" type="datetime-local" data-testid="materials-until"></label>
      <button class="btn" :disabled="!canRead || model.rowsReading || !!model.filterError" data-testid="materials-query">{{ model.rowsReading ? '读取正文中…' : '从头读取记录' }}</button>
    </form>
    <p v-if="model.filterError || model.rowsError" class="notice" data-testid="materials-query-error">{{ model.filterError || model.rowsError }}</p>
    <div class="page-heading"><span class="hint" data-testid="materials-page-meta">{{ model.page ? '本页 ' + model.page.items.length + ' 条 · 版本 ' + shortVersion(model.page.version) + ' · 稳定文件/字节序' : '尚未读取正文页' }}</span>
      <button class="btn" :disabled="!canRead || model.rowsReading || !model.page?.next_cursor" @click="controller.readPage(true)" data-testid="materials-query-next">继续读取下一页</button>
      <button class="btn" :disabled="!canRead || model.rowsReading || !model.page" @click="downloadPage" data-testid="materials-download">导出本页 {{ model.page?.items.length ?? 0 }} 条 JSONL</button>
    </div>
    <p v-if="model.page?.next_cursor" class="notice" data-testid="materials-page-more">本页未读完筛选结果，可继续；空正文页也不表示结束。</p>
    <div v-if="model.page" class="records" data-testid="materials-records">
      <article v-for="(row, index) in model.page.items" :key="index" class="record">
        <div class="hint">{{ row.tag }} · {{ row.stream }} · {{ dateTime(row.ts) }}</div><p class="record-text">{{ recordText(row) }}</p>
        <details><summary>来源、完整性与遮罩说明</summary><pre>{{ pretty({ source: row.source, completeness: row.completeness, view: row.view, redaction: row.redaction }) }}</pre></details>
      </article>
      <p v-if="!model.page.items.length" class="hint" data-testid="materials-empty-page">本页没有正文。请结合分组事实区分空语音成功、未完成或未观察到；有续页标识仍可继续。</p>
    </div>
    <p class="hint">只导出已经返回的这一页及版本、过滤范围、来源元信息；不遍历后页，不是整天或整场全量。原始数据新鲜度仅反映整理时点。</p>
  </section>
</template>

<style scoped>
.materials { margin-bottom: 20px; min-width: 0; }
.materials h3 { margin: 0; }
.materials-heading,.actions,.view-line,.group-heading,.page-heading { display:flex; flex-wrap:wrap; align-items:center; gap:10px; }
.materials-heading { justify-content:space-between; align-items:flex-start; }
.hint { color:var(--dim); font-size:12px; line-height:1.7; margin:6px 0; }
.notice { color:var(--amber); font-size:12px; line-height:1.8; margin:7px 0; overflow-wrap:anywhere; }
.status-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin:12px 0; }
.summary,.selection { border:1px solid var(--line-soft); border-radius:8px; padding:12px; font-size:13px; overflow-wrap:anywhere; }
.summary p { margin:6px 0; line-height:1.7; }
.view-line { margin-top:16px; }
.group-heading { margin-top:14px; font-size:13px; }
.groups { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px; margin:10px 0; max-height:220px; overflow:auto; }
.group { text-align:left; background:var(--panel-2); color:var(--ink); border:1px solid var(--line); border-radius:7px; padding:10px; cursor:pointer; overflow-wrap:anywhere; }
.group span { display:block; margin-top:5px; font-size:12px; color:var(--dim); }
.group:hover { border-color:var(--blue); }
.filters { display:flex; flex-wrap:wrap; align-items:flex-end; gap:10px; margin:16px 0 10px; }
.filters label,.view-line label { display:flex; flex-direction:column; gap:5px; font-size:12px; color:var(--dim); }
.filters .field { max-width:220px; min-width:140px; }
.page-heading { justify-content:space-between; margin-top:10px; }
.records { max-height:430px; overflow:auto; margin-top:10px; border-top:1px solid var(--line-soft); }
.record { padding:12px 2px; border-bottom:1px solid var(--line-soft); }
.record-text { white-space:pre-wrap; overflow-wrap:anywhere; line-height:1.8; margin:6px 0; font-size:13px; }
details { font-size:12px; color:var(--dim); margin-top:8px; }
summary { cursor:pointer; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; color:var(--ink-2); line-height:1.7; max-height:260px; overflow:auto; }
@media(max-width:700px) { .status-grid { grid-template-columns:1fr; } .filters label { width:100%; } .filters .field { max-width:100%; width:100%; } }
</style>
