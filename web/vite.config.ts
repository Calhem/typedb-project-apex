import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    host: '127.0.0.1',
    // In development the API runs separately (uv run python -m apex.serve); the dev server
    // forwards /api to it, so the browser only ever talks to one address.
    proxy: { '/api': process.env.APEX_API ?? 'http://127.0.0.1:8000' },
  },
})
