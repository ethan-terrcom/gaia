import { useInfiniteQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
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

type RunPage = components['schemas']['RunPage']
type RunStatus = components['schemas']['RunStatus']
type RunSummary = components['schemas']['RunSummary']

// `active` is filtered here, not by the API: `GET /runs` takes one status, and
// "active" is every status that is not terminal.
type Filter = 'all' | 'active' | 'failed' | 'completed' | 'cancelled'

const FILTERS: readonly { value: Filter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'failed', label: 'Failed' },
  { value: 'completed', label: 'Completed' },
  { value: 'cancelled', label: 'Cancelled' },
]

const TERMINAL_RUN_STATUSES = new Set<RunStatus>([
  'completed',
  'failed',
  'cancelled',
])

const PAGE_SIZE = 50

function parseFilter(value: string | null): Filter {
  return FILTERS.some((filter) => filter.value === value)
    ? (value as Filter)
    : 'all'
}

function apiStatus(filter: Filter): RunStatus | undefined {
  return filter === 'all' || filter === 'active' ? undefined : filter
}

async function fetchRunsPage(
  filter: Filter,
  cursor: string | null,
): Promise<RunPage> {
  const response = await api.GET('/api/v1/runs', {
    params: {
      query: {
        limit: PAGE_SIZE,
        status: apiStatus(filter),
        cursor: cursor ?? undefined,
      },
    },
    // Keep this testable: openapi-fetch captures `globalThis.fetch` when the
    // client is created, so passing the current global lets tests stub `fetch`
    // after module import (doc 17).
    fetch,
  })
  if (response.error) {
    throw response.error
  }
  return response.data
}

function RunRow({ run }: { run: RunSummary }) {
  // A run that ended can still have an unfinished step row (a cancel leaves it
  // `running`), so "current step" is shown for live runs only.
  const step = TERMINAL_RUN_STATUSES.has(run.status) ? null : run.current_step
  // `started_at` is set when the graph starts; a run that failed before then
  // only has `created_at`.
  const startedAt = run.started_at ?? run.created_at
  return (
    <tr className="border-t border-gray-200 dark:border-gray-800">
      <td className="px-3 py-2">
        <Link
          to={`/runs/${run.id}`}
          className="font-medium text-blue-600 hover:underline dark:text-blue-400"
        >
          {run.ticket_title}
        </Link>
      </td>
      <td className="px-3 py-2">
        <StatusBadge status={run.status} />
      </td>
      <td className="px-3 py-2">
        {step ? `Step ${step.index + 1}: ${step.title}` : '—'}
      </td>
      <td className="px-3 py-2 whitespace-nowrap">
        {formatUsd(run.cost_usd)} of {formatUsd(run.budget_usd)}
      </td>
      <td className="px-3 py-2">
        {run.pr_number !== null ? `#${run.pr_number}` : '—'}
      </td>
      <td className="px-3 py-2 whitespace-nowrap" title={startedAt}>
        {formatRelative(startedAt)}
      </td>
    </tr>
  )
}

export function RunsView() {
  const [searchParams, setSearchParams] = useSearchParams()
  const filter = parseFilter(searchParams.get('status'))

  const query = useInfiniteQuery({
    // The global event stream invalidates the `['runs']` prefix on `run.*` and
    // `step.*` events, so this list is live.
    queryKey: ['runs', { view: 'list', filter }],
    queryFn: ({ pageParam }) => fetchRunsPage(filter, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  })

  function selectFilter(next: Filter): void {
    const params = new URLSearchParams(searchParams)
    if (next === 'all') {
      params.delete('status')
    } else {
      params.set('status', next)
    }
    setSearchParams(params)
  }

  const loaded = query.data?.pages.flatMap((page) => page.items) ?? []
  const runs =
    filter === 'active'
      ? loaded.filter((run) => !TERMINAL_RUN_STATUSES.has(run.status))
      : loaded

  return (
    <section>
      <PageHeader
        title="Runs"
        actions={
          <div
            role="group"
            aria-label="Run status filter"
            className="flex rounded border border-gray-300 p-0.5 dark:border-gray-700"
          >
            {FILTERS.map(({ value, label }) => (
              <button
                key={value}
                type="button"
                aria-pressed={filter === value}
                onClick={() => selectFilter(value)}
                className={`rounded px-3 py-1.5 text-sm ${
                  filter === value
                    ? 'bg-blue-600 text-white'
                    : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        }
      />

      {query.isLoading ? <LoadingState label="Loading runs…" /> : null}

      {query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : null}

      {query.data && runs.length === 0 ? (
        filter === 'all' ? (
          <EmptyState title="No runs yet.">
            Create a ticket and press Start run.
          </EmptyState>
        ) : (
          <EmptyState title={`No ${filter} runs.`} />
        )
      ) : null}

      {runs.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-gray-500 dark:text-gray-400">
              <tr>
                <th scope="col" className="px-3 py-2">
                  Ticket
                </th>
                <th scope="col" className="px-3 py-2">
                  Status
                </th>
                <th scope="col" className="px-3 py-2">
                  Current step
                </th>
                <th scope="col" className="px-3 py-2">
                  Cost
                </th>
                <th scope="col" className="px-3 py-2">
                  PR
                </th>
                <th scope="col" className="px-3 py-2">
                  Started
                </th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <RunRow key={run.id} run={run} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {query.hasNextPage ? (
        <div className="mt-4">
          <button
            type="button"
            onClick={() => void query.fetchNextPage()}
            disabled={query.isFetchingNextPage}
            className="rounded border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-700"
          >
            {query.isFetchingNextPage ? 'Loading more…' : 'Load more'}
          </button>
        </div>
      ) : null}
    </section>
  )
}
