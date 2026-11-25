import { createRouter, createWebHistory } from 'vue-router'
import Live from './views/Live.vue'
import Review from './views/Review.vue'
import Settings from './views/Settings.vue'
import Anchors from './views/Anchors.vue'

const routes = [
  { path: '/', redirect: '/live' },
  { path: '/live', name: 'live', component: Live, meta: { title: '实时' } },
  { path: '/review', name: 'review', component: Review, meta: { title: '复盘' } },
  { path: '/anchors', name: 'anchors', component: Anchors, meta: { title: '主播' } },
  { path: '/settings', name: 'settings', component: Settings, meta: { title: '设置' } },
  { path: '/:pathMatch(.*)*', redirect: '/live' }
]

// history 模式：生产由 assist_board_server 对非 /api 路径回退 index.html
const router = createRouter({
  history: createWebHistory('/'),
  routes
})

router.afterEach((to) => {
  document.title = (to.meta.title ? to.meta.title + ' · ' : '') + '直播辅助看板'
})

export default router
