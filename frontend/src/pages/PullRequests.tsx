import { PageShell } from '../components/ui/PageShell'
import { PullRequestsView } from '../views/pull-requests/PullRequests'

export default function PullRequests() {
  return (
    <PageShell>
      <PullRequestsView />
    </PageShell>
  )
}
