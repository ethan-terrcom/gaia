import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { api } from '../../api/client'
import { ApiError } from '../../api/errors'
import type { components } from '../../api/schema'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { StatusBadge } from '../../components/StatusBadge'
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
} from '../../components/States'
import { formatRelative } from '../../components/format'
import { Button } from '../../components/ui/Button'
import { Field, FieldGrid } from '../../components/ui/Field'
import { Input, Select, Textarea } from '../../components/ui/Input'
import { SegmentedControl } from '../../components/ui/SegmentedControl'
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
      className="rounded-[var(--radius-md)] border border-[var(--color-danger)]/30 bg-[var(--color-danger-soft)] p-3 text-[13px] text-[var(--color-danger)]"
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

async function deleteTicket(ticketId: string): Promise<void> {
  const { error } = await api.DELETE('/api/v1/tickets/{ticket_id}', {
    params: { path: { ticket_id: ticketId } },
    fetch,
  })
  if (error) {
    throw error
  }
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
  onDelete,
}: {
  ticket: TicketRead | null
  repositories: RepositoryRead[]
  onClose: () => void
  onDelete?: (ticket: TicketRead) => void
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-[3px]">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="ticket-modal-title"
        className="w-full max-w-2xl rounded-[var(--radius-xl)] border border-[var(--color-border)] bg-[var(--color-surface-overlay)] p-5 shadow-[var(--shadow-panel)]"
      >
        <h2
          id="ticket-modal-title"
          className="text-[16px] font-semibold tracking-[-0.02em]"
        >
          {isEdit ? 'Edit ticket' : 'Create ticket'}
        </h2>
        <form onSubmit={onSubmit} className="mt-5 grid gap-4">
          <Field label="Title" htmlFor="ticket-title">
            <Input
              id="ticket-title"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              required
              autoFocus
            />
          </Field>
          <Field label="Body" htmlFor="ticket-body">
            <Textarea
              id="ticket-body"
              value={body}
              onChange={(event) => setBody(event.target.value)}
              rows={8}
              className="font-mono"
            />
          </Field>
          <FieldGrid>
            {!isEdit ? (
              <Field label="Repository" htmlFor="ticket-repository">
                <Select
                  id="ticket-repository"
                  value={repoId}
                  onChange={(event) => setRepoId(event.target.value)}
                  required
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
                </Select>
              </Field>
            ) : null}
            <Field label="Priority" htmlFor="ticket-priority">
              <Input
                id="ticket-priority"
                type="number"
                min="0"
                step="1"
                value={priority}
                onChange={(event) => setPriority(event.target.value)}
                required
              />
            </Field>
            {isEdit ? (
              <Field label="Status" htmlFor="ticket-status">
                <Select
                  id="ticket-status"
                  value={status}
                  onChange={(event) =>
                    setStatus(event.target.value as TicketStatus)
                  }
                >
                  {TICKET_STATUSES.map((value) => (
                    <option key={value} value={value}>
                      {STATUS_LABELS[value]}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : null}
          </FieldGrid>
          {mutation.isError ? <InlineApiError error={mutation.error} /> : null}
          <div className="mt-1 flex flex-wrap items-center justify-between gap-2">
            {isEdit && ticket && onDelete ? (
              <Button
                type="button"
                variant="danger"
                size="sm"
                onClick={() => onDelete(ticket)}
              >
                <Trash2 className="size-3.5" aria-hidden />
                Delete
              </Button>
            ) : (
              <span />
            )}
            <div className="flex gap-2">
              <Button type="button" variant="secondary" onClick={onClose}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={!canSubmit}>
                {mutation.isPending
                  ? 'Saving…'
                  : isEdit
                    ? 'Save changes'
                    : 'Create ticket'}
              </Button>
            </div>
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-[3px]">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="start-run-modal-title"
        className={`max-h-[90vh] w-full overflow-y-auto rounded-[var(--radius-xl)] border border-[var(--color-border)] bg-[var(--color-surface-overlay)] p-5 shadow-[var(--shadow-panel)] ${
          custom ? 'max-w-3xl' : 'max-w-lg'
        }`}
      >
        <h2
          id="start-run-modal-title"
          className="text-[16px] font-semibold tracking-[-0.02em]"
        >
          Start a run
        </h2>
        <form onSubmit={onSubmit} className="mt-5 grid gap-4">
          <fieldset>
            <legend className="text-[13px] font-medium text-[var(--color-ink-muted)]">
              Agent mode
            </legend>
            <div className="mt-2 grid gap-2 text-[13px]">
              <label className="flex items-start gap-2.5 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-3 py-2.5">
                <input
                  type="radio"
                  name="run-agent-mode"
                  checked={custom === null}
                  onChange={() => setCustom(null)}
                  className="mt-0.5"
                />
                <span>
                  Use my default
                  {defaultConfig ? (
                    <span className="mt-0.5 block text-[12px] text-[var(--color-ink-subtle)]">
                      {describeConfig(defaultConfig, catalog?.models)}
                    </span>
                  ) : null}
                </span>
              </label>
              <label className="flex items-start gap-2.5 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-surface-raised)] px-3 py-2.5">
                <input
                  type="radio"
                  name="run-agent-mode"
                  checked={custom !== null}
                  disabled={!catalog || !defaultConfig}
                  onChange={() => setCustom(defaultConfig ?? null)}
                  className="mt-0.5"
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
          <Field
            label="Budget (USD)"
            htmlFor="run-budget"
            hint="Leave blank to use the daily default."
            error={
              budgetInvalid
                ? 'Budget must be a number greater than 0.'
                : undefined
            }
          >
            <Input
              id="run-budget"
              type="number"
              min="0.01"
              step="0.01"
              value={budget}
              onChange={(event) => setBudget(event.target.value)}
              placeholder="Optional"
            />
          </Field>
          {mutation.isError ? <StartRunError error={mutation.error} /> : null}
          <div className="mt-1 flex justify-end gap-2">
            <Button type="button" variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={mutation.isPending || budgetInvalid || customInvalid}
            >
              {mutation.isPending ? 'Starting…' : 'Start run'}
            </Button>
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
  onDelete,
}: {
  ticket: TicketRead
  repoName: string
  onEdit: () => void
  onStart: () => void
  onDelete: () => void
}) {
  const canStart = ticket.status !== 'done' && ticket.status !== 'cancelled'
  return (
    <article className="rounded-[var(--radius-xl)] border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-3.5 shadow-[var(--shadow-soft)]">
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-medium text-[var(--color-ink)]">
          {ticket.title}
        </h3>
        <div className="flex shrink-0 gap-1">
          <Button type="button" variant="ghost" size="sm" onClick={onEdit}>
            Edit
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-label={`Delete ${ticket.title}`}
            onClick={onDelete}
          >
            <Trash2 className="size-3.5" />
          </Button>
        </div>
      </div>
      <p className="mt-2 text-sm text-[var(--color-ink-muted)]">{repoName}</p>
      <p className="text-sm text-[var(--color-ink-muted)]">
        Priority: {ticket.priority}
      </p>
      {canStart ? (
        <Button
          type="button"
          variant="primary"
          size="sm"
          className="mt-3"
          onClick={onStart}
        >
          Start run
        </Button>
      ) : null}
    </article>
  )
}

function Board({
  tickets,
  repoName,
  onEdit,
  onStart,
  onDelete,
}: {
  tickets: TicketRead[]
  repoName: (ticket: TicketRead) => string
  onEdit: (ticket: TicketRead) => void
  onStart: (ticket: TicketRead) => void
  onDelete: (ticket: TicketRead) => void
}) {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
      {TICKET_STATUSES.map((status) => (
        <section
          key={status}
          aria-labelledby={`ticket-status-${status}`}
          className="rounded-[var(--radius-xl)] bg-[var(--color-surface-muted)]/80 p-3"
        >
          <h2
            id={`ticket-status-${status}`}
            className="mb-3 px-1 text-xs font-semibold tracking-wide text-[var(--color-ink-muted)] uppercase"
          >
            {STATUS_LABELS[status]}
          </h2>
          <div className="grid gap-2.5">
            {tickets
              .filter((ticket) => ticket.status === status)
              .map((ticket) => (
                <TicketCard
                  key={ticket.id}
                  ticket={ticket}
                  repoName={repoName(ticket)}
                  onEdit={() => onEdit(ticket)}
                  onStart={() => onStart(ticket)}
                  onDelete={() => onDelete(ticket)}
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
  onDelete,
}: {
  ticket: TicketRead
  repoName: string
  onEdit: () => void
  onStart: () => void
  onDelete: () => void
}) {
  const canStart = ticket.status !== 'done' && ticket.status !== 'cancelled'
  return (
    <tr className="border-t border-[var(--color-border)] hover:bg-[var(--color-surface-muted)]/60">
      <td className="px-3 py-3 font-medium">{ticket.title}</td>
      <td className="px-3 py-3 text-[var(--color-ink-muted)]">{repoName}</td>
      <td className="px-3 py-3">
        <StatusBadge status={ticket.status} />
      </td>
      <td className="px-3 py-3">{ticket.priority}</td>
      <td className="px-3 py-3" title={ticket.updated_at}>
        {formatRelative(ticket.updated_at)}
      </td>
      <td className="px-3 py-3">
        <div className="flex items-center gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={onEdit}>
            Edit
          </Button>
          {canStart ? (
            <Button type="button" variant="primary" size="sm" onClick={onStart}>
              Start run
            </Button>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-label={`Delete ${ticket.title}`}
            onClick={onDelete}
          >
            <Trash2 className="size-3.5" />
          </Button>
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
  onDelete,
}: {
  tickets: TicketRead[]
  repoName: (ticket: TicketRead) => string
  onEdit: (ticket: TicketRead) => void
  onStart: (ticket: TicketRead) => void
  onDelete: (ticket: TicketRead) => void
}) {
  return (
    <div className="overflow-x-auto rounded-[var(--radius-xl)] border border-[var(--color-border)] bg-[var(--color-surface-raised)] shadow-[var(--shadow-panel)]">
      <table className="w-full text-left text-sm">
        <thead className="text-xs tracking-wide text-[var(--color-ink-muted)] uppercase">
          <tr>
            <th scope="col" className="px-3 py-3">
              Title
            </th>
            <th scope="col" className="px-3 py-3">
              Repo
            </th>
            <th scope="col" className="px-3 py-3">
              Status
            </th>
            <th scope="col" className="px-3 py-3">
              Priority
            </th>
            <th scope="col" className="px-3 py-3">
              Updated
            </th>
            <th scope="col" className="px-3 py-3">
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
              onDelete={() => onDelete(ticket)}
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

  const queryClient = useQueryClient()
  const [modal, setModal] = useState<{
    open: boolean
    ticket: TicketRead | null
  }>({ open: false, ticket: null })
  const [startTicket, setStartTicket] = useState<TicketRead | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<TicketRead | null>(null)
  const [deleteError, setDeleteError] = useState<unknown>(null)

  const deleteMutation = useMutation({
    mutationFn: (ticketId: string) => deleteTicket(ticketId),
    onSuccess: () => {
      setDeleteTarget(null)
      setDeleteError(null)
      setModal({ open: false, ticket: null })
      void queryClient.invalidateQueries({ queryKey: ['tickets'] })
    },
    onError: (error) => {
      setDeleteError(error)
    },
  })

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

  function requestDelete(ticket: TicketRead) {
    setDeleteError(null)
    setDeleteTarget(ticket)
  }

  const filterActive = repoId !== null || q.trim() !== ''

  return (
    <section>
      <PageHeader
        title="Tickets"
        description="Plan and track work across repositories."
        actions={
          <>
            <SegmentedControl
              label="Ticket view"
              value={view}
              options={[
                { value: 'board', label: 'Board' },
                { value: 'table', label: 'Table' },
              ]}
              onChange={selectView}
            />
            <Button
              type="button"
              variant="primary"
              onClick={() => setModal({ open: true, ticket: null })}
            >
              <Plus className="size-4" aria-hidden />
              New ticket
            </Button>
          </>
        }
      />

      <div className="mb-6 grid grid-cols-1 items-start gap-3 sm:grid-cols-[minmax(14rem,1fr)_minmax(18rem,2fr)]">
        <Field label="Repository" htmlFor="ticket-repository-filter">
          <Select
            id="ticket-repository-filter"
            value={repoId ?? ''}
            onChange={(event) =>
              updateParam('repo', event.target.value || null)
            }
          >
            <option value="">All repositories</option>
            {repositories.map((repository) => (
              <option key={repository.id} value={repository.id}>
                {repository.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Search tickets" htmlFor="ticket-search">
          <Input
            id="ticket-search"
            data-search-input
            value={q}
            onChange={(event) => updateParam('q', event.target.value)}
            placeholder="Filter by title"
          />
        </Field>
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
              onDelete={requestDelete}
            />
          ) : (
            <TicketsTable
              tickets={tickets}
              repoName={repoName}
              onEdit={(ticket) => setModal({ open: true, ticket })}
              onStart={setStartTicket}
              onDelete={requestDelete}
            />
          )}
          {ticketsQuery.hasNextPage ? (
            <div className="mt-4">
              <Button
                type="button"
                variant="secondary"
                onClick={() => void ticketsQuery.fetchNextPage()}
                disabled={ticketsQuery.isFetchingNextPage}
              >
                {ticketsQuery.isFetchingNextPage
                  ? 'Loading more…'
                  : 'Load more'}
              </Button>
            </div>
          ) : null}
        </>
      ) : null}

      {modal.open ? (
        <TicketModal
          ticket={modal.ticket}
          repositories={repositories}
          onClose={() => setModal({ open: false, ticket: null })}
          onDelete={requestDelete}
        />
      ) : null}
      {startTicket ? (
        <StartRunDialog
          ticket={startTicket}
          onClose={() => setStartTicket(null)}
        />
      ) : null}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete ticket?"
        confirmLabel="Delete ticket"
        destructive
        busy={deleteMutation.isPending}
        onCancel={() => {
          setDeleteTarget(null)
          setDeleteError(null)
        }}
        onConfirm={() => {
          if (deleteTarget) {
            deleteMutation.mutate(deleteTarget.id)
          }
        }}
      >
        <p>
          This permanently removes{' '}
          <span className="font-medium text-[var(--color-ink)]">
            {deleteTarget?.title}
          </span>{' '}
          and every run, approval, and usage record tied to it.
        </p>
        {deleteError instanceof ApiError && deleteError.status === 409 ? (
          <p className="mt-3 text-[var(--color-danger)]" role="alert">
            This ticket still has an active run. Cancel or finish the run before
            deleting.
          </p>
        ) : deleteError ? (
          <InlineApiError error={deleteError} />
        ) : null}
      </ConfirmDialog>
    </section>
  )
}
