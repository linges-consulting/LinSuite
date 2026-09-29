import { Receipt } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'

/**
 * Issued invoices (spec #95 user stories 6-14): service and retail, switchable, filtered by
 * date/status/client, paginated. This file is that later ticket's slot — empty until it lands.
 */
export function InvoicesTab() {
  return (
    <EmptyState
      icon={Receipt}
      title="Invoices are not built yet"
      description="Issued service and retail invoices will list here, filterable by date, status and client."
    />
  )
}
