import { fileURLToPath } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig(({ mode }) => ({
  plugins: [vue()],
  resolve: {
    alias: {
      '#workbench-api': fileURLToPath(new URL(mode === 'pages' ? './preview/api.js' : './src/api.js', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: process.env.DEMO_API_ORIGIN || 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
}))
