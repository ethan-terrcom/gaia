import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { api } from '../../api/client'
import { ApiError } from '../../api/errors'
import type { components } from '../../api/schema'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
} from '../../components/States'
import { StatusBadge } from '../../components/StatusBadge'
import { formatRelative, shortSha } from '../../components/format'

type RepositoryRead = components['schemas']['RepositoryRead']
type WorktreeItemRead = components['schemas']['WorktreeItemRead']
type WorktreeListRead = components['schemas']['WorktreeListRead']

async function fetchRepositories(): Promise<RepositoryRead[]> {
  const { data, error } = await api.GET('/api/v1/repositories', {
    fetch: globalThis.fetch,
  })
  if (error) {
    throw error
  }
  return data
}

async function fetchWorktrees(repoId: string): Promise<WorktreeListRead> {
  const { data, error } = await api.GET('/api/v1/worktrees', {
    fetch: globalThis.fetch,
    params: { query: { repo_id: repoId } },
  })
  if (error) {
    throw error
  }
  return data
}

/**
 * The generated/backend route still requires ``repo_id`` (backend
 * ``api/routes/worktrees.py``). "All" therefore fans out one typed request per
 * registered repository instead of sending a single request with no filter.
 */
async function fetchWorktreesForFilter(
  repoId: string | null,
  queryClient: QueryClient,
): Promise<WorktreeListRead> {
  if (repoId !== null) {
    return fetchWorktrees(repoId)
  }

  const repositories = await queryClient.fetchQuery({
    queryKey: ['repositories'],
    queryFn: fetchRepositories,
  })
  const responses = await Promise.all(
    repositories.map(async (repository) => {
      const { data, error } = await api.GET('/api/v1/worktrees', {
        fetch: globalThis.fetch,
        params: { query: { repo_id: repository.id } },
      })
      if (error) {
        throw error
      }
      return data
    }),
  )

  return {
    items: responses.flatMap((response) => response.items),
    unknown_paths: responses.flatMap((response) => response.unknown_paths),
  }
}

async function registerRepository(input: {
  name: string
  local_path: string
  setup_command: string[] | null
  test_command: string[] | null
}): Promise<RepositoryRead> {
  const { data, error } = await api.POST('/api/v1/repositories', {
    fetch: globalThis.fetch,
    body: {
      name: input.name.trim() === '' ? null : input.name.trim(),
      local_path: input.local_path,
      setup_command: input.setup_command,
      test_command: input.test_command,
    },
  })
  if (error) {
    throw error
  }
  return data
}

async function updateRepositoryCommands(input: {
  id: string
  setup_command: string[] | null
  test_command: string[] | null
}): Promise<RepositoryRead> {
  const { data, error } = await api.PATCH(
    '/api/v1/repositories/{repository_id}',
    {
      fetch: globalThis.fetch,
      params: { path: { repository_id: input.id } },
      body: {
        setup_command: input.setup_command,
        test_command: input.test_command,
      },
    },
  )
  if (error) {
    throw error
  }
  return data
}

async function unregisterRepository(repositoryId: string): Promise<void> {
  const { error } = await api.DELETE('/api/v1/repositories/{repository_id}', {
    fetch: globalThis.fetch,
    params: { path: { repository_id: repositoryId } },
  })
  if (error) {
    throw error
  }
}

async function removeWorktree(
  worktreeId: string,
  force: boolean,
): Promise<void> {
  const { error } = await api.DELETE('/api/v1/worktrees/{worktree_id}', {
    fetch: globalThis.fetch,
    params: { path: { worktree_id: worktreeId }, query: { force } },
  })
  if (error) {
    throw error
  }
}

function useRepositories() {
  return useQuery({
    queryKey: ['repositories'],
    queryFn: fetchRepositories,
  })
}

function useWorktrees(repoId: string | null) {
  const queryClient = useQueryClient()
  return useQuery({
    queryKey: ['worktrees', { repoId }],
    queryFn: () => fetchWorktreesForFilter(repoId, queryClient),
  })
}

function parseCommand(value: string): string[] | null {
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed.split(/\s+/)
}

function commandText(command: string[] | null | undefined): string {
  return command && command.length > 0 ? command.join(' ') : '—'
}

function InlineApiError({ error }: { error: unknown }) {
  const message =
    error instanceof ApiError
      ? error.message
      : 'Something went wrong while saving this change.'
  return (
    <p
      role="alert"
      className="mt-3 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
    >
      {message}
    </p>
  )
}

function RemoteUrl({ url }: { url: string | null }) {
  if (!url) {
    return <span>—</span>
  }
  if (url.startsWith('https://github.com/')) {
    return (
      <a href={url} target="_blank" rel="noreferrer noopener">
        {url}
      </a>
    )
  }
  return <span>{url}</span>
}

function EditCommandsForm({
  repository,
  onDone,
}: {
  repository: RepositoryRead
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [setupCommand, setSetupCommand] = useState(
    repository.setup_command?.join(' ') ?? '',
  )
  const [testCommand, setTestCommand] = useState(
    repository.test_command?.join(' ') ?? '',
  )
  const mutation = useMutation({
    mutationFn: updateRepositoryCommands,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['repositories'] })
      onDone()
    },
  })

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    mutation.mutate({
      id: repository.id,
      setup_command: parseCommand(setupCommand),
      test_command: parseCommand(testCommand),
    })
  }

  return (
    <form
      onSubmit={onSubmit}
      aria-label={`Edit commands for ${repository.name}`}
      className="mt-3 grid gap-3 text-sm"
    >
      <div>
        <label htmlFor={`edit-setup-${repository.id}`} className="block">
          Setup command
        </label>
        <input
          id={`edit-setup-${repository.id}`}
          value={setupCommand}
          onChange={(event) => setSetupCommand(event.target.value)}
          autoComplete="off"
          className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
        />
      </div>
      <div>
        <label htmlFor={`edit-test-${repository.id}`} className="block">
          Test command
        </label>
        <input
          id={`edit-test-${repository.id}`}
          value={testCommand}
          onChange={(event) => setTestCommand(event.target.value)}
          autoComplete="off"
          className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
        />
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        Split on whitespace, no shell. Leave a box empty to clear it. The next
        run uses the new commands.
      </p>
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={mutation.isPending}
          className="rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
        >
          {mutation.isPending ? 'Saving…' : 'Save commands'}
        </button>
        <button
          type="button"
          onClick={onDone}
          className="rounded border border-gray-300 px-3 py-1.5 dark:border-gray-700"
        >
          Cancel
        </button>
      </div>
      {mutation.isError ? <InlineApiError error={mutation.error} /> : null}
    </form>
  )
}

function RepositoryCard({
  repository,
  onUnregister,
}: {
  repository: RepositoryRead
  onUnregister: () => void
}) {
  const [editing, setEditing] = useState(false)
  return (
    <article className="rounded border border-gray-200 p-4 dark:border-gray-800">
      <div className="flex items-start justify-between gap-4">
        <h3 className="font-semibold">{repository.name}</h3>
        <div className="flex gap-2">
          {editing ? null : (
            <button
              type="button"
              onClick={() => setEditing(true)}
              aria-label={`Edit commands for ${repository.name}`}
              className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-700"
            >
              Edit commands
            </button>
          )}
          <button
            type="button"
            onClick={onUnregister}
            className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-700"
          >
            Unregister
          </button>
        </div>
      </div>
      <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Local path</dt>
          <dd>
            <code className="font-mono">{repository.local_path}</code>
          </dd>
        </div>
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Remote URL</dt>
          <dd>
            <RemoteUrl url={repository.remote_url} />
          </dd>
        </div>
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Default branch</dt>
          <dd>
            <code className="font-mono">{repository.default_branch}</code>
          </dd>
        </div>
        {editing ? null : (
          <>
            <div>
              <dt className="text-gray-500 dark:text-gray-400">
                Setup command
              </dt>
              <dd>
                <code className="font-mono">
                  {commandText(repository.setup_command)}
                </code>
              </dd>
            </div>
            <div>
              <dt className="text-gray-500 dark:text-gray-400">Test command</dt>
              <dd>
                <code className="font-mono">
                  {commandText(repository.test_command)}
                </code>
              </dd>
            </div>
          </>
        )}
      </dl>
      {editing ? (
        <EditCommandsForm
          repository={repository}
          onDone={() => setEditing(false)}
        />
      ) : null}
    </article>
  )
}

function RegisterRepositoryForm() {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [localPath, setLocalPath] = useState('')
  const [setupCommand, setSetupCommand] = useState('')
  const [testCommand, setTestCommand] = useState('')
  const mutation = useMutation({
    mutationFn: registerRepository,
    onSuccess: () => {
      setName('')
      setLocalPath('')
      setSetupCommand('')
      setTestCommand('')
      void queryClient.invalidateQueries({ queryKey: ['repositories'] })
      void queryClient.invalidateQueries({ queryKey: ['worktrees'] })
    },
  })

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    mutation.mutate({
      name,
      local_path: localPath,
      setup_command: parseCommand(setupCommand),
      test_command: parseCommand(testCommand),
    })
  }

  return (
    <form
      onSubmit={onSubmit}
      className="rounded border border-gray-200 p-4 dark:border-gray-800"
    >
      <h3 className="font-semibold">Register repository</h3>
      <div className="mt-3 grid gap-3">
        <div>
          <label htmlFor="repository-name" className="block text-sm">
            Name
          </label>
          <input
            id="repository-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            autoComplete="off"
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
          />
        </div>
        <div>
          <label htmlFor="repository-local-path" className="block text-sm">
            Local path
          </label>
          <input
            id="repository-local-path"
            value={localPath}
            onChange={(event) => setLocalPath(event.target.value)}
            required
            autoComplete="off"
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
          />
        </div>
        <div>
          <label htmlFor="repository-setup-command" className="block text-sm">
            Setup command
          </label>
          <input
            id="repository-setup-command"
            value={setupCommand}
            onChange={(event) => setSetupCommand(event.target.value)}
            autoComplete="off"
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
          />
        </div>
        <div>
          <label htmlFor="repository-test-command" className="block text-sm">
            Test command
          </label>
          <input
            id="repository-test-command"
            value={testCommand}
            onChange={(event) => setTestCommand(event.target.value)}
            autoComplete="off"
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
          />
        </div>
      </div>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Commands are split on whitespace, no shell:{' '}
        <code className="font-mono">uv run pytest -q</code>, not{' '}
        <code className="font-mono">cd x &amp;&amp; …</code>
      </p>
      <button
        type="submit"
        disabled={mutation.isPending}
        className="mt-4 rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
      >
        {mutation.isPending ? 'Registering…' : 'Register repository'}
      </button>
      {mutation.isError ? <InlineApiError error={mutation.error} /> : null}
    </form>
  )
}

function RepositoriesSection() {
  const queryClient = useQueryClient()
  const repositoriesQuery = useRepositories()
  const [confirmRepository, setConfirmRepository] =
    useState<RepositoryRead | null>(null)
  const unregisterMutation = useMutation({
    mutationFn: unregisterRepository,
    onSuccess: () => {
      setConfirmRepository(null)
      void queryClient.invalidateQueries({ queryKey: ['repositories'] })
      void queryClient.invalidateQueries({ queryKey: ['worktrees'] })
    },
    onError: () => {
      setConfirmRepository(null)
    },
  })

  return (
    <section aria-labelledby="repositories-heading">
      <h2 id="repositories-heading" className="mb-3 text-lg font-semibold">
        Repositories
      </h2>
      {repositoriesQuery.isPending ? (
        <LoadingState label="Loading repositories…" />
      ) : repositoriesQuery.isError ? (
        <ErrorState
          error={repositoriesQuery.error}
          onRetry={() => repositoriesQuery.refetch()}
        />
      ) : (
        <>
          {repositoriesQuery.data.length === 0 ? (
            <EmptyState title="No repositories yet">
              Register a local git repository to start creating worktrees.
            </EmptyState>
          ) : (
            <div className="grid gap-3">
              {repositoriesQuery.data.map((repository) => (
                <RepositoryCard
                  key={repository.id}
                  repository={repository}
                  onUnregister={() => setConfirmRepository(repository)}
                />
              ))}
            </div>
          )}
          {unregisterMutation.isError ? (
            <InlineApiError error={unregisterMutation.error} />
          ) : null}
          <div className="mt-4">
            <RegisterRepositoryForm />
          </div>
        </>
      )}
      <ConfirmDialog
        open={confirmRepository !== null}
        title="Unregister repository?"
        destructive
        confirmLabel="Unregister"
        busy={unregisterMutation.isPending}
        onConfirm={() => {
          if (confirmRepository) {
            unregisterMutation.mutate(confirmRepository.id)
          }
        }}
        onCancel={() => setConfirmRepository(null)}
      >
        <p>
          This only deletes the Gaia row for{' '}
          <code className="font-mono">{confirmRepository?.name}</code>. No files
          on disk are touched.
        </p>
      </ConfirmDialog>
    </section>
  )
}

function FlagBadge({ flag }: { flag: string }) {
  const classes =
    flag === 'tampered'
      ? 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200'
      : 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200'
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${classes}`}
    >
      {flag}
    </span>
  )
}

function encodePathPerSegment(path: string): string {
  const absolutePath = path.startsWith('/') ? path : `/${path}`
  return absolutePath
    .split('/')
    .map((segment) => encodeURIComponent(segment))
    .join('/')
}

function WorktreeRow({
  worktree,
  onRemove,
}: {
  worktree: WorktreeItemRead
  onRemove: () => void
}) {
  const flags: string[] = []
  if (worktree.dirty) {
    flags.push('dirty')
  }
  if (worktree.orphan) {
    flags.push('orphan')
  }
  if (!worktree.exists) {
    flags.push('missing')
  }
  if (worktree.tampered) {
    flags.push('tampered')
  }

  const canCopy = typeof navigator.clipboard?.writeText === 'function'
  const vscodeHref = `vscode://file${encodePathPerSegment(worktree.path)}`

  return (
    <tr>
      <td>
        <code className="font-mono">{worktree.branch}</code>
      </td>
      <td>
        <StatusBadge status={worktree.status} />
      </td>
      <td>
        <div className="flex flex-wrap gap-1">
          {flags.length > 0 ? (
            flags.map((flag) => <FlagBadge key={flag} flag={flag} />)
          ) : (
            <span className="text-gray-400 dark:text-gray-500">—</span>
          )}
        </div>
      </td>
      <td>
        <code className="font-mono">{shortSha(worktree.base_sha)}</code>
      </td>
      <td>{formatRelative(worktree.created_at)}</td>
      <td>
        <div className="flex items-center gap-2">
          <code className="font-mono">{worktree.path}</code>
          <button
            type="button"
            disabled={!canCopy}
            onClick={() => {
              void navigator.clipboard.writeText(worktree.path)
            }}
            className="rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-700"
          >
            Copy path
          </button>
          <a
            href={vscodeHref}
            className="text-blue-600 dark:text-blue-400"
            target="_blank"
            rel="noreferrer noopener"
          >
            Open in VS Code
          </a>
        </div>
      </td>
      <td>
        <button
          type="button"
          onClick={onRemove}
          className="rounded border border-red-300 px-2 py-1 text-sm text-red-700 dark:border-red-800 dark:text-red-300"
        >
          Remove
        </button>
      </td>
    </tr>
  )
}

function WorktreesTable({
  items,
  unknownPaths,
  onRemove,
}: {
  items: WorktreeItemRead[]
  unknownPaths: string[]
  onRemove: (worktree: WorktreeItemRead) => void
}) {
  if (items.length === 0 && unknownPaths.length === 0) {
    return (
      <EmptyState title="No worktrees yet">
        Start a run from a ticket to create one.
      </EmptyState>
    )
  }

  return (
    <>
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-left dark:border-gray-800">
            <th scope="col" className="py-2 pr-3">
              Branch
            </th>
            <th scope="col" className="py-2 pr-3">
              Status
            </th>
            <th scope="col" className="py-2 pr-3">
              Flags
            </th>
            <th scope="col" className="py-2 pr-3">
              Base SHA
            </th>
            <th scope="col" className="py-2 pr-3">
              Age
            </th>
            <th scope="col" className="py-2 pr-3">
              Path
            </th>
            <th scope="col" className="py-2">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((worktree) => (
            <WorktreeRow
              key={worktree.id}
              worktree={worktree}
              onRemove={() => onRemove(worktree)}
            />
          ))}
        </tbody>
      </table>
      {unknownPaths.length > 0 ? (
        <div className="mt-4 rounded border border-dashed border-gray-300 p-3 text-sm dark:border-gray-700">
          <p>
            Gaia did not create these; remove them with git if they are yours.
          </p>
          <ul className="mt-2 list-inside list-disc">
            {unknownPaths.map((path) => (
              <li key={path}>
                <code className="font-mono">{path}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  )
}

function WorktreesSection() {
  const queryClient = useQueryClient()
  const repositoriesQuery = useRepositories()
  const [selectedRepoId, setRepoId] = useState<string | null>(null)
  const repositories = repositoriesQuery.data ?? []
  // A selected repository that has since been unregistered falls back to
  // "All". Derived during render rather than reset in an effect (lead review).
  const repoId =
    selectedRepoId !== null &&
    repositoriesQuery.isSuccess &&
    !repositories.some((repository) => repository.id === selectedRepoId)
      ? null
      : selectedRepoId
  const worktreesQuery = useWorktrees(repoId)
  const [removeTarget, setRemoveTarget] = useState<WorktreeItemRead | null>(
    null,
  )
  const removeMutation = useMutation({
    mutationFn: ({ id, force }: { id: string; force: boolean }) =>
      removeWorktree(id, force),
    onSuccess: () => {
      setRemoveTarget(null)
      void queryClient.invalidateQueries({ queryKey: ['worktrees'] })
    },
    onError: () => {
      setRemoveTarget(null)
    },
  })

  const worktreeData = worktreesQuery.data ?? { items: [], unknown_paths: [] }

  return (
    <section aria-labelledby="worktrees-heading" className="mt-10">
      <h2 id="worktrees-heading" className="mb-3 text-lg font-semibold">
        Worktrees
      </h2>
      <div className="mb-4 flex items-end gap-2">
        <div>
          <label htmlFor="worktree-repository-filter" className="block text-sm">
            Repository
          </label>
          <select
            id="worktree-repository-filter"
            value={repoId ?? ''}
            onChange={(event) => setRepoId(event.target.value || null)}
            className="mt-1 rounded border border-gray-300 px-2 py-1.5 dark:border-gray-700 dark:bg-gray-950"
          >
            <option value="">All</option>
            {repositories.map((repository) => (
              <option key={repository.id} value={repository.id}>
                {repository.name}
              </option>
            ))}
          </select>
        </div>
      </div>
      {worktreesQuery.isPending ? (
        <LoadingState label="Loading worktrees…" />
      ) : worktreesQuery.isError ? (
        <ErrorState
          error={worktreesQuery.error}
          onRetry={() => worktreesQuery.refetch()}
        />
      ) : (
        <WorktreesTable
          items={worktreeData.items}
          unknownPaths={worktreeData.unknown_paths}
          onRemove={setRemoveTarget}
        />
      )}
      {removeMutation.isError ? (
        <InlineApiError error={removeMutation.error} />
      ) : null}
      <ConfirmDialog
        open={removeTarget !== null}
        title="Remove worktree?"
        destructive
        confirmLabel="Remove"
        busy={removeMutation.isPending}
        requireText={removeTarget?.dirty ? removeTarget.branch : undefined}
        onConfirm={() => {
          if (removeTarget) {
            removeMutation.mutate({
              id: removeTarget.id,
              force: removeTarget.dirty === true,
            })
          }
        }}
        onCancel={() => setRemoveTarget(null)}
      >
        <p>
          {removeTarget?.dirty ? (
            <>
              <code className="font-mono">{removeTarget.branch}</code> has
              uncommitted changes. Removing it will lose them forever.
            </>
          ) : (
            <>
              Remove <code className="font-mono">{removeTarget?.branch}</code>?
              The row is kept as history.
            </>
          )}
        </p>
      </ConfirmDialog>
    </section>
  )
}

export default function WorktreesView() {
  return (
    <>
      <PageHeader title="Worktrees & repositories" />
      <RepositoriesSection />
      <WorktreesSection />
    </>
  )
}
