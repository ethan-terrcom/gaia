import { PageShell } from '../components/ui/PageShell'
import { TicketsView } from '../views/tickets/TicketsView'

export default function Tickets() {
  return (
    <PageShell wide>
      <TicketsView />
    </PageShell>
  )
}
