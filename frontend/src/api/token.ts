export const TOKEN_KEY = 'gaia.token'

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY)
}

export function bootstrapToken(win: Window = window): string | null {
  const hash = win.location.hash
  if (!hash.startsWith('#')) {
    return getToken()
  }
  const params = new URLSearchParams(hash.slice(1))
  const token = params.get('token')
  if (token !== null && token !== '') {
    win.sessionStorage.setItem(TOKEN_KEY, token)
    win.history.replaceState(
      null,
      '',
      win.location.pathname + win.location.search,
    )
    return token
  }
  return getToken()
}
