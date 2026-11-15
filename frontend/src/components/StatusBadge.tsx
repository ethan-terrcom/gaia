/**
 * One badge for every status-like string the API returns (run, ticket, PR,
 * CI, review, worktree). Unknown values render neutrally rather than failing,
 * so a new backend status never breaks a view.
 */

import { TONE_CLASSES, toneOf } from './status'

export function StatusBadge({
  status,
  label,
}: {
  status: string | null | undefined
  label?: string
}) {
  const text = label ?? (status ? status.replaceAll('_', ' ') : '—')
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${TONE_CLASSES[toneOf(status)]}`}
    >
      {text}
    </span>
  )
}
