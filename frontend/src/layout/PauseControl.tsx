import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import { ApiError } from '../api/errors'
import type { components } from '../api/schema'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { formatRelative } from '../components/format'

type PauseAllResult = components['schemas']['PauseAllResult']

const PAUSE_QUERY_KEY = ['admin', 'pause'] as const

async function fetchPauseStatus(fetchImpl: typeof globalThis.fetch) {
  const response = await api.GET('/api/v1/admin/pause', { fetch: fetchImpl })
  return response.data
}

async function pauseAll(
  reason: string | null,
  fetchImpl: typeof globalThis.fetch,
): Promise<PauseAllResult> {
  const response = await api.POST('/api/v1/admin/pause-all', {
    body: reason ? { reason } : {},
    fetch: fetchImpl,
  })
  if (response.error) {
    throw response.error
  }
  return response.data
}

async function resumeAll(fetchImpl: typeof globalThis.fetch) {
  const response = await api.POST('/api/v1/admin/resume-all', {
    fetch: fetchImpl,
  })
  return response.data
}

function InlineApiError({ error }: { error: unknown }) {
  const message =
    error instanceof ApiError
      ? error.message
      : 'Something went wrong while changing the pause state.'
  return (
    <p
      role="alert"
      className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
    >
      {message}
    </p>
  )
}

/**
 * The global kill switch (doc 18a). Mount ``variant="button"`` in the top bar
 * and ``variant="banner"`` directly under it; both share the
 * ``['admin', 'pause']`` query, so React Query only fetches once.
 */
export function PauseControl({ variant }: { variant: 'button' | 'banner' }) {
  const queryClient = useQueryClient()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [reason, setReason] = useState('')

  const pauseQuery = useQuery({
    queryKey: PAUSE_QUERY_KEY,
    queryFn: () => fetchPauseStatus(fetch),
  })

  const pauseMutation = useMutation({
    mutationFn: (nextReason: string | null) => pauseAll(nextReason, fetch),
    onSuccess: () => {
      setDialogOpen(false)
      setReason('')
      void queryClient.invalidateQueries({ queryKey: ['admin'] })
      void queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  const resumeMutation = useMutation({
    mutationFn: () => resumeAll(fetch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin'] })
      void queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  // The control is not critical chrome: a 503 (orchestrator off) and every
  // other error render nothing, and nothing is logged.
  if (pauseQuery.isLoading || pauseQuery.isError) {
    return null
  }

  const pause = pauseQuery.data
  if (!pause?.paused && variant === 'button') {
    const trimmedReason = reason.trim()
    return (
      <>
        <button
          type="button"
          onClick={() => {
            pauseMutation.reset()
            setDialogOpen(true)
          }}
          className="rounded border border-red-600 px-3 py-1.5 text-sm text-red-700 hover:bg-red-50 dark:border-red-500 dark:text-red-300 dark:hover:bg-red-950"
        >
          Pause all
        </button>
        <ConfirmDialog
          open={dialogOpen}
          title="Pause every run?"
          destructive
          confirmLabel="Pause all"
          busy={pauseMutation.isPending}
          onConfirm={() => {
            pauseMutation.mutate(trimmedReason === '' ? null : trimmedReason)
          }}
          onCancel={() => setDialogOpen(false)}
        >
          <div className="space-y-3">
            <p>
              Stops every running agent now and holds every run until you
              resume. Work in progress restarts from its last completed step.
            </p>
            <div>
              <label
                htmlFor="pause-all-reason"
                className="block text-sm font-medium"
              >
                Reason (optional)
              </label>
              <textarea
                id="pause-all-reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                maxLength={500}
                rows={3}
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-950"
              />
            </div>
            {pauseMutation.isError ? (
              <InlineApiError error={pauseMutation.error} />
            ) : null}
          </div>
        </ConfirmDialog>
      </>
    )
  }

  if (pause?.paused && variant === 'banner') {
    return (
      <div className="flex items-center gap-4 border-b border-amber-300 bg-amber-50 px-6 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100">
        <p className="min-w-0 flex-1">
          All runs are paused since {formatRelative(pause.since)}
          {pause.reason ? ` (${pause.reason})` : ''}
        </p>
        {resumeMutation.isError ? (
          <InlineApiError error={resumeMutation.error} />
        ) : null}
        <button
          type="button"
          onClick={() => resumeMutation.mutate()}
          disabled={resumeMutation.isPending}
          className="rounded border border-amber-600 px-3 py-1.5 text-sm disabled:opacity-50"
        >
          {resumeMutation.isPending ? 'Resuming…' : 'Resume all'}
        </button>
      </div>
    )
  }

  return null
}
