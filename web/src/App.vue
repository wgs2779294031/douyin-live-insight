<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useBoard, initializeAccess, login, logout, startSSE } from './store'

const route = useRoute()
const board = useBoard()

const username = ref('admin')
const password = ref('')
const err = ref('')
const busy = ref(false)
const showPwd = ref(false)

const navs = [
  { to: '/live', label: '实时', ico: '◉' },
  { to: '/review', label: '复盘', ico: '▦' },
  { to: '/anchors', label: '主播', ico: '☰' },
  { to: '/settings', label: '设置', ico: '⚙' }
]

function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toTimeString().slice(0, 8)
}

const connText = computed(() => ({
  open: '已连接',
  error: '断开重连中',
  idle: '未连接'
}[board.conn] || ''))

const snapshotAt = computed(() => board.snapshot ? fmtTime(board.snapshot.now_ts) : '—')
const snapshotDate = computed(() => board.snapshot ? board.snapshot.date : '')

async function doLogin() {
  if (busy.value) return
  busy.value = true
  err.value = ''
  try {
    await login(username.value.trim() || 'admin', password.value)
    password.value = ''
    startSSE()
  } catch (e) {
    err.value = e.message || '登录失败'
  } finally {
    busy.value = false
  }
}

async function doLogout() {
  await logout()
}

async function connect() {
  if (await initializeAccess()) startSSE()
}

onMounted(connect)
</script>

<template>
  <div v-if="['loading', 'error', 'expired'].includes(board.accessState)" class="mask" data-testid="access-status">
    <div class="login">
      <div class="logo">M</div>
      <h2>{{ board.accessState === 'loading' ? '正在连接看板' : (board.accessMode === 'local' ? '本地看板连接待恢复' : '暂时无法连接看板') }}</h2>
      <p class="hint">{{ board.accessState === 'loading' ? '正在确认访问模式…' : board.accessError }}</p>
      <button v-if="board.accessState !== 'loading'" class="btn primary" @click="connect" data-testid="reconnect-local">重新连接</button>
    </div>
  </div>

  <!-- Account mode is explicitly selected by the server. -->
  <div v-else-if="board.accessState === 'login' && board.accessMode === 'account'" class="mask">
    <form class="login" @submit.prevent="doLogin">
      <div class="logo">M</div>
      <h2>直播辅助看板</h2>
      <div class="hint">内部工具 · 请用分配的账号登录</div>
      <label for="u">用户名</label>
      <input id="u" v-model="username" autocomplete="username" spellcheck="false" />
      <label for="p">密码</label>
      <div class="pw">
        <input
          id="p"
          v-model="password"
          :type="showPwd ? 'text' : 'password'"
          autocomplete="current-password"
        />
        <button type="button" class="pw-toggle" @click="showPwd = !showPwd">
          {{ showPwd ? '隐藏' : '显示' }}
        </button>
      </div>
      <button class="btn primary" type="submit" :disabled="busy">
        {{ busy ? '登录中…' : '登录' }}
      </button>
      <div class="err">{{ err }}</div>
    </form>
  </div>

  <div v-else-if="board.accessState === 'ready'" class="shell">
    <nav class="nav">
      <div class="logo">M</div>
      <router-link
        v-for="n in navs"
        :key="n.to"
        :to="n.to"
        class="item"
        :class="{ on: route.path === n.to }"
      >
        <span class="ico">{{ n.ico }}</span>
        <span>{{ n.label }}</span>
      </router-link>
      <div class="spacer"></div>
      <div class="who">{{ board.user }}</div>
    </nav>

    <div class="main">
      <div class="topbar">
        <h1>{{ route.meta.title || '看板' }}</h1>
        <span class="badge">
          <span class="dot" :class="board.conn === 'open' ? 'on' : (board.conn === 'error' ? 'off' : 'wait')"></span>
          {{ connText }}
        </span>
        <span class="meta" v-if="snapshotDate">日期 {{ snapshotDate }} · 快照 {{ snapshotAt }}</span>
        <span class="spacer"></span>
        <span class="meta" v-if="board.accessMode === 'local'" data-testid="local-mode">本地模式 · 无需看板账号</span>
        <span class="meta" v-else-if="board.role">{{ board.user }}（{{ board.role }}）</span>
        <button v-if="board.accessMode === 'account'" class="btn ghost" @click="doLogout">退出</button>
      </div>
      <div class="content">
        <router-view />
      </div>
    </div>
  </div>
</template>
