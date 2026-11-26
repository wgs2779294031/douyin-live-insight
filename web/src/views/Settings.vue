<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { api, useBoard, usageDisplay } from '../store'
import BrowserLoginGuide from '../components/BrowserLoginGuide.vue'
import AsrStatusCard from '../components/AsrStatusCard.vue'
import FeishuNotifications from '../components/FeishuNotifications.vue'

const board = useBoard()
const llm = ref(null)
const err = ref('')
const llmBusy = ref(false)
const llmHint = ref('')
const chosen = ref('')
const controlUncertain = ref(false)
const controlKnown = computed(() => !controlUncertain.value
  && typeof llm.value?.control?.allowed === 'boolean'
  && llm.value?.control?.reason !== 'control_unavailable')
const globalAllowed = computed(() => controlKnown.value && llm.value.control.allowed === true)
const dailyUsage = computed(() => usageDisplay(llm.value?.today?.scope?.kind === 'global' ? llm.value.today : null))
const providerRecords = (provider) => dailyUsage.value.providers?.find((row) => row.provider === provider.name && row.model === provider.model)?.records || '未记录'
const globalControlBusy = computed(() => llmBusy.value
  || (!!providerBusy.value && !(providerBusy.value === 'test' && globalAllowed.value)))
const permissionMessage = computed(() => {
  if (!controlKnown.value || llm.value?.control?.reason === 'control_unavailable') {
    return '许可状态待确认，本页暂不发起连接测试。请重新读取状态，确认其他内置AI调用是否已暂停。'
  }
  return globalAllowed.value
    ? '已允许内置AI调用；是否自动分析由各主播的“自动实况分析”设置决定。'
    : '已暂停新的内置AI调用，包括自动实况、按次分析和连接测试。基础采集、转写和数据读取仍可使用。'
})

const editorProvider = ref('')
const newProvider = ref('')
const providerForm = ref({ protocol: 'openai_compatible', base_url: '', model: '', api_key: '' })
const providerBusy = ref('')
const providerNotice = ref('')
const providerNoticeKind = ref('')
const connectionNotice = ref('')
const connectionKind = ref('')
const deletingKey = ref(false)
const selectedProvider = computed(() => (llm.value?.providers || [])
  .find((item) => item.name === editorProvider.value) || null)
const hasConfiguredKeys = computed(() => (llm.value?.providers || []).some((item) => item.has_key))
const providerId = computed(() => editorProvider.value === '__new__'
  ? newProvider.value.trim() : editorProvider.value)

function resetProviderForm() {
  const item = selectedProvider.value
  providerForm.value = {
    protocol: item?.protocol || (item?.name === 'doubao' ? 'doubao' : 'openai_compatible'),
    base_url: item?.base_url || '', model: item?.model || '', api_key: ''
  }
  newProvider.value = ''
  providerNotice.value = ''
  connectionNotice.value = ''
  deletingKey.value = false
}
watch(editorProvider, resetProviderForm)
watch(() => [providerForm.value.protocol, providerForm.value.base_url, providerForm.value.model,
  providerForm.value.api_key, newProvider.value], () => {
  connectionNotice.value = ''
  deletingKey.value = false
}, { flush: 'sync' })

function providerPayload() {
  const key = providerForm.value.api_key.trim()
  return { provider: providerId.value, protocol: providerForm.value.protocol,
    base_url: providerForm.value.base_url.trim(), model: providerForm.value.model.trim(),
    ...(key ? { api_key: key } : {}) }
}

function validProviderDraft() {
  if (!providerId.value || !providerForm.value.base_url.trim() || !providerForm.value.model.trim()) {
    providerNotice.value = '请填写提供商标识、API 地址和模型名。'
    providerNoticeKind.value = 'error'
    providerForm.value.api_key = ''
    return false
  }
  return true
}

async function reloadLLM() {
  try {
    llm.value = await api.llm()
    controlUncertain.value = typeof llm.value?.control?.allowed !== 'boolean'
    chosen.value = llm.value.active_provider || (llm.value.providers[0] || {}).name || ''
  } catch (error) {
    controlUncertain.value = true
    throw error
  }
}

function describeSave(result, deleting = false) {
  const action = deleting ? '密钥已删除' : '配置已保存'
  if (result?.config_saved && (result.ok === false || Number(result.sync?.failed) > 0)) {
    providerNoticeKind.value = 'warning'
    const failed = Number(result.sync?.failed) || 0
    providerNotice.value = `${action}，监控配置同步尚未全部完成${failed ? `（${failed} 项失败）` : ''}。可再次保存配置以重试同步；连接尚未验证。`
  } else if (result?.ok === true && result.config_saved === true) {
    providerNoticeKind.value = 'success'
    providerNotice.value = deleting ? '密钥已删除。后续模型请求不再使用它，基础采集和转写不受影响。'
      : '配置已保存，连接尚未验证。保存不会开启自动分析。'
  } else {
    providerNoticeKind.value = 'error'
    providerNotice.value = '配置未保存，请检查输入后重试。'
  }
}

function describeConfigError(error) {
  if (error?.result?.config_saved) {
    describeSave(error.result, providerBusy.value === 'delete')
  } else {
    providerNoticeKind.value = 'error'
    providerNotice.value = error?.status === 400 ? '配置未保存，请检查提供商、API 地址、协议和模型名。'
      : error?.status === 403 ? (board.accessMode === 'local' ? '本地会话已失效，请重新连接后核对配置。' : '当前无权修改配置，请确认登录权限。')
        : '未能完成配置操作，请检查本地配置状态后重试。'
  }
}

async function saveProvider() {
  if (providerBusy.value || !validProviderDraft()) return
  providerBusy.value = 'save'
  providerNotice.value = ''
  connectionNotice.value = ''
  deletingKey.value = false
  const payload = providerPayload()
  providerForm.value.api_key = ''
  try {
    const result = await api.llmSettings(payload)
    describeSave(result)
    if (result.config_saved) {
      const id = payload.provider
      try {
        await reloadLLM()
        if (editorProvider.value === '__new__') {
          editorProvider.value = id
          // Selection watcher clears the old draft; retain the outcome below.
          await Promise.resolve()
          describeSave(result)
        }
      } catch (_) {
        providerNoticeKind.value = 'warning'
        providerNotice.value = '配置已保存，但状态刷新失败；请刷新页面确认。连接尚未验证。'
      }
    }
  } catch (error) {
    describeConfigError(error)
    if (error?.result?.config_saved) {
      try { await reloadLLM() } catch (_) { /* Keep the accurate partial-save notice. */ }
    }
  } finally {
    providerForm.value.api_key = ''
    providerBusy.value = ''
  }
}

async function deleteProviderKey() {
  if (providerBusy.value || !selectedProvider.value?.has_key) return
  if (!deletingKey.value) {
    providerForm.value.api_key = ''
    deletingKey.value = true
    return
  }
  providerBusy.value = 'delete'
  providerNotice.value = ''
  connectionNotice.value = ''
  providerForm.value.api_key = ''
  try {
    const result = await api.llmDeleteKey(providerId.value)
    describeSave(result, true)
    if (result.config_saved) {
      try { await reloadLLM() } catch (_) {
        providerNoticeKind.value = 'warning'
        providerNotice.value = '密钥删除已保存，但状态刷新失败；请刷新页面确认。'
      }
    }
  } catch (error) {
    describeConfigError(error)
    if (error?.result?.config_saved) {
      try { await reloadLLM() } catch (_) { /* Keep the partial-delete notice. */ }
    }
  } finally {
    deletingKey.value = false
    providerForm.value.api_key = ''
    providerBusy.value = ''
  }
}

const connectionMessages = {
  ok: '连接验证通过。仅验证本次填写的配置，未保存，也未开启自动分析。',
  missing_key: '没有可用密钥。请填写本次测试密钥，或先保存一份密钥。',
  invalid_config: '配置不完整或格式不正确，请检查 API 地址、协议和模型名。',
  auth_failed: '服务商拒绝了密钥，请检查密钥是否有效及接口权限。',
  rate_limited: '服务商暂时限流，请稍后再试。',
  model_not_found: '接口未找到所填模型，请核对模型名和 API 地址。',
  timeout: '连接测试超时，请检查地址和网络后再试。',
  network_error: '无法连接服务商，请检查地址和网络。',
  busy: '已有连接测试正在进行，请等待它完成。',
  global_paused: '内置AI调用已暂停，未发起新的连接测试。可在上方明确允许后再试。',
  control_unavailable: '内置AI许可状态待确认，未发起新的连接测试。请重新读取状态。',
  upstream_error: '服务商未返回可用结果，请检查模型配置或稍后再试。'
}

async function testProvider() {
  if (providerBusy.value || llmBusy.value) return
  if (!globalAllowed.value) {
    providerForm.value.api_key = ''
    connectionKind.value = 'warning'
    connectionNotice.value = controlKnown.value ? connectionMessages.global_paused
      : connectionMessages.control_unavailable
    return
  }
  if (!validProviderDraft()) return
  providerBusy.value = 'test'
  connectionNotice.value = ''
  deletingKey.value = false
  const payload = providerPayload()
  providerForm.value.api_key = ''
  try {
    const result = await api.llmTest(payload)
    connectionKind.value = result.ok ? 'success'
      : (['global_paused', 'control_unavailable'].includes(result.code) ? 'warning' : 'error')
    connectionNotice.value = connectionMessages[result.code] || '连接尚未验证，请检查配置后重试。'
    if (['global_paused', 'control_unavailable'].includes(result.code)) {
      controlUncertain.value = true
      try { await reloadLLM() } catch (_) { /* Remain paused when permission cannot be read. */ }
    }
  } catch (error) {
    connectionKind.value = 'error'
    connectionNotice.value = (error?.name === 'AbortError'
      ? '等待连接测试结果超时，请检查本地服务或网络后重试。' : connectionMessages[error?.result?.code])
      || '未能完成连接测试，请检查本地服务状态后重试。'
  } finally {
    providerForm.value.api_key = ''
    providerBusy.value = ''
  }
}

onBeforeUnmount(() => { providerForm.value.api_key = '' })

async function toggleLLM() {
  if (!llm.value || globalControlBusy.value) return
  const turningOn = !globalAllowed.value
  if (turningOn && !window.confirm(
    '允许后，用户主动测试/按次分析以及已开启自动实况的主播可以发起模型请求，可能产生费用。'
    + '此操作不会替你开启任何主播的自动实况分析。\n\n确定允许内置AI调用？')) return
  llmBusy.value = true
  llmHint.value = ''
  try {
    const r = await api.llmGate(turningOn)
    llm.value = r
    controlUncertain.value = typeof r?.control?.allowed !== 'boolean'
      || r.control.allowed !== turningOn
    llmHint.value = controlKnown.value && r.control.allowed === turningOn
      ? (turningOn ? '已允许内置AI调用；各主播自动实况设置保持不变。'
        : '已暂停新的内置AI调用；已发送请求可按原规则收尾。')
      : '许可更新尚未确认，连接测试保持暂停。请重新读取状态。'
  } catch (_) {
    controlUncertain.value = true
    llmHint.value = '许可更新未确认，连接测试保持暂停。请重新读取状态后再操作。'
  } finally {
    llmBusy.value = false
  }
}

async function refreshControl() {
  if (llmBusy.value) return
  llmBusy.value = true
  llmHint.value = ''
  try {
    await reloadLLM()
  } catch (_) {
    llmHint.value = '无法读取当前许可状态，连接测试保持暂停。'
  } finally {
    llmBusy.value = false
  }
}

async function switchProvider() {
  llmBusy.value = true
  llmHint.value = ''
  try {
    const r = await api.llmProvider(chosen.value)
    llm.value = r
    llmHint.value = r.hint || ''
  } catch (e) {
    llmHint.value = '失败：' + (e.message || '')
  } finally {
    llmBusy.value = false
  }
}

// The existing budget is a token guard, independent of optional estimate prices.
const budgetTokens = ref('')
const budgetMsg = ref('')

function fmtTok(n) {
  if (!Number.isSafeInteger(n) || n < 0) return '未知'
  return n.toLocaleString('zh-CN')
}
const fmtCap = (n) => n === 0 ? '不设限' : fmtTok(n) + ' token'

function syncBudgetField() {
  // 优先用 config 里的目标值：账本的 cap 是采集进程启动时写死的，改完不会立刻变
  const b = (llm.value && llm.value.budget) || {}
  const cap = b.config_cap_tokens != null ? b.config_cap_tokens : b.cap_tokens
  budgetTokens.value = Number.isSafeInteger(cap) && cap >= 0 ? String(cap) : ''
}

async function saveBudget() {
  budgetMsg.value = ''
  const raw = String(budgetTokens.value).trim()
  const tokens = Number(raw)
  if (!/^\d+$/.test(raw) || !Number.isSafeInteger(tokens) || tokens < 0) {
    budgetMsg.value = '请填写非负整数 token 数（0 = 不设限）'
    return
  }
  llmBusy.value = true
  try {
    const r = await api.llmBudget(tokens)
    llm.value = r
    syncBudgetField()
    budgetMsg.value = r.hint || '已保存'
  } catch (e) {
    budgetMsg.value = '失败：' + (e.message || '')
  } finally {
    llmBusy.value = false
  }
}

const priceBook = ref({ schema: 1, entries: [] })
const priceProvider = ref('')
const priceModel = ref('')
const priceCurrency = ref('')
const priceInput = ref('')
const priceOutput = ref('')
const priceBusy = ref('')
const priceNotice = ref('')
const priceNoticeError = ref(false)
const pricesKnown = ref(false)
const savedPrice = computed(() => priceBook.value.entries.find((entry) => entry.provider === priceProvider.value
  && entry.model === priceModel.value.trim()) || null)
const priceProviders = computed(() => [...new Set([...(llm.value?.providers || []).map((p) => p.name),
  ...priceBook.value.entries.map((p) => p.provider)])].filter((value) => typeof value === 'string' && value))
const priceModels = computed(() => [...new Set([(llm.value?.providers || []).find((p) => p.name === priceProvider.value)?.model,
  ...priceBook.value.entries.filter((p) => p.provider === priceProvider.value).map((p) => p.model)])].filter(Boolean))
const priceText = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? String(value) : '未配置'
function syncPriceForm() {
  const entry = savedPrice.value
  priceCurrency.value = entry?.currency || ''
  priceInput.value = entry?.input_per_million == null ? '' : String(entry.input_per_million)
  priceOutput.value = entry?.output_per_million == null ? '' : String(entry.output_per_million)
}
watch(priceProvider, () => {
  priceModel.value = (llm.value?.providers || []).find((p) => p.name === priceProvider.value)?.model || ''
}, { flush: 'sync' })
watch([priceProvider, priceModel], () => { syncPriceForm(); priceNotice.value = '' }, { flush: 'sync' })
watch(() => llm.value?.providers, () => {
  if (!priceProvider.value) priceProvider.value = chosen.value || llm.value?.providers?.[0]?.name || ''
})
async function reloadPrices() {
  if (priceBusy.value) return
  priceBusy.value = 'read'
  pricesKnown.value = false
  priceNotice.value = ''
  try {
    const result = await api.llmPrices()
    if (result.schema !== 1 || !Array.isArray(result.entries)) throw new Error('Invalid prices')
    priceBook.value = result
    pricesKnown.value = true
    syncPriceForm()
    priceNoticeError.value = false
  } catch (_) {
    priceNoticeError.value = true
    priceNotice.value = '单价读取失败，当前单价状态待确认；不会测试或调用模型。'
  } finally { priceBusy.value = '' }
}
async function savePrices(clear = false) {
  if (priceBusy.value) return
  priceNotice.value = ''
  const parse = (value) => {
    const raw = String(value).trim()
    return raw === '' ? null : /^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw) ? Number(raw) : NaN
  }
  const input = clear ? null : parse(priceInput.value), output = clear ? null : parse(priceOutput.value)
  const currency = clear ? '' : priceCurrency.value.trim().toUpperCase()
  const provider = priceProvider.value, model = priceModel.value.trim()
  if (!provider || !model || [input, output].some((value) => value !== null && (!Number.isFinite(value) || value < 0))
    || ((input !== null || output !== null) && !/^[A-Z]{3,8}$/.test(currency))) {
    priceNoticeError.value = true
    priceNotice.value = '请填写提供商、模型与 3–8 位字母币种代码；单价须为非负有限数字，留空表示未配置。'
    return
  }
  priceBusy.value = 'save'
  pricesKnown.value = false
  try {
    const result = await api.llmSavePrices({ provider, model, currency,
      input_per_million: input, output_per_million: output })
    if (result.ok !== true || result.config_saved !== true || result.prices?.schema !== 1
      || !Array.isArray(result.prices.entries)) throw new Error('Unconfirmed price save')
    priceBook.value = result.prices
    pricesKnown.value = true
    syncPriceForm()
    priceNoticeError.value = false
    priceNotice.value = result.entry ? '估算单价已保存，仅用于后续记录的价格快照；没有测试或启用模型。'
      : '当前估算单价已清除；历史记录仍保留原价格快照，没有调用模型。'
  } catch (_) {
    priceNoticeError.value = true
    priceNotice.value = '单价保存结果未确认，请只读重新读取单价；不会自动重试、测试或启用模型。'
  } finally { priceBusy.value = '' }
}
onMounted(reloadPrices)

const oldPwd = ref('')
const newPwd = ref('')
const newPwd2 = ref('')
const pwdMsg = ref('')
const pwdErr = ref('')
const pwdBusy = ref(false)
const showPwd = ref(false)

async function changePwd() {
  pwdMsg.value = ''
  pwdErr.value = ''
  if (newPwd.value.length < 6) {
    pwdErr.value = '新密码至少 6 位'
    return
  }
  if (newPwd.value !== newPwd2.value) {
    pwdErr.value = '两次输入不一致'
    return
  }
  pwdBusy.value = true
  try {
    const r = await api.passwd(oldPwd.value, newPwd.value)
    pwdMsg.value = r.note || '已修改'
    oldPwd.value = ''
    newPwd.value = ''
    newPwd2.value = ''
  } catch (e) {
    pwdErr.value = e.message || '修改失败'
  } finally {
    pwdBusy.value = false
  }
}

onMounted(async () => {
  try {
    await reloadLLM()
    editorProvider.value = chosen.value || '__new__'
    syncBudgetField()
  } catch (e) {
    err.value = e.message || '接口未就绪'
  }
})
</script>

<template>
  <div class="grid-2">
    <div v-if="board.accessMode === 'account'" class="card" data-testid="account-settings">
      <h3>账号<span class="sub">当前登录：{{ board.user }}（{{ board.role }}）</span></h3>
      <div style="font-size: 12px; color: var(--dim); margin-bottom: 12px">
        {{ showPwd ? '密码会明文显示在屏幕上' : '密码默认隐藏，改完建议退出重登' }}
        <button class="btn ghost" style="padding: 2px 8px; font-size: 12px; margin-left: 6px" @click="showPwd = !showPwd">
          {{ showPwd ? '隐藏' : '显示' }}
        </button>
      </div>

      <label style="font-size: 12px; color: var(--dim)">当前密码</label>
      <input v-model="oldPwd" :type="showPwd ? 'text' : 'password'" class="field" style="width: 100%; margin: 4px 0 10px" autocomplete="current-password" />

      <label style="font-size: 12px; color: var(--dim)">新密码（至少 6 位）</label>
      <input v-model="newPwd" :type="showPwd ? 'text' : 'password'" class="field" style="width: 100%; margin: 4px 0 10px" autocomplete="new-password" />

      <label style="font-size: 12px; color: var(--dim)">再输一次</label>
      <input v-model="newPwd2" :type="showPwd ? 'text' : 'password'" class="field" style="width: 100%; margin: 4px 0 12px" autocomplete="new-password" />

      <button class="btn primary" :disabled="pwdBusy" @click="changePwd">
        {{ pwdBusy ? '修改中…' : '修改密码' }}
      </button>
      <div v-if="pwdErr" style="color: var(--red); font-size: 12px; margin-top: 10px">{{ pwdErr }}</div>
      <div v-if="pwdMsg" style="color: var(--green); font-size: 12px; margin-top: 10px">{{ pwdMsg }}</div>

      <div style="margin-top: 14px; font-size: 11px; color: var(--dim-2); line-height: 1.8">
        给同事开账号目前走命令行：<code>python scripts\board_auth.py adduser 名字</code>（交互输密码）。
        用户库 <code>data/board_users.json</code> 不入 git。
      </div>
    </div>

    <div v-else class="stack" style="align-self: start">
      <div class="card" data-testid="local-access-settings">
        <h3>本地访问</h3>
        <p class="muted">当前通过本机看板访问，无需创建账号或输入看板密码。关闭网页不会停止后台应用。</p>
        <p class="muted">直播平台登录与模型密钥分别设置；没有模型密钥也可查看基础数据。</p>
      </div>
      <BrowserLoginGuide />
      <AsrStatusCard />
      <FeishuNotifications />
    </div>

    <div class="card">
      <h3>内置AI调用许可<span class="sub">总许可与主播自动实况设置分别控制</span></h3>

      <div v-if="!llm" class="empty">{{ err || '读取中…' }}</div>
      <template v-else>
        <div style="display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 10px">
          <span class="dot" :class="globalAllowed ? 'on' : 'off'"></span>
          <b style="font-size: 14px" data-testid="ai-control-state">{{ !controlKnown ? '许可状态待确认' : (globalAllowed ? '已允许内置AI调用' : '已暂停内置AI调用') }}</b>
          <span style="flex: 1"></span>
          <button class="btn" :class="globalAllowed ? '' : 'primary'" :disabled="globalControlBusy" @click="toggleLLM" data-testid="global-ai-toggle">
            {{ llmBusy ? '处理中…' : (globalAllowed ? '暂停内置AI调用' : '允许内置AI调用') }}
          </button>
          <button class="btn ghost" :disabled="llmBusy" @click="refreshControl" data-testid="refresh-ai-control">重新读取状态</button>
        </div>

        <div v-if="llmHint" style="font-size: 12px; margin-bottom: 10px; color: var(--ink-2); line-height: 1.7">
          {{ llmHint }}
        </div>

        <p class="model-hint" style="margin-bottom: 12px" data-testid="ai-control-reason">{{ permissionMessage }}</p>

        <div style="display: flex; align-items: center; gap: 8px; margin: 12px 0 6px">
          <span style="font-size: 12px; color: var(--dim)">走哪一把</span>
          <select v-model="chosen" class="field" style="padding: 4px 8px" :disabled="llmBusy || !!providerBusy" @change="switchProvider">
            <option v-for="p in llm.providers" :key="p.name" :value="p.name">
              {{ p.name }}{{ p.label ? ' · ' + p.label : '' }}{{ p.owner ? ' / ' + p.owner : '' }}
            </option>
          </select>
          <span style="font-size: 11px; color: var(--dim-2)">切换只改名字，不碰密钥</span>
        </div>

        <form class="provider-editor" aria-label="提供商配置" novalidate @submit.prevent="saveProvider">
          <h4>提供商配置</h4>
          <p v-if="!hasConfiguredKeys" class="model-hint" data-testid="no-key-notice">
            尚未配置密钥。基础采集、转写和数据查询仍可使用。
          </p>
          <label for="provider-edit">编辑提供商</label>
          <select id="provider-edit" v-model="editorProvider" class="field" :disabled="!!providerBusy">
            <option v-for="item in llm.providers" :key="item.name" :value="item.name">{{ item.name }}</option>
            <option value="__new__">新增提供商</option>
          </select>
          <div v-if="editorProvider === '__new__'" class="provider-field">
            <label for="provider-name">提供商标识</label>
            <input id="provider-name" v-model="newProvider" class="field" autocomplete="off" spellcheck="false"
              placeholder="例如 my-provider" :disabled="!!providerBusy" />
          </div>
          <div class="provider-field">
            <label for="provider-protocol">接口模式</label>
            <select id="provider-protocol" v-model="providerForm.protocol" class="field" :disabled="!!providerBusy">
              <option value="openai_compatible">OpenAI 兼容接口</option>
              <option value="doubao">豆包（火山方舟）</option>
            </select>
          </div>
          <div class="provider-field">
            <label for="provider-url">API 地址</label>
            <input id="provider-url" v-model="providerForm.base_url" class="field" type="url" autocomplete="off"
              placeholder="https://服务商地址/v1" spellcheck="false" :disabled="!!providerBusy" />
          </div>
          <div class="provider-field">
            <label for="provider-model">模型名</label>
            <input id="provider-model" v-model="providerForm.model" class="field" autocomplete="off"
              spellcheck="false" :disabled="!!providerBusy" />
          </div>
          <div class="provider-field">
            <label for="provider-key">API key <span class="saved-key">{{ selectedProvider?.has_key ? '已保存密钥' : '未保存密钥' }}</span></label>
            <input id="provider-key" v-model="providerForm.api_key" class="field" type="password" autocomplete="off"
              autocapitalize="off" spellcheck="false" placeholder="留空保留已保存密钥" :disabled="!!providerBusy" />
            <p class="model-hint">明文只用于本次提交，操作完成后清空。删除密钥请使用下方独立按钮。</p>
          </div>
          <div class="provider-actions">
            <button class="btn primary" type="submit" :disabled="!!providerBusy || llmBusy" data-testid="save-provider">
              {{ providerBusy === 'save' ? '保存中…' : (providerForm.api_key.trim() ? '保存并替换密钥' : '保存配置') }}
            </button>
            <button class="btn" type="button" :disabled="!!providerBusy || llmBusy || !globalAllowed" @click="testProvider" data-testid="test-provider">
              {{ providerBusy === 'test' ? '测试中…' : '测试连接' }}
            </button>
            <button class="btn" type="button" :disabled="!!providerBusy || llmBusy || !selectedProvider?.has_key"
              @click="deleteProviderKey" data-testid="delete-provider-key">
              {{ providerBusy === 'delete' ? '删除中…' : (deletingKey ? '确认删除密钥' : '删除已保存密钥') }}
            </button>
            <button v-if="deletingKey" class="btn ghost" type="button" @click="deletingKey = false">取消</button>
          </div>
          <p class="model-hint">测试当前填写的接口和模型，留空 key 时使用已保存密钥；可能产生少量费用。仅点击才发起一次测试，不保存配置或开启自动分析。连接测试使用直连，不使用环境代理。</p>
          <p v-if="!globalAllowed" class="provider-result warning" data-testid="test-paused-reason">{{ controlKnown ? '内置AI总许可已暂停，连接测试不可用；保存和删除密钥仍可操作。' : '许可状态待确认，连接测试不可用；保存和删除密钥仍可操作。' }}</p>
          <p v-if="deletingKey" class="provider-result warning">删除后，后续模型请求不再使用此密钥；此操作保留接口地址和模型配置。</p>
          <p v-if="providerNotice" class="provider-result" :class="providerNoticeKind" role="status" data-testid="provider-notice">{{ providerNotice }}</p>
          <p v-if="connectionNotice" class="provider-result" :class="connectionKind" role="status" data-testid="connection-notice">{{ connectionNotice }}</p>
        </form>

        <table style="width: 100%; border-collapse: collapse; font-size: 13px">
          <thead>
            <tr style="color: var(--dim); font-size: 12px; text-align: left">
              <th style="padding: 6px 8px">provider</th>
              <th style="padding: 6px 8px">模型</th>
              <th style="padding: 6px 8px">密钥</th>
              <th style="padding: 6px 8px; text-align: right">今日业务记录</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in llm.providers" :key="p.name" style="border-top: 1px solid var(--line-soft)">
              <td style="padding: 6px 8px">
                <span class="dot" :class="p.active ? 'on' : ''"></span>
                <span style="margin-left: 6px">{{ p.name }}</span>
                <span v-if="p.active" style="font-size: 11px; color: var(--green); margin-left: 4px">在用</span>
              </td>
              <td style="padding: 6px 8px; color: var(--ink-2)">{{ p.model || '—' }}</td>
              <td style="padding: 6px 8px; color: var(--dim-2); font-family: ui-monospace, monospace">
                {{ p.has_key ? '已配置' : '未配置' }}
              </td>
              <td style="padding: 6px 8px; text-align: right; color: var(--ink-2)">
                {{ providerRecords(p) }}
              </td>
            </tr>
            <tr v-if="!llm.providers.length">
              <td colspan="4" class="empty">config.json 里没有 provider</td>
            </tr>
          </tbody>
        </table>

        <div class="usage-box" data-testid="global-usage">
          <h4>全局业务用量<span class="sub">实际调用日：{{ dailyUsage.callDate || '待确认' }}</span></h4>
          <p class="model-hint">仅汇总已落盘指标，不是提供商完整账单。SDK 尝试不等于 HTTP 次数；任务成功不代表费用覆盖完整。当前连接测试不落盘，日报内存报告及其他未落盘消费者不计入这里。</p>
          <div v-if="!dailyUsage.available" class="provider-result warning">用量口径待确认；不能据此认为用量或费用为零。</div>
          <template v-else>
            <div class="usage-state" data-testid="global-usage-coverage">{{ dailyUsage.coverage }}</div>
            <div class="usage-line">业务指标记录 {{ dailyUsage.records }} · SDK 尝试 {{ dailyUsage.attempts }} · 未发送记录 {{ dailyUsage.notSent }} · 失败记录 {{ dailyUsage.failed }}</div>
            <div class="usage-line">已知输入 {{ dailyUsage.input }} token · 已知输出 {{ dailyUsage.output }} token · usage 已知记录 {{ dailyUsage.knownRecords }}</div>
            <div v-for="gap in dailyUsage.gaps" :key="gap" class="provider-result warning">{{ gap }}</div>
            <div class="usage-line">探测记录 {{ dailyUsage.probeRecords }} · 连接测试记录 {{ dailyUsage.testRecords }}；{{ dailyUsage.diagnosticsExcluded ? '独立标记，不计上述业务用量、估算及原业务护栏' : '诊断纳入口径待确认' }}。</div>
            <div v-if="!dailyUsage.estimates.length" class="provider-result warning" data-testid="global-estimate-missing">没有可用估价依据，不代表费用为零；可能缺少已知 usage 或发送时价格快照。</div>
            <div v-for="estimate in dailyUsage.estimates" :key="estimate.currency" class="estimate-row" data-testid="global-estimate">
              <b>{{ estimate.partial ? '已知部分估算' : '已记录部分估算' }}：{{ estimate.currency }} {{ estimate.amount }}</b>
              <div>已定价分量 {{ estimate.priced }} · 缺价格分量 {{ estimate.unpriced }}；币种分列，不换汇合并。</div>
              <div>发送时价格版本：{{ estimate.versions.join('、') || '未记录' }}</div>
              <div v-for="snapshot in estimate.snapshots" :key="snapshot.version">{{ snapshot.source }} · 版本 {{ snapshot.version }} · 快照保存时点 {{ snapshot.saved_at }}</div>
            </div>
            <details v-if="dailyUsage.providers.length" class="usage-line"><summary>按发送时提供商 / 模型查看</summary>
              <div v-for="row in dailyUsage.providers" :key="row.provider + ':' + row.model">{{ row.provider }} / {{ row.model }} · 指标 {{ row.records }} · SDK 尝试 {{ row.attempts }} · 已知输入 {{ row.input }} / 输出 {{ row.output }} token</div>
            </details>
          </template>
        </div>

        <div style="margin-top: 14px; font-size: 12px; color: var(--ink-2); line-height: 2">
          <span v-if="llm.budget && llm.budget.used_tokens != null">今日护栏账本已记录 <b>{{ fmtTok(llm.budget.used_tokens) }}</b> token · </span>
          冷场台词 {{ llm.flags.coldline_enabled ? '开' : '关' }}
          · 商品推荐 {{ llm.flags.recommend_enabled ? '开' : '关' }}
        </div>

        <div class="budget-edit">
          <label for="daily-token-budget">当日 token 护栏</label>
          <input id="daily-token-budget" v-model="budgetTokens" class="field" type="number"
                 min="0" step="1" style="width: 160px" data-testid="token-budget" />
          <span>token / 调用日（0 = 不设限）</span>
          <button class="btn" :disabled="llmBusy || !!providerBusy" @click="saveBudget" data-testid="save-token-budget">保存护栏</button>
          <span v-if="budgetMsg" class="muted" data-testid="token-budget-notice">{{ budgetMsg }}</span>
          <span v-if="llm.budget && llm.budget.pending" class="muted" style="color: var(--amber)">
            目标 {{ fmtCap(llm.budget.config_cap_tokens) }}；运行护栏仍为 {{ fmtCap(llm.budget.cap_tokens) }}，待该路按现有机制重新加载后生效
          </span>
        </div>
        <div class="muted" style="margin-top: -4px">
          护栏按已知 token 用量检查。缺失或未知 usage 会限制覆盖范围；这不是人民币金额上限，也不保证未知用量为零。价格估算配置不会改变此护栏。
        </div>

        <section class="price-editor" data-testid="price-editor">
          <h4>可选估算单价<span class="sub">每百万 token · 来源固定为用户填写</span></h4>
          <p class="model-hint">单价只用于后续记录的价格快照，不改写旧记录，不测试或启用模型，也不改变密钥与 token 护栏。留空是未配置，明确填写 0 才是零价；单侧配置只能估算已知且有价格的部分。</p>
          <div class="price-fields">
            <label>提供商<select v-model="priceProvider" class="field" :disabled="!!priceBusy" data-testid="price-provider"><option value="">请选择</option><option v-for="name in priceProviders" :key="name" :value="name">{{ name }}</option></select></label>
            <label>模型<input v-model="priceModel" class="field" list="price-models" :disabled="!!priceBusy" autocomplete="off" data-testid="price-model" /><datalist id="price-models"><option v-for="model in priceModels" :key="model" :value="model" /></datalist></label>
            <label>币种代码<input v-model="priceCurrency" class="field" maxlength="8" placeholder="例如 CNY 或 USD" :disabled="!!priceBusy" autocomplete="off" data-testid="price-currency" /></label>
            <label>每百万输入 token<input v-model="priceInput" class="field" type="text" inputmode="decimal" placeholder="留空：未配置" :disabled="!!priceBusy" data-testid="price-input" /></label>
            <label>每百万输出 token<input v-model="priceOutput" class="field" type="text" inputmode="decimal" placeholder="留空：未配置" :disabled="!!priceBusy" data-testid="price-output" /></label>
          </div>
          <div class="provider-actions"><button class="btn primary" :disabled="!!priceBusy" @click="savePrices(false)" data-testid="save-prices">保存估算单价</button><button class="btn" :disabled="!!priceBusy || !priceProvider || !priceModel.trim()" @click="savePrices(true)" data-testid="clear-prices">清除当前单价</button><button class="btn ghost" :disabled="!!priceBusy" @click="reloadPrices" data-testid="reload-prices">只读重新读取单价</button></div>
          <p v-if="priceNotice" class="provider-result" :class="priceNoticeError ? 'warning' : 'success'" data-testid="price-notice">{{ priceNotice }}</p>
          <p v-if="!pricesKnown" class="provider-result warning">单价状态待确认，以下内容可能为上次读取值。</p>
          <p v-else-if="savedPrice" class="model-hint" data-testid="price-snapshot">用户填写 · 版本 {{ savedPrice.version }} · 保存时点 {{ savedPrice.saved_at }} · 输入 {{ priceText(savedPrice.input_per_million) }} / 输出 {{ priceText(savedPrice.output_per_million) }} {{ savedPrice.currency }} / 百万 token</p>
          <p v-else class="model-hint">此提供商 / 模型尚未配置当前估算单价；历史记录仍使用其发送时快照。</p>
        </section>

        <div v-if="llm.errors && llm.errors.length" class="empty" style="margin-top: 10px; text-align: left">
          {{ llm.errors.join('；') }}
        </div>
        <div style="margin-top: 12px; font-size: 11px; color: var(--dim-2); line-height: 1.8">
          页面只显示密钥是否配置，保存或删除不会开启自动分析。<br />
          <template v-if="board.accessMode === 'account'">新页面出问题时回旧版：<a href="/legacy" target="_blank" style="color: var(--green)">/legacy</a>（保留一个月）。</template>
          <template v-else>当前为本地访问；旧版看板已停用。</template>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.provider-editor { margin: 16px 0; padding: 16px 0; border-top: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft); }
.provider-editor h4 { margin: 0 0 12px; font-size: 14px; }
.provider-editor label { display: block; margin-bottom: 5px; color: var(--dim); font-size: 12px; }
.provider-editor .field { width: 100%; }
.provider-field { margin-top: 12px; }
.saved-key { margin-left: 8px; color: var(--dim-2); }
.model-hint { margin: 6px 0 0; color: var(--dim-2); font-size: 12px; line-height: 1.7; }
.provider-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.provider-result { margin: 10px 0 0; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.provider-result.success { color: var(--green); }
.provider-result.warning { color: var(--amber); }
.provider-result.error { color: var(--red); }
.usage-box, .price-editor { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--line-soft); }
.usage-box h4, .price-editor h4 { margin: 0 0 8px; font-size: 14px; }
.usage-state { margin-top: 10px; font-weight: 600; font-size: 13px; }
.usage-line, .estimate-row { font-size: 12px; line-height: 1.9; overflow-wrap: anywhere; margin-top: 7px; }
.estimate-row { padding: 8px 10px; border: 1px solid var(--line-soft); border-radius: 6px; }
.price-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-top: 12px; }
.price-fields label { font-size: 12px; color: var(--dim); }
.price-fields .field { display: block; width: 100%; margin-top: 5px; }
</style>
