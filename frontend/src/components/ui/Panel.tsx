import type { ReactNode } from 'react'

export function Panel({
  children,
  className = '',
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <section
      className={`overflow-hidden rounded-[var(--radius-xl)] border border-[var(--color-border)] bg-[var(--color-surface-raised)] shadow-[var(--shadow-panel)] ${className}`}
    >
      {children}
    </section>
  )
}

export function PanelHeader({
  title,
  actions,
}: {
  title: string
  actions?: ReactNode
}) {
  return (
    <div className="flex h-11 items-center justify-between gap-3 border-b border-[var(--color-border)] px-4">
      <h2 className="text-[13px] font-medium tracking-[-0.01em] text-[var(--color-ink)]">
        {title}
      </h2>
      {actions ? (
        <div className="flex items-center gap-2">{actions}</div>
      ) : null}
    </div>
  )
}

export function PanelBody({
  children,
  className = '',
}: {
  children: ReactNode
  className?: string
}) {
  return <div className={`p-4 ${className}`}>{children}</div>
}
