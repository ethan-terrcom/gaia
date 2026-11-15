import { useEffect, useState } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router'
import { makeQueryClient } from './api/queryClient'
import { getToken } from './api/token'
import { router } from './routes'

function ConnectScreen() {
  return (
    <main className="p-8">
      <h1 className="mb-2 text-2xl font-semibold">Gaia</h1>
      <p>
        Run <code>gaia ui</code> in a terminal and open the link it prints.
      </p>
      <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
        The link carries a one-time token in the URL fragment. Gaia stores it in
        this tab&apos;s session and strips it from the address bar.
      </p>
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
