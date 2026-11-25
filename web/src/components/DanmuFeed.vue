<script setup>
import { ref, computed, watch, nextTick } from 'vue'

/**
 * 弹幕流：**最新在最上面**，鼠标悬停时锁住当前视觉位置（不被新帧顶走），
 * 移开自动回到顶部跟随。
 *
 * 为什么不是「新消息追加到底部再滚到底」：直播辅助场景要一眼看到刚进来的
 * 话，滚到底还要手动回拉；倒序 + 钉顶才不用动手。
 */
const props = defineProps({
  items: { type: Array, default: () => [] },   // 后端给的是时间正序（默认/只看互动）
  // 「全部事件」用的另一份数据（含 enter/like）。不传则切 tab 也只换筛选、不换数据源。
  // 为什么不在前端对 items 做全量筛选：后端只给最后 200 条，其中 95% 是进入/点赞，
  // 筛完可能一条不剩 —— 所以"互动流"必须后端先切好再给。
  allItems: { type: Array, default: null },
  height: { type: Number, default: 300 },
  limit: { type: Number, default: 200 },
  // 实时页的 feed 后端就只收 danmaku/gift，两个tab数字一样，没必要显示
  tabs: { type: Boolean, default: true }
})

const el = ref(null)
const paused = ref(false)
// 默认「只看互动」：一场直播里 95% 事件是「进入直播间/点赞」，真弹幕会被埋掉
// （2026-10-25 老大反馈）。切「全部」才看得到 enter/like。
const mode = ref('interactive')
let prevTop = 0
let prevH = 0

function isInteractive(d) {
  const t = d.type || (d.gift ? 'gift' : 'danmaku')
  return t === 'danmaku' || t === 'gift'
}

const kept = computed(() => {
  const arr = (mode.value === 'all' && props.allItems) ? props.allItems : (props.items || [])
  return mode.value === 'all' ? arr : arr.filter(isInteractive)
})

const interactiveCount = computed(
  () => (props.items || []).filter(isInteractive).length)

const rows = computed(() => {
  const arr = kept.value.slice(-props.limit)
  return arr.reverse()
})

function setMode(m) {
  mode.value = m
  if (el.value) el.value.scrollTop = 0
}

function fmtTime(ts) {
  if (!ts) return '--:--:--'
  return new Date(ts * 1000).toTimeString().slice(0, 8)
}

// flush:'pre' —— 在 DOM 更新前记下滚动状态，更新后按差值修正
watch(
  () => props.items,
  () => {
    const node = el.value
    if (!node) return
    prevTop = node.scrollTop
    prevH = node.scrollHeight
    nextTick(() => {
      const n = el.value
      if (!n) return
      if (paused.value || prevTop > 4) {
        // 新内容插在顶部，把新增高度补进 scrollTop，视觉上不动
        n.scrollTop = prevTop + (n.scrollHeight - prevH)
      } else {
        n.scrollTop = 0
      }
    })
  },
  { flush: 'pre', deep: false }
)

function onEnter() {
  paused.value = true
}
function onLeave() {
  paused.value = false
  if (el.value) el.value.scrollTop = 0
}
</script>

<template>
  <div class="feed-wrap">
    <div v-if="tabs" class="feed-tabs">
      <button
        type="button"
        :class="{ on: mode === 'interactive' }"
        @click="setMode('interactive')"
      >只看互动 <span class="n">{{ interactiveCount }}</span></button>
      <button
        type="button"
        :class="{ on: mode === 'all' }"
        @click="setMode('all')"
      >全部事件</button>
    </div>
    <div
      ref="el"
      class="feed"
      :style="{ '--feed-h': height + 'px' }"
      @mouseenter="onEnter"
      @mouseleave="onLeave"
    >
      <div v-if="!rows.length" class="empty">暂无弹幕</div>
      <div
        v-for="(d, i) in rows"
        :key="d.seq != null ? 's' + d.seq : 'i' + i + '-' + d.ts"
        class="row"
        :class="{ gift: d.gift, quiet: d.type && d.type !== 'danmaku' && d.type !== 'gift' }"
      >
        <span class="t">{{ fmtTime(d.ts) }}</span>
        <span class="u">{{ d.uname || '匿名' }}</span>
        <span class="x">{{ d.text }}</span>
      </div>
    </div>
  </div>
</template>
