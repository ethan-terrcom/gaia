import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { makeQueryClient } from '../../api/queryClient'
import { RunsView } from './RunsView'

const run = {
  id: 'run-1',
  workspace_id: 'ws-1',
  ticket_id: 'ticket-1',
  repo_id: 'repo-1',
  worktree_id: 'wt-1',
  junior_runtime: 'native_deepseek',
  status: 'implementing',
  graph_thread_id: 'run-1',
  cost_usd: '0.17',
  budget_usd: '5.00',
  started_at: '2026-11-15T09:00:00Z',
  finished_at: null,
  created_by: null,
  created_at: '2026-11-15T08:59:00Z',
  updated_at: '2026-11-15T09:05:00Z',
  ticket_title: 'Recolour the approve button',
  current_step: { index: 0, title: 'Change the colour' },
  pr_number: null,
  pr_url: null,
  ci_status: null,
}

const failedRun = {
  ...run,
  id: 'run-2',
  status: 'failed',
  ticket_title: 'gaia live test',
  current_step: null,
  started_at: null,
  created_at: '2026-11-15T07:00:00Z',
  pr_number: 12,
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fetchMock(
  handler: (request: Request) => Response | Promise<Response>,
): ReturnType<typeof vi.fn> {
  return vi.fn(async (input: Request | URL | string) => {
    const request =
      input instanceof Request ? input : new Request(input as string)
    return handler(request)
  })
}

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{location.search}</div>
}

function renderView(initialEntry = '/runs') {
  const queryClient = makeQueryClient()
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <RunsView />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('RunsView', () => {
  it('lists every run with a link to its detail page', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock(() =>
        jsonResponse(200, { items: [run, failedRun], next_cursor: null }),
      ),
    )

    renderView()

    const link = await screen.findByRole('link', {
      name: 'Recolour the approve button',
    })
    expect(link.getAttribute('href')).toBe('/runs/run-1')
    expect(
      screen.getByRole('link', { name: 'gaia live test' }).getAttribute('href'),
    ).toBe('/runs/run-2')
    expect(screen.getByText('implementing')).toBeTruthy()
    expect(screen.getByText('failed')).toBeTruthy()
    expect(screen.getByText('Step 1: Change the colour')).toBeTruthy()
    expect(screen.getByText('#12')).toBeTruthy()
    expect(screen.getByTitle('2026-11-15T09:00:00Z')).toBeTruthy()
    // A run that never started falls back to when it was created.
    expect(screen.getByTitle('2026-11-15T07:00:00Z')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('shows no current step for a run that has ended', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock(() =>
        jsonResponse(200, {
          items: [{ ...run, status: 'cancelled' }],
          next_cursor: null,
        }),
      ),
    )

    renderView()

    await screen.findByText('cancelled')
    expect(screen.queryByText('Step 1: Change the colour')).toBeNull()
  })

  it('shows the empty state when there are no runs', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock(() => jsonResponse(200, { items: [], next_cursor: null })),
    )

    renderView()

    expect(await screen.findByText('No runs yet.')).toBeTruthy()
  })

  it('passes a terminal status filter to the API and keeps it in the URL', async () => {
    const mock = fetchMock((request) => {
      const status = new URL(request.url).searchParams.get('status')
      return jsonResponse(200, {
        items: status === 'failed' ? [failedRun] : [run, failedRun],
        next_cursor: null,
      })
    })
    vi.stubGlobal('fetch', mock)

    renderView()
    await screen.findByText('Recolour the approve button')

    fireEvent.click(screen.getByRole('button', { name: 'Failed' }))

    expect(await screen.findByText('gaia live test')).toBeTruthy()
    expect(screen.queryByText('Recolour the approve button')).toBeNull()
    const last = mock.mock.calls.at(-1)?.[0] as Request
    expect(new URL(last.url).searchParams.get('status')).toBe('failed')
    expect(screen.getByTestId('location').textContent).toBe('?status=failed')
    expect(
      screen
        .getByRole('button', { name: 'Failed' })
        .getAttribute('aria-pressed'),
    ).toBe('true')

    fireEvent.click(screen.getByRole('button', { name: 'All' }))
    await screen.findByText('Recolour the approve button')
    expect(screen.getByTestId('location').textContent).toBe('')
  })

  it('filters active runs client-side without a status parameter', async () => {
    const mock = fetchMock(() =>
      jsonResponse(200, { items: [run, failedRun], next_cursor: null }),
    )
    vi.stubGlobal('fetch', mock)

    renderView('/runs?status=active')

    expect(await screen.findByText('Recolour the approve button')).toBeTruthy()
    expect(screen.queryByText('gaia live test')).toBeNull()
    const request = mock.mock.calls[0]?.[0] as Request
    expect(new URL(request.url).searchParams.get('status')).toBeNull()
  })

  it('says so when a filter matches nothing', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock(() =>
        jsonResponse(200, { items: [failedRun], next_cursor: null }),
      ),
    )

    renderView('/runs?status=active')

    expect(await screen.findByText('No active runs.')).toBeTruthy()
  })

  it('treats an unknown filter in the URL as all runs', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock(() => jsonResponse(200, { items: [run], next_cursor: null })),
    )

    renderView('/runs?status=bogus')

    await screen.findByText('Recolour the approve button')
    expect(
      screen.getByRole('button', { name: 'All' }).getAttribute('aria-pressed'),
    ).toBe('true')
  })

  it('loads the next page with the cursor', async () => {
    const mock = fetchMock((request) => {
      const cursor = new URL(request.url).searchParams.get('cursor')
      return cursor === 'run-1'
        ? jsonResponse(200, { items: [failedRun], next_cursor: null })
        : jsonResponse(200, { items: [run], next_cursor: 'run-1' })
    })
    vi.stubGlobal('fetch', mock)

    renderView()

    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))

    expect(await screen.findByText('gaia live test')).toBeTruthy()
    expect(screen.getByText('Recolour the approve button')).toBeTruthy()
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /Load/ })).toBeNull(),
    )
  })

  it('shows an error with a retry', async () => {
    let calls = 0
    vi.stubGlobal(
      'fetch',
      fetchMock(() => {
        calls += 1
        return calls === 1
          ? jsonResponse(422, {
              error: { code: 'validation_error', message: 'status: invalid' },
            })
          : jsonResponse(200, { items: [run], next_cursor: null })
      }),
    )

    renderView()

    expect(await screen.findByText('status: invalid')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Recolour the approve button')).toBeTruthy()
  })
})
