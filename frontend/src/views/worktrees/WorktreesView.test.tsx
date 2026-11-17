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
import WorktreesView from './WorktreesView'

type RepositoryRead = components['schemas']['RepositoryRead']
type WorktreeItemRead = components['schemas']['WorktreeItemRead']

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

function worktree(overrides: Partial<WorktreeItemRead> = {}): WorktreeItemRead {
  return {
    id: 'worktree-1',
    workspace_id: 'ws-1',
    repo_id: 'repo-1',
    branch: 'gaia/issue-1',
    path: '/work/gaia/worktree-1',
    status: 'ready',
    base_sha: 'abcdef0123456789',
    dirty: false,
    exists: true,
    orphan: false,
    tampered: false,
    created_at: '2026-11-14T09:00:00Z',
    updated_at: '2026-11-14T09:00:00Z',
    ...overrides,
  }
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

let queryClient: ReturnType<typeof makeQueryClient>

function mockFetch(handler: FetchHandler): typeof fetch {
  const mock = fetchMock(handler)
  vi.stubGlobal('fetch', mock)
  return mock
}

function renderView() {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <WorktreesView />
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

describe('repositories', () => {
  it('renders the repository list with commands joined', async () => {
    mockFetch((request) => {
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/worktrees')
      ) {
        return jsonResponse(200, { items: [], unknown_paths: [] })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()

    const heading = await screen.findByRole('heading', { name: 'gaia' })
    const card = heading.closest('article')
    expect(card).not.toBeNull()
    expect(within(card!).getByText('/work/gaia')).toBeTruthy()
    expect(
      within(card!).getByText('https://github.com/example/gaia.git'),
    ).toBeTruthy()
    expect(within(card!).getByText('main')).toBeTruthy()
    expect(within(card!).getByText('uv run pytest -q')).toBeTruthy()
    expect(within(card!).getByText('npm test')).toBeTruthy()
  })

  it('posts split argv, clears on success, and invalidates repositories', async () => {
    let postedBody: unknown
    mockFetch(async (request) => {
      if (
        request.method === 'POST' &&
        request.url.includes('/api/v1/repositories')
      ) {
        postedBody = await request.json()
        return jsonResponse(201, repository)
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/worktrees')
      ) {
        return jsonResponse(200, { items: [], unknown_paths: [] })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByRole('heading', { name: 'gaia' })

    fireEvent.change(screen.getByLabelText('Name'), {
      target: { value: ' gaia ' },
    })
    fireEvent.change(screen.getByLabelText('Local path'), {
      target: { value: '/work/gaia' },
    })
    fireEvent.change(screen.getByLabelText('Setup command'), {
      target: { value: 'uv run pytest -q' },
    })
    fireEvent.change(screen.getByLabelText('Test command'), {
      target: { value: 'npm test' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Register repository' }))

    await waitFor(() => {
      expect(postedBody).toEqual({
        name: 'gaia',
        local_path: '/work/gaia',
        setup_command: ['uv', 'run', 'pytest', '-q'],
        test_command: ['npm', 'test'],
      })
    })
    await waitFor(() => {
      expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('')
      expect(
        (screen.getByLabelText('Local path') as HTMLInputElement).value,
      ).toBe('')
      expect(
        (screen.getByLabelText('Setup command') as HTMLInputElement).value,
      ).toBe('')
      expect(
        (screen.getByLabelText('Test command') as HTMLInputElement).value,
      ).toBe('')
    })
  })

  it('shows a register API error inline', async () => {
    mockFetch(async (request) => {
      if (
        request.method === 'POST' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(422, {
          error: {
            code: 'validation_error',
            message: 'path is not a git repo',
          },
        })
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [])
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/worktrees')
      ) {
        return jsonResponse(200, { items: [], unknown_paths: [] })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByText('No repositories yet')

    fireEvent.change(screen.getByLabelText('Local path'), {
      target: { value: '/work/gaia' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Register repository' }))

    expect(await screen.findByText('path is not a git repo')).toBeTruthy()
  })

  it('unregisters behind a confirm dialog and shows a 409 inline', async () => {
    let deleteUrl = ''
    mockFetch(async (request) => {
      if (request.method === 'DELETE') {
        deleteUrl = request.url
        return jsonResponse(409, {
          error: {
            code: 'conflict',
            message: 'repository has tickets or live worktrees',
          },
        })
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/worktrees')
      ) {
        return jsonResponse(200, { items: [], unknown_paths: [] })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByRole('heading', { name: 'gaia' })

    fireEvent.click(screen.getByRole('button', { name: 'Unregister' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Unregister' }))

    expect(
      await screen.findByText('repository has tickets or live worktrees'),
    ).toBeTruthy()
    expect(deleteUrl).toContain('/api/v1/repositories/repo-1')
  })
})

describe('editing a repository', () => {
  function routes(
    onPatch: (request: Request) => Response | Promise<Response>,
    suggestions: { argv: string[]; times_approved: number }[] = [],
  ) {
    mockFetch(async (request) => {
      if (request.method === 'PATCH') {
        return onPatch(request)
      }
      if (request.url.includes('/command-suggestions')) {
        return jsonResponse(200, { items: suggestions })
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/worktrees')
      ) {
        return jsonResponse(200, { items: [], unknown_paths: [] })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })
  }

  it('saves new commands and clears an empty one', async () => {
    let body: unknown = null
    let url = ''
    routes(async (request) => {
      url = request.url
      body = await request.json()
      return jsonResponse(200, repository)
    })

    renderView()
    await screen.findByRole('heading', { name: 'gaia' })
    fireEvent.click(
      screen.getByRole('button', { name: 'Edit commands for gaia' }),
    )
    const form = screen.getByRole('form', { name: 'Edit commands for gaia' })
    fireEvent.change(within(form).getByLabelText('Setup command'), {
      target: { value: 'make setup-worktree' },
    })
    fireEvent.change(within(form).getByLabelText('Test command'), {
      target: { value: '' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save commands' }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(url).toContain('/api/v1/repositories/repo-1')
    expect(body).toEqual({
      setup_command: ['make', 'setup-worktree'],
      test_command: null,
      allowed_commands: [],
    })
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'Save commands' }),
      ).toBeNull(),
    )
  })

  it('saves allowed commands, one per line, plus the ticked suggestions', async () => {
    let body: unknown = null
    routes(
      async (request) => {
        body = await request.json()
        return jsonResponse(200, repository)
      },
      [
        { argv: ['npm', '--prefix', 'frontend', 'test'], times_approved: 5 },
        { argv: ['npm', 'test'], times_approved: 1 },
      ],
    )

    renderView()
    await screen.findByRole('heading', { name: 'gaia' })
    fireEvent.click(
      screen.getByRole('button', { name: 'Edit commands for gaia' }),
    )
    const form = screen.getByRole('form', { name: 'Edit commands for gaia' })
    fireEvent.change(within(form).getByLabelText('Allowed commands'), {
      target: { value: 'make lint\n\n  npm --prefix frontend run build  \n' },
    })
    const unwanted = await within(form).findByRole('checkbox', {
      name: /^npm test/,
    })
    expect(
      (
        within(form).getByRole('checkbox', {
          name: /npm --prefix frontend test/,
        }) as HTMLInputElement
      ).checked,
    ).toBe(true)
    fireEvent.click(unwanted)
    fireEvent.click(screen.getByRole('button', { name: 'Save commands' }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body).toEqual({
      setup_command: ['uv', 'run', 'pytest', '-q'],
      test_command: ['npm', 'test'],
      allowed_commands: [
        ['make', 'lint'],
        ['npm', '--prefix', 'frontend', 'run', 'build'],
        ['npm', '--prefix', 'frontend', 'test'],
      ],
    })
  })

  it('shows the allowed commands on the repository card', async () => {
    mockFetch((request) =>
      request.url.includes('/api/v1/repositories')
        ? jsonResponse(200, [
            { ...repository, allowed_commands: [['npm', 'run', 'lint']] },
          ])
        : jsonResponse(200, { items: [], unknown_paths: [] }),
    )

    renderView()

    expect(await screen.findByText('npm run lint')).toBeTruthy()
  })

  it('shows an error inline and cancel closes the form', async () => {
    routes(() =>
      jsonResponse(422, {
        error: { code: 'validation_error', message: 'bad command' },
      }),
    )

    renderView()
    await screen.findByRole('heading', { name: 'gaia' })
    fireEvent.click(
      screen.getByRole('button', { name: 'Edit commands for gaia' }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save commands' }))
    expect(await screen.findByText('bad command')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('button', { name: 'Save commands' })).toBeNull()
    expect(
      screen.getByRole('button', { name: 'Edit commands for gaia' }),
    ).toBeTruthy()
  })
})

describe('worktrees', () => {
  it('shows flags and sends repo_id when the repo filter changes', async () => {
    const flaggedWorktree = worktree({
      branch: 'gaia/flagged',
      dirty: true,
      orphan: true,
      exists: false,
      tampered: true,
    })
    let worktreeUrl = ''
    mockFetch((request) => {
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/worktrees')
      ) {
        worktreeUrl = request.url
        return jsonResponse(200, {
          items: [flaggedWorktree],
          unknown_paths: ['/work/gaia/stray'],
        })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByRole('heading', { name: 'gaia' })

    fireEvent.change(screen.getByLabelText('Repository'), {
      target: { value: 'repo-1' },
    })

    expect(await screen.findByText('gaia/flagged')).toBeTruthy()
    expect(worktreeUrl).toContain('repo_id=repo-1')
    expect(screen.getByText('dirty')).toBeTruthy()
    expect(screen.getByText('orphan')).toBeTruthy()
    expect(screen.getByText('missing')).toBeTruthy()
    expect(screen.getByText('tampered')).toBeTruthy()
    expect(screen.getByText('abcdef01')).toBeTruthy()
    expect(screen.getByText('/work/gaia/stray')).toBeTruthy()
  })

  it('removes a dirty worktree only after typing the branch and sends force=true', async () => {
    const dirtyWorktree = worktree({
      branch: 'gaia/dirty',
      dirty: true,
    })
    let deleteUrl = ''
    mockFetch(async (request) => {
      if (request.method === 'DELETE') {
        deleteUrl = request.url
        return jsonResponse(200, dirtyWorktree)
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/worktrees')
      ) {
        return jsonResponse(200, { items: [dirtyWorktree], unknown_paths: [] })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByRole('heading', { name: 'gaia' })
    fireEvent.change(screen.getByLabelText('Repository'), {
      target: { value: 'repo-1' },
    })
    await screen.findByText('gaia/dirty')

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    const dialog = await screen.findByRole('dialog')
    const confirm = within(dialog).getByRole('button', {
      name: 'Remove',
    }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)

    fireEvent.change(within(dialog).getByRole('textbox'), {
      target: { value: 'gaia/dirty' },
    })
    expect(confirm.disabled).toBe(false)
    fireEvent.click(confirm)

    await waitFor(() => {
      expect(deleteUrl).toContain('force=true')
    })
    expect(deleteUrl).toContain('/api/v1/worktrees/worktree-1')
  })

  it('removes a clean worktree without typing and sends force=false', async () => {
    const cleanWorktree = worktree({
      branch: 'gaia/clean',
      dirty: false,
    })
    let deleteUrl = ''
    mockFetch(async (request) => {
      if (request.method === 'DELETE') {
        deleteUrl = request.url
        return jsonResponse(200, cleanWorktree)
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [repository])
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/worktrees')
      ) {
        return jsonResponse(200, { items: [cleanWorktree], unknown_paths: [] })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()
    await screen.findByRole('heading', { name: 'gaia' })
    fireEvent.change(screen.getByLabelText('Repository'), {
      target: { value: 'repo-1' },
    })
    await screen.findByText('gaia/clean')

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    const dialog = await screen.findByRole('dialog')
    const confirm = within(dialog).getByRole('button', {
      name: 'Remove',
    }) as HTMLButtonElement
    expect(confirm.disabled).toBe(false)
    fireEvent.click(confirm)

    await waitFor(() => {
      expect(deleteUrl).toContain('force=false')
    })
  })

  it('shows loading, error and empty states for both sections', async () => {
    mockFetch(() => new Promise<Response>(() => {}))

    renderView()
    expect(screen.getByText('Loading repositories…')).toBeTruthy()
    expect(screen.getByText('Loading worktrees…')).toBeTruthy()
  })

  it('shows error states for both sections', async () => {
    mockFetch(() =>
      jsonResponse(500, {
        error: { code: 'server_error', message: 'repositories exploded' },
      }),
    )

    renderView()

    await waitFor(() => {
      expect(
        screen.getAllByText('repositories exploded').length,
      ).toBeGreaterThanOrEqual(2)
    })
  })

  it('shows empty states for both sections', async () => {
    mockFetch((request) => {
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/repositories')
      ) {
        return jsonResponse(200, [])
      }
      if (
        request.method === 'GET' &&
        request.url.includes('/api/v1/worktrees')
      ) {
        return jsonResponse(200, { items: [], unknown_paths: [] })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: 'not found' },
      })
    })

    renderView()

    expect(await screen.findByText('No repositories yet')).toBeTruthy()
    expect(await screen.findByText('No worktrees yet')).toBeTruthy()
  })
})
