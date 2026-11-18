/**
 * Status → tone mapping for :func:`StatusBadge`. Unknown values are neutral, so a
 * new backend status never breaks a view.
 */

export type Tone = 'neutral' | 'info' | 'good' | 'warn' | 'bad'

const TONES: Record<string, Tone> = {
  passed: 'good',
  failed: 'bad',
  pending: 'info',
  unknown: 'neutral',
  approved: 'good',
  changes_requested: 'warn',
  open: 'info',
  merged: 'good',
  closed: 'neutral',
  planning: 'info',
  implementing: 'info',
  reviewing: 'info',
  pr_open: 'info',
  awaiting_approval: 'warn',
  paused: 'warn',
  paused_budget: 'warn',
  completed: 'good',
  cancelled: 'neutral',
  backlog: 'neutral',
  ready: 'info',
  in_progress: 'info',
  blocked: 'warn',
  done: 'good',
  in_use: 'info',
  preparing: 'info',
  removed: 'neutral',
}

export const TONE_CLASSES: Record<Tone, string> = {
  neutral:
    'bg-[var(--color-surface-muted)] text-[var(--color-ink-muted)] ring-1 ring-inset ring-[var(--color-border)]',
  info: 'bg-[var(--color-accent-soft)] text-[var(--color-accent-ink)] ring-1 ring-inset ring-[var(--color-accent)]/25',
  good: 'bg-[color-mix(in_srgb,var(--color-success)_14%,transparent)] text-[var(--color-success)] ring-1 ring-inset ring-[var(--color-success)]/25',
  warn: 'bg-[color-mix(in_srgb,var(--color-warning)_14%,transparent)] text-[var(--color-warning)] ring-1 ring-inset ring-[var(--color-warning)]/25',
  bad: 'bg-[var(--color-danger-soft)] text-[var(--color-danger)] ring-1 ring-inset ring-[var(--color-danger)]/25',
}

export function toneOf(status: string | null | undefined): Tone {
  return (status && TONES[status]) || 'neutral'
}
