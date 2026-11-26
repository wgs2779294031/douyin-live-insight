<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../store'

const saved = ref(null)
const enabled = ref(false)
const transport = ref('webhook')
const webhook = ref('')
const signingSecret = ref('')
const clearSigning = ref(false)
const busy = ref('')
const notice = ref('')
const noticeKind = ref('')
const testNotice = ref('')
const testKind = ref('')
const confirmingTest = ref(false)
const confirmingClear = ref(false)
let disposed = false, sequence = 0

const dirty = computed(() => !!saved.value && (
  enabled.value !== saved.value.enabled || transport.value !== saved.value.transport
  || !!webhook.value.trim() || !!signingSecret.value.trim() || clearSigning.value))
const configured = computed(() => transport.value === 'legacy_app'
  ? saved.value?.legacy_app_available : saved.value?.webhook_configured)
const canTest = computed(() => !!saved.value && saved.value.enabled && configured.value
  && !dirty.value && !busy.value)
const canClear = computed(() => !!saved.value && (saved.value.enabled
  || saved.value.webhook_configured || saved.value.signing_secret_configured || saved.value.source === 'legacy'))
const destination = computed(() => saved.value?.transport === 'legacy_app'
  ? '已保存的飞书应用接收会话' : '已保存群机器人的飞书群')
const errorMessages = {
  disabled: '通知当前已停用。启用并保存后才能发送测试通知。',
  not_configured: '尚未配置可用的通知地址，请填写并保存。',
  rate_limited: '测试发送过于频繁，请稍后再试。',
  invalid_request: '配置未保存，请检查输入后重试。',
  invalid_webhook: '地址未被接受，请填写飞书官方群自定义机器人地址。',
  invalid_secret: '签名密钥格式不正确，请检查后重新填写。',
  legacy_app_unavailable: '没有可沿用的完整飞书应用配置，请使用群自定义机器人。',
  configuration_unavailable: '无法读取通知配置，请检查本地配置状态后重试。',
  configuration_changed: '通知配置已经变化，本次没有继续发送。请重新读取状态并确认接收会话。',
  storage_failed: '配置保存未完成，请检查本地存储后重新读取状态。',
  remote_rejected: '飞书未接受这次通知，请检查机器人状态、安全设置和接收权限。',
  delivery_failed: '未能确认测试通知送达，请先查看接收会话并检查配置与网络。',
  test_busy: '已有通知测试正在进行，请等待完成。',
  stopping: '应用正在停止，本次未开始新的通知测试。'
}

function normalizeSettings(raw) {
  if (!raw || typeof raw !== 'object'
    || typeof raw.enabled !== 'boolean'
    || !['webhook', 'legacy_app'].includes(raw.transport)
    || !['explicit', 'legacy', 'unconfigured'].includes(raw.source)
    || ['webhook_configured', 'signing_secret_configured', 'legacy_app_available']
      .some((key) => typeof raw[key] !== 'boolean')) return null
  return { enabled: raw.enabled, transport: raw.transport, source: raw.source,
    webhook_configured: raw.webhook_configured,
    signing_secret_configured: raw.signing_secret_configured,
    legacy_app_available: raw.legacy_app_available }
}

function wipeDrafts() {
  webhook.value = ''
  signingSecret.value = ''
  clearSigning.value = false
}

function applySettings(raw) {
  const settings = normalizeSettings(raw)
  if (!settings) return false
  saved.value = settings
  enabled.value = settings.enabled
  transport.value = settings.transport
  wipeDrafts()
  return true
}

watch([enabled, transport, webhook, signingSecret, clearSigning], () => {
  confirmingTest.value = false
  confirmingClear.value = false
  testNotice.value = ''
}, { flush: 'sync' })
watch(clearSigning, (clear) => { if (clear) signingSecret.value = '' }, { flush: 'sync' })
watch(transport, wipeDrafts, { flush: 'sync' })

async function readSettings() {
  if (busy.value) return
  const id = ++sequence
  busy.value = 'read'
  notice.value = ''
  testNotice.value = ''
  confirmingTest.value = false
  confirmingClear.value = false
  wipeDrafts()
  try {
    const result = await api.feishuNotifications()
    if (disposed || id !== sequence) return
    if (result?.ok !== true || !applySettings(result.settings)) throw new Error('invalid_settings')
  } catch (_) {
    if (disposed || id !== sequence) return
    saved.value = null
    noticeKind.value = 'warning'
    notice.value = '通知状态读取失败，暂不保存或发送。请重新读取状态。'
  } finally {
    if (!disposed && id === sequence) busy.value = ''
  }
}

async function act(action, payload = {}) {
  if (busy.value || !saved.value) return
  const id = ++sequence
  busy.value = action
  notice.value = ''
  testNotice.value = ''
  confirmingTest.value = false
  confirmingClear.value = false
  wipeDrafts()
  try {
    const result = await api.feishuNotificationAction({ action, ...payload })
    if (disposed || id !== sequence) return
    const accepted = applySettings(result?.settings)
    if (action === 'test') {
      const delivered = result?.ok === true && result?.code === 'delivered' && result?.delivered === true
      testKind.value = delivered ? 'success' : 'warning'
      testNotice.value = delivered ? '飞书已接受测试通知，请到接收会话确认消息。测试成功不代表所有异常都能被发现。'
        : errorMessages[result?.code] || '本次测试结果未确认，请先查看接收会话；不会自动重发。'
    } else {
      const success = accepted && result?.ok === true
        && result?.code === (action === 'clear' ? 'settings_cleared' : 'settings_saved')
      noticeKind.value = success ? 'success' : 'warning'
      notice.value = success
        ? (action === 'clear' ? '通知已停用，通知专用地址和签名密钥已清除；原多维表格配置保持不变。'
          : '通知配置已保存。保存不会发送测试消息，也不代表消息已经送达。')
        : errorMessages[result?.code] || '操作结果未确认，请重新读取状态核对。'
    }
    if (!accepted) {
      saved.value = null
      noticeKind.value = 'warning'
      notice.value = '操作后的通知状态未确认，请重新读取状态；不会自动重复操作。'
    }
  } catch (error) {
    if (disposed || id !== sequence) return
    const fixed = errorMessages[error?.result?.code]
    if (action === 'test') {
      testKind.value = 'warning'
      testNotice.value = fixed || '测试结果未确认，请先查看接收会话，再重新读取状态；不会自动重发。'
    } else {
      noticeKind.value = 'warning'
      notice.value = fixed || '操作结果未确认，请重新读取状态后核对；不会自动重复保存或清除。'
    }
    saved.value = null
  } finally {
    if (!disposed && id === sequence) {
      wipeDrafts()
      busy.value = ''
    }
  }
}

function saveSettings() {
  const body = { enabled: enabled.value, transport: transport.value }
  if (transport.value === 'webhook') {
    if (webhook.value.trim()) body.webhook_url = webhook.value.trim()
    if (signingSecret.value.trim()) body.signing_secret = signingSecret.value.trim()
    if (clearSigning.value) body.clear_signing_secret = true
  }
  return act('save', body)
}

function prepareTest() {
  if (!canTest.value) return
  confirmingClear.value = false
  testNotice.value = ''
  confirmingTest.value = true
}

function clearSettings() {
  if (busy.value || !canClear.value) return
  if (!confirmingClear.value) {
    wipeDrafts()
    confirmingTest.value = false
    confirmingClear.value = true
    return
  }
  return act('clear')
}

onMounted(readSettings)
onBeforeUnmount(() => { disposed = true; ++sequence; wipeDrafts() })
</script>

<template>
  <section class="card feishu-notifications" data-testid="feishu-notifications">
    <h3>飞书异常通知</h3>
    <p class="muted">将已启用目标的部分采集事件、持续通道异常和恢复提醒发送到自己的飞书会话。不会开启多维表格同步，也不会自动完成平台验证。</p>
    <div v-if="!saved" class="empty">{{ busy === 'read' ? '读取通知配置…' : '通知状态待确认' }}</div>
    <form v-else aria-label="飞书通知配置" @submit.prevent="saveSettings">
      <p v-if="saved.source === 'legacy'" class="notification-hint" data-testid="feishu-legacy-source">正在沿用旧飞书通知配置。保存后将使用独立的异常通知设置，多维表格配置不随此表单修改。</p>
      <label class="notification-toggle"><input v-model="enabled" type="checkbox" :disabled="!!busy" data-testid="feishu-enabled" /> 启用飞书事件与异常通知</label>
      <label for="feishu-transport">发送方式</label>
      <select id="feishu-transport" v-model="transport" class="field" :disabled="!!busy">
        <option value="webhook">飞书群自定义机器人</option>
        <option v-if="saved.legacy_app_available || saved.transport === 'legacy_app'" value="legacy_app">沿用已有飞书应用接收配置</option>
      </select>
      <template v-if="transport === 'webhook'">
        <label for="feishu-webhook">机器人地址 <span class="saved-state">{{ saved.webhook_configured ? '已保存，地址不回显' : '尚未保存地址' }}</span></label>
        <input id="feishu-webhook" v-model="webhook" class="field" type="password" maxlength="2048" autocomplete="off" autocapitalize="off" spellcheck="false" :disabled="!!busy"
          placeholder="粘贴飞书群自定义机器人地址；留空保留已保存地址" />
        <label for="feishu-signing-secret">签名密钥（可选） <span class="saved-state">{{ saved.signing_secret_configured ? '已保存，不回显' : '尚未保存' }}</span></label>
        <input id="feishu-signing-secret" v-model="signingSecret" class="field" type="password" maxlength="512" autocomplete="off" autocapitalize="off" spellcheck="false" :disabled="!!busy || clearSigning"
          placeholder="机器人开启签名校验时填写；留空保留已保存密钥" />
        <label v-if="saved.signing_secret_configured" class="notification-toggle"><input v-model="clearSigning" type="checkbox" :disabled="!!busy" data-testid="feishu-clear-signing" /> 保存时清除签名密钥</label>
        <p class="notification-hint">请使用飞书群“自定义机器人”的地址。关键词过滤可使用消息正文中的“竞品监控”；若机器人开启签名校验，请填写对应签名密钥。</p>
      </template>
      <p v-else class="notification-hint">仅沿用本机已有的飞书应用凭据和接收目标。本页不修改应用权限或多维表格设置。</p>
      <div class="notification-actions">
        <button class="btn primary" type="submit" :disabled="!!busy" data-testid="feishu-save">{{ busy === 'save' ? '保存中…' : '保存通知配置' }}</button>
        <button class="btn" type="button" :disabled="!canTest" @click="prepareTest" data-testid="feishu-test">{{ busy === 'test' ? '测试发送中…' : '发送测试通知' }}</button>
        <button class="btn ghost" type="button" :disabled="!!busy || !canClear" @click="clearSettings" data-testid="feishu-clear">{{ confirmingClear ? '确认停用并清除' : '停用并清除通知配置' }}</button>
      </div>
      <p v-if="dirty" class="notification-hint" data-testid="feishu-unsaved">有未保存的修改。先保存，再测试已保存的接收配置。</p>
      <p v-else-if="!saved.enabled" class="notification-hint">通知当前已停用。启用并保存后，可主动发送测试消息。</p>
      <div v-if="confirmingTest" class="notification-confirm" data-testid="feishu-test-confirmation">
        <p>将向{{ destination }}发送一条“告警通道测试”消息，不包含录音或转写正文。请确认这是自己配置的接收会话。</p>
        <button class="btn primary" type="button" :disabled="!canTest" @click="act('test')" data-testid="feishu-test-confirm">确认发送测试通知</button>
        <button class="btn ghost" type="button" @click="confirmingTest = false">取消</button>
      </div>
      <p v-if="confirmingClear" class="notification-hint">再次点击“确认停用并清除”，将停用通知并清除通知专用地址与签名密钥；不会删除原多维表格或应用配置，也不会回退继续发送旧通知。</p>
    </form>
    <button class="btn ghost" type="button" :disabled="!!busy" @click="readSettings" data-testid="feishu-refresh">重新读取通知状态</button>
    <p v-if="notice" class="notification-result" :class="noticeKind" role="status" data-testid="feishu-notice">{{ notice }}</p>
    <p v-if="testNotice" class="notification-result" :class="testKind" role="status" data-testid="feishu-test-notice">{{ testNotice }}</p>
    <p class="notification-hint">消息可能包含主播标识、开播标题或故障摘要。程序、电脑或网络整体停止时，进程内通知可能无法发出；仍需定期检查看板。</p>
    <details class="other-notifications">
      <summary>想使用其他告警通道？</summary>
      <p>当前内置飞书通知。其他平台可由使用者自行研究适配或搭配独立监控，现阶段提供接入思路，不包含现成实现。</p>
      <a href="https://github.com/wgs2779294031/douyin-live-insight/blob/main/docs/OPERATING_BOUNDARIES.md#其他通道只提供接入思路" target="_blank" rel="noopener">查看其他通道的接入说明</a>
    </details>
  </section>
</template>

<style scoped>
.feishu-notifications { align-self: start; }
.feishu-notifications h3 { margin-bottom: 10px; }
.feishu-notifications form > label:not(.notification-toggle) { display: block; margin: 12px 0 6px; font-size: 12px; color: var(--dim); }
.feishu-notifications .field { width: 100%; min-width: 0; box-sizing: border-box; }
.notification-toggle { display: flex; align-items: center; gap: 8px; margin: 14px 0; font-size: 13px; }
.notification-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 8px; }
.saved-state { font-size: 11px; color: var(--dim-2); }
.notification-hint, .other-notifications { font-size: 12px; color: var(--dim); line-height: 1.8; }
.notification-confirm { padding: 12px; margin: 12px 0; border: 1px solid var(--line-soft); border-radius: 8px; font-size: 13px; line-height: 1.8; }
.notification-confirm .btn { margin-right: 8px; }
.notification-result { margin: 10px 0; font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; }
.notification-result.success { color: var(--green); }
.notification-result.warning { color: var(--amber, #ad7615); }
.other-notifications { margin-top: 14px; }
.other-notifications summary { cursor: pointer; }
</style>
