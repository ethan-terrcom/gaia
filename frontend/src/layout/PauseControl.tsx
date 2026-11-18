import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import { ApiError } from '../api/errors'
import type { components } from '../api/schema'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { formatRelative } from '../components/format'
import { Button } from '../components/ui/Button'
import { Field } from '../components/ui/Field'
import { Textarea } from '../components/ui/Input'

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
      className="rounded-[var(--radius-md)] border border-[var(--color-danger)]/30 bg-[var(--color-danger-soft)] p-3 text-[13px] text-[var(--color-danger)]"
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
        <Button
          type="button"
          variant="danger"
          size="sm"
          onClick={() => {
            pauseMutation.reset()
            setDialogOpen(true)
          }}
        >
          Pause all
        </Button>
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
            <Field label="Reason (optional)" htmlFor="pause-all-reason">
              <Textarea
                id="pause-all-reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                maxLength={500}
                rows={3}
              />
            </Field>
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
      <div className="flex items-center gap-4 border-b border-[var(--color-warning)]/30 bg-[color-mix(in_srgb,var(--color-warning)_12%,transparent)] px-4 py-2 text-[13px] text-[var(--color-warning)]">
        <p className="min-w-0 flex-1">
          All runs are paused since {formatRelative(pause.since)}
          {pause.reason ? ` (${pause.reason})` : ''}
        </p>
        {resumeMutation.isError ? (
          <InlineApiError error={resumeMutation.error} />
        ) : null}
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={() => resumeMutation.mutate()}
          disabled={resumeMutation.isPending}
        >
          {resumeMutation.isPending ? 'Resuming…' : 'Resume all'}
        </Button>
      </div>
    )
  }

  return null
}
