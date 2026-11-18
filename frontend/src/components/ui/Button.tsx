import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'sm' | 'md'

const VARIANT: Record<Variant, string> = {
  primary:
    'border border-transparent bg-[var(--color-ink)] text-[var(--color-surface)] hover:opacity-90 disabled:opacity-40',
  secondary:
    'border border-[var(--color-border-strong)] bg-[var(--color-surface-raised)] text-[var(--color-ink)] hover:bg-[var(--color-surface-overlay)] hover:border-[var(--color-ink-subtle)] disabled:opacity-40',
  ghost:
    'border border-transparent text-[var(--color-ink-muted)] hover:bg-[var(--color-surface-muted)] hover:text-[var(--color-ink)] disabled:opacity-40',
  danger:
    'border border-transparent bg-[var(--color-danger)] text-white hover:brightness-110 disabled:opacity-40',
}

const SIZE: Record<Size, string> = {
  sm: 'h-7 gap-1.5 rounded-[var(--radius-sm)] px-2.5 text-[12px]',
  md: 'h-9 gap-2 rounded-[var(--radius-md)] px-3.5 text-[13px]',
}

export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: Variant
    size?: Size
    children?: ReactNode
  }
>(function Button(
  {
    variant = 'secondary',
    size = 'md',
    className = '',
    children,
    type = 'button',
    ...props
  },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={`inline-flex items-center justify-center font-medium tracking-[-0.01em] transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)] ${VARIANT[variant]} ${SIZE[size]} ${className}`}
      {...props}
    >
      {children}
    </button>
  )
})
