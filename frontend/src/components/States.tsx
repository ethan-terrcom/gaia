import type { ReactNode } from 'react'
import { ApiError } from '../api/errors'

/**
 * Loading, error and empty states. Every view needs all three (doc 17,
 * "Shared rules"), so they look and read the same everywhere.
 */

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="p-6 text-sm text-gray-500">
      {label}
    </div>
  )
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown
  onRetry?: () => void
}) {
  const message =
    error instanceof ApiError
      ? error.message
      : 'Something went wrong while loading this page.'
  const code = error instanceof ApiError ? error.code : undefined
  return (
    <div
      role="alert"
      className="m-4 rounded border border-red-300 bg-red-50 p-4 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
    >
      <p>{message}</p>
      {code ? (
        <p className="mt-1 font-mono text-xs opacity-70">{code}</p>
      ) : null}
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded border border-current px-3 py-1"
        >
          Retry
        </button>
      ) : null}
    </div>
  )
}

export function EmptyState({
  title,
  children,
}: {
  title: string
  children?: ReactNode
}) {
  return (
    <div className="m-4 rounded border border-dashed border-gray-300 p-8 text-center dark:border-gray-700">
      <p className="font-medium">{title}</p>
      {children ? (
        <div className="mt-2 text-sm text-gray-500">{children}</div>
      ) : null}
    </div>
  )
}

export function PageHeader({
  title,
  actions,
}: {
  title: string
  actions?: ReactNode
}) {
  return (
    <header className="mb-4 flex items-center justify-between gap-4">
      <h1 className="text-xl font-semibold">{title}</h1>
      {actions ? (
        <div className="flex items-center gap-2">{actions}</div>
      ) : null}
    </header>
  )
}
