import { QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { makeQueryClient } from '../../api/queryClient'
import { TEST_CATALOG, TEST_DEFAULT } from '../agent-modes/testData'
import { AgentModeSettings } from './AgentModeSettings'

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

function serve(puts: unknown[], putResponse?: (body: unknown) => Response) {
  return mockFetch(async (request) => {
    if (request.url.includes('/api/v1/models')) {
      return jsonResponse(200, TEST_CATALOG)
    }
    if (request.method === 'PUT') {
      const body: unknown = await request.json()
      puts.push(body)
      return putResponse
        ? putResponse(body)
        : jsonResponse(200, { config: body, saved: true })
    }
    return jsonResponse(200, TEST_DEFAULT)
  })
}

let queryClient: ReturnType<typeof makeQueryClient>

function renderSettings() {
  return render(
    <QueryClientProvider client={queryClient}>
      <AgentModeSettings />
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

describe('AgentModeSettings', () => {
  it('shows the current default and its models', async () => {
    serve([])

    renderSettings()

    expect(
      await screen.findByText('Two tiered: Claude Opus 5.5 → DeepSeek V4 Pro'),
    ).toBeTruthy()
    expect(screen.getByText(/Default from settings/)).toBeTruthy()
    const lead = screen.getByLabelText('Lead (plans and reviews)')
    expect((lead as HTMLSelectElement).value).toBe('claude-opus-5-5')
    expect(
      (
        screen.getByRole('button', {
          name: 'Save default',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
  })

  it('saves a wisdom-of-crowds default and flags the missing keys', async () => {
    const puts: unknown[] = []
    serve(puts)

    renderSettings()
    fireEvent.click(
      await screen.findByRole('radio', { name: /Wisdom of crowds/ }),
    )
    const save = screen.getByRole('button', { name: 'Save default' })
    expect((save as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('Pick 2 to 5 panel models.')).toBeTruthy()

    const panel = within(screen.getByRole('group', { name: 'Panel' }))
    fireEvent.click(
      panel.getByRole('checkbox', { name: /GPT-6.2 Sol.*key not set/ }),
    )
    fireEvent.click(panel.getByRole('checkbox', { name: /^DeepSeek Flash/ }))
    expect(screen.getByText('gaia secrets set OPENAI_API_KEY')).toBeTruthy()
    fireEvent.click(save)

    await waitFor(() => {
      expect(puts).toEqual([
        {
          mode: 'crowd',
          lead_model: 'claude-opus-5-5',
          worker_models: ['deepseek-v4-pro'],
          panel_models: ['gpt-6.2-sol', 'deepseek-flash'],
        },
      ])
    })
    expect(await screen.findByText(/Saved default/)).toBeTruthy()
  })

  it('caps the worker pool at four models', async () => {
    serve([])

    renderSettings()
    await screen.findByText(/Default from settings/)
    const workers = screen
      .getAllByRole('checkbox')
      .filter((box) => !(box as HTMLInputElement).checked)
    for (const box of workers.slice(0, 3)) {
      fireEvent.click(box)
    }

    const left = screen
      .getAllByRole('checkbox')
      .filter((box) => !(box as HTMLInputElement).checked)
    expect(left).toHaveLength(1)
    expect((left[0] as HTMLInputElement).disabled).toBe(true)
  })

  it('switches to straight shot, discards, and shows a save error', async () => {
    const puts: unknown[] = []
    serve(puts, () =>
      jsonResponse(422, {
        error: {
          code: 'unknown_model',
          message: "model 'x' is not in the catalog",
        },
      }),
    )

    renderSettings()
    fireEvent.click(await screen.findByRole('radio', { name: /Straight shot/ }))
    expect(screen.getByLabelText('Model')).toBeTruthy()
    expect(screen.queryByText('Workers')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(screen.getByLabelText('Lead (plans and reviews)')).toBeTruthy()

    fireEvent.click(screen.getByRole('radio', { name: /Straight shot/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Save default' }))

    expect(
      await screen.findByText("model 'x' is not in the catalog"),
    ).toBeTruthy()
    expect(puts).toEqual([{ mode: 'single', lead_model: 'claude-opus-5-5' }])
  })

  it('asks for a model when the lead is cleared', async () => {
    serve([])

    renderSettings()
    const lead = await screen.findByLabelText('Lead (plans and reviews)')
    fireEvent.change(lead, { target: { value: '' } })

    expect(screen.getByText('Pick a lead model.')).toBeTruthy()
  })

  it('shows a load error', async () => {
    mockFetch(() =>
      jsonResponse(503, {
        error: {
          code: 'orchestrator_unavailable',
          message: 'the orchestrator is unavailable',
        },
      }),
    )

    renderSettings()

    expect(await screen.findByRole('alert')).toBeTruthy()
  })
})
