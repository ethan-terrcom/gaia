import { useEffect } from 'react'
import { useNavigate } from 'react-router'

const ROUTES: Record<string, string> = {
  d: '/',
  t: '/tickets',
  r: '/runs',
  a: '/approvals',
  p: '/prs',
  w: '/worktrees',
  s: '/settings',
}

const SEQUENCE_TIMEOUT_MS = 1_000

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false
  }
  const tag = target.tagName
  return (
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    target.isContentEditable
  )
}

export function useHotkeys(): void {
  const navigate = useNavigate()

  useEffect(() => {
    let gPressedAt = 0

    function onKeyDown(event: KeyboardEvent): void {
      if (event.ctrlKey || event.metaKey || event.altKey) {
        return
      }
      if (isEditable(event.target)) {
        return
      }

      if (event.key === 'g') {
        gPressedAt = Date.now()
        return
      }

      if (event.key === '/') {
        event.preventDefault()
        document.querySelector<HTMLElement>('[data-search-input]')?.focus()
        return
      }

      const route = ROUTES[event.key]
      if (route && Date.now() - gPressedAt <= SEQUENCE_TIMEOUT_MS) {
        event.preventDefault()
        navigate(route)
      }
      gPressedAt = 0
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [navigate])
}
