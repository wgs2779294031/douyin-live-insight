<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'

/**
 * 折线图（纯 SVG，无图表库）。
 * 文字不画在 SVG 里 —— viewBox 用 preserveAspectRatio="none" 拉伸时会把字拉变形，
 * 峰值/最新值改用 HTML 绝对定位叠在角落。
 */
const props = defineProps({
  // [{ name, color, points: [[ts, value], ...] }]
  series: { type: Array, default: () => [] },
  height: { type: Number, default: 150 },
  gap: { type: Number, default: 120 },     // 断档超过这么多秒不连线
  yMinZero: { type: Boolean, default: true }
})

const W = 1000
const H = 100

// 窄屏（手机）：卡片宽度只剩 300 上下，原高度会把折线压成一条带，
// 波动看不出来。给一个下限，宁可多占 30px 也要能看出趋势。
const narrow = ref(false)
function syncNarrow() {
  narrow.value = typeof window !== 'undefined' && window.innerWidth <= 640
}
onMounted(() => {
  syncNarrow()
  window.addEventListener('resize', syncNarrow)
})
onUnmounted(() => window.removeEventListener('resize', syncNarrow))
const pxHeight = computed(() => (narrow.value ? Math.max(props.height, 150) : props.height))

const bounds = computed(() => {
  const all = []
  for (const s of props.series) for (const p of (s.points || [])) all.push(p)
  if (!all.length) return null
  let t0 = Infinity, t1 = -Infinity, v0 = Infinity, v1 = -Infinity
  for (const [t, v] of all) {
    if (typeof t !== 'number' || typeof v !== 'number') continue
    if (t < t0) t0 = t
    if (t > t1) t1 = t
    if (v < v0) v0 = v
    if (v > v1) v1 = v
  }
  if (!isFinite(t0)) return null
  if (props.yMinZero) v0 = Math.min(0, v0)
  if (v1 === v0) v1 = v0 + 1
  if (t1 === t0) t1 = t0 + 1
  return { t0, t1, v0, v1 }
})

function pathOf(points) {
  const b = bounds.value
  if (!b) return ''
  const X = (t) => ((t - b.t0) / (b.t1 - b.t0)) * W
  const Y = (v) => H - ((v - b.v0) / (b.v1 - b.v0)) * H
  const pts = (points || []).filter((p) => typeof p[0] === 'number' && typeof p[1] === 'number')
  let d = ''
  let prev = null
  for (const p of pts) {
    // 断档超过 gap 秒（场次切换/开播前）就断开，不画斜线连过去
    const cmd = (prev === null || p[0] - prev > props.gap) ? 'M' : 'L'
    d += cmd + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)
    prev = p[0]
  }
  return d
}

const paths = computed(() =>
  props.series.map((s) => ({
    name: s.name,
    color: s.color || '#4c8dff',
    d: pathOf(s.points),
    last: (s.points || []).filter((p) => typeof p[1] === 'number').slice(-1)[0] || null
  }))
)

const latest = computed(() => {
  for (const p of paths.value) if (p.last) return p.last[1]
  return null
})
const spans = computed(() => {
  const b = bounds.value
  if (!b) return ''
  const mins = Math.round((b.t1 - b.t0) / 60)
  return mins >= 90 ? (mins / 60).toFixed(1) + ' 小时' : mins + ' 分钟'
})

// y 轴只有 0 / 中值 / 峰三个刻度：没刻度的折线只能看形状、看不出量级
// （2026-10-25 老大问「这图怎么看」时补的）。量程 < 6 就不画中值，免得挤。
const yTicks = computed(() => {
  const b = bounds.value
  if (!b) return []
  const hi = Math.round(b.v1)
  const lo = Math.round(b.v0)
  const out = [{ v: hi, pos: 'top' }]
  if (hi - lo >= 6) out.push({ v: Math.round((hi + lo) / 2), pos: 'mid' })
  out.push({ v: lo, pos: 'bottom' })
  return out
})
</script>

<template>
  <div class="chart-wrap" :style="{ height: pxHeight + 'px', position: 'relative' }">
    <div v-if="!bounds" class="empty" style="padding: 0; height: 100%; display: grid; place-items: center">
      暂无数据点
    </div>
    <svg
      v-else
      class="chart"
      :viewBox="`0 0 ${W} ${H}`"
      preserveAspectRatio="none"
      :style="{ height: pxHeight + 'px', width: '100%' }"
    >
      <line class="grid-line" x1="0" :y1="H * 0.25" :x2="W" :y2="H * 0.25" />
      <line class="grid-line" x1="0" :y1="H * 0.5" :x2="W" :y2="H * 0.5" />
      <line class="grid-line" x1="0" :y1="H * 0.75" :x2="W" :y2="H * 0.75" />
      <path
        v-for="p in paths"
        :key="'l' + p.name"
        class="line"
        :d="p.d"
        :stroke="p.color"
        vector-effect="non-scaling-stroke"
      />
    </svg>

    <!-- y 轴刻度：贴着左侧，带底色遮住穿过的线，宽度只占 20 来像素 -->
    <div
      v-for="tick in yTicks"
      :key="'y' + tick.pos"
      class="y-tick"
      :style="tick.pos === 'top' ? 'top:0'
        : tick.pos === 'mid' ? 'top:50%;transform:translateY(-50%)'
        : 'bottom:0'"
    >{{ tick.v }}</div>

    <!-- 峰值不再单独标注：y 轴顶端那个刻度就是峰值，两处并列只会互相打岔 -->
    <div
      v-if="bounds"
      style="position: absolute; top: 2px; right: 4px; font-size: 11px; color: var(--dim-2)"
    >
      最新 <b style="color: var(--ink-2)">{{ latest ?? '—' }}</b>
      <span v-if="!narrow"> · 跨度 {{ spans }}</span>
    </div>
  </div>
</template>
