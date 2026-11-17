import { QueryClientProvider } from '@tanstack/react-query'
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { makeQueryClient } from '../../api/queryClient'
import type { components } from '../../api/schema'
import {
  type GaiaEvent,
  type GaiaEventStreamSource,
  type UseEventStreamOptions,
} from '../../hooks/useEventStream'
import RunDetailView from './RunDetailView'
import { summarizeEvent } from './events'

type RunDetail = components['schemas']['RunDetail']
type RunRead = components['schemas']['RunRead']
type RunStepRead = components['schemas']['RunStepRead']
type ApprovalRead = components['schemas']['ApprovalRead']
type EventRecord = components['schemas']['EventRecord']

const baseRun: RunRead = {
  id: 'run-1',
  workspace_id: 'ws-1',
  ticket_id: 'ticket-1',
  repo_id: 'repo-1',
  worktree_id: 'worktree-1',
  status: 'implementing',
  budget_usd: '100.00',
  cost_usd: '12.34',
  junior_runtime: null,
  created_by: 'user-1',
  created_at: '2026-11-14T09:00:00Z',
  updated_at: '2026-11-14T10:00:00Z',
  started_at: '2026-11-14T09:05:00Z',
  finished_at: null,
  graph_thread_id: null,
}

const baseStep: RunStepRead = {
  id: 'step-1',
  workspace_id: 'ws-1',
  run_id: 'run-1',
  assigned_agent_id: null,
  step_index: 1,
  title: 'Implement fix',
  status: 'completed',
  attempts: 2,
  result_summary: 'done',
  spec_json: {},
  created_at: '2026-11-14T09:10:00Z',
  updated_at: '2026-11-14T09:20:00Z',
}

const baseApproval: ApprovalRead = {
  id: 'approval-1',
  workspace_id: 'ws-1',
  run_id: 'run-1',
  idempotency_key: 'idem-1',
  kind: 'plan',
  status: 'approved',
  payload_json: {},
  created_at: '2026-11-14T09:12:00Z',
  updated_at: '2026-11-14T09:13:00Z',
  decided_at: '2026-11-14T09:13:00Z',
  decided_by: 'user-1',
  comment: null,
}

function runDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    run: baseRun,
    ticket: {
      id: 'ticket-1',
      title: 'Fix login bug',
      status: 'in_progress',
    },
    steps: [baseStep],
    worktree: {
      branch: 'gaia/issue-1',
      path: '/work/gaia/worktree-1',
      status: 'ready',
    },
    approvals: [baseApproval],
    pull_request: {
      id: 'pr-1',
      number: 42,
      url: 'https://github.com/acme/gaia/pull/42',
      state: 'open',
      ci_status: 'passed',
      review_state: null,
      head_sha: 'abcdef0123456789',
      created_at: '2026-11-14T10:30:00Z',
      updated_at: '2026-11-14T10:30:00Z',
    },
    ...overrides,
  }
}

function historyEvent(
  seq: number,
  type: EventRecord['type'],
  payload: EventRecord['payload'],
): EventRecord {
  return {
    seq,
    id: `event-${seq}`,
    workspace_id: 'ws-1',
    run_id: 'run-1',
    agent_id: null,
    type,
    payload,
    ts: '2026-11-14T09:00:00Z',
  }
}

function liveEvent(overrides: Partial<GaiaEvent> = {}): GaiaEvent {
  return {
    seq: 1,
    id: 'live-event-1',
    workspace_id: 'ws-1',
    run_id: 'run-1',
    agent_id: null,
    type: 'run.created',
    payload: {},
    ts: '2026-11-14T09:00:00Z',
    ...overrides,
  }
}

class FakeEventSource implements GaiaEventStreamSource {
  static instances: FakeEventSource[] = []

  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  closed = false
  readonly url: string
  readonly listeners = new Map<string, ((event: MessageEvent) => void)[]>()

  addEventListener(
    type: string,
    listener: (event: MessageEvent) => void,
  ): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener])
  }

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  close(): void {
    this.closed = true
  }

  emitMessage(event: GaiaEvent): void {
    for (const listener of this.listeners.get(event.type) ?? []) {
      listener({ data: JSON.stringify(event) } as MessageEvent)
    }
  }
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function textResponse(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: { 'Content-Type': 'text/x-diff; charset=utf-8' },
  })
}

function fetchMock(
  handler: (request: Request) => Response | Promise<Response>,
): typeof fetch {
  return vi.fn(async (input: Request | URL | string) => {
    const request =
      input instanceof Request ? input : new Request(input as string)
    return handler(request)
  }) as unknown as typeof fetch
}

function mockApi({
  detail = runDetail(),
  history = [],
  diff = '',
  onCancel,
}: {
  detail?: RunDetail
  history?: EventRecord[]
  diff?: string
  onCancel?: () => void
} = {}): typeof fetch {
  return fetchMock((request) => {
    const url = new URL(request.url)
    if (request.method === 'GET' && url.pathname === '/api/v1/runs/run-1') {
      return jsonResponse(200, detail)
    }
    if (
      request.method === 'GET' &&
      url.pathname === '/api/v1/runs/run-1/events'
    ) {
      return jsonResponse(200, { items: history, next_after: null })
    }
    if (
      request.method === 'GET' &&
      url.pathname === '/api/v1/runs/run-1/diff'
    ) {
      return textResponse(200, diff)
    }
    if (
      request.method === 'POST' &&
      url.pathname === '/api/v1/runs/run-1/cancel'
    ) {
      onCancel?.()
      return jsonResponse(200, detail.run)
    }
    return jsonResponse(404, {
      error: { code: 'not_found', message: 'not found' },
    })
  })
}

function renderView(
  options: UseEventStreamOptions = {},
  initialEntry = '/runs/run-1',
) {
  const queryClient = makeQueryClient()
  queryClient.setDefaultOptions({ queries: { retry: false } })
  const eventStreamOptions: UseEventStreamOptions = {
    EventSourceImpl: FakeEventSource,
    ticketFetcher: vi.fn().mockResolvedValue({ ticket: 'ticket-1' }),
    ...options,
  }
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="/runs/:id"
            element={<RunDetailView eventStreamOptions={eventStreamOptions} />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  FakeEventSource.instances = []
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('RunDetailView', () => {
  it('renders the header, steps, approvals, PR and worktree', async () => {
    vi.stubGlobal('fetch', mockApi())

    renderView()

    expect(await screen.findByText('Fix login bug')).toBeTruthy()
    expect(screen.getByText('implementing')).toBeTruthy()
    expect(screen.getByText('gaia/issue-1')).toBeTruthy()
    expect(screen.getByText('/work/gaia/worktree-1')).toBeTruthy()
    expect(screen.getByRole('link', { name: '#42' }).getAttribute('href')).toBe(
      'https://github.com/acme/gaia/pull/42',
    )
    expect(screen.getByText('$12.34 / $100.00')).toBeTruthy()

    const steps = screen.getByTestId('run-steps')
    expect(within(steps).getByText('Implement fix')).toBeTruthy()
    expect(within(steps).getByText('2 attempts')).toBeTruthy()
    expect(within(steps).getByText('done')).toBeTruthy()
    expect(within(steps).getByText('completed')).toBeTruthy()

    expect(screen.getByText('plan')).toBeTruthy()
    expect(screen.getByText('approved')).toBeTruthy()
  })

  it('cancels a run behind a confirm dialog', async () => {
    let cancelled = false
    vi.stubGlobal(
      'fetch',
      mockApi({
        onCancel: () => {
          cancelled = true
        },
      }),
    )

    renderView()

    await screen.findByText('Fix login bug')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel run' }))

    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel run' }))

    await waitFor(() => {
      expect(cancelled).toBe(true)
    })
  })

  it('hides cancel when the run is terminal', async () => {
    vi.stubGlobal(
      'fetch',
      mockApi({
        detail: runDetail({ run: { ...baseRun, status: 'completed' } }),
      }),
    )

    renderView()

    await screen.findByText('Fix login bug')
    expect(screen.queryByRole('button', { name: 'Cancel run' })).toBeNull()
  })

  it('shows an inline cancel error', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock((request) => {
        const url = new URL(request.url)
        if (request.method === 'GET' && url.pathname === '/api/v1/runs/run-1') {
          return jsonResponse(200, runDetail())
        }
        if (
          request.method === 'GET' &&
          url.pathname === '/api/v1/runs/run-1/events'
        ) {
          return jsonResponse(200, { items: [], next_after: null })
        }
        if (
          request.method === 'POST' &&
          url.pathname === '/api/v1/runs/run-1/cancel'
        ) {
          return jsonResponse(409, {
            error: { code: 'conflict', message: 'run is already terminal' },
          })
        }
        return jsonResponse(404, {
          error: { code: 'not_found', message: 'not found' },
        })
      }),
    )

    renderView()

    await screen.findByText('Fix login bug')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel run' }))
    fireEvent.click(
      within(await screen.findByRole('dialog')).getByRole('button', {
        name: 'Cancel run',
      }),
    )

    expect(await screen.findByText('run is already terminal')).toBeTruthy()
  })

  it('merges history and live events without duplicates by seq', async () => {
    vi.stubGlobal(
      'fetch',
      mockApi({
        history: [
          historyEvent(1, 'run.created', {}),
          historyEvent(2, 'agent.message', { content: 'history message' }),
        ],
      }),
    )
    const ticketFetcher = vi.fn().mockResolvedValue({ ticket: 'ticket-1' })

    renderView({ EventSourceImpl: FakeEventSource, ticketFetcher })

    await screen.findByTestId('event-row-1')
    await waitFor(() => {
      expect(ticketFetcher).toHaveBeenCalled()
    })

    act(() => {
      FakeEventSource.instances[0]?.emitMessage(
        liveEvent({
          seq: 2,
          id: 'live-event-2',
          type: 'agent.message',
          payload: { content: 'live message' },
        }),
      )
    })

    expect(await screen.findByText('live message')).toBeTruthy()
    expect(screen.queryByText('history message')).toBeNull()
    expect(screen.getAllByTestId('event-row-2')).toHaveLength(1)
  })

  it('renders collapsible tool rows and payload text as text', async () => {
    const script = '<script>alert(1)</script>'
    vi.stubGlobal(
      'fetch',
      mockApi({
        history: [
          historyEvent(1, 'agent.tool_call', {
            tool: 'run_command',
            args: { command: script },
          }),
          historyEvent(2, 'agent.tool_result', {
            tool: 'run_command',
            ok: true,
            decision: 'approved',
            content: 'ok',
          }),
        ],
      }),
    )

    const { container } = renderView()

    await screen.findByTestId('event-row-1')
    const summaries = screen.getAllByText(/run_command/)
    expect(summaries[0]?.closest('details')).not.toBeNull()
    expect(
      screen.getAllByText(/<script>alert\(1\)<\/script>/).length,
    ).toBeGreaterThan(0)
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByText('ok')).toBeTruthy()
  })

  it('renders only a windowed slice for 2,000 events', async () => {
    const history = Array.from({ length: 2000 }, (_, index) =>
      historyEvent(index + 1, 'step.started', { index: index + 1 }),
    )
    vi.stubGlobal('fetch', mockApi({ history }))

    renderView()

    await screen.findByTestId('event-row-1')
    expect(screen.queryByTestId('event-row-500')).toBeNull()

    fireEvent.scroll(screen.getByTestId('timeline-scroll'), {
      target: { scrollTop: 1999 * 36 },
    })

    await waitFor(() => {
      expect(screen.getByTestId('event-row-2000')).toBeTruthy()
    })
    expect(screen.queryByTestId('event-row-1')).toBeNull()
  })

  it('renders diff line classes and the truncation note', async () => {
    const diff =
      'diff --git a/a.py b/a.py\n@@ -1 +1 @@\n+added\n-removed\n[diff truncated]'
    vi.stubGlobal('fetch', mockApi({ diff }))

    renderView()

    fireEvent.click(await screen.findByRole('tab', { name: 'Diff' }))

    expect(await screen.findByText('diff --git a/a.py b/a.py')).toBeTruthy()
    expect(screen.getByText('+added').className).toContain('bg-green')
    expect(screen.getByText('-removed').className).toContain('bg-red')
    expect(screen.getByText('@@ -1 +1 @@').className).toContain('text-gray')
    expect(screen.getByText('diff --git a/a.py b/a.py').className).toContain(
      'font-bold',
    )
    expect(
      screen.getByText('This diff was truncated by the server.'),
    ).toBeTruthy()
  })

  it('opens the diff tab from ?tab=diff, as the approvals inbox links it', async () => {
    vi.stubGlobal('fetch', mockApi({ diff: 'diff --git a/b.py b/b.py\n+x' }))

    renderView({}, '/runs/run-1?tab=diff')

    expect(await screen.findByText('diff --git a/b.py b/b.py')).toBeTruthy()
    expect(
      screen.getByRole('tab', { name: 'Diff' }).getAttribute('aria-selected'),
    ).toBe('true')
  })

  it('shows the loading state', async () => {
    let resolve!: (response: Response) => void
    const pending = new Promise<Response>((resolvePromise) => {
      resolve = resolvePromise
    })
    vi.stubGlobal(
      'fetch',
      fetchMock(() => pending),
    )

    renderView()

    expect(await screen.findByRole('status')).toBeTruthy()

    resolve(jsonResponse(200, runDetail()))
    expect(await screen.findByText('Fix login bug')).toBeTruthy()
  })

  it('shows the error state and retries', async () => {
    const fetch = fetchMock(() =>
      jsonResponse(500, {
        error: { code: 'internal_error', message: 'server exploded' },
      }),
    )
    vi.stubGlobal('fetch', fetch)

    renderView()

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('server exploded')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledTimes(2)
    })
  })

  it('shows Run not found for an ApiError 404', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock(() =>
        jsonResponse(404, {
          error: { code: 'not_found', message: 'run not found' },
        }),
      ),
    )

    renderView()

    expect(await screen.findByText('Run not found')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Cancel run' })).toBeNull()
  })

  it('summarizes lifecycle events from their payload, not by repeating the type', () => {
    expect(
      summarizeEvent({
        seq: 0,
        ts: '',
        type: 'agent.tool_result',
        payload: { ok: true, decision: 'auto_approved' },
      }),
    ).toBe('ok (auto-approved)')
    expect(
      summarizeEvent({
        seq: 1,
        ts: '',
        type: 'run.node_started',
        payload: { node: 'plan' },
      }),
    ).toBe('plan')
    expect(
      summarizeEvent({
        seq: 2,
        ts: '',
        type: 'approval.requested',
        payload: { kind: 'merge' },
      }),
    ).toBe('merge')
    expect(
      summarizeEvent({
        seq: 3,
        ts: '',
        type: 'approval.decided',
        payload: { action: 'expired' },
      }),
    ).toBe('expired')
    expect(
      summarizeEvent({
        seq: 4,
        ts: '',
        type: 'agent.message',
        payload: { role: 'junior', message: 'Start' },
      }),
    ).toBe('Start')
    expect(
      summarizeEvent({ seq: 5, ts: '', type: 'agent.usage', payload: {} }),
    ).toBe('')
  })

  it('shows why a run failed, on one line', () => {
    expect(
      summarizeEvent({
        seq: 6,
        ts: '',
        type: 'run.failed',
        payload: {
          node: 'prepare_worktree',
          error: 'SetupFailed: setup exited 2:\nerror: no network\n',
        },
      }),
    ).toBe('prepare_worktree: SetupFailed: setup exited 2: · error: no network')
    expect(
      summarizeEvent({
        seq: 7,
        ts: '',
        type: 'run.failed',
        payload: { node: 'plan' },
      }),
    ).toBe('plan')
  })
})
