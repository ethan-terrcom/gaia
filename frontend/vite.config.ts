/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Never expose the dev server beyond this machine (ARCHITECTURE section 6).
    // Must match Settings.ui_origin (doc 02).
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
  },
})
