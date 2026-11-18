import { useQuery } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
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
import { SegmentedControl } from '../../components/ui/SegmentedControl'
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
        description="Review CI and diffs in Gaia; GitHub stays the escape hatch."
        actions={
          <SegmentedControl
            label="Pull request state filter"
            value={filter}
            options={FILTERS}
            onChange={selectFilter}
          />
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
        <div className="overflow-x-auto rounded-[var(--radius-xl)] border border-[var(--color-border)] bg-[var(--color-surface-raised)] shadow-[var(--shadow-panel)]">
          <table className="w-full text-left text-sm">
            <thead className="text-xs tracking-wide text-[var(--color-ink-muted)] uppercase">
              <tr>
                <th scope="col" className="px-3 py-3">
                  PR
                </th>
                <th scope="col" className="px-3 py-3">
                  Ticket
                </th>
                <th scope="col" className="px-3 py-3">
                  Repo
                </th>
                <th scope="col" className="px-3 py-3">
                  Run
                </th>
                <th scope="col" className="px-3 py-3">
                  CI
                </th>
                <th scope="col" className="px-3 py-3">
                  Review
                </th>
                <th scope="col" className="px-3 py-3">
                  Age
                </th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((pr) => (
                <tr
                  key={pr.id}
                  className="border-t border-[var(--color-border)] hover:bg-[var(--color-surface-muted)]/60"
                >
                  <td className="px-3 py-3">
                    <span className="inline-flex items-center gap-2">
                      <Link
                        to={`/prs/${pr.id}`}
                        className="font-medium text-[var(--color-accent-ink)] hover:underline"
                      >
                        #{pr.number}
                      </Link>
                      {isGitHubUrl(pr.url) ? (
                        <a
                          href={pr.url}
                          target="_blank"
                          rel="noreferrer noopener"
                          aria-label={`Open PR #${pr.number} on GitHub`}
                          className="text-[var(--color-ink-subtle)] hover:text-[var(--color-ink)]"
                        >
                          <ExternalLink className="size-3.5" />
                        </a>
                      ) : null}
                    </span>
                  </td>
                  <td className="px-3 py-3">{pr.ticket_title}</td>
                  <td className="px-3 py-3 text-[var(--color-ink-muted)]">
                    {pr.repo_name}
                  </td>
                  <td className="px-3 py-3">
                    <Link to={`/runs/${pr.run_id}`}>
                      <StatusBadge status={pr.run_status} />
                    </Link>
                  </td>
                  <td className="px-3 py-3">
                    <StatusBadge status={pr.ci_status} />
                  </td>
                  <td className="px-3 py-3">
                    <StatusBadge
                      status={
                        pr.review_state === 'none' ? null : pr.review_state
                      }
                    />
                  </td>
                  <td className="px-3 py-3" title={pr.created_at}>
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
