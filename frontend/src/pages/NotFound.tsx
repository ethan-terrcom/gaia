import { PageShell } from '../components/ui/PageShell'

export default function NotFound() {
  return (
    <PageShell>
      <section>
        <h1 className="text-[22px] font-semibold tracking-[-0.03em]">
          Not found
        </h1>
        <p className="mt-2 text-[13px] text-[var(--color-ink-muted)]">
          The page you asked for does not exist.
        </p>
      </section>
    </PageShell>
  )
}
