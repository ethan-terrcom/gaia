import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError } from '../../api/errors'
import { AgentModeEditor } from '../agent-modes/AgentModeEditor'
import {
  AGENT_MODE_QUERY_KEY,
  MODELS_QUERY_KEY,
  configProblem,
  describeConfig,
  fetchAgentMode,
  fetchModels,
  normalizeConfig,
  saveAgentMode,
  type AgentModeConfig,
} from '../agent-modes/agentModes'

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback
}

/** The default agent mode and models for new runs (doc 19), on the Settings page. */
export function AgentModeSettings() {
  const queryClient = useQueryClient()
  const modelsQuery = useQuery({
    queryKey: MODELS_QUERY_KEY,
    queryFn: fetchModels,
  })
  const modeQuery = useQuery({
    queryKey: AGENT_MODE_QUERY_KEY,
    queryFn: fetchAgentMode,
  })
  // Local edits; ``null`` means "show the saved default as it is".
  const [edits, setEdits] = useState<AgentModeConfig | null>(null)

  const mutation = useMutation({
    mutationFn: saveAgentMode,
    onSuccess: (data) => {
      queryClient.setQueryData(AGENT_MODE_QUERY_KEY, data)
      setEdits(null)
    },
  })

  const loading = modelsQuery.isLoading || modeQuery.isLoading
  const loadError = modelsQuery.error ?? modeQuery.error
  const saved = modeQuery.data
  const draft = edits ?? saved?.config ?? null
  const dirty =
    draft !== null &&
    saved !== undefined &&
    JSON.stringify(normalizeConfig(draft)) !==
      JSON.stringify(normalizeConfig(saved.config))

  return (
    <section aria-labelledby="agent-mode-heading" className="mb-8 max-w-3xl">
      <h2 id="agent-mode-heading" className="mb-1 font-semibold">
        Agent mode
      </h2>
      <p className="mb-3 text-sm text-gray-600 dark:text-gray-400">
        How new runs split the work between models. You can still choose
        differently when you start a run, and a run keeps the models it started
        with.
      </p>

      {loading ? (
        <p className="text-sm text-gray-500">Loading agent mode…</p>
      ) : null}
      {loadError ? (
        <p role="alert" className="text-sm text-red-700 dark:text-red-300">
          {errorMessage(loadError, 'Could not load the agent mode.')}
        </p>
      ) : null}

      {draft && modelsQuery.data && saved ? (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            mutation.mutate(normalizeConfig(draft))
          }}
          className="grid gap-4"
        >
          <p className="text-sm">
            {saved.saved ? 'Saved default' : 'Default from settings'}:{' '}
            <span className="font-medium">
              {describeConfig(saved.config, modelsQuery.data.models)}
            </span>
          </p>
          <AgentModeEditor
            idPrefix="settings-agent-mode"
            value={draft}
            catalog={modelsQuery.data}
            onChange={setEdits}
          />
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={
                mutation.isPending || !dirty || configProblem(draft) !== null
              }
              className="rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
            >
              {mutation.isPending ? 'Saving…' : 'Save default'}
            </button>
            {dirty ? (
              <button
                type="button"
                onClick={() => setEdits(null)}
                className="rounded border border-gray-300 px-3 py-1.5 dark:border-gray-700"
              >
                Discard changes
              </button>
            ) : null}
          </div>
          {mutation.isError ? (
            <p role="alert" className="text-sm text-red-700 dark:text-red-300">
              {errorMessage(mutation.error, 'Could not save the agent mode.')}
            </p>
          ) : null}
        </form>
      ) : null}
    </section>
  )
}
