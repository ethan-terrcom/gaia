import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ApiError } from '../../api/errors'
import type { components } from '../../api/schema'

type AutoModeStatus = components['schemas']['AutoModeStatus']
type Family = keyof AutoModeStatus

const AUTO_MODE_QUERY_KEY = ['admin', 'auto-mode'] as const

const FAMILIES: readonly { family: Family; label: string; detail: string }[] = [
  {
    family: 'claude',
    label: 'Claude',
    detail: 'Agents running an Anthropic model.',
  },
  {
    family: 'deepseek',
    label: 'DeepSeek',
    detail: 'Agents running a DeepSeek model.',
  },
  {
    family: 'openai',
    label: 'OpenAI',
    detail: 'Agents running an OpenAI (GPT) model.',
  },
  {
    family: 'xai',
    label: 'xAI',
    detail: 'Agents running an xAI (Grok) model.',
  },
]

async function fetchAutoMode(): Promise<AutoModeStatus> {
  // Pass the current global `fetch` so tests can stub it after import (doc 17).
  // An error response throws an `ApiError` from the client middleware.
  const response = await api.GET('/api/v1/admin/auto-mode', { fetch })
  if (response.data === undefined) {
    throw new Error('empty auto-mode response')
  }
  return response.data
}

async function updateAutoMode(
  change: Partial<AutoModeStatus>,
): Promise<AutoModeStatus> {
  const response = await api.PATCH('/api/v1/admin/auto-mode', {
    body: change,
    fetch,
  })
  if (response.error) {
    throw response.error
  }
  return response.data
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback
}

/** Per-model-family auto mode for agent commands, on the Settings page. */
export function AutoModeSettings() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: AUTO_MODE_QUERY_KEY,
    queryFn: fetchAutoMode,
  })
  const mutation = useMutation({
    mutationFn: updateAutoMode,
    onSuccess: (data) => {
      queryClient.setQueryData(AUTO_MODE_QUERY_KEY, data)
    },
  })

  return (
    <section aria-labelledby="auto-mode-heading" className="mb-8 max-w-2xl">
      <h2 id="auto-mode-heading" className="mb-1 font-semibold">
        Auto mode
      </h2>
      <p className="mb-3 text-sm text-gray-600 dark:text-gray-400">
        Agents in auto mode run commands that would normally need your approval
        without waiting for you. Commands still run in the sandbox, blocked
        commands (like <code>sudo</code>, <code>curl</code> or <code>rm</code>)
        stay blocked, and edits to CI, git hook and <code>.envrc</code> files
        still ask. Plan and pull-request approvals are not affected. A change
        applies to each agent&apos;s next command.
      </p>

      {query.isLoading ? (
        <p className="text-sm text-gray-500">Loading auto mode…</p>
      ) : null}

      {query.isError ? (
        <p role="alert" className="text-sm text-red-700 dark:text-red-300">
          {errorMessage(query.error, 'Could not load auto mode.')}
        </p>
      ) : null}

      {query.data ? (
        <ul className="divide-y divide-gray-200 rounded border border-gray-200 dark:divide-gray-800 dark:border-gray-800">
          {FAMILIES.map(({ family, label, detail }) => {
            const on = query.data[family]
            return (
              <li
                key={family}
                className="flex items-center justify-between gap-4 px-4 py-3"
              >
                <div>
                  <p id={`auto-mode-${family}`} className="font-medium">
                    {label}
                  </p>
                  <p className="text-sm text-gray-600 dark:text-gray-400">
                    {detail}
                  </p>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={on}
                  aria-labelledby={`auto-mode-${family}`}
                  disabled={mutation.isPending}
                  onClick={() => mutation.mutate({ [family]: !on })}
                  className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${
                    on ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-700'
                  }`}
                >
                  <span
                    aria-hidden="true"
                    className={`inline-block h-5 w-5 rounded-full bg-white shadow transition-transform ${
                      on ? 'translate-x-5' : 'translate-x-0.5'
                    }`}
                  />
                </button>
              </li>
            )
          })}
        </ul>
      ) : null}

      {mutation.isError ? (
        <p role="alert" className="mt-2 text-sm text-red-700 dark:text-red-300">
          {errorMessage(mutation.error, 'Could not change auto mode.')}
        </p>
      ) : null}
    </section>
  )
}
