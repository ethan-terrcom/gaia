import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { makeQueryClient } from '../../api/queryClient'
import type { components } from '../../api/schema'
import { DashboardView } from './DashboardView'

type ApprovalPage = components['schemas']['ApprovalPage']
type MetricsSummary = components['schemas']['MetricsSummary']
type PullRequestPage = components['schemas']['PullRequestPage']
type RunSummary = components['schemas']['RunSummary']
type TodayCosts = components['schemas']['TodayCosts']

type FetchHandler = (request: Request) => Response | Promise<Response>

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fetchMock(handler: FetchHandler): typeof fetch {
  return vi.fn(async (input: Request | URL | string) => {
    const request =
      input instanceof Request ? input : new Request(input as string)
    return handler(request)
  }) as unknown as typeof fetch
}

function deferred(): {
  promise: Promise<Response>
  resolve: (response: Response) => void
} {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

function hoursAgo(hours: number): string {
  return new Date(Date.now() - hours * 60 * 60 * 1000).toISOString()
}

function runSummary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 'run-1',
    workspace_id: 'ws-1',
    repo_id: 'repo-1',
    ticket_id: 'ticket-1',
    ticket_title: 'Implement dark mode',
    status: 'implementing',
    current_step: { index: 0, title: 'Implement the fix' },
    pr_number: null,
    pr_url: null,
    ci_status: null,
    cost_usd: '4.50',
    budget_usd: '20.00',
    started_at: hoursAgo(3),
    finished_at: null,
    created_at: '2026-11-14T08:00:00Z',
    updated_at: '2026-11-14T10:00:00Z',
    created_by: 'user-1',
    graph_thread_id: null,
    junior_runtime: 'native_claude',
    worktree_id: 'worktree-1',
    ...overrides,
  }
}

function approvalPage(overrides: Partial<ApprovalPage> = {}): ApprovalPage {
  return {
    total_pending: 2,
    items: [
      {
        id: 'approval-1',
        workspace_id: 'ws-1',
        run_id: 'run-1',
        kind: 'plan',
        status: 'pending',
        comment: null,
        payload_json: {},
        idempotency_key: 'idem-1',
        created_at: hoursAgo(5),
        updated_at: hoursAgo(5),
        decided_at: null,
        decided_by: null,
      },
    ],
    ...overrides,
  }
}

function pullRequestPage(
  overrides: Partial<PullRequestPage> = {},
): PullRequestPage {
  return {
    items: [
      {
        id: 'pr-1',
        number: 42,
        url: 'https://github.com/acme/repo/pull/42',
        state: 'open',
        ci_status: 'passed',
        review_state: 'approved',
        head_sha: 'abcdef0123456789',
        run_id: 'run-1',
        run_status: 'implementing',
        ticket_id: 'ticket-2',
        ticket_title: 'Fix login bug',
        repo_name: 'acme/repo',
        created_at: '2026-11-14T09:00:00Z',
        updated_at: '2026-11-14T10:00:00Z',
      },
    ],
    ...overrides,
  }
}

function todayCosts(overrides: Partial<TodayCosts> = {}): TodayCosts {
  return {
    spent_usd: '3.73',
    budget_usd: '10.00',
    by_provider: [
      { provider: 'deepseek', cost_usd: '1.23' },
      { provider: 'anthropic', cost_usd: '2.50' },
    ],
    day_start: '2026-11-14T00:00:00Z',
    ...overrides,
  }
}

function metricsSummary(
  overrides: Partial<MetricsSummary> = {},
): MetricsSummary {
  return {
    runs_by_status: { completed: 3, failed: 1, cancelled: 0 },
    success_rate: 0.75,
    avg_cost_per_run_usd: '7.89',
    cost_by_model: [],
    cost_by_day: [
      { day: '2026-11-13', cost_usd: '2.00' },
      { day: '2026-11-12', cost_usd: '8.00' },
    ],
    approval_wait_s: null,
    ...overrides,
  }
}

type EndpointOverride = (request: Request) => Response | Promise<Response>

function stubDashboardFetch(
  overrides: Partial<Record<string, EndpointOverride>> = {},
): typeof fetch {
  const fetch = fetchMock((request) => {
    const url = request.url
    if (url.includes('/api/v1/runs')) {
      return overrides.runs
        ? overrides.runs(request)
        : jsonResponse(200, {
            items: [runSummary()],
            next_cursor: null,
          })
    }
    if (url.includes('/api/v1/approvals')) {
      return overrides.approvals
        ? overrides.approvals(request)
        : jsonResponse(200, approvalPage())
    }
    if (url.includes('/api/v1/pull-requests')) {
      return overrides.prs
        ? overrides.prs(request)
        : jsonResponse(200, pullRequestPage())
    }
    if (url.includes('/api/v1/costs/today')) {
      return overrides.costs
        ? overrides.costs(request)
        : jsonResponse(200, todayCosts())
    }
    if (url.includes('/api/v1/metrics/summary')) {
      return overrides.metrics
        ? overrides.metrics(request)
        : jsonResponse(200, metricsSummary())
    }
    return jsonResponse(404, {
      error: { code: 'not_found', message: 'not found' },
    })
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

function renderView() {
  const queryClient = makeQueryClient()
  queryClient.setDefaultOptions({ queries: { retry: false } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DashboardView />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('DashboardView', () => {
  it('renders every card from stubbed responses and filters terminal runs', async () => {
    stubDashboardFetch({
      runs: () =>
        jsonResponse(200, {
          items: [
            runSummary(),
            runSummary({
              id: 'run-2',
              ticket_id: 'ticket-2',
              ticket_title: 'Already finished',
              status: 'completed',
              started_at: null,
            }),
          ],
          next_cursor: null,
        }),
    })

    renderView()

    // Active runs card.
    expect(
      await screen.findByRole('link', { name: 'Implement dark mode' }),
    ).toBeTruthy()
    expect(
      screen
        .getByRole('link', { name: 'Implement dark mode' })
        .getAttribute('href'),
    ).toBe('/runs/run-1')
    expect(screen.getByText('implementing')).toBeTruthy()
    expect(screen.getByText('Step 1: Implement the fix')).toBeTruthy()
    expect(screen.getByText('$4.50 of $20.00')).toBeTruthy()
    expect(screen.getByText('3 hours ago')).toBeTruthy()
    expect(screen.queryByText('Already finished')).toBeNull()

    // Needs you card.
    expect(screen.getByRole('link', { name: '2' })).toBeTruthy()
    expect(
      screen
        .getByRole('link', { name: 'Review approvals' })
        .getAttribute('href'),
    ).toBe('/approvals')
    const oldest = screen.getByText('plan').closest('p')
    expect(oldest?.textContent).toContain('5 hours ago')

    // Open PRs card.
    const prLink = screen.getByRole('link', { name: '#42' })
    expect(prLink.getAttribute('href')).toBe('/prs/pr-1')
    expect(screen.getByText('Fix login bug')).toBeTruthy()
    expect(screen.getByText('passed')).toBeTruthy()

    // Today's spend card.
    expect(screen.getByText('deepseek')).toBeTruthy()
    expect(screen.getByText('$1.23')).toBeTruthy()
    expect(screen.getByText('anthropic')).toBeTruthy()
    expect(screen.getByText('$2.50')).toBeTruthy()
    expect(screen.getByText('$3.73 of $10.00')).toBeTruthy()

    // Last 14 days card.
    expect(screen.getByText('75%')).toBeTruthy()
    expect(screen.getByText('$7.89')).toBeTruthy()
    expect(screen.getByText('2026-11-13')).toBeTruthy()
    expect(screen.getByText('2026-11-12')).toBeTruthy()
    expect(screen.getByText('$2.00')).toBeTruthy()
    expect(screen.getByText('$8.00')).toBeTruthy()
  })

  it('shows the empty state when there have never been any runs', async () => {
    stubDashboardFetch({
      runs: () => jsonResponse(200, { items: [], next_cursor: null }),
      approvals: () =>
        jsonResponse(200, approvalPage({ total_pending: 0, items: [] })),
      prs: () => jsonResponse(200, pullRequestPage({ items: [] })),
      costs: () =>
        jsonResponse(200, todayCosts({ spent_usd: '0', by_provider: [] })),
      metrics: () => jsonResponse(200, metricsSummary({ cost_by_day: [] })),
    })

    renderView()

    expect(await screen.findByText('No runs yet.')).toBeTruthy()
    expect(
      screen.getByText('Create a ticket and press Start run.'),
    ).toBeTruthy()
  })

  it('keeps the other cards when one card fails and retries only that card', async () => {
    let runCalls = 0
    stubDashboardFetch({
      runs: () => {
        runCalls += 1
        return jsonResponse(500, {
          error: { code: 'boom', message: 'runs exploded' },
        })
      },
    })

    renderView()

    expect(await screen.findByText('runs exploded')).toBeTruthy()
    expect(screen.getByRole('link', { name: '2' })).toBeTruthy()
    expect(screen.getByRole('link', { name: '#42' })).toBeTruthy()
    expect(screen.getByText('$3.73 of $10.00')).toBeTruthy()
    expect(screen.getByText('75%')).toBeTruthy()
    expect(runCalls).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => {
      expect(runCalls).toBe(2)
    })
  })

  it('shows a card loading state without waiting for the other cards', async () => {
    const pendingRuns = deferred()
    stubDashboardFetch({
      runs: () => pendingRuns.promise,
    })

    renderView()

    expect(await screen.findByText('Loading active runs…')).toBeTruthy()
    expect(await screen.findByRole('link', { name: '2' })).toBeTruthy()
    expect(screen.getByText('$3.73 of $10.00')).toBeTruthy()

    pendingRuns.resolve(
      jsonResponse(200, { items: [runSummary()], next_cursor: null }),
    )

    expect(
      await screen.findByRole('link', { name: 'Implement dark mode' }),
    ).toBeTruthy()
  })
})
