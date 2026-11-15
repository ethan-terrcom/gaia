import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import { api } from '../../api/client'
import {
  ErrorState,
  LoadingState,
  EmptyState,
  PageHeader,
} from '../../components/States'
import { StatusBadge } from '../../components/StatusBadge'
import { formatRelative } from '../../components/format'
import type { components } from '../../api/schema'

type PullRequestState = components['schemas']['PullRequestState']
type Filter = PullRequestState | 'all'

const FILTERS: readonly { value: Filter; label: string }[] = [
  { value: 'open', label: 'Open' },
  { value: 'merged', label: 'Merged' },
  { value: 'closed', label: 'Closed' },
  { value: 'all', label: 'All' },
]

function parseFilter(value: string | null): Filter {
  if (value === 'merged' || value === 'closed' || value === 'all') {
    return value
  }
  return 'open'
}

function isGitHubUrl(url: string): boolean {
  return url.startsWith('https://github.com/')
}

export function PullRequestsView() {
  const [searchParams, setSearchParams] = useSearchParams()
  const filter = parseFilter(searchParams.get('state'))

  const { data, error, isError, isLoading, refetch } = useQuery({
    // The global event stream invalidates the `['pull-requests']` prefix on
    // `pr.*` and `ci.*` events (see `INVALIDATIONS`), so this view is live.
    queryKey: ['pull-requests', { state: filter }],
    queryFn: async () => {
      const response = await api.GET('/api/v1/pull-requests', {
        params: {
          query: {
            limit: 100,
            state: filter === 'all' ? undefined : filter,
          },
        },
        // Keep this testable: openapi-fetch captures `globalThis.fetch` when
        // the client is created, so passing the current global lets tests stub
        // `fetch` after module import.
        fetch,
      })
      if (response.error) {
        throw response.error
      }
      return response.data
    },
  })

  function selectFilter(next: Filter): void {
    const params = new URLSearchParams(searchParams)
    if (next === 'open') {
      params.delete('state')
    } else {
      params.set('state', next)
    }
    setSearchParams(params)
  }

  return (
    <section>
      <PageHeader
        title="Pull requests"
        actions={
          <div
            role="group"
            aria-label="Pull request state filter"
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

      {isLoading ? <LoadingState /> : null}

      {isError ? (
        <ErrorState error={error} onRetry={() => void refetch()} />
      ) : null}

      {data && data.items.length === 0 ? (
        <EmptyState title="No pull requests yet.">
          Gaia opens a draft PR when a run&apos;s final review is approved.
        </EmptyState>
      ) : null}

      {data && data.items.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-gray-500 dark:text-gray-400">
              <tr>
                <th scope="col" className="px-3 py-2">
                  PR
                </th>
                <th scope="col" className="px-3 py-2">
                  Ticket
                </th>
                <th scope="col" className="px-3 py-2">
                  Repo
                </th>
                <th scope="col" className="px-3 py-2">
                  Run
                </th>
                <th scope="col" className="px-3 py-2">
                  CI
                </th>
                <th scope="col" className="px-3 py-2">
                  Review
                </th>
                <th scope="col" className="px-3 py-2">
                  Age
                </th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((pr) => (
                <tr
                  key={pr.id}
                  className="border-t border-gray-200 dark:border-gray-800"
                >
                  <td className="px-3 py-2">
                    {isGitHubUrl(pr.url) ? (
                      <a
                        href={pr.url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="text-blue-600 hover:underline dark:text-blue-400"
                      >
                        #{pr.number}
                      </a>
                    ) : (
                      <span>#{pr.number}</span>
                    )}
                  </td>
                  <td className="px-3 py-2">{pr.ticket_title}</td>
                  <td className="px-3 py-2">{pr.repo_name}</td>
                  <td className="px-3 py-2">
                    <Link
                      to={`/runs/${pr.run_id}`}
                      className="text-blue-600 hover:underline dark:text-blue-400"
                    >
                      <StatusBadge status={pr.run_status} />
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={pr.ci_status} />
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge
                      status={
                        pr.review_state === 'none' ? null : pr.review_state
                      }
                    />
                  </td>
                  <td className="px-3 py-2" title={pr.created_at}>
                    {formatRelative(pr.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  )
}
