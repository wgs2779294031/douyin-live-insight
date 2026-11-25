<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../store'

const snapshot = ref(null)
const fresh = ref(false)
const targets = ref([])
const targetsKnown = ref(false)
const selected = ref('')
const reading = ref(false)
const busyAction = ref('')
const hint = ref('')
const clock = ref(Date.now() / 1000)
const receivedAt = ref(null)
let sequence = 0
let disposed = false
let timer = null

const browserLabels = {
  not_prepared: '尚未准备', starting: '正在启动', ready: '自有浏览器已准备',
  missing: '未找到浏览器', conflict: '浏览器端口冲突', closed: '浏览器已关闭',
  failed: '浏览器准备失败', identity_unknown: '浏览器归属待确认', stopping: '应用正在停止'
}
const channelLabels = {
  not_checked: '尚未检查通道', need_target: '通道待配置主播后检查', page_opened: '登录页已打开，通道仍待检查',
  checking: '正在检查通道', channel_live: '通道可查询 · 目标显示直播',
  channel_offline: '通道可查询 · 目标显示未开播', restricted: '需扫码/验证或环境受限',
  unknown: '通道查询未知', expired: '通道证据已过期，请主动检查'
}
function normalize(response) {
  const value = response?.status
  if (!value || !Object.hasOwn(browserLabels, value.browser?.state)
    || !Object.hasOwn(channelLabels, value.channel?.state) || typeof value.busy !== 'boolean') {
    throw new Error('invalid browser status')
  }
  return {
    browser: { state: value.browser.state },
    channel: {
      state: value.channel.state,
      target: typeof value.channel.target_tag === 'string' && value.channel.target_tag.length <= 40 ? value.channel.target_tag : '',
      checkedAt: typeof value.channel.checked_at === 'number' && Number.isFinite(value.channel.checked_at) && value.channel.checked_at > 0
        ? value.channel.checked_at : null,
      source: value.channel.source === 'owned_browser_profile_api' ? 'owned_browser_profile_api' : 'none'
    },
    busy: value.busy
  }
}
const statusCurrent = computed(() => fresh.value && receivedAt.value
  && receivedAt.value <= clock.value + 5 && clock.value - receivedAt.value <= 60)
const browserText = computed(() => {
  if (!fresh.value || !snapshot.value) return '浏览器状态待确认'
  return statusCurrent.value ? browserLabels[snapshot.value.browser.state] : '浏览器状态读取已过期，请重新读取'
})
const stateReadTime = computed(() => receivedAt.value
  ? new Date(receivedAt.value * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚无成功读取记录')
const targetMatches = computed(() => !!selected.value && targetsKnown.value
  && snapshot.value?.channel.target === selected.value && targets.value.includes(selected.value))
const evidenceCurrent = computed(() => {
  const channel = snapshot.value?.channel
  return statusCurrent.value && channel && targetMatches.value && snapshot.value.browser.state === 'ready'
    && channel.source === 'owned_browser_profile_api' && channel.checkedAt
    && channel.checkedAt <= clock.value + 5 && clock.value - channel.checkedAt <= 60
})
const channelState = computed(() => {
  if (!statusCurrent.value || !snapshot.value) return 'unknown'
  const state = snapshot.value.channel.state
  if (['channel_live', 'channel_offline'].includes(state)) {
    if (!targetMatches.value) return 'not_checked'
    const channel = snapshot.value.channel
    if (snapshot.value.browser.state !== 'ready' || channel.source !== 'owned_browser_profile_api'
      || !channel.checkedAt || channel.checkedAt > clock.value + 5) return 'unknown'
    return evidenceCurrent.value ? state : 'expired'
  }
  if (snapshot.value.channel.target && !targetMatches.value) return 'not_checked'
  return state
})
const channelText = computed(() => channelLabels[channelState.value])
const channelReady = computed(() => evidenceCurrent.value && ['channel_live', 'channel_offline'].includes(channelState.value))
const checkedTime = computed(() => {
  const checked = snapshot.value?.channel.checkedAt
  return fresh.value && targetMatches.value && checked ? new Date(checked * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚无当前目标的检查证据'
})
const actionDisabled = computed(() => !!busyAction.value || reading.value || snapshot.value?.busy
  || snapshot.value?.browser.state === 'stopping')

async function refresh() {
  if (reading.value || busyAction.value) return
  const current = ++sequence
  fresh.value = false
  targetsKnown.value = false
  reading.value = true
  hint.value = ''
  try {
    const [status, roster] = await Promise.allSettled([api.browserLoginStatus(), api.anchors()])
    if (disposed || current !== sequence) return
    if (roster.status === 'fulfilled' && Array.isArray(roster.value?.anchors)) {
      targets.value = [...new Set(roster.value.anchors.map((row) => row?.tag)
        .filter((tag) => typeof tag === 'string' && tag.length > 0 && tag.length <= 40))]
      targetsKnown.value = true
      if (selected.value && !targets.value.includes(selected.value)) selected.value = ''
    } else {
      targets.value = []
      targetsKnown.value = false
      selected.value = ''
    }
    if (status.status !== 'fulfilled') throw new Error('status unavailable')
    snapshot.value = normalize(status.value)
    receivedAt.value = Date.now() / 1000
    clock.value = receivedAt.value
    fresh.value = true
    if (!targetsKnown.value) hint.value = '主播列表尚未确认，请重新读取状态；仍可明确准备或打开登录页。'
  } catch (_) {
    if (!disposed && current === sequence) {
      snapshot.value = null
      hint.value = '浏览器状态读取失败，当前状态待确认。请重新读取；不会自动打开页面或检查平台。'
    }
  } finally {
    if (!disposed && current === sequence) reading.value = false
  }
}

async function act(action) {
  if (actionDisabled.value || (action === 'check' && !selected.value)) return
  const current = ++sequence
  const target = selected.value
  fresh.value = false
  busyAction.value = action
  hint.value = ''
  try {
    const response = await api.browserLoginAction(action, target)
    if (disposed || current !== sequence || (action === 'check' && selected.value !== target)) return
    snapshot.value = normalize(response)
    receivedAt.value = Date.now() / 1000
    clock.value = receivedAt.value
    fresh.value = true
    if (response.ok !== true) hint.value = '本次操作未确认完成，请核对当前状态后再明确操作；不会自动重试。'
  } catch (_) {
    if (!disposed && current === sequence) {
      snapshot.value = null
      hint.value = '操作结果待确认。请重新读取状态后再决定下一步；不会自动重试或继续检查。'
    }
  } finally {
    if (!disposed) busyAction.value = ''
  }
}

watch(selected, () => {
  ++sequence
  fresh.value = false
  reading.value = false
  hint.value = '已切换检查目标，请明确点击“检查所选主播通道”。'
})
onMounted(() => {
  refresh() // Safe state and roster reads only. Never opens a page or checks a platform.
  timer = setInterval(() => { clock.value = Date.now() / 1000 }, 1000)
})
onUnmounted(() => { disposed = true; ++sequence; clearInterval(timer) })
</script>

<template>
  <section class="card login-guide" data-testid="browser-login-guide">
    <h3>采集浏览器与平台登录<span class="sub">本机私有浏览器</span></h3>
    <p class="intro">请在打开的平台页面自行扫码或验证。登录态保存在当前应用的私有浏览器中，不需要模型密钥。</p>
    <div class="status-row">
      <span class="dot" data-testid="login-browser-dot" :class="statusCurrent && snapshot?.browser.state === 'ready' ? 'on' : 'wait'"></span>
      <strong data-testid="login-browser-state">{{ browserText }}</strong>
    </div>
    <p class="detail">浏览器准备只确认进程与通道归属，不代表平台账号有效或采集已经恢复。</p>
    <p class="detail" data-testid="login-state-read-time">最近成功读取：{{ stateReadTime }}。状态超过60秒需主动重新读取。</p>
    <div class="actions">
      <button class="btn" data-testid="login-prepare" :disabled="actionDisabled" @click="act('prepare')">{{ busyAction === 'prepare' ? '正在准备…' : '准备浏览器' }}</button>
      <button class="btn primary" data-testid="login-open" :disabled="actionDisabled" @click="act('open')">{{ busyAction === 'open' ? '正在打开…' : '打开抖音登录页' }}</button>
      <button class="btn ghost" data-testid="login-refresh" :disabled="reading || !!busyAction" @click="refresh">{{ reading ? '读取中…' : '重新读取状态' }}</button>
    </div>
    <label for="login-target">单次检查目标</label>
    <select id="login-target" class="field" v-model="selected" data-testid="login-target">
      <option value="">请选择已登记的主播</option>
      <option v-for="tag in targets" :key="tag" :value="tag">{{ tag }}</option>
    </select>
    <p v-if="targetsKnown && !targets.length" class="detail" data-testid="login-empty-roster">尚未配置主播：可先打开登录页，通道及采集尚待主播配置检查。可前往“主播”页添加自己的目标。</p>
    <p v-else-if="!targetsKnown" class="detail">主播列表尚未确认，暂不检查平台。</p>
    <div class="actions">
      <button class="btn" data-testid="login-check" :disabled="actionDisabled || !selected || !targetsKnown" @click="act('check')">{{ busyAction === 'check' ? '正在检查…' : '检查所选主播通道' }}</button>
    </div>
    <div class="status-row" data-testid="login-channel-status" :data-current="channelReady ? 'true' : 'false'">
      <span class="dot" :class="channelReady ? 'on' : 'wait'"></span>
      <strong>{{ channelText }}</strong>
    </div>
    <p class="detail">检查目标：{{ selected || '未选择' }} · {{ checkedTime }}</p>
    <p class="detail">证据来源：{{ fresh && targetMatches && snapshot?.channel.source === 'owned_browser_profile_api' ? '受管浏览器资料通道的单次响应' : '尚待确认' }}。通道可查询不等于账号已登录，也不等于采集恢复。</p>
    <p class="detail">操作不会开启被关闭的采集、录音、转写或自动AI。刷新、重连只读取状态，页面打开和通道检查均需明确点击。</p>
    <p v-if="hint" class="notice" data-testid="login-hint">{{ hint }}</p>
  </section>
</template>

<style scoped>
.login-guide { align-self: start; }
.intro, .detail, .notice { font-size: 12px; line-height: 1.7; }
.intro { color: var(--dim); margin: 6px 0 12px; }
.detail { color: var(--dim-2); margin: 7px 0; }
.notice { color: var(--amber); margin-top: 10px; }
.status-row { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-top: 10px; }
.actions { display: flex; flex-wrap: wrap; gap: 7px; margin: 12px 0; }
label { display: block; color: var(--dim); font-size: 12px; margin-bottom: 6px; }
select { width: 100%; }
</style>
