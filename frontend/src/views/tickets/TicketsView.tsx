import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { api } from '../../api/client'
import { ApiError } from '../../api/errors'
import type { components } from '../../api/schema'
import { StatusBadge } from '../../components/StatusBadge'
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
} from '../../components/States'
import { formatRelative } from '../../components/format'
import { AgentModeEditor } from '../agent-modes/AgentModeEditor'
import {
  AGENT_MODE_QUERY_KEY,
  MODELS_QUERY_KEY,
  configProblem,
  describeConfig,
  fetchAgentMode,
  fetchModels,
  normalizeConfig,
  type AgentModeConfig,
} from '../agent-modes/agentModes'

type TicketStatus = components['schemas']['TicketStatus']
type TicketRead = components['schemas']['TicketRead']
type TicketPage = components['schemas']['TicketPage']
type TicketCreate = components['schemas']['TicketCreate']
type TicketPatch = components['schemas']['TicketPatch']
type RepositoryRead = components['schemas']['RepositoryRead']
type RunRead = components['schemas']['RunRead']

const TICKET_STATUSES: readonly TicketStatus[] = [
  'backlog',
  'ready',
  'in_progress',
  'in_review',
  'done',
  'cancelled',
]

const STATUS_LABELS: Record<TicketStatus, string> = {
  backlog: 'Backlog',
  ready: 'Ready',
  in_progress: 'In progress',
  in_review: 'In review',
  done: 'Done',
  cancelled: 'Cancelled',
}

function parseView(value: string | null): 'board' | 'table' {
  return value === 'table' ? 'table' : 'board'
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

async function fetchRepositories(): Promise<RepositoryRead[]> {
  const { data, error } = await api.GET('/api/v1/repositories', { fetch })
  if (error) {
    throw error
  }
  return data
}

async function fetchTicketsPage(input: {
  repoId: string | null
  q: string
  cursor: string | null
}): Promise<TicketPage> {
  const { data, error } = await api.GET('/api/v1/tickets', {
    params: {
      query: {
        limit: 100,
        repo_id: input.repoId ?? undefined,
        q: input.q.trim() === '' ? undefined : input.q,
        cursor: input.cursor ?? undefined,
      },
    },
    fetch,
  })
  if (error) {
    throw error
  }
  return data
}

async function createTicket(input: {
  title: string
  body: string
  repo_id: string
  priority: number
}): Promise<TicketRead> {
  const body: TicketCreate = {
    title: input.title,
    body: input.body,
    repo_id: input.repo_id,
    priority: input.priority,
  }
  const { data, error } = await api.POST('/api/v1/tickets', { body, fetch })
  if (error) {
    throw error
  }
  return data
}

async function updateTicket(input: {
  id: string
  title: string
  body: string
  priority: number
  status: TicketStatus
}): Promise<TicketRead> {
  const body: TicketPatch = {
    title: input.title,
    body: input.body,
    priority: input.priority,
    status: input.status,
  }
  const { data, error } = await api.PATCH('/api/v1/tickets/{ticket_id}', {
    params: { path: { ticket_id: input.id } },
    body,
    fetch,
  })
  if (error) {
    throw error
  }
  return data
}

async function startRun(input: {
  ticket_id: string
  agent_config?: AgentModeConfig
  budget_usd?: number
}): Promise<RunRead> {
  const body: components['schemas']['RunCreate'] = {
    ticket_id: input.ticket_id,
  }
  if (input.agent_config) {
    body.agent_config = input.agent_config
  }
  if (input.budget_usd !== undefined) {
    body.budget_usd = input.budget_usd
  }
  const { data, error } = await api.POST('/api/v1/runs', { body, fetch })
  if (error) {
    throw error
  }
  return data
}

function TicketModal({
  ticket,
  repositories,
  onClose,
}: {
  ticket: TicketRead | null
  repositories: RepositoryRead[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const isEdit = ticket !== null
  const [title, setTitle] = useState(ticket?.title ?? '')
  const [body, setBody] = useState(ticket?.body ?? '')
  const [repoId, setRepoId] = useState(
    ticket?.repo_id ?? repositories[0]?.id ?? '',
  )
  const [priority, setPriority] = useState(String(ticket?.priority ?? 0))
  const [status, setStatus] = useState<TicketStatus>(
    ticket?.status ?? 'backlog',
  )

  const mutation = useMutation({
    mutationFn: (values: {
      title: string
      body: string
      repoId: string
      priority: number
      status: TicketStatus
    }) =>
      isEdit
        ? updateTicket({
            id: ticket.id,
            title: values.title,
            body: values.body,
            priority: values.priority,
            status: values.status,
          })
        : createTicket({
            title: values.title,
            body: values.body,
            repo_id: values.repoId,
            priority: values.priority,
          }),
    onSuccess: () => {
      onClose()
      void queryClient.invalidateQueries({ queryKey: ['tickets'] })
    },
  })

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const parsedPriority = Number(priority)
    mutation.mutate({
      title: title.trim(),
      body,
      repoId,
      priority: Number.isFinite(parsedPriority)
        ? Math.trunc(parsedPriority)
        : 0,
      status,
    })
  }

  const canSubmit =
    !mutation.isPending &&
    title.trim() !== '' &&
    (isEdit || repoId !== '') &&
    Number.isFinite(Number(priority))

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="ticket-modal-title"
        className="w-full max-w-lg rounded-lg bg-white p-5 shadow-xl dark:bg-gray-900"
      >
        <h2 id="ticket-modal-title" className="text-lg font-semibold">
          {isEdit ? 'Edit ticket' : 'Create ticket'}
        </h2>
        <form onSubmit={onSubmit} className="mt-4 grid gap-3">
          <div>
            <label htmlFor="ticket-title" className="block text-sm">
              Title
            </label>
            <input
              id="ticket-title"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              required
              autoFocus
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-950"
            />
          </div>
          <div>
            <label htmlFor="ticket-body" className="block text-sm">
              Body
            </label>
            <textarea
              id="ticket-body"
              value={body}
              onChange={(event) => setBody(event.target.value)}
              rows={6}
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
            />
          </div>
          {!isEdit ? (
            <div>
              <label htmlFor="ticket-repository" className="block text-sm">
                Repository
              </label>
              <select
                id="ticket-repository"
                value={repoId}
                onChange={(event) => setRepoId(event.target.value)}
                required
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-950"
              >
                {repositories.length === 0 ? (
                  <option value="">No repositories</option>
                ) : (
                  repositories.map((repository) => (
                    <option key={repository.id} value={repository.id}>
                      {repository.name}
                    </option>
                  ))
                )}
              </select>
            </div>
          ) : null}
          <div>
            <label htmlFor="ticket-priority" className="block text-sm">
              Priority
            </label>
            <input
              id="ticket-priority"
              type="number"
              min="0"
              step="1"
              value={priority}
              onChange={(event) => setPriority(event.target.value)}
              required
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-950"
            />
          </div>
          {isEdit ? (
            <div>
              <label htmlFor="ticket-status" className="block text-sm">
                Status
              </label>
              <select
                id="ticket-status"
                value={status}
                onChange={(event) =>
                  setStatus(event.target.value as TicketStatus)
                }
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-950"
              >
                {TICKET_STATUSES.map((value) => (
                  <option key={value} value={value}>
                    {STATUS_LABELS[value]}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          {mutation.isError ? <InlineApiError error={mutation.error} /> : null}
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-gray-300 px-3 py-1.5 dark:border-gray-700"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canSubmit}
              className="rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
            >
              {mutation.isPending
                ? 'Saving…'
                : isEdit
                  ? 'Save changes'
                  : 'Create ticket'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function StartRunDialog({
  ticket,
  onClose,
}: {
  ticket: TicketRead
  onClose: () => void
}) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [budget, setBudget] = useState('')
  // ``null`` runs with the owner's default; a config overrides it for this run.
  const [custom, setCustom] = useState<AgentModeConfig | null>(null)
  const modelsQuery = useQuery({
    queryKey: MODELS_QUERY_KEY,
    queryFn: fetchModels,
  })
  const modeQuery = useQuery({
    queryKey: AGENT_MODE_QUERY_KEY,
    queryFn: fetchAgentMode,
  })
  const catalog = modelsQuery.data
  const defaultConfig = modeQuery.data?.config

  const mutation = useMutation({
    mutationFn: () => {
      const parsedBudget = budget.trim() === '' ? Number.NaN : Number(budget)
      return startRun({
        ticket_id: ticket.id,
        agent_config: custom ? normalizeConfig(custom) : undefined,
        budget_usd:
          Number.isFinite(parsedBudget) && parsedBudget > 0
            ? parsedBudget
            : undefined,
      })
    },
    onSuccess: (run) => {
      onClose()
      void queryClient.invalidateQueries({ queryKey: ['tickets'] })
      void queryClient.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/runs/${run.id}`)
    },
  })

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    mutation.mutate()
  }

  const budgetInvalid =
    budget.trim() !== '' &&
    (!Number.isFinite(Number(budget)) || Number(budget) <= 0)
  const customInvalid = custom !== null && configProblem(custom) !== null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="start-run-modal-title"
        className={`max-h-[90vh] w-full overflow-y-auto rounded-lg bg-white p-5 shadow-xl dark:bg-gray-900 ${
          custom ? 'max-w-3xl' : 'max-w-md'
        }`}
      >
        <h2 id="start-run-modal-title" className="text-lg font-semibold">
          Start a run
        </h2>
        <form onSubmit={onSubmit} className="mt-4 grid gap-3">
          <fieldset>
            <legend className="block text-sm">Agent mode</legend>
            <div className="mt-1 grid gap-1 text-sm">
              <label className="flex items-start gap-2">
                <input
                  type="radio"
                  name="run-agent-mode"
                  checked={custom === null}
                  onChange={() => setCustom(null)}
                />
                <span>
                  Use my default
                  {defaultConfig ? (
                    <span className="block text-gray-600 dark:text-gray-400">
                      {describeConfig(defaultConfig, catalog?.models)}
                    </span>
                  ) : null}
                </span>
              </label>
              <label className="flex items-start gap-2">
                <input
                  type="radio"
                  name="run-agent-mode"
                  checked={custom !== null}
                  disabled={!catalog || !defaultConfig}
                  onChange={() => setCustom(defaultConfig ?? null)}
                />
                <span>Choose for this run</span>
              </label>
            </div>
          </fieldset>
          {custom && catalog ? (
            <AgentModeEditor
              idPrefix="run-agent-mode"
              value={custom}
              catalog={catalog}
              onChange={setCustom}
            />
          ) : null}
          <div>
            <label htmlFor="run-budget" className="block text-sm">
              Budget (USD)
            </label>
            <input
              id="run-budget"
              type="number"
              min="0.01"
              step="0.01"
              value={budget}
              onChange={(event) => setBudget(event.target.value)}
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-950"
            />
            {budgetInvalid ? (
              <p className="mt-1 text-sm text-red-700 dark:text-red-300">
                Budget must be a number greater than 0.
              </p>
            ) : null}
          </div>
          {mutation.isError ? <StartRunError error={mutation.error} /> : null}
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-gray-300 px-3 py-1.5 dark:border-gray-700"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={mutation.isPending || budgetInvalid || customInvalid}
              className="rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
            >
              {mutation.isPending ? 'Starting…' : 'Start run'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function StartRunError({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.status === 503) {
    return (
      <p
        role="alert"
        className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
      >
        The orchestrator is not running.
      </p>
    )
  }

  return <InlineApiError error={error} />
}

function TicketCard({
  ticket,
  repoName,
  onEdit,
  onStart,
}: {
  ticket: TicketRead
  repoName: string
  onEdit: () => void
  onStart: () => void
}) {
  const canStart = ticket.status !== 'done' && ticket.status !== 'cancelled'
  return (
    <article className="rounded border border-gray-200 bg-white p-3 shadow-sm dark:border-gray-800 dark:bg-gray-900">
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-medium">{ticket.title}</h3>
        <button
          type="button"
          onClick={onEdit}
          className="shrink-0 rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-700"
        >
          Edit
        </button>
      </div>
      <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">
        {repoName}
      </p>
      <p className="text-sm text-gray-600 dark:text-gray-300">
        Priority: {ticket.priority}
      </p>
      {canStart ? (
        <button
          type="button"
          onClick={onStart}
          className="mt-3 rounded bg-blue-600 px-2 py-1 text-sm text-white"
        >
          Start run
        </button>
      ) : null}
    </article>
  )
}

function Board({
  tickets,
  repoName,
  onEdit,
  onStart,
}: {
  tickets: TicketRead[]
  repoName: (ticket: TicketRead) => string
  onEdit: (ticket: TicketRead) => void
  onStart: (ticket: TicketRead) => void
}) {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
      {TICKET_STATUSES.map((status) => (
        <section
          key={status}
          aria-labelledby={`ticket-status-${status}`}
          className="rounded border border-gray-200 bg-gray-50 p-3 dark:border-gray-800 dark:bg-gray-900/50"
        >
          <h2
            id={`ticket-status-${status}`}
            className="mb-3 text-sm font-semibold text-gray-700 dark:text-gray-200"
          >
            {STATUS_LABELS[status]}
          </h2>
          <div className="grid gap-2">
            {tickets
              .filter((ticket) => ticket.status === status)
              .map((ticket) => (
                <TicketCard
                  key={ticket.id}
                  ticket={ticket}
                  repoName={repoName(ticket)}
                  onEdit={() => onEdit(ticket)}
                  onStart={() => onStart(ticket)}
                />
              ))}
          </div>
        </section>
      ))}
    </div>
  )
}

function TicketRow({
  ticket,
  repoName,
  onEdit,
  onStart,
}: {
  ticket: TicketRead
  repoName: string
  onEdit: () => void
  onStart: () => void
}) {
  const canStart = ticket.status !== 'done' && ticket.status !== 'cancelled'
  return (
    <tr className="border-t border-gray-200 dark:border-gray-800">
      <td className="px-3 py-2">{ticket.title}</td>
      <td className="px-3 py-2">{repoName}</td>
      <td className="px-3 py-2">
        <StatusBadge status={ticket.status} />
      </td>
      <td className="px-3 py-2">{ticket.priority}</td>
      <td className="px-3 py-2" title={ticket.updated_at}>
        {formatRelative(ticket.updated_at)}
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onEdit}
            className="rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-700"
          >
            Edit
          </button>
          {canStart ? (
            <button
              type="button"
              onClick={onStart}
              className="rounded bg-blue-600 px-2 py-1 text-xs text-white"
            >
              Start run
            </button>
          ) : null}
        </div>
      </td>
    </tr>
  )
}

function TicketsTable({
  tickets,
  repoName,
  onEdit,
  onStart,
}: {
  tickets: TicketRead[]
  repoName: (ticket: TicketRead) => string
  onEdit: (ticket: TicketRead) => void
  onStart: (ticket: TicketRead) => void
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-gray-500 dark:text-gray-400">
          <tr>
            <th scope="col" className="px-3 py-2">
              Title
            </th>
            <th scope="col" className="px-3 py-2">
              Repo
            </th>
            <th scope="col" className="px-3 py-2">
              Status
            </th>
            <th scope="col" className="px-3 py-2">
              Priority
            </th>
            <th scope="col" className="px-3 py-2">
              Updated
            </th>
            <th scope="col" className="px-3 py-2">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {tickets.map((ticket) => (
            <TicketRow
              key={ticket.id}
              ticket={ticket}
              repoName={repoName(ticket)}
              onEdit={() => onEdit(ticket)}
              onStart={() => onStart(ticket)}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function TicketsView() {
  const [searchParams, setSearchParams] = useSearchParams()
  const view = parseView(searchParams.get('view'))
  const repoId = searchParams.get('repo') || null
  const q = searchParams.get('q') ?? ''

  const repositoriesQuery = useQuery({
    queryKey: ['repositories'],
    queryFn: fetchRepositories,
  })
  const repositories = repositoriesQuery.data ?? []

  const ticketsQuery = useInfiniteQuery({
    queryKey: ['tickets', { repoId, q }],
    queryFn: ({ pageParam }) =>
      fetchTicketsPage({
        repoId,
        q,
        cursor: pageParam as string | null,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  })

  const tickets = ticketsQuery.data?.pages.flatMap((page) => page.items) ?? []
  const repoName = (ticket: TicketRead): string =>
    repositories.find((repository) => repository.id === ticket.repo_id)?.name ??
    ticket.repo_id

  const [modal, setModal] = useState<{
    open: boolean
    ticket: TicketRead | null
  }>({ open: false, ticket: null })
  const [startTicket, setStartTicket] = useState<TicketRead | null>(null)

  function updateParam(key: 'view' | 'repo' | 'q', value: string | null) {
    const params = new URLSearchParams(searchParams)
    if (value === null || value === '') {
      params.delete(key)
    } else {
      params.set(key, value)
    }
    // Typing in the search box replaces the history entry, so Back leaves the
    // page instead of stepping through every keystroke (lead review).
    setSearchParams(params, { replace: key === 'q' })
  }

  function selectView(next: 'board' | 'table') {
    updateParam('view', next === 'board' ? null : 'table')
  }

  const filterActive = repoId !== null || q.trim() !== ''

  return (
    <section>
      <PageHeader
        title="Tickets"
        actions={
          <>
            <div
              role="group"
              aria-label="Ticket view"
              className="flex rounded border border-gray-300 p-0.5 dark:border-gray-700"
            >
              <button
                type="button"
                aria-pressed={view === 'board'}
                onClick={() => selectView('board')}
                className={`rounded px-3 py-1.5 text-sm ${
                  view === 'board'
                    ? 'bg-blue-600 text-white'
                    : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
                }`}
              >
                Board
              </button>
              <button
                type="button"
                aria-pressed={view === 'table'}
                onClick={() => selectView('table')}
                className={`rounded px-3 py-1.5 text-sm ${
                  view === 'table'
                    ? 'bg-blue-600 text-white'
                    : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
                }`}
              >
                Table
              </button>
            </div>
            <button
              type="button"
              onClick={() => setModal({ open: true, ticket: null })}
              className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white"
            >
              New ticket
            </button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor="ticket-repository-filter" className="block text-sm">
            Repository
          </label>
          <select
            id="ticket-repository-filter"
            value={repoId ?? ''}
            onChange={(event) =>
              updateParam('repo', event.target.value || null)
            }
            className="mt-1 rounded border border-gray-300 px-2 py-1.5 dark:border-gray-700 dark:bg-gray-950"
          >
            <option value="">All repositories</option>
            {repositories.map((repository) => (
              <option key={repository.id} value={repository.id}>
                {repository.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="ticket-search" className="block text-sm">
            Search tickets
          </label>
          <input
            id="ticket-search"
            data-search-input
            value={q}
            onChange={(event) => updateParam('q', event.target.value)}
            placeholder="Filter by title"
            className="mt-1 w-72 rounded border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-700 dark:bg-gray-950"
          />
        </div>
      </div>

      {repositoriesQuery.isError ? (
        <div className="mb-4">
          <ErrorState
            error={repositoriesQuery.error}
            onRetry={() => repositoriesQuery.refetch()}
          />
        </div>
      ) : null}

      {ticketsQuery.isPending ? <LoadingState /> : null}

      {ticketsQuery.isError ? (
        <ErrorState
          error={ticketsQuery.error}
          onRetry={() => ticketsQuery.refetch()}
        />
      ) : null}

      {ticketsQuery.isSuccess && tickets.length === 0 ? (
        <EmptyState
          title={
            filterActive
              ? 'No tickets match the current filters.'
              : 'No tickets yet.'
          }
        >
          {filterActive
            ? 'Clear the repository or search filters, or create a new ticket.'
            : 'Create a ticket to start planning work.'}
        </EmptyState>
      ) : null}

      {tickets.length > 0 ? (
        <>
          {view === 'board' ? (
            <Board
              tickets={tickets}
              repoName={repoName}
              onEdit={(ticket) => setModal({ open: true, ticket })}
              onStart={setStartTicket}
            />
          ) : (
            <TicketsTable
              tickets={tickets}
              repoName={repoName}
              onEdit={(ticket) => setModal({ open: true, ticket })}
              onStart={setStartTicket}
            />
          )}
          {ticketsQuery.hasNextPage ? (
            <div className="mt-4">
              <button
                type="button"
                onClick={() => void ticketsQuery.fetchNextPage()}
                disabled={ticketsQuery.isFetchingNextPage}
                className="rounded border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-700"
              >
                {ticketsQuery.isFetchingNextPage
                  ? 'Loading more…'
                  : 'Load more'}
              </button>
            </div>
          ) : null}
        </>
      ) : null}

      {modal.open ? (
        <TicketModal
          ticket={modal.ticket}
          repositories={repositories}
          onClose={() => setModal({ open: false, ticket: null })}
        />
      ) : null}
      {startTicket ? (
        <StartRunDialog
          ticket={startTicket}
          onClose={() => setStartTicket(null)}
        />
      ) : null}
    </section>
  )
}
