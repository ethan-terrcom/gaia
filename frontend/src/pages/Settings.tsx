import { PageHeader } from '../components/States'
import { PageShell } from '../components/ui/PageShell'
import { AgentModeSettings } from '../views/settings/AgentModeSettings'
import { AutoModeSettings } from '../views/settings/AutoModeSettings'

export default function Settings() {
  return (
    <PageShell>
      <section>
        <PageHeader
          title="Settings"
          description="Defaults for agents, auto-mode, and keyboard shortcuts."
        />
        <AgentModeSettings />
        <AutoModeSettings />
        <h2 className="mt-8 mb-3 text-[13px] font-medium text-[var(--color-ink-muted)]">
          Keyboard navigation
        </h2>
        <ul className="grid gap-1.5 text-[13px] text-[var(--color-ink-muted)]">
          <li>
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              g
            </kbd>{' '}
            then{' '}
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              d
            </kbd>{' '}
            — Dashboard
          </li>
          <li>
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              g
            </kbd>{' '}
            then{' '}
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              t
            </kbd>{' '}
            — Tickets
          </li>
          <li>
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              g
            </kbd>{' '}
            then{' '}
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              r
            </kbd>{' '}
            — Runs
          </li>
          <li>
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              g
            </kbd>{' '}
            then{' '}
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              a
            </kbd>{' '}
            — Approvals
          </li>
          <li>
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              g
            </kbd>{' '}
            then{' '}
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              p
            </kbd>{' '}
            — Pull requests
          </li>
          <li>
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              g
            </kbd>{' '}
            then{' '}
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              w
            </kbd>{' '}
            — Worktrees
          </li>
          <li>
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              g
            </kbd>{' '}
            then{' '}
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              s
            </kbd>{' '}
            — Settings
          </li>
          <li>
            <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-ink)]">
              /
            </kbd>{' '}
            — focus search
          </li>
        </ul>
      </section>
    </PageShell>
  )
}
