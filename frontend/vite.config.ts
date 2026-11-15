import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  resolve: {
    // TSP 的代码用 @/ 别名，统一指向 src/tick（我们的代码走相对路径，不冲突）
    alias: { '@': new URL('./src/tick', import.meta.url).pathname },
  },
  plugins: [react(), tailwindcss()],
  // 构建产物挂在 FastAPI 的 /app 下，旧页面保持在 /、/dashboard 等路径
  base: '/app/',
  server: {
    port: 5273,
    proxy: {
      '/api': { target: 'http://127.0.0.1:3021', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
})
