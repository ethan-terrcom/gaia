/**
 * Status → tone mapping for :func:`StatusBadge`. Unknown values are neutral, so a
 * new backend status never breaks a view.
 */

export type Tone = 'neutral' | 'info' | 'good' | 'warn' | 'bad'

const TONES: Record<string, Tone> = {
  // CI and reviews
  passed: 'good',
  failed: 'bad',
  pending: 'info',
  unknown: 'neutral',
  approved: 'good',
  changes_requested: 'warn',
  // PRs
  open: 'info',
  merged: 'good',
  closed: 'neutral',
  // runs
  planning: 'info',
  implementing: 'info',
  reviewing: 'info',
  pr_open: 'info',
  awaiting_approval: 'warn',
  paused: 'warn',
  paused_budget: 'warn',
  completed: 'good',
  cancelled: 'neutral',
  // worktrees
  ready: 'good',
  in_use: 'info',
  preparing: 'info',
  removed: 'neutral',
}

export const TONE_CLASSES: Record<Tone, string> = {
  neutral: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
  info: 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200',
  good: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200',
  warn: 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200',
  bad: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200',
}

export function toneOf(status: string | null | undefined): Tone {
  return (status && TONES[status]) || 'neutral'
}
