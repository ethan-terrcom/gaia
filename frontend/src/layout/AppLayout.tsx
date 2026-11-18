import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import {
  CheckSquare,
  GitBranch,
  GitPullRequest,
  LayoutDashboard,
  Monitor,
  Moon,
  PlayCircle,
  Search,
  Settings,
  Sun,
  Ticket,
} from 'lucide-react'
import { fetchMe, fetchPendingApprovals, fetchTodayCosts } from '../api/client'
import { useEventStream } from '../hooks/useEventStream'
import { useHotkeys } from '../hooks/useHotkeys'
import { IconButton } from '../components/ui/IconButton'
import { Input } from '../components/ui/Input'
import { DemoBadge } from './DemoBadge'
import { PauseControl } from './PauseControl'

type Theme = 'light' | 'dark' | 'system'

const THEME_KEY = 'gaia.theme'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', end: true, icon: LayoutDashboard },
  { to: '/tickets', label: 'Tickets', end: false, icon: Ticket },
  { to: '/runs', label: 'Runs', end: false, icon: PlayCircle },
  { to: '/approvals', label: 'Approvals', end: false, icon: CheckSquare },
  { to: '/prs', label: 'PRs', end: false, icon: GitPullRequest },
  { to: '/worktrees', label: 'Worktrees', end: false, icon: GitBranch },
  { to: '/settings', label: 'Settings', end: false, icon: Settings },
] as const

function readTheme(): Theme {
  const stored = localStorage.getItem(THEME_KEY)
  if (stored === 'light' || stored === 'dark' || stored === 'system') {
    return stored
  }
  // Product default: dark black/grey control plane.
  return 'dark'
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
      theme === 'dark' ? 'light' : theme === 'light' ? 'system' : 'dark'
    localStorage.setItem(THEME_KEY, next)
    setTheme(next)
  }

  return [theme, cycleTheme]
}

function ThemeIcon({ theme }: { theme: Theme }) {
  if (theme === 'dark') {
    return <Moon className="size-3.5" strokeWidth={1.75} />
  }
  if (theme === 'light') {
    return <Sun className="size-3.5" strokeWidth={1.75} />
  }
  return <Monitor className="size-3.5" strokeWidth={1.75} />
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
      className="rounded-full bg-[var(--color-warning)] px-1.5 py-0.5 text-[10px] font-semibold text-black tabular-nums"
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
    ratio >= 1
      ? 'bg-[var(--color-danger)]'
      : ratio >= 0.8
        ? 'bg-[var(--color-warning)]'
        : 'bg-[var(--color-success)]'
  const tooltip = data
    ? data.by_provider
        .map(
          (entry) => `${entry.provider}: $${Number(entry.cost_usd).toFixed(2)}`,
        )
        .join('\n')
    : ''
  return (
    <div
      className="w-44"
      title={tooltip}
      aria-label={`Spent $${spent.toFixed(2)} of $${budget.toFixed(2)}`}
    >
      <div className="text-[12px] font-medium tracking-[-0.01em] text-[var(--color-ink)] tabular-nums">
        ${spent.toFixed(2)} / ${budget.toFixed(2)}
      </div>
      <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-[var(--color-surface-muted)]">
        <div
          className={`h-full rounded-full ${barColor}`}
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
    <div className="flex min-h-screen bg-[var(--color-surface)] text-[var(--color-ink)]">
      <aside className="flex w-[220px] shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)]">
        <div className="flex h-12 items-center px-4">
          <div className="text-[14px] font-semibold tracking-[-0.03em]">
            Gaia
          </div>
        </div>
        <nav className="flex flex-1 flex-col gap-0.5 px-2 pb-4">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `group flex h-8 items-center justify-between gap-2 rounded-[var(--radius-md)] px-2.5 text-[13px] tracking-[-0.01em] transition ${
                    isActive
                      ? 'bg-[var(--color-surface-overlay)] font-medium text-[var(--color-ink)]'
                      : 'text-[var(--color-ink-muted)] hover:bg-[var(--color-surface-muted)] hover:text-[var(--color-ink)]'
                  }`
                }
              >
                {({ isActive }) => (
                  <>
                    <span className="flex items-center gap-2.5">
                      <Icon
                        className={`size-3.5 shrink-0 ${isActive ? 'text-[var(--color-ink)]' : 'text-[var(--color-ink-subtle)] group-hover:text-[var(--color-ink-muted)]'}`}
                        strokeWidth={1.75}
                        aria-hidden
                      />
                      {item.label}
                    </span>
                    {item.to === '/approvals' ? (
                      <PendingApprovalsBadge />
                    ) : null}
                  </>
                )}
              </NavLink>
            )
          })}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col bg-[var(--color-surface-muted)]">
        <header className="flex h-12 items-center gap-3 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4">
          <div className="relative min-w-0 flex-1 max-w-xl">
            <Search
              className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-[var(--color-ink-subtle)]"
              strokeWidth={1.75}
              aria-hidden
            />
            <Input
              data-search-input
              aria-label="Search"
              placeholder="Search…"
              className="pl-8"
            />
          </div>
          <TodaySpend />
          <div className="ml-auto flex items-center gap-1.5">
            <DemoBadge />
            <PauseControl variant="button" />
            <IconButton label={`Theme: ${theme}`} onClick={cycleTheme}>
              <ThemeIcon theme={theme} />
            </IconButton>
            <span className="max-w-36 truncate px-1.5 text-[11px] text-[var(--color-ink-subtle)]">
              {me?.user_id ?? '…'}
            </span>
          </div>
        </header>
        <PauseControl variant="banner" />
        <main className="flex min-h-0 flex-1 flex-col overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
