import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { makeQueryClient } from '../../api/queryClient'
import { AutoModeSettings } from './AutoModeSettings'

type FetchHandler = (request: Request) => Response | Promise<Response>

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mockFetch(handler: FetchHandler) {
  const mock = vi.fn(async (input: Request | URL | string) => {
    const request =
      input instanceof Request ? input : new Request(input as string)
    return handler(request)
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

let queryClient: ReturnType<typeof makeQueryClient>

function renderSettings() {
  return render(
    <QueryClientProvider client={queryClient}>
      <AutoModeSettings />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  queryClient = makeQueryClient()
  queryClient.setDefaultOptions({
    queries: { retry: false },
    mutations: { retry: false },
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AutoModeSettings', () => {
  it('shows one switch per model family with its current state', async () => {
    mockFetch(() => jsonResponse(200, { claude: false, deepseek: true }))

    renderSettings()

    const claude = await screen.findByRole('switch', { name: 'Claude' })
    const deepseek = screen.getByRole('switch', { name: 'DeepSeek' })
    expect(claude.getAttribute('aria-checked')).toBe('false')
    expect(deepseek.getAttribute('aria-checked')).toBe('true')
  })

  it('switches one family with a PATCH of only that family', async () => {
    const bodies: unknown[] = []
    mockFetch(async (request) => {
      if (request.method === 'PATCH') {
        bodies.push(await request.json())
        return jsonResponse(200, { claude: true, deepseek: false })
      }
      return jsonResponse(200, { claude: false, deepseek: false })
    })

    renderSettings()
    fireEvent.click(await screen.findByRole('switch', { name: 'Claude' }))

    await waitFor(() => {
      expect(
        screen
          .getByRole('switch', { name: 'Claude' })
          .getAttribute('aria-checked'),
      ).toBe('true')
    })
    expect(bodies).toEqual([{ claude: true }])
    expect(
      screen
        .getByRole('switch', { name: 'DeepSeek' })
        .getAttribute('aria-checked'),
    ).toBe('false')
  })

  it('says why when auto mode cannot be loaded', async () => {
    mockFetch(() =>
      jsonResponse(503, {
        error: {
          code: 'orchestrator_unavailable',
          message: 'the orchestrator is unavailable',
        },
      }),
    )

    renderSettings()

    expect((await screen.findByRole('alert')).textContent).toContain(
      'the orchestrator is unavailable',
    )
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('keeps the old state and shows the error when a change fails', async () => {
    mockFetch((request) =>
      request.method === 'PATCH'
        ? jsonResponse(422, {
            error: { code: 'validation_error', message: 'bad change' },
          })
        : jsonResponse(200, { claude: false, deepseek: false }),
    )

    renderSettings()
    fireEvent.click(await screen.findByRole('switch', { name: 'DeepSeek' }))

    expect((await screen.findByRole('alert')).textContent).toContain(
      'bad change',
    )
    expect(
      screen
        .getByRole('switch', { name: 'DeepSeek' })
        .getAttribute('aria-checked'),
    ).toBe('false')
  })
})
