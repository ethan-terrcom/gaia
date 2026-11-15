/**
 * Display formatting shared by every view. The API sends money as decimal
 * strings (Pydantic ``Decimal``), so these accept strings and parse them for
 * display only. Never do arithmetic on the parsed numbers and send it back.
 */

export function formatUsd(value: string | number | null | undefined): string {
  const amount = Number(value ?? 0)
  if (!Number.isFinite(amount)) {
    return '$—'
  }
  return amount < 0.01 && amount > 0 ? '<$0.01' : `$${amount.toFixed(2)}`
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
]

/** "3 hours ago" / "in 2 minutes"; "just now" under a minute. */
export function formatRelative(
  iso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!iso) {
    return '—'
  }
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) {
    return '—'
  }
  const seconds = Math.round((then.getTime() - now.getTime()) / 1000)
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) {
      return formatter.format(Math.round(seconds / size), unit)
    }
  }
  return 'just now'
}

/** Shorten a git SHA for display. */
export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 8) : '—'
}
