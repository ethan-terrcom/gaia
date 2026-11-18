import type { ReactNode } from 'react'
import { ApiError } from '../api/errors'
import { Button } from './ui/Button'

/**
 * Loading, error and empty states. Every view needs all three (doc 17,
 * "Shared rules"), so they look and read the same everywhere.
 */

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center gap-3 px-1 py-8 text-[13px] text-[var(--color-ink-muted)]"
    >
      <span
        aria-hidden
        className="size-3.5 animate-spin rounded-full border border-[var(--color-border-strong)] border-t-[var(--color-ink)]"
      />
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
      className="rounded-[var(--radius-xl)] border border-[var(--color-danger)]/30 bg-[var(--color-danger-soft)] p-4 text-[13px] text-[var(--color-danger)]"
    >
      <p className="font-medium tracking-[-0.01em]">{message}</p>
      {code ? (
        <p className="mt-1 font-mono text-[11px] opacity-70">{code}</p>
      ) : null}
      {onRetry ? (
        <Button
          variant="secondary"
          size="sm"
          className="mt-3"
          onClick={onRetry}
        >
          Retry
        </Button>
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
    <div className="rounded-[var(--radius-xl)] border border-dashed border-[var(--color-border-strong)] bg-[var(--color-surface-muted)]/50 px-6 py-12 text-center">
      <p className="text-[14px] font-medium tracking-[-0.015em] text-[var(--color-ink)]">
        {title}
      </p>
      {children ? (
        <div className="mx-auto mt-2 max-w-md text-[13px] leading-relaxed text-[var(--color-ink-muted)]">
          {children}
        </div>
      ) : null}
    </div>
  )
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-[22px] leading-tight font-semibold tracking-[-0.03em] text-[var(--color-ink)]">
          {title}
        </h1>
        {description ? (
          <p className="mt-1.5 max-w-2xl text-[13px] leading-relaxed text-[var(--color-ink-muted)]">
            {description}
          </p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      ) : null}
    </header>
  )
}
