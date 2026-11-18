import { PageShell } from '../components/ui/PageShell'
import { PullRequestDetailView } from '../views/pull-requests/PullRequestDetailView'

export default function PullRequestDetail() {
  return (
    <PageShell wide>
      <PullRequestDetailView />
    </PageShell>
  )
}
