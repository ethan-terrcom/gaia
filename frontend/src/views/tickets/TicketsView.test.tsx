import { QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { makeQueryClient } from '../../api/queryClient'
import type { components } from '../../api/schema'
import { TicketsView } from './TicketsView'
import { TEST_CATALOG, TEST_DEFAULT } from '../agent-modes/testData'

type RepositoryRead = components['schemas']['RepositoryRead']
type TicketRead = components['schemas']['TicketRead']
type RunRead = components['schemas']['RunRead']

type FetchHandler = (request: Request) => Response | Promise<Response>

const repository: RepositoryRead = {
  id: 'repo-1',
  workspace_id: 'ws-1',
  name: 'gaia',
  local_path: '/work/gaia',
  remote_url: 'https://github.com/example/gaia.git',
  default_branch: 'main',
  setup_command: ['uv', 'run', 'pytest', '-q'],
  test_command: ['npm', 'test'],
  allowed_commands: [],
  created_at: '2026-11-14T10:00:00Z',
  updated_at: '2026-11-14T10:00:00Z',
  created_by: 'user-1',
}

function ticket(overrides: Partial<TicketRead> = {}): TicketRead {
  return {
    id: 'ticket-1',
    workspace_id: 'ws-1',
    title: 'Fix login bug',
    body: 'The login form fails on submit.',
    repo_id: 'repo-1',
    priority: 1,
    status: 'backlog',
    external_ref: null,
    created_by: null,
    created_at: '2026-11-14T09:00:00Z',
    updated_at: '2026-11-14T10:00:00Z',
    ...overrides,
  }
}

const run: RunRead = {
  id: 'run-1',
  workspace_id: 'ws-1',
  ticket_id: 'ticket-1',
  repo_id: 'repo-1',
  status: 'pending',
  junior_runtime: null,
  budget_usd: '10.00',
  cost_usd: '0.00',
  created_at: '2026-11-14T11:00:00Z',
  updated_at: '2026-11-14T11:00:00Z',
  created_by: 'user-1',
  started_at: null,
  finished_at: null,
  graph_thread_id: null,
  worktree_id: null,
}

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

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname + location.search}</div>
}

let queryClient: ReturnType<typeof makeQueryClient>

function mockFetch(handler: FetchHandler): typeof fetch {
  const mock = fetchMock(handler)
  vi.stubGlobal('fetch', mock)
  return mock
}

function renderView(initialEntry = '/tickets') {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <TicketsView />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function defaultHandler(
  handler: (request: Request) => Response | Promise<Response>,
): FetchHandler {
  return (request) => {
    if (
      request.method === 'GET' &&
      request.url.includes('/api/v1/repositories')
    ) {
      return jsonResponse(200, [repository])
    }
    if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
      return jsonResponse(200, { items: [ticket()], next_cursor: null })
    }
    return handler(request)
  }
}

beforeEach(() => {
  queryClient = makeQueryClient()
  queryClient.setDefaultOptions({ queries: { retry: false } })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('TicketsView', () => {
  it('groups tickets by status on the board', async () => {
    mockFetch((request) => {
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, {
          items: [
            ticket({
              id: 'ticket-1',
              title: 'Backlog ticket',
              status: 'backlog',
            }),
            ticket({
              id: 'ticket-2',
              title: 'Another backlog',
              status: 'backlog',
            }),
            ticket({
              id: 'ticket-3',
              title: 'In progress ticket',
              status: 'in_progress',
            }),
            ticket({ id: 'ticket-4', title: 'Done ticket', status: 'done' }),
          ],
          next_cursor: null,
        })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()

    const backlog = (
      await screen.findByRole('heading', { name: 'Backlog' })
    ).closest('section')
    expect(backlog).not.toBeNull()
    expect(within(backlog!).getByText('Backlog ticket')).toBeTruthy()
    expect(within(backlog!).getByText('Another backlog')).toBeTruthy()
    expect(within(backlog!).queryByText('In progress ticket')).toBeNull()

    const inProgress = screen
      .getByRole('heading', { name: 'In progress' })
      .closest('section')
    expect(within(inProgress!).getByText('In progress ticket')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Done' })).toBeTruthy()
  })

  it('toggles between board and table and keeps the view in the URL', async () => {
    mockFetch(
      defaultHandler(() =>
        jsonResponse(404, {
          error: { code: 'not_found', message: 'not found' },
        }),
      ),
    )

    renderView()

    await screen.findByText('Fix login bug')
    fireEvent.click(screen.getByRole('button', { name: 'Table' }))

    expect(
      await screen.findByRole('columnheader', { name: 'Title' }),
    ).toBeTruthy()
    expect(screen.getByTestId('location').textContent).toBe(
      '/tickets?view=table',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Board' }))

    expect(await screen.findByRole('heading', { name: 'Backlog' })).toBeTruthy()
    expect(screen.getByTestId('location').textContent).toBe('/tickets')
  })

  it('puts the repo filter and search in the URL and in the tickets request', async () => {
    const ticketUrls: string[] = []
    mockFetch((request) => {
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        ticketUrls.push(request.url)
        return jsonResponse(200, { items: [], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByText('No tickets yet.')

    fireEvent.change(screen.getByLabelText('Repository'), {
      target: { value: 'repo-1' },
    })
    fireEvent.change(screen.getByLabelText('Search tickets'), {
      target: { value: 'login' },
    })

    await waitFor(() => {
      expect(
        ticketUrls.some((url) => {
          const params = new URL(url).searchParams
          return (
            params.get('repo_id') === 'repo-1' && params.get('q') === 'login'
          )
        }),
      ).toBe(true)
    })
    expect(screen.getByTestId('location').textContent).toBe(
      '/tickets?repo=repo-1&q=login',
    )
  })

  it('creates a ticket with a POST body and closes on success', async () => {
    let postedBody: unknown
    mockFetch(async (request) => {
      if (
        request.method === 'POST' &&
        request.url.includes('/api/v1/tickets')
      ) {
        postedBody = await request.json()
        return jsonResponse(201, ticket())
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, { items: [], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByText('No tickets yet.')

    fireEvent.click(screen.getByRole('button', { name: 'New ticket' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Title'), {
      target: { value: 'Fix login bug' },
    })
    fireEvent.change(within(dialog).getByLabelText('Body'), {
      target: { value: 'The login form fails on submit.' },
    })
    fireEvent.change(within(dialog).getByLabelText('Repository'), {
      target: { value: 'repo-1' },
    })
    fireEvent.change(within(dialog).getByLabelText('Priority'), {
      target: { value: '2' },
    })
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Create ticket' }),
    )

    await waitFor(() => {
      expect(postedBody).toEqual({
        title: 'Fix login bug',
        body: 'The login form fails on submit.',
        repo_id: 'repo-1',
        priority: 2,
      })
    })
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull()
    })
  })

  it('shows a create API error inline in the modal', async () => {
    mockFetch(async (request) => {
      if (
        request.method === 'POST' &&
        request.url.includes('/api/v1/tickets')
      ) {
        return jsonResponse(422, {
          error: {
            code: 'validation_error',
            message: 'title: tickets must be unique',
          },
        })
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, { items: [], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByText('No tickets yet.')

    fireEvent.click(screen.getByRole('button', { name: 'New ticket' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Title'), {
      target: { value: 'Duplicate title' },
    })
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Create ticket' }),
    )

    expect(
      await within(dialog).findByText('title: tickets must be unique'),
    ).toBeTruthy()
  })

  it('edits a ticket with a PATCH request', async () => {
    let patchUrl = ''
    let patchBody: unknown
    mockFetch(async (request) => {
      if (
        request.method === 'PATCH' &&
        request.url.includes('/api/v1/tickets/ticket-1')
      ) {
        patchUrl = request.url
        patchBody = await request.json()
        return jsonResponse(
          200,
          ticket({ title: 'New title', status: 'in_progress', priority: 3 }),
        )
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, { items: [ticket()], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByText('Fix login bug')

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Title'), {
      target: { value: 'New title' },
    })
    fireEvent.change(within(dialog).getByLabelText('Status'), {
      target: { value: 'in_progress' },
    })
    fireEvent.change(within(dialog).getByLabelText('Priority'), {
      target: { value: '3' },
    })
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Save changes' }),
    )

    await waitFor(() => {
      expect(patchUrl).toContain('/api/v1/tickets/ticket-1')
      expect(patchBody).toEqual({
        title: 'New title',
        body: 'The login form fails on submit.',
        priority: 3,
        status: 'in_progress',
      })
    })
  })

  it('starts a run with only ticket_id and navigates to the new run', async () => {
    let postedBody: unknown
    mockFetch(async (request) => {
      if (request.method === 'POST' && request.url.includes('/api/v1/runs')) {
        postedBody = await request.json()
        return jsonResponse(201, run)
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, { items: [ticket()], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByText('Fix login bug')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))

    await waitFor(() => {
      expect(postedBody).toEqual({ ticket_id: 'ticket-1' })
      expect(screen.getByTestId('location').textContent).toBe('/runs/run-1')
    })
  })

  function startRunFetch(posted: unknown[]) {
    return mockFetch(async (request) => {
      if (request.method === 'POST' && request.url.includes('/api/v1/runs')) {
        posted.push(await request.json())
        return jsonResponse(201, run)
      }
      if (request.url.includes('/api/v1/models')) {
        return jsonResponse(200, TEST_CATALOG)
      }
      if (request.url.includes('/api/v1/admin/agent-mode')) {
        return jsonResponse(200, TEST_DEFAULT)
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, { items: [ticket()], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })
  }

  it('starts a run with the default agent mode and a budget', async () => {
    const posted: unknown[] = []
    startRunFetch(posted)

    renderView()
    await screen.findByText('Fix login bug')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog')
    await within(dialog).findByText(
      'Two tiered: Claude Opus 5.5 → DeepSeek V4 Pro',
    )
    fireEvent.change(within(dialog).getByLabelText('Budget (USD)'), {
      target: { value: '12.50' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))

    await waitFor(() => {
      expect(posted).toEqual([{ ticket_id: 'ticket-1', budget_usd: 12.5 }])
    })
  })

  it('starts a run with an agent mode chosen for this run', async () => {
    const posted: unknown[] = []
    startRunFetch(posted)

    renderView()
    await screen.findByText('Fix login bug')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog')
    const choose = await within(dialog).findByRole('radio', {
      name: 'Choose for this run',
    })
    await waitFor(() => {
      expect((choose as HTMLInputElement).disabled).toBe(false)
    })
    fireEvent.click(choose)
    fireEvent.click(
      within(dialog).getByRole('radio', { name: /Straight shot/ }),
    )
    fireEvent.change(within(dialog).getByLabelText('Model'), {
      target: { value: 'deepseek-flash' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))

    await waitFor(() => {
      expect(posted).toEqual([
        {
          ticket_id: 'ticket-1',
          agent_config: { mode: 'single', lead_model: 'deepseek-flash' },
        },
      ])
    })
  })

  it('shows the active-run 409 message in the start-run dialog', async () => {
    mockFetch(async (request) => {
      if (request.method === 'POST' && request.url.includes('/api/v1/runs')) {
        return jsonResponse(409, {
          error: {
            code: 'conflict',
            message: 'ticket already has an active run',
          },
        })
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, { items: [ticket()], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByText('Fix login bug')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))

    expect(
      await within(dialog).findByText('ticket already has an active run'),
    ).toBeTruthy()
  })

  it('shows the orchestrator_paused 409 message in the start-run dialog', async () => {
    mockFetch(async (request) => {
      if (request.method === 'POST' && request.url.includes('/api/v1/runs')) {
        return jsonResponse(409, {
          error: {
            code: 'orchestrator_paused',
            message:
              'the orchestrator is paused; resume it before starting runs',
          },
        })
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, { items: [ticket()], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByText('Fix login bug')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))

    expect(
      await within(dialog).findByText(
        'the orchestrator is paused; resume it before starting runs',
      ),
    ).toBeTruthy()
  })

  it('shows the daily_budget_exhausted 409 message in the start-run dialog', async () => {
    mockFetch(async (request) => {
      if (request.method === 'POST' && request.url.includes('/api/v1/runs')) {
        return jsonResponse(409, {
          error: {
            code: 'daily_budget_exhausted',
            message: 'daily LLM budget exhausted: 12.00 of 10.00 USD spent',
          },
        })
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, { items: [ticket()], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByText('Fix login bug')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))

    expect(
      await within(dialog).findByText(
        'daily LLM budget exhausted: 12.00 of 10.00 USD spent',
      ),
    ).toBeTruthy()
  })

  it('shows the 503 orchestrator message in the start-run dialog', async () => {
    mockFetch(async (request) => {
      if (request.method === 'POST' && request.url.includes('/api/v1/runs')) {
        return jsonResponse(503, {
          error: {
            code: 'orchestrator_unavailable',
            message: 'the orchestrator is unavailable',
          },
        })
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, { items: [ticket()], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByText('Fix login bug')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))

    expect(
      await within(dialog).findByText('The orchestrator is not running.'),
    ).toBeTruthy()
  })

  it('shows the loading state', async () => {
    const pending = deferred()
    mockFetch((request) => {
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return pending.promise
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()

    expect(await screen.findByRole('status')).toBeTruthy()

    pending.resolve(jsonResponse(200, { items: [ticket()], next_cursor: null }))
    expect(await screen.findByText('Fix login bug')).toBeTruthy()
  })

  it('shows the error state and retries', async () => {
    const fetch = mockFetch((request) => {
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(422, {
          error: {
            code: 'validation_error',
            message: 'q: invalid value',
          },
        })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('q: invalid value')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledTimes(3)
    })
  })

  it('shows the empty state', async () => {
    mockFetch((request) => {
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (request.method === 'GET' && request.url.includes('/api/v1/tickets')) {
        return jsonResponse(200, { items: [], next_cursor: null })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()

    expect(await screen.findByText('No tickets yet.')).toBeTruthy()
    expect(
      screen.getByText('Create a ticket to start planning work.'),
    ).toBeTruthy()
  })
})
