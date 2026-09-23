import path from 'node:path'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The JSON API is served same-origin in production (relative "/api/..." paths),
// so the bundle must use relative asset URLs (base "./"). During `npm run dev`
// we proxy /api to a locally running Discord Recall backend instead.
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000'

const apiProxy = {
  '/api': {
    target: apiTarget,
    changeOrigin: true,
  },
}

export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    proxy: apiProxy,
  },
  // `npm run preview` serves the real dist, so it needs the same proxy to be
  // usable for end-to-end checks against a local backend.
  preview: {
    proxy: apiProxy,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    // The entry chunk is React + base-ui + cmdk + react-day-picker + lucide
    // (~550 kB / ~175 kB gzip). The markdown renderer is already split out into
    // the digest-browser/ask-card chunks, so raise the warning limit instead of
    // splitting the shell further.
    chunkSizeWarningLimit: 600,
  },
})
