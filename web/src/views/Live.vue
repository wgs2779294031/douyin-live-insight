<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useBoard, api } from '../store'
import KpiCard from '../components/KpiCard.vue'
import TrendChart from '../components/TrendChart.vue'
import DanmuFeed from '../components/DanmuFeed.vue'

const board = useBoard()
const active = ref('')

// 采集存活总览：八路里几路停写（2026-10-22 全停 7.5 小时没人发现的教训）
const health = ref(null)
let healthTimer = null
onMounted(async () => {
  const tick = async () => {
    try {
      health.value = await api.anchorsHealth()
    } catch (e) { /* 接口失败不打扰主视图 */ }
  }
  tick()
  healthTimer = setInterval(tick, 30000)
})
onUnmounted(() => clearInterval(healthTimer))

const collectProblem = computed(() => {
  const h = health.value
  if (!h) return null
  const n = (h.stalled || 0) + (h.stopped || 0)
  if (!n) return null
  return { n, stalled: h.stalled || 0, stopped: h.stopped || 0, problems: h.problems || [] }
})

const snap = computed(() => board.snapshot)
const tags = computed(() => (snap.value && snap.value.tags) || [])

watch(
  tags,
  (list) => {
    if (!list.length) {
      active.value = ''
      return
    }
    if (!active.value || !list.some((x) => x.tag === active.value)) {
      const live = list.find((x) => x.live && x.live[0])
      active.value = (live || list[0]).tag
    }
  },
  { immediate: true }
)

const t = computed(() => tags.value.find((x) => x.tag === active.value) || null)
const failedProbes = computed(() => tags.value.filter((item) => item.probe_state === 'error'))

const probeNotice = computed(() => {
  const item = t.value
  if (!item || item.probe_state === 'live' || item.probe_state === 'offline') return ''
  const dataActive = item.live[0]
  if (item.probe_state === 'error') {
    return (dataActive ? '探测异常，近期弹幕或人气数据仍有活动。' : '探测异常，开播状态未知。')
      + '请检查监控浏览器的登录状态和验证提示。'
  }
  if (item.probe_state === 'stale') {
    return dataActive ? '开播探测已超过 5 分钟，近期弹幕或人气数据仍有活动。'
      : '开播探测已超过 5 分钟，当前开播状态未知。'
  }
  if (item.probe_state === 'missing' && !dataActive && !item.last_ts) {
    return '等待首次采集数据，开播状态待确认。'
  }
  return dataActive ? '尚无有效开播探测，近期弹幕或人气数据仍有活动。'
    : '尚无有效开播探测，开播状态待确认。'
})

function fmtN(n) {
  if (n == null) return '—'
  if (n >= 100000000) return (n / 100000000).toFixed(1) + '亿'
  if (n >= 10000) return (n / 10000).toFixed(1) + 'w'
  return String(n)
}
function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toTimeString().slice(0, 8)
}

const onlineSeries = computed(() => {
  if (!t.value) return []
  return [{ name: '在线', color: '#4c8dff', points: t.value.online_series || [] }]
})

const isCold = computed(() => t.value && t.value.cold && t.value.cold.state === 'cold')
const questions = computed(() => (t.value && t.value.questions) || [])
const suggestions = computed(() => (t.value && t.value.suggestions) || [])
const feed = computed(() => (t.value && t.value.recent_danmaku) || [])
</script>

<template>
  <div v-if="!snap" class="waiting">
    <div><span class="spin"></span></div>
    <div class="small">正在连接看板服务…</div>
  </div>

  <div v-else-if="snap.roster_error" class="waiting">
    <div class="big">监控名册读取异常</div>
    <div class="small">{{ snap.roster_error }}</div>
  </div>

  <div v-else-if="!tags.length" class="waiting">
    <div class="big">暂无启用监控的主播</div>
    <div class="small">可在「主播」页查看和启用主播。</div>
  </div>

  <template v-else>
    <div class="tabs" style="margin-bottom: 14px">
      <button
        v-for="x in tags"
        :key="x.tag"
        class="tab"
        :class="{ on: x.tag === active }"
        @click="active = x.tag"
      >
        {{ x.tag }}
        <span v-if="x.live && x.live[0]" class="dot on live-dot"></span>
      </button>
    </div>

    <div v-if="collectProblem" class="banner err">
      采集异常：{{ collectProblem.stalled }} 路探测停写、{{ collectProblem.stopped }} 路未运行
      （{{ collectProblem.problems.map(p => p.tag).slice(0, 4).join('、') }}{{ collectProblem.problems.length > 4 ? ' 等' : '' }}）
      —— 去「主播」页看明细
    </div>

    <div v-if="failedProbes.length && t?.probe_state !== 'error'" class="banner err">
      {{ failedProbes.length }} 路开播探测异常（{{ failedProbes.map((item) => item.tag).join('、') }}），请检查监控浏览器的登录状态和验证提示。
    </div>
    <div v-if="probeNotice" class="banner" :class="{ err: t.probe_state === 'error' }">
      {{ probeNotice }}
    </div>

    <template v-if="t && !t.live[0]">
      <div v-if="t.stale" class="banner err">
        数据停滞 · 数据显示至 {{ fmtTime(t.last_ts) }}（已 {{ Math.round((t.data_age || 0) / 60) }} 分钟无新数据）。
        主播可能正在直播而我们没在采，检查采集进程；看板侧不做臆测。
      </div>
      <div v-else-if="t.probe_state === 'offline'" class="banner">
        未开播（判定依据：{{ t.live[1] }}{{ t.last_ts ? ' · 最近活动 ' + fmtTime(t.last_ts) : '' }}）
      </div>

      <div class="grid-kpi">
        <KpiCard k="今日弹幕" :v="fmtN(t.totals.danmaku)" />
        <KpiCard k="今日进入" :v="fmtN(t.totals.enter)" />
        <KpiCard k="今日点赞" :v="fmtN(t.totals.like)" />
        <KpiCard k="今日礼物" :v="fmtN(t.totals.gift)" />
      </div>
      <div class="card" style="margin-top: 14px">
        <h3>在线人数走势<span class="sub">近 3 分钟</span></h3>
        <TrendChart :series="onlineSeries" :height="120" />
      </div>
    </template>

    <template v-else-if="t">
      <div class="grid-kpi">
        <KpiCard
          k="当前在线"
          :v="fmtN(t.online)"
          :s="t.live[1]"
          tone="blue"
        />
        <KpiCard k="累计观看 PV" :v="fmtN(t.total_pv)" s="stats 流 room_user_seq" />
        <KpiCard k="弹幕流速" :v="t.rate_per_min + ' 条/分'" s="近 5 分钟均值" />
        <KpiCard
          k="冷场预警"
          :v="isCold ? '冷场中' : '正常'"
          :s="isCold ? '' : (t.cold.ts ? '上次冷场 ' + fmtTime(t.cold.ts) : '今日暂无冷场事件')"
          :tone="isCold ? 'warn' : 'ok'"
        >
          <div v-if="isCold" class="feed" style="max-height: 84px; margin-top: 6px">
            <div v-for="(line, i) in t.cold.lines" :key="i" style="color: var(--amber); font-size: 12px; padding: 2px 0">
              {{ line }}
            </div>
          </div>
        </KpiCard>
      </div>

      <div class="card" style="margin: 14px 0">
        <h3>在线人数走势<span class="sub">近 3 分钟 · 断档不连线</span></h3>
        <TrendChart :series="onlineSeries" :height="150" />
      </div>

      <div class="card" style="margin-bottom: 14px; display: flex; gap: 22px; flex-wrap: wrap">
        <span class="meta" style="font-size: 12px; color: var(--dim)">
          弹幕 <b style="color: var(--ink); font-size: 15px; margin-left: 4px">{{ fmtN(t.totals.danmaku) }}</b>
        </span>
        <span class="meta" style="font-size: 12px; color: var(--dim)">
          进入 <b style="color: var(--ink); font-size: 15px; margin-left: 4px">{{ fmtN(t.totals.enter) }}</b>
        </span>
        <span class="meta" style="font-size: 12px; color: var(--dim)">
          点赞 <b style="color: var(--ink); font-size: 15px; margin-left: 4px">{{ fmtN(t.totals.like) }}</b>
        </span>
        <span class="meta" style="font-size: 12px; color: var(--dim)">
          礼物 <b style="color: var(--ink); font-size: 15px; margin-left: 4px">{{ fmtN(t.totals.gift) }}</b>
        </span>
        <span v-if="t.portrait_ts" class="meta" style="font-size: 12px; color: var(--dim)">
          画像快照 <b style="color: var(--ink); font-size: 15px; margin-left: 4px">{{ fmtTime(t.portrait_ts) }}</b>
        </span>
        <span v-if="t.consumption_intent" class="meta" style="font-size: 12px; color: var(--dim)">
          消费意向 <b style="color: var(--ink); font-size: 15px; margin-left: 4px">{{ t.consumption_intent.level || '—' }}</b>
        </span>
      </div>

      <div class="grid-3">
        <div class="stack">
          <div class="card">
            <h3>弹幕流<span class="feed-hint">最新在上 · 悬停暂停跟随</span></h3>
            <DanmuFeed :items="feed" :height="360" :tabs="false" />
          </div>
        </div>
        <div class="stack">
          <div class="card">
            <h3>观众在问什么<span class="sub">提问聚类 Top5</span></h3>
            <ul v-if="questions.length" class="list">
              <li v-for="(q, i) in questions" :key="i">
                <span class="q">{{ q.topic || '—' }}</span>
                <span class="n">×{{ q.count || 0 }}</span>
                <div v-if="q.examples && q.examples.length" class="ex">如：{{ q.examples[0] }}</div>
              </li>
            </ul>
            <div v-else class="empty">暂无画像快照</div>
          </div>

          <div class="card">
            <h3>LLM 给主播的建议</h3>
            <ul v-if="suggestions.length" class="list">
              <li v-for="(s, i) in suggestions" :key="i" class="arrow">{{ s }}</li>
            </ul>
            <div v-else class="empty">暂无建议</div>
          </div>

          <div v-if="t.activity_layers" class="card">
            <h3>活跃分层</h3>
            <div style="display: flex; gap: 16px; font-size: 12px; color: var(--dim)">
              <span>高 <b style="color: var(--ink)">{{ (t.activity_layers.high || []).length }}</b></span>
              <span>中 <b style="color: var(--ink)">{{ (t.activity_layers.mid || []).length }}</b></span>
              <span>低 <b style="color: var(--ink)">{{ (t.activity_layers.low || []).length }}</b></span>
            </div>
          </div>
        </div>
      </div>
    </template>
  </template>
</template>
