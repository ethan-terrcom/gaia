import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { components } from '../../api/schema'
import { formatRelative, formatUsd } from '../../components/format'
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
} from '../../components/States'
import { StatusBadge } from '../../components/StatusBadge'
import type { ReactNode } from 'react'

type ApprovalPage = components['schemas']['ApprovalPage']
type MetricsSummary = components['schemas']['MetricsSummary']
type PullRequestPage = components['schemas']['PullRequestPage']
type RunSummary = components['schemas']['RunSummary']
type TodayCosts = components['schemas']['TodayCosts']

const TERMINAL_RUN_STATUSES = new Set(['completed', 'failed', 'cancelled'])

function asNumber(value: string | null | undefined): number {
  const parsed = Number(value ?? 0)
  return Number.isFinite(parsed) ? parsed : 0
}

async function fetchActiveRuns(): Promise<RunSummary[]> {
  const response = await api.GET('/api/v1/runs', {
    params: { query: { limit: 100 } },
    // Keep this testable: openapi-fetch captures `globalThis.fetch` when the
    // client is created, so passing the current global lets tests stub `fetch`
    // after module import (doc 17).
    fetch,
  })
  if (response.error) {
    throw response.error
  }
  return response.data.items
}

async function fetchPendingApprovals(): Promise<ApprovalPage> {
  // The same one-row request as `api/client.fetchPendingApprovals`, but with
  // the current global `fetch` passed per request so tests can stub it.
  const response = await api.GET('/api/v1/approvals', {
    params: { query: { status: 'pending', limit: 1 } },
    fetch,
  })
  if (response.error) {
    throw response.error
  }
  return response.data
}

async function fetchOpenPullRequests(): Promise<PullRequestPage> {
  const response = await api.GET('/api/v1/pull-requests', {
    params: { query: { state: 'open', limit: 5 } },
    fetch,
  })
  if (response.error) {
    throw response.error
  }
  return response.data
}

async function fetchTodayCosts(): Promise<TodayCosts> {
  const response = await api.GET('/api/v1/costs/today', { fetch })
  if (response.data === undefined) {
    throw new Error('today cost response was empty')
  }
  return response.data
}

async function fetchMetricsSummary(): Promise<MetricsSummary> {
  const response = await api.GET('/api/v1/metrics/summary', {
    params: { query: { days: 14 } },
    fetch,
  })
  if (response.error) {
    throw response.error
  }
  return response.data
}

function CostBar({
  costUsd,
  budgetUsd,
}: {
  costUsd: string | null | undefined
  budgetUsd: string | null | undefined
}) {
  const cost = asNumber(costUsd)
  const budget = asNumber(budgetUsd)
  const ratio = budget > 0 ? cost / budget : 0
  const width = Math.max(0, Math.min(100, ratio * 100))
  const barColor =
    ratio >= 1 ? 'bg-red-500' : ratio >= 0.8 ? 'bg-amber-500' : 'bg-emerald-500'
  return (
    <div className="h-1.5 w-full rounded bg-gray-200 dark:bg-gray-700">
      <div
        className={`h-full rounded ${barColor}`}
        style={{ width: `${width}%` }}
      />
    </div>
  )
}

function Card({
  title,
  linkTo,
  linkLabel,
  children,
}: {
  title: string
  linkTo?: string
  linkLabel?: string
  children: ReactNode
}) {
  return (
    <section className="rounded border border-gray-200 p-4 dark:border-gray-800">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">{title}</h2>
        {linkTo ? (
          <Link
            to={linkTo}
            className="text-sm text-blue-600 hover:underline dark:text-blue-400"
          >
            {linkLabel ?? 'View all'}
          </Link>
        ) : null}
      </div>
      {children}
    </section>
  )
}

function ActiveRunItem({ run }: { run: RunSummary }) {
  const step = run.current_step
  return (
    <article className="rounded border border-gray-200 p-3 dark:border-gray-800">
      <div className="flex items-start justify-between gap-3">
        <Link
          to={`/runs/${run.id}`}
          className="font-medium text-blue-600 hover:underline dark:text-blue-400"
        >
          {run.ticket_title}
        </Link>
        <StatusBadge status={run.status} />
      </div>
      <p className="mt-2 text-sm">
        {step ? (
          <>
            Step {step.index + 1}: {step.title}
          </>
        ) : (
          'No step in progress'
        )}
      </p>
      <div className="mt-3">
        <div className="mb-1 flex items-center justify-between text-sm">
          <span>
            {formatUsd(run.cost_usd)} of {formatUsd(run.budget_usd)}
          </span>
          <span title={run.started_at ?? undefined}>
            {formatRelative(run.started_at)}
          </span>
        </div>
        <CostBar costUsd={run.cost_usd} budgetUsd={run.budget_usd} />
      </div>
    </article>
  )
}

function ActiveRunsCard() {
  const query = useQuery({
    // The global stream invalidates the `['runs']` prefix on `run.*` and
    // `step.*` events, so this card is live.
    queryKey: ['runs', { view: 'active' }],
    queryFn: fetchActiveRuns,
  })

  if (query.isLoading) {
    return (
      <Card title="Active runs">
        <LoadingState label="Loading active runs…" />
      </Card>
    )
  }

  if (query.isError) {
    return (
      <Card title="Active runs">
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    )
  }

  if (query.data === undefined) {
    return (
      <Card title="Active runs">
        <LoadingState label="Loading active runs…" />
      </Card>
    )
  }

  const runs = query.data
  if (runs.length === 0) {
    return (
      <Card title="Active runs">
        <EmptyState title="No runs yet.">
          Create a ticket and press Start run.
        </EmptyState>
      </Card>
    )
  }

  const activeRuns = runs.filter(
    (run) => !TERMINAL_RUN_STATUSES.has(run.status),
  )
  if (activeRuns.length === 0) {
    return (
      <Card title="Active runs">
        <EmptyState title="No active runs.">
          Start a run from a ticket to see it here.
        </EmptyState>
      </Card>
    )
  }

  return (
    <Card title="Active runs">
      <div className="grid gap-3">
        {activeRuns.map((run) => (
          <ActiveRunItem key={run.id} run={run} />
        ))}
      </div>
    </Card>
  )
}

function NeedsYouCard() {
  const query = useQuery({
    queryKey: ['approvals', 'pending'],
    queryFn: fetchPendingApprovals,
  })

  if (query.isLoading) {
    return (
      <Card title="Needs you" linkTo="/approvals" linkLabel="Review approvals">
        <LoadingState label="Loading pending approvals…" />
      </Card>
    )
  }

  if (query.isError) {
    return (
      <Card title="Needs you" linkTo="/approvals" linkLabel="Review approvals">
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    )
  }

  if (query.data === undefined) {
    return (
      <Card title="Needs you" linkTo="/approvals" linkLabel="Review approvals">
        <LoadingState label="Loading pending approvals…" />
      </Card>
    )
  }

  const data = query.data
  const total = data.total_pending
  const oldest = data.items[0]

  return (
    <Card title="Needs you" linkTo="/approvals" linkLabel="Review approvals">
      <Link to="/approvals" className="block">
        <span className="text-5xl font-semibold">{total}</span>
      </Link>
      {oldest ? (
        <p className="mt-2 text-sm">
          Oldest: <span className="font-medium">{oldest.kind}</span> ·{' '}
          {formatRelative(oldest.created_at)}
        </p>
      ) : (
        <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
          No pending approvals.
        </p>
      )}
    </Card>
  )
}

function OpenPrsCard() {
  const query = useQuery({
    queryKey: ['pull-requests', { state: 'open', limit: 5 }],
    queryFn: fetchOpenPullRequests,
  })

  if (query.isLoading) {
    return (
      <Card title="Open PRs" linkTo="/prs">
        <LoadingState label="Loading open PRs…" />
      </Card>
    )
  }

  if (query.isError) {
    return (
      <Card title="Open PRs" linkTo="/prs">
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    )
  }

  if (query.data === undefined) {
    return (
      <Card title="Open PRs" linkTo="/prs">
        <LoadingState label="Loading open PRs…" />
      </Card>
    )
  }

  const prs = query.data.items
  if (prs.length === 0) {
    return (
      <Card title="Open PRs" linkTo="/prs">
        <EmptyState title="No open PRs.">
          Gaia opens a draft PR when a run&apos;s final review is approved.
        </EmptyState>
      </Card>
    )
  }

  return (
    <Card title="Open PRs" linkTo="/prs">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-gray-500 dark:text-gray-400">
          <tr>
            <th scope="col" className="px-2 py-1">
              PR
            </th>
            <th scope="col" className="px-2 py-1">
              Ticket
            </th>
            <th scope="col" className="px-2 py-1">
              CI
            </th>
          </tr>
        </thead>
        <tbody>
          {prs.map((pr) => (
            <tr
              key={pr.id}
              className="border-t border-gray-200 dark:border-gray-800"
            >
              <td className="px-2 py-2">
                <Link
                  to="/prs"
                  className="text-blue-600 hover:underline dark:text-blue-400"
                >
                  #{pr.number}
                </Link>
              </td>
              <td className="px-2 py-2">{pr.ticket_title}</td>
              <td className="px-2 py-2">
                <StatusBadge status={pr.ci_status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

function TodaySpendCard() {
  const query = useQuery({
    queryKey: ['costs', 'today'],
    queryFn: fetchTodayCosts,
  })

  if (query.isLoading) {
    return (
      <Card title="Today's spend">
        <LoadingState label="Loading today's spend…" />
      </Card>
    )
  }

  if (query.isError) {
    return (
      <Card title="Today's spend">
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    )
  }

  if (query.data === undefined) {
    return (
      <Card title="Today's spend">
        <LoadingState label="Loading today's spend…" />
      </Card>
    )
  }

  const data = query.data
  if (data.by_provider.length === 0) {
    return (
      <Card title="Today's spend">
        <EmptyState title="No spend today.">
          LLM usage will appear here as agents work.
        </EmptyState>
      </Card>
    )
  }

  return (
    <Card title="Today's spend">
      <ul className="divide-y divide-gray-200 dark:divide-gray-800">
        {data.by_provider.map((entry) => (
          <li
            key={entry.provider}
            className="flex items-center justify-between py-1.5 text-sm"
          >
            <span>{entry.provider}</span>
            <span>{formatUsd(entry.cost_usd)}</span>
          </li>
        ))}
      </ul>
      <div className="mt-3">
        <div className="mb-1 text-sm">
          {formatUsd(data.spent_usd)} of {formatUsd(data.budget_usd)}
        </div>
        <CostBar costUsd={data.spent_usd} budgetUsd={data.budget_usd} />
      </div>
    </Card>
  )
}

function MetricsCard() {
  const query = useQuery({
    queryKey: ['metrics', 'summary', { days: 14 }],
    queryFn: fetchMetricsSummary,
  })

  if (query.isLoading) {
    return (
      <Card title="Last 14 days">
        <LoadingState label="Loading 14-day metrics…" />
      </Card>
    )
  }

  if (query.isError) {
    return (
      <Card title="Last 14 days">
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    )
  }

  if (query.data === undefined) {
    return (
      <Card title="Last 14 days">
        <LoadingState label="Loading 14-day metrics…" />
      </Card>
    )
  }

  const data = query.data
  const successRate =
    data.success_rate === null ? '—' : `${Math.round(data.success_rate * 100)}%`
  const maxCost = Math.max(
    0,
    ...data.cost_by_day.map((entry) => asNumber(entry.cost_usd)),
  )

  return (
    <Card title="Last 14 days">
      <dl className="grid gap-2 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Success rate</dt>
          <dd className="text-lg font-semibold">{successRate}</dd>
        </div>
        <div>
          <dt className="text-gray-500 dark:text-gray-400">
            Average cost per run
          </dt>
          <dd className="text-lg font-semibold">
            {formatUsd(data.avg_cost_per_run_usd)}
          </dd>
        </div>
      </dl>
      {data.cost_by_day.length === 0 ? (
        <div className="mt-4">
          <EmptyState title="No spend in the last 14 days." />
        </div>
      ) : (
        <div className="mt-4 grid gap-1">
          {data.cost_by_day.map((entry) => {
            const width =
              maxCost > 0 ? (asNumber(entry.cost_usd) / maxCost) * 100 : 0
            return (
              <div key={entry.day} className="flex items-center gap-3 text-sm">
                <div className="w-24 shrink-0 text-gray-500 dark:text-gray-400">
                  {entry.day}
                </div>
                <div className="h-4 flex-1 rounded bg-gray-200 dark:bg-gray-700">
                  <div
                    className="h-full rounded bg-blue-500"
                    style={{ width: `${width}%` }}
                  />
                </div>
                <div className="w-20 text-right">
                  {formatUsd(entry.cost_usd)}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}

export function DashboardView() {
  return (
    <section>
      <PageHeader title="Dashboard" />
      <div className="grid gap-4 xl:grid-cols-2">
        <ActiveRunsCard />
        <NeedsYouCard />
        <OpenPrsCard />
        <TodaySpendCard />
        <MetricsCard />
      </div>
    </section>
  )
}
