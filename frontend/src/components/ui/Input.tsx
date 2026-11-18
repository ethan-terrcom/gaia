import type {
  InputHTMLAttributes,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'

const FIELD_BASE =
  'w-full border border-[var(--color-border-strong)] bg-[var(--color-surface-muted)] text-[var(--color-ink)] outline-none transition-[border-color,box-shadow,background-color] placeholder:text-[var(--color-ink-subtle)] hover:border-[var(--color-ink-subtle)] focus:border-[var(--color-accent)] focus:bg-[var(--color-surface-raised)] focus:ring-2 focus:ring-[var(--color-accent)]/20 disabled:cursor-not-allowed disabled:opacity-50'

export function Input({
  className = '',
  ...props
}: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input className={`gaia-control ${FIELD_BASE} ${className}`} {...props} />
  )
}

export function Textarea({
  className = '',
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={`gaia-control-multiline ${FIELD_BASE} resize-y ${className}`}
      {...props}
    />
  )
}

export function Select({
  className = '',
  children,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={`gaia-control ${FIELD_BASE} appearance-none bg-[length:12px] bg-[right_0.65rem_center] bg-no-repeat pr-8 ${className}`}
      style={{
        backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238a8a8a' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E")`,
      }}
      {...props}
    >
      {children}
    </select>
  )
}

/** @deprecated Prefer Field label prop */
export const fieldLabelClass =
  'text-[13px] leading-none font-medium tracking-[-0.01em] text-[var(--color-ink-muted)]'

export const fieldHintClass =
  'text-[12px] leading-snug text-[var(--color-ink-subtle)]'
