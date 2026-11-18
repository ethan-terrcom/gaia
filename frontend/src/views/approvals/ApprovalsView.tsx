import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { z } from 'zod'
import { api } from '../../api/client'
import { ApiError } from '../../api/errors'
import type { components } from '../../api/schema'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
} from '../../components/States'
import { formatRelative, formatUsd } from '../../components/format'
import { TONE_CLASSES, type Tone } from '../../components/status'

type ApprovalRead = components['schemas']['ApprovalRead']
type ApprovalKind = components['schemas']['ApprovalKind']
type Decision = components['schemas']['DecideRequest']['decision']
type ApprovalPage = components['schemas']['ApprovalPage']

type InboxMode = 'pending' | 'decided'

type DecideAction = {
  decision: Decision
  editedPayload?: Record<string, unknown>
  confirm?: string
  budgetUsd?: string | null
  remember?: boolean
}

type StepDraft = {
  title: string
  instructions: string
  testCommand: string
  files: string[]
  acceptance: string[]
  // The worker the lead assigned (doc 19); kept as it is through an edit.
  worker: string | null
}

const SEQUENCE_TIMEOUT_MS = 1_000

const KIND_TONES: Record<ApprovalKind, Tone> = {
  plan: 'info',
  command: 'warn',
  escalation: 'bad',
  pr_open: 'info',
  merge: 'warn',
  budget: 'warn',
}

const planStepSchema = z.object({
  title: z.string().trim().min(1, 'title must not be empty'),
  instructions: z.string().trim().min(1, 'instructions must not be empty'),
  files_likely_touched: z.array(z.string()),
  acceptance: z.array(z.string()),
  test_command: z.array(z.string()).nullable(),
  worker: z.string().nullable().optional(),
})

const planSchema = z.object({
  summary: z.string(),
  risks: z.array(z.string()),
  steps: z
    .array(planStepSchema)
    .min(1, 'a plan must have at least one step')
    .max(10, 'a plan can have at most 10 steps'),
})

// Display tolerates the one invalid shape the edit form is meant to catch:
// more than 10 steps. The backend normally never produces that, but a test
// (and a defensive inbox) must still be able to show the payload while the
// stricter ``planSchema`` blocks submitting it.
const planDisplaySchema = z.object({
  summary: z.string(),
  risks: z.array(z.string()),
  steps: z.array(planStepSchema).min(1, 'a plan must have at least one step'),
})

type PlanPayload = z.infer<typeof planDisplaySchema>

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false
  }
  const tag = target.tagName
  return (
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    target.isContentEditable
  )
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return null
  }
  return value as Record<string, unknown>
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return []
  }
  return value.filter((item): item is string => typeof item === 'string')
}

function asNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value
  }
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function parseArgv(value: string): string[] | null {
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed.split(/\s+/)
}

function ticketTitleFromPayload(
  payload: Record<string, unknown>,
): string | null {
  const direct = asString(payload.ticket_title)
  if (direct) {
    return direct
  }
  const ticket = payload.ticket
  if (typeof ticket === 'string') {
    return ticket
  }
  const ticketRecord = asRecord(ticket)
  if (ticketRecord) {
    return asString(ticketRecord.title)
  }
  return null
}

function parsePlanPayload(
  payload: Record<string, unknown>,
): PlanPayload | null {
  const raw = payload.plan ?? payload
  const parsed = planDisplaySchema.safeParse(raw)
  return parsed.success ? parsed.data : null
}

function prNumberFromPayload(payload: Record<string, unknown>): number | null {
  return asNumber(payload.pr_number) ?? asNumber(payload.number)
}

function githubPrUrlFromPayload(
  payload: Record<string, unknown>,
): string | null {
  for (const key of ['pr_url', 'url', 'html_url']) {
    const value = asString(payload[key])
    if (value && value.startsWith('https://github.com/')) {
      return value
    }
  }
  return null
}

function reviewSummaryFromPayload(
  payload: Record<string, unknown>,
): string | null {
  const direct = asString(payload.review_summary)
  if (direct) {
    return direct
  }
  const review = asRecord(payload.review)
  if (review) {
    return asString(review.feedback) ?? asString(review.summary)
  }
  return null
}

function diffSummaryFromPayload(
  payload: Record<string, unknown>,
): string | null {
  return asString(payload.diff_summary)
}

function escalationReasonFromPayload(
  payload: Record<string, unknown>,
): string | null {
  return asString(payload.reason) ?? asString(payload.source)
}

function escalationFeedbackFromPayload(
  payload: Record<string, unknown>,
): string | null {
  const direct = asString(payload.feedback)
  if (direct) {
    return direct
  }
  const review = asRecord(payload.review)
  if (review) {
    return asString(review.feedback)
  }
  return null
}

async function fetchApprovals(
  mode: InboxMode,
  fetchImpl: typeof globalThis.fetch,
): Promise<ApprovalPage> {
  const response = await api.GET('/api/v1/approvals', {
    params: {
      query:
        mode === 'pending'
          ? { status: 'pending' }
          : { decided: true, order: 'newest', limit: 50 },
    },
    fetch: fetchImpl,
  })
  if (response.error) {
    throw response.error
  }
  return response.data
}

async function decideApproval(
  approvalId: string,
  input: DecideAction & { comment: string | null },
): Promise<void> {
  const body: components['schemas']['DecideRequest'] = {
    decision: input.decision,
    comment: input.comment,
    edited_payload: input.editedPayload ?? null,
    confirm: input.confirm ?? null,
    remember: input.remember ?? false,
  }
  if (input.budgetUsd !== undefined) {
    body.budget_usd = input.budgetUsd
  }
  const { error } = await api.POST('/api/v1/approvals/{approval_id}/decide', {
    fetch: globalThis.fetch,
    params: { path: { approval_id: approvalId } },
    body,
  })
  if (error) {
    throw error
  }
}

function KindBadge({ kind }: { kind: ApprovalKind }) {
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${TONE_CLASSES[KIND_TONES[kind]]}`}
    >
      {kind.replaceAll('_', ' ')}
    </span>
  )
}

function InlineApiError({ error }: { error: unknown }) {
  const message =
    error instanceof ApiError
      ? error.message
      : 'Something went wrong while saving this decision.'
  return (
    <p
      role="alert"
      className="mt-3 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
    >
      {message}
    </p>
  )
}

function ApprovalList({
  items,
  selectedId,
  onSelect,
}: {
  items: ApprovalRead[]
  selectedId: string | null
  onSelect: (index: number) => void
}) {
  return (
    <ul
      data-testid="approval-list"
      className="divide-y divide-gray-200 overflow-y-auto rounded border border-gray-200 dark:divide-gray-800 dark:border-gray-800"
    >
      {items.map((item, index) => {
        const ticketTitle = ticketTitleFromPayload(item.payload_json)
        return (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => onSelect(index)}
              aria-pressed={item.id === selectedId}
              className={`w-full px-3 py-3 text-left ${
                item.id === selectedId
                  ? 'bg-blue-50 dark:bg-blue-950'
                  : 'hover:bg-gray-50 dark:hover:bg-gray-900'
              }`}
            >
              <span className="flex items-center justify-between gap-2">
                <KindBadge kind={item.kind} />
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  {formatRelative(item.created_at)}
                </span>
              </span>
              <span className="mt-1 block truncate text-sm">
                {ticketTitle ?? `Run ${item.run_id}`}
              </span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

type PlanCandidate = { label: string; model: string; plan: PlanPayload }

function crowdFromPayload(payload: Record<string, unknown>): {
  candidates: PlanCandidate[]
  provenance: string[]
  notes: string[]
} {
  const candidates: PlanCandidate[] = []
  if (Array.isArray(payload.candidates)) {
    for (const raw of payload.candidates) {
      const record = asRecord(raw)
      const label = asString(record?.label)
      const model = asString(record?.model)
      const plan = planDisplaySchema.safeParse(record?.plan)
      if (label && model && plan.success) {
        candidates.push({ label, model, plan: plan.data })
      }
    }
  }
  return {
    candidates,
    provenance: asStringArray(payload.provenance),
    notes: asStringArray(payload.notes),
  }
}

/** Wisdom of crowds (doc 19): the panel's plans and what the lead took from each. */
function CrowdDetail({ payload }: { payload: Record<string, unknown> }) {
  const { candidates, provenance, notes } = crowdFromPayload(payload)
  if (
    candidates.length === 0 &&
    provenance.length === 0 &&
    notes.length === 0
  ) {
    return null
  }
  return (
    <section
      aria-labelledby="crowd-heading"
      className="mt-6 rounded border border-gray-200 p-3 dark:border-gray-800"
    >
      <h3 id="crowd-heading" className="text-sm font-semibold">
        How the panel was used
      </h3>
      {provenance.length > 0 ? (
        <ul className="mt-2 list-inside list-disc text-sm">
          {provenance.map((line, index) => (
            <li key={index}>{line}</li>
          ))}
        </ul>
      ) : null}
      {notes.map((note, index) => (
        <p
          key={index}
          className="mt-2 text-sm text-amber-800 dark:text-amber-200"
        >
          {note}
        </p>
      ))}
      <div className="mt-3 space-y-2">
        {candidates.map((candidate) => (
          <details
            key={candidate.label}
            className="rounded border border-gray-200 p-2 dark:border-gray-800"
          >
            <summary className="cursor-pointer text-sm">
              Plan {candidate.label}{' '}
              <code className="font-mono">{candidate.model}</code> (
              {candidate.plan.steps.length}{' '}
              {candidate.plan.steps.length === 1 ? 'step' : 'steps'})
            </summary>
            <p className="mt-2 whitespace-pre-wrap text-sm">
              {candidate.plan.summary}
            </p>
            <ol className="mt-2 list-inside list-decimal text-sm">
              {candidate.plan.steps.map((step, index) => (
                <li key={index}>{step.title}</li>
              ))}
            </ol>
          </details>
        ))}
      </div>
    </section>
  )
}

function PlanKindDetail({
  plan,
  editing,
  busy,
  onEditStart,
  onEditCancel,
  onApproveWithEdits,
}: {
  plan: PlanPayload
  editing: boolean
  busy: boolean
  onEditStart: () => void
  onEditCancel: () => void
  onApproveWithEdits: (plan: PlanPayload) => void
}) {
  const [drafts, setDrafts] = useState<StepDraft[]>(() =>
    plan.steps.map((step) => ({
      title: step.title,
      instructions: step.instructions,
      testCommand: step.test_command ? step.test_command.join(' ') : '',
      files: step.files_likely_touched,
      acceptance: step.acceptance,
      worker: step.worker ?? null,
    })),
  )
  const [validationError, setValidationError] = useState<string | null>(null)

  function updateDraft(index: number, patch: Partial<StepDraft>): void {
    setDrafts((current) =>
      current.map((draft, draftIndex) =>
        draftIndex === index ? { ...draft, ...patch } : draft,
      ),
    )
  }

  function addStep(): void {
    setDrafts((current) => [
      ...current,
      {
        title: '',
        instructions: '',
        testCommand: '',
        files: [],
        acceptance: [],
        worker: null,
      },
    ])
  }

  function removeStep(index: number): void {
    setDrafts((current) =>
      current.filter((_, draftIndex) => draftIndex !== index),
    )
  }

  function cancel(): void {
    setValidationError(null)
    setDrafts(
      plan.steps.map((step) => ({
        title: step.title,
        instructions: step.instructions,
        testCommand: step.test_command ? step.test_command.join(' ') : '',
        files: step.files_likely_touched,
        acceptance: step.acceptance,
        worker: step.worker ?? null,
      })),
    )
    onEditCancel()
  }

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    const editedPlan: PlanPayload = {
      summary: plan.summary,
      risks: plan.risks,
      steps: drafts.map((draft) => ({
        title: draft.title,
        instructions: draft.instructions,
        files_likely_touched: draft.files,
        acceptance: draft.acceptance,
        test_command: parseArgv(draft.testCommand),
        ...(draft.worker ? { worker: draft.worker } : {}),
      })),
    }
    const parsed = planSchema.safeParse(editedPlan)
    if (!parsed.success) {
      setValidationError(parsed.error.issues[0]?.message ?? 'invalid plan')
      return
    }
    setValidationError(null)
    onApproveWithEdits(parsed.data)
  }

  if (!editing) {
    return (
      <div className="space-y-4">
        <div>
          <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
            Summary
          </h3>
          <p className="mt-1 whitespace-pre-wrap">{plan.summary}</p>
        </div>
        <div>
          <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
            Risks
          </h3>
          {plan.risks.length === 0 ? (
            <p className="mt-1">No risks listed.</p>
          ) : (
            <ul className="mt-1 list-inside list-disc">
              {plan.risks.map((risk, index) => (
                <li key={index}>{risk}</li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
            Steps
          </h3>
          <ol className="mt-2 space-y-4">
            {plan.steps.map((step, index) => (
              <li
                key={index}
                className="rounded border border-gray-200 p-3 dark:border-gray-800"
              >
                <p className="font-medium">
                  {index + 1}. {step.title}
                </p>
                <p className="mt-1 whitespace-pre-wrap">{step.instructions}</p>
                <dl className="mt-2 grid gap-1 text-sm">
                  <div>
                    <dt className="text-gray-500 dark:text-gray-400">Files</dt>
                    <dd>{step.files_likely_touched.join(', ') || '—'}</dd>
                  </div>
                  <div>
                    <dt className="text-gray-500 dark:text-gray-400">
                      Acceptance
                    </dt>
                    <dd>{step.acceptance.join(', ') || '—'}</dd>
                  </div>
                  <div>
                    <dt className="text-gray-500 dark:text-gray-400">
                      Test command
                    </dt>
                    <dd>
                      <code className="font-mono">
                        {step.test_command ? step.test_command.join(' ') : '—'}
                      </code>
                    </dd>
                  </div>
                  {step.worker ? (
                    <div>
                      <dt className="text-gray-500 dark:text-gray-400">
                        Worker
                      </dt>
                      <dd>
                        <code className="font-mono">{step.worker}</code>
                      </dd>
                    </div>
                  ) : null}
                </dl>
              </li>
            ))}
          </ol>
        </div>
        <button
          type="button"
          onClick={onEditStart}
          className="rounded border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-700"
        >
          Edit steps
        </button>
      </div>
    )
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="space-y-4">
        {drafts.map((draft, index) => (
          <fieldset
            key={index}
            className="rounded border border-gray-200 p-3 dark:border-gray-800"
          >
            <legend className="px-1 text-sm font-medium">
              Step {index + 1}
            </legend>
            <div className="grid gap-3">
              <div>
                <label
                  htmlFor={`plan-step-${index}-title`}
                  className="block text-sm"
                >
                  Title
                </label>
                <input
                  id={`plan-step-${index}-title`}
                  value={draft.title}
                  onChange={(event) =>
                    updateDraft(index, { title: event.target.value })
                  }
                  autoComplete="off"
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
                />
              </div>
              <div>
                <label
                  htmlFor={`plan-step-${index}-instructions`}
                  className="block text-sm"
                >
                  Instructions
                </label>
                <textarea
                  id={`plan-step-${index}-instructions`}
                  value={draft.instructions}
                  onChange={(event) =>
                    updateDraft(index, { instructions: event.target.value })
                  }
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
                />
              </div>
              <div>
                <label
                  htmlFor={`plan-step-${index}-test-command`}
                  className="block text-sm"
                >
                  Test command
                </label>
                <input
                  id={`plan-step-${index}-test-command`}
                  value={draft.testCommand}
                  onChange={(event) =>
                    updateDraft(index, { testCommand: event.target.value })
                  }
                  autoComplete="off"
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
                />
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  One line, split on whitespace into argv.
                </p>
              </div>
              <div className="text-sm text-gray-500 dark:text-gray-400">
                Files: {draft.files.join(', ') || '—'} · Acceptance:{' '}
                {draft.acceptance.join(', ') || '—'}
              </div>
              <div>
                <button
                  type="button"
                  onClick={() => removeStep(index)}
                  disabled={drafts.length <= 1}
                  className="rounded border border-gray-300 px-2 py-1 text-sm disabled:opacity-50 dark:border-gray-700"
                >
                  Remove step
                </button>
              </div>
            </div>
          </fieldset>
        ))}
      </div>
      <button
        type="button"
        onClick={addStep}
        className="rounded border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-700"
      >
        Add step
      </button>
      {validationError ? (
        <p
          role="alert"
          className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
        >
          {validationError}
        </p>
      ) : null}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={busy}
          className="rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
        >
          {busy ? 'Working…' : 'Approve with edits'}
        </button>
        <button
          type="button"
          onClick={cancel}
          disabled={busy}
          className="rounded border border-gray-300 px-3 py-1.5 dark:border-gray-700"
        >
          Cancel
        </button>
      </div>
    </form>
  )
}

function CommandKindDetail({ payload }: { payload: Record<string, unknown> }) {
  const argv = asStringArray(payload.argv)
  const rememberPrefix = asStringArray(payload.remember_prefix)
  const cwd = asString(payload.cwd)
  const tool = asString(payload.tool)
  const reason = asString(payload.reason)
  return (
    <div className="space-y-4">
      <div className="rounded border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-800 dark:bg-amber-950">
        This command runs inside the sandbox with your approval.
      </div>
      {rememberPrefix.length > 0 ? (
        <p className="text-sm text-gray-600 dark:text-gray-400">
          <strong>Approve + Log</strong> also adds{' '}
          <code className="font-mono">{rememberPrefix.join(' ')}</code> to this
          repository&apos;s allowed commands, so agents run it (with any
          arguments after it) without asking. Edit the list under{' '}
          <strong>Worktrees → Edit commands</strong>.
        </p>
      ) : null}
      <div>
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
          Command
        </h3>
        <ul className="mt-1 flex flex-wrap gap-2">
          {argv.length === 0 ? (
            <li className="text-sm">No argv provided.</li>
          ) : (
            argv.map((element, index) => (
              <li
                key={index}
                className="rounded bg-gray-100 px-2 py-1 font-mono text-sm dark:bg-gray-900"
              >
                <code>{element}</code>
              </li>
            ))
          )}
        </ul>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
          Working directory
        </h3>
        <p className="mt-1">
          <code className="font-mono">{cwd ?? '—'}</code>
        </p>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
          Tool
        </h3>
        <p className="mt-1">{tool ?? '—'}</p>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
          Reason
        </h3>
        <p className="mt-1 whitespace-pre-wrap">{reason ?? '—'}</p>
      </div>
    </div>
  )
}

function PrOpenKindDetail({ approval }: { approval: ApprovalRead }) {
  const reviewSummary = reviewSummaryFromPayload(approval.payload_json)
  const diffSummary = diffSummaryFromPayload(approval.payload_json)
  // The backend's `Review` is {verdict, feedback, issues} (gaia/domain/plans.py).
  const review = asRecord(approval.payload_json.review)
  const verdict = review ? asString(review.verdict) : null
  const issues = review ? asStringArray(review.issues) : []
  return (
    <div className="space-y-4">
      {verdict ? (
        <p className="text-sm">
          Lead verdict: <span className="font-medium">{verdict}</span>
        </p>
      ) : null}
      {issues.length > 0 ? (
        <div>
          <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
            Issues raised
          </h3>
          <ul className="mt-1 list-inside list-disc">
            {issues.map((issue, index) => (
              <li key={index}>{issue}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <div>
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
          Review summary
        </h3>
        <p className="mt-1 whitespace-pre-wrap">
          {reviewSummary ?? 'No review summary in the payload.'}
        </p>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
          Diff summary
        </h3>
        <p className="mt-1 whitespace-pre-wrap">
          {diffSummary ?? 'No diff summary in the payload.'}
        </p>
      </div>
      <Link
        to={`/runs/${approval.run_id}?tab=diff`}
        className="inline-block text-blue-600 hover:underline dark:text-blue-400"
      >
        View diff
      </Link>
    </div>
  )
}

function MergeKindDetail({ payload }: { payload: Record<string, unknown> }) {
  const prNumber = prNumberFromPayload(payload)
  const prUrl = githubPrUrlFromPayload(payload)
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
          Pull request
        </h3>
        <p className="mt-1">
          {prNumber === null ? (
            'Unknown PR number.'
          ) : prUrl ? (
            <a
              href={prUrl}
              target="_blank"
              rel="noreferrer noopener"
              className="text-blue-600 hover:underline dark:text-blue-400"
            >
              #{prNumber}
            </a>
          ) : (
            <span>#{prNumber}</span>
          )}
        </p>
      </div>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        Merging always requires typing the confirmation phrase.
      </p>
    </div>
  )
}

function BudgetKindDetail({
  payload,
  budgetInput,
  onBudgetChange,
}: {
  payload: Record<string, unknown>
  budgetInput: string
  onBudgetChange: (value: string) => void
}) {
  const scope = asString(payload.scope) ?? 'run'

  if (scope === 'daily') {
    const spentToday = asNumber(payload.spent_today_usd)
    const dailyBudget = asNumber(payload.daily_budget_usd)
    return (
      <div className="space-y-4">
        <dl className="grid gap-2 sm:grid-cols-2">
          <div>
            <dt className="text-sm font-semibold text-gray-500 dark:text-gray-400">
              Spent today
            </dt>
            <dd className="mt-1 text-lg">{formatUsd(spentToday)}</dd>
          </div>
          <div>
            <dt className="text-sm font-semibold text-gray-500 dark:text-gray-400">
              Daily cap
            </dt>
            <dd className="mt-1 text-lg">{formatUsd(dailyBudget)}</dd>
          </div>
        </dl>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          The daily cap is a hard limit. Approve after raising{' '}
          <code className="font-mono">GAIA_DAILY_BUDGET_USD</code> or after
          midnight UTC to try again; reject to cancel the run.
        </p>
      </div>
    )
  }

  const spent = asNumber(payload.cost_usd)
  const budget = asNumber(payload.budget_usd)
  return (
    <div className="space-y-4">
      <dl className="grid gap-2 sm:grid-cols-2">
        <div>
          <dt className="text-sm font-semibold text-gray-500 dark:text-gray-400">
            Spent
          </dt>
          <dd className="mt-1 text-lg">{formatUsd(spent)}</dd>
        </div>
        <div>
          <dt className="text-sm font-semibold text-gray-500 dark:text-gray-400">
            Budget
          </dt>
          <dd className="mt-1 text-lg">{formatUsd(budget)}</dd>
        </div>
      </dl>
      <div>
        <label htmlFor="budget-usd" className="block text-sm">
          New budget (USD, optional)
        </label>
        <input
          id="budget-usd"
          value={budgetInput}
          onChange={(event) => onBudgetChange(event.target.value)}
          inputMode="decimal"
          autoComplete="off"
          placeholder="Leave blank to grant one more run budget"
          className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
        />
      </div>
    </div>
  )
}

function EscalationKindDetail({
  payload,
}: {
  payload: Record<string, unknown>
}) {
  const reason = escalationReasonFromPayload(payload)
  const feedback = escalationFeedbackFromPayload(payload)
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
          Reason
        </h3>
        <p className="mt-1 whitespace-pre-wrap">{reason ?? '—'}</p>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400">
          Feedback
        </h3>
        <p className="mt-1 whitespace-pre-wrap">{feedback ?? '—'}</p>
      </div>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        Approve retries the step. Reject stops the run.
      </p>
    </div>
  )
}

function ApprovalDetail({
  approval,
  comment,
  busy,
  error,
  onCommentChange,
  onDecide,
  onOpenMergeDialog,
}: {
  approval: ApprovalRead
  comment: string
  busy: boolean
  error: unknown
  onCommentChange: (value: string) => void
  onDecide: (action: DecideAction) => void
  onOpenMergeDialog: () => void
}) {
  const [budgetInput, setBudgetInput] = useState('')
  const [planEditing, setPlanEditing] = useState(false)

  function approve(): void {
    if (approval.kind === 'merge') {
      onOpenMergeDialog()
      return
    }
    if (approval.kind === 'budget') {
      const scope = asString(approval.payload_json.scope) ?? 'run'
      onDecide({
        decision: 'approve',
        // Daily scope changes no run budget; the user is approving "the cap
        // changed or it is a new UTC day". Do not send budget_usd at all.
        budgetUsd:
          scope === 'daily'
            ? undefined
            : budgetInput.trim()
              ? budgetInput.trim()
              : null,
      })
      return
    }
    onDecide({ decision: 'approve' })
  }

  function reject(): void {
    onDecide({ decision: 'reject' })
  }

  // What "Approve + Log" adds to the repository's allowed commands. Gaia
  // computes it when the agent asks; it is absent for a command that cannot be
  // allowed by prefix (an inline `node -e` script, a path to a binary).
  const rememberPrefix =
    approval.kind === 'command'
      ? asStringArray(approval.payload_json.remember_prefix)
      : []

  let content
  switch (approval.kind) {
    case 'plan': {
      const plan = parsePlanPayload(approval.payload_json)
      content = plan ? (
        <>
          <PlanKindDetail
            plan={plan}
            editing={planEditing}
            busy={busy}
            onEditStart={() => setPlanEditing(true)}
            onEditCancel={() => setPlanEditing(false)}
            onApproveWithEdits={(editedPlan) =>
              onDecide({ decision: 'edit', editedPayload: editedPlan })
            }
          />
          <CrowdDetail payload={approval.payload_json} />
        </>
      ) : (
        <p className="text-sm">This plan approval has an invalid payload.</p>
      )
      break
    }
    case 'command':
      content = <CommandKindDetail payload={approval.payload_json} />
      break
    case 'pr_open':
      content = <PrOpenKindDetail approval={approval} />
      break
    case 'merge':
      content = <MergeKindDetail payload={approval.payload_json} />
      break
    case 'budget':
      content = (
        <BudgetKindDetail
          payload={approval.payload_json}
          budgetInput={budgetInput}
          onBudgetChange={setBudgetInput}
        />
      )
      break
    case 'escalation':
      content = <EscalationKindDetail payload={approval.payload_json} />
      break
  }

  return (
    <div data-testid="approval-detail" className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <KindBadge kind={approval.kind} />
        <span className="text-xs text-gray-500 dark:text-gray-400">
          {formatRelative(approval.created_at)}
        </span>
      </div>
      {content}
      <div className="border-t border-gray-200 pt-4 dark:border-gray-800">
        <label htmlFor="approval-comment" className="block text-sm">
          Comment (optional)
        </label>
        <textarea
          id="approval-comment"
          value={comment}
          onChange={(event) => onCommentChange(event.target.value)}
          className="mt-1 w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-950"
        />
        {error ? <InlineApiError error={error} /> : null}
        {approval.status === 'pending' &&
        (approval.kind !== 'plan' || !planEditing) ? (
          <div className="mt-4 flex gap-2">
            <button
              type="button"
              data-testid="approve-button"
              onClick={approve}
              disabled={busy}
              className="rounded bg-emerald-600 px-3 py-1.5 text-white disabled:opacity-50"
            >
              {busy ? 'Working…' : 'Approve'}
            </button>
            {rememberPrefix.length > 0 ? (
              <button
                type="button"
                data-testid="approve-log-button"
                onClick={() =>
                  onDecide({ decision: 'approve', remember: true })
                }
                disabled={busy}
                title={`Approve, and let agents run "${rememberPrefix.join(' ')}" in this repository without asking`}
                className="rounded border border-blue-600 px-3 py-1.5 text-blue-700 disabled:opacity-50 dark:text-blue-300"
              >
                {busy ? 'Working…' : 'Approve + Log'}
              </button>
            ) : null}
            <button
              type="button"
              data-testid="reject-button"
              onClick={reject}
              disabled={busy}
              className="rounded bg-red-600 px-3 py-1.5 text-white disabled:opacity-50"
            >
              {busy ? 'Working…' : 'Reject'}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  )
}

export function ApprovalsView() {
  const [mode, setMode] = useState<InboxMode>('pending')
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [comment, setComment] = useState('')
  const [mergeDialogOpen, setMergeDialogOpen] = useState(false)
  const queryClient = useQueryClient()

  const { data, error, isError, isLoading, refetch } = useQuery({
    queryKey:
      mode === 'pending'
        ? ['approvals', { status: 'pending' }]
        : ['approvals', { decided: true }],
    queryFn: () => fetchApprovals(mode, fetch),
  })

  // A decided item leaves the list at once: graph gates are applied
  // asynchronously (202), so a refetch can still show it as pending for a
  // moment. Keeping the index then lands on the next item (lead review).
  const [decidedIds, setDecidedIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  )
  const items = (data?.items ?? []).filter(
    (item) => mode !== 'pending' || !decidedIds.has(item.id),
  )
  const safeSelectedIndex =
    items.length === 0 ? 0 : Math.min(selectedIndex, items.length - 1)
  const selected = items[safeSelectedIndex] ?? null

  const decisionMutation = useMutation({
    mutationFn: ({
      approvalId,
      input,
    }: {
      approvalId: string
      input: DecideAction & { comment: string | null }
    }) => decideApproval(approvalId, input),
    onSuccess: (_data, { approvalId }) => {
      setMergeDialogOpen(false)
      setComment('')
      setDecidedIds((current) => new Set(current).add(approvalId))
      void queryClient.invalidateQueries({ queryKey: ['approvals'] })
      void queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  const handleDecide = useCallback(
    (action: DecideAction) => {
      if (!selected) {
        return
      }
      decisionMutation.mutate({
        approvalId: selected.id,
        input: {
          ...action,
          comment: comment.trim() ? comment.trim() : null,
        },
      })
    },
    [selected, comment, decisionMutation],
  )

  function selectIndex(next: number): void {
    decisionMutation.reset()
    setSelectedIndex(next)
    setComment('')
  }

  function selectMode(next: InboxMode): void {
    decisionMutation.reset()
    setMode(next)
    setSelectedIndex(0)
    setComment('')
    setMergeDialogOpen(false)
  }

  const mergePrNumber =
    selected?.kind === 'merge'
      ? prNumberFromPayload(selected.payload_json)
      : null

  useEffect(() => {
    let gPressedAt = 0

    function onKeyDown(event: KeyboardEvent): void {
      if (event.ctrlKey || event.metaKey || event.altKey) {
        return
      }
      if (isEditable(event.target)) {
        return
      }
      if (mergeDialogOpen) {
        return
      }

      if (event.key === 'g') {
        gPressedAt = Date.now()
        return
      }
      if (event.key === 'a' && Date.now() - gPressedAt <= SEQUENCE_TIMEOUT_MS) {
        // The shell's `g a` shortcut navigates to /approvals. Don't also
        // approve an item while the user is issuing that navigation.
        gPressedAt = 0
        return
      }
      gPressedAt = 0

      if (event.key === 'j') {
        event.preventDefault()
        if (items.length === 0) {
          return
        }
        decisionMutation.reset()
        setComment('')
        setSelectedIndex((index) =>
          Math.min(Math.min(index, items.length - 1) + 1, items.length - 1),
        )
        return
      }
      if (event.key === 'k') {
        event.preventDefault()
        if (items.length === 0) {
          return
        }
        decisionMutation.reset()
        setComment('')
        setSelectedIndex((index) =>
          Math.max(Math.min(index, items.length - 1) - 1, 0),
        )
        return
      }
      if (event.key === 'a') {
        event.preventDefault()
        if (
          !selected ||
          selected.status !== 'pending' ||
          selected.kind === 'merge'
        ) {
          return
        }
        handleDecide({ decision: 'approve' })
        return
      }
      if (event.key === 'r') {
        event.preventDefault()
        if (!selected || selected.status !== 'pending') {
          return
        }
        handleDecide({ decision: 'reject' })
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [items.length, selected, mergeDialogOpen, handleDecide, decisionMutation])

  return (
    <section>
      <PageHeader
        title="Approvals"
        actions={
          <div
            role="group"
            aria-label="Approval filter"
            className="flex rounded border border-gray-300 p-0.5 dark:border-gray-700"
          >
            <button
              type="button"
              aria-pressed={mode === 'pending'}
              onClick={() => selectMode('pending')}
              className={`rounded px-3 py-1.5 text-sm ${
                mode === 'pending'
                  ? 'bg-blue-600 text-white'
                  : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
              }`}
            >
              Pending
            </button>
            <button
              type="button"
              aria-pressed={mode === 'decided'}
              onClick={() => selectMode('decided')}
              className={`rounded px-3 py-1.5 text-sm ${
                mode === 'decided'
                  ? 'bg-blue-600 text-white'
                  : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
              }`}
            >
              Decided
            </button>
          </div>
        }
      />

      {isLoading ? <LoadingState label="Loading approvals…" /> : null}

      {isError ? (
        <ErrorState error={error} onRetry={() => void refetch()} />
      ) : null}

      {data && items.length === 0 ? (
        <EmptyState
          title={
            mode === 'pending' ? 'Nothing needs you' : 'No decided approvals'
          }
        >
          {mode === 'pending'
            ? 'When an agent needs a decision, it will appear here.'
            : 'Recent decisions will appear here once they are made.'}
        </EmptyState>
      ) : null}

      {data && items.length > 0 ? (
        <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
          <ApprovalList
            items={items}
            selectedId={selected?.id ?? null}
            onSelect={selectIndex}
          />
          {selected ? (
            <ApprovalDetail
              key={`${mode}-${selected.id}`}
              approval={selected}
              comment={comment}
              busy={decisionMutation.isPending}
              error={decisionMutation.error}
              onCommentChange={setComment}
              onDecide={handleDecide}
              onOpenMergeDialog={() => setMergeDialogOpen(true)}
            />
          ) : (
            <EmptyState title="Select an approval">
              Choose an item from the list to review and decide it.
            </EmptyState>
          )}
        </div>
      ) : null}

      <ConfirmDialog
        open={mergeDialogOpen}
        title={`Approve merge of PR #${mergePrNumber ?? '?'}`}
        destructive
        requireText={
          mergePrNumber === null ? undefined : `merge ${mergePrNumber}`
        }
        confirmLabel="Approve merge"
        busy={decisionMutation.isPending}
        onConfirm={() => {
          if (mergePrNumber === null) {
            return
          }
          handleDecide({
            decision: 'approve',
            confirm: `merge ${mergePrNumber}`,
          })
        }}
        onCancel={() => setMergeDialogOpen(false)}
      >
        Merging is final. Type{' '}
        <code className="font-mono">merge {mergePrNumber ?? '?'}</code> to
        confirm.
      </ConfirmDialog>
    </section>
  )
}
