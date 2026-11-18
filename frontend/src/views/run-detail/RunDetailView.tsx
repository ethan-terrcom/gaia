import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bot,
  CheckCircle2,
  Copy,
  ExternalLink,
  FileDiff,
  Hammer,
  MessageSquare,
  Search,
  Terminal,
  Wrench,
  XCircle,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { api } from '../../api/client'
import { ApiError } from '../../api/errors'
import type { components } from '../../api/schema'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { EmptyState, ErrorState, LoadingState } from '../../components/States'
import { StatusBadge } from '../../components/StatusBadge'
import { Button } from '../../components/ui/Button'
import { Panel, PanelBody, PanelHeader } from '../../components/ui/Panel'
import { formatRelative, formatUsd } from '../../components/format'
import {
  type GaiaEvent,
  type UseEventStreamOptions,
  useEventStream,
} from '../../hooks/useEventStream'
import {
  MODELS_QUERY_KEY,
  describeConfig,
  fetchModels,
  type AgentModeConfig,
} from '../agent-modes/agentModes'
import {
  BASE_ROW_HEIGHT,
  isWriteLikeTool,
  messageContent,
  parseUnifiedDiff,
  summarizeEvent,
  timelineRowHeight,
  toolNameFromEvent,
  truncatedPaths,
  type TimelineEvent,
} from './events'

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

const OVERSCAN_ROWS = 5
const DEFAULT_VIEWPORT_HEIGHT = 600
const DIFF_REFETCH_MS = 3_000

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

function linkClassName(): string {
  return 'font-medium text-[var(--color-accent-ink)] underline-offset-2 hover:underline'
}

function CopyPathButton({ path }: { path: string }) {
  const canCopy = typeof navigator.clipboard?.writeText === 'function'
  return (
    <Button
      variant="secondary"
      size="sm"
      disabled={!canCopy}
      onClick={() => {
        void navigator.clipboard.writeText(path)
      }}
    >
      <Copy className="size-3.5" aria-hidden />
      Copy path
    </Button>
  )
}

function WorktreePath({ path }: { path: string }) {
  const vscodeHref = `vscode://file${encodePathPerSegment(path)}`
  return (
    <div className="flex flex-wrap items-center gap-2">
      <code className="rounded-[var(--radius-sm)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-xs">
        {path}
      </code>
      <CopyPathButton path={path} />
      <a
        href={vscodeHref}
        target="_blank"
        rel="noreferrer noopener"
        className={`inline-flex items-center gap-1 text-sm ${linkClassName()}`}
      >
        Open in VS Code
        <ExternalLink className="size-3.5" aria-hidden />
      </a>
    </div>
  )
}

function PullRequestLink({ pr }: { pr: RunPullRequestRead | null }) {
  if (!pr) {
    return (
      <span className="text-[var(--color-ink-muted)]">No pull request yet</span>
    )
  }
  return (
    <span className="flex items-center gap-2">
      {isGitHubUrl(pr.url) ? (
        <a
          href={pr.url}
          target="_blank"
          rel="noreferrer noopener"
          className={linkClassName()}
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
      className="min-w-52"
      aria-label={`Cost ${formatUsd(run.cost_usd)} of budget ${formatUsd(run.budget_usd)}`}
    >
      <div className="text-sm font-medium text-[var(--color-ink)]">
        {formatUsd(run.cost_usd)} / {formatUsd(run.budget_usd)}
      </div>
      <div className="mt-1.5 h-1.5 w-full rounded-full bg-[var(--color-surface-muted)]">
        <div
          className={`h-full rounded-full ${barColor}`}
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
      className="mt-3 rounded-[var(--radius-md)] border border-[var(--color-danger)] bg-[var(--color-danger-soft)] p-3 text-sm text-[var(--color-ink)]"
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
          className="rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-surface)] p-3"
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-[var(--color-ink-muted)]">
              {step.step_index + 1}.
            </span>
            <span className="font-medium text-[var(--color-ink)]">
              {step.title}
            </span>
            <StatusBadge status={step.status} />
            <span className="text-sm text-[var(--color-ink-muted)]">
              {step.attempts} {step.attempts === 1 ? 'attempt' : 'attempts'}
            </span>
          </div>
          {step.result_summary ? (
            <p className="mt-2 whitespace-pre-wrap text-sm text-[var(--color-ink-muted)]">
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
        <thead className="text-xs font-medium uppercase tracking-wide text-[var(--color-ink-subtle)]">
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
              className="border-t border-[var(--color-border)]"
            >
              <td className="px-3 py-2 capitalize text-[var(--color-ink)]">
                {approval.kind.replaceAll('_', ' ')}
              </td>
              <td className="px-3 py-2">
                <StatusBadge status={approval.status} />
              </td>
              <td
                className="px-3 py-2 text-[var(--color-ink-muted)]"
                title={approval.created_at}
              >
                {formatRelative(approval.created_at)}
              </td>
              <td className="px-3 py-2">
                {approval.status === 'pending' ? (
                  <Link to="/approvals" className={linkClassName()}>
                    Review
                  </Link>
                ) : (
                  <span className="text-[var(--color-ink-subtle)]">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TruncatedBadge({ paths }: { paths: string[] }) {
  if (paths.length === 0) {
    return null
  }
  const label =
    paths.length <= 2
      ? paths.join(', ')
      : `${paths.slice(0, 2).join(', ')} +${paths.length - 2}`
  return (
    <span
      title={paths.join(', ')}
      className="shrink-0 rounded-[var(--radius-sm)] bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-900 dark:bg-amber-950 dark:text-amber-200"
    >
      truncated {label}
    </span>
  )
}

function toolIconElement(tool: string | null, className: string) {
  const name = (tool ?? '').toLowerCase()
  if (name.includes('search') || name.includes('grep') || name === 'rg') {
    return <Search className={className} aria-hidden />
  }
  if (name.includes('shell') || name.includes('command') || name === 'bash') {
    return <Terminal className={className} aria-hidden />
  }
  if (
    name.includes('write') ||
    name.includes('edit') ||
    name.includes('patch') ||
    name.includes('file')
  ) {
    return <FileDiff className={className} aria-hidden />
  }
  if (name.includes('commit') || name.includes('git')) {
    return <Hammer className={className} aria-hidden />
  }
  return <Wrench className={className} aria-hidden />
}

function EventTimestamp({ ts }: { ts: string }) {
  return (
    <time
      dateTime={ts}
      title={ts}
      className="shrink-0 text-xs text-[var(--color-ink-subtle)]"
    >
      {formatRelative(ts)}
    </time>
  )
}

function MessageBubble({ event }: { event: TimelineEvent }) {
  const text = messageContent(event.payload)
  const truncated = truncatedPaths(event.payload)
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1 py-2 pl-2">
      <h2 className="flex items-center gap-1.5 text-xs font-medium text-[var(--color-ink-muted)]">
        <MessageSquare className="size-3.5" aria-hidden />
        Agent
        <TruncatedBadge paths={truncated} />
      </h2>
      <p className="line-clamp-3 whitespace-pre-wrap rounded-[var(--radius-lg)] bg-[var(--color-accent-soft)] px-3 py-2 text-sm text-[var(--color-ink)]">
        {text}
      </p>
    </div>
  )
}

function ToolCard({ event }: { event: TimelineEvent }) {
  const tool = toolNameFromEvent(event) ?? 'tool'
  const summary = summarizeEvent(event)
  const isResult = event.type === 'agent.tool_result'
  const ok = event.payload.ok === true
  const iconClass = 'mt-0.5 size-4 shrink-0 text-[var(--color-ink-muted)]'

  return (
    <details className="group min-h-0 flex-1 py-1.5">
      <summary className="flex cursor-pointer list-none items-start gap-2 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-2.5 py-2 hover:bg-[var(--color-surface)]">
        {toolIconElement(tool, iconClass)}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs font-medium text-[var(--color-ink)]">
              {tool}
            </span>
            {isResult && ok ? (
              <CheckCircle2
                className="size-3.5 text-emerald-600 dark:text-emerald-400"
                aria-hidden
              />
            ) : null}
            {isResult && !ok ? (
              <XCircle
                className="size-3.5 text-red-600 dark:text-red-400"
                aria-hidden
              />
            ) : null}
            <TruncatedBadge paths={truncatedPaths(event.payload)} />
          </div>
          <p className="mt-0.5 line-clamp-2 font-mono text-xs text-[var(--color-ink-muted)]">
            {summary}
          </p>
        </div>
      </summary>
      <pre className="mt-1 max-h-48 overflow-auto rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-surface-raised)] p-2 text-xs text-[var(--color-ink)]">
        {JSON.stringify(event.payload, null, 2)}
      </pre>
    </details>
  )
}

function UsageRow({ event }: { event: TimelineEvent }) {
  const summary = summarizeEvent(event)
  return (
    <div className="flex flex-1 items-center gap-2 py-2 text-sm text-[var(--color-ink-muted)]">
      <Bot className="size-4 shrink-0" aria-hidden />
      <span className="font-mono text-xs">{event.type}</span>
      <span className="truncate">{summary}</span>
    </div>
  )
}

function CompactRow({ event }: { event: TimelineEvent }) {
  const summary = summarizeEvent(event)
  return (
    <div className="flex flex-1 items-center gap-2 py-1">
      <span className="shrink-0 font-mono text-xs text-[var(--color-ink-subtle)]">
        {event.type}
      </span>
      <span className="min-w-0 truncate text-sm text-[var(--color-ink)]">
        {summary}
      </span>
      <TruncatedBadge paths={truncatedPaths(event.payload)} />
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
  return (
    <div
      data-testid={`event-row-${event.seq}`}
      style={style}
      className="flex items-start gap-2 border-b border-[var(--color-border)] px-3"
    >
      <EventTimestamp ts={event.ts} />
      {event.type === 'agent.message' ? (
        <MessageBubble event={event} />
      ) : event.type === 'agent.tool_call' ||
        event.type === 'agent.tool_result' ? (
        <ToolCard event={event} />
      ) : event.type === 'agent.usage' ? (
        <UsageRow event={event} />
      ) : (
        <CompactRow event={event} />
      )}
    </div>
  )
}

function buildRowOffsets(events: TimelineEvent[]): number[] {
  const offsets = [0]
  for (const event of events) {
    const height = timelineRowHeight(event)
    offsets.push(offsets[offsets.length - 1]! + height)
  }
  return offsets
}

function indexForScrollOffset(offsets: number[], scrollTop: number): number {
  let low = 0
  let high = offsets.length - 2
  while (low < high) {
    const mid = Math.floor((low + high + 1) / 2)
    if (offsets[mid]! <= scrollTop) {
      low = mid
    } else {
      high = mid - 1
    }
  }
  return low
}

function WindowedTimeline({ events }: { events: TimelineEvent[] }) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [viewportHeight, setViewportHeight] = useState(DEFAULT_VIEWPORT_HEIGHT)

  const heights = useMemo(
    () => events.map((event) => timelineRowHeight(event)),
    [events],
  )
  const offsets = useMemo(() => buildRowOffsets(events), [events])
  const totalHeight = offsets[offsets.length - 1] ?? 0

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) {
      return
    }
    const updateHeight = () => {
      setViewportHeight(viewport.clientHeight || DEFAULT_VIEWPORT_HEIGHT)
    }
    updateHeight()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', updateHeight)
      return () => window.removeEventListener('resize', updateHeight)
    }
    const observer = new ResizeObserver(updateHeight)
    observer.observe(viewport)
    return () => observer.disconnect()
  }, [])

  const startIndex = Math.max(
    0,
    indexForScrollOffset(offsets, scrollTop) - OVERSCAN_ROWS,
  )
  const endOffset = scrollTop + viewportHeight
  let endIndex = startIndex
  while (endIndex < events.length && offsets[endIndex]! < endOffset) {
    endIndex += 1
  }
  endIndex = Math.min(events.length, endIndex + OVERSCAN_ROWS)
  const visible = events.slice(startIndex, endIndex)

  return (
    <div
      ref={viewportRef}
      data-testid="timeline-scroll"
      onScroll={() => {
        setScrollTop(viewportRef.current?.scrollTop ?? 0)
      }}
      className="h-full overflow-auto"
    >
      <div style={{ height: totalHeight, position: 'relative' }}>
        {visible.map((event, index) => {
          const rowIndex = startIndex + index
          const height = heights[rowIndex] ?? BASE_ROW_HEIGHT
          return (
            <TimelineRow
              key={event.seq}
              event={event}
              style={{
                position: 'absolute',
                top: offsets[rowIndex] ?? 0,
                height,
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

function useDiffInvalidation(runId: string, liveEvents: GaiaEvent[]) {
  const queryClient = useQueryClient()
  const seenSeqRef = useRef(0)

  useEffect(() => {
    for (const event of liveEvents) {
      if (event.seq === null || event.seq <= seenSeqRef.current) {
        continue
      }
      if (event.type === 'agent.tool_call') {
        const tool =
          typeof event.payload.tool === 'string' ? event.payload.tool : ''
        if (isWriteLikeTool(tool)) {
          void queryClient.invalidateQueries({ queryKey: ['run-diff', runId] })
        }
      }
      seenSeqRef.current = Math.max(seenSeqRef.current, event.seq)
    }
  }, [liveEvents, queryClient, runId])
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

  useDiffInvalidation(runId, liveEvents)

  if (historyQuery.isPending) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <LoadingState label="Loading events…" />
      </div>
    )
  }
  if (historyQuery.isError) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <ErrorState
          error={historyQuery.error}
          onRetry={() => void historyQuery.refetch()}
        />
      </div>
    )
  }
  if (events.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState title="No events yet.">
          Agent messages and tool calls will appear here as the run progresses.
        </EmptyState>
      </div>
    )
  }
  return <WindowedTimeline events={events} />
}

function diffLineClass(line: string): string {
  if (line.startsWith('diff --git')) {
    return 'font-bold text-[var(--color-ink)]'
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
  return 'text-[var(--color-ink)]'
}

function DiffFileSectionView({
  section,
}: {
  section: ReturnType<typeof parseUnifiedDiff>[number]
}) {
  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        title={section.title}
        actions={
          <FileDiff
            className="size-4 text-[var(--color-ink-muted)]"
            aria-hidden
          />
        }
      />
      <PanelBody className="p-0">
        <pre className="overflow-x-auto p-3 font-mono text-xs leading-relaxed">
          {section.lines.map((line, index) => (
            <span
              key={`${section.id}-${index}-${line}`}
              className={`block ${diffLineClass(line)}`}
            >
              {line || ' '}
            </span>
          ))}
        </pre>
      </PanelBody>
    </Panel>
  )
}

function DiffView({ diff }: { diff: string }) {
  if (diff.trim() === '') {
    return (
      <EmptyState title="No diff yet.">
        Gaia commits and records a diff when the run starts changing files.
      </EmptyState>
    )
  }
  const truncated = diff.includes('[diff truncated]')
  const sections = parseUnifiedDiff(diff)
  return (
    <div className="space-y-3">
      {truncated ? (
        <p role="status" className="text-sm text-amber-700 dark:text-amber-300">
          This diff was truncated by the server.
        </p>
      ) : null}
      {sections.length === 0 ? (
        <pre className="overflow-auto rounded-[var(--radius-lg)] border border-[var(--color-border)] p-3 text-xs">
          {diff}
        </pre>
      ) : (
        sections.map((section) => (
          <DiffFileSectionView key={section.id} section={section} />
        ))
      )}
    </div>
  )
}

function DiffTab({ runId, live }: { runId: string; live: boolean }) {
  const diffQuery = useQuery({
    queryKey: ['run-diff', runId],
    queryFn: () => fetchRunDiff(runId),
    refetchInterval: live ? DIFF_REFETCH_MS : false,
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
    <header
      data-testid="run-header"
      className="border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Link
              to="/runs"
              className="text-[12px] text-[var(--color-ink-subtle)] hover:text-[var(--color-ink-muted)]"
            >
              Runs
            </Link>
            <span className="text-[var(--color-ink-subtle)]">/</span>
            <StatusBadge status={run.status} />
            {runIsLiveBadge(run.status)}
          </div>
          <h1 className="mt-1.5 text-[18px] font-semibold tracking-[-0.03em] text-[var(--color-ink)]">
            {ticket.title}
          </h1>
          <code className="mt-1 inline-block font-mono text-[12px] text-[var(--color-ink-muted)]">
            {worktree?.branch ?? 'no worktree'}
          </code>
        </div>
        <div className="flex items-start gap-3">
          <CostBar run={run} />
          {!TERMINAL_RUN_STATUSES.has(run.status) ? (
            <Button variant="danger" size="sm" onClick={onCancel}>
              Cancel run
            </Button>
          ) : null}
        </div>
      </div>
      {cancelError ? <InlineApiError error={cancelError} /> : null}
    </header>
  )
}

function runIsLiveBadge(status: RunStatus) {
  if (TERMINAL_RUN_STATUSES.has(status)) {
    return null
  }
  return (
    <span className="inline-flex items-center gap-1 text-[11px] font-medium text-[var(--color-success)]">
      <span className="size-1.5 animate-pulse rounded-full bg-[var(--color-success)]" />
      Live
    </span>
  )
}

function RunMetaSidebar({ detail }: { detail: RunDetail }) {
  const { worktree } = detail
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
      <section className="rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-surface-raised)]">
        <div className="border-b border-[var(--color-border)] px-3 py-2 text-[12px] font-medium text-[var(--color-ink-muted)]">
          Details
        </div>
        <dl className="grid gap-3 p-3 text-[13px]">
          <div>
            <dt className="text-[11px] tracking-wide text-[var(--color-ink-subtle)] uppercase">
              Worktree
            </dt>
            <dd className="mt-1">
              {worktree ? (
                <WorktreePath path={worktree.path} />
              ) : (
                <span className="text-[var(--color-ink-muted)]">
                  No worktree
                </span>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] tracking-wide text-[var(--color-ink-subtle)] uppercase">
              Pull request
            </dt>
            <dd className="mt-1">
              <PullRequestLink pr={detail.pull_request} />
            </dd>
          </div>
          <div>
            <dt className="text-[11px] tracking-wide text-[var(--color-ink-subtle)] uppercase">
              Agent mode
            </dt>
            <dd className="mt-1 text-[var(--color-ink)]">
              <RunAgentMode config={detail.run.agent_config ?? null} />
            </dd>
          </div>
        </dl>
      </section>

      <section className="rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-surface-raised)]">
        <div className="border-b border-[var(--color-border)] px-3 py-2 text-[12px] font-medium text-[var(--color-ink-muted)]">
          Steps
        </div>
        <div className="p-3" data-testid="run-steps">
          <StepList steps={detail.steps} />
        </div>
      </section>

      <section className="rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-surface-raised)]">
        <div className="border-b border-[var(--color-border)] px-3 py-2 text-[12px] font-medium text-[var(--color-ink-muted)]">
          Approvals
        </div>
        <div className="p-3">
          <ApprovalList approvals={detail.approvals} />
        </div>
      </section>
    </div>
  )
}

/** The mode and models this run started with (doc 19). */
function RunAgentMode({ config }: { config: AgentModeConfig | null }) {
  const models = useQuery({ queryKey: MODELS_QUERY_KEY, queryFn: fetchModels })
  if (config === null) {
    return <span>Two tiered (started before agent modes)</span>
  }
  return <span>{describeConfig(config, models.data?.models)}</span>
}

export default function RunDetailView({
  eventStreamOptions = {},
}: {
  eventStreamOptions?: UseEventStreamOptions
}) {
  const { id = '' } = useParams()
  const queryClient = useQueryClient()
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
  const runIsLive = !TERMINAL_RUN_STATUSES.has(detail.run.status)

  return (
    <section className="flex h-full min-h-[32rem] flex-col bg-[var(--color-surface-muted)]">
      <RunDetailHeader
        detail={detail}
        onCancel={() => setConfirmCancel(true)}
        cancelError={cancelMutation.error}
      />

      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(17rem,22rem)_minmax(0,1fr)]">
        <aside className="min-h-0 border-b border-[var(--color-border)] bg-[var(--color-surface)] lg:border-r lg:border-b-0">
          <RunMetaSidebar detail={detail} />
        </aside>

        <section
          aria-labelledby="run-details-tabs"
          className="flex min-h-0 min-w-0 flex-col bg-[var(--color-surface-muted)]"
        >
          <div
            id="run-details-tabs"
            role="tablist"
            aria-label="Run workspace"
            className="flex h-10 shrink-0 items-center gap-1 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-2"
          >
            <button
              id="run-timeline-tab"
              type="button"
              role="tab"
              aria-selected={activeTab === 'timeline'}
              aria-controls="run-timeline-panel"
              onClick={() => setActiveTab('timeline')}
              className={`inline-flex h-7 items-center rounded-[var(--radius-md)] px-3 text-[12px] font-medium tracking-[-0.01em] transition ${
                activeTab === 'timeline'
                  ? 'bg-[var(--color-surface-overlay)] text-[var(--color-ink)]'
                  : 'text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]'
              }`}
            >
              Agent activity
            </button>
            <button
              id="run-diff-tab"
              type="button"
              role="tab"
              aria-selected={activeTab === 'diff'}
              aria-controls="run-diff-panel"
              onClick={() => setActiveTab('diff')}
              className={`inline-flex h-7 items-center rounded-[var(--radius-md)] px-3 text-[12px] font-medium tracking-[-0.01em] transition ${
                activeTab === 'diff'
                  ? 'bg-[var(--color-surface-overlay)] text-[var(--color-ink)]'
                  : 'text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]'
              }`}
            >
              Diff
            </button>
            {runIsLive && activeTab === 'timeline' ? (
              <span className="ml-auto pr-2 text-[11px] text-[var(--color-ink-subtle)]">
                Streaming live
              </span>
            ) : null}
            {runIsLive && activeTab === 'diff' ? (
              <span className="ml-auto pr-2 text-[11px] text-[var(--color-ink-subtle)]">
                Auto-refreshing
              </span>
            ) : null}
          </div>

          {activeTab === 'timeline' ? (
            <div
              id="run-timeline-panel"
              role="tabpanel"
              aria-labelledby="run-timeline-tab"
              data-testid="run-timeline"
              className="min-h-0 flex-1 overflow-hidden p-3"
            >
              <div className="h-full overflow-hidden rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-surface-raised)]">
                <TimelineTab
                  runId={id}
                  eventStreamOptions={eventStreamOptions}
                />
              </div>
            </div>
          ) : (
            <div
              id="run-diff-panel"
              role="tabpanel"
              aria-labelledby="run-diff-tab"
              data-testid="run-diff"
              className="min-h-0 flex-1 overflow-auto p-3"
            >
              <DiffTab runId={id} live={runIsLive} />
            </div>
          )}
        </section>
      </div>

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
