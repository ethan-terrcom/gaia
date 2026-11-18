import { useQuery } from '@tanstack/react-query'
import {
  ArrowLeft,
  CheckSquare,
  Copy,
  ExternalLink,
  FileCode2,
  GitPullRequest,
} from 'lucide-react'
import { Link, useParams } from 'react-router'
import { api } from '../../api/client'
import type { components } from '../../api/schema'
import { formatRelative } from '../../components/format'
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
} from '../../components/States'
import { StatusBadge } from '../../components/StatusBadge'
import { Button } from '../../components/ui/Button'
import { Panel, PanelBody, PanelHeader } from '../../components/ui/Panel'
import { parseUnifiedDiff } from '../run-detail/events'

type PullRequestDetail = components['schemas']['PullRequestDetail']

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

async function fetchPullRequest(id: string): Promise<PullRequestDetail> {
  const { data, error } = await api.GET(
    '/api/v1/pull-requests/{pull_request_id}',
    {
      params: { path: { pull_request_id: id } },
      fetch,
    },
  )
  if (error) {
    throw error
  }
  return data
}

async function fetchRunDiff(runId: string): Promise<string> {
  const { data, error } = await api.GET('/api/v1/runs/{run_id}/diff', {
    fetch,
    parseAs: 'text',
    params: { path: { run_id: runId } },
  })
  if (error) {
    throw error
  }
  return data
}

function DiffPanels({ diff }: { diff: string }) {
  const files = parseUnifiedDiff(diff)
  if (files.length === 0) {
    return (
      <pre className="overflow-x-auto rounded-[var(--radius-lg)] bg-[var(--color-surface-muted)] p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap">
        {diff || 'No diff.'}
      </pre>
    )
  }
  return (
    <div className="grid gap-4">
      {files.map((file) => (
        <Panel key={file.id}>
          <PanelHeader title={file.title} />
          <PanelBody className="p-0">
            <pre className="overflow-x-auto p-4 font-mono text-xs leading-relaxed">
              {file.lines.map((line, index) => {
                const tone =
                  line.startsWith('+') && !line.startsWith('+++')
                    ? 'bg-emerald-50 text-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-100'
                    : line.startsWith('-') && !line.startsWith('---')
                      ? 'bg-red-50 text-red-900 dark:bg-red-950/40 dark:text-red-100'
                      : line.startsWith('@@')
                        ? 'bg-sky-50 text-sky-900 dark:bg-sky-950/40 dark:text-sky-100'
                        : ''
                return (
                  <div key={`${file.id}-${index}`} className={tone}>
                    {line}
                  </div>
                )
              })}
            </pre>
          </PanelBody>
        </Panel>
      ))}
    </div>
  )
}

export function PullRequestDetailView() {
  const { id = '' } = useParams()
  const detailQuery = useQuery({
    queryKey: ['pull-requests', id],
    queryFn: () => fetchPullRequest(id),
    enabled: id !== '',
  })
  const pr = detailQuery.data
  const diffQuery = useQuery({
    queryKey: ['run-diff', pr?.run_id],
    queryFn: () => fetchRunDiff(pr!.run_id),
    enabled: Boolean(pr?.run_id),
  })

  if (detailQuery.isLoading) {
    return <LoadingState label="Loading pull request…" />
  }
  if (detailQuery.isError) {
    return (
      <ErrorState
        error={detailQuery.error}
        onRetry={() => void detailQuery.refetch()}
      />
    )
  }
  if (!pr) {
    return <EmptyState title="Pull request not found." />
  }

  const pendingApprovals = pr.pending_approvals ?? []
  const mergeApproval = pendingApprovals.find(
    (approval) => approval.kind === 'merge',
  )
  const openApproval = pendingApprovals.find(
    (approval) => approval.kind === 'pr_open',
  )

  return (
    <section className="grid gap-6">
      <div>
        <Link
          to="/prs"
          className="mb-3 inline-flex items-center gap-1.5 text-sm text-[var(--color-ink-muted)] hover:text-[var(--color-ink)]"
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          All pull requests
        </Link>
        <PageHeader
          title={`PR #${pr.number}`}
          description={pr.ticket_title}
          actions={
            <>
              {isGitHubUrl(pr.url) ? (
                <a
                  href={pr.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1.5 text-sm text-[var(--color-ink-muted)] hover:text-[var(--color-accent-ink)]"
                >
                  GitHub
                  <ExternalLink className="size-3.5" aria-hidden />
                </a>
              ) : null}
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void navigator.clipboard.writeText(pr.url)}
              >
                <Copy className="size-3.5" aria-hidden />
                Copy URL
              </Button>
            </>
          }
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Panel className="lg:col-span-2">
          <PanelHeader title="Status" />
          <PanelBody className="grid gap-4 sm:grid-cols-2">
            <div>
              <p className="text-xs font-medium tracking-wide text-[var(--color-ink-muted)] uppercase">
                PR
              </p>
              <div className="mt-1.5">
                <StatusBadge status={pr.state} />
              </div>
            </div>
            <div>
              <p className="text-xs font-medium tracking-wide text-[var(--color-ink-muted)] uppercase">
                CI
              </p>
              <div className="mt-1.5">
                <StatusBadge status={pr.ci_status} />
              </div>
            </div>
            <div>
              <p className="text-xs font-medium tracking-wide text-[var(--color-ink-muted)] uppercase">
                Review
              </p>
              <div className="mt-1.5">
                <StatusBadge status={pr.review_state} />
              </div>
            </div>
            <div>
              <p className="text-xs font-medium tracking-wide text-[var(--color-ink-muted)] uppercase">
                Run
              </p>
              <div className="mt-1.5 flex items-center gap-2">
                <StatusBadge status={pr.run_status} />
                <Link
                  to={`/runs/${pr.run_id}`}
                  className="text-sm text-[var(--color-accent-ink)] hover:underline"
                >
                  Open run
                </Link>
              </div>
            </div>
            <div className="sm:col-span-2">
              <p className="text-xs font-medium tracking-wide text-[var(--color-ink-muted)] uppercase">
                Meta
              </p>
              <p className="mt-1.5 text-sm text-[var(--color-ink-muted)]">
                {pr.repo_name}
                {pr.head_sha ? (
                  <>
                    {' '}
                    ·{' '}
                    <span className="font-mono text-xs">
                      {pr.head_sha.slice(0, 7)}
                    </span>
                  </>
                ) : null}{' '}
                · opened {formatRelative(pr.created_at)}
              </p>
            </div>
          </PanelBody>
        </Panel>

        <Panel>
          <PanelHeader title="Actions" />
          <PanelBody className="grid gap-3">
            <Link
              to={`/runs/${pr.run_id}?tab=diff`}
              className="inline-flex items-center gap-2 text-sm font-medium text-[var(--color-accent-ink)] hover:underline"
            >
              <FileCode2 className="size-4" aria-hidden />
              View run diff
            </Link>
            {openApproval ? (
              <Link
                to={`/approvals?focus=${openApproval.id}`}
                className="inline-flex items-center gap-2 text-sm font-medium text-[var(--color-accent-ink)] hover:underline"
              >
                <CheckSquare className="size-4" aria-hidden />
                Approve opening PR
              </Link>
            ) : null}
            {mergeApproval ? (
              <Link
                to={`/approvals?focus=${mergeApproval.id}`}
                className="inline-flex items-center gap-2 text-sm font-medium text-[var(--color-accent-ink)] hover:underline"
              >
                <GitPullRequest className="size-4" aria-hidden />
                Review merge gate
              </Link>
            ) : null}
            {pr.worktree_path ? (
              <div className="rounded-[var(--radius-lg)] bg-[var(--color-surface-muted)] p-3 text-sm">
                <p className="font-medium text-[var(--color-ink)]">
                  {pr.worktree_branch ?? 'Worktree'}
                </p>
                <p className="mt-1 break-all font-mono text-xs text-[var(--color-ink-muted)]">
                  {pr.worktree_path}
                </p>
                <a
                  href={`vscode://file${encodePathPerSegment(pr.worktree_path)}`}
                  className="mt-2 inline-block text-[var(--color-accent-ink)] hover:underline"
                >
                  Open in VS Code
                </a>
              </div>
            ) : null}
            {pendingApprovals.length === 0 ? (
              <p className="text-sm text-[var(--color-ink-muted)]">
                No pending PR gates for this run.
              </p>
            ) : null}
          </PanelBody>
        </Panel>
      </div>

      <div>
        <h2 className="mb-3 text-sm font-semibold text-[var(--color-ink)]">
          Diff
        </h2>
        {diffQuery.isLoading ? <LoadingState label="Loading diff…" /> : null}
        {diffQuery.isError ? (
          <ErrorState
            error={diffQuery.error}
            onRetry={() => void diffQuery.refetch()}
          />
        ) : null}
        {diffQuery.data !== undefined ? (
          <DiffPanels diff={diffQuery.data} />
        ) : null}
      </div>
    </section>
  )
}
