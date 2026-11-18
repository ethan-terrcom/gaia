type Option<T extends string> = {
  value: T
  label: string
}

export function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: T
  options: readonly Option<T>[]
  onChange: (value: T) => void
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="inline-flex h-9 items-center rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-surface-muted)] p-0.5"
    >
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(option.value)}
            className={`h-full rounded-[calc(var(--radius-md)-2px)] px-3 text-[12px] font-medium tracking-[-0.01em] transition ${
              active
                ? 'bg-[var(--color-surface-overlay)] text-[var(--color-ink)] shadow-[var(--shadow-soft)]'
                : 'text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]'
            }`}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}
