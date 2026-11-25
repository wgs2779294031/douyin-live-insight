<script setup>
import { computed } from 'vue'

const props = defineProps({
  k: { type: String, default: '' },
  v: { type: [String, Number], default: '—' },
  s: { type: String, default: '' },
  tone: { type: String, default: '' },   // '' | blue | green | warn | ok
  icon: { type: String, default: '' }
})

const cls = computed(() => ['card', 'kpi', props.tone].filter(Boolean).join(' '))
const vCls = computed(() => ['v', props.tone === 'warn' ? '' : props.tone].filter(Boolean).join(' '))
</script>

<template>
  <div :class="cls">
    <div class="k">
      {{ k }}<span v-if="icon" style="margin-left: 4px; opacity: .7">{{ icon }}</span>
    </div>
    <div :class="vCls">{{ v }}</div>
    <div v-if="s" class="s">{{ s }}</div>
    <slot />
  </div>
</template>
