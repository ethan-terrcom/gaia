import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { makeQueryClient } from '../../api/queryClient'
import { PullRequestsView } from './PullRequests'

const pr = {
  id: 'pr-1',
  number: 42,
  url: 'https://github.com/acme/repo/pull/42',
  state: 'open',
  ci_status: 'passed',
  review_state: 'approved',
  head_sha: 'abcdef0123456789',
  run_id: 'run-1',
  run_status: 'completed',
  ticket_id: 'ticket-1',
  ticket_title: 'Fix login bug',
  repo_name: 'acme/repo',
  created_at: '2026-11-14T09:00:00Z',
  updated_at: '2026-11-14T10:00:00Z',
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
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
  return <div data-testid="location">{location.search}</div>
}

function renderView(initialEntry = '/prs') {
  const queryClient = makeQueryClient()
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <PullRequestsView />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('PullRequestsView', () => {
  it('renders a row with every column', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock(() => jsonResponse(200, { items: [pr] })),
    )

    renderView()

    expect(await screen.findByText('#42')).toBeTruthy()
    expect(screen.getByText('Fix login bug')).toBeTruthy()
    expect(screen.getByText('acme/repo')).toBeTruthy()
    expect(screen.getByText('passed')).toBeTruthy()
    expect(screen.getByText('approved')).toBeTruthy()
    expect(
      screen.getByRole('link', { name: 'completed' }).getAttribute('href'),
    ).toBe('/runs/run-1')
    expect(screen.getByTitle('2026-11-14T09:00:00Z')).toBeTruthy()
  })

  it('links a GitHub PR out in a new tab', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock(() => jsonResponse(200, { items: [pr] })),
    )

    renderView()

    const link = await screen.findByRole('link', { name: '#42' })
    expect(link.getAttribute('href')).toBe(
      'https://github.com/acme/repo/pull/42',
    )
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toBe('noreferrer noopener')
  })

  it('renders a non-GitHub PR URL as text instead of a link', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock(() =>
        jsonResponse(200, {
          items: [{ ...pr, url: 'https://example.com/acme/repo/pull/42' }],
        }),
      ),
    )

    renderView()

    const number = await screen.findByText('#42')
    expect(number.closest('a')).toBeNull()
    expect(screen.queryByRole('link', { name: '#42' })).toBeNull()
  })

  it('changes the API state query and the URL with the filter', async () => {
    const calls: string[] = []
    const fetch = fetchMock((request) => {
      calls.push(request.url)
      return jsonResponse(200, { items: [pr] })
    })
    vi.stubGlobal('fetch', fetch)

    renderView()

    await screen.findByText('#42')
    expect(new URL(calls[0]!).searchParams.get('state')).toBe('open')

    fireEvent.click(screen.getByRole('button', { name: 'Merged' }))

    await waitFor(() => {
      expect(
        calls.some(
          (url) => new URL(url).searchParams.get('state') === 'merged',
        ),
      ).toBe(true)
    })
    expect(screen.getByTestId('location').textContent).toBe('?state=merged')

    fireEvent.click(screen.getByRole('button', { name: 'All' }))

    await waitFor(() => {
      expect(calls.some((url) => !new URL(url).searchParams.has('state'))).toBe(
        true,
      )
    })
    expect(screen.getByTestId('location').textContent).toBe('?state=all')
  })

  it('shows the loading state', async () => {
    const pending = deferred()
    vi.stubGlobal(
      'fetch',
      vi.fn(() => pending.promise) as unknown as typeof fetch,
    )

    renderView()

    expect(await screen.findByRole('status')).toBeTruthy()

    pending.resolve(jsonResponse(200, { items: [pr] }))
    expect(await screen.findByText('#42')).toBeTruthy()
  })

  it('shows the error state and retries', async () => {
    const fetch = fetchMock(() =>
      jsonResponse(422, {
        error: { code: 'validation_error', message: 'state: invalid value' },
      }),
    )
    vi.stubGlobal('fetch', fetch)

    renderView()

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('state: invalid value')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledTimes(2)
    })
  })

  it('shows the empty state', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMock(() => jsonResponse(200, { items: [] })),
    )

    renderView()

    expect(await screen.findByText('No pull requests yet.')).toBeTruthy()
    expect(
      screen.getByText(
        /Gaia opens a draft PR when a run's final review is approved\./,
      ),
    ).toBeTruthy()
  })
})
