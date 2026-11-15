import { QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { makeQueryClient } from '../../api/queryClient'
import type { components } from '../../api/schema'
import { ApprovalsView } from './ApprovalsView'

type ApprovalRead = components['schemas']['ApprovalRead']

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

function approval(overrides: Partial<ApprovalRead> = {}): ApprovalRead {
  return {
    id: 'approval-1',
    workspace_id: 'ws-1',
    run_id: 'run-1',
    idempotency_key: 'approval:1',
    kind: 'plan',
    payload_json: {},
    status: 'pending',
    decided_by: null,
    decided_at: null,
    comment: null,
    created_at: '2026-11-14T09:00:00Z',
    updated_at: '2026-11-14T09:00:00Z',
    ...overrides,
  }
}

function planStep(overrides: Record<string, unknown> = {}) {
  return {
    title: 'Write code',
    instructions: 'Implement the thing',
    files_likely_touched: ['src/a.ts', 'src/b.ts'],
    acceptance: ['Tests pass'],
    test_command: ['uv', 'run', 'pytest', '-q'],
    ...overrides,
  }
}

function planApproval(overrides: Partial<ApprovalRead> = {}): ApprovalRead {
  return approval({
    payload_json: {
      kind: 'plan',
      plan: {
        summary: 'Ship it',
        risks: ['risk one'],
        steps: [planStep()],
      },
    },
    ...overrides,
  })
}

let queryClient: ReturnType<typeof makeQueryClient>

function mockFetch(handler: FetchHandler): typeof fetch {
  const mock = fetchMock(handler)
  vi.stubGlobal('fetch', mock)
  return mock
}

function renderView() {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/approvals']}>
        <ApprovalsView />
      </MemoryRouter>
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

describe('ApprovalsView', () => {
  it('shows the loading state', async () => {
    const pending = deferred()
    vi.stubGlobal(
      'fetch',
      vi.fn(() => pending.promise) as unknown as typeof fetch,
    )

    renderView()

    expect(await screen.findByRole('status')).toBeTruthy()

    pending.resolve(
      jsonResponse(200, { items: [planApproval()], total_pending: 1 }),
    )
    expect(await screen.findByText('Ship it')).toBeTruthy()
  })

  it('shows the error state and retries', async () => {
    const fetch = mockFetch(() =>
      jsonResponse(422, {
        error: { code: 'validation_error', message: 'status: invalid value' },
      }),
    )

    renderView()

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('status: invalid value')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledTimes(2)
    })
  })

  it('shows the empty inbox', async () => {
    mockFetch(() => jsonResponse(200, { items: [], total_pending: 0 }))

    renderView()

    expect(await screen.findByText('Nothing needs you')).toBeTruthy()
    expect(screen.getByText(/When an agent needs a decision/)).toBeTruthy()
  })

  it('renders a plan approval detail', async () => {
    mockFetch(() =>
      jsonResponse(200, { items: [planApproval()], total_pending: 1 }),
    )

    renderView()

    expect(await screen.findByText('Ship it')).toBeTruthy()
    expect(screen.getByText('risk one')).toBeTruthy()
    expect(screen.getByText(/Write code/)).toBeTruthy()
    expect(screen.getByText('Implement the thing')).toBeTruthy()
    expect(screen.getByText('src/a.ts, src/b.ts')).toBeTruthy()
    expect(screen.getByText('Tests pass')).toBeTruthy()
    expect(screen.getByText('uv run pytest -q')).toBeTruthy()
  })

  it('renders a command with each argv element in its own code element', async () => {
    mockFetch(() =>
      jsonResponse(200, {
        items: [
          approval({
            kind: 'command',
            payload_json: {
              argv: ['pytest', '-q', '--tb=short'],
              cwd: '/work/gaia',
              tool: 'run_command',
              reason: 'run the tests',
            },
          }),
        ],
        total_pending: 1,
      }),
    )

    renderView()

    expect(
      await screen.findByText(
        'This command runs inside the sandbox with your approval.',
      ),
    ).toBeTruthy()
    expect(screen.getByText('pytest').tagName).toBe('CODE')
    expect(screen.getByText('-q').tagName).toBe('CODE')
    expect(screen.getByText('--tb=short').tagName).toBe('CODE')
    expect(screen.getByText('/work/gaia')).toBeTruthy()
    expect(screen.getByText('run_command')).toBeTruthy()
    expect(screen.getByText('run the tests')).toBeTruthy()
  })

  it('renders pr_open review and diff summaries and links to the diff tab', async () => {
    mockFetch(() =>
      jsonResponse(200, {
        items: [
          approval({
            kind: 'pr_open',
            payload_json: {
              review_summary: 'Looks good',
              diff_summary: '12 files changed',
            },
          }),
        ],
        total_pending: 1,
      }),
    )

    renderView()

    expect(await screen.findByText('Looks good')).toBeTruthy()
    expect(screen.getByText('12 files changed')).toBeTruthy()
    expect(
      screen.getByRole('link', { name: 'View diff' }).getAttribute('href'),
    ).toBe('/runs/run-1?tab=diff')
  })

  it('renders budget figures and sends a new budget with approve', async () => {
    const posts: Record<string, unknown>[] = []
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        posts.push(await request.json())
        return jsonResponse(202, {
          approval_id: 'approval-1',
          status: 'accepted',
        })
      }
      return jsonResponse(200, {
        items: [
          approval({
            kind: 'budget',
            payload_json: {
              cost_usd: '2.50',
              budget_usd: '10.00',
            },
          }),
        ],
        total_pending: 1,
      })
    })

    renderView()

    expect(await screen.findByText('$2.50')).toBeTruthy()
    expect(screen.getByText('$10.00')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('New budget (USD, optional)'), {
      target: { value: '25.00' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    await waitFor(() => {
      expect(posts).toHaveLength(1)
    })
    expect(posts[0]).toMatchObject({
      decision: 'approve',
      budget_usd: '25.00',
    })
  })

  it('renders daily-scope budget figures and approves without budget_usd', async () => {
    const posts: Record<string, unknown>[] = []
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        posts.push(await request.json())
        return jsonResponse(202, {
          approval_id: 'approval-1',
          status: 'accepted',
        })
      }
      return jsonResponse(200, {
        items: [
          approval({
            kind: 'budget',
            payload_json: {
              scope: 'daily',
              spent_today_usd: '3.75',
              daily_budget_usd: '20.00',
            },
          }),
        ],
        total_pending: 1,
      })
    })

    renderView()

    expect(await screen.findByText('Spent today')).toBeTruthy()
    expect(screen.getByText('$3.75')).toBeTruthy()
    expect(screen.getByText('Daily cap')).toBeTruthy()
    expect(screen.getByText('$20.00')).toBeTruthy()
    expect(screen.getByText(/The daily cap is a hard limit/)).toBeTruthy()
    expect(screen.queryByLabelText('New budget (USD, optional)')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    await waitFor(() => {
      expect(posts).toHaveLength(1)
    })
    expect(posts[0]).toMatchObject({ decision: 'approve' })
    expect(posts[0]?.budget_usd).toBeUndefined()
  })

  it('renders escalation reason and feedback', async () => {
    mockFetch(() =>
      jsonResponse(200, {
        items: [
          approval({
            kind: 'escalation',
            payload_json: {
              reason: 'tests flaked',
              feedback: 'rerun the suite',
            },
          }),
        ],
        total_pending: 1,
      }),
    )

    renderView()

    expect(await screen.findByText('tests flaked')).toBeTruthy()
    expect(screen.getByText('rerun the suite')).toBeTruthy()
    expect(screen.getByText(/Approve retries the step/)).toBeTruthy()
  })

  it('validates an empty plan step title and posts an edit body', async () => {
    const posts: Record<string, unknown>[] = []
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        posts.push(await request.json())
        return jsonResponse(202, {
          approval_id: 'approval-1',
          status: 'accepted',
        })
      }
      return jsonResponse(200, { items: [planApproval()], total_pending: 1 })
    })

    renderView()
    await screen.findByText('Ship it')

    fireEvent.click(screen.getByRole('button', { name: 'Edit steps' }))
    fireEvent.change(screen.getByLabelText('Title'), {
      target: { value: '   ' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Approve with edits' }))

    expect(await screen.findByText('title must not be empty')).toBeTruthy()
    expect(posts).toHaveLength(0)

    fireEvent.change(screen.getByLabelText('Title'), {
      target: { value: 'Updated title' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Approve with edits' }))

    await waitFor(() => {
      expect(posts).toHaveLength(1)
    })
    expect(posts[0]).toMatchObject({
      decision: 'edit',
      edited_payload: {
        summary: 'Ship it',
        risks: ['risk one'],
        steps: [{ title: 'Updated title' }],
      },
    })
  })

  it('blocks a plan with 11 steps', async () => {
    const posts: Record<string, unknown>[] = []
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        posts.push(await request.json())
        return jsonResponse(202, {
          approval_id: 'approval-1',
          status: 'accepted',
        })
      }
      return jsonResponse(200, {
        items: [
          approval({
            kind: 'plan',
            payload_json: {
              kind: 'plan',
              plan: {
                summary: 'Too many steps',
                risks: [],
                steps: Array.from({ length: 11 }, (_, index) =>
                  planStep({ title: `Step ${index + 1}` }),
                ),
              },
            },
          }),
        ],
        total_pending: 1,
      })
    })

    renderView()
    await screen.findByText('Too many steps')

    fireEvent.click(screen.getByRole('button', { name: 'Edit steps' }))
    fireEvent.click(screen.getByRole('button', { name: 'Approve with edits' }))

    expect(
      await screen.findByText('a plan can have at most 10 steps'),
    ).toBeTruthy()
    expect(posts).toHaveLength(0)
  })

  it('needs the typed merge phrase and sends confirm; a does nothing on merge', async () => {
    const posts: Record<string, unknown>[] = []
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        posts.push(await request.json())
        return jsonResponse(202, {
          approval_id: 'approval-1',
          status: 'accepted',
        })
      }
      return jsonResponse(200, {
        items: [
          approval({
            kind: 'merge',
            payload_json: {
              pr_number: 42,
              pr_url: 'https://github.com/acme/repo/pull/42',
            },
          }),
        ],
        total_pending: 1,
      })
    })

    renderView()
    await screen.findByText('#42')

    fireEvent.keyDown(window, { key: 'a' })
    expect(posts).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    const dialog = await screen.findByRole('dialog')
    const confirm = within(dialog).getByRole('button', {
      name: 'Approve merge',
    }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)

    fireEvent.change(within(dialog).getByRole('textbox'), {
      target: { value: 'merge 42' },
    })
    expect(confirm.disabled).toBe(false)
    fireEvent.click(confirm)

    await waitFor(() => {
      expect(posts).toHaveLength(1)
    })
    expect(posts[0]).toMatchObject({
      decision: 'approve',
      confirm: 'merge 42',
    })
  })

  it('moves selection with j and k', async () => {
    mockFetch(() =>
      jsonResponse(200, {
        items: [
          planApproval({
            id: 'approval-1',
            payload_json: {
              kind: 'plan',
              plan: {
                summary: 'First plan summary',
                risks: [],
                steps: [planStep()],
              },
            },
          }),
          planApproval({
            id: 'approval-2',
            payload_json: {
              kind: 'plan',
              plan: {
                summary: 'Second plan summary',
                risks: [],
                steps: [planStep()],
              },
            },
          }),
        ],
        total_pending: 2,
      }),
    )

    renderView()
    expect(await screen.findByText('First plan summary')).toBeTruthy()

    fireEvent.keyDown(window, { key: 'j' })
    expect(await screen.findByText('Second plan summary')).toBeTruthy()

    fireEvent.keyDown(window, { key: 'k' })
    expect(await screen.findByText('First plan summary')).toBeTruthy()
  })

  it('sends approve and reject decisions from the keyboard', async () => {
    const posts: Record<string, unknown>[] = []
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        posts.push(await request.json())
        return jsonResponse(202, {
          approval_id: 'approval-1',
          status: 'accepted',
        })
      }
      return jsonResponse(200, { items: [planApproval()], total_pending: 1 })
    })

    renderView()
    await screen.findByText('Ship it')

    fireEvent.keyDown(window, { key: 'a' })
    await waitFor(() => {
      expect(posts).toHaveLength(1)
    })
    expect(posts[0]).toMatchObject({ decision: 'approve' })
  })

  it('sends reject from the keyboard', async () => {
    const posts: Record<string, unknown>[] = []
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        posts.push(await request.json())
        return jsonResponse(202, {
          approval_id: 'approval-1',
          status: 'accepted',
        })
      }
      return jsonResponse(200, { items: [planApproval()], total_pending: 1 })
    })

    renderView()
    await screen.findByText('Ship it')

    fireEvent.keyDown(window, { key: 'r' })
    await waitFor(() => {
      expect(posts).toHaveLength(1)
    })
    expect(posts[0]).toMatchObject({ decision: 'reject' })
  })

  it('ignores hotkeys while focus is in a textarea', async () => {
    const posts: Record<string, unknown>[] = []
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        posts.push(await request.json())
        return jsonResponse(202, {
          approval_id: 'approval-1',
          status: 'accepted',
        })
      }
      return jsonResponse(200, { items: [planApproval()], total_pending: 1 })
    })

    renderView()
    await screen.findByText('Ship it')

    const textarea = screen.getByLabelText('Comment (optional)')
    fireEvent.keyDown(textarea, { key: 'a' })
    fireEvent.keyDown(textarea, { key: 'r' })

    expect(posts).toHaveLength(0)
  })

  it('shows a 409 message inline', async () => {
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        return jsonResponse(409, {
          error: {
            code: 'approval_not_pending',
            message: "approval 'approval-1' is approved, not pending",
          },
        })
      }
      return jsonResponse(200, { items: [planApproval()], total_pending: 1 })
    })

    renderView()
    await screen.findByText('Ship it')

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    expect(
      await screen.findByText("approval 'approval-1' is approved, not pending"),
    ).toBeTruthy()
  })

  it('shows an orchestrator_paused 409 message inline', async () => {
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        return jsonResponse(409, {
          error: {
            code: 'orchestrator_paused',
            message: 'the orchestrator is paused; resume it before deciding',
          },
        })
      }
      return jsonResponse(200, { items: [planApproval()], total_pending: 1 })
    })

    renderView()
    await screen.findByText('Ship it')

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    expect(
      await screen.findByText(
        'the orchestrator is paused; resume it before deciding',
      ),
    ).toBeTruthy()
  })

  it('shows a 422 message inline', async () => {
    mockFetch(async (request) => {
      if (request.method === 'POST') {
        return jsonResponse(422, {
          error: {
            code: 'validation_error',
            message: 'steps: too many steps',
          },
        })
      }
      return jsonResponse(200, { items: [planApproval()], total_pending: 1 })
    })

    renderView()
    await screen.findByText('Ship it')

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    expect(await screen.findByText('steps: too many steps')).toBeTruthy()
  })

  it('switches to recent decided approvals, newest first', async () => {
    const calls: string[] = []
    mockFetch(async (request) => {
      calls.push(request.url)
      if (
        request.method === 'GET' &&
        !new URL(request.url).searchParams.has('status')
      ) {
        return jsonResponse(200, { items: [], total_pending: 0 })
      }
      return jsonResponse(200, { items: [planApproval()], total_pending: 1 })
    })

    renderView()
    await screen.findByText('Ship it')
    expect(new URL(calls[0]!).searchParams.get('status')).toBe('pending')

    fireEvent.click(screen.getByRole('button', { name: 'Decided' }))

    expect(await screen.findByText('No decided approvals')).toBeTruthy()
    await waitFor(() => {
      expect(
        calls.some(
          (url) =>
            !new URL(url).searchParams.has('status') &&
            new URL(url).searchParams.get('decided') === 'true' &&
            new URL(url).searchParams.get('order') === 'newest' &&
            new URL(url).searchParams.get('limit') === '50',
        ),
      ).toBe(true)
    })
  })

  it.each([
    ['the refetch drops the decided item', false],
    ['the refetch still lists it (202 applies asynchronously)', true],
  ])(
    'selects the next item after a decision when %s',
    async (_label, stillListed) => {
      const first = planApproval({ id: 'approval-a' })
      const second = planApproval({
        id: 'approval-b',
        payload_json: {
          kind: 'plan',
          plan: { summary: 'Second plan', risks: [], steps: [planStep()] },
        },
      })
      const third = planApproval({
        id: 'approval-c',
        payload_json: {
          kind: 'plan',
          plan: { summary: 'Third plan', risks: [], steps: [planStep()] },
        },
      })
      let decided = false
      mockFetch(async (request) => {
        if (request.method === 'POST') {
          decided = true
          return jsonResponse(202, {
            approval_id: 'approval-a',
            status: 'accepted',
          })
        }
        const items =
          decided && !stillListed ? [second, third] : [first, second, third]
        return jsonResponse(200, { items, total_pending: items.length })
      })

      renderView()
      await screen.findByText('Ship it')
      fireEvent.click(screen.getByTestId('approve-button'))

      expect(await screen.findByText('Second plan')).toBeTruthy()
      expect(screen.queryByText('Third plan')).toBeNull()
    },
  )

  it('shows no decision buttons for an approval that is already decided', async () => {
    mockFetch(async () =>
      jsonResponse(200, {
        items: [planApproval({ status: 'approved' })],
        total_pending: 0,
      }),
    )

    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Decided' }))
    await screen.findByText('Ship it')

    expect(screen.queryByTestId('approve-button')).toBeNull()
    expect(screen.queryByTestId('reject-button')).toBeNull()
  })

  it('shows the lead review verdict and issues on a pr_open approval', async () => {
    mockFetch(async () =>
      jsonResponse(200, {
        items: [
          approval({
            kind: 'pr_open',
            payload_json: {
              kind: 'pr_open',
              review: {
                verdict: 'accept',
                feedback: 'Looks right.',
                issues: ['Docstring is thin'],
              },
            },
          }),
        ],
        total_pending: 1,
      }),
    )

    renderView()

    expect(await screen.findByText('accept')).toBeTruthy()
    expect(screen.getByText('Looks right.')).toBeTruthy()
    expect(screen.getByText('Docstring is thin')).toBeTruthy()
  })
})
