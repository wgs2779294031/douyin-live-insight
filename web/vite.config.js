import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发态：Vite 5173，API 与 SSE 都代理到看板 8765（同源，免 CORS）
// 生产态：npm run build -> web/dist，由 assist_board_server.py 静态托管
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        // SSE 不能被缓冲：http-proxy 默认直通，这里显式关掉压缩相关头
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            delete proxyRes.headers['content-encoding']
          })
        }
      }
    }
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    chunkSizeWarningLimit: 700
  }
})
