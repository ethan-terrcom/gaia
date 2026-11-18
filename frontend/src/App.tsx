import { useEffect, useState } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router'
import { makeQueryClient } from './api/queryClient'
import { getToken } from './api/token'
import { router } from './routes'

function ConnectScreen() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-[var(--color-surface)] px-6 text-[var(--color-ink)]">
      <div className="w-full max-w-md rounded-[var(--radius-xl)] border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-8 shadow-[var(--shadow-panel)]">
        <p className="text-[12px] font-medium tracking-[0.08em] text-[var(--color-ink-subtle)] uppercase">
          Local control plane
        </p>
        <h1 className="mt-3 text-[28px] font-semibold tracking-[-0.04em]">
          Gaia
        </h1>
        <p className="mt-3 text-[14px] leading-relaxed text-[var(--color-ink-muted)]">
          Run{' '}
          <code className="rounded-[var(--radius-sm)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[12px] text-[var(--color-ink)]">
            gaia ui
          </code>{' '}
          in a terminal and open the link it prints.
        </p>
        <p className="mt-4 text-[12px] leading-relaxed text-[var(--color-ink-subtle)]">
          The link carries a one-time token in the URL fragment. Gaia stores it
          in this tab&apos;s session and strips it from the address bar.
        </p>
      </div>
    </main>
  )
}

export default function App() {
  const [queryClient] = useState(makeQueryClient)
  const [hasToken, setHasToken] = useState(() => getToken() !== null)

  useEffect(() => {
    function onUnauthorized(): void {
      setHasToken(false)
    }
    window.addEventListener('gaia:unauthorized', onUnauthorized)
    return () => window.removeEventListener('gaia:unauthorized', onUnauthorized)
  }, [])

  if (!hasToken) {
    return <ConnectScreen />
  }

  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  )
}
