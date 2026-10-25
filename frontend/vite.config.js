import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const BACKEND_URL = process.env.VITE_BACKEND_URL || 'http://localhost:8081'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: '0.0.0.0',
    proxy: {
      '/api': { target: BACKEND_URL, changeOrigin: true },
      '/upload': { target: BACKEND_URL, changeOrigin: true },
      '/status': { target: BACKEND_URL, changeOrigin: true },
      '/sample_oas': { target: BACKEND_URL, changeOrigin: true },
      '/github': { target: BACKEND_URL, changeOrigin: true },
      '/download_tests': { target: BACKEND_URL, changeOrigin: true },
      '/download_suite': { target: BACKEND_URL, changeOrigin: true },
      '/stream_scenario': { target: BACKEND_URL, changeOrigin: true },
      '/stream_suite': { target: BACKEND_URL, changeOrigin: true },
      '/execute_single': { target: BACKEND_URL, changeOrigin: true },
      '/docs': { target: BACKEND_URL, changeOrigin: true },
      '/openapi.json': { target: BACKEND_URL, changeOrigin: true },
    },
  },
})
