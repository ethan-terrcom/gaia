import type { ReactNode } from 'react'

/** Standard padded content frame for list/settings pages (not full-bleed splits). */
export function PageShell({
  children,
  className = '',
  wide = false,
}: {
  children: ReactNode
  className?: string
  wide?: boolean
}) {
  return (
    <div
      className={`mx-auto w-full px-5 py-6 md:px-8 ${wide ? 'max-w-[1400px]' : 'max-w-[1200px]'} ${className}`}
    >
      {children}
    </div>
  )
}
