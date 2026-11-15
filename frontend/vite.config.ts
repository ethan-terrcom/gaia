/// <reference types="vitest/config" />
import { readFileSync } from 'node:fs'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

type CspFile = { value: string }

const csp: string = (
  JSON.parse(
    readFileSync(new URL('./csp.json', import.meta.url), 'utf8'),
  ) as CspFile
).value

// The production build is the only place the CSP meta tag is allowed. Vite's
// dev server and React refresh inject inline scripts, which this policy blocks.
function cspMeta(): Plugin {
  return {
    name: 'gaia-csp-meta',
    apply: 'build',
    transformIndexHtml() {
      return {
        html: '',
        tags: [
          {
            tag: 'meta',
            attrs: {
              'http-equiv': 'Content-Security-Policy',
              content: csp,
            },
            injectTo: 'head-prepend',
          },
        ],
      }
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), cspMeta()],
  server: {
    // Never expose the dev server beyond this machine (ARCHITECTURE section 6).
    // Must match Settings.ui_origin (doc 02).
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
})
