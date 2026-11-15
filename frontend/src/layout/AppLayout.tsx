import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { fetchMe, fetchPendingApprovals, fetchTodayCosts } from '../api/client'
import { useEventStream } from '../hooks/useEventStream'
import { useHotkeys } from '../hooks/useHotkeys'
import { DemoBadge } from './DemoBadge'
import { PauseControl } from './PauseControl'

type Theme = 'light' | 'dark' | 'system'

const THEME_KEY = 'gaia.theme'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/tickets', label: 'Tickets', end: false },
  { to: '/runs', label: 'Runs', end: false },
  { to: '/approvals', label: 'Approvals', end: false },
  { to: '/prs', label: 'PRs', end: false },
  { to: '/worktrees', label: 'Worktrees', end: false },
  { to: '/settings', label: 'Settings', end: false },
] as const

function readTheme(): Theme {
  const stored = localStorage.getItem(THEME_KEY)
  if (stored === 'light' || stored === 'dark' || stored === 'system') {
    return stored
  }
  return 'system'
}

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(readTheme)

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const apply = () => {
      const dark = theme === 'dark' || (theme === 'system' && media.matches)
      document.documentElement.classList.toggle('dark', dark)
    }
    apply()
    if (theme !== 'system') {
      return
    }
    media.addEventListener('change', apply)
    return () => media.removeEventListener('change', apply)
  }, [theme])

  function cycleTheme(): void {
    const next: Theme =
      theme === 'light' ? 'dark' : theme === 'dark' ? 'system' : 'light'
    localStorage.setItem(THEME_KEY, next)
    setTheme(next)
  }

  return [theme, cycleTheme]
}

export function PendingApprovalsBadge() {
  const { data } = useQuery({
    queryKey: ['approvals', 'pending'],
    queryFn: fetchPendingApprovals,
  })
  const total = data?.total_pending ?? 0
  if (total === 0) {
    return null
  }
  const label = total > 99 ? '99+' : String(total)
  return (
    <span
      aria-label={`${total} pending approvals`}
      className="rounded-full bg-amber-500 px-2 py-0.5 text-xs text-black"
    >
      {label}
    </span>
  )
}

export function TodaySpend() {
  const { data } = useQuery({
    queryKey: ['costs', 'today'],
    queryFn: fetchTodayCosts,
  })
  const spent = Number(data?.spent_usd ?? 0)
  const budget = Number(data?.budget_usd ?? 0)
  const ratio = budget > 0 ? spent / budget : 0
  const barColor =
    ratio >= 1 ? 'bg-red-500' : ratio >= 0.8 ? 'bg-amber-500' : 'bg-emerald-500'
  const tooltip = data
    ? data.by_provider
        .map(
          (entry) => `${entry.provider}: $${Number(entry.cost_usd).toFixed(2)}`,
        )
        .join('\n')
    : ''
  return (
    <div
      className="w-56"
      title={tooltip}
      aria-label={`Spent $${spent.toFixed(2)} of $${budget.toFixed(2)}`}
    >
      <div className="text-sm">
        ${spent.toFixed(2)} / ${budget.toFixed(2)}
      </div>
      <div className="h-1.5 w-full rounded bg-gray-200 dark:bg-gray-700">
        <div
          className={`h-full rounded ${barColor}`}
          style={{ width: `${Math.min(100, ratio * 100)}%` }}
        />
      </div>
    </div>
  )
}

export default function AppLayout() {
  useHotkeys()
  useEventStream()
  const [theme, cycleTheme] = useTheme()
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: fetchMe })

  return (
    <div className="flex min-h-screen bg-white text-black dark:bg-gray-950 dark:text-gray-100">
      <aside className="w-60 shrink-0 border-r border-gray-200 p-4 dark:border-gray-800">
        <div className="mb-6 text-lg font-semibold">Gaia</div>
        <nav className="flex flex-col gap-1">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `flex items-center justify-between rounded px-3 py-2 ${
                  isActive
                    ? 'bg-gray-100 dark:bg-gray-800'
                    : 'hover:bg-gray-50 dark:hover:bg-gray-900'
                }`
              }
            >
              {item.label}
              {item.to === '/approvals' ? <PendingApprovalsBadge /> : null}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-4 border-b border-gray-200 px-6 py-3 dark:border-gray-800">
          <input
            data-search-input
            aria-label="Search"
            placeholder="Search"
            className="w-72 rounded border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-700 dark:bg-gray-900"
          />
          <TodaySpend />
          <div className="ml-auto flex items-center gap-3">
            <DemoBadge />
            <PauseControl variant="button" />
            <button
              type="button"
              onClick={cycleTheme}
              className="rounded border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-700"
            >
              Theme: {theme}
            </button>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {me?.user_id ?? 'loading…'}
            </span>
          </div>
        </header>
        <PauseControl variant="banner" />
        <main className="p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
