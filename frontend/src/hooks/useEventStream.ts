import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { z } from 'zod'
import { api } from '../api/client'
import { ApiError } from '../api/errors'
import EVENT_TYPES from '../api/event-types.json'

export type StreamStatus = 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface GaiaEvent {
  /** ``null`` only for the transport-only ``stream.gap`` marker. */
  seq: number | null
  id: string | null
  workspace_id: string
  run_id: string | null
  agent_id: string | null
  type: string
  payload: Record<string, unknown>
  ts: string
}

export interface GaiaEventStreamSource {
  onopen: ((event: Event) => void) | null
  onmessage: ((event: MessageEvent) => void) | null
  onerror: ((event: Event) => void) | null
  addEventListener(type: string, listener: (event: MessageEvent) => void): void
  close(): void
}

export interface UseEventStreamOptions {
  EventSourceImpl?: new (url: string) => GaiaEventStreamSource
  ticketFetcher?: () => Promise<{ ticket: string }>
}

const gaiaEventSchema = z.object({
  seq: z.number().int().nullable(),
  id: z.string().nullable(),
  workspace_id: z.string(),
  run_id: z.string().nullable(),
  agent_id: z.string().nullable(),
  type: z.string(),
  payload: z.record(z.string(), z.unknown()),
  ts: z.string(),
})

const RING_BUFFER_LIMIT = 500

const OPEN_RESET_MS = 30_000

const BACKOFF_SEQUENCE_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000]

/**
 * Base query keys invalidated for each event type. A key ending in ``.`` is a
 * prefix match; anything else is an exact type match.
 */
export const INVALIDATIONS: Record<string, readonly (readonly string[])[]> = {
  'run.': [['runs']],
  'step.': [['runs']],
  'approval.': [['approvals']],
  'pr.': [['pull-requests']],
  'ci.': [['pull-requests']],
  'worktree.': [['worktrees']],
  'agent.usage': [['costs']],
  'run.budget_exceeded': [['costs']],
}

function backoffDelayMs(attempt: number): number {
  const index = Math.max(
    0,
    Math.min(attempt - 1, BACKOFF_SEQUENCE_MS.length - 1),
  )
  const base =
    BACKOFF_SEQUENCE_MS[index] ??
    BACKOFF_SEQUENCE_MS[BACKOFF_SEQUENCE_MS.length - 1]!
  const jitter = base * 0.2 * (Math.random() * 2 - 1)
  return Math.round(base + jitter)
}

async function defaultTicketFetcher(): Promise<{ ticket: string }> {
  const { data, error } = await api.POST('/api/v1/stream/ticket')
  if (error) {
    throw error
  }
  return data
}

function parseGaiaEvent(raw: string): GaiaEvent | null {
  try {
    return gaiaEventSchema.parse(JSON.parse(raw))
  } catch (error) {
    if (import.meta.env.DEV) {
      console.warn('dropping invalid stream event', error)
    }
    return null
  }
}

export function useEventStream(
  { runId }: { runId?: string } = {},
  options: UseEventStreamOptions = {},
) {
  const queryClient = useQueryClient()
  const [events, setEvents] = useState<GaiaEvent[]>([])
  const [status, setStatus] = useState<StreamStatus>('connecting')
  const lastSeqRef = useRef<number | null>(null)
  const sourceRef = useRef<GaiaEventStreamSource | null>(null)
  const backoffAttemptRef = useRef(0)
  const reconnectTimerRef = useRef<number | null>(null)
  const openTimerRef = useRef<number | null>(null)

  const invalidateFor = useCallback(
    (event: GaiaEvent) => {
      if (event.type === 'stream.gap') {
        queryClient.invalidateQueries({ queryKey: ['runs'] })
        if (event.run_id) {
          queryClient.invalidateQueries({ queryKey: ['runs', event.run_id] })
        }
        return
      }

      for (const [matcher, baseKeyLists] of Object.entries(INVALIDATIONS)) {
        const matches = matcher.endsWith('.')
          ? event.type.startsWith(matcher)
          : event.type === matcher
        if (!matches) {
          continue
        }
        for (const baseKey of baseKeyLists) {
          queryClient.invalidateQueries({ queryKey: baseKey })
          if ((matcher === 'run.' || matcher === 'step.') && event.run_id) {
            queryClient.invalidateQueries({
              queryKey: [...baseKey, event.run_id],
            })
          }
        }
      }
    },
    [queryClient],
  )

  useEffect(() => {
    let stopped = false
    const EventSourceImpl = options.EventSourceImpl ?? window.EventSource
    const fetchTicket = options.ticketFetcher ?? defaultTicketFetcher

    const clearTimers = () => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      if (openTimerRef.current !== null) {
        window.clearTimeout(openTimerRef.current)
        openTimerRef.current = null
      }
    }

    const resetBackoff = () => {
      backoffAttemptRef.current = 0
    }

    const scheduleReconnect = () => {
      setStatus('reconnecting')
      backoffAttemptRef.current += 1
      const delay = backoffDelayMs(backoffAttemptRef.current)
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null
        void connect()
      }, delay)
    }

    const handleMessage = (event: MessageEvent) => {
      const parsed = parseGaiaEvent(String(event.data))
      if (!parsed) {
        return
      }
      if (openTimerRef.current !== null) {
        window.clearTimeout(openTimerRef.current)
        openTimerRef.current = null
      }
      resetBackoff()
      if (parsed.seq !== null) {
        lastSeqRef.current = parsed.seq
      }
      setEvents((previous) => [...previous, parsed].slice(-RING_BUFFER_LIMIT))
      invalidateFor(parsed)
    }

    const handleOpen = () => {
      setStatus('open')
      openTimerRef.current = window.setTimeout(() => {
        openTimerRef.current = null
        resetBackoff()
      }, OPEN_RESET_MS)
    }

    const handleError = () => {
      sourceRef.current?.close()
      sourceRef.current = null
      clearTimers()
      scheduleReconnect()
    }

    async function connect(): Promise<void> {
      if (stopped) {
        return
      }
      if (typeof EventSourceImpl === 'undefined') {
        setStatus('closed')
        return
      }
      setStatus((current) => (current === 'open' ? 'reconnecting' : current))
      try {
        const { ticket } = await fetchTicket()
        if (stopped) {
          return
        }
        const params = new URLSearchParams({ ticket })
        if (runId) {
          params.set('run_id', runId)
        }
        if (lastSeqRef.current !== null) {
          params.set('last_event_id', String(lastSeqRef.current))
        }
        const source = new EventSourceImpl(`/api/v1/stream?${params}`)
        sourceRef.current = source
        source.onopen = handleOpen
        source.onerror = handleError
        // The server names every event (``event: run.created``), and a browser
        // delivers named events only to listeners for that name, never to
        // ``onmessage`` (lead review U1). ``event-types.json`` is checked
        // against the backend's EventType enum by a backend test.
        for (const type of EVENT_TYPES) {
          source.addEventListener(type, handleMessage)
        }
        source.onmessage = handleMessage
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          setStatus('closed')
          return
        }
        if (!stopped) {
          scheduleReconnect()
        }
      }
    }

    void connect()

    return () => {
      stopped = true
      clearTimers()
      sourceRef.current?.close()
      sourceRef.current = null
    }
  }, [
    runId,
    queryClient,
    invalidateFor,
    options.EventSourceImpl,
    options.ticketFetcher,
  ])

  return { events, status }
}
