import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { api } from '../../api/client'
import { ApiError } from '../../api/errors'
import type { components } from '../../api/schema'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { EmptyState, ErrorState, LoadingState } from '../../components/States'
import { StatusBadge } from '../../components/StatusBadge'
import { formatRelative, formatUsd } from '../../components/format'
import {
  type GaiaEvent,
  type UseEventStreamOptions,
  useEventStream,
} from '../../hooks/useEventStream'
import { summarizeEvent, type TimelineEvent } from './events'

type RunDetail = components['schemas']['RunDetail']
type RunRead = components['schemas']['RunRead']
type RunStepRead = components['schemas']['RunStepRead']
type ApprovalRead = components['schemas']['ApprovalRead']
type EventRecord = components['schemas']['EventRecord']
type RunPullRequestRead = components['schemas']['RunPullRequestRead']
type RunStatus = components['schemas']['RunStatus']

const TERMINAL_RUN_STATUSES: ReadonlySet<RunStatus> = new Set([
  'failed',
  'completed',
  'cancelled',
])

const ROW_HEIGHT = 36
const OVERSCAN_ROWS = 5
const DEFAULT_VIEWPORT_HEIGHT = 600

function isGitHubUrl(url: string): boolean {
  return url.startsWith('https://github.com/')
}

function encodePathPerSegment(path: string): string {
  const absolutePath = path.startsWith('/') ? path : `/${path}`
  return absolutePath
    .split('/')
    .map((segment) => encodeURIComponent(segment))
    .join('/')
}

async function fetchRunDetail(runId: string): Promise<RunDetail> {
  const { data, error } = await api.GET('/api/v1/runs/{run_id}', {
    fetch: globalThis.fetch,
    params: { path: { run_id: runId } },
  })
  if (error) {
    throw error
  }
  return data
}

async function fetchRunEvents(runId: string): Promise<EventRecord[]> {
  const items: EventRecord[] = []
  let after = 0
  while (true) {
    const { data, error } = await api.GET('/api/v1/runs/{run_id}/events', {
      fetch: globalThis.fetch,
      params: {
        path: { run_id: runId },
        query: { after, limit: 500 },
      },
    })
    if (error) {
      throw error
    }
    items.push(...data.items)
    if (data.items.length === 0 || data.next_after === null) {
      break
    }
    if (data.next_after <= after) {
      break
    }
    after = data.next_after
  }
  return items
}

async function fetchRunDiff(runId: string): Promise<string> {
  const { data, error } = await api.GET('/api/v1/runs/{run_id}/diff', {
    fetch: globalThis.fetch,
    parseAs: 'text',
    params: { path: { run_id: runId } },
  })
  if (error) {
    throw error
  }
  return data
}

async function cancelRun(runId: string): Promise<void> {
  const { error } = await api.POST('/api/v1/runs/{run_id}/cancel', {
    fetch: globalThis.fetch,
    params: { path: { run_id: runId } },
  })
  if (error) {
    throw error
  }
}

function toTimelineEvent(event: EventRecord | GaiaEvent): TimelineEvent | null {
  if (event.seq === null) {
    return null
  }
  return {
    seq: event.seq,
    ts: event.ts,
    type: event.type,
    payload: event.payload,
  }
}

function mergeTimelineEvents(
  history: EventRecord[] | undefined,
  live: GaiaEvent[],
): TimelineEvent[] {
  const bySeq = new Map<number, TimelineEvent>()
  for (const event of history ?? []) {
    const item = toTimelineEvent(event)
    if (item) {
      bySeq.set(item.seq, item)
    }
  }
  for (const event of live) {
    const item = toTimelineEvent(event)
    if (item) {
      bySeq.set(item.seq, item)
    }
  }
  return [...bySeq.values()].sort((left, right) => left.seq - right.seq)
}

function CopyPathButton({ path }: { path: string }) {
  const canCopy = typeof navigator.clipboard?.writeText === 'function'
  return (
    <button
      type="button"
      disabled={!canCopy}
      onClick={() => {
        void navigator.clipboard.writeText(path)
      }}
      className="rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-700"
    >
      Copy path
    </button>
  )
}

function WorktreePath({ path }: { path: string }) {
  const vscodeHref = `vscode://file${encodePathPerSegment(path)}`
  return (
    <div className="flex flex-wrap items-center gap-2">
      <code className="font-mono">{path}</code>
      <CopyPathButton path={path} />
      <a
        href={vscodeHref}
        target="_blank"
        rel="noreferrer noopener"
        className="text-blue-600 dark:text-blue-400"
      >
        Open in VS Code
      </a>
    </div>
  )
}

function PullRequestLink({ pr }: { pr: RunPullRequestRead | null }) {
  if (!pr) {
    return <span>No pull request yet</span>
  }
  return (
    <span className="flex items-center gap-2">
      {isGitHubUrl(pr.url) ? (
        <a
          href={pr.url}
          target="_blank"
          rel="noreferrer noopener"
          className="text-blue-600 hover:underline dark:text-blue-400"
        >
          #{pr.number}
        </a>
      ) : (
        <span>#{pr.number}</span>
      )}
      <StatusBadge status={pr.ci_status} />
    </span>
  )
}

function CostBar({ run }: { run: RunRead }) {
  const cost = Number(run.cost_usd)
  const budget = Number(run.budget_usd)
  const ratio = budget > 0 ? cost / budget : 0
  const barColor =
    ratio >= 1 ? 'bg-red-500' : ratio >= 0.8 ? 'bg-amber-500' : 'bg-emerald-500'
  return (
    <div
      className="w-56"
      aria-label={`Cost ${formatUsd(run.cost_usd)} of budget ${formatUsd(run.budget_usd)}`}
    >
      <div className="text-sm">
        {formatUsd(run.cost_usd)} / {formatUsd(run.budget_usd)}
      </div>
      <div className="h-1.5 w-full rounded bg-gray-200 dark:bg-gray-700">
        <div
          className={`h-full rounded ${barColor}`}
          style={{ width: `${Math.min(100, ratio * 100)}%` }}
        />
      </div>
    </div>
  )
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

function StepList({ steps }: { steps: RunStepRead[] }) {
  if (steps.length === 0) {
    return (
      <EmptyState title="No steps yet.">
        A plan has not been approved or recorded for this run.
      </EmptyState>
    )
  }
  return (
    <ol className="space-y-2">
      {steps.map((step) => (
        <li
          key={step.id}
          className="rounded border border-gray-200 p-3 dark:border-gray-800"
        >
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500 dark:text-gray-400">
              {step.step_index + 1}.
            </span>
            <span className="font-medium">{step.title}</span>
            <StatusBadge status={step.status} />
            <span className="text-sm text-gray-500 dark:text-gray-400">
              {step.attempts} {step.attempts === 1 ? 'attempt' : 'attempts'}
            </span>
          </div>
          {step.result_summary ? (
            <p className="mt-2 whitespace-pre-wrap text-sm">
              {step.result_summary}
            </p>
          ) : null}
        </li>
      ))}
    </ol>
  )
}

function ApprovalList({ approvals }: { approvals: ApprovalRead[] }) {
  if (approvals.length === 0) {
    return (
      <EmptyState title="No approvals for this run yet.">
        Approvals requested during the run will appear here.
      </EmptyState>
    )
  }
  const sorted = [...approvals].sort((left, right) => {
    const leftCreated = Date.parse(left.created_at)
    const rightCreated = Date.parse(right.created_at)
    if (leftCreated !== rightCreated) {
      return leftCreated - rightCreated
    }
    return left.id.localeCompare(right.id)
  })
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-gray-500 dark:text-gray-400">
          <tr>
            <th scope="col" className="px-3 py-2">
              Kind
            </th>
            <th scope="col" className="px-3 py-2">
              Status
            </th>
            <th scope="col" className="px-3 py-2">
              Created
            </th>
            <th scope="col" className="px-3 py-2">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((approval) => (
            <tr
              key={approval.id}
              className="border-t border-gray-200 dark:border-gray-800"
            >
              <td className="px-3 py-2">
                {approval.kind.replaceAll('_', ' ')}
              </td>
              <td className="px-3 py-2">
                <StatusBadge status={approval.status} />
              </td>
              <td className="px-3 py-2" title={approval.created_at}>
                {formatRelative(approval.created_at)}
              </td>
              <td className="px-3 py-2">
                {approval.status === 'pending' ? (
                  <Link
                    to="/approvals"
                    className="text-blue-600 hover:underline dark:text-blue-400"
                  >
                    Review
                  </Link>
                ) : (
                  '—'
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TimelineRow({
  event,
  style,
}: {
  event: TimelineEvent
  style: CSSProperties
}) {
  const collapsible =
    event.type === 'agent.tool_call' || event.type === 'agent.tool_result'
  const summary = summarizeEvent(event)

  if (!collapsible) {
    return (
      <div
        data-testid={`event-row-${event.seq}`}
        style={style}
        className="flex items-center gap-2 border-b border-gray-200 px-3 text-sm dark:border-gray-800"
      >
        <time
          dateTime={event.ts}
          title={event.ts}
          className="shrink-0 text-gray-500 dark:text-gray-400"
        >
          {formatRelative(event.ts)}
        </time>
        <span className="shrink-0 font-mono text-xs">{event.type}</span>
        <span className="truncate">{summary}</span>
      </div>
    )
  }

  return (
    <div
      data-testid={`event-row-${event.seq}`}
      style={style}
      className="border-b border-gray-200 px-3 text-sm dark:border-gray-800"
    >
      <details className="h-full">
        <summary className="flex h-full cursor-pointer list-none items-center gap-2">
          <time
            dateTime={event.ts}
            title={event.ts}
            className="shrink-0 text-gray-500 dark:text-gray-400"
          >
            {formatRelative(event.ts)}
          </time>
          <span className="shrink-0 font-mono text-xs">{event.type}</span>
          <span className="truncate">{summary}</span>
        </summary>
        <pre
          style={{ top: ROW_HEIGHT }}
          className="absolute left-0 right-0 z-10 max-h-60 overflow-auto rounded border border-gray-300 bg-white p-2 text-xs dark:border-gray-700 dark:bg-gray-950"
        >
          {JSON.stringify(event.payload, null, 2)}
        </pre>
      </details>
    </div>
  )
}

function WindowedTimeline({ events }: { events: TimelineEvent[] }) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [viewportHeight, setViewportHeight] = useState(DEFAULT_VIEWPORT_HEIGHT)

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) {
      return
    }
    const updateHeight = () => {
      setViewportHeight(viewport.clientHeight || DEFAULT_VIEWPORT_HEIGHT)
    }
    updateHeight()
    window.addEventListener('resize', updateHeight)
    return () => window.removeEventListener('resize', updateHeight)
  }, [])

  const startIndex = Math.max(
    0,
    Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN_ROWS,
  )
  const endIndex = Math.min(
    events.length,
    Math.ceil((scrollTop + viewportHeight) / ROW_HEIGHT) + OVERSCAN_ROWS,
  )
  const visible = events.slice(startIndex, endIndex)

  return (
    <div
      ref={viewportRef}
      data-testid="timeline-scroll"
      onScroll={() => {
        setScrollTop(viewportRef.current?.scrollTop ?? 0)
      }}
      className="h-96 overflow-auto rounded border border-gray-200 dark:border-gray-800"
    >
      <div style={{ height: events.length * ROW_HEIGHT, position: 'relative' }}>
        {visible.map((event, index) => {
          const rowIndex = startIndex + index
          return (
            <TimelineRow
              key={event.seq}
              event={event}
              style={{
                position: 'absolute',
                top: rowIndex * ROW_HEIGHT,
                height: ROW_HEIGHT,
                left: 0,
                right: 0,
              }}
            />
          )
        })}
      </div>
    </div>
  )
}

function TimelineTab({
  runId,
  eventStreamOptions,
}: {
  runId: string
  eventStreamOptions: UseEventStreamOptions
}) {
  const { events: liveEvents } = useEventStream({ runId }, eventStreamOptions)
  const historyQuery = useQuery({
    queryKey: ['run-events', runId],
    queryFn: () => fetchRunEvents(runId),
  })
  const events = useMemo(
    () => mergeTimelineEvents(historyQuery.data, liveEvents),
    [historyQuery.data, liveEvents],
  )

  if (historyQuery.isPending) {
    return <LoadingState label="Loading events…" />
  }
  if (historyQuery.isError) {
    return (
      <ErrorState
        error={historyQuery.error}
        onRetry={() => void historyQuery.refetch()}
      />
    )
  }
  if (events.length === 0) {
    return (
      <EmptyState title="No events yet.">
        Agent messages and tool calls will appear here as the run progresses.
      </EmptyState>
    )
  }
  return <WindowedTimeline events={events} />
}

function diffLineClass(line: string): string {
  if (line.startsWith('diff --git')) {
    return 'font-bold'
  }
  if (line.startsWith('@@')) {
    return 'text-gray-500 dark:text-gray-400'
  }
  if (line.startsWith('+')) {
    return 'bg-green-50 text-green-800 dark:bg-green-950 dark:text-green-200'
  }
  if (line.startsWith('-')) {
    return 'bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-200'
  }
  return ''
}

function DiffView({ diff }: { diff: string }) {
  if (diff.trim() === '') {
    return (
      <EmptyState title="No diff yet.">
        Gaia commits and records a diff when the run starts changing files.
      </EmptyState>
    )
  }
  const lines = diff.split('\n')
  const truncated = diff.includes('[diff truncated]')
  return (
    <div>
      {truncated ? (
        <p
          role="status"
          className="mb-2 text-sm text-amber-700 dark:text-amber-300"
        >
          This diff was truncated by the server.
        </p>
      ) : null}
      <pre className="overflow-auto rounded border border-gray-200 p-3 text-xs dark:border-gray-800">
        {lines.map((line, index) => (
          <span
            key={`${index}-${line}`}
            className={`block ${diffLineClass(line)}`}
          >
            {line || ' '}
          </span>
        ))}
      </pre>
    </div>
  )
}

function DiffTab({ runId }: { runId: string }) {
  const diffQuery = useQuery({
    queryKey: ['run-diff', runId],
    queryFn: () => fetchRunDiff(runId),
  })

  if (diffQuery.isPending) {
    return <LoadingState label="Loading diff…" />
  }
  if (diffQuery.isError) {
    return (
      <ErrorState
        error={diffQuery.error}
        onRetry={() => void diffQuery.refetch()}
      />
    )
  }
  return <DiffView diff={diffQuery.data} />
}

type ActiveTab = 'timeline' | 'diff'

function RunDetailHeader({
  detail,
  onCancel,
  cancelError,
}: {
  detail: RunDetail
  onCancel: () => void
  cancelError: unknown
}) {
  const { run, ticket, worktree } = detail
  return (
    <header data-testid="run-header" className="mb-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold">{ticket.title}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StatusBadge status={run.status} />
            <code className="font-mono">
              {worktree?.branch ?? 'no worktree'}
            </code>
          </div>
        </div>
        <div className="flex items-start gap-3">
          <CostBar run={run} />
          {!TERMINAL_RUN_STATUSES.has(run.status) ? (
            <button
              type="button"
              onClick={onCancel}
              className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-700 dark:border-red-800 dark:text-red-300"
            >
              Cancel run
            </button>
          ) : null}
        </div>
      </div>

      <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Worktree path</dt>
          <dd className="mt-1">
            {worktree ? (
              <WorktreePath path={worktree.path} />
            ) : (
              <span>No worktree</span>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Pull request</dt>
          <dd className="mt-1">
            <PullRequestLink pr={detail.pull_request} />
          </dd>
        </div>
      </dl>
      {cancelError ? <InlineApiError error={cancelError} /> : null}
    </header>
  )
}

export default function RunDetailView({
  eventStreamOptions = {},
}: {
  eventStreamOptions?: UseEventStreamOptions
}) {
  const { id = '' } = useParams()
  const queryClient = useQueryClient()
  // The tab lives in the URL, so the approvals inbox can link straight to a
  // run's diff (`/runs/<id>?tab=diff`) (lead review).
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTab: ActiveTab =
    searchParams.get('tab') === 'diff' ? 'diff' : 'timeline'
  const setActiveTab = (tab: ActiveTab) => {
    const params = new URLSearchParams(searchParams)
    if (tab === 'timeline') {
      params.delete('tab')
    } else {
      params.set('tab', tab)
    }
    setSearchParams(params, { replace: true })
  }
  const [confirmCancel, setConfirmCancel] = useState(false)

  const runQuery = useQuery({
    queryKey: ['runs', id],
    queryFn: () => fetchRunDetail(id),
    enabled: id !== '',
  })

  const cancelMutation = useMutation({
    mutationFn: () => cancelRun(id),
    onSuccess: () => {
      setConfirmCancel(false)
      void queryClient.invalidateQueries({ queryKey: ['runs'] })
      void queryClient.invalidateQueries({ queryKey: ['runs', id] })
    },
    onError: () => {
      setConfirmCancel(false)
    },
  })

  if (runQuery.isPending) {
    return <LoadingState label="Loading run…" />
  }

  if (runQuery.isError) {
    const error = runQuery.error
    const displayError =
      error instanceof ApiError && error.status === 404
        ? new ApiError(404, error.code, 'Run not found', error.requestId)
        : error
    return (
      <ErrorState
        error={displayError}
        onRetry={() => void runQuery.refetch()}
      />
    )
  }

  if (!runQuery.data) {
    return null
  }

  const detail = runQuery.data

  return (
    <section>
      <RunDetailHeader
        detail={detail}
        onCancel={() => setConfirmCancel(true)}
        cancelError={cancelMutation.error}
      />

      <section aria-labelledby="run-steps-heading" data-testid="run-steps">
        <h2 id="run-steps-heading" className="mb-3 text-lg font-semibold">
          Steps
        </h2>
        <StepList steps={detail.steps} />
      </section>

      <section aria-labelledby="run-approvals-heading" className="mt-8">
        <h2 id="run-approvals-heading" className="mb-3 text-lg font-semibold">
          Approvals
        </h2>
        <ApprovalList approvals={detail.approvals} />
      </section>

      <section aria-labelledby="run-details-tabs" className="mt-8">
        <div
          id="run-details-tabs"
          role="tablist"
          aria-label="Run details"
          className="flex gap-1 border-b border-gray-200 dark:border-gray-800"
        >
          <button
            id="run-timeline-tab"
            type="button"
            role="tab"
            aria-selected={activeTab === 'timeline'}
            aria-controls="run-timeline-panel"
            onClick={() => setActiveTab('timeline')}
            className={`rounded-t px-3 py-2 text-sm ${
              activeTab === 'timeline'
                ? 'border border-b-0 border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950'
                : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
            }`}
          >
            Timeline
          </button>
          <button
            id="run-diff-tab"
            type="button"
            role="tab"
            aria-selected={activeTab === 'diff'}
            aria-controls="run-diff-panel"
            onClick={() => setActiveTab('diff')}
            className={`rounded-t px-3 py-2 text-sm ${
              activeTab === 'diff'
                ? 'border border-b-0 border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950'
                : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
            }`}
          >
            Diff
          </button>
        </div>
        {activeTab === 'timeline' ? (
          <div
            id="run-timeline-panel"
            role="tabpanel"
            aria-labelledby="run-timeline-tab"
            data-testid="run-timeline"
            className="pt-4"
          >
            <TimelineTab runId={id} eventStreamOptions={eventStreamOptions} />
          </div>
        ) : (
          <div
            id="run-diff-panel"
            role="tabpanel"
            aria-labelledby="run-diff-tab"
            data-testid="run-diff"
            className="pt-4"
          >
            <DiffTab runId={id} />
          </div>
        )}
      </section>

      <ConfirmDialog
        open={confirmCancel}
        title="Cancel this run?"
        destructive
        confirmLabel="Cancel run"
        busy={cancelMutation.isPending}
        onConfirm={() => cancelMutation.mutate()}
        onCancel={() => setConfirmCancel(false)}
      >
        <p>The run will be marked cancelled. Its worktree is kept on disk.</p>
      </ConfirmDialog>
    </section>
  )
}
