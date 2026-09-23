import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],

  resolve: {
    alias: {
      // 用 @ 代表 src 目录，import 时不用写 ../../../
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    port: 5173,
    // ★ 必须显式指定，不能靠默认值。
    //
    // Vite 默认的 host 是 "localhost"，在 Windows 上 Node 会把它解析成
    // IPv6 的 ::1 —— 结果是只监听 [::1]:5173，不监听 127.0.0.1:5173。
    // 现象是浏览器用 localhost 能打开，但任何用 127.0.0.1 去连的地方
    // （脚本、Postman、某些工具）都连不上，排查起来很费时间。
    //
    // 写死 127.0.0.1 而不是 true：
    // host: true 会监听 0.0.0.0，同一 WiFi 下的其他人也能访问你的开发服务器。
    // 本地开发没这个必要。
    host: '127.0.0.1',
    // ★ 开发时代理：前端请求 /api/xxx 会被转发到后端 8000 端口。
    //
    // 这样做的好处是【开发时完全不涉及跨域】：
    //   浏览器看到的是 http://localhost:5173/api/v1/auth/login
    //   Vite 在背后转发给 http://127.0.0.1:8000/api/v1/auth/login
    // 浏览器认为前后端同源，所以根本不会发 CORS 预检请求。
    //
    // 注意：这只是开发期的便利。生产环境前端会打包成静态文件，
    // 由 nginx 同时负责托管静态资源和反代 /api（见阶段 11 的 Docker 配置）。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
