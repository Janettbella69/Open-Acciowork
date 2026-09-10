import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const proxy = {
  '/api/acciowork': {
    target: process.env.ACCIOWORK_EVAL_API_TARGET || 'http://127.0.0.1:8101',
    changeOrigin: true,
  },
}

export default defineConfig({
  base: '/',
  plugins: [react(), tailwindcss()],
  server: { host: '127.0.0.1', port: 5201, strictPort: true, proxy },
  preview: { host: '127.0.0.1', port: 5201, strictPort: true, proxy },
})
