import { createElement, type ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { makeQueryClient } from '../api/queryClient'
import {
  type GaiaEvent,
  type GaiaEventStreamSource,
  useEventStream,
} from './useEventStream'

class FakeEventSource implements GaiaEventStreamSource {
  static instances: FakeEventSource[] = []

  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  closed = false
  readonly url: string
  readonly listeners = new Map<string, ((event: MessageEvent) => void)[]>()

  addEventListener(
    type: string,
    listener: (event: MessageEvent) => void,
  ): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener])
  }

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  close(): void {
    this.closed = true
  }

  /** Like a browser: a named event reaches its listeners, never ``onmessage``. */
  emitMessage(event: GaiaEvent): void {
    this.emitNamed(event.type, JSON.stringify(event))
  }

  emitNamed(type: string, data: string): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data } as MessageEvent)
    }
  }

  emitError(): void {
    this.onerror?.(new Event('error'))
  }
}

function event(overrides: Partial<GaiaEvent> = {}): GaiaEvent {
  return {
    seq: 1,
    id: 'event-1',
    workspace_id: 'ws-1',
    run_id: 'run-1',
    agent_id: null,
    type: 'run.created',
    payload: {},
    ts: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderStream(
  options: {
    runId?: string
    EventSourceImpl?: new (url: string) => GaiaEventStreamSource
    ticketFetcher?: () => Promise<{ ticket: string }>
  } = {},
  queryClient = makeQueryClient(),
) {
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children)
  return renderHook(() => useEventStream({ runId: options.runId }, options), {
    wrapper,
  })
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

describe('useEventStream', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('connects with a fresh ticket', async () => {
    const ticketFetcher = vi.fn().mockResolvedValue({ ticket: 'ticket-1' })

    const { result } = renderStream({
      runId: 'run-1',
      EventSourceImpl: FakeEventSource,
      ticketFetcher,
    })
    await flush()

    expect(ticketFetcher).toHaveBeenCalledTimes(1)
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0]?.url).toBe(
      '/api/v1/stream?ticket=ticket-1&run_id=run-1',
    )
    expect(result.current.status).toBe('connecting')
  })

  it('reconnects with a new ticket and the last event id', async () => {
    vi.useFakeTimers()
    const ticketFetcher = vi
      .fn()
      .mockResolvedValueOnce({ ticket: 'ticket-1' })
      .mockResolvedValueOnce({ ticket: 'ticket-2' })

    renderStream({
      runId: 'run-1',
      EventSourceImpl: FakeEventSource,
      ticketFetcher,
    })
    await flush()

    FakeEventSource.instances[0]?.emitMessage(event({ seq: 42 }))
    FakeEventSource.instances[0]?.emitError()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000)
      await Promise.resolve()
    })

    expect(ticketFetcher).toHaveBeenCalledTimes(2)
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(FakeEventSource.instances[1]?.url).toBe(
      '/api/v1/stream?ticket=ticket-2&run_id=run-1&last_event_id=42',
    )
  })

  it('drops an invalid payload', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const ticketFetcher = vi.fn().mockResolvedValue({ ticket: 'ticket-1' })

    const { result } = renderStream({
      EventSourceImpl: FakeEventSource,
      ticketFetcher,
    })
    await flush()

    FakeEventSource.instances[0]?.emitNamed('run.created', 'not-json')

    expect(result.current.events).toEqual([])
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })

  it('keeps the ring buffer capped at 500', async () => {
    const ticketFetcher = vi.fn().mockResolvedValue({ ticket: 'ticket-1' })

    const { result } = renderStream({
      EventSourceImpl: FakeEventSource,
      ticketFetcher,
    })
    await flush()

    act(() => {
      for (let index = 1; index <= 501; index += 1) {
        FakeEventSource.instances[0]?.emitMessage(
          event({ seq: index, id: `event-${index}`, type: 'step.started' }),
        )
      }
    })

    expect(result.current.events).toHaveLength(500)
    expect(result.current.events[0]?.seq).toBe(2)
    expect(result.current.events[499]?.seq).toBe(501)
  })

  it('invalidates the query keys for each event type', async () => {
    const queryClient = makeQueryClient()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const ticketFetcher = vi.fn().mockResolvedValue({ ticket: 'ticket-1' })

    renderStream(
      {
        EventSourceImpl: FakeEventSource,
        ticketFetcher,
      },
      queryClient,
    )
    await flush()

    act(() => {
      FakeEventSource.instances[0]?.emitMessage(
        event({ type: 'run.created', run_id: 'run-1' }),
      )
      FakeEventSource.instances[0]?.emitMessage(event({ type: 'agent.usage' }))
      FakeEventSource.instances[0]?.emitMessage(
        event({ type: 'approval.requested' }),
      )
      FakeEventSource.instances[0]?.emitMessage(
        event({ type: 'run.status_changed', run_id: 'run-1' }),
      )
    })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['runs'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['runs', 'run-1'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['costs'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['approvals'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['admin'] })
  })
})

describe('useEventStream named events (lead review U1)', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
  })

  it('listens for every event type the server can name', async () => {
    const ticketFetcher = vi.fn().mockResolvedValue({ ticket: 'ticket-1' })
    renderStream({ EventSourceImpl: FakeEventSource, ticketFetcher })
    await flush()

    const source = FakeEventSource.instances[0]!
    for (const type of [
      'run.created',
      'approval.requested',
      'agent.usage',
      'stream.gap',
    ]) {
      expect(source.listeners.has(type)).toBe(true)
    }
  })

  it('receives a named event the way a browser delivers it', async () => {
    const ticketFetcher = vi.fn().mockResolvedValue({ ticket: 'ticket-1' })
    const { result } = renderStream({
      EventSourceImpl: FakeEventSource,
      ticketFetcher,
    })
    await flush()

    act(() => {
      FakeEventSource.instances[0]?.emitMessage(
        event({ type: 'approval.requested', seq: 7 }),
      )
    })

    expect(result.current.events.map((item) => item.type)).toEqual([
      'approval.requested',
    ])
  })

  it('keeps a stream.gap marker, whose seq and id are null', async () => {
    const ticketFetcher = vi.fn().mockResolvedValue({ ticket: 'ticket-1' })
    const { result } = renderStream({
      EventSourceImpl: FakeEventSource,
      ticketFetcher,
    })
    await flush()

    act(() => {
      FakeEventSource.instances[0]?.emitMessage(
        event({ type: 'run.created', seq: 5 }),
      )
      FakeEventSource.instances[0]?.emitMessage(
        event({
          type: 'stream.gap',
          seq: null,
          id: null,
          payload: { missed_from: 3 },
        }),
      )
      FakeEventSource.instances[0]?.emitError()
    })

    expect(result.current.events.map((item) => item.type)).toEqual([
      'run.created',
      'stream.gap',
    ])
  })
})
