import { QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { makeQueryClient } from '../api/queryClient'
import { PauseControl } from './PauseControl'

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

let queryClient: ReturnType<typeof makeQueryClient>

function mockFetch(handler: FetchHandler): typeof fetch {
  const mock = fetchMock(handler)
  vi.stubGlobal('fetch', mock)
  return mock
}

function renderPause(variant: 'button' | 'banner') {
  return render(
    <QueryClientProvider client={queryClient}>
      <PauseControl variant={variant} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  queryClient = makeQueryClient()
  queryClient.setDefaultOptions({ queries: { retry: false } })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('PauseControl', () => {
  it('renders nothing when the pause state endpoint returns 503', async () => {
    const fetch = mockFetch(() =>
      jsonResponse(503, {
        error: {
          code: 'orchestrator_unavailable',
          message: 'the orchestrator is unavailable',
        },
      }),
    )

    renderPause('button')
    renderPause('banner')

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledTimes(1)
      expect(queryClient.getQueryState(['admin', 'pause'])?.status).toBe(
        'error',
      )
    })
    expect(screen.queryByRole('button', { name: 'Pause all' })).toBeNull()
    expect(screen.queryByText(/All runs are paused/)).toBeNull()
  })

  it('renders the pause-all button when not paused', async () => {
    mockFetch(() =>
      jsonResponse(200, { paused: false, since: null, reason: null }),
    )

    renderPause('button')

    expect(
      await screen.findByRole('button', { name: 'Pause all' }),
    ).toBeTruthy()
  })

  it('renders no pause button when already paused', async () => {
    mockFetch(() =>
      jsonResponse(200, {
        paused: true,
        since: '2026-11-15T09:00:00Z',
        reason: 'dinner',
      }),
    )

    renderPause('button')

    await waitFor(() => {
      expect(queryClient.getQueryState(['admin', 'pause'])?.status).toBe(
        'success',
      )
    })
    expect(screen.queryByRole('button', { name: 'Pause all' })).toBeNull()
  })

  it('pauses all with an optional reason and invalidates admin and runs', async () => {
    const posts: Record<string, unknown>[] = []
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        posts.push(await request.json())
        return jsonResponse(200, { paused_run_ids: ['run-1'] })
      }
      return jsonResponse(200, { paused: false, since: null, reason: null })
    })

    renderPause('button')
    fireEvent.click(await screen.findByRole('button', { name: 'Pause all' }))

    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Reason (optional)'), {
      target: { value: 'dinner' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Pause all' }))

    await waitFor(() => {
      expect(posts).toHaveLength(1)
    })
    expect(posts[0]).toEqual({ reason: 'dinner' })
    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['admin'] })
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['runs'] })
    })
  })

  it('shows an inline error when pause-all fails', async () => {
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        return jsonResponse(500, {
          error: { code: 'internal_error', message: 'could not pause runs' },
        })
      }
      return jsonResponse(200, { paused: false, since: null, reason: null })
    })

    renderPause('button')
    fireEvent.click(await screen.findByRole('button', { name: 'Pause all' }))

    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Pause all' }))

    expect(await within(dialog).findByText('could not pause runs')).toBeTruthy()
  })

  it('resumes all directly from the banner and invalidates admin and runs', async () => {
    const posts: string[] = []
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        posts.push(request.url)
        return jsonResponse(200, { resumed_run_ids: ['run-1'] })
      }
      return jsonResponse(200, {
        paused: true,
        since: '2026-11-15T09:00:00Z',
        reason: 'dinner',
      })
    })

    renderPause('banner')

    expect(await screen.findByText(/All runs are paused since/)).toBeTruthy()
    expect(screen.getByText(/\(dinner\)/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Resume all' }))

    await waitFor(() => {
      expect(posts).toHaveLength(1)
    })
    expect(posts[0]).toContain('/api/v1/admin/resume-all')
    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['admin'] })
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['runs'] })
    })
  })
})
