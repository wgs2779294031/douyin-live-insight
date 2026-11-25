<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../store'
import { fmtTime } from '../utils'

const data = ref(null)
const loading = ref(false)
const err = ref('')
const busy = ref('')            // 正在操作的 tag
const confirmDel = ref('')      // 二次确认中的 tag
const showAdd = ref(false)
const addErr = ref('')
const added = ref(null)
const form = ref({ tag: '', name: '', sec_uid: '', watch: true, record: false, llm: false })
const automaticUncertain = ref({})
const snapshotCurrent = ref(false)

let timer = null
let loadSequence = 0

async function load(quiet) {
  const sequence = ++loadSequence
  snapshotCurrent.value = false
  if (!quiet) loading.value = true
  try {
    const result = await api.anchors()
    if (sequence !== loadSequence) return false
    data.value = result
    automaticUncertain.value = {}
    snapshotCurrent.value = true
    err.value = ''
    llmHint.value = ''
    return true
  } catch (e) {
    if (sequence === loadSequence) {
      snapshotCurrent.value = false
      err.value = e.message || '加载失败'
    }
    return false
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}

onMounted(() => {
  load()
  timer = setInterval(() => { if (!busy.value) load(true) }, 15000)
})
onUnmounted(() => clearInterval(timer))

const health = computed(() => (data.value && data.value.health) || {})
const rows = computed(() => (data.value && data.value.anchors) || [])

const stateText = { ok: '正常', stalled: '采集停写', stopped: '未运行', silent: '弹幕静默', disabled: '已停用', unknown: '待探测' }
const stateTone = { ok: 'on', stalled: 'off', stopped: 'off', silent: 'wait', disabled: '', unknown: 'wait' }

function ageText(sec) {
  if (sec == null) return '—'
  if (sec < 90) return sec + ' 秒'
  if (sec < 5400) return Math.round(sec / 60) + ' 分钟'
  return (sec / 3600).toFixed(1) + ' 小时'
}

async function act(fn, tag) {
  if (busy.value) return
  busy.value = tag
  ++loadSequence
  loading.value = false
  snapshotCurrent.value = false
  err.value = ''
  try {
    await fn()
    await load(true)
  } catch (e) {
    err.value = e.message || '操作失败'
  } finally {
    busy.value = ''
  }
}

const doStart = (t) => act(() => api.anchorStart(t, 'all'), t)
const doStop = (t) => act(() => api.anchorStop(t, 'all', true), t)
// 重启：只杀进程再拉起，不动名册 enabled（重启 ≠ 停用）
const doRestart = (t) => act(() => api.anchorRestart(t), t)
const doToggleWatch = (a) => act(() => api.anchorUpdate(a.tag, { watch: !a.watch, record: a.record }), a.tag)
const doToggleRecord = (a) => act(() => api.anchorUpdate(a.tag, { record: !a.record, watch: a.watch }), a.tag)

// Intent and actual runtime evidence are separate. The old CLI flag is not evidence.
const automaticKnown = (a) => typeof a.automatic_analysis?.enabled === 'boolean'
const automaticOn = (a) => a.automatic_analysis?.enabled === true
const statusConfirmed = (a) => snapshotCurrent.value && !automaticUncertain.value[a.tag]
const workerLabels = {
  running: '正在处理实况事件', waiting_input: '已就绪，等待新事件', stopping: '正在停止自动实况',
  waiting_permission: '等待内置AI许可', no_key: '等待配置可用密钥', waiting_collector: '等待采集进程',
  waiting_live: '等待开播', stopped: '自动实况未运行', error: '自动实况状态异常', unknown: '实际运行状态待确认'
}
const workerReasons = {
  global_paused: '内置AI总许可已暂停', control_unavailable: '内置AI许可状态待确认',
  automatic_disabled: '未开启此主播自动实况', auto_disabled: '未开启此主播自动实况',
  missing_key: '没有可用模型密钥', no_key: '没有可用模型密钥'
}
function workerText(a) {
  if (!statusConfirmed(a)) return workerLabels.unknown
  const state = a.worker_state?.state
  return typeof workerLabels[state] === 'string' ? workerLabels[state] : workerLabels.unknown
}
function workerReason(a) {
  if (!statusConfirmed(a)) return '上次回执已失效，请刷新确认'
  return [a.worker_state?.reason, a.automatic_analysis?.reason]
    .map((reason) => workerReasons[reason]).find((reason) => typeof reason === 'string') || ''
}
const llmHint = ref('')
async function doToggleLlm(a) {
  if (busy.value) return
  const want = !automaticOn(a)
  busy.value = a.tag
  ++loadSequence
  loading.value = false
  snapshotCurrent.value = false
  err.value = ''
  llmHint.value = ''
  try {
    await api.anchorUpdate(a.tag, { llm: want })
    const refreshed = await load(true)
    const current = rows.value.find((item) => item.tag === a.tag)
    if (refreshed && current && automaticKnown(current) && automaticOn(current) === want) {
      llmHint.value = `已保存自动实况分析设置：${want ? '开' : '关'}。实际运行状态见该主播状态栏。`
    } else {
      automaticUncertain.value = { ...automaticUncertain.value, [a.tag]: true }
      llmHint.value = '自动实况设置尚未确认，请刷新核实；不能据此判断已经运行。'
    }
  } catch (_) {
    automaticUncertain.value = { ...automaticUncertain.value, [a.tag]: true }
    llmHint.value = '自动实况设置未确认，请刷新核实。'
  } finally {
    busy.value = ''
  }
}

async function doDelete(tag) {
  await act(() => api.anchorDelete(tag), tag)
  confirmDel.value = ''
}

async function doAdd() {
  if (busy.value) return
  addErr.value = ''
  added.value = null
  const body = { ...form.value }
  body.name = body.name || body.tag
  busy.value = body.tag || '__new_anchor__'
  ++loadSequence
  loading.value = false
  snapshotCurrent.value = false
  try {
    const result = await api.anchorAdd(body)
    added.value = result.anchor
    showAdd.value = false
    form.value = { tag: '', name: '', sec_uid: '', watch: true, record: false, llm: false }
    await load(true)
  } catch (e) {
    addErr.value = e.message || '添加失败'
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="banner">
    “自动实况分析”只控制该主播直播期间的自动调用，默认关闭，与内置AI总许可分别控制。
    实际运行状态来自最近一次采集回执；开启意图不代表分析已经运行。
  </div>

  <div class="grid-kpi" style="margin-bottom: 14px">
    <div class="card kpi">
      <div class="k">名册总数</div>
      <div class="v">{{ health.total ?? '—' }}</div>
      <div class="s">其中启用 {{ health.enabled ?? '—' }}</div>
    </div>
    <div class="card kpi">
      <div class="k">采集正常</div>
      <div class="v green">{{ health.ok ?? '—' }}</div>
      <div class="s">进程在 + 探测在写</div>
    </div>
    <div class="card kpi" :class="{ warn: (health.stalled || 0) + (health.stopped || 0) > 0 }">
      <div class="k">采集异常</div>
      <div class="v">{{ (health.stalled || 0) + (health.stopped || 0) }}</div>
      <div class="s">停写 {{ health.stalled || 0 }} · 未运行 {{ health.stopped || 0 }}</div>
    </div>
    <div class="card kpi">
      <div class="k">弹幕静默</div>
      <div class="v">{{ health.silent || 0 }}</div>
      <div class="s">在播但 10 分钟无弹幕</div>
    </div>
  </div>

  <div class="toolbar">
    <button class="btn" :disabled="loading || !!busy" @click="load()" data-testid="refresh-anchors">{{ loading ? '刷新中…' : '刷新' }}</button>
    <button class="btn primary" :disabled="!!busy" @click="showAdd = !showAdd; added = null" data-testid="show-add-anchor">{{ showAdd ? '收起' : '+ 新增主播' }}</button>
    <span class="meta" style="font-size: 12px; color: var(--dim)">每 15 秒自动刷新</span>
  </div>

  <div v-if="err" class="banner err">{{ err }}</div>
  <div v-if="added" class="banner" role="status" style="overflow-wrap: anywhere">已保存「{{ added.tag }}」，账号标识 <code>{{ added.sec_uid }}</code></div>

  <div v-if="showAdd" class="card" style="margin-bottom: 14px">
    <h3>新增主播<span class="sub">tag 是主键，建了不能改名（文件名/pidfile 全按它）</span></h3>
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px">
      <div>
        <label style="font-size: 12px; color: var(--dim)">tag（中文标签，唯一）</label>
        <input v-model="form.tag" class="field" style="width: 100%; margin-top: 4px" placeholder="例：某某老师讲就业" />
      </div>
      <div>
        <label style="font-size: 12px; color: var(--dim)">显示名（可留空）</label>
        <input v-model="form.name" class="field" style="width: 100%; margin-top: 4px" />
      </div>
      <div style="grid-column: span 2">
        <label for="anchor-profile" style="font-size: 12px; color: var(--dim)">抖音个人主页或 sec_uid</label>
        <textarea id="anchor-profile" v-model="form.sec_uid" class="field" rows="2" style="width: 100%; margin-top: 4px" spellcheck="false"
          placeholder="粘贴主页长链接、含主页长链接的分享文字，或 MS4wLjABAAAA 开头的账号标识" />
      </div>
    </div>
    <div style="display: flex; gap: 18px; margin-top: 12px; font-size: 13px; color: var(--ink-2)">
      <label><input v-model="form.watch" type="checkbox" /> 采集弹幕/统计</label>
      <label><input v-model="form.record" type="checkbox" /> 录音+转写</label>
      <label><input v-model="form.llm" type="checkbox" data-testid="new-anchor-automatic" /> 自动实况分析（直播期间可能产生费用）</label>
    </div>
    <div v-if="addErr" style="color: var(--red); font-size: 12px; margin-top: 10px">{{ addErr }}</div>
    <div style="margin-top: 12px">
      <button class="btn primary" :disabled="!!busy" @click="doAdd">保存到名册</button>
      <span style="font-size: 12px; color: var(--dim-2); margin-left: 10px">保存后点该行「启动」才真正拉起采集</span>
    </div>
  </div>

  <div class="card">
    <h3>名册<span class="sub">anchors.json · watch 与 recorder 共用的唯一真相源</span></h3>
    <div class="roster-scroll">
    <table style="width: 100%; border-collapse: collapse; font-size: 13px">
      <thead>
        <tr style="color: var(--dim); font-size: 12px; text-align: left">
          <th style="padding: 8px">主播</th>
          <th style="padding: 8px">状态</th>
          <th style="padding: 8px">采集</th>
          <th style="padding: 8px">录音</th>
          <th style="padding: 8px; min-width: 185px">自动实况分析</th>
          <th style="padding: 8px">探测</th>
          <th style="padding: 8px">开播</th>
          <th style="padding: 8px; text-align: right">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="a in rows" :key="a.tag" :data-tag="a.tag" style="border-top: 1px solid var(--line-soft)">
          <td style="padding: 8px">
            <div>{{ a.tag }}</div>
            <div v-if="a.name !== a.tag" style="font-size: 11px; color: var(--dim-2)">{{ a.name }}</div>
          </td>
          <td style="padding: 8px">
            <span class="dot" :class="stateTone[a.state]"></span>
            <span style="font-size: 12px; margin-left: 6px; color: var(--ink-2)">{{ stateText[a.state] || a.state }}</span>
          </td>
          <td style="padding: 8px">
            <button class="btn ghost" style="padding: 2px 8px; font-size: 12px" :disabled="!!busy" @click="doToggleWatch(a)">
              {{ a.watch ? '开' : '关' }}
            </button>
          </td>
          <td style="padding: 8px">
            <button class="btn ghost" style="padding: 2px 8px; font-size: 12px" :disabled="!!busy" @click="doToggleRecord(a)">
              {{ a.record ? '开' : '关' }}
            </button>
          </td>
          <td style="padding: 8px">
            <button
              class="btn ghost"
              style="padding: 2px 8px; font-size: 12px"
              :disabled="!!busy"
              title="只设置直播期间的自动实况分析，不改变总许可，也不阻止人工复盘或数据读取"
              @click="doToggleLlm(a)"
              data-testid="automatic-analysis-toggle"
            >
              {{ busy === a.tag ? '处理中…' : (!statusConfirmed(a) || !automaticKnown(a) ? '待确认' : (automaticOn(a) ? '开' : '关')) }}
            </button>
            <div v-if="!statusConfirmed(a) && automaticKnown(a)" class="worker-reason" data-testid="automatic-last-confirmed">上次确认：{{ automaticOn(a) ? '开' : '关' }}（当前待确认）</div>
            <div class="worker-state" data-testid="worker-state">{{ workerText(a) }}</div>
            <div v-if="workerReason(a)" class="worker-reason" data-testid="worker-reason">{{ workerReason(a) }}</div>
          </td>
          <td style="padding: 8px; font-size: 12px; color: var(--dim-2)">{{ ageText(a.watch_age_sec) }}前</td>
          <td style="padding: 8px; font-size: 12px">
            <span v-if="a.is_live" style="color: var(--green)">直播中</span>
            <span v-else style="color: var(--dim-2)">—</span>
          </td>
          <td style="padding: 8px; text-align: right; white-space: nowrap">
            <!-- 按钮跟着「进程死活」走，不跟名册 enabled 走。
                 enabled 只代表「开机自启会拉起它」；进程死了照样得有人点一下。
                 2026-10-28 老大在页面上找不到「启动」就是因为这里以前只看 enabled。 -->
            <button
              v-if="!a.watch_running"
              class="btn primary"
              style="padding: 3px 10px; font-size: 12px"
              :disabled="!!busy"
              :title="a.enabled ? '名册是启用的，但进程没在跑' : '会同时把名册 enabled 置 true'"
              @click="doStart(a.tag)"
            >启动</button>
            <button
              v-else
              class="btn ghost"
              style="padding: 3px 10px; font-size: 12px"
              :disabled="!!busy"
              title="杀掉重拉（名册开关不变）"
              @click="doRestart(a.tag)"
            >重启</button>
            <button
              v-if="a.enabled"
              class="btn"
              style="padding: 3px 10px; font-size: 12px; margin-left: 6px"
              :disabled="!!busy"
              :title="a.watch_running ? '停进程，并把名册 enabled 置 false（开机不会自己回来）' : '只把名册 enabled 置 false（进程本来就没跑）'"
              @click="doStop(a.tag)"
            >{{ a.watch_running ? '停止' : '停用' }}</button>
            <template v-if="confirmDel === a.tag">
              <button class="btn" style="padding: 3px 10px; font-size: 12px; margin-left: 6px; color: var(--red); border-color: var(--red)" @click="doDelete(a.tag)">确认删除</button>
              <button class="btn ghost" style="padding: 3px 10px; font-size: 12px" @click="confirmDel = ''">取消</button>
            </template>
            <button v-else class="btn ghost" style="padding: 3px 8px; font-size: 12px; margin-left: 6px" @click="confirmDel = a.tag">删除</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="8" style="padding: 20px; text-align: center; color: var(--dim-2)">名册为空</td>
        </tr>
      </tbody>
    </table>
    </div>
    <div style="margin-top: 10px; font-size: 11px; color: var(--dim-2)">
      删除只出名册 + 停进程，<b>data/ 下的历史 jsonl 一律保留</b>。tag 不可改名。
      <br />
      关闭<b>自动实况分析</b>只暂停此主播直播期间的自动调用，不阻止人工复盘和智能体读取数据。
      人工发起内置分析仍受设置页的内置AI总许可控制；已发送请求可按原规则收尾。
    </div>
    <div v-if="llmHint" style="margin-top: 8px; font-size: 12px; color: var(--blue)">{{ llmHint }}</div>
  </div>
</template>

<style scoped>
.roster-scroll { overflow-x: auto; }
.worker-state { margin-top: 5px; font-size: 12px; color: var(--ink-2); line-height: 1.6; }
.worker-reason { margin-top: 2px; font-size: 11px; color: var(--amber); line-height: 1.6; }
</style>
