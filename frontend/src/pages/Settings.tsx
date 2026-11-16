import { PageHeader } from '../components/States'

export default function Settings() {
  return (
    <section>
      <PageHeader title="Settings" />
      <h2 className="mb-2 font-semibold">Keyboard navigation</h2>
      <ul>
        <li>
          <kbd>g</kbd> then <kbd>d</kbd> — Dashboard
        </li>
        <li>
          <kbd>g</kbd> then <kbd>t</kbd> — Tickets
        </li>
        <li>
          <kbd>g</kbd> then <kbd>r</kbd> — Runs
        </li>
        <li>
          <kbd>g</kbd> then <kbd>a</kbd> — Approvals
        </li>
        <li>
          <kbd>g</kbd> then <kbd>p</kbd> — Pull requests
        </li>
        <li>
          <kbd>g</kbd> then <kbd>w</kbd> — Worktrees
        </li>
        <li>
          <kbd>g</kbd> then <kbd>s</kbd> — Settings
        </li>
        <li>
          <kbd>/</kbd> — focus search
        </li>
      </ul>
    </section>
  )
}
