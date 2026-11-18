import type { ReactNode } from 'react'

/**
 * Consistent label + control stack so every form row shares the same rhythm
 * and text boxes line up across grids.
 */
export function Field({
  label,
  htmlFor,
  hint,
  error,
  children,
  className = '',
}: {
  label: string
  htmlFor?: string
  hint?: string
  error?: string
  children: ReactNode
  className?: string
}) {
  return (
    <div className={`flex min-w-0 flex-col gap-1.5 ${className}`}>
      <label
        htmlFor={htmlFor}
        className="text-[13px] leading-none font-medium tracking-[-0.01em] text-[var(--color-ink-muted)]"
      >
        {label}
      </label>
      {children}
      {error ? (
        <p
          className="text-[12px] leading-snug text-[var(--color-danger)]"
          role="alert"
        >
          {error}
        </p>
      ) : hint ? (
        <p className="text-[12px] leading-snug text-[var(--color-ink-subtle)]">
          {hint}
        </p>
      ) : null}
    </div>
  )
}

export function FieldGrid({
  children,
  cols = 2,
  className = '',
}: {
  children: ReactNode
  cols?: 1 | 2 | 3
  className?: string
}) {
  const colClass =
    cols === 1
      ? 'grid-cols-1'
      : cols === 3
        ? 'grid-cols-1 sm:grid-cols-3'
        : 'grid-cols-1 sm:grid-cols-2'
  return (
    <div
      className={`grid items-start gap-x-3 gap-y-4 ${colClass} ${className}`}
    >
      {children}
    </div>
  )
}
