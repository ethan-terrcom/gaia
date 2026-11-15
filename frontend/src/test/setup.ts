import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => {
  cleanup()
})

if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  })
}

// jsdom exposes Node's undici Request, which rejects relative URLs. The browser
// accepts them, and openapi-fetch intentionally builds `/api/...` URLs from
// `baseUrl: ""`, so tests provide the browser behaviour.
const NativeRequest = globalThis.Request
class RelativeRequest extends (NativeRequest as typeof Request) {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    if (typeof input === 'string') {
      super(new URL(input, 'http://127.0.0.1:8765'), init)
    } else {
      super(input, init)
    }
  }
}

Object.defineProperty(globalThis, 'Request', {
  configurable: true,
  writable: true,
  value: RelativeRequest,
})
