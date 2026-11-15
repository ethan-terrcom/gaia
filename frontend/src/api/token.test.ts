import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TOKEN_KEY, bootstrapToken, clearToken, getToken } from './token'

describe('bootstrapToken', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    window.history.replaceState(null, '', '/')
  })

  it('stores the hash token in sessionStorage and strips the hash', () => {
    window.history.replaceState(null, '', '/path?from=ui#token=abc123')

    const token = bootstrapToken()

    expect(token).toBe('abc123')
    expect(sessionStorage.getItem(TOKEN_KEY)).toBe('abc123')
    expect(window.location.pathname).toBe('/path')
    expect(window.location.search).toBe('?from=ui')
    expect(window.location.hash).toBe('')
  })

  it('never touches localStorage', () => {
    window.history.replaceState(null, '', '/#token=abc123')
    const setItem = vi.spyOn(Storage.prototype, 'setItem')

    bootstrapToken()

    expect(setItem).toHaveBeenCalled()
    expect(setItem.mock.instances[0]).not.toBe(window.localStorage)
  })

  it('keeps an existing session token when the hash has no token', () => {
    sessionStorage.setItem(TOKEN_KEY, 'existing')

    const token = bootstrapToken()

    expect(token).toBe('existing')
    expect(sessionStorage.getItem(TOKEN_KEY)).toBe('existing')
  })

  it('clears a token from sessionStorage', () => {
    sessionStorage.setItem(TOKEN_KEY, 'abc')
    clearToken()

    expect(getToken()).toBeNull()
  })
})
